"""Apply preview, shown as the folder tree the apply would produce.

A flat "WOULD APPLY x -> y" dump is unreadable once there are more than a handful of
books; what you actually want to check before moving files is the *shape* of the
resulting library. So this builds the destination tree - output root, author, series,
book folder, files - and lets you collapse the parts you have already eyeballed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout,
)

from .theme import (ACCENT, STATUS_TEXT, TEXT, TEXT_DIM, TEXT_FAINT,
                    table_modal_width)

COLUMNS = ['Destination', 'Operation', 'Source']
PREVIEW_WIDTHS_KEY = 'AO_UI_PREVIEW_COLUMN_WIDTHS'


def restore_preview_widths(tree: QTreeWidget, settings) -> None:
    """Restore shared preview widths, with a readable Operation default."""
    saved = (settings.get(PREVIEW_WIDTHS_KEY) if settings is not None else '') or ''
    try:
        widths = [int(value) for value in saved.split(',')]
    except ValueError:
        widths = []
    if len(widths) == len(COLUMNS) and all(value >= 40 for value in widths):
        for index, width in enumerate(widths):
            tree.setColumnWidth(index, width)
    else:
        tree.setColumnWidth(1, 130)


def save_preview_widths(tree: QTreeWidget, settings) -> None:
    if settings is None:
        return
    widths = [tree.columnWidth(index) for index in range(len(COLUMNS))]
    settings.set(PREVIEW_WIDTHS_KEY, ','.join(str(width) for width in widths))
    try:
        settings.save()
    except OSError:
        pass


class PreviewDialog(QDialog):
    """Tree view of an apply plan (#21). Read-only - nothing here writes anything."""

    # Emitted with a Settings tab name when a link on this dialog is followed.
    settings_requested = pyqtSignal(str)

    def __init__(self, results: List, output_root: Path, dry_run: bool = True,
                 parent=None, settings=None):
        super().__init__(parent)
        self.results = list(results)
        self.output_root = Path(output_root)
        # When given, the rename switches on this dialog edit it directly.
        self.settings = settings

        applied = sum(1 for r in self.results if r.ok)
        skipped = sum(1 for r in self.results if r.skipped)
        failed = sum(1 for r in self.results if r.error)

        self.setWindowTitle('Preview' if dry_run else 'Apply results')
        # The same width as the review table this is a preview of - see
        # theme.table_modal_width. 1500 is the fallback for a dialog with no window
        # behind it: wide enough to read a real tree of author/series/title folders
        # without scrolling sideways on every row.
        self.resize(table_modal_width(parent, 1500), 900)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        headline = QLabel(
            f'<b>{len(self.results)}</b> entries &nbsp;·&nbsp; '
            f'<span style="color:{STATUS_TEXT["approved"]}">{applied} ok</span> &nbsp;·&nbsp; '
            f'<span style="color:{STATUS_TEXT["risky"]}">{skipped} skipped</span> &nbsp;·&nbsp; '
            f'<span style="color:{STATUS_TEXT["rejected"]}">{failed} failed</span>')
        layout.addWidget(headline)

        self.note = QLabel(
            'Nothing has been written. This is what applying would produce.'
            if dry_run else 'This is what was written.')
        self.note.setStyleSheet(f'color: {TEXT_DIM};')
        layout.addWidget(self.note)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 760)
        layout.addWidget(self.tree, stretch=1)

        self._build_tree()
        restore_preview_widths(self.tree, self.settings)
        self.tree.header().sectionResized.connect(
            lambda *_: save_preview_widths(self.tree, self.settings))

        buttons = QHBoxLayout()
        self.show_files = QCheckBox('Show individual files')
        self.show_files.setChecked(True)
        self.show_files.toggled.connect(self._toggle_files)
        buttons.addWidget(self.show_files)

        expand = QPushButton('Expand all')
        expand.clicked.connect(self.tree.expandAll)
        collapse = QPushButton('Collapse all')
        collapse.clicked.connect(self._collapse_to_top)
        # Closing just the book folders keeps the author/series shape visible while
        # hiding the file lists - the usual way to skim a large plan.
        collapse_books = QPushButton('Collapse Books')
        collapse_books.setToolTip('Close every book folder, keeping the author and '
                                  'series folders open')
        collapse_books.clicked.connect(self._collapse_books)
        buttons.addWidget(expand)
        buttons.addWidget(collapse)
        buttons.addWidget(collapse_books)

        # The two settings that decide what this screen shows are edited here rather
        # than three tabs away in Settings - you are looking at the result of them.
        if self.settings is not None:
            self.rename_files = QCheckBox('Rename audio files')
            self.rename_files.setToolTip(
                'Rename the audio files to the file template, rather than keeping '
                'their original names')
            self.rename_files.setChecked(
                self.settings.get_bool('AO_RENAME_FILES', True))
            self.rename_files.toggled.connect(
                lambda on: self._set_setting('AO_RENAME_FILES', on))
            buttons.addWidget(self.rename_files)

            self.rename_support = QCheckBox('Rename covers / epub / pdf too')
            self.rename_support.setToolTip(
                'Also rename companion files to match, where there is exactly one '
                'file of that type')
            self.rename_support.setChecked(
                self.settings.get_bool('AO_RENAME_SUPPORT_FILES', False))
            self.rename_support.toggled.connect(
                lambda on: self._set_setting('AO_RENAME_SUPPORT_FILES', on))
            buttons.addWidget(self.rename_support)

            output = QPushButton('Output settings...')
            output.setToolTip('Open the Output settings page - templates, collision '
                              'policy, tags and sidecars')
            output.clicked.connect(lambda: self.settings_requested.emit('Output'))
            buttons.addWidget(output)

        buttons.addStretch(1)

        close = QPushButton('Close')
        close.setProperty('accent', True)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------ build

    def _display(self, path) -> str:
        """A path as the settings describe it - absolute only if configured absolute."""
        if self.settings is not None:
            return self.settings.display_path(path)
        return str(path)

    def _build_tree(self) -> None:
        root = QTreeWidgetItem([self._display(self.output_root), '', ''])
        root.setForeground(0, QColor(ACCENT))
        self.tree.addTopLevelItem(root)

        # Folders are shared between entries (one author holds many books), so nodes
        # are looked up by their relative path rather than created per result.
        folders: Dict[Path, QTreeWidgetItem] = {Path('.'): root}
        problems: List = []

        for result in self.results:
            if result.error or result.skipped:
                problems.append(result)
                continue
            parent = self._folder_node(result.destination, folders, root)
            self._mark_book(parent, result)
            for op in result.operations:
                target = Path(op['destination'])
                source = Path(op['source'])
                # Say when a file is being *renamed*, not just moved. "a.mp3 ->
                # Deep Sky 01 - The Deep Sky 001.mp3" is the thing worth checking
                # before you let this loose on a library.
                renamed = source.name != target.name
                # Only the new name. The old one is right there in the Source column,
                # and printing both put the answer in the middle of the noise - the
                # thing being checked here is what the library is about to look like.
                label = target.name
                operation = (f'{op["operation"]} + rename' if renamed
                             else op['operation'])
                child = QTreeWidgetItem([label, operation, self._display(source)])
                child.setForeground(0, QColor(ACCENT if renamed else TEXT))
                child.setForeground(1, QColor(TEXT_DIM))
                child.setForeground(2, QColor(TEXT_FAINT))
                child.setData(0, Qt.ItemDataRole.UserRole, 'file')
                parent.addChild(child)

        if problems:
            trouble = QTreeWidgetItem([f'Not applied ({len(problems)})', '', ''])
            trouble.setForeground(0, QColor(STATUS_TEXT['rejected']))
            self.tree.addTopLevelItem(trouble)
            for result in problems:
                reason = result.error or 'destination already exists - skipped'
                item = QTreeWidgetItem([result.entry_id, 'skip' if result.skipped
                                        else 'error', reason])
                colour = (STATUS_TEXT['risky'] if result.skipped
                          else STATUS_TEXT['rejected'])
                item.setForeground(0, QColor(colour))
                item.setForeground(2, QColor(colour))
                trouble.addChild(item)
            trouble.setExpanded(True)

        # Everything open by default. Checking an apply means reading the leaves -
        # the renamed files - so opening on a tree of closed author folders hides the
        # only part worth looking at, and "Collapse all" is right there for the rest.
        self.tree.expandAll()

        # Size the name column to its deepest expanded path rather than a guess, so
        # nothing is elided - this is the one view whose whole job is showing names.
        self.tree.resizeColumnToContents(0)
        self.tree.setColumnWidth(
            0, min(max(self.tree.columnWidth(0) + 24, 420), 1100))

    def _folder_node(self, destination: Path, folders: Dict[Path, QTreeWidgetItem],
                     root: QTreeWidgetItem) -> QTreeWidgetItem:
        """Node for `destination`, creating every missing ancestor beneath the root."""
        try:
            relative = Path(destination).relative_to(self.output_root)
        except ValueError:
            # Outside the configured output folder - show the whole path as one node,
            # still written the way the settings describe it.
            relative = Path(self._display(destination))

        current = Path('.')
        node = root
        for part in relative.parts:
            current = current / part
            existing = folders.get(current)
            if existing is None:
                existing = QTreeWidgetItem([part, '', ''])
                existing.setForeground(0, QColor(TEXT))
                node.addChild(existing)
                folders[current] = existing
            node = existing
        return node

    def _mark_book(self, node: QTreeWidgetItem, result) -> None:
        """The deepest folder of a result is the book itself - label it as such."""
        node.setForeground(0, QColor(STATUS_TEXT['approved']))
        node.setData(0, Qt.ItemDataRole.UserRole, 'book')
        node.setToolTip(0, f'{result.entry_id}\n{self._display(result.destination)}')
        node.setText(1, f'{len(result.operations)} file'
                        f'{"" if len(result.operations) == 1 else "s"}')
        node.setForeground(1, QColor(TEXT_DIM))

    # ---------------------------------------------------------------- actions

    def _set_setting(self, key: str, value: bool) -> None:
        """Flip a rename switch and say that the preview has to be re-run to see it."""
        self.settings.set(key, 'true' if value else 'false')
        try:
            self.settings.save()
        except OSError:
            pass
        self.note.setText('Setting saved. Re-run the preview to see it applied.')

    def _toggle_files(self, visible: bool) -> None:
        iterator = self.tree.findItems('', Qt.MatchFlag.MatchContains
                                       | Qt.MatchFlag.MatchRecursive, 0)
        for item in iterator:
            if item.data(0, Qt.ItemDataRole.UserRole) == 'file':
                item.setHidden(not visible)

    def _collapse_to_top(self) -> None:
        self.tree.collapseAll()
        for index in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(index).setExpanded(True)

    def _collapse_books(self) -> None:
        """Expand everything, then close only the book folders."""
        self.tree.expandAll()
        for item in self.tree.findItems('', Qt.MatchFlag.MatchContains
                                        | Qt.MatchFlag.MatchRecursive, 0):
            if item.data(0, Qt.ItemDataRole.UserRole) == 'book':
                item.setExpanded(False)
