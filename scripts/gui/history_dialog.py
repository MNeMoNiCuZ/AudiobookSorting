"""The undo history, as a list you can step back and forward through.

Right-clicking Undo opens this. It behaves the way a history palette does in an image
editor, because that is the model everyone already has:

* every undoable step is listed oldest at the top, newest at the bottom;
* one of them is the *current* position, highlighted;
* steps below the current position are the future - they are drawn greyed out, and
  clicking one redoes forward to it;
* the moment you do something new, the future is gone and those rows disappear.

The window is a view onto the main window's history, not a copy of it. It asks the
window to move to a position and then re-reads the whole list, so there is exactly one
history and no way for the two to drift apart.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .theme import ACCENT, TEXT, TEXT_DIM, TEXT_FAINT

COLUMNS = ['#', 'WHAT HAPPENED', 'KIND']


class HistoryDialog(QDialog):
    """Pick a point in the undo history and go to it."""

    # Emitted with an index into the combined history: everything from there on is
    # undone. -1 means "undo everything".
    undo_to = pyqtSignal(int)
    # Emitted with an index into the future list: redo forward through it.
    redo_to = pyqtSignal(int)

    def __init__(self, provider: Callable[[], Dict], parent=None):
        """`provider` returns {'past': [...], 'future': [...]} on every call."""
        super().__init__(parent)
        self.provider = provider

        self.setWindowTitle('History')
        self.resize(720, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        headline = QLabel('Everything you have done, oldest first.')
        headline.setStyleSheet(f'color: {TEXT};')
        layout.addWidget(headline)

        note = QLabel(
            'Click a step to go back to how things were <i>after</i> it. Greyed-out '
            'steps below the current position are the future - click one to redo '
            'forward to it. Doing anything new discards the future.')
        note.setWordWrap(True)
        note.setStyleSheet(f'color: {TEXT_DIM};')
        layout.addWidget(note)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 48)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(2, 110)
        self.table.itemDoubleClicked.connect(lambda _item: self._go())
        layout.addWidget(self.table, stretch=1)

        buttons = QHBoxLayout()
        self.go_button = QPushButton('Go to the selected step')
        self.go_button.setProperty('accent', True)
        self.go_button.clicked.connect(self._go)
        buttons.addWidget(self.go_button)

        undo_all = QPushButton('Undo everything')
        undo_all.setProperty('danger', True)
        undo_all.setToolTip('Walk the whole history back to the original state')
        undo_all.clicked.connect(lambda: self.undo_to.emit(0))
        buttons.addWidget(undo_all)

        buttons.addStretch(1)
        close = QPushButton('Close')
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.refresh()

    # ------------------------------------------------------------------ update

    def refresh(self) -> None:
        """Re-read the history. Called after every step so the list cannot go stale."""
        state = self.provider() or {}
        self.past: List[Dict] = list(state.get('past') or [])
        self.future: List[Dict] = list(state.get('future') or [])

        self.table.setRowCount(0)
        # Row 0 is the original state, so "undo everything" is a row you can click
        # rather than a button you have to know about.
        self._add_row(0, 'Original state - nothing done yet', '', past=True,
                      current=not self.past, origin=True)
        for index, step in enumerate(self.past):
            self._add_row(index + 1, step.get('label', ''), step.get('kind', ''),
                          past=True, current=index == len(self.past) - 1)
        for index, step in enumerate(self.future):
            self._add_row(len(self.past) + index + 1, step.get('label', ''),
                          step.get('kind', ''), past=False, current=False)

        self.go_button.setEnabled(self.table.rowCount() > 1)

    def _add_row(self, number: int, label: str, kind: str, past: bool,
                 current: bool, origin: bool = False) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)

        # The future is dimmed rather than hidden: seeing what you can redo to is the
        # reason to open this window at all.
        colour = ACCENT if current else (TEXT if past else TEXT_FAINT)
        marker = '▶' if current else ('' if past else '·')
        cells = [f'{marker} {number}' if marker else str(number),
                 label or '(unnamed step)',
                 'redo' if not past else ('start' if origin else kind)]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(str(text))
            item.setForeground(_colour(colour))
            if current:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.table.setItem(row, column, item)
        if current:
            self.table.selectRow(row)

    # ----------------------------------------------------------------- actions

    def _go(self) -> None:
        rows = {index.row() for index in self.table.selectedIndexes()}
        if not rows:
            return
        row = min(rows)
        # Row 0 is the original state; rows 1..len(past) are done steps; anything
        # beyond that is the redo list.
        if row <= len(self.past):
            self.undo_to.emit(row)
        else:
            self.redo_to.emit(row - len(self.past) - 1)


def _colour(name: str):
    from PyQt6.QtGui import QBrush, QColor
    return QBrush(QColor(name))
