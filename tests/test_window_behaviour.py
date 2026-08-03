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
from scripts.models import BookEntry, Field                           # noqa: E402


@pytest.fixture(scope='session')
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, settings):
    win = MainWindow(settings)
    win.show()
    qt_app.processEvents()
    yield win
    win.close()


def shape_filter(name):
    return next(test for label, test in SHAPE_FILTERS if label == name)


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
