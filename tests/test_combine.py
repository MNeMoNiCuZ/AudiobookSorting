"""Combining entries preserves their position and uses explicit metadata choices."""

import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PyQt6.QtWidgets')

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QMessageBox

from scripts.data_manager import DataManager
from scripts.gui.main_window import COL_TITLE, MainWindow
from scripts.models import BookEntry, Field


@pytest.fixture
def window(qt_app, settings):
    win = MainWindow(settings)
    win.table.horizontalHeader().setSortIndicator(COL_TITLE, Qt.SortOrder.AscendingOrder)
    yield win
    win.close()


def book(entry_id, title):
    return BookEntry(entry_id=entry_id, folder='books',
                     audio_files=[f'{entry_id}.mp3'], audio_sizes=[100],
                     author=Field('Author', 'user', 1.0),
                     title=Field(title, 'metadata', 0.75))


@pytest.mark.parametrize('live_sort', [False, True])
def test_combine_keeps_row_and_selection_under_active_sort(window, tmp_path, live_sort):
    window.settings.set('AO_UI_RESORT_LIVE', str(live_sort).lower())
    before, first, second, after = [book(str(i), title) for i, title in
                                  enumerate(['Alpha', 'Beta', 'Gamma', 'Omega'])]
    window.set_entries([after, second, first, before])
    window.table.horizontalHeader().setSortIndicator(COL_TITLE, Qt.SortOrder.AscendingOrder)
    window._sort_table()
    manager = DataManager(tmp_path / 'entries.json')
    manager.entries = dict(window.entries)
    combined = manager.combine([first, second], {'title': Field('Zulu', 'user', 1.0)})
    window.show_combined_entry(combined, [first, second])

    assert [window._entry_at(row) for row in range(3)] == [before, first, after]
    assert window.selected_entries() == [first]
    assert window.table.item(1, COL_TITLE).text() == 'Zulu'
    assert second.entry_id not in window.entries
    assert second.entry_id not in window._identity_keys
    assert all(second.entry_id not in group for group in window._identity_groups.values())
    assert first.audio_files == ['1.mp3', '2.mp3']
    manager.flush()
    restored = DataManager(tmp_path / 'entries.json')
    assert restored.entries[first.entry_id].value('title') == 'Zulu'
    assert restored.entries[first.entry_id].combined_by_user


def test_conflicts_require_choices_and_emit_in_visible_order(window, monkeypatch):
    first, second = book('first', 'First'), book('second', 'Second')
    second.series = Field('Series', 'metadata', 0.75)
    window.set_entries([first, second])
    emitted = []
    window.combine_requested.connect(lambda entries, fields: emitted.append((entries, fields)))

    def choose(dialog):
        combos = dialog.findChildren(QComboBox)
        assert len(combos) == 2
        buttons = dialog.findChild(QDialogButtonBox)
        accept = next(button for button in buttons.buttons() if button.text() == 'Combine')
        assert not accept.isEnabled()
        combos[0].setCurrentIndex(1)  # Keep the empty series deliberately.
        assert not accept.isEnabled()
        combos[1].setCurrentIndex(2)  # Use the second book's title.
        assert accept.isEnabled()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, 'exec', choose)
    window._confirm_combine([second, first])
    entries, fields = emitted[0]
    assert entries == [first, second]
    assert fields['series'].value == ''
    assert fields['title'].value == 'Second'
    assert first.value('title') == 'First'


def test_cancelling_conflicts_does_not_combine_or_change_metadata(window, monkeypatch):
    first, second = book('first', 'First'), book('second', 'Second')
    window.set_entries([first, second])
    emitted = []
    window.combine_requested.connect(lambda *args: emitted.append(args))
    monkeypatch.setattr(QDialog, 'exec', lambda dialog: QDialog.DialogCode.Rejected)
    window._confirm_combine([first, second])
    assert emitted == []
    assert first.value('title') == 'First'
    assert window.table.rowCount() == 2


def test_matching_metadata_needs_no_conflict_dialog(window, monkeypatch):
    first, second = book('first', 'Same'), book('second', 'Same')
    window.set_entries([first, second])
    emitted = []
    window.combine_requested.connect(lambda *args: emitted.append(args))
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.Yes)
    window._confirm_combine([first, second])
    assert emitted == [([first, second], {})]


def test_combined_metadata_keeps_provenance_without_sharing_fields(tmp_path):
    first, second = book('first', 'First'), book('second', 'Second')
    second.title.corroborated_by = ['search']
    manager = DataManager(tmp_path / 'entries.json')
    manager.entries = {entry.entry_id: entry for entry in [first, second]}
    combined = manager.combine([first, second], {'title': second.title})
    assert combined.title == second.title
    assert combined.title is not second.title
    assert combined.title.corroborated_by is not second.title.corroborated_by
    manager.flush()
