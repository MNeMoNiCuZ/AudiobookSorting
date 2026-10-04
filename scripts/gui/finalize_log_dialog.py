"""The Finalize log: every file Finalize moved or copied, and a way to put it back.

Right-clicking Finalize opens this. Each Finalize of a book is one row; expand it to
see its files. Select books, files, or a mix, and revert just those - the rest of the
library stays where it was filed. Reverted rows stay in the list, greyed out, so the
log is a record of what happened and not only of what can still be undone.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from .theme import TEXT, TEXT_DIM, TEXT_FAINT

COLUMNS = ['WHEN', 'BOOK / FILE', 'FROM', 'TO', 'STATE']
# Stored on each item: (pending index or None, move index or None).
ROLE = Qt.ItemDataRole.UserRole


class FinalizeLogDialog(QDialog):
    """Browse the Finalize journal and revert chosen books or files."""

    # {pending index: [move indices] or None for the whole book}
    revert_requested = pyqtSignal(dict)

    def __init__(self, provider: Callable[[], List[Dict]], parent=None):
        """`provider` returns one dict per transaction, oldest first, on every call."""
        super().__init__(parent)
        self.provider = provider
        self.setWindowTitle('Finalize log')
        self.resize(1100, 600)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        note = QLabel(
            'Every book Finalize has written, oldest first. Expand a book to see its '
            'files. Select books or single files and revert them to where they came '
            'from. If a later Finalize moved the same file again, that is reverted too.')
        note.setWordWrap(True)
        note.setStyleSheet(f'color: {TEXT_DIM};')
        layout.addWidget(note)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setColumnWidth(1, 260)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.tree, stretch=1)

        buttons = QHBoxLayout()
        self.revert_button = QPushButton('Revert selected')
        self.revert_button.setProperty('accent', True)
        self.revert_button.clicked.connect(self._revert_selected)
        buttons.addWidget(self.revert_button)

        self.revert_all = QPushButton('Revert everything')
        self.revert_all.setProperty('danger', True)
        self.revert_all.clicked.connect(self._revert_everything)
        buttons.addWidget(self.revert_all)

        buttons.addStretch(1)
        close = QPushButton('Close')
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.refresh()

    def refresh(self) -> None:
        expanded = {self.tree.topLevelItem(i).data(1, ROLE)
                    for i in range(self.tree.topLevelItemCount())
                    if self.tree.topLevelItem(i).isExpanded()}
        self.tree.clear()
        self._pending: List[int] = []
        for record in self.provider() or []:
            index = record.get('index')
            done = record.get('undone', False)
            moves = record.get('moves', [])
            reverted = sum(1 for m in moves if m.get('undone'))
            state = ('reverted' if done else
                     f'{reverted}/{len(moves)} reverted' if reverted else 'on disk')
            parent = QTreeWidgetItem([
                record.get('when', ''), record.get('name', ''), '',
                record.get('destination', ''), state])
            parent.setData(0, ROLE, (index, None))
            parent.setData(1, ROLE, record.get('key'))
            _tint(parent, TEXT_FAINT if done else TEXT)
            for position, move in enumerate(moves):
                child = QTreeWidgetItem([
                    move.get('operation', ''), _name(move.get('destination', '')),
                    move.get('source', ''), move.get('destination', ''),
                    'reverted' if move.get('undone') else 'on disk'])
                child.setData(0, ROLE, (None if move.get('undone') else index, position))
                _tint(child, TEXT_FAINT if move.get('undone') else TEXT)
                parent.addChild(child)
            self.tree.addTopLevelItem(parent)
            if record.get('key') in expanded:
                parent.setExpanded(True)
            if index is not None:
                self._pending.append(index)
        self._update_buttons()

    def _selection(self) -> Dict[int, Optional[List[int]]]:
        chosen: Dict[int, Optional[List[int]]] = {}
        for item in self.tree.selectedItems():
            index, position = item.data(0, ROLE) or (None, None)
            if index is None:
                continue
            if position is None:
                chosen[index] = None
            elif chosen.get(index, []) is not None:
                chosen.setdefault(index, []).append(position)
        return chosen

    def _update_buttons(self) -> None:
        self.revert_button.setEnabled(bool(self._selection()))
        self.revert_all.setEnabled(bool(self._pending))

    def _revert_selected(self) -> None:
        chosen = self._selection()
        if chosen:
            self.revert_requested.emit(chosen)

    def _revert_everything(self) -> None:
        if self._pending:
            self.revert_requested.emit({index: None for index in self._pending})


def _name(path: str) -> str:
    return path.replace('\\', '/').rsplit('/', 1)[-1]


def _tint(item: QTreeWidgetItem, colour: str) -> None:
    for column in range(item.columnCount()):
        item.setForeground(column, QBrush(QColor(colour)))
