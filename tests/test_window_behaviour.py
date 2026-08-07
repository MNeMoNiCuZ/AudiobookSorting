"""The window behaviours that kept regressing, pinned.

Every one of these is here because it was reported broken: the explanation panel
came back the wrong width, right-clicking Undo did nothing at all, and there was no
way to see which rows had edits waiting to be saved.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

pytest.importorskip('PyQt6.QtWidgets')

from PyQt6.QtCore import QPoint, Qt                                   # noqa: E402
from PyQt6.QtGui import QContextMenuEvent                             # noqa: E402
from PyQt6.QtTest import QTest                                        # noqa: E402
from PyQt6.QtWidgets import QApplication                              # noqa: E402

from scripts.gui.main_window import SHAPE_FILTERS, MainWindow         # noqa: E402
from scripts.load_options import plan_load                            # noqa: E402
from scripts.models import BookEntry, Field                           # noqa: E402
from scripts.gui.why_panel import GOOD, STATE_COLOURS                 # noqa: E402


@pytest.fixture
def window(qt_app, settings):
    win = MainWindow(settings)
    win.show()
    qt_app.processEvents()
    yield win
    win.close()


def shape_filter(name):
    return next(test for label, test in SHAPE_FILTERS if label == name)


def test_manual_source_run_is_marked_queued_on_its_card(window):
    entry = BookEntry(entry_id='queued')
    window.why_panel.show_entry(entry)
    window.set_identification_queued(entry, 'llm')

    card = next(card for card in window.why_panel._tier_cards(entry)
                if card.key == 'llm')
    assert card._badge == 'queued'


def test_manual_edit_card_only_shows_current_user_fields(window):
    entry = BookEntry(entry_id='manual')
    entry.log('user', 'Cleared on load: author, series, series_index, title')
    empty = window.why_panel._manual_card(entry)
    assert empty._badge == ''
    assert 'Cleared on load' not in empty.body.text()

    entry.set_field('title', 'Typed title', 'user')
    edited = window.why_panel._manual_card(entry)
    assert edited._badge == '1 field'
    assert 'Typed title' in edited.body.text()
    assert STATE_COLOURS[GOOD] in edited.header.styleSheet()


# ------------------------------------------------------------------ the panel width

def test_the_panel_comes_back_at_the_width_it_was_closed_at(qt_app, settings):
    """Saved as a width, restored as a width - not as a share of the window.

    The old code handed the split to a splitter that was not yet its final size, so
    Qt scaled it, and maximising afterwards shared the difference out by stretch
    factor. A panel dragged to 505px reopened at something else every time.
    """
    settings.set('AO_UI_REMEMBER_LAYOUT', 'true')
    settings.set('AO_UI_WINDOW', '1400,900,895,505,normal')

    win = MainWindow(settings)
    win.show()
    qt_app.processEvents()
    # The offscreen platform reports a small screen, and the window is shrunk to fit
    # it. What is under test is the arithmetic, so the splitter is given a real
    # working width. It has to be a generous one: the filter row cannot shrink below
    # about 1150px, so on anything narrower the panel gets whatever is left over
    # rather than the width it was saved at.
    win.splitter.resize(1900, 800)
    win._restore_split()

    assert win.splitter.sizes()[1] == pytest.approx(505, abs=4)
    win.close()


def test_a_saved_panel_width_cannot_swallow_the_table(qt_app, settings):
    settings.set('AO_UI_REMEMBER_LAYOUT', 'true')
    settings.set('AO_UI_WINDOW', '1400,900,10,9000,normal')

    win = MainWindow(settings)
    win.show()
    qt_app.processEvents()
    win.splitter.resize(1900, 800)
    win._restore_split()

    left, right = win.splitter.sizes()
    assert left > 0 and right < left
    win.close()


def test_closing_writes_the_panel_width_back(window, settings):
    window.splitter.resize(1400, 800)
    window.splitter.setSizes([1000, 400])
    expected = window.splitter.sizes()[1]

    window.close()

    parts = settings.get('AO_UI_WINDOW').split(',')
    assert len(parts) == 5
    assert int(parts[3]) == expected


def test_window_resize_keeps_the_restored_panel_width(qt_app, settings):
    settings.set('AO_UI_REMEMBER_LAYOUT', 'true')
    settings.set('AO_UI_WINDOW', '1400,900,1000,400,normal')

    win = MainWindow(settings)
    win.show()
    qt_app.processEvents()
    win.splitter.resize(1600, 800)
    win._restore_split()
    win.resize(1900, win.height())
    QTest.qWait(10)

    assert win.splitter.sizes()[1] == pytest.approx(400, abs=4)
    win.close()


def test_reset_layout_forgets_the_saved_widths(window):
    window.table.setColumnWidth(2, 999)
    window._has_saved_widths = True

    window.reset_layout()

    assert window._has_saved_widths is False
    assert window.table.columnWidth(2) != 999


# --------------------------------------------------------------- right-click Undo

def test_right_clicking_undo_opens_the_history(window, qt_app):
    """Reported broken repeatedly: customContextMenuRequested never fired."""
    button = window.toolbar.widgetForAction(window.tool_actions['undo'])

    QTest.mouseClick(button, Qt.MouseButton.RightButton, pos=QPoint(5, 5))
    qt_app.processEvents()

    assert window._history_dialog is not None


def test_a_platform_context_menu_event_opens_it_too(window, qt_app):
    """Windows sends this instead of a plain press, so both routes are wired."""
    button = window.toolbar.widgetForAction(window.tool_actions['undo'])
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(5, 5),
                              button.mapToGlobal(QPoint(5, 5)))

    qt_app.sendEvent(button, event)

    assert window._history_dialog is not None


def test_right_clicking_identify_opens_the_queue(window, qt_app):
    button = window.toolbar.widgetForAction(window.tool_actions['identify'])

    QTest.mouseClick(button, Qt.MouseButton.RightButton, pos=QPoint(5, 5))
    qt_app.processEvents()

    assert window._queue_dialog is not None


# ------------------------------------------------------------------- redo history

def test_the_future_is_listed_until_a_new_edit_discards_it(window, entries):
    window.set_entries(entries[:2])
    first = entries[0]

    window._record_edit('Set title', [(first.entry_id, 'title', first.title)])
    window._undo_last_edit()
    assert len(window.history_state()['future']) == 1

    window._record_edit('Set author', [(first.entry_id, 'author', first.author)])
    assert window.history_state()['future'] == []


def test_redo_walks_forward_to_any_point(window, entries):
    window.set_entries(entries[:2])
    first = entries[0]

    for step in range(3):
        window._record_edit(f'Edit {step}',
                            [(first.entry_id, 'title', Field(value=f'v{step}',
                                                             source='user'))])
    for _ in range(3):
        window._undo_last_edit()
    assert len(window.history_state()['future']) == 3

    # Index 1 means "redo forward through two of them".
    window._redo_to_position(1)
    state = window.history_state()
    assert len(state['past']) == 2
    assert len(state['future']) == 1


# ------------------------------------------------------------------- new filter

def test_unsaved_changes_matches_a_hand_edited_row():
    entry = BookEntry(entry_id='a', title=Field(value='Typed', source='user'))
    assert shape_filter('Unsaved changes')(entry) is True


def test_unsaved_changes_ignores_a_row_that_was_already_written():
    entry = BookEntry(entry_id='a', title=Field(value='Typed', source='user'),
                      applied_path='/library/Typed.m4b')
    assert shape_filter('Unsaved changes')(entry) is False


def test_unsaved_changes_ignores_a_row_nobody_touched():
    entry = BookEntry(entry_id='a', title=Field(value='Found', source='audnexus'))
    assert shape_filter('Unsaved changes')(entry) is False


# ------------------------------------------------------------- the paths banner

def test_the_banner_appears_when_no_input_folder_is_set(qt_app, settings):
    settings.set('AO_INPUT_DIR', '')
    win = MainWindow(settings)
    win.show()
    qt_app.processEvents()

    assert win.paths_banner.isVisible()
    assert 'input' in win.paths_banner_label.text()
    win.close()


def test_the_banner_stays_away_once_both_folders_are_set(window):
    assert not window.paths_banner.isVisible()


# ----------------------------------------------------------------- table headers

def test_every_heading_but_the_number_reads_from_the_left(window):
    from scripts.gui.main_window import COLUMNS, COL_INDEX

    for column in range(len(COLUMNS)):
        alignment = window.table.horizontalHeaderItem(column).textAlignment()
        if column == COL_INDEX:
            assert alignment & Qt.AlignmentFlag.AlignHCenter
        else:
            assert alignment & Qt.AlignmentFlag.AlignLeft, COLUMNS[column]


# ---------------------------------------------------------- the cancel controls

def _toolbar_item_geometry(window, widget):
    """The rect the toolbar's own layout gave `widget`, or None if it never laid it out."""
    layout = window.toolbar.layout()
    layout.activate()
    for index in range(layout.count()):
        if layout.itemAt(index).widget() is widget:
            return layout.itemAt(index).geometry()
    return None


def test_hiding_a_toolbar_widget_must_go_through_its_action(qt_app):
    """The mechanism behind the bug, in isolation.

    A widget hidden with widget.setVisible(False) is dropped from QToolBarLayout, and
    showing it again never puts it back: it stays parked at 0,0 at its default 640x480,
    which is how Cancel stayed unreachable for a whole run in a toolbar with hundreds of
    spare pixels. Hiding the QAction that addWidget returned is the only way that comes
    back. This test needs no window width, so it holds on the 800px test screen.
    """
    from PyQt6.QtWidgets import QToolBar, QToolButton

    def reveal(hide_the_widget: bool):
        bar = QToolBar()
        button = QToolButton()
        button.setText('Cancel')
        if hide_the_widget:
            button.setVisible(False)
            action = bar.addWidget(button)
        else:
            action = bar.addWidget(button)
            action.setVisible(False)
        bar.resize(1000, 40)          # far more room than one button needs
        bar.show()
        qt_app.processEvents()
        (button if hide_the_widget else action).setVisible(True)
        bar.layout().activate()
        qt_app.processEvents()
        bar.layout().activate()
        return button.geometry().width(), button.isHidden()

    assert reveal(hide_the_widget=True) == (640, True)     # the bug
    width, hidden = reveal(hide_the_widget=False)          # the fix
    assert width < 640 and not hidden


def test_cancel_and_the_queue_count_are_revealed_once_a_job_starts(window):
    """MainWindow toggles the actions, not the widgets - see the test above for why."""
    window.set_busy(True)
    window.show_progress(1, 5, 'Identifying')
    window.show_queue(['a job'])

    assert window._cancel_action.isVisible()
    assert window._queue_action.isVisible()
    assert window._progress_action.isVisible()
    # Each one is still a member of the toolbar's layout, not orphaned by a rebuild.
    for widget in (window.progress, window.cancel_button, window.queue_button):
        assert _toolbar_item_geometry(window, widget) is not None, widget


def test_the_cancel_controls_survive_a_toolbar_rebuild_mid_run(window):
    """Reordering the toolbar while a job runs used to lose Cancel until the next job."""
    window.set_busy(True)
    window.show_progress(1, 5, 'Identifying')
    window.show_queue(['a job'])

    window._build_toolbar()

    assert window._cancel_action.isVisible()
    assert window._queue_action.isVisible()
    assert window._progress_action.isVisible()


def test_the_cancel_controls_go_away_when_nothing_is_running(window):
    window.set_busy(True)
    window.show_queue(['a job'])
    window.set_busy(False)
    window.show_queue([])

    assert not window._cancel_action.isVisible()
    assert not window._queue_action.isVisible()
    assert not window._progress_action.isVisible()


def test_right_clicking_offers_both_cancels_from_cancel_and_from_the_queue_count(
        window, monkeypatch):
    from PyQt6.QtWidgets import QMenu

    shown = []
    monkeypatch.setattr(QMenu, 'exec',
                        lambda self, *a: shown.append([x.text() for x in self.actions()]))
    window.set_busy(True)
    window.show_queue(['a job', 'another'])

    window._show_cancel_menu()
    window._show_cancel_menu(window.queue_button)

    assert len(shown) == 2
    for menu in shown:
        assert menu[0] == 'Cancel the job running now'
        assert menu[1].startswith('Cancel all')
        assert '2 queued jobs' in menu[1]


def test_a_long_status_message_cannot_crowd_out_cancel(window):
    """An uncapped label grows with the message and pushes Cancel off the toolbar."""
    window.set_busy(True)
    window.show_progress(1, 5, 'Merging ' + 'a very long chapter title ' * 12)

    rect = _toolbar_item_geometry(window, window.status_label)
    assert rect is not None and rect.width() <= 340


# ------------------------------------------------------------- the stale-scan badge

def _icon_bytes(action, size):
    """The rendered pixels of an action's icon, so a badge can be seen rather than
    assumed present because the code that draws it was called."""
    image = action.icon().pixmap(size, size).toImage()
    return image.constBits().asstring(image.sizeInBytes())


def test_scan_badge_appears_when_the_input_folder_has_drifted(window, qt_app):
    """A "!" is painted onto Scan - and really onto its pixels, not just recorded."""
    action = window.tool_actions['scan']
    size = window._icon_size()

    window.set_scan_stale(None)
    qt_app.processEvents()
    clean = _icon_bytes(action, size)
    assert 'input folder has changed' not in action.toolTip()

    window.set_scan_stale({'added': 3, 'missing': 1, 'changed': 0})
    qt_app.processEvents()
    badged = _icon_bytes(action, size)

    assert badged != clean, 'the badge changed nothing on screen'
    assert '3 added' in action.toolTip()
    assert '1 missing' in action.toolTip()

    window.set_scan_stale(None)
    qt_app.processEvents()
    assert _icon_bytes(action, size) == clean, 'the badge did not come off again'


def test_scan_badge_survives_a_toolbar_rebuild(window, qt_app):
    """Rebuilding the toolbar makes fresh actions; the badge has to be put back."""
    window.set_scan_stale({'added': 2, 'missing': 0, 'changed': 0})
    window.refresh_toolbar()
    qt_app.processEvents()

    action = window.tool_actions['scan']
    size = window._icon_size()
    window_clean = MainWindow(window.settings)
    try:
        clean = _icon_bytes(window_clean.tool_actions['scan'], size)
    finally:
        window_clean.close()
    assert _icon_bytes(action, size) != clean
    assert '2 added' in action.toolTip()


# ------------------------------------------------------------------- Load Input

def _loaded(window, *entries):
    window.set_entries(list(entries))
    QApplication.processEvents()


def test_loading_an_empty_list_asks_nothing(window, monkeypatch):
    """There is nothing to lose, so there is no question worth putting on screen."""
    from scripts.gui import load_dialog

    monkeypatch.setattr(load_dialog.LoadInputDialog, 'exec',
                        lambda self: pytest.fail('a dialog for an empty list'))
    seen = []
    window.load_requested.connect(lambda targets, keep: seen.append((targets, keep)))

    window._request_load()

    assert len(seen) == 1
    targets, keep = seen[0]
    assert targets is None and keep.keeps_everything()


def test_loading_over_work_asks_what_to_keep(window, monkeypatch):
    """The one question a load has - and it is only asked when it is a real one."""
    from PyQt6.QtWidgets import QDialog

    from scripts.gui import load_dialog

    _loaded(window, BookEntry(entry_id='a', title=Field(value='Typed', source='user',
                                                        confidence=1.0)))
    monkeypatch.setattr(load_dialog.LoadInputDialog, 'exec',
                        lambda self: QDialog.DialogCode.Accepted)
    seen = []
    window.load_requested.connect(lambda targets, keep: seen.append((targets, keep)))

    window._request_load()

    assert len(seen) == 1
    targets, keep = seen[0]
    assert targets is None, 'no selection means the whole input folder'
    assert keep.manual is True


def test_the_load_dialog_defaults_to_the_selection(qt_app, settings):
    """Selecting rows says which books you mean, so the dialog starts there."""
    from scripts.gui.load_dialog import LoadInputDialog

    books = [BookEntry(entry_id=str(i)) for i in range(4)]
    dialog = LoadInputDialog(books, books[:2], settings)

    assert dialog.scope_selected.isChecked()
    assert len(dialog.scope()) == 2

    assert LoadInputDialog(books, [], settings).scope() is None, 'no selection, no scope'


def test_a_fresh_load_dialog_keeps_only_what_you_typed(qt_app, tmp_path):
    """Out of the box a load re-reads the folder; only your own edits are protected."""
    from scripts.gui.load_dialog import LoadInputDialog
    from scripts.settings import Settings

    dialog = LoadInputDialog([BookEntry(entry_id='a')], [], Settings(tmp_path / '.env'))

    assert dialog.keep_manual.isChecked()
    assert not dialog.keep_confident.isChecked()
    assert not dialog.keep_decisions.isChecked()
    keep = dialog.keep_options()
    assert keep.manual and keep.above > 100 and not keep.decisions


def test_the_load_button_is_never_painted_as_a_danger(qt_app, settings):
    """Red next to Cancel reads as a second Cancel - the summary says what is lost."""
    from scripts.gui.load_dialog import LoadInputDialog

    losing = BookEntry(entry_id='a', title=Field(value='Guessed', source='api',
                                                 confidence=0.2))
    dialog = LoadInputDialog([losing], [], settings)

    assert dialog.go.property('danger') in (None, False)
    assert 'RESET' in dialog.summary.text(), 'the loss is still spelled out'


def test_the_summary_counts_each_field_separately(qt_app, settings):
    """"31 values cleared" is not actionable; "every series number goes" is."""
    from scripts.gui.load_dialog import LoadInputDialog

    books = [BookEntry(entry_id=str(i),
                       author=Field(value='Sanderson', source='api', confidence=0.9),
                       series=Field(value='Mistborn', source='regex', confidence=0.4))
             for i in range(3)]
    dialog = LoadInputDialog(books, [], settings)
    dialog.keep_confident.setChecked(True)
    dialog.threshold.setValue(75)

    plan = plan_load(books, dialog.keep_options())
    assert (plan.tally('author').kept, plan.tally('author').cleared) == (3, 0)
    assert (plan.tally('series').kept, plan.tally('series').cleared) == (0, 3)

    text = dialog.summary.text()
    assert 'Series #' in text and 'Author' in text
    # The two sentences that said nothing worth reading are gone.
    assert 'Loading' not in text and 'tags and filenames' not in text


def test_the_load_dialog_remembers_what_you_ticked(qt_app, tmp_path):
    from scripts.gui.load_dialog import LoadInputDialog
    from scripts.settings import Settings

    config = Settings(tmp_path / '.env')
    dialog = LoadInputDialog([BookEntry(entry_id='a')], [], config)
    dialog.keep_decisions.setChecked(True)
    dialog.remember()

    assert Settings(tmp_path / '.env').get_bool('AO_LOAD_KEEP_DECISIONS') is True


def test_cancelling_the_load_dialog_loads_nothing(window, monkeypatch):
    from PyQt6.QtWidgets import QDialog

    from scripts.gui import load_dialog

    _loaded(window, BookEntry(entry_id='a'))
    monkeypatch.setattr(load_dialog.LoadInputDialog, 'exec',
                        lambda self: QDialog.DialogCode.Rejected)
    seen = []
    window.load_requested.connect(lambda *a: seen.append(a))

    window._request_load()

    assert not seen


def test_right_clicking_load_offers_the_scope_and_the_folder(window, monkeypatch):
    from PyQt6.QtWidgets import QMenu

    shown = []
    monkeypatch.setattr(QMenu, 'exec',
                        lambda self, *a: shown.append([x.text() for x in self.actions()]))
    _loaded(window, BookEntry(entry_id='a'), BookEntry(entry_id='b'))
    window.table.selectRow(0)

    window._show_load_menu()

    assert len(shown) == 1
    menu = shown[0]
    assert menu[0] == 'Load the whole input folder'
    assert menu[1] == 'Load only the 1 selected book'
    assert 'Choose input folder...' in menu


def test_the_load_menu_drops_the_selected_entry_when_nothing_is_selected(
        window, monkeypatch):
    """A menu lists what you can do right now - nothing greyed out, nothing lying."""
    from PyQt6.QtWidgets import QMenu

    shown = []
    monkeypatch.setattr(QMenu, 'exec',
                        lambda self, *a: shown.append([x.text() for x in self.actions()]))
    _loaded(window, BookEntry(entry_id='a'))
    window.table.clearSelection()

    window._show_load_menu()

    assert not any('selected' in text for text in shown[0])


def test_changing_the_input_folder_badges_the_button(window, qt_app):
    """The one kind of staleness no file comparison can find."""
    action = window.tool_actions['scan']
    size = window._icon_size()

    window.set_input_folder_changed(None)
    qt_app.processEvents()
    clean = _icon_bytes(action, size)

    window.set_input_folder_changed('D:/Audiobooks/Old')
    qt_app.processEvents()

    assert _icon_bytes(action, size) != clean, 'the badge changed nothing on screen'
    assert 'D:/Audiobooks/Old' in action.toolTip()

    window.set_input_folder_changed(None)
    qt_app.processEvents()
    assert _icon_bytes(action, size) == clean, 'the badge did not come off again'


def test_unsaved_rows_light_up_and_then_stop(window, qt_app):
    """Highlighted really means painted: the row carries the tint, the others do not."""
    from scripts.gui.delegates import ROLE_FLASH
    from scripts.gui.main_window import COL_TITLE

    typed = BookEntry(entry_id='typed', folder='/library/typed', resolved=True,
                      explicit_work_pending=True,
                      title=Field(value='Typed', source='user', confidence=1.0))
    found = BookEntry(entry_id='found', folder='/library/found',
                      title=Field(value='Found', source='audnexus', confidence=0.9))
    _loaded(window, typed, found)

    window.flash_unsaved(seconds=10)
    qt_app.processEvents()

    lit = {window.row_ids[row]: window.table.item(row, COL_TITLE).data(ROLE_FLASH)
           for row in range(window.table.rowCount())}
    assert lit['typed'], 'the unsaved row was not highlighted'
    assert not lit['found'], 'a row with nothing waiting was highlighted'
    assert 'unsaved changes' in window._status_text

    # Run it out: the highlight is temporary, and has to actually come off.
    window._flash_left = 0.01
    window._tick_flash()
    qt_app.processEvents()
    assert not window.table.item(0, COL_TITLE).data(ROLE_FLASH)
    assert not window._flash_timer.isActive()


# ------------------------------------------------ confidence review thresholds

def _identified(entry_id, confidence, status='pending'):
    return BookEntry(
        entry_id=entry_id, folder=f'/library/{entry_id}', status=status,
        author=Field(value='Author', source='test', confidence=confidence),
        title=Field(value='Title', source='test', confidence=confidence))


def test_confidence_setting_is_not_on_the_main_filter_row(window):
    assert not hasattr(window, 'confidence_score')


def test_confidence_bands_do_not_replace_review_status(window):
    from scripts.gui.delegates import ROLE_STATUS

    settings = window.settings
    settings.set('AO_UI_CONFIDENT_THRESHOLD', '0.85')
    settings.set('AO_UI_DOUBTFUL_THRESHOLD', '0.45')
    books = [_identified('green', 0.90), _identified('amber', 0.60),
             _identified('red', 0.30)]
    _loaded(window, *books)

    displayed = {}
    for row in range(window.table.rowCount()):
        entry_id = window._entry_at(row).entry_id
        displayed[entry_id] = (window.table.item(row, 7).text(),
                               window.table.item(row, 0).data(ROLE_STATUS))
    assert displayed == {
        'green': ('Pending', 'pending'),
        'amber': ('Pending', 'pending'),
        'red': ('Pending', 'pending'),
    }


def test_status_filter_lists_coloured_review_statuses(window):
    items = [window.status_filter.itemText(index)
             for index in range(window.status_filter.count())]
    assert items == ['All statuses', 'Pending', 'Unsure', 'Approved', 'Rejected',
                     'Applied', 'Duplicate', 'Has Warning']
    for index in range(1, window.status_filter.count()):
        assert window.status_filter.itemData(
            index, Qt.ItemDataRole.ForegroundRole) is not None


def test_has_warning_status_filter(window):
    warned = _identified('warned', 0.8)
    warned.author = Field(value='UPPER CASE', source='test', confidence=0.8)
    clean = _identified('clean', 0.8)
    _loaded(window, warned, clean)

    window.status_filter.setCurrentText('Has Warning')
    window._apply_filters()

    shown = {window._entry_at(row).entry_id
             for row in range(window.table.rowCount())
             if not window.table.isRowHidden(row)}
    assert shown == {'warned'}


def test_warnings_dialog_shows_book_columns_and_inline_fix(window, monkeypatch):
    from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QHeaderView,
                                 QLineEdit, QPushButton, QToolButton, QTreeWidget)
    from scripts.gui.theme import STATUS_TEXT

    book = BookEntry(
        entry_id='warned', folder='/library/warned', audio_files=['book.m4b'],
        status='approved',
        author=Field(value='AUTHOR NAME', source='test', confidence=0.8),
        series=Field(value='Series', source='test', confidence=0.8),
        series_index=Field(value='3', source='test', confidence=0.8),
        title=Field(value='Book Title', source='test', confidence=0.8))
    _loaded(window, book)
    shown = []
    def capture(dialog):
        dialog.show()
        QApplication.processEvents()
        QApplication.processEvents()
        shown.append(dialog)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(
        QDialog, 'exec', capture)

    window._show_warnings_dialog()

    dialog = shown[0]
    tree = dialog.findChild(QTreeWidget)
    assert dialog.width() == 1860
    assert [tree.headerItem().text(column) for column in range(8)] == [
        'File', 'Author', 'Series', '#', 'Title', 'Fixed', 'Fix', 'Ignore']
    finding = tree.topLevelItem(0)
    assert finding.text(0) == 'book.m4b'
    assert [tree.itemWidget(finding, column).text() for column in range(1, 6)] == [
        'AUTHOR NAME', 'Series', '3', 'Book Title', 'Author Name']
    assert [finding.text(column) for column in range(1, 6)] == ['', '', '', '', '']
    assert finding.foreground(1).color().name() == STATUS_TEXT['rejected']
    assert finding.foreground(5).color().name() == STATUS_TEXT['approved']
    assert finding.data(0, Qt.ItemDataRole.UserRole) == 'warned'
    assert finding.sizeHint(0).height() == 58
    assert tree.isSortingEnabled()
    widths = [tree.columnWidth(column) for column in range(8)]
    assert widths[3] == 52
    assert widths[6] == tree.ACTION_WIDTH
    assert widths[7] == tree.ACTION_WIDTH
    assert widths[0] > widths[4] > widths[1] >= widths[2] > widths[3]
    assert tree.viewport().width() - sum(widths) < 12

    button_texts = [button.text() for button in dialog.findChildren(QPushButton)]
    assert not any('Approve' in text or 'Reject' in text for text in button_texts)
    for column in range(1, 6):
        assert isinstance(tree.itemWidget(finding, column), QLineEdit)
    editor = tree.itemWidget(finding, 5)
    assert isinstance(editor, QLineEdit)
    assert editor.text() == 'Author Name'
    assert not editor.isClearButtonEnabled()
    assert not editor.hasFrame()
    editor.setText('Edited Author')
    fix_button = tree.itemWidget(finding, 6)
    ignore_button = tree.itemWidget(finding, 7)
    assert isinstance(fix_button, QToolButton)
    assert isinstance(ignore_button, QToolButton)
    assert not dialog.findChildren(QCheckBox)
    assert not fix_button.icon().isNull()
    assert not ignore_button.icon().isNull()
    assert fix_button.width() >= widths[6] - 2
    assert ignore_button.width() >= widths[7] - 2
    assert fix_button.height() == tree.visualItemRect(finding).height()
    assert ignore_button.height() == tree.visualItemRect(finding).height()
    assert fix_button.iconSize().width() == 44
    assert ignore_button.iconSize().width() == 44
    assert tree.header().sectionResizeMode(6) == QHeaderView.ResizeMode.Fixed
    assert tree.header().sectionResizeMode(7) == QHeaderView.ResizeMode.Fixed
    assert tree.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectItems
    for column in range(1, 6):
        assert tree.itemWidget(finding, column).alignment() & Qt.AlignmentFlag.AlignLeft
    assert fix_button.isEnabled()
    fix_button.click()
    assert book.value('author') == 'Edited Author'
    assert book.status == 'pending'
    assert book.explicit_work_pending


def test_warnings_table_redistributes_space_after_column_resize(window, monkeypatch):
    from PyQt6.QtWidgets import QDialog, QTreeWidget

    book = BookEntry(
        entry_id='warned', folder='/library/warned', audio_files=['book.m4b'],
        author=Field(value='UPPER CASE', source='test', confidence=0.8))
    _loaded(window, book)
    shown = []
    monkeypatch.setattr(
        QDialog, 'exec',
        lambda self: shown.append(self) or QDialog.DialogCode.Accepted)

    window._show_warnings_dialog()

    tree = shown[0].findChild(QTreeWidget)
    tree.setColumnWidth(0, tree.columnWidth(0) - 120)
    QApplication.processEvents()

    widths = [tree.columnWidth(column) for column in range(tree.columnCount())]
    assert tree.viewport().width() - sum(widths) < 12


def test_warnings_dialog_hides_fix_icon_without_a_proposal(window, monkeypatch):
    from PyQt6.QtWidgets import QDialog, QLineEdit, QToolButton, QTreeWidget

    book = BookEntry(
        entry_id='warned', folder='/library/warned', audio_files=['book.m4b'],
        title=Field(value='ThisIsAVeryLongJoinedTitleName',
                    source='test', confidence=0.8))
    _loaded(window, book)
    shown = []
    monkeypatch.setattr(
        QDialog, 'exec',
        lambda self: shown.append(self) or QDialog.DialogCode.Accepted)

    window._show_warnings_dialog()

    tree = shown[0].findChild(QTreeWidget)
    finding = tree.topLevelItem(0)
    fixed = tree.itemWidget(finding, 5)
    fix_button = tree.itemWidget(finding, 6)
    assert isinstance(fixed, QLineEdit)
    assert fixed.placeholderText() == ''
    assert fix_button.icon().isNull()
    assert not fix_button.isEnabled()


def test_warnings_dialog_sorts_fixable_findings_by_fix_column(window, monkeypatch):
    from PyQt6.QtWidgets import QDialog, QTreeWidget

    fixable = BookEntry(
        entry_id='fixable', folder='/library/fixable', audio_files=['fixable.m4b'],
        author=Field(value='AUTHOR NAME', source='test', confidence=0.8))
    manual = BookEntry(
        entry_id='manual', folder='/library/manual', audio_files=['manual.m4b'],
        title=Field(value='ThisIsAVeryLongJoinedTitleName',
                    source='test', confidence=0.8))
    _loaded(window, fixable, manual)
    shown = []
    monkeypatch.setattr(
        QDialog, 'exec',
        lambda self: shown.append(self) or QDialog.DialogCode.Accepted)

    window._show_warnings_dialog()

    tree = shown[0].findChild(QTreeWidget)
    tree.sortItems(6, Qt.SortOrder.AscendingOrder)
    assert tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole) == 'fixable'


def test_warnings_dialog_field_buttons_filter_one_field_at_a_time(window, monkeypatch):
    from PyQt6.QtWidgets import QDialog, QLabel, QPushButton, QTreeWidget

    book = BookEntry(
        entry_id='warned', folder='/library/warned', audio_files=['book.m4b'],
        author=Field(value='AUTHOR NAME', source='test', confidence=0.8),
        series=Field(value='SERIES NAME', source='test', confidence=0.8),
        title=Field(value='Track 01', source='test', confidence=0.8))
    _loaded(window, book)
    shown = []
    monkeypatch.setattr(
        QDialog, 'exec',
        lambda self: shown.append(self) or QDialog.DialogCode.Accepted)

    window._show_warnings_dialog()

    dialog = shown[0]
    tree = dialog.findChild(QTreeWidget)
    filters = {button.text(): button for button in dialog.findChildren(QPushButton)
               if button.text() in ('Author', 'Series', 'Title')}
    assert any(label.text() == 'Filter:' for label in dialog.findChildren(QLabel))
    filter_label = next(label for label in dialog.findChildren(QLabel)
                        if label.text() == 'Filter:')
    assert 'background: transparent' in filter_label.styleSheet()
    assert tree.topLevelItemCount() == 3

    filters['Author'].click()
    assert tree.topLevelItemCount() == 1
    assert tree.topLevelItem(0).data(
        0, int(Qt.ItemDataRole.UserRole) + 1) == 'author'
    assert filters['Author'].isChecked()

    filters['Series'].click()
    assert tree.topLevelItemCount() == 1
    assert tree.topLevelItem(0).data(
        0, int(Qt.ItemDataRole.UserRole) + 1) == 'series'
    assert not filters['Author'].isChecked()
    assert filters['Series'].isChecked()

    filters['Series'].click()
    assert tree.topLevelItemCount() == 3
    assert not any(button.isChecked() for button in filters.values())


def test_warnings_dialog_manual_fields_save_and_require_review(window, monkeypatch):
    from PyQt6.QtWidgets import QDialog, QLineEdit, QTreeWidget

    book = BookEntry(
        entry_id='warned', folder='/library/warned', audio_files=['book.m4b'],
        status='approved',
        author=Field(value='AUTHOR NAME', source='test', confidence=0.8),
        title=Field(value='Old Title', source='test', confidence=0.8))
    _loaded(window, book)
    shown = []
    monkeypatch.setattr(
        QDialog, 'exec',
        lambda self: shown.append(self) or QDialog.DialogCode.Accepted)

    window._show_warnings_dialog()

    tree = shown[0].findChild(QTreeWidget)
    title = tree.itemWidget(tree.topLevelItem(0), 4)
    assert isinstance(title, QLineEdit)
    title.setText('New Title')
    title.editingFinished.emit()

    assert book.value('title') == 'New Title'
    assert book.title.source == 'user'
    assert book.status == 'risky'
    assert book.explicit_work_pending


def test_warnings_dialog_has_apply_and_ignore_context_menu(window, monkeypatch):
    from PyQt6.QtWidgets import QDialog, QMenu, QTreeWidget

    book = BookEntry(
        entry_id='warned', folder='/library/warned', audio_files=['book.m4b'],
        author=Field(value='AUTHOR NAME', source='test', confidence=0.8))
    _loaded(window, book)
    shown = []
    menus = []
    monkeypatch.setattr(
        QDialog, 'exec',
        lambda self: shown.append(self) or QDialog.DialogCode.Accepted)
    monkeypatch.setattr(QMenu, 'exec', lambda self, *_args: menus.append(self))

    window._show_warnings_dialog()

    tree = shown[0].findChild(QTreeWidget)
    finding = tree.topLevelItem(0)
    tree.customContextMenuRequested.emit(tree.visualItemRect(finding).center())
    actions = {action.text(): action for action in menus[0].actions()}
    assert actions['Apply fix'].isEnabled()
    assert 'Ignore fix' in actions
    actions['Ignore fix'].trigger()
    assert book.warnings_silenced


def test_warnings_dialog_sorts_files_by_folder_then_filename(window, monkeypatch):
    from PyQt6.QtWidgets import QDialog, QTreeWidget

    books = [
        BookEntry(entry_id='z', folder='/library/A', audio_files=['z.m4b'],
                  author=Field(value='UPPER CASE', source='test', confidence=0.8)),
        BookEntry(entry_id='a', folder='/library/B', audio_files=['a.m4b'],
                  author=Field(value='UPPER CASE', source='test', confidence=0.8)),
        BookEntry(entry_id='b', folder='/library/A', audio_files=['b.m4b'],
                  author=Field(value='UPPER CASE', source='test', confidence=0.8)),
    ]
    _loaded(window, *books)
    shown = []
    monkeypatch.setattr(
        QDialog, 'exec',
        lambda self: shown.append(self) or QDialog.DialogCode.Accepted)

    window._show_warnings_dialog()

    tree = shown[0].findChild(QTreeWidget)
    tree.sortItems(0, Qt.SortOrder.AscendingOrder)
    assert [tree.topLevelItem(row).text(0) for row in range(3)] == [
        'b.m4b', 'z.m4b', 'a.m4b']


def test_first_subfolder_filter_is_added_to_shape_filter(window, tmp_path):
    root = tmp_path / '!Sorting'
    window.settings.set('AO_INPUT_DIR', str(root))
    target = _identified('target', 0.8)
    target.folder = str(root / '41-60 and SS' / 'J.D. Robb - 49 Vendetta in Death')
    other = _identified('other', 0.8)
    other.folder = str(root / 'Other' / 'Book')
    _loaded(window, target, other)

    name, folder = window._first_input_folder(target)
    window._set_folder_filter(name, folder)

    assert window.shape_filter.currentText() == 'Folder: 41-60 and SS'
    shown = {window._entry_at(row).entry_id
             for row in range(window.table.rowCount())
             if not window.table.isRowHidden(row)}
    assert shown == {'target'}


def test_unsure_rows_have_no_status_background_tint():
    from scripts.gui.theme import BG_BASE, STATUS_COLORS

    assert STATUS_COLORS['risky'] == BG_BASE


def test_confidence_filter_lists_coloured_bands_and_custom(window):
    items = [window.confidence_filter.itemText(index)
             for index in range(window.confidence_filter.count())]
    assert items == ['Any confidence', '80% and above', '50% to 79%',
                     'Below 50%', '...']
    for index in (1, 2, 3):
        assert window.confidence_filter.itemData(
            index, Qt.ItemDataRole.ForegroundRole) is not None


def test_confidence_filter_uses_the_configured_band_boundaries(window):
    window.settings.set('AO_UI_CONFIDENT_THRESHOLD', '0.80')
    window.settings.set('AO_UI_DOUBTFUL_THRESHOLD', '0.50')
    books = [_identified('high', 0.90), _identified('middle', 0.60),
             _identified('low', 0.30)]
    _loaded(window, *books)

    expected = {'80% and above': {'high'}, '50% to 79%': {'middle'},
                'Below 50%': {'low'}}
    for label, visible in expected.items():
        window.confidence_filter.setCurrentText(label)
        window._apply_filters()
        shown = {window._entry_at(row).entry_id
                 for row in range(window.table.rowCount())
                 if not window.table.isRowHidden(row)}
        assert shown == visible


def test_completeness_filter_has_extra_closed_width(window):
    text_width = window.missing_filter.fontMetrics().horizontalAdvance(
        'Any completeness')
    assert window.missing_filter.minimumWidth() >= text_width + 20


def test_threshold_actions_only_change_matching_undecided_rows(window):
    books = [_identified('high', 0.90), _identified('middle', 0.60),
             _identified('low', 0.30), _identified('decided', 0.95, 'rejected')]
    _loaded(window, *books)

    window._apply_review_threshold('approve', 80)
    window._apply_review_threshold('reject', 50)

    assert books[0].status == 'approved'
    assert books[1].status == 'pending'
    assert books[2].status == 'rejected'
    assert books[3].status == 'rejected'


def test_middle_click_runs_the_saved_threshold(window, qt_app):
    window.settings.set('AO_REVIEW_APPROVE_THRESHOLD', '0.80')
    high = _identified('high', 0.90)
    low = _identified('low', 0.40)
    _loaded(window, high, low)
    button = window.toolbar.widgetForAction(window.tool_actions['approve'])

    QTest.mouseClick(button, Qt.MouseButton.MiddleButton, pos=QPoint(5, 5))
    qt_app.processEvents()

    assert high.status == 'approved'
    assert low.status == 'pending'


def test_blank_saved_threshold_disables_middle_click(window, qt_app):
    window.settings.set('AO_REVIEW_APPROVE_THRESHOLD', '')
    high = _identified('high', 0.90)
    _loaded(window, high)
    button = window.toolbar.widgetForAction(window.tool_actions['approve'])

    QTest.mouseClick(button, Qt.MouseButton.MiddleButton, pos=QPoint(5, 5))
    qt_app.processEvents()

    assert high.status == 'pending'
    assert 'Disabled in Settings' in window.tool_actions['approve'].toolTip()


def test_legacy_auto_approve_setting_no_longer_approves_during_identification(settings):
    from scripts.resolver import Resolver

    settings.set('AO_AUTO_APPROVE_THRESHOLD', '0.80')
    book = _identified('high', 0.95)

    Resolver(settings)._finalise(book)

    assert book.status == 'pending'


def test_review_button_menu_has_saved_and_custom_entries(window, monkeypatch):
    from PyQt6.QtWidgets import QMenu

    shown = []
    monkeypatch.setattr(
        QMenu, 'exec',
        lambda self, *args: shown.append(
            [(action.text(), action.toolTip()) for action in self.actions()]))
    window.settings.set('AO_REVIEW_REJECT_THRESHOLD', '0.45')
    _loaded(window, _identified('book', 0.40))
    button = window.toolbar.widgetForAction(window.tool_actions['reject'])

    window._show_review_threshold_menu('reject', button)

    assert [text for text, _tip in shown[0]] == [
        'Decline under 45%', 'Decline under...']
    assert all(tip for _text, tip in shown[0])


def test_review_button_tooltips_explain_all_three_mouse_buttons(window):
    _loaded(window, _identified('book', 0.90))
    for key in ('approve', 'reject'):
        tooltip = window.tool_actions[key].toolTip()
        assert 'LMB:' in tooltip
        assert 'MMB:' in tooltip
        assert 'RMB:' in tooltip
