"""Main window: the review table and everything around it.

Layout, deliberately:

* The window is one horizontal split, running the full height - table on the left,
  the explanation panel on the right. The panel starts at the very top because it
  describes the selected row, and nothing should sit above it pushing it down.
* Filters live above the table, on the left, because that is what they filter.
* Every action lives on one toolbar along the bottom, icon-only, next to the keys
  that trigger them. The review loop is look -> identify (F4) -> approve (F5) /
  reject (F6), and all three should be under the same hand.
* Progress is a strip in the status bar, not a full-width row - it is a background
  detail, and it only appears while something is running.

Anything that is a bulk operation on the selection lives in the right-click menu
rather than on the toolbar, so the toolbar stays short enough to read at a glance.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QEvent, QObject, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QAction, QColor, QFont, QIntValidator, QKeySequence,
                         QPixmap, QShortcut)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QHBoxLayout, QHeaderView,
    QInputDialog,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar,
    QPushButton, QSizePolicy, QSplitter, QTableWidget, QTableWidgetItem,
    QTextEdit, QToolBar, QToolButton, QVBoxLayout, QWidget,
)

from ..models import (IDENTITY_FIELDS, STATUS_APPROVED, STATUS_PENDING,
                      STATUS_REJECTED, BookEntry, pretty_status)
from .delegates import (KIND_CONFIDENCE, KIND_COVER, KIND_FILES, KIND_STATUS,
                        ROLE_CONFIDENCE, ROLE_ENTRY_ID, ROLE_KIND, ROLE_PROGRESS,
                        ROLE_PROGRESS_TEXT, ROLE_SECONDARY, ROLE_STATUS,
                        ReviewDelegate)
from .icons import badged_icon
from .icons import icon as make_icon
from .queue_dialog import plural
from .theme import (ACCENT, ACCENT_DARK, BG_RAISED, ROW_HEIGHTS, STATUS_TEXT, TEXT,
                    TEXT_DIM, TEXT_FAINT, source_color)
from .toolbar import ITEMS_BY_KEY, SEPARATOR, parse_layout
from .why_panel import WhyPanel

logger = logging.getLogger(__name__)

COLUMNS = ['', 'FILES', 'AUTHOR', 'SERIES', '#', 'TITLE', 'CONFIDENCE', 'STATUS']
COL_COVER, COL_FILES, COL_AUTHOR, COL_SERIES, COL_INDEX, COL_TITLE, COL_CONF, COL_STATUS = range(8)

# Which table column maps to which model field, for inline editing.
EDITABLE = {COL_AUTHOR: 'author', COL_SERIES: 'series',
            COL_INDEX: 'series_index', COL_TITLE: 'title'}

# What to call each field in a menu entry, so "Rename cell" can say what it renames.
FIELD_LABELS = {'author': 'Author', 'series': 'Series',
                'series_index': 'Number', 'title': 'Title'}

# Plurals for the same fields, so "Clear" can name what it is about to blank.
# "Series" is its own plural, which is why this is a table and not an "s".
FIELD_PLURALS = {'author': 'authors', 'series': 'series entries',
                 'series_index': 'numbers', 'title': 'titles'}

# Everything that puts something on the clipboard. There are five of them, and five
# near-identical "Copy ..." lines in a row is most of what made the right-click menu
# unreadable - so they collapse into one "Copy..." submenu plus however many
# most-recently-used entries AO_UI_COPY_RECENTS asks for.
# Each entry is (id, label, method name, tooltip).
COPY_ACTIONS = [
    ('files', 'Copy files', '_copy_files',
     'Put the audio files themselves on the clipboard, ready to paste into a file '
     'manager. A book that is alone in its folder copies the folder.'),
    ('file_path', 'Copy file path', '_copy_file_paths',
     'Copy the full path of every audio file in the selection, one per line'),
    ('file_name', 'Copy file name', '_copy_file_names',
     'Copy the name of every audio file in the selection, one per line'),
    ('folder_path', 'Copy folder path', '_copy_paths',
     'Copy the folder path of every selected row, one per line'),
    ('table', 'Copy as table', '_copy_rows',
     'Copy the selected rows as a Markdown table'),
]
COPY_BY_ID = {action[0]: action for action in COPY_ACTIONS}

# Starting column widths, sized to hold a real author/series/title without clipping.
# They are also sized to *fit*: much wider than this and Status falls off the right
# edge at the default window size, which is worse than a slightly narrower Title.
# Drag any of them - the widths are remembered.
DEFAULT_WIDTHS = [56, 420, 230, 200, 52, 300, 124, 104]

# Relative share of the leftover width. Files needs the most - it now shows the
# whole path from the library root - and Title about half of that. Author and
# Series keep their starting width and only grow a little.
STRETCH = {1: 1.0, 2: 0.15, 3: 0.15, 5: 0.5}

# The extra filter drop-down: things that are true of the *files*, not of the
# identification. Each entry is (label, predicate). The first is the "off" position,
# which every filter combo in this window uses as index 0.
SHAPE_FILTERS = [
    ('Any shape', lambda e: True),
    ('Multi-file', lambda e: len(e.audio_files) > 1),
    ('Single file', lambda e: len(e.audio_files) == 1),
    ('Already one .m4b', lambda e: len(e.audio_files) == 1
     and e.audio_files[0].lower().endswith(('.m4b', '.m4a'))),
    ('Shares its folder', lambda e: bool(e.is_multi_book_folder)),
    ('Has companion files', lambda e: bool(e.image_files)),
    ('Written to disk', lambda e: bool(e.applied_path)),
    # What Save is about to act on: values you typed yourself that have not been
    # written out yet. "Am I about to save what I think I am" is a question you ask
    # right before pressing Apply, and until now the only way to answer it was to
    # read the whole table.
    ('Unsaved changes', lambda e: not e.applied_path and any(
        getattr(e, name).source == 'user' for name in IDENTITY_FIELDS)),
    ('Flagged as odd', lambda e: bool(getattr(e, 'warnings', None))),
]

# The identification sources the user can switch on and off per run. The key is the
# resolver tier name; the settings key is only used to seed the initial state.
MODES = [
    ('metadata', 'Tags', 'AO_ENABLE_METADATA',
     'Read author/title/series from tags embedded in the audio files'),
    ('regex', 'Filename', 'AO_ENABLE_REGEX',
     'Parse author/series/title out of the file and folder names'),
    ('api', 'Book Databases', 'AO_ENABLE_API',
     'Look the book up in Audnexus / Apple Books / Google Books / Open Library / '
     'LibriVox'),
    ('search', 'Web search', 'AO_ENABLE_SEARCH',
     'Fall back to a web search and scrape the results'),
    ('llm', 'LLM', 'AO_ENABLE_LLM',
     'Ask the configured language model to fill whatever is still missing'),
]

# The approve/reject/reset block in the right-click menu. Turned off to keep the
# menu short - the same three actions are on the toolbar and on F5/F6/F8. The code
# below stays in place so flipping this back on restores it exactly.
SHOW_REVIEW_SECTION = False

# How many books may be opened on Goodreads before we ask, and the range the
# per-tab delay is drawn from. The delay is randomised because a burst of
# identical-interval requests is what spam protection looks for.
GOODREADS_ASK_ABOVE = 5
GOODREADS_DELAY = (0.7, 1.9)

# Frames for the "something is still happening" spinner in the toolbar status. Braille
# dots, because they are the same width in every font and never reflow the sentence.
SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'


class _NumericItem(QTableWidgetItem):
    """Displays a formatted string, sorts on the number behind it."""

    def __init__(self, text: str, value: float):
        super().__init__(text)
        self.value = value

    def __lt__(self, other) -> bool:
        if isinstance(other, _NumericItem):
            return self.value < other.value
        return super().__lt__(other)


class _KeyedItem(QTableWidgetItem):
    """Shows nothing (or a picture), sorts on a string you give it.

    The cover column is the only one with no heading, which makes it the only one whose
    sort is not already spoken for - so it sorts by the path from the library root.
    Clicking it groups the table by folder, which is how a library is actually walked.
    """

    def __init__(self, key: str):
        super().__init__('')
        self.key = (key or '').lower()

    def __lt__(self, other) -> bool:
        if isinstance(other, _KeyedItem):
            return self.key < other.key
        return super().__lt__(other)


class _RightClickFilter(QObject):
    """Turns a right-click on one widget into a call, whatever Qt calls the event."""

    def __init__(self, parent, handler: Callable[[], None]):
        super().__init__(parent)
        self._handler = handler

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.ContextMenu:
            self._handler()
            return True
        if (kind == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.RightButton):
            self._handler()
            return True
        # The release is swallowed too, or the button sees an unmatched release and
        # repaints itself as still pressed.
        if (kind == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.RightButton):
            return True
        return super().eventFilter(watched, event)


class _DeselectFilter(QObject):
    """A left-click on empty chrome clears the table selection.

    There was nowhere to click to select nothing. Every row stayed selected until
    another was clicked, which matters more here than in most tables because half the
    toolbar acts on the selection - so "act on everything" and "act on the one row I
    left selected an hour ago" were a click apart with nothing to tell them apart.

    Installed on the widgets that have genuinely dead space: the toolbar around its
    buttons, the filter row, and the table's own viewport below the last row. The
    press is observed and passed on, never swallowed - a click on a button in the
    toolbar still presses the button, and it clears the selection first only when it
    landed on the background.
    """

    def __init__(self, window: 'MainWindow'):
        super().__init__(window)
        self._window = window

    def eventFilter(self, watched, event) -> bool:
        if (event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton):
            table = self._window.table
            if watched is table.viewport():
                # Below the last row, or right of the last column.
                if not table.indexAt(event.position().toPoint()).isValid():
                    table.clearSelection()
            else:
                # The filter is on the container itself, so an event that reaches it
                # at all did not land on a child control.
                table.clearSelection()
        return super().eventFilter(watched, event)


class MainWindow(QMainWindow):
    """The review UI. All heavy work is delegated to the controller via signals."""

    # Emitted for the controller (main.py) to act on.
    scan_requested = pyqtSignal()
    resolve_requested = pyqtSignal(list, list)     # entries, tier names
    apply_requested = pyqtSignal(list, bool)       # entries, preview
    undo_requested = pyqtSignal(int)               # index into the pending journal
    merge_requested = pyqtSignal(object)           # entry
    settings_requested = pyqtSignal()
    settings_requested_on_tab = pyqtSignal(str)    # open Settings on a tab
    cancel_requested = pyqtSignal()                # cancel everything (Esc, toolbar)
    cancel_current_requested = pyqtSignal()        # cancel only the running job
    settings_changed = pyqtSignal()                # a setting was edited in the window
    queue_remove_requested = pyqtSignal(int)       # index into the pending queue
    queue_clear_requested = pyqtSignal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.entries: Dict[str, BookEntry] = {}
        self.row_ids: List[str] = []
        self._updating = False
        self._columns_fitted = False
        self._has_saved_widths = False
        self.mode_actions: Dict[str, QAction] = {}
        self.tool_actions: Dict[str, QAction] = {}
        self._busy = False
        # The toolbar width labelled buttons last asked for - see _fit_toolbar.
        self._labelled_toolbar_width = 0
        self._queue_labels: List[str] = []
        self._status_text = 'Ready'
        # A long encode reports the same sentence for a minute at a time, which reads
        # as a hung program. The spinner is the cheapest possible proof that something
        # is still turning over, and it costs one timer.
        self._spin = 0
        self._spinner = QTimer(self)
        self._spinner.setInterval(110)
        self._spinner.timeout.connect(self._tick_spinner)
        self._custom_confidence = 0.8
        # Set by the controller: returns the undoable transactions, newest last.
        self.history_provider: Callable[[], List[str]] = lambda: []
        # Undo history for edits made in the table - typing, clearing, filling down,
        # the grid editor. Applies live in the controller's journal instead; the two
        # are interleaved by comparing journal lengths, see _undo_last.
        self._edit_undo: List[dict] = []
        # Steps that have been undone and can be put back, oldest-undone first - so
        # the *last* element is the next thing a redo would do. Emptied by any new
        # edit, which is what "the future is gone once you do something new" means.
        self._edit_redo: List[dict] = []
        # Opened by right-clicking Undo, and by the queue button. Kept on self so a
        # second click raises the existing window instead of stacking another one.
        self._history_dialog = None
        self._queue_dialog = None
        # Event filters for the right-clickable toolbar buttons. Rebuilt with the
        # toolbar, and held here because a filter nobody references stops filtering.
        self._right_click_filters: List[QObject] = []
        # Panel width to restore once the window is its final size - see _restore_split.
        self._wanted_split: Optional[int] = None
        # Set by the controller: a WorkerManager.status() snapshot on demand.
        self.queue_provider: Callable[[], dict] = dict
        self._last_progress = (0, 0, '')
        # entry_id -> (fraction 0..1, short label), for the bar drawn across a row
        # while a long job - a chapter merge - is working on that one book.
        self._row_progress: Dict[str, tuple] = {}

        self.setWindowTitle('Audiobook Organizer')
        # Wide enough that every column fits beside the explanation panel.
        self.resize(1780, 960)
        self._build()
        self._install_shortcuts()

    # ------------------------------------------------------------------ build

    def _build(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 4)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        self.paths_banner = self._build_paths_banner()
        left_layout.addWidget(self.paths_banner)

        self.filter_bar = QWidget()
        self.filter_bar.setLayout(self._build_filter_row())
        left_layout.addWidget(self.filter_bar)
        left_layout.addWidget(self._build_table(), stretch=1)
        splitter.addWidget(left)

        self.why_panel = WhyPanel()
        self.why_panel.run_requested.connect(self._run_one_source)
        self.panel_container = QWidget()
        panel_layout = QVBoxLayout(self.panel_container)
        panel_layout.setContentsMargins(8, 0, 0, 0)
        panel_layout.addWidget(self.why_panel)
        splitter.addWidget(self.panel_container)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([1360, 400])
        self.splitter = splitter
        # Dragging the splitter resizes the table without resizing the window, so the
        # window's resizeEvent never fires and the columns used to just sit there.
        splitter.splitterMoved.connect(lambda *_: self._fit_columns())
        outer.addWidget(splitter, stretch=1)
        self.setCentralWidget(central)

        self._build_toolbar()
        self._restore_window_state()
        self.apply_ui_settings()
        self._apply_column_widths()

        # Somewhere to click that means "nothing" - see _DeselectFilter.
        self._deselect_filter = _DeselectFilter(self)
        for widget in (self.toolbar, self.filter_bar, self.table.viewport()):
            widget.installEventFilter(self._deselect_filter)

        # Now the table exists, so the toolbar can finally say what is available.
        self.refresh_action_states()
        self._refresh_identify_badge()

    def _build_paths_banner(self) -> QWidget:
        """Says which folders are not set yet, and opens the page that sets them.

        Neither path has a default any more, because a default sent a fresh install
        at a folder inside the program directory and then never mentioned it again.
        An empty table with no explanation is the same failure with extra steps, so
        this sits above the table until both folders have been chosen.
        """
        banner = QWidget()
        banner.setStyleSheet(
            f'background: {BG_RAISED}; border: 1px solid {ACCENT_DARK}; '
            f'border-radius: 6px;')
        row = QHBoxLayout(banner)
        row.setContentsMargins(12, 9, 12, 9)
        row.setSpacing(10)

        self.paths_banner_label = QLabel('')
        self.paths_banner_label.setWordWrap(True)
        self.paths_banner_label.setStyleSheet(
            f'color: {TEXT}; border: none; background: transparent;')
        row.addWidget(self.paths_banner_label, stretch=1)

        button = QPushButton('Set them in Settings')
        button.setProperty('accent', True)
        button.setToolTip('Open the General tab, where the input and output folders '
                          'are chosen')
        button.clicked.connect(
            lambda: self.settings_requested_on_tab.emit('General'))
        row.addWidget(button)
        return banner

    def refresh_paths_banner(self) -> None:
        """Show or hide the banner for whatever the settings currently say."""
        missing = [name for key, name in (('AO_INPUT_DIR', 'input'),
                                          ('AO_OUTPUT_DIR', 'output'))
                   if not self.settings.is_set(key)]
        self.paths_banner.setVisible(bool(missing))
        if not missing:
            return
        which = ' and '.join(missing)
        self.paths_banner_label.setText(
            f'<b>No {which} folder is set.</b>  '
            + ('Nothing can be scanned or saved until both are chosen.'
               if len(missing) == 2 else
               'Scanning has nothing to read.' if missing == ['input'] else
               'Saving has nowhere to write.'))

    def _build_filter_row(self) -> QHBoxLayout:
        """Three zones: the filters on the left, the count centred, Confidence right.

        The count is centred rather than right-aligned because it is not a control -
        it is a readout, and parking it next to the Confidence field made two unrelated
        numbers read as one pair. Confidence keeps the right-hand end to itself.
        """
        row = QHBoxLayout()
        row.setSpacing(6)
        row.setContentsMargins(0, 0, 0, 0)

        self.status_filter = self._filter_combo(
            ['All statuses', 'Pending', 'Approved', 'Rejected', 'Risky', 'Applied',
             'Duplicate'],
            'Show only rows with this review status')
        row.addWidget(self.status_filter)

        self.missing_filter = self._filter_combo(
            ['Any completeness', 'Missing any field', 'Missing author',
             'Missing series', 'Missing index', 'Missing title', 'Missing cover',
             'Has cover', 'Complete only'],
            'Show only rows that are missing a given field or cover image')
        row.addWidget(self.missing_filter)

        self.confidence_filter = self._filter_combo(
            ['Any confidence', 'Below 50%', 'Below 80%', '80% and above',
             'Custom threshold...'],
            'Show only rows whose overall identification confidence is in this range. '
            '"Custom threshold" asks for your own number.')
        self.confidence_filter.activated.connect(self._confidence_filter_chosen)
        row.addWidget(self.confidence_filter)

        # Everything that is a property of the *files* rather than of the
        # identification. Multi-file is the one you actually go looking for, because it
        # is the set of books "Merge chapters into one .m4b" can act on.
        self.shape_filter = self._filter_combo(
            [name for name, _test in SHAPE_FILTERS],
            'Filter on what the entry is on disk rather than on how it was identified')
        row.addWidget(self.shape_filter)

        self.search_box = QLineEdit()
        self.search_box.setProperty('search', True)
        self.search_box.setPlaceholderText('Search author, series, title or folder...')
        self.search_box.setToolTip(
            'Free-text filter across author, series, title and folder name  '
            '(F3 or Ctrl+F)')
        self.search_box.setClearButtonEnabled(True)
        # Twice the width it had: this is the control that gets used most, and 180px
        # showed about three words of a query. It still yields before the panel beside
        # it, being Expanding rather than fixed.
        self.search_box.setMinimumWidth(360)
        self.search_box.setSizePolicy(QSizePolicy.Policy.Expanding,
                                      QSizePolicy.Policy.Fixed)
        self.search_box.textChanged.connect(self._apply_filters)
        row.addWidget(self.search_box, stretch=2)

        # Stretch, count, stretch: two equal springs put the readout in the middle of
        # whatever space is left between the filters and the Confidence control.
        row.addStretch(1)
        self.count_label = QLabel('')
        self.count_label.setProperty('count', True)
        self.count_label.setToolTip('How many rows pass the current filters')
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.count_label)
        row.addStretch(1)

        # Not a filter - the setting that decides when identification stops digging.
        # It lives here because it is the number you actually adjust while working
        # through a library, and burying it three tabs deep in Settings meant it was
        # adjusted never. Bold, because it is the one thing on this row that changes
        # what the program *does* rather than what it shows.
        label = QLabel('Confidence')
        label.setStyleSheet(f'color: {TEXT}; font-weight: 700; padding-right: 6px;')
        row.addWidget(label)

        self.confidence_score = QLineEdit()
        self.confidence_score.setProperty('filter', True)
        self.confidence_score.setFixedWidth(56)
        self.confidence_score.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.confidence_score.setValidator(QIntValidator(0, 100, self.confidence_score))
        tip = ('How sure an identification has to be before the tiers stop looking.\n'
               'This is NOT a filter - it changes what identification does, not what\n'
               'the table shows. Lower it to accept looser matches; raise it to keep\n'
               'digging through more sources.')
        self.confidence_score.setToolTip(tip)
        label.setToolTip(tip)
        self.confidence_score.editingFinished.connect(self._confidence_score_changed)
        row.addWidget(self.confidence_score)

        percent = QLabel('%')
        # Same weight as its label - "Confidence" and "%" are one phrase with a box in
        # the middle, so they cannot be two different weights. The padding is the
        # right-hand margin of the whole row.
        percent.setStyleSheet(f'color: {TEXT}; font-weight: 700; padding: 0 14px 0 3px;')
        percent.setToolTip(tip)
        row.addWidget(percent)
        self._load_confidence_score()
        return row

    def _load_confidence_score(self) -> None:
        self.confidence_score.setText(
            str(int(round(self.settings.get_float('AO_CONFIDENCE_SCORE', 0.8) * 100))))

    def _confidence_score_changed(self) -> None:
        """Write the threshold straight back to .env - it is a setting, not a filter."""
        try:
            percent = min(100, max(0, int(self.confidence_score.text().strip())))
        except ValueError:
            self._load_confidence_score()
            return
        self.settings.set('AO_CONFIDENCE_SCORE', str(round(percent / 100.0, 4)))
        try:
            self.settings.save()
        except OSError as exc:
            logger.warning('Could not save the confidence score: %s', exc)
        self.settings_changed.emit()
        self.show_message(f'Identification now stops at {percent}% confidence')

    def _filter_combo(self, items: List[str], tooltip: str) -> QComboBox:
        """A filter drop-down that looks like one, and marks itself when it is on."""
        combo = QComboBox()
        combo.setProperty('filter', True)
        combo.addItems(items)
        combo.setToolTip(tooltip)
        # A combo's minimum width is its widest entry unless it is told otherwise,
        # and four of them side by side held the whole left-hand pane at 1590px -
        # which meant the explanation panel could not be given the width it was
        # saved at on anything narrower. The closed box elides; the popup does not.
        combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(10)
        combo.view().setMinimumWidth(
            max(combo.fontMetrics().horizontalAdvance(text) for text in items) + 40)
        combo.currentIndexChanged.connect(
            lambda index, c=combo: self._filter_changed(c, index))
        return combo

    def _filter_changed(self, combo: QComboBox, index: int) -> None:
        # Index 0 of every filter is its "off" entry, so an active filter is visible
        # at a glance rather than needing to be read.
        combo.setProperty('active', index > 0)
        combo.style().unpolish(combo)
        combo.style().polish(combo)
        self._apply_filters()

    def _build_table(self) -> QTableWidget:
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        # Everything is painted by the delegate, which needs hover state and draws
        # its own row separators - so no zebra striping and no grid.
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.setMouseTracking(True)
        self.delegate = ReviewDelegate(self.table)
        self.delegate.move_after_edit.connect(self._step_edit)
        self.table.setItemDelegate(self.delegate)
        # Cells, not rows: clearing a column, or three cells in two rows, has to be
        # expressible. Row-wide actions still work - they read the rows the selected
        # cells belong to.
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(58)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        # Moving the cursor with the arrow keys repaints only the two cells involved,
        # and anything the delegate drew beyond a cell's own rect - the selection
        # frame's rounded joins - was left behind as a ghost outline. Repainting the
        # viewport on every move costs nothing at these row counts and cannot smear.
        self.table.currentCellChanged.connect(
            lambda *_: self.table.viewport().update())
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemChanged.connect(self._item_edited)
        self.table.setToolTip('Double-click author, series, # or title to edit it. '
                              'Right-click for everything you can do to the selection.')

        header = self.table.horizontalHeader()
        # Nothing is sorted until the user asks for it, so no arrow parked on the
        # cover column pretending the table is sorted by artwork. Qt turns the
        # indicator back on every time sorting is re-enabled, hence the flag.
        self._user_sorted = False
        header.setSortIndicatorShown(False)
        header.sectionClicked.connect(self._sorted_by_user)
        header.setSectionResizeMode(COL_COVER, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_FILES, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_AUTHOR, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_SERIES, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_INDEX, QHeaderView.ResizeMode.Fixed)
        # Files is the stretch column: it holds the longest strings and is the one
        # you read when an identification looks wrong. Everything else is sized to
        # hold a realistic value without clipping - author and series names are long.
        header.setSectionResizeMode(COL_TITLE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_CONF, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Fixed)
        # No stretch column at all: a stretch column eats whatever the others give
        # back and squeezes the rest into ellipses. Generous fixed widths that the
        # user can drag, with a horizontal scrollbar when the window is narrow.
        # The widths themselves are applied later - see _apply_column_widths.
        header.setSectionResizeMode(COL_FILES, QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(44)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignVCenter)
        # "#" is the one centred heading: its values are centred numbers, and a
        # left-aligned title over a centred two-character column reads as a stray
        # label. Everything else - Confidence and Status included - reads from the
        # left, in line with the column beside it.
        #
        # Every heading states its alignment rather than leaning on the header's
        # default. A section left with no TextAlignmentRole is at the mercy of
        # whatever draws it - and with a stylesheet on QHeaderView::section, that is
        # not always the default alignment. Saying it per column costs one line and
        # cannot be overridden by a repolish.
        for column in range(len(COLUMNS)):
            item = QTableWidgetItem(COLUMNS[column])
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter if column == COL_INDEX
                else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.table.setHorizontalHeaderItem(column, item)
        # The cover column has no heading of its own, so its sort is free: it sorts by
        # the path from the library root, which groups the table by folder. That is the
        # one ordering the other columns cannot give you, and it is what you want when
        # you are working through a library directory by directory.
        cover_header = QTableWidgetItem('')
        cover_header.setTextAlignment(Qt.AlignmentFlag.AlignLeft
                                      | Qt.AlignmentFlag.AlignVCenter)
        cover_header.setToolTip('Click to sort by folder path, so books group by '
                                'where they live on disk')
        self.table.setHorizontalHeaderItem(COL_COVER, cover_header)

        # Right-clicking the header is where people look to hide a column.
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._show_column_menu)
        header.setToolTip('Click to sort. Right-click to choose which columns to show.')
        return self.table

    def _show_column_menu(self, position) -> None:
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        header = self.table.horizontalHeader()
        for column in range(1, len(COLUMNS)):
            action = menu.addAction(COLUMNS[column].title())
            action.setCheckable(True)
            action.setChecked(not self.table.isColumnHidden(column))
            action.setToolTip(f'Show or hide the {COLUMNS[column].lower()} column')
            action.toggled.connect(
                lambda shown, c=column: self._set_column_visible(c, shown))
        menu.exec(header.mapToGlobal(position))

    def _set_column_visible(self, column: int, shown: bool) -> None:
        self._hide_column(column, not shown)
        # Column 0 is the cover, which follows the density/covers settings rather
        # than this list - and it has no name to store anyway.
        hidden = [COLUMNS[c] for c in range(1, len(COLUMNS))
                  if self.table.isColumnHidden(c)]
        self.settings.set('AO_UI_HIDDEN_COLUMNS', ','.join(hidden))
        self.settings.save()

    # ------------------------------------------------------- interface options

    def apply_ui_settings(self) -> None:
        """Push every AO_UI_* preference into the widgets. Safe to call repeatedly."""
        get_bool = self.settings.get_bool

        self.delegate.show_stripe = get_bool('AO_UI_STATUS_STRIPE', True)
        self.delegate.row_tint = get_bool('AO_UI_ROW_TINT', False)
        self.delegate.show_covers = get_bool('AO_UI_SHOW_COVERS', True)
        self.delegate.colour_confidence = get_bool('AO_UI_CONFIDENCE_COLOR', True)

        density = self.settings.get('AO_UI_DENSITY') or 'normal'
        height = ROW_HEIGHTS.get(density, ROW_HEIGHTS['normal'])
        self.table.verticalHeader().setDefaultSectionSize(height)
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, height)
        # Covers need a row tall enough to hold one; in compact rows they are noise.
        self._hide_column(COL_COVER, not self.delegate.show_covers
                          or density == 'compact')

        hidden = {name.strip().upper() for name
                  in (self.settings.get('AO_UI_HIDDEN_COLUMNS') or '').split(',')
                  if name.strip()}
        for column in range(1, len(COLUMNS)):
            self._hide_column(column, COLUMNS[column].upper() in hidden)

        self.refresh_paths_banner()
        self.filter_bar.setVisible(get_bool('AO_UI_SHOW_FILTERS', True))
        self.panel_container.setVisible(get_bool('AO_UI_SHOW_PANEL', True))
        # Button size and labels are toolbar-wide, so the toolbar is rebuilt rather
        # than poked - that is also what re-renders the icons at the new size.
        self.refresh_toolbar()
        self.table.viewport().update()

    def _apply_column_widths(self) -> None:
        """Set the column widths, and do it *after* the window is fully built.

        Applying a stylesheet re-polishes the header, and Qt resets every section to
        the default 100px when that happens - so widths set while building are thrown
        away. Setting them once the central widget is in place makes them stick.
        """
        saved = (self.settings.get('AO_UI_COLUMN_WIDTHS') or '').split(',')
        self._has_saved_widths = len(saved) == len(COLUMNS)
        metrics = self.table.horizontalHeader().fontMetrics()
        for column in range(len(COLUMNS)):
            width = DEFAULT_WIDTHS[column]
            if self._has_saved_widths:
                try:
                    width = max(44, int(saved[column]))
                except ValueError:
                    pass
            # A column narrower than its own heading renders as "NFIDEN". Saved
            # widths predate a renamed column, so the floor is enforced on load
            # rather than trusted to have been right when it was written.
            if COLUMNS[column]:
                width = max(width, metrics.horizontalAdvance(COLUMNS[column]) + 34)
            self.table.setColumnWidth(column, width)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # The viewport has no real width until the window is on screen, and on a
        # scaled display it is not the width the numbers above assume. Fit once.
        if not self._columns_fitted:
            self._columns_fitted = True
            QTimer.singleShot(0, self._restore_split)
            QTimer.singleShot(0, self._fit_columns)
            # Typing is the first thing anyone does here, so the caret starts in the
            # search box. Set a turn late, or the table takes the focus back as it
            # finishes building.
            QTimer.singleShot(0, self.search_box.setFocus)

    def _restore_split(self) -> None:
        """Give the explanation panel back the exact width it was closed at.

        Run from showEvent, one event loop turn late, which is the first moment the
        splitter is the width it is actually going to be - including the jump to
        maximised. Sizes are handed over as (everything else, the saved width) so
        the number that is preserved is the one the user dragged.
        """
        wanted = getattr(self, '_wanted_split', None)
        if not wanted or wanted <= 0:
            return
        total = self.splitter.width() - self.splitter.handleWidth()
        if total <= 0:
            return
        # Never let a stale number swallow the table: the panel gets at most two
        # thirds, which is far more than anyone drags it to.
        right = min(int(wanted), int(total * 0.67))
        self.splitter.setSizes([max(0, total - right), right])

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Narrowing the window must shrink the columns, not push Status out of sight.
        if self._columns_fitted:
            QTimer.singleShot(0, self._fit_columns)
        self._fit_toolbar()

    def _fit_toolbar(self) -> None:
        """Drop the toolbar labels before the toolbar drops the status cluster.

        QToolBar answers not fitting by hiding items from the end and offering them
        under a ">>" button - and the items at the end are the progress bar, Cancel and
        the queue count. So in a window too narrow for labelled buttons, the only way to
        stop a run was a menu you had to know was hiding behind a chevron.

        Labels are worth less than a reachable Cancel, so they are what gives way. Once
        collapsed the labelled width can no longer be measured, so it is remembered from
        the moment it stopped fitting, and labels come back only 60px clear of it - a gap
        that stops the two decisions fighting each other one pixel apart.
        """
        if not self.settings.get_bool('AO_UI_TOOLBAR_LABELS', False):
            return                       # already icon-only; nothing to give up
        labelled = self._tool_button_style()
        icon_only = Qt.ToolButtonStyle.ToolButtonIconOnly
        available = self.toolbar.width()
        if available <= 0:
            return
        if self.toolbar.toolButtonStyle() != icon_only:
            needed = self.toolbar.layout().sizeHint().width()
            if needed > available:
                self._labelled_toolbar_width = needed
                self._set_toolbar_labels(icon_only)
        elif available > self._labelled_toolbar_width + 60:
            self._set_toolbar_labels(labelled)

    def _set_toolbar_labels(self, style) -> None:
        self.toolbar.setToolButtonStyle(style)
        sources = getattr(self, '_sources_button', None)
        if sources is not None:
            sources.setToolButtonStyle(style)

    def _fit_columns(self) -> None:
        """Scale the flexible columns so every column is visible without scrolling.

        Only the text columns flex; the cover, number, confidence and status columns
        are already the size of their contents and shrinking them gains nothing.
        Skipped entirely once the user has dragged a column - their widths win.
        """
        flexible = [COL_FILES, COL_AUTHOR, COL_SERIES, COL_TITLE]
        available = self.table.viewport().width()
        visible = [c for c in range(len(COLUMNS)) if not self.table.isColumnHidden(c)]
        current = {c: self.table.columnWidth(c) for c in visible}
        total = sum(current.values())

        # Saved widths are the user's, and are left alone - unless they no longer fit,
        # which happens when the window or the panel split changed since they were
        # saved. Columns falling off the right edge is never what was wanted.
        # Saved widths are the user's, so they are kept - but only while they fill the
        # viewport. Leaving hundreds of pixels of dead space after the splitter moved
        # is not "respecting the user's widths", it is ignoring the resize.
        if self._has_saved_widths and available - 24 <= total <= available:
            return

        fixed = sum(current[c] for c in visible if c not in flexible)
        base = current if self._has_saved_widths else {
            c: DEFAULT_WIDTHS[c] for c in range(len(COLUMNS))}
        wanted = sum(base[c] for c in flexible if c in current)
        room = available - fixed - 2
        if wanted <= 0 or room <= 0:
            return

        # Share the leftover width by STRETCH, so dragging the splitter right actually
        # widens the columns instead of leaving a blank gutter on the right.
        total_share = sum(STRETCH.get(c, 0.0) for c in flexible if c in current)
        if total_share <= 0:
            return
        spare = room - sum(base[c] for c in flexible if c in current)
        for column in flexible:
            if column not in current:
                continue
            share = STRETCH.get(column, 0.0) / total_share
            width = base[column] + spare * share
            self.table.setColumnWidth(column, max(90, int(width)))

    def _save_column_widths(self) -> None:
        widths = [self.table.columnWidth(c) or DEFAULT_WIDTHS[c]
                  for c in range(len(COLUMNS))]
        self.settings.set('AO_UI_COLUMN_WIDTHS', ','.join(str(w) for w in widths))

    def _hide_column(self, column: int, hidden: bool) -> None:
        """Hide/show a column without disturbing its width.

        Qt resets a section to the default width when it is *shown*, even if it was
        never hidden - so re-applying the visibility of every column on each settings
        change would silently flatten all the column widths back to 100px.
        """
        if self.table.isColumnHidden(column) == hidden:
            return
        width = self.table.columnWidth(column)
        self.table.setColumnHidden(column, hidden)
        if not hidden and width > 0:
            self.table.setColumnWidth(column, width)

    def _sorted_by_user(self, _column: int) -> None:
        self._user_sorted = True
        self.table.horizontalHeader().setSortIndicatorShown(True)

    # ---------------------------------------------------------------- toolbar

    def _build_toolbar(self) -> None:
        """One icon-only toolbar along the bottom, laid out per ``AO_TOOLBAR``."""
        self.toolbar = QToolBar('Actions')
        self.toolbar.setObjectName('actions')
        self.toolbar.setToolButtonStyle(self._tool_button_style())
        self.toolbar.setMovable(False)
        self.toolbar.setFloatable(False)
        self.toolbar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.toolbar.customContextMenuRequested.connect(self._show_toolbar_menu)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolbar)
        self.refresh_toolbar()

    def refresh_toolbar(self) -> None:
        """Rebuild from settings - called again when the Settings page saves."""
        self.toolbar.clear()
        self.tool_actions.clear()
        # The buttons those filters were watching have just been destroyed with the
        # toolbar; keeping the filters would leak one set per rebuild.
        self._right_click_filters.clear()
        # Re-read on every rebuild, not once at construction. Setting it only in
        # _build_toolbar is why "Toolbar Labels" appeared to do nothing until a
        # restart: the Sources button sets its own style, so it was the only one that
        # ever followed the setting.
        self.toolbar.setToolButtonStyle(self._tool_button_style())

        handlers = {
            'scan': self._request_scan,
            'identify': lambda: self._request_resolve(),
            'approve': lambda: self._set_status_selected(STATUS_APPROVED),
            'reject': lambda: self._set_status_selected(STATUS_REJECTED),
            'reset': lambda: self._set_status_selected(STATUS_PENDING),
            'preview': lambda: self._request_apply(preview=True),
            'apply': lambda: self._request_apply(preview=False),
            'undo': self._undo_last,
            # Bare methods would receive QAction's `checked` bool as their first
            # argument, so anything taking a parameter is wrapped.
            'goodreads': lambda: self._search_goodreads(),
            'settings': self.settings_requested.emit,
        }
        # Nothing on this toolbar is accent-coloured. The accent means "selected" in
        # the table, and having it also mean "this button is important" is why the
        # colour scheme stopped carrying information.
        accented = set()
        tinted = {'approve': STATUS_TEXT['approved'], 'reject': STATUS_TEXT['rejected']}

        icon = self._icon_size()
        self.toolbar.setIconSize(QSize(icon, icon))

        for key in parse_layout(self.settings.get('AO_TOOLBAR')):
            if key == SEPARATOR:
                self.toolbar.addSeparator()
                continue
            item = ITEMS_BY_KEY[key]
            if key == 'sources':
                self.toolbar.addWidget(self._build_sources_button(item, icon))
                continue

            colour = tinted.get(key, ACCENT if key in accented else TEXT)
            action = QAction(make_icon(key, colour, icon), item.label, self)
            action.setToolTip(f'{item.label}\n{item.tooltip}')
            action.triggered.connect(handlers[key])
            self.toolbar.addAction(action)
            self.tool_actions[key] = action

            if key == 'undo':
                # Right-clicking undo opens the whole history - edits and applies.
                # This used to rely on customContextMenuRequested and did not fire:
                # the toolbar is the widget with a context-menu policy, and the
                # button is a child that Qt is entitled to let it answer for. An
                # event filter watching the right-button press on the button itself
                # is not answerable to any of that, so it always fires.
                self._watch_right_click(action, self._show_history_dialog)
            if key == 'identify':
                # The badge is painted onto this button's icon, so it has to be
                # findable again when the queue changes.
                self._watch_right_click(action, self.show_queue_window)

        self._add_toolbar_status()
        self.refresh_action_states()
        self._refresh_identify_badge()
        self._tighten_toolbar(icon)
        self.toolbar.setVisible(True)
        # The rebuild made fresh actions; re-apply whatever the running job implies.
        # set_busy covers Cancel; the queue count and the progress bar have to be put
        # back by hand, or reordering the toolbar mid-run loses them until the next job.
        self._queue_action.setVisible(bool(self._queue_labels))
        self.set_busy(self._busy)
        if self._busy:
            done, total, message = self._last_progress
            self.show_progress(done, total, message)
        # A rebuild changes what the toolbar needs, so re-decide whether labels fit.
        self._labelled_toolbar_width = 0
        QTimer.singleShot(0, self._fit_toolbar)

    def _show_cancel_menu(self, anchor=None) -> None:
        """Right-clicking Cancel: the destructive one, named, behind a second click.

        "Cancel all" is not on the left button because binning a queue of forty
        identifications is not something to do by mis-aiming at a small red icon.

        `anchor` is the widget the menu drops from - the Cancel button, or the queue
        count, which offers the same two choices because that is the other place you
        look when you want the run to stop.
        """
        menu = QMenu(self)
        queued = len(self._queue_labels)
        current = menu.addAction('Cancel the job running now')
        current.setEnabled(self._busy)
        current.triggered.connect(self.cancel_current_requested.emit)
        every = menu.addAction(
            f'Cancel all  -  the running job and {plural(queued, "queued job")}'
            if queued else 'Cancel all')
        every.setEnabled(self._busy or bool(queued))
        every.triggered.connect(self.cancel_requested.emit)
        menu.addSeparator()
        menu.addAction('Open the queue...', self.show_queue_window)
        anchor = anchor if anchor is not None else self.cancel_button
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def can_undo(self) -> bool:
        """True when there is actually something to reverse - an edit or an apply."""
        try:
            applies = len(self.history_provider() or [])
        except Exception:              # a controller that is not wired up yet
            applies = 0
        return bool(self._edit_undo) or applies > 0

    def refresh_action_states(self) -> None:
        """Grey out the buttons that would do nothing if you pressed them.

        A button that is enabled is a promise that it does something. Undo with an
        empty history answered a click with "Nothing to undo" in the status strip -
        which is a button teaching you, once per click, that it was never going to
        work. The buttons that need a selection say so the same way, by being
        unavailable until there is one.

        Scan, Sources and Settings are never disabled: they are how you get out of
        the state where everything else is.
        """
        table = getattr(self, 'table', None)
        if not self.tool_actions or table is None:
            # The toolbar is built before the table is; there is nothing to reflect yet.
            return
        selected = bool(table.selectionModel()
                        and table.selectionModel().hasSelection())
        anything = bool(self.entries)
        approved = any(e.status == STATUS_APPROVED for e in self.entries.values())

        available = {
            # Identify needs entries, not a selection - with none it identifies the
            # lot, like Scan and Preview.
            'identify': anything,
            'approve': selected,
            'reject': selected,
            'reset': selected,
            'goodreads': selected,
            # Preview needs no selection - with none it previews the lot - but it does
            # need something to preview.
            'preview': anything,
            'apply': approved,
            'undo': self.can_undo(),
        }
        reasons = {
            'identify': 'Nothing has been scanned yet',
            'approve': 'Select some rows first',
            'reject': 'Select some rows first',
            'reset': 'Select some rows first',
            'goodreads': 'Select some rows first',
            'preview': 'Nothing has been scanned yet',
            'apply': 'Nothing is approved yet - approve some rows first (F5)',
            'undo': 'Nothing to undo yet',
        }
        for key, action in self.tool_actions.items():
            if key not in available:
                continue
            enabled = available[key]
            action.setEnabled(enabled)
            if key == 'identify':
                # Its tooltip carries the queue badge too, so it is written in one
                # place - _refresh_identify_badge, called immediately after this.
                continue
            item = ITEMS_BY_KEY[key]
            action.setToolTip(f'{item.label}\n{item.tooltip}' if enabled
                              else f'{item.label}\n{reasons[key]}')

    def _watch_right_click(self, target, handler: Callable[[], None]) -> None:
        """Call `handler` when `target` is right-clicked.

        `target` is either a QAction on the toolbar - in which case the button Qt built
        for it is looked up - or a widget added to the toolbar directly, which has no
        action to look up.

        Both the press and the context-menu event are watched. Which of the two a
        platform actually delivers to a QToolButton inside a styled QToolBar is not
        something worth being at the mercy of - Windows sends the context-menu event
        on release, X11 on press, and a toolbar with its own context-menu policy can
        absorb either. Whichever arrives first wins, and the second is ignored
        because the handler is idempotent: it raises the window it already opened.
        """
        button = (self.toolbar.widgetForAction(target)
                  if isinstance(target, QAction) else target)
        if button is None:
            return
        button.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        watcher = _RightClickFilter(button, handler)
        button.installEventFilter(watcher)
        # Kept alive by the window: an event filter that is garbage-collected stops
        # filtering, silently, and the button goes back to doing nothing.
        self._right_click_filters.append(watcher)

    def _tighten_toolbar(self, _icon: int) -> None:
        """Take the dead space out of the toolbar row.

        Fusion reserves eight pixels above and below every toolbar button - the
        style's ``PM_ToolBarFrameWidth``, which QToolBarLayout copies into its own
        contents margins. It is a layout margin, not padding, so nothing in the
        stylesheet reaches it, and sixteen pixels of nothing on a row of icons is a
        lot of table to give away.

        Setting the margins once at construction does not hold: the layout re-reads
        the metric on every ``invalidate()``, and clearing the toolbar to rebuild it
        invalidates. So it is re-applied here, at the end of every rebuild, which is
        the only point where it is guaranteed to be the last word.
        """
        layout = self.toolbar.layout()
        if layout is None:
            return
        margins = layout.contentsMargins()
        # Horizontal margins are left alone - buttons do need room at the ends. The
        # bottom margin is larger than the top when labels are on: the label sits at
        # the very bottom edge of the button, so with nothing under it the filter row
        # crops the descenders off "Identify" and "Settings".
        # Eight was not enough: at the larger icon sizes the label sits right on the
        # bottom edge of the button and the filter row underneath cropped the
        # descenders off "Identify" and "Settings". This is the gap between the two
        # rows, so it has to clear the text, not merely separate the widgets.
        below = 16 if self.settings.get_bool('AO_UI_TOOLBAR_LABELS', True) else 4
        layout.setContentsMargins(margins.left(), 1, margins.right(), below)
        layout.setSpacing(2)
        self.toolbar.updateGeometry()

    def _icon_size(self) -> int:
        return {'compact': 26, 'normal': 34, 'comfortable': 40, 'large': 60}.get(
            self.settings.get('AO_UI_ICON_SIZE') or 'comfortable', 40)

    def _tool_button_style(self):
        """Icon only, or icon with its name underneath."""
        if self.settings.get_bool('AO_UI_TOOLBAR_LABELS', False):
            return Qt.ToolButtonStyle.ToolButtonTextUnderIcon
        return Qt.ToolButtonStyle.ToolButtonIconOnly

    def _add_toolbar_status(self) -> None:
        """The old status bar, living on the right-hand end of the toolbar.

        Same information - what is running, how far in, what mode we are in - but in
        the space that was already there instead of a second full-width strip.
        """
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        # A widget with no layout has an *invalid* sizeHint, and QWidgetItem then falls
        # back to the widget's own size - the default 640x480. The toolbar therefore
        # believed it needed ~640px more than it did, decided it had overflowed, and
        # swept the widgets after this one (progress, Cancel, the queue count) into the
        # hidden ">>" extension. That is why Cancel was nowhere to be found while a job
        # ran. An empty zero-margin layout gives the spacer the 0x0 hint it should have
        # had, so it stretches without claiming any space of its own.
        stretch = QHBoxLayout(spacer)
        stretch.setContentsMargins(0, 0, 0, 0)
        stretch.setSpacing(0)
        # Transparent, or it reads as an empty input field sitting in the toolbar.
        spacer.setStyleSheet('background: transparent;')
        self.toolbar.addWidget(spacer)

        self.status_label = QLabel('Ready')
        self.status_label.setStyleSheet(f'color: {TEXT_DIM}; padding: 0 10px;')
        self.status_label.setToolTip('What just happened')
        # Capped so the sentence cannot spend the width the Cancel button needs. The
        # whole message is on the tooltip - see _render_status.
        self.status_label.setMaximumWidth(340)
        # The queue count printed here is a link, not a caption. It was asked for on
        # the Identify button and it is there too - but this is where the sentence
        # "Queued behind 2 jobs" is actually printed, so this is where you go looking
        # for a way to act on it.
        self.status_label.setTextFormat(Qt.TextFormat.RichText)
        self.status_label.setOpenExternalLinks(False)
        self.status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status_label.linkActivated.connect(lambda _: self.show_queue_window())
        self.toolbar.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(150)
        self.progress.setFixedHeight(14)
        self.progress.setFormat('%v / %m')
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setToolTip('Progress of the running job')
        # Visibility is toggled on the *action*, never on the widget. A widget hidden
        # with widget.setVisible(False) is dropped from QToolBarLayout and showing it
        # again never puts it back - it stays parked, unlaid-out, at 0,0 behind the
        # first button. That is what hid the progress bar, Cancel and the queue count
        # for the entire run. See _set_status_widget_visible.
        self._progress_action = self.toolbar.addWidget(self.progress)
        self._progress_action.setVisible(False)

        # Left-click cancels the job that is running *now* and lets the queue carry on;
        # right-click cancels the running job and everything waiting behind it. It used
        # to be icon-only and wired straight to "cancel everything", so the only cancel
        # within reach binned the whole queue - the one thing you cannot get back by
        # pressing the button again. The label is spelled out because an unlabelled red
        # circle beside a progress bar does not say which of the two it does.
        self.cancel_button = QToolButton()
        self.cancel_button.setIcon(make_icon('cancel', STATUS_TEXT['rejected'], 18))
        self.cancel_button.setIconSize(QSize(18, 18))
        self.cancel_button.setText('Cancel')
        self.cancel_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.cancel_button.setToolTip(
            'Cancel the job running now  (Esc)\n'
            'The rest of the queue carries on.\n\n'
            'Right-click to cancel everything, running and queued.')
        self.cancel_button.clicked.connect(self.cancel_current_requested.emit)
        self._watch_right_click(self.cancel_button, self._show_cancel_menu)
        self._cancel_action = self.toolbar.addWidget(self.cancel_button)
        self._cancel_action.setVisible(False)

        # What is running and what is behind it. Clicking opens the queue window,
        # because a count you cannot act on only tells you how long to wait.
        self.queue_button = QToolButton()
        self.queue_button.setText('')
        self.queue_button.setToolTip('Jobs waiting to run - click to see and manage '
                                     'the queue\n\n'
                                     'Right-click to cancel the running job, or all of '
                                     'them.')
        self.queue_button.clicked.connect(lambda: self.show_queue_window())
        # The same two cancels as the Cancel button. The queue count is the other thing
        # you aim at when you want the run to stop, so it answers for it too.
        self._watch_right_click(
            self.queue_button, lambda: self._show_cancel_menu(self.queue_button))
        self._queue_action = self.toolbar.addWidget(self.queue_button)
        self._queue_action.setVisible(False)


    def _build_sources_button(self, item, icon: int) -> QToolButton:
        """A menu of tickable sources - five checkboxes collapsed into one button."""
        button = QToolButton()
        button.setIconSize(QSize(icon, icon))
        button.setIcon(make_icon('sources', TEXT, icon))
        button.setToolTip(f'{item.label}\n{item.tooltip}')
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        # Sources is the one toolbar entry that is a widget rather than a QAction, so
        # it never picked up the toolbar's text-under-icon style and sat there as the
        # only unlabelled button whenever labels were switched on.
        button.setText(item.label)
        button.setToolButtonStyle(self._tool_button_style())
        # Kept so _fit_toolbar can collapse it with the rest; being a widget, it does not
        # follow the toolbar's own toolButtonStyle.
        self._sources_button = button

        menu = QMenu(button)
        header = menu.addAction('Identify with...')
        header.setEnabled(False)
        menu.addSeparator()
        for tier, title, key, tooltip in MODES:
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setToolTip(tooltip)
            # Re-use the action across rebuilds so ticks survive a toolbar reorder.
            previous = self.mode_actions.get(tier)
            action.setChecked(previous.isChecked() if previous is not None
                              else self.settings.get_bool(key, tier != 'search'))
            action.toggled.connect(lambda _=False: self._sources_changed())
            self.mode_actions[tier] = action
        menu.setToolTipsVisible(True)

        button.setMenu(menu)
        return button

    def _sources_changed(self) -> None:
        chosen = [title for tier, title, _k, _t in MODES
                  if self.mode_actions[tier].isChecked()]
        self.show_message('Identifying with: ' + (', '.join(chosen) or 'nothing selected'))

    def _show_toolbar_menu(self, position) -> None:
        menu = QMenu(self)
        menu.addAction('Customise toolbar...',
                       lambda: self.settings_requested_on_tab.emit('Toolbar'))
        menu.exec(self.toolbar.mapToGlobal(position))

    def _install_shortcuts(self) -> None:
        """F-keys, as requested - no single-letter bindings.

        F2 is rename, as it is in every file manager: it edits the cell you are on.
        F4-F8 are the review loop. Settings sits out of the way on F12.
        """
        bindings = [
            ('F2', self._edit_current_cell),
            ('F4', lambda: self._request_resolve()),
            ('F5', lambda: self._set_status_selected(STATUS_APPROVED)),
            ('F6', lambda: self._set_status_selected(STATUS_REJECTED)),
            ('F7', lambda: self._request_apply(preview=True)),
            ('F8', lambda: self._set_status_selected(STATUS_PENDING)),
            ('F12', self.settings_requested.emit),
            ('Ctrl+R', self._request_scan),
            ('Ctrl+Z', self._undo_last),
            ('Ctrl+Y', self._redo_last),
            ('Ctrl+Shift+Z', self._redo_last),
            ('Ctrl+H', self._show_history_dialog),
            ('Ctrl+A', self.table.selectAll),
            ('Ctrl+F', self.search_box.setFocus),
            # F3 is "find" everywhere else, and this is the only find there is here.
            ('F3', self.search_box.setFocus),
            # The running job only, matching the Cancel button beside the progress bar.
            # Esc used to bin the whole queue, which is a lot to lose to a keypress
            # people use to mean "stop this".
            ('Escape', self.cancel_current_requested.emit),
        ]
        for sequence, slot in bindings:
            QShortcut(QKeySequence(sequence), self, activated=slot)

    # ------------------------------------------------------------------ modes

    def selected_tiers(self) -> List[str]:
        """The identification sources currently ticked, in resolver order."""
        return [tier for tier, _title, _key, _tip in MODES
                if self.mode_actions[tier].isChecked()]

    # ------------------------------------------------------------- table data

    def set_entries(self, entries: List[BookEntry]) -> None:
        """Replace the whole table."""
        self.entries = {entry.entry_id: entry for entry in entries}
        self._rebuild_table()

    def upsert_entry(self, entry: BookEntry) -> None:
        """Add or refresh one entry without rebuilding everything."""
        is_new = entry.entry_id not in self.entries
        self.entries[entry.entry_id] = entry
        if is_new:
            self._rebuild_table()
        else:
            row = self._row_for(entry.entry_id)
            if row is not None:
                self._fill_row(row, entry)
            self._apply_filters()
            self.refresh_stats()
        if self.entry_is_selected(entry):
            self.why_panel.show_entry(entry)

    def entry_is_selected(self, entry: BookEntry) -> bool:
        selected = self.selected_entries()
        return bool(selected) and selected[0].entry_id == entry.entry_id

    def refresh_stats(self) -> None:
        """Library counts, in the toolbar, each in its own status colour."""
        # The counts already live in the filter bar ("12 of 38") and in the status
        # filter itself. Printing them a third time in the toolbar was noise.
        self.why_panel.set_stats(self.entries.values())
        # Approving a row is what makes Save available, so the counts and the buttons
        # are refreshed together - every path that changes a status comes through here.
        self.refresh_action_states()

    def _rebuild_table(self) -> None:
        self._updating = True
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.row_ids = []

        for entry in self.entries.values():
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.row_ids.append(entry.entry_id)
            self._fill_row(row, entry)

        self.table.setSortingEnabled(sorting)
        self.table.horizontalHeader().setSortIndicatorShown(self._user_sorted)
        self._updating = False
        self._apply_filters()
        self.refresh_stats()

    def _fill_row(self, row: int, entry: BookEntry) -> None:
        previous = self._updating
        self._updating = True
        # Writing into a sorted table makes Qt re-sort on the spot, so approving one
        # row teleports it somewhere else in the list mid-review. Rows only move when
        # you sort, rescan, or reopen - unless you ask for live re-sorting.
        resort = self.settings.get_bool('AO_UI_RESORT_LIVE', False)
        was_sorting = self.table.isSortingEnabled()
        if not resort:
            self.table.setSortingEnabled(False)

        filename, folder = self._files_summary(entry)

        # Sorted on the folder path, so clicking this column groups the table by where
        # the books live rather than by what they are called.
        cover = _KeyedItem(f'{folder}/{filename}')
        pixmap = self._cover_pixmap(entry)
        if pixmap is not None:
            cover.setData(Qt.ItemDataRole.DecorationRole, pixmap)
        cover.setData(ROLE_KIND, KIND_COVER)
        cover.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        cover.setToolTip('Cover art embedded in the file, or an image in its folder.\n'
                         'Click this column\'s header to sort the table by folder path.')
        self.table.setItem(row, COL_COVER, cover)

        files = QTableWidgetItem(filename)
        files.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        files.setToolTip(f'{entry.folder}\n\n' + '\n'.join(entry.audio_files))
        files.setData(ROLE_ENTRY_ID, entry.entry_id)
        files.setData(ROLE_SECONDARY, folder)
        files.setData(ROLE_KIND, KIND_FILES)
        # A row can be repainted mid-job (a value arrives, a filter runs), so the
        # progress a long job is reporting has to be re-attached rather than lost.
        progress = self._row_progress.get(entry.entry_id)
        if progress is not None:
            files.setData(ROLE_PROGRESS, progress[0])
            files.setData(ROLE_PROGRESS_TEXT, progress[1])
        self.table.setItem(row, COL_FILES, files)

        for column, name in EDITABLE.items():
            field = entry.get_field(name)
            item = QTableWidgetItem(str(field.value))
            item.setForeground(QColor(self._field_colour(entry, field)))
            item.setToolTip(
                f'{field.value or "(empty)"}\n'
                f'source: {field.source or "none"}\n'
                f'confidence: {field.confidence:.0%}'
                + (f'\nconfirmed by: {", ".join(field.corroborated_by)}'
                   if field.corroborated_by else '')
                + '\n\nDouble-click to edit.')
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter if column == COL_INDEX
                else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, column, item)

        confidence = entry.confidence()
        # Shown as "83%", sorted as 0.83 - hence the custom item rather than an
        # EditRole float, which would replace the text with a raw "0,83".
        conf_item = _NumericItem(f'{confidence:.0%}', confidence)
        conf_item.setData(ROLE_CONFIDENCE, confidence)
        conf_item.setData(ROLE_KIND, KIND_CONFIDENCE)
        conf_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        conf_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        conf_item.setToolTip('How sure the identification is, averaged over the fields')
        self.table.setItem(row, COL_CONF, conf_item)

        status = QTableWidgetItem(pretty_status(entry.status))
        status.setData(ROLE_KIND, KIND_STATUS)
        status.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        status.setToolTip(f'Review status: {pretty_status(entry.status)}. The stripe '
                          f'down the left of '
                          f'the row and this pill are the only things colour means.')
        self.table.setItem(row, COL_STATUS, status)
        if not resort and was_sorting:
            self.table.setSortingEnabled(True)

        # The delegate paints the stripe and the pill from this, on every cell.
        for column in range(len(COLUMNS)):
            item = self.table.item(row, column)
            if item is not None:
                item.setData(ROLE_STATUS, entry.status)

        self._updating = previous

    def _confidence_filter_chosen(self, _index: int) -> None:
        """"Custom threshold..." asks for a number and becomes a real filter entry."""
        if not self.confidence_filter.currentText().startswith('Custom'):
            return

        percent, ok = QInputDialog.getInt(
            self, 'Confidence threshold',
            'Show rows whose confidence is at least this percentage:',
            int(self._custom_confidence * 100), 0, 100, 5)
        if not ok:
            self.confidence_filter.setCurrentIndex(0)
            return

        self._custom_confidence = percent / 100.0
        label = f'At least {percent}%'
        # Replace any previous custom entry so the list does not grow every time.
        for index in range(self.confidence_filter.count()):
            if self.confidence_filter.itemText(index).startswith('At least'):
                self.confidence_filter.removeItem(index)
                break
        self.confidence_filter.insertItem(4, label)
        self.confidence_filter.setCurrentIndex(4)
        self._apply_filters()

    def _field_colour(self, entry: BookEntry, field) -> str:
        """One row, one colour story.

        A decided row is that decision, all the way across: approved is green,
        rejected is red. Only undecided rows are free to say anything else, and even
        then the per-source hues are opt-in - a row that was amber-for-regex, green
        for its status and blue in one cell was three claims at once, none legible.
        """
        if not field.value:
            return TEXT_FAINT
        if entry.status == STATUS_APPROVED:
            return STATUS_TEXT['approved']
        if entry.status == STATUS_REJECTED:
            return STATUS_TEXT['rejected']
        if self.settings.get_bool('AO_UI_COLOR_BY_SOURCE', False):
            return source_color(field.source)
        return TEXT

    def _files_summary(self, entry: BookEntry):
        """(filename, path-from-root) - the delegate draws them as two lines.

        The filename leads because it is what identifies the row at a glance. The
        path sits underneath because it is context: the author, the series, or both
        are very often only in the folders above the file.
        """
        count = len(entry.audio_files)
        folder = self._relative_folder(entry)
        if count == 0:
            return 'no audio files', folder
        if count == 1:
            return entry.audio_files[0], folder
        # Multi-file books are a different thing to review, so they say so plainly.
        return (f'{entry.audio_files[0]}   (+{count - 1} more)', folder)

    def _relative_folder(self, entry: BookEntry) -> str:
        """The entry's folder as the *whole* path below the input root.

        "Sci-Fi\\Kitasei, Yume\\The Deep Sky", not "The Deep Sky". The point of this
        line is that the author and the series are very often only in the folders above
        the file, and the deepest folder alone throws away exactly that. Every fallback
        below therefore keeps as much of the path as it can, and the last resort is the
        full absolute path rather than a single component.
        """
        root = None
        try:
            root = self.settings.get_path('AO_INPUT_DIR')
        except (TypeError, OSError):
            pass

        # The scanner records the file's path relative to the library root, so its
        # parent is the answer whenever it is there.
        if entry.relative_path:
            relative = Path(entry.relative_path)
            # relative_path may name the file or the folder; a suffix means the file.
            folder = relative.parent if relative.suffix else relative
            text = str(folder).strip('\\/')
            if text not in ('.', ''):
                return text

        folder = Path(entry.folder)
        if root is not None:
            try:
                text = str(folder.relative_to(root)).strip('\\/')
                if text not in ('.', ''):
                    return text
            except ValueError:
                # Outside the configured input folder - which is a thing worth seeing,
                # so show where it really is instead of pretending it is inside.
                return str(folder)
        return str(folder)

    def _cover_pixmap(self, entry: BookEntry) -> Optional[QPixmap]:
        """Embedded cover art, else a folder image, else nothing (#26)."""
        cached = getattr(entry, '_cover_pixmap', None)
        if cached is not None:
            return cached if not cached.isNull() else None

        pixmap = QPixmap()
        try:
            from ..metadata_extractor import MetadataExtractor
            data = MetadataExtractor().extract_cover(entry.primary_audio)
            if data:
                pixmap.loadFromData(data)
            if pixmap.isNull() and entry.image_files:
                pixmap.load(str(Path(entry.folder) / entry.image_files[0]))
        except Exception as exc:
            logger.debug('No cover for %s: %s', entry.entry_id, exc)

        if not pixmap.isNull():
            pixmap = pixmap.scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
        # Cache on the entry so scrolling doesn't re-decode every repaint.
        object.__setattr__(entry, '_cover_pixmap', pixmap)
        return pixmap if not pixmap.isNull() else None

    # --------------------------------------------------------------- filtering

    def _apply_filters(self) -> None:
        text = self.search_box.text().strip().lower()
        status = self.status_filter.currentText()
        missing = self.missing_filter.currentText()
        confidence = self.confidence_filter.currentText()
        shape = dict(SHAPE_FILTERS).get(self.shape_filter.currentText())
        visible = 0

        for row in range(self.table.rowCount()):
            entry = self._entry_at(row)
            if entry is None:
                continue
            show = True

            if text:
                haystack = ' '.join([entry.value('author'), entry.value('series'),
                                     entry.value('title'), entry.entry_id]).lower()
                show = text in haystack
            if show and status != 'All statuses':
                show = entry.status == status.lower()
            if show and missing != 'Any completeness':
                gaps = entry.missing_fields()
                if missing == 'Missing any field':
                    show = bool(gaps)
                elif missing == 'Complete only':
                    show = not gaps
                elif missing in ('Missing cover', 'Has cover'):
                    has_cover = self._cover_pixmap(entry) is not None
                    show = has_cover if missing == 'Has cover' else not has_cover
                else:
                    show = missing.replace('Missing ', '').replace('index', 'series_index') in gaps
            if show and confidence != 'Any confidence':
                value = entry.confidence()
                if confidence == 'Below 50%':
                    show = value < 0.5
                elif confidence == 'Below 80%':
                    show = value < 0.8
                elif confidence.startswith('At least'):
                    show = value >= self._custom_confidence
                elif confidence.startswith('Below') :
                    show = value < self._custom_confidence
                else:
                    show = value >= 0.8
            if show and shape is not None:
                show = bool(shape(entry))

            self.table.setRowHidden(row, not show)
            visible += show

        total = self.table.rowCount()
        self.count_label.setText(f'{visible} of {total}')

    # ----------------------------------------------------------------- events

    def _selection_changed(self) -> None:
        entries = self.selected_entries()
        self.why_panel.show_entry(entries[0] if entries else None,
                                  extra_selected=max(0, len(entries) - 1))
        # Half the toolbar acts on the selection, so it follows the selection.
        self.refresh_action_states()

    def _item_edited(self, item: QTableWidgetItem) -> None:
        """A manually typed value is authoritative - source 'user', confidence 1.0."""
        if self._updating:
            return
        field = EDITABLE.get(item.column())
        if field is None:
            return
        entry = self._entry_at(item.row())
        if entry is None:
            return

        value = item.text().strip()
        current = entry.get_field(field)
        if value == str(current.value):
            return

        from ..models import Field, clean_value
        cleaned = clean_value(field, value)
        self._record_edit(f'{field} typed on "{self._label_for(entry)}"',
                          [(entry.entry_id, field, current)])
        setattr(entry, field, Field(value=cleaned, source='user', confidence=1.0))
        entry.log('user', f'{field} set manually to "{cleaned}"')
        self._fill_row(item.row(), entry)
        self.why_panel.show_entry(entry)
        self.entry_changed(entry)

    def entry_changed(self, entry: BookEntry) -> None:
        """Hook the controller overrides to persist changes."""

    # ----------------------------------------------------------- context menu

    def _show_context_menu(self, position) -> None:
        """Everything here acts on the *whole* selection, never on just the first row."""
        entries = self.selected_entries()
        if not entries:
            return

        count = len(entries)
        # One way of saying "this many". Every entry that acts on the whole selection
        # gets the same bare "(5)" - "Copy files (5)" and "Set series (5)" are the
        # same kind of statement, so they are not written two different ways.
        suffix = '' if count == 1 else f' ({count})'
        menu = QMenu(self)
        menu.setToolTipsVisible(True)

        pending_section = ['']

        def section(title: str) -> None:
            """Remember a heading. It is only drawn once something lands under it."""
            pending_section[0] = title

        def add(text: str, slot, tooltip: str = '', enabled: bool = True):
            """One menu entry, or nothing at all.

            An entry that cannot be used is not shown. No greyed-out rows, no "-
            select two or more rows" suffixes explaining why you may not have it: the
            menu lists what you can do to this selection, right now, and nothing else.
            A heading with nothing under it is not drawn either.
            """
            if not enabled:
                return None
            if pending_section[0]:
                if menu.actions():
                    menu.addSeparator()
                heading = menu.addAction(pending_section[0])
                heading.setEnabled(False)
                font = QFont(menu.font())
                font.setBold(True)
                font.setPointSizeF(max(7.0, menu.font().pointSizeF() - 1.0))
                font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
                heading.setFont(font)
                pending_section[0] = ''
            action = menu.addAction(text, slot)
            if tooltip:
                action.setToolTip(tooltip)
            return action

        multiple = count > 1
        mergeable = [e for e in entries if len(e.audio_files) > 1]
        applied = [e for e in entries if e.applied_path]
        # Which column was actually right-clicked. Author- and series-specific
        # actions only appear on their own column, because "set author" offered
        # from the Title cell is an action you did not ask for on a field you were
        # not looking at.
        clicked = EDITABLE.get(self.table.columnAt(position.x()))

        # Ordered the way the work goes: identify it, correct it, then act on the
        # files. Destructive things last, where they are hard to hit.
        section('IDENTIFY')
        add(f'Identify{suffix}  (F4)', lambda: self._request_resolve(),
            'Run the ticked sources over every selected row')
        # One source per entry rather than a dialog with a dropdown: picking a
        # source is one click, not a click, a scroll and an OK.
        sources = menu.addMenu('Identify with...')
        sources.setToolTipsVisible(True)
        for tier, title, _key, tooltip in MODES:
            action = sources.addAction(
                title, lambda _=False, t=tier: self.resolve_requested.emit(entries, [t]))
            action.setToolTip(tooltip)
            if tier == 'api':
                # "Book Databases" is five separate databases, and running the tier ran
                # whichever of them answered first. They are listed individually so
                # one can be asked by name, and the tier entry above now means
                # "every one of them".
                self._add_database_menu(sources, entries)
        add(f'Preview{suffix}  (F7)',
            lambda: self.apply_requested.emit(entries, True),
            'Show where the selected rows would end up, without touching any files')
        add(f'Search Goodreads{suffix}',
            lambda: self._search_goodreads(entries),
            'Open a Goodreads search for each selected book in your browser')

        section('EDIT')
        # "Rename cell" named the widget rather than the thing: you clicked Author,
        # so the menu says Author. Falls back to the generic word only when the click
        # landed somewhere with no field behind it.
        field_label = FIELD_LABELS.get(clicked, 'cell')
        add(f'Rename {field_label}  (F2)', self._edit_current_cell,
            f'Edit the {field_label.lower()} of this row, exactly as '
            'double-clicking it does',
            enabled=not multiple)
        add(f'Edit in a grid{suffix}...  (F2)', lambda: self._edit_grid(entries),
            'Every field of every selected row, editable from the keyboard',
            enabled=multiple)
        # Writing one value across many rows only makes sense for fields books
        # share, and only on the column you clicked.
        if clicked in ('author', 'series') and multiple:
            label = clicked
            add(f'Set {label}{suffix}...',
                lambda _=False, f=clicked: self._bulk_edit(f),
                f'Type one {label} and write it to every selected row')
            first = entries[0].value(clicked)
            add(f'Set {label} to "{first}"{suffix}',
                lambda _=False, f=clicked: self._fill_down(entries, f),
                f'Write the {label} of the first selected row onto the other '
                f'{count - 1}',
                enabled=bool(first))
        # Named after what it would actually blank: "Clear" alone gave no way to tell
        # a three-cell selection from a three-column one until after you had done it.
        targets = self._clear_targets()
        add(self._clear_label(targets), self._clear_selected_cells,
            'Blank the selected cells so they can be identified again. Select whole '
            'columns or individual cells to choose what gets cleared.',
            enabled=bool(targets))

        if SHOW_REVIEW_SECTION:
            section('REVIEW')
            add(f'Approve{suffix}  (F5)',
                lambda: self._set_status_selected(STATUS_APPROVED),
                'Mark every selected row approved, ready to be applied')
            add(f'Reject{suffix}  (F6)',
                lambda: self._set_status_selected(STATUS_REJECTED),
                'Mark every selected row rejected - rejected rows are never applied')
            add(f'Reset to pending{suffix}  (F8)',
                lambda: self._set_status_selected(STATUS_PENDING),
                'Clear the decision on every selected row and return it to pending')

        section('FILES')
        add(f'Open folder{suffix}', lambda: self._open_folders(entries),
            'Open each selected book folder in the file manager')
        self._add_copy_entries(menu, add, entries, suffix)
        add('Merge chapters into one .m4b...'
            + (f' ({len(mergeable)})' if len(mergeable) > 1 else ''),
            lambda: self._merge_selected(mergeable),
            'Join the chapter files of every selected multi-file book into one .m4b',
            enabled=bool(mergeable))
        add(f'Open where it was written{suffix}',
            lambda: self._open_folders(applied),
            'Open the output folder these rows were written to',
            enabled=bool(applied))

        menu.exec(self.table.viewport().mapToGlobal(position))

    def _add_database_menu(self, parent: QMenu, entries: List[BookEntry]) -> None:
        """The individual book databases, under the "Book DBs" tier entry.

        Running the tier queries every configured database; these run exactly one, by
        name, and always go to the network - a source you asked for by name replaying a
        cached miss is indistinguishable from that source being broken.
        """
        from ..api_query import AVAILABLE_SOURCES

        configured = [s.lower() for s in self.settings.get_list('AO_API_SOURCES')]
        submenu = parent.addMenu('Database...')
        submenu.setToolTipsVisible(True)
        for key, label, blurb in AVAILABLE_SOURCES:
            action = submenu.addAction(
                label + ('' if key in configured else '   (not enabled in Settings)'),
                lambda _=False, k=key: self.resolve_requested.emit(
                    entries, [f'api:{k}']))
            action.setToolTip(f'{blurb}\nQueries only this database, ignoring the '
                              f'cache.')

    # ------------------------------------------------------------- copy actions

    def _copy_recents(self) -> List[str]:
        """The copy actions in most-recently-used order, unknown ones dropped."""
        stored = [key.strip() for key
                  in (self.settings.get('AO_UI_COPY_RECENT_LIST') or '').split(',')
                  if key.strip() in COPY_BY_ID]
        # Anything never used yet keeps its declaration order behind the used ones.
        return stored + [key for key, *_ in COPY_ACTIONS if key not in stored]

    def _copy_used(self, key: str) -> None:
        """Remember that this copy action was the last one used."""
        order = [key] + [other for other in self._copy_recents() if other != key]
        self.settings.set('AO_UI_COPY_RECENT_LIST', ','.join(order))
        try:
            self.settings.save()
        except OSError as exc:
            logger.warning('Could not save the copy history: %s', exc)

    def _run_copy(self, key: str, entries: List[BookEntry]) -> None:
        action = COPY_BY_ID.get(key)
        if action is None:
            return
        self._copy_used(key)
        getattr(self, action[2])(entries)

    def _add_copy_entries(self, menu: QMenu, add, entries: List[BookEntry],
                          suffix: str) -> None:
        """The recently-used copy actions, then a "Copy..." submenu with all of them.

        How many get promoted out of the submenu is AO_UI_COPY_RECENTS. Ask for as many
        as there are actions (or more) and they are all promoted and the submenu goes
        away - at that point it would only be a second copy of the same list.
        """
        wanted = max(0, self.settings.get_int('AO_UI_COPY_RECENTS', 1))
        recents = self._copy_recents()
        promoted = recents[:min(wanted, len(COPY_ACTIONS))]

        for key in promoted:
            _key, label, _method, tooltip = COPY_BY_ID[key]
            add(f'{label}{suffix}', lambda _=False, k=key: self._run_copy(k, entries),
                tooltip)

        if len(promoted) >= len(COPY_ACTIONS):
            return

        # `add` draws the pending section heading, so the submenu is attached through
        # a placeholder that guarantees FILES is on screen before it appears.
        submenu = QMenu('Copy...', menu)
        submenu.setToolTipsVisible(True)
        submenu.setToolTip('Everything that can be put on the clipboard for this '
                           'selection')
        for key, label, _method, tooltip in COPY_ACTIONS:
            action = submenu.addAction(
                f'{label}{suffix}', lambda _=False, k=key: self._run_copy(k, entries))
            action.setToolTip(tooltip)
        if promoted:
            menu.addMenu(submenu)
        else:
            # Nothing was promoted, so this submenu is the first thing in the section
            # and has to carry the heading itself.
            placeholder = add('Copy...', lambda: None)
            if placeholder is not None:
                placeholder.setMenu(submenu)

    def _preview_names(self, entries: List[BookEntry]) -> None:
        """Render the current templates against the selection, as plain text.

        No longer on the right-click menu: it and "Preview apply" answered the same
        question, and the apply preview answers it better - it resolves against the
        real output folder and shows collisions. Kept because it is the only view of
        the templates alone, and cheap to put back on a menu.
        """
        from ..paths import build_destination, render_template

        output = self.settings.get('AO_OUTPUT_DIR')
        folder_template = self.settings.get('AO_OUTPUT_TEMPLATE')
        file_template = self.settings.get('AO_FILE_TEMPLATE')
        renaming = self.settings.get_bool('AO_RENAME_FILES', True)

        lines = [f'Output folder: {output}',
                 f'Folder template: {folder_template}',
                 f'File template:   {file_template if renaming else "(renaming is off)"}',
                 '']
        for entry in entries:
            values = {'author': entry.value('author'), 'series': entry.value('series'),
                      'series_index': entry.value('series_index'),
                      'title': entry.value('title')}
            folder = render_template(folder_template, values)
            lines.append(folder + '/')
            width = max(2, len(str(len(entry.audio_files))))
            for number, name in enumerate(entry.audio_files, start=1):
                if renaming:
                    suffix = Path(name).suffix.lower()
                    rendered = render_template(file_template, dict(
                        values,
                        file_index=(f'{number:0{width}d}'
                                    if len(entry.audio_files) > 1 else ''),
                        extension=suffix.lstrip('.')))
                    if not rendered.lower().endswith(suffix):
                        rendered += suffix
                else:
                    rendered = name
                lines.append(f'    {rendered}')
            lines.append('')
        self.show_report('Preview names', '\n'.join(lines))

    def _run_one_source(self, tier: str) -> None:
        """The Run button on a card in the explanation panel.

        `tier` is either a tier name ("api") or one database within it ("api:itunes"),
        which is what the Run button on a single database's card sends.
        """
        entries = self.selected_entries()
        if not entries:
            self.show_message('Select a row first')
            return
        name, _, source = tier.partition(':')
        self.show_message(f'Asking {source or name} about this book...')
        self.resolve_requested.emit(entries[:1], [tier])

    def _label_for(self, entry: BookEntry) -> str:
        """The shortest thing that names a book in a message: its title, else its file."""
        return entry.value('title') or Path(entry.primary_audio).stem

    def _fill_down(self, entries: List[BookEntry], field: str) -> None:
        """The first row's value for one field, written onto every other selected row.

        The menu entry names the value it is about to write, so this needs no
        confirmation dialog - you already read what you were choosing, and Ctrl+Z
        puts it back.
        """
        if len(entries) < 2:
            return
        value = entries[0].value(field)
        if not value:
            return
        self._write_field(entries[1:], field, str(value))

    def set_row_progress(self, entry_id: str, fraction: Optional[float],
                         label: str = '') -> None:
        """Show (or clear, with ``fraction=None``) a progress wash across one row."""
        if fraction is None:
            self._row_progress.pop(entry_id, None)
        else:
            self._row_progress[entry_id] = (max(0.0, min(1.0, fraction)), label)

        row = self._row_for(entry_id)
        if row is None:
            return
        item = self.table.item(row, COL_FILES)
        if item is None:
            return
        previous, self._updating = self._updating, True
        item.setData(ROLE_PROGRESS, None if fraction is None else fraction)
        item.setData(ROLE_PROGRESS_TEXT, label)
        self._updating = previous
        self.table.viewport().update()

    def _merge_selected(self, entries: List[BookEntry]) -> None:
        """Set the merges up in one dialog, then queue them like any other job."""
        from .merge_dialog import MergeDialog

        if not entries:
            return
        dialog = MergeDialog(entries, self.settings,
                             self.settings.get_path('AO_OUTPUT_DIR'), parent=self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            if dialog.wants_identify:
                # They chose "identify these first" - so do that, rather than making
                # them find the menu entry again.
                self._request_resolve_on(entries)
            return

        plans = dialog.plans()
        if not plans:
            self.show_message('Nothing could be named, so nothing was merged')
            return
        for plan in plans:
            self.merge_requested.emit(plan)
        self.show_message(f'Queued {plural(len(plans), "merge")}')

    def _request_resolve_on(self, entries: List[BookEntry]) -> None:
        tiers = self.selected_tiers()
        if not tiers:
            self.show_message('Tick at least one source in the Sources menu')
            return
        self.resolve_requested.emit(entries, tiers)

    def _open_folders(self, entries: List[BookEntry]) -> None:
        """Opening 40 explorer windows helps nobody, so ask past a handful."""
        if len(entries) > 5 and QMessageBox.question(
                self, 'Open folders',
                f'Open {len(entries)} separate folders?'
        ) != QMessageBox.StandardButton.Yes:
            return
        import os
        import subprocess
        for entry in entries:
            path = entry.applied_path or entry.folder
            try:
                if os.name == 'nt':
                    os.startfile(path)
                else:
                    subprocess.Popen(['xdg-open', path])
            except OSError as exc:
                self.show_message(f'Could not open {path}: {exc}')
                return
        self.show_message(f'Opened {len(entries)} folder(s)')

    def _copy_paths(self, entries: List[BookEntry]) -> None:
        text = '\n'.join(entry.applied_path or entry.folder for entry in entries)
        self._to_clipboard(text, f'Copied {len(entries)} path(s)')

    def _copy_rows(self, entries: List[BookEntry]) -> None:
        """The selection as a Markdown table, with a header row and the path.

        This used to emit only the four identity fields, so copying a row that was not
        identified yet put three tab characters on the clipboard - indistinguishable
        from having copied nothing. The path is always known, so there is always
        something to paste.
        """
        header = ['Path', 'Author', 'Series', '#', 'Title', 'Confidence', 'Status']
        rows = [[
            self._relative_folder(entry),
            entry.value('author'), entry.value('series'),
            str(entry.value('series_index')), entry.value('title'),
            f'{entry.confidence():.0%}', pretty_status(entry.status),
        ] for entry in entries]

        # Pad the columns so the table is readable as plain text too - Markdown does
        # not care about the alignment, but whoever pastes it somewhere else does.
        widths = [max(len(header[i]), *(len(r[i]) for r in rows)) if rows
                  else len(header[i]) for i in range(len(header))]

        def line(cells):
            return '| ' + ' | '.join(c.replace('|', r'\|').ljust(widths[i])
                                     for i, c in enumerate(cells)) + ' |'

        text = '\n'.join([line(header),
                          '|-' + '-|-'.join('-' * w for w in widths) + '-|',
                          *(line(r) for r in rows)])
        self._to_clipboard(text,
                           f'Copied {len(entries)} row(s) as a Markdown table')

    def _audio_paths(self, entries: List[BookEntry]) -> List[Path]:
        """Every audio file of every selected book, in selection order."""
        return [Path(entry.applied_path or entry.folder) / name
                for entry in entries for name in entry.audio_files]

    def _copy_file_paths(self, entries: List[BookEntry]) -> None:
        paths = self._audio_paths(entries)
        if not paths:
            self.show_message('The selection has no audio files')
            return
        self._to_clipboard('\n'.join(str(p) for p in paths),
                           f'Copied {len(paths)} file path(s)')

    def _copy_file_names(self, entries: List[BookEntry]) -> None:
        names = [name for entry in entries for name in entry.audio_files]
        if not names:
            self.show_message('The selection has no audio files')
            return
        self._to_clipboard('\n'.join(names), f'Copied {len(names)} file name(s)')

    def _copy_files(self, entries: List[BookEntry]) -> None:
        """The files themselves on the clipboard, pasteable in a file manager.

        A book that is the only one in its folder copies the folder instead: the
        folder is the book in that case, and pasting the loose files would leave the
        cover art and the sidecars behind.
        """
        from PyQt6.QtCore import QUrl
        from PyQt6.QtCore import QMimeData
        from PyQt6.QtWidgets import QApplication

        # How many rows share each folder - across the whole table, not just the
        # selection, so copying one row out of a three-book folder still copies files.
        occupants: Dict[str, int] = {}
        for entry in self.entries.values():
            occupants[entry.folder] = occupants.get(entry.folder, 0) + 1

        paths: List[Path] = []
        folders = 0
        for entry in entries:
            folder = entry.applied_path or entry.folder
            if occupants.get(entry.folder, 1) <= 1:
                paths.append(Path(folder))
                folders += 1
            else:
                paths.extend(Path(folder) / name for name in entry.audio_files)

        paths = [p for p in paths if p.exists()]
        if not paths:
            self.show_message('Nothing on disk to copy for the selection')
            return

        data = QMimeData()
        data.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        data.setText('\n'.join(str(p) for p in paths))
        QApplication.clipboard().setMimeData(data)
        detail = (f'{folders} folder(s)' if folders == len(paths)
                  else f'{len(paths)} item(s)')
        self.show_message(f'Copied {detail} to the clipboard - paste in a file manager')

    def _to_clipboard(self, text: str, message: str) -> None:
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
        self.show_message(message)

    # ------------------------------------------------------------------ undo

    def _record_edit(self, label: str, changes: List[tuple]) -> None:
        """Remember the *before* state of an edit so Ctrl+Z can put it back.

        `changes` is (entry_id, field name, the Field as it was). Every path that
        writes a field from the UI calls this first - typing in a cell, clearing
        cells, the grid editor, filling a value down, bulk edit. Whatever a source
        writes during identification is not recorded: that already has its own undo
        in the form of re-running or clearing the row.
        """
        if not changes:
            return
        self._edit_undo.append({
            'label': label,
            'changes': changes,
            # Where the apply journal stood when this edit happened, so an apply made
            # afterwards is undone before the edits that preceded it.
            'history': len(self.history_provider()),
        })
        del self._edit_undo[:-100]
        # A new action branches the history: whatever had been undone can no longer be
        # put back, because it is no longer what comes next.
        self._edit_redo.clear()
        self._refresh_history_dialog()
        self.refresh_action_states()

    def _snapshot(self, entries, fields: List[str]) -> List[tuple]:
        """The current value of `fields` on `entries`, in _record_edit's shape."""
        return [(entry.entry_id, name, entry.get_field(name))
                for entry in entries for name in fields]

    def _undo_last(self) -> None:
        """One undo for the whole window: the last thing you did, whatever it was.

        Edits and applies are two separate histories, so they are ordered by the
        journal length captured with each edit: if the journal has grown since the
        newest edit, an apply happened after it and that comes off first.
        """
        history = self.history_provider()
        if self._edit_undo and self._edit_undo[-1]['history'] >= len(history):
            self._undo_last_edit()
            return
        if not history:
            self.show_message('Nothing to undo')
            return
        self.undo_requested.emit(len(history) - 1)

    def _undo_last_edit(self) -> None:
        record = self._edit_undo.pop()
        # Capture what the cells hold *now*, before restoring - that is exactly what a
        # redo has to put back, and it is only knowable at this moment.
        after = [(entry_id, name, self.entries[entry_id].get_field(name))
                 for entry_id, name, _field in record['changes']
                 if entry_id in self.entries]
        self._restore(record['changes'], 'undo')
        self._edit_redo.append({'label': record['label'], 'changes': after,
                                'history': record.get('history', 0)})
        del self._edit_redo[:-100]
        self.show_message(f'Undid: {record["label"]}')
        self._refresh_history_dialog()

    def _redo_last_edit(self) -> bool:
        """Put back the most recently undone edit. False when there is nothing to redo."""
        if not self._edit_redo:
            return False
        record = self._edit_redo.pop()
        before = [(entry_id, name, self.entries[entry_id].get_field(name))
                  for entry_id, name, _field in record['changes']
                  if entry_id in self.entries]
        self._restore(record['changes'], 'redo')
        # Straight back onto the undo stack, without going through _record_edit -
        # that would clear the redo stack we are in the middle of walking.
        self._edit_undo.append({'label': record['label'], 'changes': before,
                                'history': record.get('history', 0)})
        self.show_message(f'Redid: {record["label"]}')
        self._refresh_history_dialog()
        return True

    def _restore(self, changes: List[tuple], verb: str) -> None:
        """Write a set of (entry_id, field, Field) back onto the entries, and repaint."""
        touched = set()
        for entry_id, name, field in changes:
            entry = self.entries.get(entry_id)
            if entry is None:
                continue
            setattr(entry, name, field)
            entry.log('user', f'{name} restored by {verb}')
            touched.add(entry_id)

        for entry_id in touched:
            row = self._row_for(entry_id)
            if row is not None:
                self._fill_row(row, self.entries[entry_id])
            self.entry_changed(self.entries[entry_id])
        self._apply_filters()
        self.refresh_stats()
        entries = [self.entries[i] for i in touched if i in self.entries]
        if entries:
            self.why_panel.show_entry(entries[0], extra_selected=len(entries) - 1)

    def _redo_last(self) -> None:
        """Ctrl+Y / Ctrl+Shift+Z."""
        if not self._redo_last_edit():
            self.show_message('Nothing to redo')

    def _combined_history(self) -> List[dict]:
        """Edits and applies as one chronological list, oldest first.

        Left-clicking undo already treats them as one history - it compares journal
        lengths to decide which came last - so the menu shows the same single list
        rather than only the applies. Each entry is
        ``{'kind', 'label', 'index'}``; ``index`` is the journal index for an apply.
        """
        applies = self.history_provider()
        items: List[tuple] = []
        for position, record in enumerate(self._edit_undo):
            # (when it happened, edits-before-applies at the same point, order)
            items.append(((record['history'], 0, position),
                          {'kind': 'edit', 'label': record['label'], 'index': position}))
        for index in range(len(applies)):
            items.append(((index + 1, 1, index),
                          {'kind': 'apply', 'label': str(applies[index]),
                           'index': index}))
        items.sort(key=lambda pair: pair[0])
        return [entry for _, entry in items]

    def history_state(self) -> Dict[str, List[dict]]:
        """What the History window draws: what has been done, and what can be redone.

        The future only ever contains edits. An apply is a filesystem transaction, and
        offering to "redo" one from a list would mean silently moving files again from
        a window whose job is to describe history - so undoing an apply simply ends the
        redo chain, and the window says so.
        """
        past = [{'kind': step['kind'], 'label': step['label']}
                for step in self._combined_history()]
        future = [{'kind': 'edit', 'label': record['label']}
                  for record in reversed(self._edit_redo)]
        return {'past': past, 'future': future}

    def _show_history_dialog(self) -> None:
        from .history_dialog import HistoryDialog

        if self._history_dialog is None:
            dialog = HistoryDialog(self.history_state, parent=self)
            dialog.undo_to.connect(self._undo_to_position)
            dialog.redo_to.connect(self._redo_to_position)
            dialog.finished.connect(lambda _: setattr(self, '_history_dialog', None))
            self._history_dialog = dialog
        self._history_dialog.refresh()
        self._history_dialog.show()
        self._history_dialog.raise_()
        self._history_dialog.activateWindow()

    def _refresh_history_dialog(self) -> None:
        if self._history_dialog is not None:
            self._history_dialog.refresh()

    def _undo_to_position(self, keep: int) -> None:
        """Undo back until only the first `keep` steps of the history remain."""
        total = len(self._combined_history())
        if keep >= total:
            return
        self._undo_through(keep)
        self._refresh_history_dialog()

    def _redo_to_position(self, index: int) -> None:
        """Redo forward through the future list, up to and including `index`."""
        for _ in range(index + 1):
            if not self._redo_last_edit():
                break
        self._refresh_history_dialog()

    def _undo_through(self, position: int) -> None:
        """Undo everything from `position` in the combined history onwards.

        The two histories are rolled back in their own terms: the edits are popped
        newest-first off the local stack, and the applies are handed to the
        controller as one request for the oldest of them, which already means
        "roll back that apply and everything after it".
        """
        targets = self._combined_history()[position:]
        applies = [entry['index'] for entry in targets if entry['kind'] == 'apply']
        for _ in [entry for entry in targets if entry['kind'] == 'edit']:
            if self._edit_undo:
                self._undo_last_edit()
        if applies:
            # Files are about to move back on disk. That is not something this window
            # will offer to replay from a history list, so the redo chain ends here.
            self._edit_redo.clear()
            self.undo_requested.emit(min(applies))

    # ---------------------------------------------------------------- actions

    def _set_status_selected(self, status: str) -> None:
        entries = self.selected_entries()
        if not entries:
            self.show_message('Select some rows first')
            return
        for entry in entries:
            entry.status = status
            row = self._row_for(entry.entry_id)
            if row is not None:
                self._fill_row(row, entry)
            self.entry_changed(entry)
        self._apply_filters()
        self.refresh_stats()
        # A row vanishing on approve is the status filter doing its job, but it looks
        # exactly like the row being deleted. Say which it was.
        hidden = sum(1 for e in entries
                     if (row := self._row_for(e.entry_id)) is not None
                     and self.table.isRowHidden(row))
        message = (f'{len(entries)} entr{"y" if len(entries) == 1 else "ies"} '
                   f'set to {pretty_status(status)}')
        if hidden:
            message += (f' - {hidden} no longer match the "'
                        f'{self.status_filter.currentText()}" filter and are hidden, '
                        f'not lost')
        self.show_message(message)
        self._advance_selection()

    def _advance_selection(self) -> None:
        """After a decision, move to the next visible row - keeps review flowing."""
        if not self.settings.get_bool('AO_UI_ADVANCE_AFTER_DECISION', True):
            return
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows:
            return
        for row in range(rows[-1] + 1, self.table.rowCount()):
            if not self.table.isRowHidden(row):
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, COL_TITLE))
                return

    def _edit_current_cell(self) -> None:
        """F2 = rename, exactly as double-clicking the cell does.

        With several rows selected it opens the grid instead, because editing twelve
        books one cell at a time through a single-cell editor is not editing them.
        """
        rows = {index.row() for index in self.table.selectedIndexes()}
        if len(rows) > 1:
            self._edit_grid(self.selected_entries())
            return

        item = self.table.currentItem()
        # Landed on a row rather than a cell (or on something not editable): start at
        # Author, which is the first field you would retype anyway.
        if item is None or item.column() not in EDITABLE:
            row = self.table.currentRow()
            if row < 0:
                self.show_message('Select a row first')
                return
            item = self.table.item(row, COL_AUTHOR)
            if item is None:
                return
            self.table.setCurrentItem(item)
        self.table.editItem(item)

    def _step_edit(self, rows: int, columns: int) -> None:
        """Move the edit cursor by (rows, columns) and open the editor there.

        Hidden rows are stepped over: with a filter on, the next book is the next book
        you can see, not the next one in the underlying table.
        """
        row, column = self.table.currentRow(), self.table.currentColumn()
        if row < 0:
            return

        if columns:
            editable = sorted(EDITABLE)
            try:
                position = editable.index(column) + columns
            except ValueError:
                position = 0
            column = editable[max(0, min(len(editable) - 1, position))]

        target = row
        step = 1 if rows >= 0 else -1
        for _ in range(abs(rows) or 0):
            candidate = target + step
            while 0 <= candidate < self.table.rowCount() and self.table.isRowHidden(
                    candidate):
                candidate += step
            if not 0 <= candidate < self.table.rowCount():
                break     # at the end of the list; stay put rather than wrapping
            target = candidate

        item = self.table.item(target, column)
        if item is None:
            return
        self.table.setCurrentItem(item)
        self.table.scrollToItem(item)
        if column in EDITABLE:
            self.table.editItem(item)

    def _edit_grid(self, entries: List[BookEntry]) -> None:
        """Open the selected books in a grid and write back whatever changed."""
        from ..models import Field, clean_value
        from .bulk_edit_dialog import BulkEditDialog

        dialog = BulkEditDialog(entries, parent=self, settings=self.settings)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        changes = dialog.values()
        self._record_edit(
            f'grid edit of {len(changes)} row{"" if len(changes) == 1 else "s"}',
            [(entry.entry_id, name, entry.get_field(name)) for entry in entries
             for name in (changes.get(entry.entry_id) or {})])
        for entry in entries:
            fields = changes.get(entry.entry_id)
            if not fields:
                continue
            for name, value in fields.items():
                cleaned = clean_value(name, value)
                setattr(entry, name,
                        Field(value=cleaned, source='user', confidence=1.0))
                entry.log('user', f'{name} set to "{cleaned}" in the grid editor')
            row = self._row_for(entry.entry_id)
            if row is not None:
                self._fill_row(row, entry)
            self.entry_changed(entry)

        self._apply_filters()
        self.refresh_stats()
        self.show_message(f'Edited {len(changes)} of {len(entries)} rows'
                          if changes else 'No changes were made')

    def _bulk_edit(self, field: str) -> None:
        entries = self.selected_entries()
        if not entries:
            self.show_message('Select some rows first')
            return

        current = entries[0].value(field)
        label = field.replace('_', ' ')
        value, ok = QInputDialog.getText(
            self, f'Set {label}',
            f'Set {label} for {len(entries)} selected '
            f'entr{"y" if len(entries) == 1 else "ies"}:',
            QLineEdit.EchoMode.Normal, str(current))
        if not ok:
            return
        self._write_field(entries, field, value)

    def review_overwrites(self, entries: List[BookEntry]) -> None:
        """Put every proposed overwrite of a manual edit to the user, once per run."""
        from .overwrite_dialog import OverwriteDialog, Proposal, book_label

        proposals = []
        for entry in entries:
            for item in entry.pending_overwrites:
                proposals.append(Proposal(
                    entry_id=entry.entry_id, book=book_label(entry),
                    field=item.get('field', ''), before=item.get('before', ''),
                    after=item.get('after', ''), source=item.get('source', '')))
        if not proposals:
            return

        dialog = OverwriteDialog(proposals, parent=self)
        accepted = dialog.accepted_changes() if dialog.exec() == QDialog.DialogCode.Accepted else {}

        applied = 0
        for entry in entries:
            allowed = accepted.get(entry.entry_id, [])
            for item in list(entry.pending_overwrites):
                if item.get('field') in allowed:
                    entry.force_field(item['field'], item['after'], item['source'])
                    entry.log('user', f'{item["field"]} overwritten with '
                                      f'"{item["after"]}" from {item["source"]}, '
                                      f'with your approval')
                    applied += 1
            entry.pending_overwrites = []
            row = self._row_for(entry.entry_id)
            if row is not None:
                self._fill_row(row, entry)
            self.entry_changed(entry)

        self._apply_filters()
        self.refresh_stats()
        self.show_message(
            f'Kept your edits; applied {applied} of {len(proposals)} proposed changes'
            if applied else f'Kept all {len(proposals)} of your edits')

    def _clear_targets(self) -> Dict[str, List[str]]:
        """entry_id -> the fields a Clear would blank, given the current selection.

        Selecting a column targets that field on every row in it; selecting a row
        targets the whole row; selecting three cells targets three cells. There is no
        "which field?" prompt because the selection already said which. Split out from
        the action itself so the menu entry can *name* what it is about to do.
        """
        targets: Dict[str, List[str]] = {}
        for index in self.table.selectedIndexes():
            name = EDITABLE.get(index.column())
            if name is None:
                continue
            entry = self._entry_at(index.row())
            if entry is not None and entry.get_field(name).value:
                targets.setdefault(entry.entry_id, []).append(name)

        if not targets:
            # A whole-row selection lands on non-editable cells too, so fall back to
            # "everything on these rows" rather than doing nothing.
            for entry in self.selected_entries():
                fields = [n for n in EDITABLE.values() if entry.get_field(n).value]
                if fields:
                    targets[entry.entry_id] = fields
        return targets

    @staticmethod
    def _clear_label(targets: Dict[str, List[str]]) -> str:
        """"Clear 3 authors", or "Clear 7 entries" when the selection is mixed."""
        names = {name for fields in targets.values() for name in fields}
        count = sum(len(fields) for fields in targets.values())
        if not count:
            return 'Clear'
        if len(names) == 1:
            field = next(iter(names))
            label = (FIELD_LABELS[field].lower() if count == 1
                     else FIELD_PLURALS[field])
            return f'Clear {count} {label}'
        return f'Clear {count} entries'

    def _clear_selected_cells(self) -> None:
        """Blank exactly the cells that are selected."""
        from ..models import Field

        targets = self._clear_targets()
        if not targets:
            self.show_message('Nothing to clear in the selection')
            return

        cells = sum(len(names) for names in targets.values())
        self._record_edit(
            f'cleared {cells} cell{"" if cells == 1 else "s"}',
            [(entry_id, name, self.entries[entry_id].get_field(name))
             for entry_id, names in targets.items() if entry_id in self.entries
             for name in names])
        for entry_id, names in targets.items():
            entry = self.entries.get(entry_id)
            if entry is None:
                continue
            for name in names:
                setattr(entry, name, Field())
                entry.log('user', f'{name} cleared')
            row = self._row_for(entry_id)
            if row is not None:
                self._fill_row(row, entry)
            self.entry_changed(entry)

        self._apply_filters()
        self.refresh_stats()
        self.show_message(f'Cleared {cells} cell{"" if cells == 1 else "s"} '
                          f'across {len(targets)} row{"" if len(targets) == 1 else "s"}')

    def _write_field(self, entries: List[BookEntry], field: str, value: str) -> None:
        from ..models import Field, clean_value
        cleaned = clean_value(field, value)
        self._record_edit(f'{field} set on {len(entries)} row'
                          f'{"" if len(entries) == 1 else "s"}',
                          self._snapshot(entries, [field]))
        for entry in entries:
            setattr(entry, field, Field(value=cleaned, source='user', confidence=1.0))
            entry.log('user', f'{field} set manually to "{cleaned}"')
            row = self._row_for(entry.entry_id)
            if row is not None:
                self._fill_row(row, entry)
            self.entry_changed(entry)
        self._apply_filters()
        self.why_panel.show_entry(entries[0], extra_selected=len(entries) - 1)
        label = field.replace('_', ' ')
        self.show_message(f'{"Cleared" if cleaned in ("", 0) else "Set"} {label} '
                          f'on {len(entries)} entries')

    def _request_scan(self) -> None:
        """No selection: rescan everything. With a selection: redo just those rows.

        Re-reading one badly-identified book used to mean rescanning the library.
        """
        entries = self.selected_entries()
        if not entries:
            self.scan_requested.emit()
            return

        if QMessageBox.question(
                self, 'Rescan selected',
                f'Discard what we know about {len(entries)} selected '
                f'entr{"y" if len(entries) == 1 else "ies"} and read them again '
                f'from disk?\n\nApproval and manual edits on those rows are lost.'
        ) != QMessageBox.StandardButton.Yes:
            return

        from ..models import Field
        for entry in entries:
            for name in ('author', 'series', 'series_index', 'title'):
                setattr(entry, name, Field())
            entry.trace, entry.evidence, entry.raw_tags = [], {}, {}
            entry.resolved = False
            entry.status = STATUS_PENDING
            row = self._row_for(entry.entry_id)
            if row is not None:
                self._fill_row(row, entry)
            self.entry_changed(entry)
        self._apply_filters()
        self.refresh_stats()
        self.show_message(f'Reset {len(entries)} entr'
                          f'{"y" if len(entries) == 1 else "ies"} - re-reading them')
        self.resolve_requested.emit(entries, ['metadata', 'regex'])

    def _request_resolve(self) -> None:
        tiers = self.selected_tiers()
        if not tiers:
            self.show_message('Tick at least one source in the Sources menu')
            return
        # No selection means the whole library, the same way Scan and Preview read it.
        # Refusing the click and telling you about Ctrl+A was making you do by hand the
        # only thing an empty selection could have meant.
        entries = self.selected_entries() or list(self.entries.values())
        if not entries:
            self.show_message('Nothing to identify - scan a folder first')
            return
        self.resolve_requested.emit(entries, tiers)

    def _request_apply(self, preview: bool) -> None:
        if preview:
            # Preview writes nothing, so it never refuses. Selected rows if you
            # selected some; the whole library if you did not - "preview everything"
            # is the only thing an empty selection could reasonably mean, and a
            # button that answers a click with a status message and no window is a
            # button that looks broken.
            entries = self.selected_entries() or list(self.entries.values())
            if not entries:
                self.show_message('Nothing to preview - scan a folder first')
                return
            self.apply_requested.emit(entries, True)
            return

        entries = [e for e in self.entries.values() if e.status == STATUS_APPROVED]
        if not entries:
            self.show_message('No approved entries. Approve some rows first (F5).')
            return

        # Applying is the one action that touches the filesystem, so it is the one
        # action worth a confirmation - and even that is optional.
        if self.settings.get_bool('AO_UI_CONFIRM_APPLY', True):
            # This is the only irreversible-looking step, so it gets a real dialog -
            # one that lets you *change* copy-vs-move and the templates rather than
            # only reciting them and sending you off to Settings to fix one.
            from .apply_dialog import ApplyDialog

            dialog = ApplyDialog(len(entries), self.settings, parent=self)
            dialog.settings_requested.connect(self.settings_requested_on_tab.emit)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            # The dialog writes straight to .env, so the controller has to rebuild
            # FileOperations against the mode and templates just chosen.
            self.settings_changed.emit()
        self.apply_requested.emit(entries, False)

    def _search_goodreads(self, entries: Optional[List[BookEntry]] = None) -> None:
        """Open Goodreads for every selected row, using whatever we currently know.

        Goodreads has no usable public API any more, so this is the honest version of
        "look it up there": it hands the query to your browser and gets out of the way.
        Tabs open one at a time, spaced out by a randomised delay - a dozen identical
        requests arriving in the same instant is what rate limiting is looking for.
        """
        if entries is None:
            entries = self.selected_entries()
        if not entries:
            self.show_message('Select a row first')
            return

        if len(entries) > GOODREADS_ASK_ABOVE and QMessageBox.question(
                self, 'Search Goodreads',
                f'Open {len(entries)} Goodreads tabs, one per selected book?\n\n'
                f'They open a second or two apart, so this takes about '
                f'{int(len(entries) * sum(GOODREADS_DELAY) / 2)} seconds.'
        ) != QMessageBox.StandardButton.Yes:
            return

        import random
        elapsed = 0.0
        for entry in entries:
            QTimer.singleShot(int(elapsed * 1000),
                              lambda e=entry: self._open_goodreads(e))
            elapsed += random.uniform(*GOODREADS_DELAY)
        self.show_message(f'Opening Goodreads for {len(entries)} book'
                          f'{"" if len(entries) == 1 else "s"}')

    def _open_goodreads(self, entry: BookEntry) -> None:
        import webbrowser
        from urllib.parse import quote_plus

        query = ' '.join(filter(None, [entry.value('title'), entry.value('author')]))
        if not query:
            query = Path(entry.primary_audio).stem
        webbrowser.open(
            'https://www.goodreads.com/search?utf8=%E2%9C%93'
            f'&q={quote_plus(query)}&search_type=books&search%5Bfield%5D=on')

    # -------------------------------------------------------- window state

    def _restore_window_state(self) -> None:
        """Size, split, and whether the window was maximised.

        Saved geometry is never trusted blindly: monitors get unplugged and
        resolutions change, and a window restored onto a screen that no longer exists
        opens off the edge of the desktop. Anything that does not fit the current
        screen is re-centred on it instead.
        """
        if not self.settings.get_bool('AO_UI_REMEMBER_LAYOUT', True):
            self._centre_on_screen()
            return

        parts = (self.settings.get('AO_UI_WINDOW') or '').split(',')
        try:
            width, height, split_left, split_right = [int(p) for p in parts[:4]]
        except (AttributeError, ValueError):
            self._centre_on_screen()
            return
        maximised = len(parts) > 4 and parts[4].strip() == 'max'

        if width > 400 and height > 300:
            self.resize(width, height)

        # The panel width is remembered as a *width*, not as a share of the window,
        # and it is applied last of all - see _restore_split. Handing sizes to a
        # splitter that is not yet its final width is where this kept going wrong:
        # Qt scales whatever you give it to the width it currently has, so a split
        # saved from a maximised window came back squeezed, and then maximising
        # again shared the new space out by stretch factor instead of restoring it.
        self._wanted_split = split_right if split_right >= 0 else None

        self._centre_on_screen()
        if maximised:
            self.showMaximized()

    def reset_layout(self) -> None:
        """Go back to the built-in window size, split and column widths, now.

        The Settings page has already blanked the saved keys; this is the half that
        makes the window look like it. Un-maximising first is deliberate - snapping
        the columns back inside a maximised window shows you nothing about the size
        that was just reset.
        """
        self._wanted_split = None
        self._has_saved_widths = False
        if self.isMaximized() or self.isFullScreen():
            self.showNormal()
        self.resize(1780, 960)
        self._centre_on_screen()

        for column in range(len(COLUMNS)):
            self.table.setColumnHidden(column, False)
            self.table.setColumnWidth(column, DEFAULT_WIDTHS[column])
        # Covers and density still decide whether column 0 is shown at all.
        self.apply_ui_settings()
        # The splitter has not been told its new width yet - the resize above is a
        # request, not a fact, until the layout runs. Same reason _restore_split is
        # deferred; splitting on a stale width is how you get a 40-pixel panel.
        QTimer.singleShot(0, self._default_split)
        QTimer.singleShot(0, self._fit_columns)
        self.show_message('Layout reset')

    def _default_split(self) -> None:
        """The built-in three-to-one split, against the width the splitter now has."""
        total = self.splitter.width() - self.splitter.handleWidth()
        if total > 0:
            self.splitter.setSizes([int(total * 0.77), int(total * 0.23)])

    def _centre_on_screen(self) -> None:
        """Shrink to fit the available screen, then centre on it."""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()

        size = self.size()
        self.resize(min(size.width(), available.width() - 40),
                    min(size.height(), available.height() - 60))
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    def closeEvent(self, event) -> None:
        if self.settings.get_bool('AO_UI_REMEMBER_LAYOUT', True):
            self._save_column_widths()
            sizes = self.splitter.sizes()
            # Maximised windows report their *restored* size, which is the one to save:
            # reopening maximised then un-maximising should give back a sane window.
            geometry = self.normalGeometry()
            self.settings.set(
                'AO_UI_WINDOW',
                f'{geometry.width()},{geometry.height()},'
                f'{sizes[0]},{sizes[1] if len(sizes) > 1 else 0},'
                f'{"max" if self.isMaximized() or self.isFullScreen() else "normal"}')
            try:
                self.settings.save()
            except OSError as exc:
                logger.warning('Could not save the window layout: %s', exc)
        super().closeEvent(event)

    # ---------------------------------------------------------------- helpers

    def selected_entries(self) -> List[BookEntry]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        entries = [self._entry_at(row) for row in rows]
        return [entry for entry in entries if entry is not None]

    def _entry_at(self, row: int) -> Optional[BookEntry]:
        item = self.table.item(row, COL_FILES)
        if item is None:
            return None
        return self.entries.get(item.data(Qt.ItemDataRole.UserRole))

    def _row_for(self, entry_id: str) -> Optional[int]:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_FILES)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == entry_id:
                return row
        return None

    def refresh_mode_label(self) -> None:
        """Kept as a no-op so callers need not care that the strip is gone.

        Copy-vs-move and the output folder belong to the apply step, and the apply
        confirmation states both. Parking them permanently in the toolbar made the
        status area read as a run-on sentence.
        """

    # ------------------------------------------------------------ progress UI

    def show_message(self, text: str) -> None:
        self._status_text = text
        self._render_status()
        logger.info(text)

    def _tick_spinner(self) -> None:
        self._spin = (self._spin + 1) % len(SPINNER)
        self._render_status()
        # Everything that says "still working" moves off this one timer. A chapter
        # encode holds the same step for minutes at a time, so motion is the only
        # thing distinguishing a slow job from a dead one, and it has to be
        # somewhere the eye already is: the bar, the row, and the sentence.
        self.delegate.phase = (self.delegate.phase + 0.055) % 1.0
        self._animate_progress_bar()
        if self._row_progress:
            self.table.viewport().update()

    def _animate_progress_bar(self) -> None:
        """Sweep a highlight along the toolbar bar while a job is running.

        A determinate QProgressBar is a still image between updates, which for a
        chapter encode means a still image for a minute. Qt has no animated chunk,
        so the chunk is a gradient and the gradient is re-stated on every tick.
        """
        if not self._busy or not self.progress.isVisible():
            return
        head = (self._spin / len(SPINNER))
        self.progress.setStyleSheet(
            'QProgressBar { background: %s; border: none; border-radius: 3px; }'
            'QProgressBar::chunk { border-radius: 3px; background: '
            'qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 %s, stop:%.3f %s, '
            'stop:%.3f %s, stop:%.3f %s, stop:1 %s); }'
            % (BG_RAISED, ACCENT_DARK, max(0.0, head - 0.2), ACCENT_DARK,
               head, ACCENT, min(1.0, head + 0.2), ACCENT_DARK, ACCENT_DARK))

    def _render_status(self) -> None:
        """The status sentence, with the queue count appended as a clickable link."""
        import html as _html

        text = _html.escape(getattr(self, '_status_text', '') or '')
        if self._busy:
            text = (f'<span style="color:{ACCENT};">{SPINNER[self._spin]}</span>  '
                    + text)
        waiting = len(self._queue_labels)
        if waiting:
            text += (f'   ·   <a href="queue" style="color:{ACCENT};">'
                     f'{plural(waiting, "job")} queued</a>')
        elif self._busy:
            text += (f'   ·   <a href="queue" style="color:{ACCENT};">'
                     f'open the queue</a>')
        self.status_label.setText(text)
        # The full sentence lives in the tooltip, because the label is width-capped -
        # an uncapped one grows with the message and shoves Cancel off the toolbar,
        # which is how the only stop button vanished mid-run.
        self.status_label.setToolTip(getattr(self, '_status_text', '')
                                     or 'What just happened')

    def show_progress(self, done: float, total: float, message: str = '') -> None:
        """One consistent readout: "<what is happening>  12/38".

        The bar carries the numbers, the label carries the sentence. Nothing else goes
        in here - a status strip that concatenates unrelated fragments is unreadable.

        `done` may be fractional. A chapter merge spends minutes inside a single
        chapter, and a bar that only moves when one finishes sits still while the
        message beside it counts steadily up through the audio. So the bar runs on a
        fixed 1000-step range and is filled from the exact fraction, while the text
        still counts in whole chapters - which is the unit you can check against the
        folder in front of you.
        """
        self._last_progress = (done, total, message)
        self._refresh_queue_dialog()
        self._progress_action.setVisible(True)
        if total > 0:
            fraction = max(0.0, min(1.0, done / total))
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(round(fraction * 1000)))
            # The literal numbers are baked into the format string rather than left to
            # %v/%m, which would print the 1000-step scale instead of the chapters.
            self.progress.setFormat(
                f'{int(done)} / {int(total)}  ({fraction:.0%})')
        else:
            self.progress.setRange(0, 0)  # indeterminate - no count is known yet
            self.progress.setFormat('')
        if message:
            self._status_text = message
            self._render_status()

    def show_queue(self, labels: List[str]) -> None:
        """Called by the controller whenever the pending-job list changes."""
        self._queue_labels = list(labels)
        self._queue_action.setVisible(bool(labels))
        self.queue_button.setText(f'  {plural(len(labels), "job")} queued  ')
        self._render_status()
        self._refresh_identify_badge()
        self._refresh_queue_dialog()

    def _refresh_identify_badge(self) -> None:
        """Put the number of outstanding identifications on the Identify button."""
        action = self.tool_actions.get('identify')
        if action is None:
            return
        count = int((self.queue_provider() or {}).get('identifying') or 0)
        size = self._icon_size()
        action.setIcon(badged_icon('identify', TEXT, size, count))
        base = ITEMS_BY_KEY['identify']
        # Disabled, the tooltip's job is to say why - the queue count is beside the
        # point when the button cannot be pressed. See refresh_action_states.
        body = (base.tooltip if action.isEnabled()
                else 'Nothing has been scanned yet')
        action.setToolTip(
            f'{base.label}\n{body}'
            + (f'\n\n{plural(count, "identification")} outstanding. '
               f'Right-click to open the queue.' if count else ''))

    def show_queue_window(self) -> None:
        """Open (or raise) the queue window."""
        from .queue_dialog import QueueDialog

        if self._queue_dialog is None:
            dialog = QueueDialog(parent=self)
            dialog.remove_requested.connect(self.queue_remove_requested.emit)
            dialog.clear_requested.connect(self.queue_clear_requested.emit)
            # "Cancel the running job" means exactly that. Routing it through the
            # blanket cancel binned everything queued behind it as well.
            dialog.cancel_requested.connect(self.cancel_current_requested.emit)
            dialog.finished.connect(lambda _: setattr(self, '_queue_dialog', None))
            self._queue_dialog = dialog
        self._refresh_queue_dialog()
        self._queue_dialog.show()
        self._queue_dialog.raise_()
        self._queue_dialog.activateWindow()

    def _refresh_queue_dialog(self) -> None:
        if self._queue_dialog is None:
            return
        done, total, message = self._last_progress
        self._queue_dialog.update_status(self.queue_provider() or {},
                                         done, total, message)

    def hide_progress(self) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._progress_action.setVisible(False)

    def set_busy(self, busy: bool) -> None:
        """Show that a job is running - without taking the window away from you.

        The table stays live: reviewing, editing and approving rows are local
        operations that have nothing to do with the background job, and blocking them
        for the length of a network run made the app feel broken. Only the actions
        that would start a *second* long job are held back, and those queue rather
        than being refused.
        """
        self._cancel_action.setVisible(busy)
        self._busy = busy
        if busy and not self._spinner.isActive():
            self._spinner.start()
        elif not busy and self._spinner.isActive():
            self._spinner.stop()
        # Nothing is disabled: a second job queues behind the first. Greying the
        # buttons out was what made the queue impossible to reach.
        if not busy:
            self._last_progress = (0, 0, '')
            self.hide_progress()
            # Hand the bar back to the theme, or it keeps the last frame of the
            # sweep the next time something makes it visible.
            self.progress.setStyleSheet('')
        self._render_status()
        # Every job that changes what Undo can reach - an apply, an undo, a merge -
        # ends by calling this, so it is where the toolbar catches up with the journal.
        self.refresh_action_states()
        self._refresh_identify_badge()
        self._refresh_queue_dialog()

    def show_report(self, title: str, body: str) -> None:
        """Scrollable plain-text report, for anything without a richer view."""
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(900, 620)
        layout = QVBoxLayout(dialog)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setPlainText(body)
        view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(view)
        close = QPushButton('Close')
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()
