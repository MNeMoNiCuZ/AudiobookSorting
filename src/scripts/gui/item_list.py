"""Warnings that name what they are about.

"3 books have no files on disk" is not something you can act on: before pressing a
button that removes them you need to see which three. Every warning about books or
files in the input folder lists them here - the full list, in a box you can scroll,
select and copy - rather than only counting them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from ..models import BookEntry

# (heading, lines) - one block of the list.
Section = Tuple[str, List[str]]


def book_lines(entry: BookEntry, files: bool = True) -> List[str]:
    """A book as it reads in a list: its name, its folder, and each of its files."""
    author, title = entry.value('author'), entry.value('title')
    name = ' - '.join(part for part in (author, title) if part)
    name = name or Path(entry.primary_audio).name or entry.entry_id
    if entry.status:
        name += f'   [{entry.status}]'
    lines = [name, f'    {entry.folder}']
    if files:
        lines.extend(f'        {file}' for file in entry.audio_files)
    if entry.applied_path:
        lines.append(f'    moved to: {entry.applied_path}')
    return lines


def books_lines(entries: Iterable[BookEntry], files: bool = True) -> List[str]:
    lines: List[str] = []
    for entry in entries:
        lines.extend(book_lines(entry, files))
    return lines


def sections_text(sections: Sequence[Section]) -> str:
    blocks = []
    for heading, lines in sections:
        if lines:
            blocks.append('\n'.join([heading, *lines]))
    return '\n\n'.join(blocks)


def short_list(lines: Sequence[str], limit: int = 10) -> str:
    """The first few, for a tooltip; the dialogs show all of them."""
    shown = '\n'.join(f'   {line}' for line in lines[:limit])
    rest = len(lines) - limit
    return shown + (f'\n   ... and {rest} more' if rest > 0 else '')


class ItemList(QPlainTextEdit):
    """The list itself: read-only, monospaced, selectable."""

    def __init__(self, sections: Sequence[Section] = (), parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont('Consolas')
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self.setFont(font)
        self.max_lines = 14
        self.set_sections(sections)

    def set_sections(self, sections: Sequence[Section]) -> None:
        text = sections_text(sections)
        self.setPlainText(text)
        self.setVisible(bool(text))
        # As tall as what it holds, up to a limit - no empty box under two lines.
        lines = min(text.count('\n') + 1, self.max_lines)
        margins = self.contentsMargins()
        self.setFixedHeight(self.fontMetrics().lineSpacing() * lines
                            + int(self.document().documentMargin() * 2)
                            + margins.top() + margins.bottom()
                            + self.horizontalScrollBar().sizeHint().height() + 4)


def list_dialog(parent, title: str, text: str, sections: Sequence[Section],
                buttons: Sequence[str], default: Optional[int] = None) -> Optional[str]:
    """A message with the full list under it. Returns the button pressed, or None."""
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumWidth(900)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(10)

    message = QLabel(text)
    message.setWordWrap(True)
    layout.addWidget(message)
    items = ItemList()
    items.max_lines = 30
    items.set_sections(sections)
    layout.addWidget(items)

    row = QHBoxLayout()
    row.addStretch(1)
    chosen: List[str] = []
    for position, label in enumerate(buttons):
        button = QPushButton(label)
        if position == default:
            button.setDefault(True)
            button.setFocus()

        def press(_=False, label=label):
            chosen.append(label)
            dialog.accept()

        button.clicked.connect(press)
        row.addWidget(button)
    layout.addLayout(row)

    dialog.resize(1320, dialog.sizeHint().height())
    dialog.exec()
    return chosen[0] if chosen else None


def confirm_list(parent, title: str, text: str, sections: Sequence[Section],
                 yes: str = 'Yes', no: str = 'No') -> bool:
    """Ask, showing exactly what the answer applies to. Defaults to no."""
    return list_dialog(parent, title, text, sections, [yes, no], default=1) == yes
