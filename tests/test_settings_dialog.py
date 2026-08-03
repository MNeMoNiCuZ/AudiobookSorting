"""The settings page: every schema key must be reachable from some tab.

A setting that exists in SCHEMA but on no tab can only be changed by hand-editing .env,
which is how the Google Books key - the one thing standing between that source and
working at all - came to be unfindable.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QGroupBox, QLineEdit

from scripts.settings import SCHEMA
from scripts.gui.settings_dialog import TABS, SettingsDialog

# Keys that are state rather than settings: window geometry, column widths, the toolbar
# layout and the recent-folder list are all written by the UI as you use it.
NOT_ON_A_TAB = {'AO_UI_HIDDEN_COLUMNS', 'AO_UI_WINDOW', 'AO_UI_COLUMN_WIDTHS',
                'AO_UI_COPY_RECENT_LIST', 'AO_TOOLBAR', 'AO_PROVIDER'}

# Built by the Providers tab rather than listed in TABS.
PROVIDER_TAB_KEYS = {'AO_TEMPERATURE', 'AO_MAX_TOKENS', 'AO_TIMEOUT', 'AO_MAX_RETRIES',
                     'AO_GOOGLE_BOOKS_KEY'}


@pytest.fixture
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(qt_app, settings):
    page = SettingsDialog(settings)
    yield page
    page.close()


def test_every_setting_is_editable_somewhere(dialog):
    listed = {key for keys in TABS.values() for key in keys}
    for key in SCHEMA:
        if key in NOT_ON_A_TAB:
            continue
        assert key in listed or key in dialog.widgets, \
            f'{key} is in SCHEMA but on no tab - it can only be set by editing .env'


def test_the_provider_tab_covers_every_outside_service(dialog):
    titles = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    assert 'Providers' in titles, titles
    assert 'LLM Provider' not in titles

    tab = dialog.tabs.widget(titles.index('Providers'))
    groups = [g.title() for g in tab.findChildren(QGroupBox)]
    assert 'Book databases' in groups, groups


def test_the_google_books_key_is_on_the_providers_tab_and_saves(dialog):
    titles = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    tab = dialog.tabs.widget(titles.index('Providers'))

    widget = dialog.widgets.get('AO_GOOGLE_BOOKS_KEY')
    assert isinstance(widget, QLineEdit)
    assert widget in tab.findChildren(QLineEdit), 'not on the Providers tab'

    widget.setText('AIza-example')
    assert dialog._collect()['AO_GOOGLE_BOOKS_KEY'] == 'AIza-example'


def test_the_key_reaches_the_book_api_client(settings):
    """Saving the key is pointless if the client never reads it."""
    from scripts.resolver import Resolver

    settings.set('AO_GOOGLE_BOOKS_KEY', 'AIza-example')
    assert Resolver(settings).api.google_key == 'AIza-example'
