"""The settings page: every schema key must be reachable from some tab.

A setting that exists in SCHEMA but on no tab can only be changed by hand-editing .env,
which is how the Google Books key - the one thing standing between that source and
working at all - came to be unfindable.
"""

from __future__ import annotations

import re

import pytest
from PyQt6.QtWidgets import QApplication, QGroupBox, QLineEdit, QPushButton

from scripts.settings import SCHEMA
from scripts.gui.settings_dialog import CREDENTIAL_KEYS, TABS, SettingsDialog

# Keys that are state rather than settings: window geometry, column widths, the toolbar
# layout and the recent-folder list are all written by the UI as you use it.
NOT_ON_A_TAB = {'AO_UI_HIDDEN_COLUMNS', 'AO_UI_WINDOW', 'AO_UI_COLUMN_WIDTHS',
                'AO_UI_COPY_RECENT_LIST', 'AO_TOOLBAR', 'AO_PROVIDER'}

# Built by the Providers tab rather than listed in TABS.
PROVIDER_TAB_KEYS = {'AO_TEMPERATURE', 'AO_MAX_TOKENS', 'AO_TIMEOUT', 'AO_MAX_RETRIES',
                     'AO_GOOGLE_BOOKS_KEY', 'AO_SEARCH_BRAVE_KEY'}


@pytest.fixture
def dialog(qt_app, settings):
    """A settings page, destroyed deterministically at the end of the test.

    close() alone leaves the dialog alive and its C++ half owned by nothing in
    particular, so Python frees it whenever the garbage collector next runs - which
    may be in the middle of a later test, while Qt still holds pointers into it. That
    is a segfault that shows up as an unrelated test "crashing", and it gets more
    likely the more dialogs a session builds. deleteLater plus a drained event queue
    makes the teardown happen here, on purpose.
    """
    page = SettingsDialog(settings)
    yield page
    page.close()
    page.deleteLater()
    qt_app.processEvents()


def test_every_setting_is_editable_somewhere(dialog):
    listed = {key for keys in TABS.values() for key in keys}
    for key in SCHEMA:
        if key in NOT_ON_A_TAB:
            continue
        assert key in listed or key in dialog.widgets, \
            f'{key} is in SCHEMA but on no tab - it can only be set by editing .env'


def test_the_provider_tab_gathers_every_credential(dialog):
    """One place to look for anything that authenticates."""
    titles = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    assert 'Providers' in titles, titles
    assert 'LLM Provider' not in titles

    tab = dialog.tabs.widget(titles.index('Providers'))
    groups = [g.title() for g in tab.findChildren(QGroupBox)]
    assert groups[0] == 'Credentials', groups   # first group on the tab
    assert 'Connection' in groups, groups

    for key in CREDENTIAL_KEYS:
        widget = dialog.widgets[key]
        assert tab.isAncestorOf(widget), f'{key} is not on the Providers tab'


def test_the_google_books_key_round_trips(dialog):
    edit = dialog.widgets['AO_GOOGLE_BOOKS_KEY'].findChild(QLineEdit, 'value')
    edit.setText('AIza-example')
    assert dialog._collect()['AO_GOOGLE_BOOKS_KEY'] == 'AIza-example'


def test_the_key_reaches_the_book_api_client(settings):
    """Saving the key is pointless if the client never reads it."""
    from scripts.resolver import Resolver

    settings.set('AO_GOOGLE_BOOKS_KEY', 'AIza-example')
    assert Resolver(settings).api.google_key == 'AIza-example'


def test_secret_fields_are_masked(dialog):
    """A key is a password. A settings page gets opened over a shared screen."""
    for key in CREDENTIAL_KEYS:
        edit = dialog.widgets[key].findChild(QLineEdit, 'value')
        assert edit.echoMode() == QLineEdit.EchoMode.Password, key


def test_a_secret_can_be_revealed_and_re_hidden(dialog):
    """Masking is a default, not a wall - you have to be able to check a paste."""
    widget = dialog.widgets['AO_SEARCH_BRAVE_KEY']
    edit = widget.findChild(QLineEdit, 'value')
    reveal = widget.findChild(QPushButton, 'reveal')

    reveal.setChecked(True)
    assert edit.echoMode() == QLineEdit.EchoMode.Normal
    assert reveal.text() == 'Hide'

    reveal.setChecked(False)
    assert edit.echoMode() == QLineEdit.EchoMode.Password
    assert reveal.text() == 'Show'


def test_hint_links_are_legible_on_the_dark_theme(dialog):
    """Qt's default link colour is a near-black blue - invisible on this palette."""
    from scripts.gui.theme import LINK

    for key in CREDENTIAL_KEYS:
        html = dialog._secret_status(key).text()
        assert '<a ' in html, f'{key} lost its link'
        for anchor in re.findall(r'<a\b[^>]*>', html):
            assert LINK.lower() in anchor.lower(), anchor


def test_the_books_api_enable_page_is_linked(dialog):
    """The one link that matters: a key without that API enabled returns 429."""
    html = dialog._secret_status('AO_GOOGLE_BOOKS_KEY').text()
    assert 'console.cloud.google.com/apis/library/books.googleapis.com' in html


def test_secret_field_round_trips_its_value(dialog, settings):
    """'secret' is a new widget kind, so load, collect and reset all had to learn it."""
    edit = dialog.widgets['AO_SEARCH_BRAVE_KEY'].findChild(QLineEdit, 'value')
    assert edit is not None, 'the composite must expose a QLineEdit named "value"'

    edit.setText('BSA-test-key')
    assert dialog._collect()['AO_SEARCH_BRAVE_KEY'] == 'BSA-test-key'

    settings.set('AO_SEARCH_BRAVE_KEY', 'BSA-from-disk')
    dialog._load()
    assert edit.text() == 'BSA-from-disk'


def test_testing_an_empty_secret_says_so_instead_of_calling_out(dialog):
    """No key means no request - and the reason has to be visible, not silent."""
    dialog.widgets['AO_SEARCH_BRAVE_KEY'].findChild(QLineEdit, 'value').setText('')
    dialog._test_secret('AO_SEARCH_BRAVE_KEY')
    label = dialog._secret_status('AO_SEARCH_BRAVE_KEY')
    assert 'Enter a key first' in label.text()
    assert label.isVisible() or label.text()


def test_a_failing_probe_reports_rather_than_raises(dialog, monkeypatch):
    """A broken probe must not take the settings page down with it."""
    monkeypatch.setattr(type(dialog), '_probe_secret',
                        lambda self, key, typed: (_ for _ in ()).throw(
                            RuntimeError('network on fire')))
    dialog.widgets['AO_SEARCH_BRAVE_KEY'].findChild(QLineEdit, 'value').setText('x')
    dialog._test_secret('AO_SEARCH_BRAVE_KEY')
    assert 'network on fire' in dialog._secret_status('AO_SEARCH_BRAVE_KEY').text()


def test_opening_the_page_flashes_no_stray_windows(qt_app, settings):
    """Opening Settings must not pop little empty frames open and shut again.

    Showing a widget before a layout has adopted it promotes it to a top-level
    window: it appears as a stray frame and vanishes when addWidget reparents it.
    The cure is ordering - addWidget first, then setVisible - which is invisible in
    review, so it is pinned here.

    Watched with an event filter rather than by patching QWidget.setVisible: patching
    that method for every widget in the process crashes Qt outright.
    """
    from PyQt6.QtCore import QEvent, QObject
    from PyQt6.QtWidgets import QWidget

    shown: list = []

    class Watch(QObject):
        def eventFilter(self, obj, event):
            if (event.type() == QEvent.Type.Show
                    and isinstance(obj, QWidget) and obj.isWindow()):
                shown.append(type(obj).__name__)
            return False

    watch = Watch()
    qt_app.installEventFilter(watch)
    try:
        page = SettingsDialog(settings)
        qt_app.processEvents()
    finally:
        qt_app.removeEventFilter(watch)
    page.close()
    page.deleteLater()
    qt_app.processEvents()

    strays = [name for name in shown if name != 'SettingsDialog']
    assert not strays, f'stray top-level windows appeared while building: {strays}'
