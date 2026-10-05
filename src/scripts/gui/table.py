"""The table widget every grid in the app is built on."""

from __future__ import annotations

from PyQt6.QtCore import QModelIndex, Qt
from PyQt6.QtWidgets import QAbstractItemView, QTableWidget


class ColumnTable(QTableWidget):
    """A QTableWidget whose Home/End move within the current column.

    Home and End jump to the top and bottom of the column the cursor is in, skipping
    rows the filter has hidden. Ctrl+Home and Ctrl+End take over Qt's plain behaviour
    and go to the start and end of the row. Shift still extends the selection, since
    Qt builds that from whatever index this returns.
    """

    # The direction of the sort in progress, for items whose comparison depends on it.
    sort_order = Qt.SortOrder.AscendingOrder

    def sortItems(self, column: int,
                  order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self.sort_order = order
        super().sortItems(column, order)

    def moveCursor(self, action, modifiers) -> QModelIndex:
        Action = QAbstractItemView.CursorAction
        current = self.currentIndex()
        if action not in (Action.MoveHome, Action.MoveEnd) or not current.isValid():
            return super().moveCursor(action, modifiers)
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            return super().moveCursor(action, Qt.KeyboardModifier.NoModifier)
        rows = range(self.rowCount())
        if action == Action.MoveEnd:
            rows = reversed(rows)
        for row in rows:
            if not self.isRowHidden(row):
                return self.model().index(row, current.column())
        return current
