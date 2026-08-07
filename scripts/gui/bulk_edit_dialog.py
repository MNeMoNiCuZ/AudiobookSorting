"""Edit several books at once, in a grid.

Setting one title across twelve rows is meaningless - every book has its own title -
so multi-row editing is not "type one value, write it everywhere". It is a small
spreadsheet: the selected books down the side, the four identity fields across, and
keys that let you type through it without touching the mouse.

Navigation, as asked for:
    Enter        commit and move down - to the same field on the next book
    Shift+Enter  commit and move up
    Tab          commit and move right - to the next field on this book
    Shift+Tab    commit and move left
    Ctrl+Z       undo the last edit made in here, one step at a time

Enter going down is the point: a column is one *kind* of value, so working down one
is working through the same field on successive books, which is what fixing a series
actually consists of.

Undo in here is local and complete: every edit made in this dialog can be walked back
inside it, and nothing that happens in here reaches the main window's undo history
until Apply - at which point the whole session lands as a single step, because that
is what it was.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtGui import QKeyEvent, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemDelegate, QDialog, QDialogButtonBox, QHeaderView, QLabel, QLayout,
    QLayoutItem, QMenu, QPushButton, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

from ..models import BookEntry
from ..settings import display_path
from .theme import (ACCENT, BG_BASE, BG_RAISED, FIELD, FIELD_BORDER, FIELD_HOVER,
                    RADIUS, TEXT, TEXT_DIM, TEXT_FAINT)

# The file column is first and read-only: it is what identifies the row, so it is a
# real, resizable, titled column rather than a vertical header you cannot drag.
FIELDS = [('file', 'FILE'), ('author', 'AUTHOR'), ('series', 'SERIES'),
          ('series_index', '#'), ('title', 'TITLE')]

EDITABLE = [column for column, (name, _) in enumerate(FIELDS) if name != 'file']
DEFAULT_WIDTHS = [360, 220, 200, 60, 300]

# How the leftover width is shared out, matching the main table exactly: File takes
# the bulk of it because it holds the longest strings, Title about half of that,
# Author and Series a little. The number column never stretches - it holds "12".
STRETCH = {0: 1.0, 1: 0.15, 2: 0.15, 4: 0.5}

SETTING_WINDOW = 'AO_UI_GRID_WINDOW'
SETTING_COLUMNS = 'AO_UI_GRID_COLUMNS'


class _FlowLayout(QLayout):
    """Lays widgets out in a centred row, wrapping to the next line when they run out.

    The one property that matters here: it never makes a child narrower than the child
    says it needs to be. A QHBoxLayout squeezes its children to fit the window, and a
    squeezed QPushButton elides its own text - which is how a button whose entire job
    is to show you a name ended up showing you half of one.
    """

    def __init__(self, spacing: int = 10):
        super().__init__()
        self._items: List[QLayoutItem] = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    # QLayout's required plumbing.
    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._lay_out(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._lay_out(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        # As wide as the widest single button - below that there is nowhere left to
        # wrap to, and the text would have to be cut.
        # sizeHint, not minimumSize: the hint is what the styled button actually needs
        # to paint its label, and it is what _lay_out gives every item.
        width = max((item.sizeHint().width() for item in self._items), default=0)
        height = self.heightForWidth(width) if self._items else 0
        return QSize(width, height)

    def _lay_out(self, rect: QRect, apply: bool) -> int:
        """Place the items (or just measure them); returns the height used."""
        x, y = rect.x(), rect.y()
        line_height = 0
        line: List[QLayoutItem] = []

        def flush() -> None:
            """Centre the line just finished, then start the next one."""
            nonlocal x, y, line_height, line
            if apply and line:
                used = sum(item.sizeHint().width() for item in line)
                used += self._spacing * (len(line) - 1)
                left = rect.x() + max(0, (rect.width() - used) // 2)
                for item in line:
                    size = item.sizeHint()
                    item.setGeometry(QRect(left, y, size.width(), line_height))
                    left += size.width() + self._spacing
            y += line_height + (self._spacing if line else 0)
            x, line_height, line = rect.x(), 0, []

        for item in self._items:
            size = item.sizeHint()
            if line and x + size.width() > rect.x() + rect.width():
                flush()
            line.append(item)
            x += size.width() + self._spacing
            line_height = max(line_height, size.height())
        flush()

        return y - rect.y() - self._spacing


class _NumberItem(QTableWidgetItem):
    """A book number that sorts as a number: 2 before 10, and blanks last.

    A bundled omnibus holds a range - "1-3" - which sorts on where it starts, which is
    where it belongs in the series.
    """

    def _key(self) -> float:
        text = self.text().strip().split('-')[0].strip().replace(',', '.')
        try:
            return float(text)
        except ValueError:
            return float('inf')

    def __lt__(self, other) -> bool:
        if isinstance(other, _NumberItem):
            mine, theirs = self._key(), other._key()
            if mine != theirs:
                return mine < theirs
            return self.text() < other.text()
        return super().__lt__(other)


class _Grid(QTableWidget):
    """A table whose Enter/Tab keys move the way the docstring above describes."""

    # Set by the dialog that owns it, right after construction. Not `parent()`: the
    # grid's parent is whatever widget Qt reparents it to when it joins a layout.
    dialog: 'BulkEditDialog'

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._commit()
            self._step(-1 if shift else 1, 0)      # down, or up with Shift
            event.accept()
            return
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self._commit()
            self._step(0, -1 if (shift or key == Qt.Key.Key_Backtab) else 1)
            event.accept()
            return

        # Delete, Ctrl+C and Ctrl+V reach here only when no editor is open - an open
        # editor is a QLineEdit child with the focus, and it keeps its own three.
        control = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        dialog = self.dialog
        if key == Qt.Key.Key_Delete and not control:
            dialog._clear_selected_cells()
            event.accept()
            return
        if control and key == Qt.Key.Key_C:
            dialog._copy_selected_cells()
            event.accept()
            return
        if control and key == Qt.Key.Key_V:
            dialog._paste_into_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def _commit(self) -> None:
        """Close the open editor, keeping what was typed.

        `closePersistentEditor` was the wrong call - these are transient editors, and
        closing them that way discarded the keystrokes. `commitData` writes the
        editor's value into the model first, which is the whole point.
        """
        if self.state() != QTableWidget.State.EditingState:
            return
        editor = self.focusWidget()
        if editor is not None and editor is not self:
            self.commitData(editor)
            self.closeEditor(editor,
                             QAbstractItemDelegate.EndEditHint.NoHint)

    def _step(self, rows: int, columns: int) -> None:
        """Move the cursor, wrapping at the edges so long runs never dead-end.

        Movement is over the *editable* columns only. The file column is along for
        identification; tabbing into a cell that cannot be typed in is a dead key.
        """
        total_rows = self.rowCount()
        if not total_rows or not EDITABLE:
            return

        row = self.currentRow()
        try:
            column = EDITABLE.index(self.currentColumn())
        except ValueError:
            column = 0

        position = row * len(EDITABLE) + column
        position += rows * len(EDITABLE) + columns
        position %= total_rows * len(EDITABLE)

        self.setCurrentCell(position // len(EDITABLE),
                            EDITABLE[position % len(EDITABLE)])
        self.editItem(self.currentItem())


class BulkEditDialog(QDialog):
    """The grid, plus the fill-down conveniences that a grid still can't do."""

    def __init__(self, entries: List[BookEntry], parent=None, settings=None):
        super().__init__(parent)
        self.entries = list(entries)
        self.settings = settings

        # (row, column) -> text, as the grid currently stands. The undo stack is
        # built by diffing against this, so it has to be kept in step with every
        # write - including the ones undo itself makes.
        self._current: Dict[Tuple[int, int], str] = {}
        self._undo: List[List[Tuple[int, int, str]]] = []
        self._batch: Optional[List[Tuple[int, int, str]]] = None
        self._suspend = False
        # field -> the row you last had the cursor in for that field. "Set as ..."
        # copies from here rather than always from row 1: you click the row that has
        # the right author precisely because it is the right one, and copying the top
        # row instead means the button writes a value you did not choose.
        self._last_row: Dict[str, int] = {}
        # (column, order) of the last heading click, so clicking the same one again
        # reverses it. Nothing is sorted until you ask.
        self._sorted: Optional[Tuple[int, Qt.SortOrder]] = None

        self.setWindowTitle(f'Edit {len(self.entries)} books')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        note = QLabel('Enter moves down · Shift+Enter up · Tab right · Shift+Tab left '
                      '· Ctrl+Z undoes one edit · Delete clears the selected cells '
                      '· Ctrl+C / Ctrl+V copy and paste them · click a heading to sort. '
                      'Nothing is written until you press Apply.')
        note.setStyleSheet(f'color: {TEXT_DIM};')
        # A wrapped QLabel still reports its whole unwrapped line as its minimum
        # width, which is how one long sentence of key hints came to decide how wide
        # this window opens. It is a hint; it wraps, and it gets no say in the size.
        note.setMinimumWidth(1)
        layout.addWidget(note)

        self.grid = _Grid(len(self.entries), len(FIELDS))
        self.grid.dialog = self
        self.grid.setHorizontalHeaderLabels([title for _, title in FIELDS])
        # Row numbers only. The file name is a column now, so the vertical header has
        # nothing left to say except where you are.
        self.grid.setVerticalHeaderLabels(
            [str(row + 1) for row in range(len(self.entries))])

        header = self.grid.horizontalHeader()
        # Every column drags, including File and Title. Stretching the last section
        # is what made Title the one column you could not size.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        # Headings read left-aligned, in line with the values under them. The number
        # column is the exception: its values are centred, so its title is too.
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignVCenter)
        index_column = self._column_for('series_index')
        self.grid.horizontalHeaderItem(index_column).setTextAlignment(
            Qt.AlignmentFlag.AlignCenter)
        self._restore_columns()

        # Click a heading to sort. Qt's own setSortingEnabled re-sorts on every write,
        # which in a grid you type down means the row you just edited jumps out from
        # under the cursor mid-run. Sorting is done on the click instead, so the order
        # only ever changes when you ask it to.
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._sort_by)
        header.setSortIndicatorShown(False)
        header.setToolTip('Click a heading to sort by that column. '
                          'Rows keep their order while you type.')

        for row, entry in enumerate(self.entries):
            for column, (name, _) in enumerate(FIELDS):
                text = self._row_label(entry) if name == 'file' else entry.value(name)
                item = (_NumberItem(text) if name == 'series_index'
                        else QTableWidgetItem(text))
                if name == 'file':
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    item.setToolTip(display_path(entry.primary_audio)
                                    if entry.primary_audio else entry.entry_id)
                    # Which book this row is, surviving every re-sort: the undo stack
                    # and the final diff are anchored to this, not to a row number,
                    # because a row number means a different book after a sort.
                    item.setData(Qt.ItemDataRole.UserRole, row)
                elif name == 'series_index':
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.grid.setItem(row, column, item)
                self._current[(row, column)] = text
        layout.addWidget(self.grid, stretch=1)
        self.grid.itemChanged.connect(self._item_changed)
        self.grid.currentCellChanged.connect(self._cursor_moved)

        # Filling a column from its first cell is the one bulk action a grid does not
        # already cover, and it is the common one for a series split across folders.
        #
        # These used to be held under their own columns, which sounded right and was
        # not: the buttons then inherited the *column's* width, so "Set as 1, 2, 3"
        # lived under the "#" column and was squeezed to a sliver, and every one of
        # them jumped sideways whenever a column was dragged. They are one centred row
        # now. Each says which field it writes, so nothing is lost by moving them.
        # A flow, not a fixed row: each button names the value it would write, in full,
        # and a real author plus a real series is easily wider than three buttons'
        # worth of window. Given the choice between cutting the name short and using a
        # second line, it takes the second line - the name is the whole message.
        bar = _FlowLayout(spacing=10)
        self.column_buttons: Dict[str, QWidget] = {}
        for name in ('author', 'series', 'series_index'):
            if name == 'series_index':
                button = self._build_number_button()
            else:
                button = QPushButton()
                button.clicked.connect(lambda _=False, n=name: self._fill_down(n))
            button.setMinimumWidth(190)
            self.column_buttons[name] = button
            bar.addWidget(button)
        layout.addLayout(bar)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply
                                   | QDialogButtonBox.StandardButton.Cancel)
        self.undo_button = QPushButton('Undo')
        self.undo_button.setToolTip('Undo the last edit made in this window  (Ctrl+Z).\n'
                                    'Nothing outside this window is touched.')
        self.undo_button.clicked.connect(self._undo_last)
        buttons.addButton(self.undo_button, QDialogButtonBox.ButtonRole.ResetRole)

        apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        apply_button.setText(f'Apply to {len(self.entries)} books')
        apply_button.setProperty('accent', True)
        apply_button.clicked.connect(lambda: (self.grid._commit(), self.accept()))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        QShortcut(QKeySequence.StandardKey.Undo, self, self._undo_last)

        self._refresh_buttons()
        self.grid.setCurrentCell(0, EDITABLE[0])
        # Sized last: the height depends on the row count and the width on the table
        # we were opened from, and neither is knowable while the widgets are being
        # assembled.
        self._restore_size()

    # ------------------------------------------------------------ persistence

    def _restore_size(self) -> None:
        """As wide as the main table was, and exactly as tall as its contents.

        Width: the table you were just looking at is the width you have decided these
        columns want, so the grid opens at that width rather than at a number picked
        here. Height: computed from the row count, so there is no band of empty grid
        under the last book - a saved height is only honoured while it is not bigger
        than the content needs.
        """
        saved = (self.settings.get(SETTING_WINDOW) if self.settings else '') or ''
        try:
            saved_width, saved_height = [int(part) for part in saved.split(',')[:2]]
        except ValueError:
            saved_width = saved_height = 0

        # Never narrower than the layout says it can be - the fill-down buttons carry
        # a whole author or series name, and that name is not allowed to be clipped by
        # the window edge any more than by an ellipsis.
        width = max(saved_width, self._main_table_width(), 900,
                    self.minimumSizeHint().width())

        # Header, one line per book, then the note, the button row and the buttons.
        rows = self.grid.verticalHeader().defaultSectionSize() * len(self.entries)
        chrome = 210
        content = self.grid.horizontalHeader().height() + rows + 4
        needed = content + chrome
        available = self._screen_height()
        height = min(needed, available)
        # A saved height only wins while it is *smaller* than the content needs. A
        # taller one would put the empty band back, which is what it was doing: the
        # grid stretches, so any spare height in the window becomes blank grid under
        # the last book.
        if 300 < saved_height < height:
            height = saved_height

        # And the grid itself is capped at its content, so even a window dragged taller
        # than the list leaves the space below the buttons rather than inside the table.
        if needed <= available:
            self.grid.setMaximumHeight(content)

        self.resize(min(width, self._screen_width()), int(height))

    def _main_table_width(self) -> int:
        """How wide the review table is right now, if we were opened from it."""
        parent = self.parent()
        table = getattr(parent, 'table', None)
        if table is None:
            return 0
        # Plus the dialog's own margins, so the columns get the table's width rather
        # than the table's width minus padding.
        return table.width() + 28

    def _screen_width(self) -> int:
        screen = self.screen()
        return screen.availableGeometry().width() - 80 if screen else 1600

    def _screen_height(self) -> int:
        screen = self.screen()
        return screen.availableGeometry().height() - 100 if screen else 800

    def _restore_columns(self) -> None:
        """Saved widths if there are any, otherwise the main table's own proportions."""
        saved = (self.settings.get(SETTING_COLUMNS) if self.settings else '') or ''
        widths = []
        for part in saved.split(','):
            try:
                widths.append(int(part))
            except ValueError:
                widths = []
                break
        # Five columns of exactly 100 is Qt's untouched default, not a decision, and
        # older versions of this dialog wrote it out. Treat it as "never set".
        if len(widths) != len(FIELDS) or set(widths) == {100}:
            widths = list(DEFAULT_WIDTHS)
            saved = ''
        for column, width in enumerate(widths):
            self.grid.setColumnWidth(column, max(40, width))
        self._saved_columns = bool(saved)

    def _fit_columns(self) -> None:
        """Share the width out by STRETCH, exactly as the main table does.

        Qt's default is to leave every column at its stored pixel width and park the
        remainder in a dead gutter on the right, which is what made the grid open with
        four narrow columns and half a window of nothing.
        """
        available = self.grid.viewport().width()
        if available <= 0:
            return
        fixed = sum(self.grid.columnWidth(c) for c in range(len(FIELDS))
                    if c not in STRETCH)
        room = available - fixed - 2
        total_share = sum(STRETCH.values())
        base = sum(DEFAULT_WIDTHS[c] for c in STRETCH)
        spare = room - base
        if room <= 0:
            return
        for column, share in STRETCH.items():
            width = DEFAULT_WIDTHS[column] + spare * (share / total_share)
            self.grid.setColumnWidth(column, max(80, int(width)))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if getattr(self, '_fitted', False):
            return
        self._fitted = True
        # Widths are applied here rather than while building, for the same reason the
        # main table does it late: applying a stylesheet re-polishes the header, and
        # Qt resets every section to the default 100px when that happens, throwing
        # away anything set beforehand.
        self._restore_columns()
        if not self._saved_columns:
            self._fit_columns()

    def _save_state(self) -> None:
        # Nothing is worth saving from a dialog that never made it onto the screen:
        # its columns are still Qt's untouched 100px default, and writing those back
        # would make every future grid open with five identical narrow columns.
        if self.settings is None or not getattr(self, '_fitted', False):
            return
        size = self.size()
        self.settings.set(SETTING_WINDOW, f'{size.width()},{size.height()}')
        self.settings.set(SETTING_COLUMNS, ','.join(
            str(self.grid.columnWidth(column)) for column in range(len(FIELDS))))
        try:
            self.settings.save()
        except OSError:
            pass

    def done(self, result: int) -> None:
        # Saved on the way out whichever button was pressed: the size you left the
        # window at is the size you wanted, even if you cancelled the edit.
        self._save_state()
        super().done(result)

    # ------------------------------------------------------------------- undo

    def _item_changed(self, item: QTableWidgetItem) -> None:
        """Record every value change so it can be walked back one step at a time."""
        if self._suspend:
            return
        key = (self._book_at(item.row()), item.column())
        before = self._current.get(key, '')
        if item.text() == before:
            return
        self._current[key] = item.text()
        change = (key[0], item.column(), before)
        if self._batch is not None:
            self._batch.append(change)
            return          # the group refreshes once, when it closes
        self._undo.append([change])
        self._refresh_buttons()

    def _group(self) -> None:
        """Start collecting the next writes into one undo step."""
        self._batch = []

    def _ungroup(self) -> None:
        """Close the group, keeping it only if it actually changed something."""
        if self._batch:
            self._undo.append(self._batch)
        self._batch = None
        self._refresh_buttons()

    def _undo_last(self) -> None:
        if not self._undo:
            return
        self.grid._commit()
        batch = self._undo.pop()
        self._suspend = True
        try:
            # Newest change in the batch first, so a cell written twice within one
            # step lands back on the value it had before the step.
            for book, column, before in reversed(batch):
                item = self.grid.item(self._row_of(book), column)
                if item is None:
                    continue
                item.setText(before)
                self._current[(book, column)] = before
        finally:
            self._suspend = False
        self._refresh_buttons()

    # --------------------------------------------------------------- ordering

    def _book_at(self, row: int) -> int:
        """Which entry the given visible row is showing."""
        item = self.grid.item(row, 0)
        book = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return row if book is None else int(book)

    def _row_of(self, book: int) -> int:
        """Where the given entry currently sits. The inverse of :meth:`_book_at`."""
        for row in range(self.grid.rowCount()):
            if self._book_at(row) == book:
                return row
        return book

    def _sort_by(self, column: int) -> None:
        """Sort on a heading click, ascending then descending on the next click."""
        self.grid._commit()
        order = (Qt.SortOrder.DescendingOrder
                 if self._sorted == (column, Qt.SortOrder.AscendingOrder)
                 else Qt.SortOrder.AscendingOrder)

        # "The row I was last in" is a statement about a book, not about a position,
        # so it is carried across the re-sort rather than left pointing at whichever
        # book landed on that row.
        remembered = {name: self._book_at(row) for name, row in self._last_row.items()
                      if 0 <= row < self.grid.rowCount()}
        current = self.grid.currentRow()
        current_book = self._book_at(current) if current >= 0 else None

        self.grid.sortItems(column, order)
        self._sorted = (column, order)
        self.grid.horizontalHeader().setSortIndicator(column, order)
        self.grid.horizontalHeader().setSortIndicatorShown(True)

        self._last_row = {name: self._row_of(book) for name, book in remembered.items()}
        if current_book is not None:
            self.grid.setCurrentCell(self._row_of(current_book),
                                     max(self.grid.currentColumn(), EDITABLE[0]))
        self._refresh_buttons()

    # ---------------------------------------------------------------- buttons

    def _cursor_moved(self, row: int, column: int, *_previous) -> None:
        """Remember which row the cursor is in, per field, and re-label the buttons."""
        if row < 0 or column < 0 or column >= len(FIELDS):
            return
        name = FIELDS[column][0]
        if name != 'file':
            self._last_row[name] = row
        self._refresh_buttons()

    def _source_row(self, name: str) -> int:
        """The row "Set as ..." copies from: the last one you were in, else the first."""
        row = self._last_row.get(name, 0)
        return row if 0 <= row < self.grid.rowCount() else 0

    def _build_number_button(self) -> QToolButton:
        """"Set as 1, 2, 3" with an arrow for the other numbering.

        A QToolButton in this application's theme is a toolbar button: transparent, no
        border, no chrome - which is right on a toolbar and wrong here, where it sits
        between two real buttons and has to look like one. So it is given the
        QPushButton styling explicitly, menu arrow included.
        """
        button = QToolButton()
        button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(f"""
            QToolButton {{
                background-color: {FIELD}; color: {TEXT};
                border: 1px solid {FIELD_BORDER}; border-radius: {RADIUS}px;
                padding: 7px 14px; padding-right: 26px; font-weight: 600;
            }}
            QToolButton:hover {{ background-color: {FIELD_HOVER};
                                 border-color: {ACCENT}; }}
            QToolButton:pressed {{ background-color: {BG_BASE}; }}
            QToolButton:disabled {{ color: {TEXT_FAINT}; background-color: {BG_BASE};
                                    border-color: {BG_RAISED}; }}
            QToolButton::menu-button {{
                width: 20px; border-left: 1px solid {FIELD_BORDER};
                border-top-right-radius: {RADIUS}px;
                border-bottom-right-radius: {RADIUS}px;
            }}
        """)
        button.clicked.connect(lambda: self._number_rows(1))

        menu = QMenu(button)
        self.number_from_one = menu.addAction(
            'Set as 1, 2, 3...', lambda: self._number_rows(1))
        self.number_from_top = menu.addAction(
            'Set as x, x+1, x+2...', lambda: self._number_rows(self._top_number()))
        button.setMenu(menu)
        return button

    def _top_number(self) -> int:
        """The number the "count on from here" option starts at.

        Taken from the row you last had the cursor in, for the same reason the author
        and series buttons are: you clicked that row because it is the one that is
        already right.
        """
        column = self._column_for('series_index')
        item = self.grid.item(self._source_row('series_index'), column)
        try:
            return int(float((item.text() if item else '').strip()))
        except ValueError:
            return 1

    def _refresh_buttons(self) -> None:
        """Show what each button would actually write, not what it is called.

        "Fill down" under a column tells you the mechanism; "Set as Brandon
        Sanderson" tells you the result, which is the thing you are deciding about.
        """
        for name in ('author', 'series'):
            button = self.column_buttons.get(name)
            if button is None:
                continue
            row = self._source_row(name)
            value = self._value_at(row, name)
            button.setEnabled(bool(value) and self.grid.rowCount() > 1)
            self._set_button_text(
                button,
                f'Set as {value}' if value else f'Set as the selected {name}')
            button.setToolTip(
                f'Write "{value}" - the {name} on row {row + 1}, the last one you had '
                f'the cursor in - into the {name} of every other row.\n'
                f'Click a different row\'s {name} to copy that one instead.'
                if value else
                f'Row {row + 1} has no {name} to copy. Click a row that does.')

        number = self.column_buttons.get('series_index')
        if number is not None:
            top = self._top_number()
            self._set_button_text(number, 'Set as 1, 2, 3')
            self.number_from_top.setText(
                f'Set as {top}, {top + 1}, {top + 2}...')
            number.setToolTip('Number the rows 1, 2, 3 in the order shown.\n'
                              'The arrow offers counting on from the first row '
                              f'instead ({top}, {top + 1}, {top + 2}...).')

        self.undo_button.setEnabled(bool(self._undo))
        self.undo_button.setText(f'Undo ({len(self._undo)})' if self._undo else 'Undo')

    @staticmethod
    def _set_button_text(button: QWidget, text: str) -> None:
        """Label a button with the value it would write - all of it, always.

        The whole point of these buttons is that they name the value, so a name cut
        short is the one thing they must not do: no ellipsis in the string, and no
        eliding by Qt to make it fit. Nothing is measured here on purpose. The button
        reports what it needs through sizeHint - which is the styled width of this
        exact text - and _FlowLayout gives every button exactly that, wrapping to the
        next line rather than shaving anything off.
        """
        button.setText(text)
        button.updateGeometry()

    # ---------------------------------------------------------------- actions

    @staticmethod
    def _row_label(entry: BookEntry) -> str:
        return Path(entry.primary_audio).name or entry.entry_id

    def _column_for(self, name: str) -> int:
        return [field for field, _ in FIELDS].index(name)

    def _value_at(self, row: int, name: str) -> str:
        item = self.grid.item(row, self._column_for(name))
        return item.text().strip() if item else ''

    def _fill_down(self, name: str) -> None:
        """Copy one row's value to every other row. One undo step - it was one decision.

        The source is the row you last had the cursor in, not row 1. "Fill down" is the
        mechanism; what you actually mean is "this one is right, make the rest match".
        """
        column = self._column_for(name)
        source = self._source_row(name)
        origin = self.grid.item(source, column)
        if origin is None:
            return
        self._group()
        for row in range(self.grid.rowCount()):
            if row != source:
                self.grid.item(row, column).setText(origin.text())
        self._ungroup()

    # -------------------------------------------------------- cell clipboard

    def _selected_cells(self) -> List[Tuple[int, int]]:
        """The selected cells, in the order shown, editable ones only."""
        return sorted({(index.row(), index.column())
                       for index in self.grid.selectedIndexes()
                       if index.column() in EDITABLE})

    def _clear_selected_cells(self) -> None:
        """Delete blanks the selected cells - one undo step, it was one keypress."""
        cells = self._selected_cells()
        if not cells:
            return
        self._group()
        for row, column in cells:
            self.grid.item(row, column).setText('')
        self._ungroup()

    def _copy_selected_cells(self) -> None:
        """The selection as tab-separated text, in the shape it is on screen."""
        from PyQt6.QtWidgets import QApplication

        indexes = self.grid.selectedIndexes()
        if not indexes:
            return
        rows = sorted({index.row() for index in indexes})
        columns = sorted({index.column() for index in indexes})
        chosen = {(index.row(), index.column()) for index in indexes}

        lines = []
        for row in rows:
            lines.append('\t'.join(
                self.grid.item(row, column).text()
                if (row, column) in chosen and self.grid.item(row, column) else ''
                for column in columns))
        QApplication.clipboard().setText('\n'.join(lines))

    def _paste_into_selection(self) -> None:
        """Clipboard into the selection: one value fills it, a block is laid out.

        The read-only file column is skipped rather than written through, so a block
        copied from a spreadsheet that starts a column early still lands in the right
        fields instead of silently shifting everything left.
        """
        from PyQt6.QtWidgets import QApplication

        text = QApplication.clipboard().text()
        cells = self._selected_cells()
        if not text.strip() or not cells:
            return

        grid = [line.split('\t') for line in text.replace('\r\n', '\n').split('\n')]
        self._group()
        if len(grid) == 1 and len(grid[0]) == 1:
            for row, column in cells:
                self.grid.item(row, column).setText(grid[0][0].strip())
        else:
            start_row, start_column = cells[0]
            for row_offset, line in enumerate(grid):
                row = start_row + row_offset
                if row >= self.grid.rowCount():
                    break
                for column_offset, value in enumerate(line):
                    column = start_column + column_offset
                    if column >= len(FIELDS):
                        break
                    if column in EDITABLE:
                        self.grid.item(row, column).setText(value.strip())
        self._ungroup()

    def _number_rows(self, start: int) -> None:
        column = self._column_for('series_index')
        self._group()
        for row in range(self.grid.rowCount()):
            self.grid.item(row, column).setText(str(start + row))
        self._ungroup()

    def values(self) -> Dict[str, Dict[str, str]]:
        """entry_id -> {field: new value}, containing only what actually changed."""
        changes: Dict[str, Dict[str, str]] = {}
        for row in range(self.grid.rowCount()):
            # By book, not by position: after a sort, row 3 is not entries[3].
            entry = self.entries[self._book_at(row)]
            for column, (name, _) in enumerate(FIELDS):
                if name == 'file':
                    continue
                item = self.grid.item(row, column)
                if item is None:
                    continue
                text = item.text().strip()
                if text != entry.value(name):
                    changes.setdefault(entry.entry_id, {})[name] = text
        return changes
