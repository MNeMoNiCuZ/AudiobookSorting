"""Loading the input folder: bring the list in step with the disk, or start over.

Two different actions used to share one "Load" button, and which one you got depended
on four checkboxes remembered from the last time - so pressing Load could quietly reset
every author in the list. They are separate now:

- **Update the list** (the default, every time): books that appeared are added, books
  whose files are gone are taken out, and each listed book's file list is refreshed.
  Nothing anyone worked out or typed on a listed book is touched.
- **Reload and reset values**: read the books again from scratch, keeping only what is
  ticked. Chosen on purpose, named for what it does, and confirmed before it runs.

What differs between the list and the folder is checked live when the dialog opens -
not taken from a comparison made at start-up - and every book and file it is about is
listed by name.

The counts are produced by the same code that does the clearing (scripts/load_options),
so what the dialog promises and what the load performs cannot drift apart.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable, List, Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QRadioButton, QSpinBox, QVBoxLayout,
)

from ..file_scanner import FileScanner
from ..load_options import KeepOptions, LoadPlan, entries_without_files, plan_load
from ..models import IDENTITY_FIELDS, BookEntry
from ..paths import PROJECT_ROOT
from .item_list import books_lines, confirm_list, list_dialog
from .theme import ACCENT, STATUS_TEXT, TEXT_DIM, TEXT_FAINT

# The table's row names. Same words as the main table's columns, spelled out - a
# column header of "#" is readable above a column of numbers, on its own it is not.
FIELD_LABELS = {'author': 'Author', 'series': 'Series',
                'series_index': 'Series #', 'title': 'Title'}

# Wide enough for a full path to a chapter file inside a series folder.
DIALOG_WIDTH = 1320


def _under(folder: str, root: Path) -> bool:
    try:
        path = Path(folder).resolve()
        root = root.resolve()
    except OSError:
        return False
    return path == root or root in path.parents


def _plural(count: int, word: str) -> str:
    return f'{count} {word}{"" if count == 1 else "s"}'


def _book_name(entry: BookEntry) -> str:
    """Author - Title, else the folder's name: what a person calls the book."""
    name = ' - '.join(part for part in (entry.value('author'), entry.value('title'))
                      if part)
    return name or Path(entry.folder).name or entry.entry_id


class _BookCounter(QObject):
    """Counts the books a load of a folder would produce, off the GUI thread.

    It is the scanner's own walk, so the number is the one the load will arrive at.
    Each count carries a generation, so a slow count of a folder you have since typed
    over is thrown away instead of overwriting the right one.
    """

    counted = pyqtSignal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.generation = 0
        self._stop = threading.Event()

    def start(self, folder: Path) -> None:
        self.stop()
        self._stop = threading.Event()
        self.generation += 1
        generation, stop = self.generation, self._stop

        def run():
            try:
                entries = FileScanner(str(folder)).scan_directory(
                    should_stop=stop.is_set)
                found = {entry.entry_id: entry for entry in entries}
            except Exception:
                found = None
            if not stop.is_set():
                try:
                    self.counted.emit(generation, found)
                except RuntimeError:
                    pass    # the dialog closed before the count finished

        threading.Thread(target=run, daemon=True).start()

    def stop(self) -> None:
        """Abandon the count in flight; its result, if it arrives, is ignored."""
        self._stop.set()
        self.generation += 1


class _DriftFinder(QObject):
    """Compares the list with the folder as it is now, off the GUI thread.

    The same comparison that lights the "!" on Inputs, run again when the dialog
    opens: the one made at start-up can be hours old, and a warning about a file you
    have since renamed is worse than no warning.
    """

    found = pyqtSignal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.generation = 0

    def start(self, folder: Optional[Path], entries: List[BookEntry]) -> None:
        self.generation += 1
        generation = self.generation

        def run():
            try:
                if folder is not None:
                    drift = FileScanner(str(folder)).compare_to_entries(entries)
                else:
                    # No folder to compare with, but a book with no files is still
                    # worth naming, and still removable.
                    empty = [entry.entry_id for entry in entries_without_files(entries)]
                    drift = {'empty': len(empty), 'files': {'empty': empty}}
            except Exception:
                drift = None
            try:
                self.found.emit(generation, drift)
            except RuntimeError:
                pass    # the dialog closed before the comparison finished

        threading.Thread(target=run, daemon=True).start()


class LoadInputDialog(QDialog):
    """Choose the folder, and whether a load updates the list or resets it."""

    def __init__(self, all_entries: List[BookEntry], selected: List[BookEntry],
                 settings, parent=None, drift: Optional[dict] = None,
                 folder_changed_from: Optional[str] = None,
                 on_remove: Optional[Callable[[List[str]], None]] = None):
        super().__init__(parent)
        self.settings = settings
        self.all_entries = list(all_entries)
        self.selected = list(selected)
        # What differs between the list and the folder. Starts empty and is filled by
        # a comparison made now; ``drift`` from the caller is not shown, being stale.
        self.drift: dict = {}
        # The comparison made while the dialog was open, for the window's "!" badge.
        self.fresh_drift: Optional[dict] = None
        self.folder_changed_from = folder_changed_from
        # Books with none of their files left on disk; None while they are looked for.
        self._empty_ids: Optional[set] = None
        # Removes books from the list there and then - never tied to a load.
        self.on_remove = on_remove
        # How many were removed while the dialog was open, to say so.
        self._removed = 0

        self.setWindowTitle('Load input folder')
        self.setMinimumWidth(900)
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry().width() if screen is not None else 0
        self._width = min(DIALOG_WIDTH, int(available * 0.9)) if available             else DIALOG_WIDTH
        # The height the contents asked for last; the dialog follows it as the notice
        # and the counts come and go, so it never carries an empty band.
        self._fitted_height = 0
        # Set when the dialog closes on "Clear list" rather than on Load.
        self.clear_chosen = False
        # The entry ids a load of the chosen folder would produce; None while counting.
        self._folder_ids: Optional[set] = None
        # The same books, by id, so the new ones can be named.
        self._folder_entries: dict = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        headline = QLabel('<b>Load the input folder</b>')
        headline.setStyleSheet('font-size: 15px;')
        layout.addWidget(headline)

        # The folder, editable where it is used. Written the way it is configured, so
        # a relative "input" stays relative.
        row = QHBoxLayout()
        row.setSpacing(6)
        self.folder = QLineEdit(str(settings.get('AO_INPUT_DIR')).strip())
        self.folder.setPlaceholderText('The folder your audiobooks are in')
        self.folder.setStyleSheet(
            f'color: {ACCENT}; font-family: Consolas, monospace; font-size: 12px;')
        row.addWidget(self.folder, 1)
        browse = QPushButton('Browse...')
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        layout.addLayout(row)

        layout.addWidget(self._build_notice())

        layout.addWidget(self._build_scope())
        layout.addWidget(self._build_mode())

        self.list_only = QCheckBox('Only list new books - skip their initial scan')
        self.list_only.setToolTip(
            'New books go into the list as they are on disk. Their tags and file names '
            'are not read until you press Initial Scan.')
        self.list_only.setChecked(self.settings.get_bool('AO_LOAD_LIST_ONLY', False))
        layout.addWidget(self.list_only)

        buttons = QHBoxLayout()
        clear = QPushButton('Clear list...')
        clear.setToolTip('Remove every book from the list. Nothing on disk is touched.')
        clear.setEnabled(bool(self.all_entries))
        clear.clicked.connect(self._clear)
        buttons.addWidget(clear)
        buttons.addStretch(1)

        cancel = QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)

        self.go = QPushButton('')
        self.go.setProperty('accent', True)
        self.go.setDefault(True)
        self.go.clicked.connect(self.accept)
        buttons.addWidget(self.go)
        layout.addLayout(buttons)

        self._counter = _BookCounter(self)
        self._counter.counted.connect(self._counted)
        self._drift_finder = _DriftFinder(self)
        self._drift_finder.found.connect(self._drift_found)
        self._recount_timer = QTimer(self)
        self._recount_timer.setSingleShot(True)
        self._recount_timer.setInterval(400)
        self._recount_timer.timeout.connect(self._recount)
        self.folder.textChanged.connect(lambda _: self._folder_edited())

        self._refresh()
        self._recount()

    # ------------------------------------------------------------------- build

    def _build_notice(self) -> QFrame:
        """What is different on disk, by name, with the actions that deal with it."""
        self.notice = QFrame()
        self.notice.setStyleSheet(
            f'QFrame {{ border: 1px solid {STATUS_TEXT["risky"]}; border-radius: 6px; }}'
            f'QLabel {{ border: none; }}')
        column = QVBoxLayout(self.notice)
        column.setContentsMargins(10, 8, 10, 8)
        column.setSpacing(8)
        self.notice_text = QLabel('')
        self.notice_text.setWordWrap(True)
        self.notice_text.setTextFormat(Qt.TextFormat.RichText)
        # Exactly which books and files the notice is about, a click away - inline,
        # a list of hundreds of paths pushed the rest of the dialog off the screen.
        self.notice_text.linkActivated.connect(lambda _: self._show_notice_list())
        column.addWidget(self.notice_text)
        self.purge = QPushButton('')
        self.purge.setToolTip('Removes them from the list now, without loading. '
                              'Nothing on disk is touched, and the list is backed up '
                              'first.')
        self.purge.clicked.connect(self._remove_empty)
        column.addWidget(self.purge, 0, Qt.AlignmentFlag.AlignLeft)
        self.notice.setVisible(False)
        return self.notice

    def _build_scope(self) -> QGroupBox:
        """Which books. Only asked when a selection makes it a real question."""
        box = QGroupBox('Which books')
        column = QVBoxLayout(box)
        column.setSpacing(4)

        count = len(self.all_entries)
        self.scope_all = QRadioButton(
            f'The whole input folder  -  {_plural(count, "book")} in the list, plus '
            f'anything new')
        self.scope_all.setToolTip('Walks the input folder: books that appeared are '
                                  'added, books that are gone are removed.')
        self.scope_selected = QRadioButton(
            f'Only the {_plural(len(self.selected), "selected book")}')
        self.scope_selected.setToolTip('Re-reads just those books from disk. The rest '
                                       'of the list is left exactly as it is.')

        # A selection is a statement about which books you mean, so the dialog takes
        # it at its word - reloading the whole folder is then the deliberate choice.
        self.scope_selected.setChecked(bool(self.selected))
        self.scope_all.setChecked(not self.selected)
        for button in (self.scope_all, self.scope_selected):
            button.toggled.connect(lambda _: self._refresh())
            column.addWidget(button)

        box.setVisible(bool(self.selected))
        return box

    def _build_mode(self) -> QGroupBox:
        """Update (keeps everything) or reset (keeps what is ticked). Update by default."""
        box = QGroupBox('What happens to the books already in the list')
        column = QVBoxLayout(box)
        column.setSpacing(6)

        self.mode_update = QRadioButton('Update the list  (recommended)')
        self.mode_update.setStyleSheet('font-weight: 600;')
        column.addWidget(self.mode_update)
        update_note = QLabel(
            'Adds the books that appeared in the folder, takes out the ones whose files '
            'are gone, and refreshes each book\'s file list. Every value, edit and '
            'Approved / Rejected decision on the books already listed is kept.')
        update_note.setWordWrap(True)
        update_note.setStyleSheet(f'color: {TEXT_DIM}; margin-left: 24px;')
        column.addWidget(update_note)

        self.counts = QLabel('')
        self.counts.setWordWrap(True)
        self.counts.setTextFormat(Qt.TextFormat.RichText)
        self.counts.setStyleSheet(f'color: {TEXT_DIM}; margin-left: 24px;')
        self.counts.linkActivated.connect(lambda _: self._show_diff())
        column.addWidget(self.counts)

        column.addSpacing(8)
        self.mode_reset = QRadioButton('Reload and reset values')
        self.mode_reset.setStyleSheet(f'font-weight: 600; color: '
                                      f'{STATUS_TEXT["rejected"]};')
        column.addWidget(self.mode_reset)
        reset_note = QLabel(
            'Also reads every book again from scratch. Whatever is not ticked below is '
            'thrown away and has to be identified again. You are asked before anything '
            'you cannot get back is lost, and the list is backed up first.')
        reset_note.setWordWrap(True)
        reset_note.setStyleSheet(f'color: {TEXT_DIM}; margin-left: 24px;')
        column.addWidget(reset_note)

        self.reset_options = QFrame()
        # By name: a bare QFrame rule reaches every QLabel inside, each a QFrame.
        self.reset_options.setObjectName('resetOptions')
        self.reset_options.setStyleSheet('#resetOptions { margin-left: 24px; }')
        reset_column = QHBoxLayout(self.reset_options)
        reset_column.setContentsMargins(0, 0, 0, 0)
        reset_column.setSpacing(24)
        reset_column.addWidget(self._build_keep())

        self.summary = QLabel('')
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(
            f'color: {TEXT_DIM}; border-left: 2px solid {ACCENT}; padding: 6px 10px;')
        reset_column.addWidget(self.summary, 1)
        column.addWidget(self.reset_options)

        # Never remembered: every load starts as the one that loses nothing.
        self.mode_update.setChecked(True)
        for button in (self.mode_update, self.mode_reset):
            button.toggled.connect(lambda _: self._refresh())

        # With an empty list there is nothing to keep, so nothing to choose.
        box.setVisible(bool(self.all_entries))
        return box

    def _build_keep(self) -> QGroupBox:
        box = QGroupBox('Keep')
        column = QVBoxLayout(box)
        column.setSpacing(6)

        self.keep_manual = QCheckBox('Values I typed myself')
        self.keep_manual.setToolTip(
            'Anything you edited by hand. Ticked, a reset can never overwrite your own '
            'work, whatever the confidence threshold below says.')
        column.addWidget(self.keep_manual)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.keep_confident = QCheckBox('Values we are at least')
        self.keep_confident.setToolTip(
            'Keep identifications the sources were sure about, and throw away the weak '
            'guesses so they can be worked out again.')
        row.addWidget(self.keep_confident)
        self.threshold = QSpinBox()
        self.threshold.setRange(0, 100)
        self.threshold.setSingleStep(5)
        self.threshold.setSuffix('%')
        self.threshold.setFixedWidth(70)
        self.threshold.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # The window's stylesheet paints a spin box as a plain field, which leaves
        # Fusion's up/down buttons sitting in an unpainted notch on the right. Every
        # other input here is a plain field, so this is one too - the arrow keys and
        # the wheel still step it.
        self.threshold.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.threshold.setToolTip('Type a percentage, or step it with the arrow keys')
        row.addWidget(self.threshold)
        row.addWidget(QLabel('sure of'))
        row.addStretch(1)
        column.addLayout(row)

        self.keep_decisions = QCheckBox('Approved / Rejected decisions')
        self.keep_decisions.setToolTip(
            'Unticked, every book this reset clears goes back to Pending and has to be '
            'reviewed again.')
        column.addWidget(self.keep_decisions)

        # Remembered between runs, but only ever applied when you choose the reset.
        self.keep_manual.setChecked(self.settings.get_bool('AO_LOAD_KEEP_MANUAL', True))
        self.keep_confident.setChecked(
            self.settings.get_bool('AO_LOAD_KEEP_CONFIDENT', False))
        self.threshold.setValue(self.settings.get_int('AO_LOAD_KEEP_ABOVE', 75))
        self.keep_decisions.setChecked(
            self.settings.get_bool('AO_LOAD_KEEP_DECISIONS', False))

        for widget in (self.keep_manual, self.keep_confident, self.keep_decisions):
            widget.toggled.connect(lambda _: self._refresh())
        self.threshold.valueChanged.connect(lambda _: self._refresh())
        return box

    # ------------------------------------------------------------------ notice

    def _drift_found(self, generation: int, drift) -> None:
        if generation != self._drift_finder.generation:
            return
        drift = drift or {}
        self.drift = drift
        if 'added' in drift:
            self.fresh_drift = drift
        self._empty_ids = set(drift.get('files', {}).get('empty') or ())
        self._refresh()

    def _remove_empty(self) -> None:
        """Take the books with no files out of the list, now. Nothing else changes."""
        ids = set(self._empty_ids or ())
        if not ids or self.on_remove is None:
            return
        count = len(ids)
        books = [entry for entry in self.all_entries if entry.entry_id in ids]
        confirm = self.settings.get_bool('AO_UI_CONFIRM_INPUT_ACTIONS', True)
        if confirm and not confirm_list(
                self, 'Are you sure?',
                f'Remove these {_plural(count, "book")} with no files on disk from the '
                f'list?\n\nNothing is loaded and nothing on disk is touched. The list '
                f'is backed up first.',
                [(f'Removed from the list ({count}):', books_lines(books))],
                yes=f'Remove {count}', no='Cancel'):
            return
        self.on_remove(sorted(ids))
        self.all_entries = [entry for entry in self.all_entries
                            if entry.entry_id not in ids]
        self.selected = [entry for entry in self.selected if entry.entry_id not in ids]
        self._removed += len(ids)
        self._empty_ids = set()
        if self.drift.get('files'):
            self.drift['files']['empty'] = []
            self.drift['empty'] = 0
        self._refresh()

    def _live_entries(self) -> List[BookEntry]:
        return self.all_entries

    def _notice_sections(self) -> list:
        """The books and files behind every line of the notice, named one by one."""
        sections = []
        folder = self.folder_path()
        if self.folder_changed_from and folder is not None:
            old = [entry for entry in self.all_entries if not _under(entry.folder, folder)]
            sections.append((f'Books in the list from {self.folder_changed_from} '
                             f'({len(old)}):', books_lines(old, files=False)))
        files = self.drift.get('files', {})
        for key, heading in (('added', 'Audio files added'),
                             ('missing', 'Audio files no longer there'),
                             ('changed', 'Audio files that changed size')):
            items = files.get(key) or []
            sections.append((f'{heading} ({len(items)}):', list(items)))
        empty = self._empty_ids or set()
        if empty:
            books = [entry for entry in self.all_entries if entry.entry_id in empty]
            sections.append((f'Books with no files on disk ({len(books)}):',
                             books_lines(books)))
        return sections

    def _notice_html(self) -> str:
        lines = []
        if self.folder_changed_from:
            lines.append(f'The input folder was changed. The list still holds the '
                         f'books from <b>{self.folder_changed_from}</b>.')
        drift = self.drift
        if drift.get('unreadable'):
            lines.append('The input folder could not be read.')
        parts = [f'{_plural(count, "audio file")} {word}'
                 for word, count in (('added', drift.get('added', 0)),
                                     ('no longer there', drift.get('missing', 0)),
                                     ('changed size', drift.get('changed', 0)))
                 if count]
        if parts:
            lines.append('The folder differs from the list: ' + ', '.join(parts)
                         + '. <b>Update the list</b> brings it back in step without '
                           'losing anything.')
        empty = self._empty_ids or set()
        if empty:
            books = [entry for entry in self.all_entries if entry.entry_id in empty]
            applied = sum(1 for entry in books if entry.status == 'applied')
            count = len(books)
            what = (f'<b>{_plural(count, "book")}</b> in the list '
                    f'{"has" if count == 1 else "have"} no files on disk any more')
            why = (f' ({applied} already applied and moved - their records were left '
                   f'behind)' if applied else '')
            lines.append(f'{what}{why}. Removing them does not load anything.')
        if self._removed:
            lines.append(f'Removed {_plural(self._removed, "book")} with no files from '
                         f'the list. Nothing else was changed.')
        if not lines:
            return ''
        link = (f' <a href="list" style="color:{ACCENT}">show which</a>'
                if any(items for _, items in self._notice_sections()) else '')
        return (f'<span style="color:{STATUS_TEXT["risky"]}"><b>!</b></span>&nbsp; '
                + '<br>'.join(lines) + link)

    def _show_notice_list(self) -> None:
        list_dialog(self, 'What differs from the list',
                    'Every book and file the notice is about.',
                    self._notice_sections(), ['Close'], default=0)

    def done(self, result: int) -> None:
        self._counter.stop()
        self._recount_timer.stop()
        super().done(result)

    # ------------------------------------------------------------------ folder

    def folder_path(self) -> Optional[Path]:
        """The chosen folder, resolved the way Settings.get_path resolves it."""
        text = self.folder.text().strip()
        if not text:
            return None
        path = Path(text)
        return path if path.is_absolute() else PROJECT_ROOT / path

    def _browse(self) -> None:
        start = self.folder_path()
        path = QFileDialog.getExistingDirectory(
            self, 'Input folder', str(start) if start else '')
        if not path:
            return
        # Inside the program directory it is recorded relative, as Settings does.
        chosen = Path(path)
        try:
            chosen = chosen.relative_to(PROJECT_ROOT)
        except ValueError:
            pass
        self.folder.setText(str(chosen))

    def _folder_edited(self) -> None:
        self._folder_ids = None
        self._counter.stop()
        self._recount_timer.start()
        self._refresh()

    def _recount(self) -> None:
        self._folder_ids = None
        folder = self.folder_path()
        if folder is not None and folder.is_dir():
            self._counter.start(folder)
            self._drift_finder.start(folder, self.all_entries)
        else:
            self._counter.stop()
            self._drift_finder.start(None, self.all_entries)
        self._refresh()

    def _counted(self, generation: int, ids) -> None:
        if generation != self._counter.generation:
            return
        self._folder_entries = dict(ids or {})
        self._folder_ids = set(self._folder_entries)
        self._refresh()

    def _counts_html(self) -> str:
        live = self._live_entries()
        now = len(live)
        before = f'In the list now: <b>{_plural(now, "book")}</b>'
        folder = self.folder_path()
        if folder is None:
            return f'{before} &middot; no input folder chosen'
        if not folder.is_dir():
            return (f'{before} &middot; <span style="color:{STATUS_TEXT["rejected"]}">'
                    f'that folder does not exist</span>')
        if self._folder_ids is None:
            return f'{before} &middot; reading the folder...'
        diff = self._diff()
        after = now + len(diff['new']) - len(diff['folded']) - len(diff['gone'])

        rows = []

        def row(text: str, colour: str = TEXT_DIM) -> None:
            rows.append(f'<div style="color:{colour}">&nbsp;&nbsp;&middot;&nbsp; '
                        f'{text}</div>')

        row(f'{_plural(len(diff["stay"]), "book")} still in the folder, kept as '
            f'{"it is" if len(diff["stay"]) == 1 else "they are"}'
            + (f' ({len(diff["refreshed"])} with an updated file list)'
               if diff['refreshed'] else ''))
        if diff['finalized']:
            row(f'{_plural(len(diff["finalized"]), "book")} already finalized, kept as '
                f'{"it is" if len(diff["finalized"]) == 1 else "they are"}')
        if diff['outside']:
            row(f'{_plural(len(diff["outside"]), "book")} from another folder, kept as '
                f'{"it is" if len(diff["outside"]) == 1 else "they are"}')
        if diff['new']:
            row(f'{_plural(len(diff["new"]), "new book")} added',
                STATUS_TEXT['approved'])
        if diff['folded']:
            count = len(diff['folded'])
            row(f'{_plural(count, "book")} removed: '
                f'{"its" if count == 1 else "their"} files now belong to another book',
                STATUS_TEXT['risky'])
        if diff['gone']:
            row(f'{_plural(len(diff["gone"]), "book")} removed: '
                f'{"its" if len(diff["gone"]) == 1 else "their"} files are no longer '
                f'in the folder', STATUS_TEXT['rejected'])
        link = (f' &middot; <a href="diff" style="color:{ACCENT}">show every book</a>')
        return (f'{before} &rarr; after updating: <b>{_plural(after, "book")}</b>'
                f'{link}{"".join(rows)}')

    def _diff(self) -> dict:
        """What an update does to each book in the list, sorted into its kind of change.

        ``folded`` are books whose files are still on disk but now belong to another
        book - a folder the scanner used to split in two and now reads as one. They
        are not "gone", and saying so was what made the old count unreadable.
        """
        empty = {'new': [], 'gone': [], 'folded': [], 'stay': [], 'refreshed': [],
                 'finalized': [], 'outside': []}
        folder = self.folder_path()
        if folder is None or self._folder_ids is None:
            return empty
        found = self._folder_ids
        diff = empty
        owner = {}
        for entry in self._folder_entries.values():
            for path in entry.absolute_files():
                owner[os.path.normcase(str(path))] = entry
        listed = set()
        for entry in self._live_entries():
            if not _under(entry.folder, folder):
                diff['outside'].append(entry)
                continue
            listed.add(entry.entry_id)
            if entry.entry_id in found:
                diff['stay'].append(entry)
                scanned = self._folder_entries.get(entry.entry_id)
                if scanned is not None and (sorted(scanned.audio_files)
                                            != sorted(entry.audio_files)):
                    diff['refreshed'].append((entry, scanned))
                continue
            # Applied books are never dropped by a load: their files were moved.
            if entry.status == 'applied':
                diff['finalized'].append(entry)
                continue
            into = next((owner[key] for key in
                         (os.path.normcase(str(path)) for path in entry.absolute_files())
                         if key in owner), None)
            if into is not None:
                diff['folded'].append((entry, into))
            else:
                diff['gone'].append(entry)
        diff['new'] = [self._folder_entries[entry_id] for entry_id in sorted(found - listed)
                       if entry_id in self._folder_entries]
        return diff

    def _show_diff(self) -> None:
        diff = self._diff()
        folded = []
        for entry, into in diff['folded']:
            folded += books_lines([entry])
            folded.append(f'    -> now part of: {_book_name(into)}  '
                          f'({_plural(len(into.audio_files), "file")})')
        refreshed = [f'{_book_name(entry)}   {_plural(len(entry.audio_files), "file")} '
                     f'-> {_plural(len(scanned.audio_files), "file")}'
                     for entry, scanned in diff['refreshed']]
        list_dialog(self, 'What updating the list does',
                    'Nothing on disk is touched. Values, edits and Approved / Rejected '
                    'decisions are kept on every book that stays.',
                    [(f'Added - new in the folder ({len(diff["new"])}):',
                      books_lines(diff['new'], files=False)),
                     (f'Removed - now part of another book ({len(diff["folded"])}):',
                      folded),
                     (f'Removed - files no longer in the folder ({len(diff["gone"])}):',
                      books_lines(diff['gone'])),
                     (f'File list updated ({len(refreshed)}):', refreshed),
                     (f'Still in the folder, kept ({len(diff["stay"])}):',
                      [_book_name(entry) for entry in diff['stay']]),
                     (f'Already finalized, kept ({len(diff["finalized"])}):',
                      [_book_name(entry) for entry in diff['finalized']]),
                     (f'From another folder, kept ({len(diff["outside"])}):',
                      [_book_name(entry) for entry in diff['outside']])],
                    ['Close'], default=0)

    def _clear(self) -> None:
        count = len(self.all_entries)
        if not confirm_list(
                self, 'Clear list',
                f'Remove all {_plural(count, "book")} from the list, along with '
                f'everything identified, edited and decided for them?\n\n'
                f'Nothing on disk is touched.',
                [(f'Removed from the list ({count}):',
                  books_lines(self.all_entries, files=False))],
                yes=f'Clear {count}', no='Cancel'):
            return
        self.clear_chosen = True
        self.accept()

    # ---------------------------------------------------------------- results

    def resetting(self) -> bool:
        """True only when you chose to reset values - never by default."""
        return bool(self.all_entries) and self.mode_reset.isChecked()

    def keep_options(self) -> KeepOptions:
        if not self.resetting():
            return KeepOptions.keep_everything()
        return self._ticked_keep()

    def _ticked_keep(self) -> KeepOptions:
        return KeepOptions(
            manual=self.keep_manual.isChecked(),
            # Unticked, nothing is kept for being confident - only your own edits are,
            # and only if the box above is ticked. 101 is out of reach of any value.
            above=self.threshold.value() if self.keep_confident.isChecked() else 101,
            decisions=self.keep_decisions.isChecked(),
        )

    def scope(self) -> Optional[List[BookEntry]]:
        """The books to load: None for "the whole input folder"."""
        if self.selected and self.scope_selected.isChecked():
            return list(self.selected)
        return None

    def load_list_only(self) -> bool:
        return self.list_only.isChecked()

    def remember(self) -> None:
        """Persist the folder and the choices, the way the apply dialog persists its own."""
        self.settings.set('AO_INPUT_DIR', self.folder.text().strip())
        self.settings.set('AO_LOAD_LIST_ONLY', self.list_only.isChecked())
        if self.resetting():
            self.settings.set('AO_LOAD_KEEP_MANUAL', self.keep_manual.isChecked())
            self.settings.set('AO_LOAD_KEEP_CONFIDENT', self.keep_confident.isChecked())
            self.settings.set('AO_LOAD_KEEP_ABOVE', self.threshold.value())
            self.settings.set('AO_LOAD_KEEP_DECISIONS', self.keep_decisions.isChecked())
        try:
            self.settings.save()
        except OSError:
            pass

    # ---------------------------------------------------------------- refresh

    def _refresh(self) -> None:
        resetting = self.resetting()
        self.reset_options.setEnabled(resetting)
        self.threshold.setEnabled(resetting and self.keep_confident.isChecked())
        targets = self.scope()
        entries = self._live_entries() if targets is None else targets
        plan = plan_load(entries, self._ticked_keep())
        self.summary.setText(self._summary_html(plan, len(entries)))

        # The button says what it does. Not painted red: red next to Cancel reads as
        # a second Cancel; the reset's cost is spelled out beside it and confirmed.
        if not self.all_entries:
            text = 'Load'
        elif resetting:
            text = (f'Reload and reset {_plural(plan.cleared, "value")}'
                    if plan.cleared else 'Reload')
        else:
            text = 'Update list'
        if targets is not None:
            text += f'  ({_plural(len(targets), "selected book")})'
        self.go.setText(text)
        folder = self.folder_path()
        self.go.setEnabled(folder is not None and folder.is_dir())
        self.counts.setText(self._counts_html())

        notice = self._notice_html()
        self.notice_text.setText(notice)
        self.notice.setVisible(bool(notice))
        empty = len(self._empty_ids or ())
        self.purge.setVisible(bool(empty) and self.on_remove is not None)
        self.purge.setText(f'Remove {empty} from the list now')
        self._fit_height()

    def _fit_height(self) -> None:
        layout = self.layout()
        if layout is None:
            return
        layout.activate()
        height = self.sizeHint().height()
        if height != self._fitted_height:
            self._fitted_height = height
            self.resize(self._width if not self.isVisible() else self.width(), height)

    def _summary_html(self, plan: LoadPlan, books: int) -> str:
        """A table of what a reset keeps and clears, per field.

        Per field, because "31 values cleared" is a number you cannot do anything
        with: what you want to know before resetting is that every series number is
        about to go and every author is safe.
        """
        rows = []
        for name in IDENTITY_FIELDS:
            tally = plan.tally(name)
            reset = (f'<span style="color:{STATUS_TEXT["rejected"]}">{tally.cleared}'
                     f'</span>' if tally.cleared else
                     f'<span style="color:{TEXT_FAINT}">-</span>')
            kept = (f'{tally.kept}' if tally.kept else
                    f'<span style="color:{TEXT_FAINT}">-</span>')
            rows.append(f'<tr><td>{FIELD_LABELS[name]}</td>'
                        f'<td align="right">{kept}</td>'
                        f'<td align="right">{reset}</td></tr>')

        header = (f'<tr><td></td>'
                  f'<td align="right" style="color:{TEXT_FAINT}">KEPT&nbsp;&nbsp;</td>'
                  f'<td align="right" style="color:{TEXT_FAINT}">RESET</td></tr>')
        table = (f'<table cellspacing="0" cellpadding="0" width="260">'
                 f'{header}{"".join(rows)}</table>')

        notes = []
        if not plan.cleared:
            notes.append('Nothing is reset')
        elif plan.books:
            notes.append(f'{plan.books} of {_plural(books, "book")} affected')
        if plan.unreviewed:
            notes.append(f'{plan.unreviewed} back to Pending')
        if plan.skipped_applied:
            notes.append(f'{plan.skipped_applied} already saved, untouched')
        footer = (f'<div style="color:{TEXT_FAINT}">{" &middot; ".join(notes)}</div>'
                  if notes else '')
        return table + footer
