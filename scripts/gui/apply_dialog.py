"""The "are you sure?" step in front of writing to disk.

Applying is the only thing this program does that touches your files, so it is the one
action worth stopping for. What it was stopping with was a message box reciting the
settings - copy or move, renaming on or off - and if any of them were wrong the only
way to change one was to dismiss the box, open Settings, find the tab, change it, come
back and press Save again.

So the settings are here instead. The same four decisions, at the moment they are
actually being made, with the summary sentence updating as they change. Anything set
here is written to .env like any other setting, because "how do I file my library" is
not a per-run choice - it is the choice, and this is where people make it.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QRadioButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from .preview_dialog import (COLUMNS, restore_preview_widths,
                             save_preview_widths)
from .theme import ACCENT, STATUS_TEXT, TEXT_DIM, TEXT_FAINT


class ApplyDialog(QDialog):
    """Confirm an apply, and edit the settings that decide what it does."""

    settings_requested = pyqtSignal(str)
    preview_requested = pyqtSignal()

    def __init__(self, entries, settings, preview_result=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.entries = list(entries)
        self.count = len(self.entries)

        self.setWindowTitle('Write approved books to disk')
        self.setMinimumWidth(700)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self.headline = QLabel('')
        self.headline.setStyleSheet('font-size: 15px;')
        self.headline.setWordWrap(True)
        layout.addWidget(self.headline)

        layout.addWidget(self._build_mode())
        layout.addWidget(self._build_naming())

        self.summary = QLabel('')
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(
            f'color: {TEXT_DIM}; border-left: 2px solid {ACCENT}; padding: 6px 10px;')
        layout.addWidget(self.summary)

        if preview_result is not None:
            layout.addWidget(self._build_preview(preview_result), stretch=1)

        undo = QLabel('Undo (Ctrl+Z) reverses this, and the History window can walk it '
                      'back further.')
        undo.setStyleSheet(f'color: {TEXT_FAINT};')
        layout.addWidget(undo)

        buttons = QHBoxLayout()
        more = QPushButton('All output settings...')
        more.setToolTip('Open the Output settings page - collision policy, tags, '
                        'sidecars, illegal characters')
        more.clicked.connect(lambda: self.settings_requested.emit('Output'))
        buttons.addWidget(more)
        buttons.addStretch(1)

        cancel = QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)

        self.go = QPushButton('')
        self.go.setProperty('accent', True)
        self.go.setDefault(True)
        self.go.clicked.connect(self._accept)
        buttons.addWidget(self.go)
        layout.addLayout(buttons)

        self._load()
        self._refresh()

    # ------------------------------------------------------------------- build

    def _build_mode(self) -> QGroupBox:
        box = QGroupBox('What happens to the originals')
        column = QVBoxLayout(box)
        column.setSpacing(4)

        self.copy_mode = QRadioButton(
            'Copy - the originals stay where they are')
        self.copy_mode.setToolTip(
            'A second copy is written to the output folder. Your input library is not '
            'touched, at the cost of twice the disk space.')
        self.move_mode = QRadioButton(
            'Move - the originals are taken out of the input folder')
        self.move_mode.setToolTip(
            'Faster, instant on the same drive, and nothing is duplicated - but the '
            'input copy is gone and Undo is the only way back.')
        for button in (self.copy_mode, self.move_mode):
            button.toggled.connect(lambda _: self._refresh())
            column.addWidget(button)
        return box

    def _build_naming(self) -> QGroupBox:
        box = QGroupBox('Naming')
        column = QVBoxLayout(box)
        column.setSpacing(6)

        self.rename_files = QCheckBox('Rename the audio files to the file template')
        self.rename_files.setToolTip(
            'Off, the files keep the names they have now and only the folders are '
            'organised.')
        self.rename_files.toggled.connect(lambda _: self._refresh())
        column.addWidget(self.rename_files)

        self.rename_support = QCheckBox(
            'Rename every other file that travels with the book to match')
        self.rename_support.setToolTip(
            'Cover art, .epub, .pdf, .nfo, .cue, .txt - anything that is not audio. '
            'A file is only renamed when it is the only one of its extension in the '
            'output folder, since two images would collide on the same name.\n'
            'This is the AO_RENAME_SUPPORT_FILES setting on the Output tab; changing '
            'it here changes it there.')
        self.rename_support.toggled.connect(lambda _: self._refresh())
        column.addWidget(self.rename_support)

        for label, key, tip in (
            ('Folder', 'AO_OUTPUT_TEMPLATE',
             'The folder each book is filed under, below the output folder.'),
            ('File', 'AO_FILE_TEMPLATE',
             'The name given to each audio file when renaming is on.'),
        ):
            row = QHBoxLayout()
            row.setSpacing(8)
            caption = QLabel(label)
            caption.setFixedWidth(50)
            caption.setStyleSheet(f'color: {TEXT_DIM};')
            row.addWidget(caption)
            edit = QLineEdit()
            edit.setToolTip(tip)
            edit.textChanged.connect(lambda _: self._refresh())
            row.addWidget(edit, stretch=1)
            setattr(self, 'template_' + key.lower(), edit)
            column.addLayout(row)

        self.example = QLabel('')
        self.example.setWordWrap(True)
        self.example.setStyleSheet(
            f'color: {ACCENT}; font-family: Consolas, monospace; font-size: 11px;')
        column.addWidget(self.example)
        return box

    def _build_preview(self, result) -> QGroupBox:
        box = QGroupBox('One-book preview')
        column = QVBoxLayout(box)
        self.preview_tree = QTreeWidget()
        self.preview_tree.setHeaderLabels(COLUMNS)
        self.preview_tree.setAlternatingRowColors(True)
        self.preview_tree.setUniformRowHeights(True)
        for operation in result.operations:
            self.preview_tree.addTopLevelItem(QTreeWidgetItem([
                str(operation['destination']), operation['operation'],
                str(operation['source'])]))
        restore_preview_widths(self.preview_tree, self.settings)
        self.preview_tree.header().sectionResized.connect(
            lambda *_: save_preview_widths(self.preview_tree, self.settings))
        column.addWidget(self.preview_tree)

        full = QPushButton('Open full preview...')
        full.setFlat(True)
        full.setToolTip('Preview every approved book without writing any files')
        full.setStyleSheet(f'color: {ACCENT}; text-align: left;')
        full.clicked.connect(self.preview_requested.emit)
        column.addWidget(full)
        return box

    # ------------------------------------------------------------- load / save

    def _load(self) -> None:
        copying = self.settings.get_bool('AO_COPY_MODE', True)
        self.copy_mode.setChecked(copying)
        self.move_mode.setChecked(not copying)
        self.rename_files.setChecked(self.settings.get_bool('AO_RENAME_FILES', True))
        self.rename_support.setChecked(
            self.settings.get_bool('AO_RENAME_SUPPORT_FILES', False))
        self.template_ao_output_template.setText(
            self.settings.get('AO_OUTPUT_TEMPLATE'))
        self.template_ao_file_template.setText(self.settings.get('AO_FILE_TEMPLATE'))

    def _accept(self) -> None:
        self.settings.set('AO_COPY_MODE', self.copy_mode.isChecked())
        self.settings.set('AO_RENAME_FILES', self.rename_files.isChecked())
        self.settings.set('AO_RENAME_SUPPORT_FILES', self.rename_support.isChecked())
        self.settings.set('AO_OUTPUT_TEMPLATE',
                          self.template_ao_output_template.text().strip())
        self.settings.set('AO_FILE_TEMPLATE',
                          self.template_ao_file_template.text().strip())
        try:
            self.settings.save()
        except OSError:
            pass
        self.accept()

    # ---------------------------------------------------------------- refresh

    def _refresh(self) -> None:
        from ..paths import render_template

        copying = self.copy_mode.isChecked()
        verb = 'Copy' if copying else 'Move'
        books = f'{self.count} approved book' + ('' if self.count == 1 else 's')
        self.headline.setText(f'<b>{verb} {books}</b> into your output folder?')
        self.go.setText(f'{verb} {self.count}')

        destination = self.settings.get('AO_OUTPUT_DIR')
        self.summary.setText(
            f'Destination: {destination}<br>'
            + ('Originals stay exactly where they are.' if copying else
               f'<span style="color:{STATUS_TEXT["rejected"]}">The originals are '
               f'removed from the input folder.</span>'))

        values = {'author': 'Brandon Sanderson', 'series': 'Mistborn',
                  'series_index': 2, 'title': 'The Well of Ascension',
                  'file_index': '03', 'extension': 'mp3'}
        try:
            folder = render_template(self.template_ao_output_template.text(), values)
            name = (render_template(self.template_ao_file_template.text(), values)
                    if self.rename_files.isChecked() else '(files keep their names)')
        except Exception as exc:          # a half-typed template is not an error
            self.example.setText(f'...  ({exc})')
            return
        if self.rename_files.isChecked() and not name.lower().endswith('.mp3'):
            name += '.mp3'
        self.example.setText(f'→  {folder}/{name}')
