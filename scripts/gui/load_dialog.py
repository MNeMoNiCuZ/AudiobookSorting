"""The one question a load has to ask: what do you want to keep?

Loading the input folder is a single action, and on an empty list it asks nothing at
all - there is nothing to lose. It is only once you have worked on a list that the
question exists, and then it is always the same question, so it is always the same
dialog: which books, and which of the things we know about them survive.

The counts under the boxes are produced by the same code that does the clearing
(scripts/load_options), so what the dialog promises and what the load performs cannot
drift apart.
"""

from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QGroupBox, QHBoxLayout, QLabel, QPushButton, QRadioButton,
    QSpinBox, QVBoxLayout,
)

from ..load_options import KeepOptions, LoadPlan, plan_load
from ..models import IDENTITY_FIELDS, BookEntry
from .theme import ACCENT, STATUS_TEXT, TEXT_DIM, TEXT_FAINT

# The table's row names. Same words as the main table's columns, spelled out - a
# column header of "#" is readable above a column of numbers, on its own it is not.
FIELD_LABELS = {'author': 'Author', 'series': 'Series',
                'series_index': 'Series #', 'title': 'Title'}


class LoadInputDialog(QDialog):
    """Choose the scope of a load and what it is allowed to throw away."""

    settings_requested = pyqtSignal(str)

    def __init__(self, all_entries: List[BookEntry], selected: List[BookEntry],
                 settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.all_entries = list(all_entries)
        self.selected = list(selected)

        self.setWindowTitle('Load input folder')
        self.setMinimumWidth(660)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        headline = QLabel('<b>Load the input folder</b>')
        headline.setStyleSheet('font-size: 15px;')
        layout.addWidget(headline)

        # The folder itself, on its own line and in the font paths are read in -
        # "Load input" was a sentence that happened to contain a folder name.
        folder = QLabel(settings.display_path(settings.get_path('AO_INPUT_DIR')))
        folder.setWordWrap(True)
        folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        folder.setStyleSheet(
            f'color: {ACCENT}; font-family: Consolas, monospace; font-size: 12px;')
        layout.addWidget(folder)

        blurb = QLabel(f'{len(self.all_entries)} book'
                       f'{"" if len(self.all_entries) == 1 else "s"} are already in the '
                       f'list. Loading reads them from disk again, so choose what to '
                       f'keep of the work already done on them.')
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f'color: {TEXT_DIM};')
        layout.addWidget(blurb)

        layout.addWidget(self._build_scope())
        layout.addWidget(self._build_keep())

        self.summary = QLabel('')
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(
            f'color: {TEXT_DIM}; border-left: 2px solid {ACCENT}; padding: 6px 10px;')
        layout.addWidget(self.summary)

        buttons = QHBoxLayout()
        choose = QPushButton('Choose input folder...')
        choose.setToolTip('Open Settings and pick a different folder to load from')
        choose.clicked.connect(lambda: self.settings_requested.emit('General'))
        buttons.addWidget(choose)
        buttons.addStretch(1)

        cancel = QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)

        self.go = QPushButton('Load')
        self.go.setProperty('accent', True)
        self.go.setDefault(True)
        self.go.clicked.connect(self.accept)
        buttons.addWidget(self.go)
        layout.addLayout(buttons)

        self._refresh()

    # ------------------------------------------------------------------- build

    def _build_scope(self) -> QGroupBox:
        """Which books. Only asked when a selection makes it a real question."""
        box = QGroupBox('Load')
        column = QVBoxLayout(box)
        column.setSpacing(4)

        count = len(self.all_entries)
        self.scope_all = QRadioButton(
            f'The whole input folder  -  {count} book'
            f'{"" if count == 1 else "s"} in the list, plus anything new')
        self.scope_all.setToolTip('Walks the input folder: books that appeared are '
                                  'added, books that are gone are removed.')
        self.scope_selected = QRadioButton(
            f'Only the {len(self.selected)} selected book'
            f'{"" if len(self.selected) == 1 else "s"}')
        self.scope_selected.setToolTip('Re-reads just those books from disk. The rest '
                                       'of the list is left exactly as it is.')

        # A selection is a statement about which books you mean, so the dialog takes
        # it at its word - reloading the whole folder is then the deliberate choice.
        self.scope_selected.setChecked(bool(self.selected))
        self.scope_all.setChecked(not self.selected)
        for button in (self.scope_all, self.scope_selected):
            button.toggled.connect(lambda _: self._refresh())
            column.addWidget(button)

        # With nothing selected there is only one possible scope, so the question is
        # not worth a groupbox - the headline already said which folder.
        box.setVisible(bool(self.selected))
        return box

    def _build_keep(self) -> QGroupBox:
        box = QGroupBox('Keep')
        column = QVBoxLayout(box)
        column.setSpacing(6)

        self.keep_manual = QCheckBox('Values I typed myself')
        self.keep_manual.setToolTip(
            'Anything you edited by hand. Ticked, a load can never overwrite your own '
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
            'Unticked, every book this load clears goes back to Pending and has to be '
            'reviewed again.')
        column.addWidget(self.keep_decisions)

        # Remembered between runs: what you keep is a habit, not a per-load decision.
        # Fresh out of the box only your own typing is protected - a load means "read
        # this folder again", and everything the program worked out it can work out.
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

    # ---------------------------------------------------------------- results

    def keep_options(self) -> KeepOptions:
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

    def remember(self) -> None:
        """Persist the keep choices, the way the apply dialog persists its own."""
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
        self.threshold.setEnabled(self.keep_confident.isChecked())
        targets = self.scope()
        entries = self.all_entries if targets is None else targets
        plan = plan_load(entries, self.keep_options())

        # Not painted as a danger button: red here reads as "Cancel" next to the real
        # Cancel. What the load throws away is spelled out in the summary below.
        self.go.setText(f'Load {len(targets)}' if targets is not None else 'Load')
        self.summary.setText(self._summary_html(plan, len(entries)))

    def _summary_html(self, plan: LoadPlan, books: int) -> str:
        """A table of what survives, per field, and one line of anything else.

        Per field, because "31 values cleared" is a number you cannot do anything
        with: what you want to know before pressing Load is that every series number
        is about to go and every author is safe.
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
            notes.append(f'{plan.books} of {books} book'
                         f'{"" if books == 1 else "s"} affected')
        if plan.unreviewed:
            notes.append(f'{plan.unreviewed} back to Pending')
        if plan.skipped_applied:
            notes.append(f'{plan.skipped_applied} already saved, untouched')
        footer = (f'<div style="color:{TEXT_FAINT}">{" &middot; ".join(notes)}</div>'
                  if notes else '')
        return table + footer
