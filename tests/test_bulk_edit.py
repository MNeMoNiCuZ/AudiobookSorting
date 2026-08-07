"""The Edit-many-books grid.

Sorting the grid is the point of these tests: rows move, books do not. Every edit,
every undo step and the final diff has to follow the book it was made on, not the row
number it happened to be at.
"""

from __future__ import annotations

import pytest

from scripts.models import BookEntry


@pytest.fixture
def grid(qt_app):
    from scripts.gui.bulk_edit_dialog import BulkEditDialog

    def make(entry_id, index, title):
        entry = BookEntry(entry_id=entry_id, primary_audio=f'/library/{title}.mp3')
        for name, value in (('author', 'A'), ('series', 'S'),
                            ('series_index', index), ('title', title)):
            entry.set_field(name, value, 'user')
        return entry

    dialog = BulkEditDialog([make('e0', '10', 'Zeta'),
                             make('e1', '2', 'Alpha'),
                             make('e2', '1-3', 'Beta')])
    yield dialog
    dialog.deleteLater()


def titles(dialog):
    column = dialog._column_for('title')
    return [dialog.grid.item(row, column).text()
            for row in range(dialog.grid.rowCount())]


def test_headings_sort_and_reverse(grid):
    title_column = grid._column_for('title')

    grid._sort_by(title_column)
    assert titles(grid) == ['Alpha', 'Beta', 'Zeta']
    grid._sort_by(title_column)
    assert titles(grid) == ['Zeta', 'Beta', 'Alpha']


def test_book_numbers_sort_as_numbers(grid):
    """2 before 10, and a bundled "1-3" sorts on where the bundle starts."""
    grid._sort_by(grid._column_for('series_index'))
    assert titles(grid) == ['Beta', 'Alpha', 'Zeta']


def test_edits_follow_the_book_across_a_sort(grid):
    grid._sort_by(grid._column_for('title'))       # Alpha, Beta, Zeta
    grid.grid.item(0, grid._column_for('title')).setText('Edited')

    # Row 0 is Alpha, which is entry e1 - not entries[0].
    assert grid.values() == {'e1': {'title': 'Edited'}}

    grid._undo_last()
    assert grid.values() == {}
    assert titles(grid) == ['Alpha', 'Beta', 'Zeta']


def test_the_row_a_button_copies_from_survives_a_sort(grid):
    series_column = grid._column_for('series')
    grid.grid.setCurrentCell(0, series_column)     # unsorted row 0 is Zeta / e0
    grid.grid.item(0, series_column).setText('Chosen')

    grid._sort_by(grid._column_for('title'))       # Zeta drops to the bottom
    grid._fill_down('series')

    # Filled from Zeta's row wherever it now sits, not from whatever landed on row 0.
    assert grid.values() == {'e0': {'series': 'Chosen'},
                             'e1': {'series': 'Chosen'},
                             'e2': {'series': 'Chosen'}}


def test_fill_down_buttons_never_crop_the_name_they_would_write(qt_app):
    """The button's whole job is naming the value - it may not be shortened."""
    from scripts.gui.bulk_edit_dialog import BulkEditDialog

    author = 'Wolfgang Amadeus Fitzgerald-Montgomery III'
    series = 'A Very Long Series Name Indeed, Book The Second'

    def make(entry_id, title):
        entry = BookEntry(entry_id=entry_id, primary_audio=f'/library/{title}.mp3')
        for name, value in (('author', author), ('series', series),
                            ('series_index', '1'), ('title', title)):
            entry.set_field(name, value, 'user')
        return entry

    dialog = BulkEditDialog([make('e0', 'One'), make('e1', 'Two')])
    dialog.show()
    qt_app.processEvents()
    try:
        for name, value in (('author', author), ('series', series)):
            button = dialog.column_buttons[name]
            assert button.text() == f'Set as {value}'      # no ellipsis in the text
            # And wide enough to paint it, inside a window wide enough to hold it.
            assert button.width() >= button.sizeHint().width(), name
            assert button.x() + button.width() <= dialog.width(), name
    finally:
        dialog.close()
        dialog.deleteLater()
