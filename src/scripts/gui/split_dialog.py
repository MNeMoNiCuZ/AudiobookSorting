"""Split one chaptered .m4b into separate books.

The grid editor, with one row per chapter instead of one per book: the same four
fields, the same keys, copy and paste, undo and the "Set as" buttons. Each chapter
starts out as a book of its own, titled after the chapter. Blank a chapter's title
and it joins the book above it instead - an omnibus whose books each run to twenty
chapters is split by clearing the titles of the chapters that are not a book's first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from PyQt6.QtWidgets import QDialogButtonBox, QLabel, QPushButton

from ..chapter_split import Chapter
from ..models import IDENTITY_FIELDS, BookEntry, Field
from ..paths import render_template, sanitize_component
from .bulk_edit_dialog import FIELDS, BulkEditDialog
from .theme import STATUS_TEXT


class SplitBook(NamedTuple):
    """One book the split will write: its values, its chapters, and where it goes."""

    values: Dict[str, str]
    chapters: List[Chapter]
    folder: Path
    filename: str


def _clock(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours}:{minutes:02d}:{seconds:02d}'


class SplitDialog(BulkEditDialog):
    """Name the books an .m4b is cut into; one row per chapter."""

    def __init__(self, entry: BookEntry, chapters: List[Chapter], base: Path,
                 settings, parent=None):
        self.source = entry
        self.chapters = list(chapters)
        self.base = Path(base)
        rows = []
        for index, chapter in enumerate(self.chapters):
            row = BookEntry(entry_id=f'chapter-{index}', primary_audio=entry.primary_audio,
                            notes=f'{index + 1:>3}.  {chapter.title}   '
                                  f'({_clock(chapter.start)} - {_clock(chapter.end)})')
            for name in ('author', 'series'):
                if entry.value(name):
                    setattr(row, name, Field(value=entry.value(name), source='user',
                                             confidence=1.0))
            row.title = Field(value=chapter.title, source='user', confidence=1.0)
            rows.append(row)
        super().__init__(rows, parent=parent, settings=settings)

        self.setWindowTitle(f'Split {Path(entry.primary_audio).name} into books')
        self.grid.horizontalHeaderItem(0).setText('CHAPTER')

        self.headline = QLabel('')
        self.headline.setWordWrap(True)
        self.headline.setMinimumWidth(1)
        self.layout().insertWidget(0, self.headline)

        box = self.findChild(QDialogButtonBox)
        self.split_button: Optional[QPushButton] = (
            box.button(QDialogButtonBox.StandardButton.Apply) if box else None)
        self.grid.itemChanged.connect(lambda _item: self._refresh_split())
        self._refresh_split()

    @staticmethod
    def _row_label(entry: BookEntry) -> str:
        return entry.notes

    # ------------------------------------------------------------------ books

    def _row_values(self, chapter: int) -> Dict[str, str]:
        row = self._row_of(chapter)
        names = [name for name, _ in FIELDS]
        out = {}
        for name in IDENTITY_FIELDS:
            item = self.grid.item(row, names.index(name))
            out[name] = item.text().strip() if item else ''
        return out

    def books(self) -> List[SplitBook]:
        """The books, in chapter order. A chapter with no title joins the one above.

        Read by chapter, not by row: a heading click re-sorts the rows, and the books
        are still cut in the order the chapters play.
        """
        groups: List[Dict] = []
        for index, chapter in enumerate(self.chapters):
            values = self._row_values(index)
            if values['title'] or not groups:
                groups.append({'values': values, 'chapters': [chapter]})
            else:
                groups[-1]['chapters'].append(chapter)

        taken = set()
        out = []
        for group in groups:
            name = self._name_for(group['values'])
            folder_name = name
            counter = 2
            while (folder_name.lower() in taken
                   or (self.base / folder_name).exists()):
                folder_name = f'{name} ({counter})'
                counter += 1
            taken.add(folder_name.lower())
            out.append(SplitBook(values=group['values'], chapters=group['chapters'],
                                 folder=self.base / folder_name,
                                 filename=f'{name}.m4b'))
        return out

    def _name_for(self, values: Dict[str, str]) -> str:
        """Folder and file name, from the file template every other book is named by."""
        template = self.settings.get(
            'AO_FILE_TEMPLATE', '{series} {series_index:02d} - {title}')
        template = template.replace('{file_index:03d}', '').replace('{file_index}', '')
        rendered = render_template(template, values).replace('/', ' - ').strip()
        return (sanitize_component(rendered, fallback='')
                or sanitize_component(values.get('title', ''), fallback='Book'))

    # ---------------------------------------------------------------- refresh

    def _refresh_split(self) -> None:
        if not hasattr(self, 'headline'):
            return
        first_blank = not self._row_values(0)['title']
        books = self.books()
        count = len(books)
        where = self.settings.display_path(self.base) if self.settings else str(self.base)
        if first_blank:
            self.headline.setText(
                f'<span style="color:{STATUS_TEXT["rejected"]}">Chapter 1 needs a '
                f'title - it is where the first book starts.</span>')
        else:
            self.headline.setText(
                f'<b>{count}</b> book{"" if count == 1 else "s"}, each written to a '
                f'new folder in <b>{where}</b>. Each chapter starts as a book titled '
                f'after it; clear a chapter\'s title to join it to the book above. '
                f'The audio is copied, not re-encoded.')
        if self.split_button is not None:
            self.split_button.setText(f'Split into {count} book'
                                      f'{"" if count == 1 else "s"}')
            self.split_button.setEnabled(not first_blank and count > 1)

    def accept(self) -> None:
        self.grid._commit()
        if not self._row_values(0)['title'] or len(self.books()) < 2:
            return
        super().accept()
