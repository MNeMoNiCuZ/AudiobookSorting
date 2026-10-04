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


def test_delete_clears_the_selected_cells(window, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    def unexpected_confirmation(*args):
        pytest.fail('Clearing individual cells must not ask for whole-row confirmation')

    monkeypatch.setattr(QMessageBox, 'question', unexpected_confirmation)
    select(window, [(0, COL_AUTHOR), (2, COL_TITLE)])
    press(window, Qt.Key.Key_Delete)

    assert values(window, 'author') == ['', 'A2', 'A3']
    assert values(window, 'title') == ['One', 'Two', '']

    window._undo_last()
    assert values(window, 'author') == ['A1', 'A2', 'A3']
    assert values(window, 'title') == ['One', 'Two', 'Three']


@pytest.mark.parametrize('choice', ['list', 'drive', 'cancel'])
@pytest.mark.parametrize('column', [None, 1, 6, 7])  # whole row, files, confidence, status
def test_delete_on_a_book_asks_to_remove_it(window, monkeypatch, choice, column):
    from PyQt6.QtWidgets import QMessageBox
    from scripts.models import IDENTITY_FIELDS

    book = window.entries['e0']
    original = {name: book.value(name) for name in IDENTITY_FIELDS}
    calls = []
    monkeypatch.setattr(window, '_remove_from_list', lambda e: calls.append(('list', e)))
    monkeypatch.setattr(window, '_delete_from_drive', lambda e: calls.append(('drive', e)))

    def exec_(box):
        buttons = {b.text(): b for b in box.buttons()}
        target = {'list': 'Remove from list', 'drive': 'Delete from drive...'}.get(choice)
        button = buttons[target] if target else box.button(
            QMessageBox.StandardButton.Cancel)
        button.click()
        return 0

    monkeypatch.setattr(QMessageBox, 'exec', exec_)
    row = window._row_for(book.entry_id)
    if column is None:
        window.table.selectRow(row)
    else:
        select(window, [(row, column)])
    press(window, Qt.Key.Key_Delete)

    assert {name: book.value(name) for name in IDENTITY_FIELDS} == original
    if choice == 'cancel':
        assert calls == []
    else:
        assert calls == [(choice, [book])]


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
    """The approved rows, plus the rejected ones - which the preview shows as skipped."""
    from scripts.models import STATUS_APPROVED, STATUS_REJECTED

    window.entries['e0'].status = STATUS_APPROVED
    window.entries['e1'].status = STATUS_REJECTED
    sent = []
    window.apply_requested.connect(lambda entries, preview: sent.append(
        ([entry.entry_id for entry in entries], preview)))

    window.table.clearSelection()
    window._request_apply(preview=True)
    assert sent == [(['e0', 'e1'], True)]

    # Nothing approved yet is the one case where previewing the lot is the answer.
    window.entries['e0'].status = 'pending'
    sent.clear()
    window._request_apply(preview=True)
    assert sent == [(['e0', 'e1', 'e2'], True)]


def test_finalize_sends_rejected_rows_to_be_reported_as_skipped(window):
    from scripts.models import STATUS_APPROVED, STATUS_REJECTED

    window.settings.set('AO_UI_CONFIRM_APPLY', False)
    window.entries['e0'].status = STATUS_APPROVED
    window.entries['e1'].status = STATUS_REJECTED
    sent = []
    window.apply_requested.connect(lambda entries, preview: sent.append(
        ([entry.entry_id for entry in entries], preview)))

    window._request_apply(preview=False)
    assert sent == [(['e0', 'e1'], False)]
