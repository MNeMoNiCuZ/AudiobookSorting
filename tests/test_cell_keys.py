"""Delete, Ctrl+C and Ctrl+V on the review table.

The table is a small spreadsheet, and these are the three keys people expect a
spreadsheet to answer. Each one has to go through the same undo machinery every other
edit does, and none of them may fire while a cell is actually being typed in.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

pytest.importorskip('PyQt6.QtWidgets')

from PyQt6.QtCore import QEvent, Qt                                   # noqa: E402
from PyQt6.QtGui import QKeyEvent                                     # noqa: E402
from PyQt6.QtWidgets import QApplication                              # noqa: E402

from scripts.gui.main_window import (COL_AUTHOR, COL_SERIES, COL_TITLE,  # noqa: E402
                                     MainWindow)
from scripts.models import BookEntry                                  # noqa: E402


@pytest.fixture
def window(qt_app, settings):
    win = MainWindow(settings)

    def book(entry_id, author, series, title):
        entry = BookEntry(entry_id=entry_id, primary_audio=f'/library/{title}.mp3')
        for name, value in (('author', author), ('series', series),
                            ('series_index', '1'), ('title', title)):
            entry.set_field(name, value, 'user')
        return entry

    win.set_entries([book('e0', 'A1', 'S1', 'One'),
                     book('e1', 'A2', 'S2', 'Two'),
                     book('e2', 'A3', 'S3', 'Three')])
    win.show()
    qt_app.processEvents()
    yield win
    win.close()


def press(window, key, control=False):
    """A key press delivered to the table, exactly as the filter would see it."""
    modifier = (Qt.KeyboardModifier.ControlModifier if control
                else Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(window.table,
                           QKeyEvent(QEvent.Type.KeyPress, key, modifier))


def select(window, cells):
    window.table.clearSelection()
    for row, column in cells:
        window.table.item(row, column).setSelected(True)


def values(window, name):
    return [window.entries[f'e{index}'].value(name) for index in range(3)]


def test_delete_clears_the_selected_cells(window):
    select(window, [(0, COL_AUTHOR), (2, COL_TITLE)])
    press(window, Qt.Key.Key_Delete)

    assert values(window, 'author') == ['', 'A2', 'A3']
    assert values(window, 'title') == ['One', 'Two', '']

    window._undo_last()
    assert values(window, 'author') == ['A1', 'A2', 'A3']
    assert values(window, 'title') == ['One', 'Two', 'Three']


def test_copy_puts_the_selected_block_on_the_clipboard(window):
    select(window, [(0, COL_AUTHOR), (0, COL_SERIES),
                    (1, COL_AUTHOR), (1, COL_SERIES)])
    press(window, Qt.Key.Key_C, control=True)

    assert QApplication.clipboard().text() == 'A1\tS1\nA2\tS2'


def test_one_copied_value_fills_the_whole_selection(window):
    QApplication.clipboard().setText('Filled')
    select(window, [(0, COL_AUTHOR), (1, COL_AUTHOR), (2, COL_AUTHOR)])
    press(window, Qt.Key.Key_V, control=True)

    assert values(window, 'author') == ['Filled', 'Filled', 'Filled']

    window._undo_last()
    assert values(window, 'author') == ['A1', 'A2', 'A3']


def test_a_copied_block_pastes_from_the_top_left_of_the_selection(window):
    select(window, [(1, COL_AUTHOR), (1, COL_SERIES),
                    (2, COL_AUTHOR), (2, COL_SERIES)])
    press(window, Qt.Key.Key_C, control=True)

    select(window, [(0, COL_AUTHOR)])
    press(window, Qt.Key.Key_V, control=True)

    # Two rows written from row 0 down, spilling past the selection as a block should.
    assert values(window, 'author') == ['A2', 'A3', 'A3']
    assert values(window, 'series') == ['S2', 'S3', 'S3']


def test_paste_leaves_read_only_columns_alone(window):
    QApplication.clipboard().setText('Filled')
    select(window, [(0, 1)])       # the Files column - nothing there is editable
    press(window, Qt.Key.Key_V, control=True)

    assert values(window, 'author') == ['A1', 'A2', 'A3']


def test_preview_with_no_selection_shows_what_finalize_would_write(window):
    """Preview used to list every row, rejected books included."""
    from scripts.models import STATUS_APPROVED, STATUS_REJECTED

    window.entries['e0'].status = STATUS_APPROVED
    window.entries['e1'].status = STATUS_REJECTED
    sent = []
    window.apply_requested.connect(lambda entries, preview: sent.append(
        ([entry.entry_id for entry in entries], preview)))

    window.table.clearSelection()
    window._request_apply(preview=True)
    assert sent == [(['e0'], True)]

    # Nothing approved yet is the one case where previewing the lot is the answer.
    window.entries['e0'].status = 'pending'
    sent.clear()
    window._request_apply(preview=True)
    assert sent == [(['e0', 'e1', 'e2'], True)]
