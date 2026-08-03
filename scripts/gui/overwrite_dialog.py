"""Ask before a run overwrites something you typed yourself.

A value with source ``user`` is the one value in the program that is known to be
correct: a person looked at the book and wrote it down. Identification tiers are
guesses, however well-sourced, so they are not allowed to quietly replace one. When a
run wants to, it stops here first and shows exactly what it proposes to change.

Selection works the way lists work everywhere: click, Ctrl+click to add, Shift+click
for a range, plus a check box per row and select-all / select-none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, NamedTuple

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .theme import (ACCENT, FIELD, FIELD_BORDER, STATUS_HUES, TEXT_DIM,
                    table_modal_width)


class Proposal(NamedTuple):
    """One tier-proposed change to a cell a human filled in."""

    entry_id: str
    book: str          # what to call the book in the list
    field: str
    before: str
    after: str
    source: str        # the tier proposing it


# "ACCEPT" rather than a blank heading: a column of tick boxes with no title is a
# column of tick boxes that mean whatever you assume they mean.
COLUMNS = ['ACCEPT', 'BOOK', 'FIELD', 'YOURS', 'PROPOSED', 'FROM']

# Sources that are trusted enough to be ticked on arrival. These are the book
# databases - a catalogue record beating something typed in a hurry is the normal
# case, and making the user tick eleven boxes to accept the normal case is a tax on
# the thing they most often want. The model and the web scrape are not on the list:
# those are guesses, and a guess does not get to overwrite a person by default.
TRUSTED_SOURCES = {'audnexus', 'itunes', 'googlebooks', 'openlibrary', 'librivox',
                   'api', 'metadata'}


class OverwriteDialog(QDialog):
    """Pick which of the proposed overwrites to accept."""

    def __init__(self, proposals: List[Proposal], parent=None):
        super().__init__(parent)
        self.proposals = list(proposals)

        self.setWindowTitle('Overwrite your own edits?')
        # As wide as the review table it is proposing changes to; see
        # theme.table_modal_width.
        self.resize(table_modal_width(parent, 1100),
                    min(260 + 34 * len(self.proposals), 760))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        count = len(self.proposals)
        ticked = sum(1 for p in self.proposals
                     if p.source.lower() in TRUSTED_SOURCES)
        headline = QLabel(
            f'<b>{count}</b> value{"" if count == 1 else "s"} you typed yourself '
            f'would be replaced by this run.')
        headline.setStyleSheet('font-size: 15px;')
        layout.addWidget(headline)

        note = QLabel(
            f'The <b>{ticked}</b> coming from a book database '
            f'{"is" if ticked == 1 else "are"} ticked already - a catalogue record is '
            f'usually right. Anything from the web search or the language model is '
            f'left unticked; those are guesses, and yours wins by default. '
            f'Untick anything you would rather keep. Ctrl+click and Shift+click '
            f'select ranges.')
        note.setWordWrap(True)
        note.setStyleSheet(f'color: {TEXT_DIM};')
        layout.addWidget(note)

        self.table = QTableWidget(count, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Rows tall enough that a 20px check box is not squeezed into a hairline.
        self.table.verticalHeader().setDefaultSectionSize(32)
        # Every heading reads from the left, in line with the values under it. A
        # centred title over left-aligned text does not look like its column.
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 86)
        for column in (1, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        # The tick is the control on this screen, so it is given a real target and a
        # colour that is not the background it sits on.
        self.table.setStyleSheet(f"""
            QTableWidget::indicator {{
                width: 20px; height: 20px;
                border: 2px solid {FIELD_BORDER};
                border-radius: 4px;
                background: {FIELD};
                margin-left: 8px;
            }}
            QTableWidget::indicator:hover {{ border-color: {ACCENT}; }}
            QTableWidget::indicator:checked {{
                background: {STATUS_HUES['approved']};
                border-color: {STATUS_HUES['approved']};
            }}
        """)

        for row, proposal in enumerate(self.proposals):
            tick = QTableWidgetItem()
            tick.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                          | Qt.ItemFlag.ItemIsUserCheckable)
            tick.setCheckState(
                Qt.CheckState.Checked
                if proposal.source.lower() in TRUSTED_SOURCES
                else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, tick)

            for column, text in enumerate(
                    [proposal.book, proposal.field.replace('_', ' '),
                     proposal.before, proposal.after, proposal.source], start=1):
                item = QTableWidgetItem(str(text))
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.table.setItem(row, column, item)
            # "Yours" is what is being defended and "proposed" is what would replace
            # it, so the two are coloured against each other rather than left as one
            # undifferentiated wall of text.
            self.table.item(row, 3).setForeground(QColor('#ffffff'))
            self.table.item(row, 4).setForeground(QColor(STATUS_HUES['approved']))
            self.table.item(row, 5).setForeground(QColor(TEXT_DIM))
        layout.addWidget(self.table, stretch=1)

        # One row of buttons. The selection helpers and the decision belong to the
        # same moment, and stacking them made the window read as two dialogs.
        tools = QHBoxLayout()
        tools.setSpacing(8)
        for label, tooltip, slot in (
            ('Tick all', 'Accept every proposed change',
             lambda: self._set_all(Qt.CheckState.Checked)),
            ('Tick none', 'Keep every value you typed',
             lambda: self._set_all(Qt.CheckState.Unchecked)),
            ('Tick selected rows', 'Tick every row highlighted in the list',
             self._tick_selected),
        ):
            button = QPushButton(label)
            button.setToolTip(tooltip)
            button.clicked.connect(lambda _=False, s=slot: s())
            tools.addWidget(button)

        tools.addStretch(1)
        cancel = QPushButton('Keep all of mine')
        cancel.setToolTip('Discard every proposal and keep everything you typed')
        cancel.clicked.connect(self.reject)
        tools.addWidget(cancel)

        self.ok_button = QPushButton('')
        self.ok_button.setProperty('accent', True)
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self.accept)
        tools.addWidget(self.ok_button)
        layout.addLayout(tools)

        self.table.itemChanged.connect(lambda _item: self._update_button())
        self._update_button()

    def _update_button(self) -> None:
        """Name the button after what it will do - the count is the whole decision."""
        ticked = sum(1 for row in range(self.table.rowCount())
                     if self.table.item(row, 0).checkState() == Qt.CheckState.Checked)
        self.ok_button.setText(
            f'Apply the {ticked} ticked' if ticked else 'Apply nothing')

    def _set_all(self, state: Qt.CheckState) -> None:
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(state)

    def _tick_selected(self) -> None:
        for row in sorted({index.row() for index in self.table.selectedIndexes()}):
            self.table.item(row, 0).setCheckState(Qt.CheckState.Checked)

    def accepted_changes(self) -> Dict[str, List[str]]:
        """entry_id -> the fields whose overwrite was accepted."""
        allowed: Dict[str, List[str]] = {}
        for row, proposal in enumerate(self.proposals):
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked:
                allowed.setdefault(proposal.entry_id, []).append(proposal.field)
        return allowed


def book_label(entry) -> str:
    """A short, recognisable name for a book in the proposal list."""
    return (entry.value('title')
            or Path(entry.primary_audio).name
            or entry.entry_id)
