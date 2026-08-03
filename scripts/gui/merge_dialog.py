"""Setting up a "merge these chapters into one .m4b" run.

This used to be a Save-file dialog, pre-filled with ``entry.value('title')``. That was
wrong in two ways at once. A native save dialog can only ask about one file, so merging
five books meant five of them; and the name it offered came from whatever the *first
chapter's* title tag happened to say - which for a book split into numbered mp3s is the
name of chapter one, not the name of the book. Hence output called
``Chapter 1.1 - 16 March 2051.m4b``.

So the name is not asked for at all by default: it is rendered from the same output
template the rest of the program files books under, from the metadata we already have.
The dialog exists to show you what every selected book is about to be called - before
anything is encoded - and to hold the options that actually vary: where it lands, what
happens to the originals, and the bitrate.

Books we know nothing about cannot be named this way, and the dialog says so plainly
rather than inventing something: identify them first, or type a name.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, NamedTuple, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..models import BookEntry
from ..paths import render_template, sanitize_component, unique_path
from .theme import ACCENT, STATUS_TEXT, TEXT_DIM, table_modal_width

COLUMNS = ['BOOK', 'CHAPTERS', 'WILL BE CALLED', 'WHERE']

# Starting widths, taken from the review table's so the same value is the same width
# in both places: BOOK matches FILES (420), WILL BE CALLED matches TITLE plus the
# room an extension needs, and WHERE - which holds a whole directory path - gets the
# most. Every one of them is draggable afterwards.
DEFAULT_WIDTHS = [420, 90, 360, 560]

# CHAPTERS is a count, so it is centred over its centred numbers. Everything else
# reads from the left. Stated per column rather than left to the header default -
# see the same decision in the review table.
HEADER_ALIGNMENT = [
    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
    Qt.AlignmentFlag.AlignCenter,
    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
]

# Offered in the pattern box. The first entry is "whatever the Output tab says" - and
# it prints the template that actually is, rather than a sentence telling you to go and
# look it up somewhere else.
PATTERNS = [
    '',
    '{series} {series_index:02d} - {title}',
    '{author} - {title}',
    '{title}',
    '{author} - {series} {series_index:02d} - {title}',
]

# What the bitrate box offers. "same" is first and is the default: re-encoding a 128k
# source down to a fixed 64k throws away half the file for no reason anyone asked for.
BITRATES = [
    ('same', 'Same as the source files  (no quality lost, and much faster)'),
    ('320k', '320 kbps  -  highest'),
    ('256k', '256 kbps'),
    ('192k', '192 kbps'),
    ('128k', '128 kbps'),
    ('96k', '96 kbps'),
    ('64k', '64 kbps  -  the usual choice for spoken word'),
    ('32k', '32 kbps  -  smallest, audibly worse'),
]


class MergePlan(NamedTuple):
    """One book, and exactly where its merged .m4b is going."""

    entry: BookEntry
    destination: Path
    delete_originals: bool
    bitrate: str
    replace_entry: bool


class MergeDialog(QDialog):
    """Confirm what a batch of chapter merges will produce, and how."""

    def __init__(self, entries: List[BookEntry], settings, output_root: Path,
                 parent=None):
        super().__init__(parent)
        self.entries = list(entries)
        self.settings = settings
        self.output_root = Path(output_root)

        self.setWindowTitle(f'Merge {len(self.entries)} '
                            f'book{"" if len(self.entries) == 1 else "s"} into .m4b')
        # As wide as the review table, not as wide as the window - see
        # theme.table_modal_width. The columns below use the review table's widths, so
        # this is what makes the two line up.
        self.resize(table_modal_width(parent, sum(DEFAULT_WIDTHS) + 60),
                    min(320 + 30 * len(self.entries), 820))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        self.headline = QLabel('')
        self.headline.setWordWrap(True)
        layout.addWidget(self.headline)

        layout.addWidget(self._build_options())

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignVCenter)
        # Sized exactly like the review table: every column Interactive, generous
        # starting widths, and a horizontal scrollbar when the dialog is narrow.
        # A Stretch column in the middle was the whole problem - it ate the space
        # the other three gave back, and it pinned WHERE against the right edge so
        # the only handle that could have resized it was off the end of the viewport.
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(60)
        for column in range(len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        self.table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        for column, alignment in enumerate(HEADER_ALIGNMENT):
            item = QTableWidgetItem(COLUMNS[column])
            item.setTextAlignment(alignment)
            self.table.setHorizontalHeaderItem(column, item)
        layout.addWidget(self.table, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.identify_button = QPushButton('Identify these books first')
        self.identify_button.setToolTip(
            'Close this and run identification over the selection, so the names can '
            'come from real metadata. Reopen the merge afterwards.')
        buttons.addButton(self.identify_button,
                          QDialogButtonBox.ButtonRole.ActionRole)
        self.identify_button.clicked.connect(self._identify_instead)

        self.merge_button = QPushButton('')
        self.merge_button.setProperty('accent', True)
        self.merge_button.clicked.connect(self.accept)
        buttons.addButton(self.merge_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.wants_identify = False
        self._widths_applied = False
        self._load()
        self._refresh()

    def showEvent(self, event) -> None:
        """Set the column widths only once the dialog is actually on screen.

        Setting them in the constructor does not hold, and this is the whole reason
        the preview opened with four cropped 100px columns and a WHERE column showing
        nothing but a drive letter. Applying a stylesheet re-polishes QHeaderView, and
        Qt resets every section to the default width when that happens - so anything
        set while building is thrown away before the dialog is ever shown. The review
        table hit exactly this and solves it the same way; see
        MainWindow._apply_column_widths.
        """
        super().showEvent(event)
        if self._widths_applied:
            return
        self._widths_applied = True
        metrics = self.table.horizontalHeader().fontMetrics()
        for column, heading in enumerate(COLUMNS):
            # Never narrower than the heading itself, or "WILL BE CALLED" renders as
            # "WILL BE C..." over a column with plenty of room in it.
            self.table.setColumnWidth(
                column, max(DEFAULT_WIDTHS[column],
                            metrics.horizontalAdvance(heading) + 34))

    # ----------------------------------------------------------------- options

    def _build_options(self) -> QWidget:
        box = QGroupBox('Options')
        form = QFormLayout(box)
        form.setSpacing(8)

        # The fallback entry prints the template it would fall back to. A dialog that
        # says "use the file template from Output settings" has told you where to go
        # and look rather than telling you the thing you asked about.
        inherited = self.settings.get(
            'AO_FILE_TEMPLATE', '{series} {series_index:02d} - {title}')
        self.pattern = QComboBox()
        self.pattern.setEditable(True)
        self.pattern.setToolTip(
            'How the merged file is named. The first entry is the file template from '
            'the Output tab, so merged books are named exactly like every other book '
            'in your library. Placeholders: {author} {series} {series_index} {title}.')
        for text in PATTERNS:
            self.pattern.addItem(
                text or f'{inherited}      (from Output settings)', text)
        self.pattern.currentTextChanged.connect(lambda _: self._refresh())
        form.addRow('Name pattern', self.pattern)

        self.in_place = QCheckBox('Write it next to the chapter files')
        self.in_place.setToolTip(
            'On: the .m4b appears in the book\'s own folder, and the entry stays where '
            'it is until you Save like any other book.\n'
            'Off: it is written straight into the output folder instead.')
        self.in_place.toggled.connect(lambda _: self._refresh())
        form.addRow('Destination', self.in_place)

        self.delete_originals = QCheckBox(
            'Delete the original chapter files once the merge succeeds')
        self.delete_originals.setToolTip(
            'Only after ffmpeg reports success and the output file is non-empty. '
            'There is no undo for this - the files are gone.')
        self.delete_originals.toggled.connect(lambda _: self._refresh())
        form.addRow('Originals', self.delete_originals)

        self.replace_entry = QCheckBox(
            'Point this library entry at the merged file afterwards')
        self.replace_entry.setToolTip(
            'The book becomes a single-file entry, so the table stops showing it as '
            'twelve chapters and Save writes the one .m4b.')
        form.addRow('Afterwards', self.replace_entry)

        self.bitrate = QComboBox()
        self.bitrate.setToolTip(
            '"Same as the source files" never costs you quality, and is much the '
            'fastest: chapters that are already AAC (.m4a/.m4b) are copied straight '
            'in without being encoded at all, and anything else is encoded at the '
            'bitrate it already had. Picking a fixed rate forces a re-encode of the '
            'whole book, whatever the source was.')
        for value, label in BITRATES:
            self.bitrate.addItem(label, value)
        form.addRow('Bitrate', self.bitrate)

        self.overwrite = QCheckBox('Overwrite an existing file of the same name')
        self.overwrite.setToolTip(
            'Off, a name that is already taken gets " (2)" appended, exactly as '
            'applying does.')
        self.overwrite.toggled.connect(lambda _: self._refresh())
        form.addRow('If it exists', self.overwrite)
        return box

    def _load(self) -> None:
        stored = self.settings.get('AO_MERGE_TEMPLATE', '')
        index = self.pattern.findData(stored)
        if index >= 0:
            self.pattern.setCurrentIndex(index)
        elif stored:
            self.pattern.setCurrentText(stored)
        self.in_place.setChecked(self.settings.get_bool('AO_MERGE_IN_PLACE', True))
        self.delete_originals.setChecked(
            self.settings.get_bool('AO_MERGE_DELETE_ORIGINALS', False))
        self.replace_entry.setChecked(
            self.settings.get_bool('AO_MERGE_REPLACE_ENTRY', True))
        self.overwrite.setChecked(self.settings.get_bool('AO_MERGE_OVERWRITE', False))
        index = self.bitrate.findData(
            (self.settings.get('AO_MERGE_BITRATE', 'same') or 'same').strip().lower())
        self.bitrate.setCurrentIndex(max(0, index))

    def _save(self) -> None:
        """Everything in this dialog is a real setting, written to .env like any other.

        Nothing here is per-run: how you want merged books named, where they land and
        at what bitrate is a decision you make once. It is also all on the Merging tab
        in Settings - a setting that exists only inside one modal is a setting you can
        only find by opening that modal.
        """
        self.settings.set('AO_MERGE_TEMPLATE', self._pattern())
        self.settings.set('AO_MERGE_IN_PLACE', self.in_place.isChecked())
        self.settings.set('AO_MERGE_DELETE_ORIGINALS',
                          self.delete_originals.isChecked())
        self.settings.set('AO_MERGE_REPLACE_ENTRY', self.replace_entry.isChecked())
        self.settings.set('AO_MERGE_OVERWRITE', self.overwrite.isChecked())
        self.settings.set('AO_MERGE_BITRATE', self._bitrate())
        try:
            self.settings.save()
        except OSError:
            pass

    def _bitrate(self) -> str:
        return str(self.bitrate.currentData() or 'same')

    def _pattern(self) -> str:
        """The template to render, or '' meaning "use the Output settings one"."""
        data = self.pattern.currentData()
        text = self.pattern.currentText()
        # An untouched combo returns the placeholder label as its text; the data is
        # what actually means "empty".
        if data is not None and text == self.pattern.itemText(
                self.pattern.currentIndex()):
            return str(data)
        return text.strip()

    # ------------------------------------------------------------------ naming

    def _name_for(self, entry: BookEntry) -> str:
        """The merged filename for one book, or '' when we do not know enough.

        Returning '' rather than a guess is the point: a book with no title cannot be
        named from a template, and calling it after its first chapter is how this went
        wrong before.
        """
        template = self._pattern() or self.settings.get(
            'AO_FILE_TEMPLATE', '{series} {series_index:02d} - {title}')
        # {file_index} is meaningless here - the whole book is one file now.
        template = template.replace('{file_index:03d}', '').replace('{file_index}', '')

        values = {'author': entry.value('author'), 'series': entry.value('series'),
                  'series_index': entry.value('series_index'),
                  'title': entry.value('title')}
        if not values['title'] and not values['author']:
            return ''
        rendered = render_template(template, values)
        # render_template builds paths; a filename is one component of one.
        rendered = rendered.replace('/', ' - ').strip()
        rendered = sanitize_component(rendered, fallback='')
        return f'{rendered}.m4b' if rendered else ''

    def _destination_for(self, entry: BookEntry, name: str) -> Optional[Path]:
        if not name:
            return None
        if self.in_place.isChecked():
            folder = Path(entry.folder)
        else:
            folder = self.output_root
        path = folder / name
        if not self.overwrite.isChecked():
            path = unique_path(path)
        return path

    # ---------------------------------------------------------------- refresh

    def _refresh(self) -> None:
        self.table.setRowCount(0)
        unnamed = 0

        for entry in self.entries:
            name = self._name_for(entry)
            destination = self._destination_for(entry, name)
            if not name:
                unnamed += 1

            row = self.table.rowCount()
            self.table.insertRow(row)
            cells = [
                entry.value('title') or Path(entry.primary_audio).name,
                str(len(entry.audio_files)),
                name or 'nothing to name it after',
                str(destination.parent) if destination else '-',
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(HEADER_ALIGNMENT[column])
                if column == 2:
                    item.setForeground(_brush(ACCENT if name
                                              else STATUS_TEXT['rejected']))
                elif column == 3:
                    item.setForeground(_brush(TEXT_DIM))
                self.table.setItem(row, column, item)

        ready = len(self.entries) - unnamed
        if unnamed:
            self.headline.setText(
                f'<b>{unnamed}</b> of {len(self.entries)} selected book'
                f'{"" if len(self.entries) == 1 else "s"} '
                f'{"has" if unnamed == 1 else "have"} no title or author, so there is '
                f'nothing to build a filename from. Identify them first, or type a '
                f'name pattern that works without those fields. The other '
                f'<b>{ready}</b> can be merged now.')
        else:
            self.headline.setText(
                f'<b>{ready}</b> book{"" if ready == 1 else "s"} will be merged, named '
                f'from the metadata already on {"it" if ready == 1 else "them"}. '
                f'Nothing is written until you press the button below.')

        self.merge_button.setEnabled(ready > 0)
        self.merge_button.setText(
            f'Merge {ready} book{"" if ready == 1 else "s"}' if ready
            else 'Nothing can be merged')
        self.identify_button.setVisible(bool(unnamed))

    def _identify_instead(self) -> None:
        self.wants_identify = True
        self.reject()

    # ------------------------------------------------------------------ result

    def plans(self) -> List[MergePlan]:
        """One plan per book that can actually be named. Unnamed books are skipped."""
        bitrate = self._bitrate()
        out = []
        for entry in self.entries:
            destination = self._destination_for(entry, self._name_for(entry))
            if destination is None:
                continue
            out.append(MergePlan(
                entry=entry, destination=destination,
                delete_originals=self.delete_originals.isChecked(),
                bitrate=bitrate, replace_entry=self.replace_entry.isChecked()))
        return out

    def done(self, result: int) -> None:
        # The options are remembered whichever way the dialog closes - what you set up
        # is what you meant, even if you cancelled to go and identify something first.
        self._save()
        super().done(result)


def _brush(colour: str):
    from PyQt6.QtGui import QBrush, QColor
    return QBrush(QColor(colour))
