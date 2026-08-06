"""Settings page - the UI for everything stored in ``.env``.

Every control is bound to a key from :data:`scripts.settings.SCHEMA`, so adding a
setting there makes it appear here automatically on the relevant tab.

Numbers are typed into ordinary text fields with a validator. There are no spin
boxes anywhere in this application: their arrows are a 12-pixel target for a value
you already know, and they invite click-click-click instead of typing.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Optional

from PyQt6.QtCore import Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from ..paths import PROJECT_ROOT
from ..settings import SCHEMA, Settings
from ..workers import FunctionWorker
from .icons import icon as make_icon
from .theme import ACCENT, LINK, STATUS_TEXT, TEXT, TEXT_DIM
from .toolbar import (DEFAULT_LAYOUT, ITEMS_BY_KEY, SEPARATOR, TOOL_ITEMS,
                      format_layout, parse_layout)

logger = logging.getLogger(__name__)

SECRET_PLACEHOLDERS: Dict[str, str] = {
    'AO_SEARCH_BRAVE_KEY': 'BSA... - leave empty to search Goodreads only',
    'AO_GOOGLE_BOOKS_KEY': 'AIza... - leave empty to skip Google Books',
}

# Service keys, in the order they appear in the Credentials group. Not derived from
# SCHEMA's 'secret' kind: the order is a judgement, and the LLM provider's own key is
# deliberately not here - it belongs to the selected provider, not to the app.
CREDENTIAL_KEYS = ('AO_SEARCH_BRAVE_KEY', 'AO_GOOGLE_BOOKS_KEY')

# Where to get the key, as links. Each names the page it opens and the button to
# press on it, because "enable the Books API" is the name of the task, not the name
# of anything on screen - the button on that page just says Enable.
SECRET_HINTS: Dict[str, str] = {
    'AO_SEARCH_BRAVE_KEY':
        'Free, 2,000 searches a month: sign up at '
        '<a href="https://brave.com/search/api">brave.com/search/api</a>, subscribe '
        'to the Free "Data for Search" plan, then copy the key from its dashboard.',
    'AO_GOOGLE_BOOKS_KEY':
        'Free, two steps. 1: open '
        '<a href="https://console.cloud.google.com/apis/library/'
        'books.googleapis.com">the Books API page</a> and press <b>Enable</b> '
        '(pick or create a project if it asks). 2: open '
        '<a href="https://console.cloud.google.com/apis/credentials">Credentials</a>, '
        'press <b>Create credentials</b> and choose <b>API key</b>. Leave '
        'Application restrictions on None - a desktop app sends no referrer.',
}

# Which schema keys live on which tab, in display order.
#
# Order matters: the tabs you open this page *for* come first. "General" is two folder
# pickers and a log level - things you set once, on the first run, and never look at
# again - so it does not get the first tab. Nor does the provider form. Both sit at
# the right-hand end, next to the other set-and-forget page.
TABS: Dict[str, list] = {
    'Identification': [
        'AO_ENABLE_METADATA', 'AO_ENABLE_REGEX', 'AO_ENABLE_API', 'AO_ENABLE_SEARCH',
        # Credentials are not here. Every API key lives on the Providers tab, in one
        # Credentials group at the top of it - one place to look for anything that
        # authenticates. See _build_credentials_box.
        'AO_ENABLE_LLM', 'AO_API_SOURCES', 'AO_CONFIDENCE_SCORE',
        'AO_ALWAYS_SEARCH_TO_TIER', 'AO_REQUIRE_COVER', 'AO_FOLDER_REASONING',
        'AO_AUTO_APPROVE_THRESHOLD', 'AO_DETECT_DUPLICATES', 'AO_WARN_DIRTY_OUTPUT',
    ],
    'Output': [
        'AO_COPY_MODE', 'AO_OUTPUT_TEMPLATE', 'AO_RENAME_FILES',
        'AO_FILE_TEMPLATE', 'AO_RENAME_SUPPORT_FILES', 'AO_ILLEGAL_CHARS',
        'AO_COLLISION_POLICY', 'AO_WRITE_TAGS', 'AO_WRITE_SIDECAR',
    ],
    # Everything the "Merge chapters into one .m4b" modal asks for also lives here.
    # A setting that exists only inside one modal is a setting you can only find by
    # opening that modal, and only change while you are committing to a merge.
    'Merging': [
        'AO_MERGE_TEMPLATE', 'AO_MERGE_BITRATE', 'AO_MERGE_IN_PLACE',
        'AO_MERGE_DELETE_ORIGINALS', 'AO_MERGE_REPLACE_ENTRY', 'AO_MERGE_OVERWRITE',
        'AO_FFMPEG_PATH',
    ],
    'Cache': ['AO_CACHE_DB', 'AO_CACHE_MISS_TTL', 'AO_RESUME_SCANS'],
    'Interface': [
        'AO_UI_DENSITY', 'AO_UI_ICON_SIZE', 'AO_UI_TOOLBAR_LABELS',
        'AO_UI_SHOW_COVERS', 'AO_UI_STATUS_STRIPE', 'AO_UI_CONFIDENCE_COLOR',
        'AO_UI_COLOR_BY_SOURCE', 'AO_UI_ROW_TINT', 'AO_UI_SHOW_FILTERS',
        'AO_UI_SHOW_PANEL', 'AO_UI_COPY_RECENTS', 'AO_UI_RESORT_LIVE',
        'AO_UI_ADVANCE_AFTER_DECISION', 'AO_UI_CONFIRM_APPLY',
        'AO_UI_REMEMBER_LAYOUT',
    ],
    'General': ['AO_INPUT_DIR', 'AO_OUTPUT_DIR', 'AO_LOG_LEVEL', 'AO_THREADS'],
}

# Settings that genuinely cannot take effect until the program is restarted, because
# something is built from them once at start-up and then owned by a long-lived object:
# the thread pool is sized when it is created, and the cache file is opened when it is
# opened. Everything else - every Interface setting included - applies immediately.
#
# This list is deliberately as short as it can be made rather than a hedge. A setting
# on it is marked with a * and explained at the foot of its tab; if you find yourself
# adding to it, fix the setting instead.
RESTART_KEYS = {'AO_THREADS', 'AO_CACHE_DB'}


class SettingsDialog(QDialog):
    """Edits ``.env`` in place.

    Nothing is *written* until Save is pressed, but Interface settings take effect in
    the live window as you change them - a preview you can actually see. Save leaves
    the page open; Close closes it, and says "Cancel" while there is anything unsaved.
    """

    # Emitted whenever the settings have been written to disk.
    saved = pyqtSignal()
    # Emitted by the "Reset window size..." button, for the window to act on. The
    # saved keys have already been cleared and written when this fires.
    layout_reset = pyqtSignal()

    def __init__(self, settings: Settings, parent=None, live_preview=None):
        super().__init__(parent)
        self.settings = settings
        # Called after an Interface setting changes, so the window can restyle itself
        # before the change is written to disk.
        self.live_preview = live_preview
        self._dirty = False
        self._loading = False
        self._examples: Dict[str, QLabel] = {}
        self.widgets: Dict[str, QWidget] = {}
        self.provider_widgets: Dict[str, QLineEdit] = {}
        self._models: list = []
        self._fetching = False
        self._worker = None

        self.setWindowTitle('Settings')
        # 15% off again, from 1003 to 853. The tab strip is what forced the original
        # 1180 - eight tabs that did not fit were hidden behind scroll arrows, so the
        # page looked like it had four. That is solved on the tab bar itself now (see
        # _build), not by making the whole dialog wide enough to never need it.
        self.setMinimumSize(766, 700)
        self.resize(853, 780)
        self._build()
        self._load()

    # ------------------------------------------------------------------ build

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        header = QLabel('Settings are stored in '
                        f'<code>{self.settings.display_path(self.settings.env_path)}'
                        '</code>')
        header.setStyleSheet(f'color: {TEXT_DIM};')
        layout.addWidget(header)

        self.tabs = QTabWidget()
        # Every tab stays visible in a narrower window. Without this Qt keeps the tabs
        # at their full width and hides the overflow behind scroll arrows, which is how
        # a settings page ends up looking like it has four tabs instead of eight; with
        # it, the labels elide instead and all of them can still be clicked. The
        # minimum is what stops a squeezed tab collapsing to a bare ellipsis.
        bar = self.tabs.tabBar()
        bar.setUsesScrollButtons(False)
        bar.setElideMode(Qt.TextElideMode.ElideRight)
        bar.setExpanding(False)
        self.tabs.setUsesScrollButtons(False)
        layout.addWidget(self.tabs, stretch=1)

        for title, keys in TABS.items():
            # The Interface tab implies "UI" on every one of its rows, so the prefix
            # is stripped from the labels there rather than repeated eleven times.
            tab = self._build_schema_tab(
                keys, drop='ui' if title == 'Interface' else '')
            # Toolbar belongs with Interface - it is the same decision - so it is
            # slipped in directly after it, before General and the provider form.
            if title == 'General':
                self.tabs.addTab(self._build_toolbar_tab(), 'Toolbar')
            self.tabs.addTab(tab, title)
        # "Providers", not "LLM Provider": this page is where every outside service is
        # configured, and the book databases have credentials of their own.
        self.tabs.addTab(self._build_provider_tab(), 'Providers')

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Close
            | QDialogButtonBox.StandardButton.RestoreDefaults)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.save_button.setProperty('accent', True)
        self.save_button.setToolTip('Write these settings to .env. The page stays open.')
        self.close_button.setToolTip('Close the settings page')
        # Save keeps the page open - changing five things should not mean opening
        # this dialog five times. Close is the only button that closes it.
        self.save_button.clicked.connect(self._save)
        self.close_button.clicked.connect(self._close_requested)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults
                       ).clicked.connect(self._restore_defaults)
        layout.addWidget(buttons)
        self._watch_for_changes()
        self._update_buttons()

    def show_tab(self, title: str) -> None:
        """Open on a named tab - "Customise toolbar" should land on Toolbar."""
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index).lower() == title.lower():
                self.tabs.setCurrentIndex(index)
                return

    def _build_schema_tab(self, keys, drop: str = '') -> QWidget:
        container = QWidget()
        form = QFormLayout(container)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        needs_restart = False
        for key in keys:
            if key not in SCHEMA:
                continue
            default, kind, help_text = SCHEMA[key]
            widget = self._make_widget(key, kind, default)
            self.widgets[key] = widget

            # A setting that cannot take effect until a restart says so where you
            # change it, not in a dialog after the fact.
            marked = key in RESTART_KEYS
            needs_restart = needs_restart or marked
            if marked:
                help_text = (help_text + '  * Takes effect the next time the '
                                         'program starts.')
            label = QLabel(_pretty(key, drop) + (' *' if marked else ''))
            label.setToolTip(help_text)
            widget.setToolTip(help_text)

            hint = QLabel(help_text)
            hint.setWordWrap(True)
            hint.setStyleSheet(f'color: {TEXT_DIM}; font-size: 11px;')

            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)

            if key in ('AO_OUTPUT_TEMPLATE', 'AO_FILE_TEMPLATE'):
                # A template is unreadable until you see what it produces, so the
                # example sits directly under the field and updates as you type.
                cell_layout.addWidget(widget)
                example = QLabel()
                example.setWordWrap(True)
                example.setStyleSheet(
                    f'color: {ACCENT}; font-family: Consolas, monospace; '
                    f'font-size: 11px; padding: 2px 0;')
                self._examples[key] = example
                widget.textChanged.connect(lambda _='', k=key: self._show_example(k))
                cell_layout.addWidget(example)
            else:
                cell_layout.addWidget(widget)

            cell_layout.addWidget(hint)
            form.addRow(label, cell)

            # "Remember layout" is the setting the reset belongs to: it is what saved
            # the sizes in the first place, so it is where you look when they are
            # wrong. A button rather than a checkbox, because it is an action.
            if key == 'AO_UI_REMEMBER_LAYOUT':
                reset = QPushButton('Reset window size, panel split and columns')
                reset.setToolTip(
                    'Forget the saved window size, the width of the explanation panel, '
                    'the column widths and which columns are hidden. Everything goes '
                    'back to the built-in layout straight away - nothing else is '
                    'touched, and you do not have to restart.')
                reset.clicked.connect(self._reset_layout)
                form.addRow('', reset)

        # Only tabs that actually have a starred row say anything. A tab with nothing
        # to warn about says nothing: "every setting here applies immediately" is the
        # program telling you about itself, which is not information, it is noise.
        if needs_restart:
            footnote = QLabel(
                '<b>*</b>  Marked settings are read once when the program starts, so '
                'changing them here has no effect until you restart it.')
            footnote.setWordWrap(True)
            footnote.setStyleSheet(
                f'color: {ACCENT}; font-size: 11px; padding-top: 12px;')
            form.addRow('', footnote)

        scroll = QScrollArea()
        scroll.setWidget(container)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        return scroll

    def _make_widget(self, key: str, kind: str, default: str) -> QWidget:
        if key == 'AO_API_SOURCES':
            return self._make_sources_widget()

        if kind == 'bool':
            widget = QCheckBox()
            return widget

        if kind == 'percent':
            widget = QWidget()
            row = QHBoxLayout(widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            edit = QLineEdit()
            edit.setObjectName('value')
            edit.setValidator(QIntValidator(0, 100, edit))
            edit.setMaximumWidth(90)
            row.addWidget(edit)
            suffix = QLabel('%')
            suffix.setStyleSheet(f'color: {TEXT_DIM};')
            row.addWidget(suffix)
            row.addStretch(1)
            return widget

        if kind == 'int':
            widget = QLineEdit()
            widget.setValidator(QIntValidator(0, 10_000_000, widget))
            widget.setMaximumWidth(140)
            return widget

        if kind == 'float':
            validator = QDoubleValidator(0.0, 1_000_000.0, 4)
            validator.setNotation(QDoubleValidator.Notation.StandardNotation)
            widget = QLineEdit()
            widget.setValidator(validator)
            widget.setMaximumWidth(140)
            return widget

        if kind.startswith('choice:'):
            # The stored value is a lower-case id ("underscore", "openlibrary"); the
            # label is written the way a person writes it. A drop-down reading
            # "smart / dash / underscore" is a list of variable names on show.
            widget = QComboBox()
            for value in kind.split(':', 1)[1].split('|'):
                widget.addItem(_choice_label(value), value)
            return widget

        if kind == 'secret':
            return self._make_secret_widget(key)

        if kind == 'path':
            widget = QWidget()
            row = QHBoxLayout(widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            edit = QLineEdit()
            edit.setObjectName('value')
            # No placeholder and no example path. A path field shows the path, and
            # nothing else - greyed-out example text in an empty field reads as a value
            # that is already set.
            browse = QPushButton('Browse...')
            browse.setFixedWidth(90)
            browse.clicked.connect(lambda _, e=edit, k=key: self._browse(e, k))
            row.addWidget(edit, stretch=1)
            row.addWidget(browse)
            return widget

        return QLineEdit()

    def _make_secret_widget(self, key: str) -> QWidget:
        """An API-key field: the box, a Test button, and a line of feedback.

        Self-contained in one form row so a credential can sit on whichever tab the
        feature it belongs to lives on, rather than being exiled to Providers where
        nobody thinks to look for it.
        """
        widget = QWidget()
        column = QVBoxLayout(widget)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        edit = QLineEdit()
        edit.setObjectName('value')
        edit.setPlaceholderText(SECRET_PLACEHOLDERS.get(key, ''))
        # Masked. A settings page gets opened over a shared screen, and a key sitting
        # in plain text is a key read by whoever is watching.
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        row.addWidget(edit, stretch=1)

        # ...but you still have to be able to check what you pasted, so masking is a
        # default rather than a wall.
        reveal = QPushButton('Show')
        reveal.setObjectName('reveal')
        reveal.setCheckable(True)
        reveal.setFixedWidth(64)
        reveal.setToolTip('Show the key in plain text')
        reveal.toggled.connect(
            lambda shown, e=edit, b=reveal: self._reveal_secret(e, b, shown))
        row.addWidget(reveal)

        test = QPushButton('Test')
        test.setFixedWidth(90)
        test.setToolTip('Make one real request with this key')
        test.clicked.connect(lambda _=False, k=key: self._test_secret(k))
        row.addWidget(test)
        column.addLayout(row)

        status = QLabel(SECRET_HINTS.get(key, ''))
        status.setObjectName('status')
        status.setWordWrap(True)
        status.setOpenExternalLinks(True)
        # `a { color }` in the sheet, not just the palette's Link role: a rich-text
        # QLabel honours this, and it keeps the link legible even if the label is
        # restyled later with a dimmer colour for its own text.
        status.setStyleSheet(
            f'color: {TEXT_DIM}; font-size: 11px;')
        status.setText(_link_coloured(status.text()))
        # Added to the layout *before* the visibility is set, and that order matters:
        # showing a widget that has no parent yet promotes it to a top-level window,
        # so it flashes up as a stray little frame and vanishes again the moment
        # addWidget reparents it. Layout first, then decide whether it shows.
        column.addWidget(status)
        # A key with nothing to say about it should not reserve a blank line.
        status.setVisible(bool(status.text()))
        return widget

    def _make_sources_widget(self) -> QWidget:
        """One checkbox per known book database.

        A comma-separated string was unusable: nothing told you which ids existed, so
        the setting looked like there was only ever one source.
        """
        from ..api_query import AVAILABLE_SOURCES

        widget = QWidget()
        column = QVBoxLayout(widget)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        for key, label, blurb in AVAILABLE_SOURCES:
            box = QCheckBox(f'{label}  -  {blurb}')
            box.setObjectName(f'source_{key}')
            box.toggled.connect(lambda _=False: self._changed('AO_API_SOURCES'))
            column.addWidget(box)
        return widget

    @staticmethod
    def _sources_boxes(widget: QWidget):
        from ..api_query import AVAILABLE_SOURCES

        for key, _, _ in AVAILABLE_SOURCES:
            box = widget.findChild(QCheckBox, f'source_{key}')
            if box is not None:
                yield key, box

    def _build_credentials_box(self) -> QWidget:
        """Every service key, in one group at the top of the Providers tab.

        First on the tab, because a credential is the one setting that cannot be
        defaulted for you - and the only reason to open this page on a fresh install.
        The LLM provider's own key stays in the Connection group below, since it
        belongs to whichever provider is selected rather than to the app.
        """
        box = QGroupBox('Credentials')
        form = QFormLayout(box)
        for key in CREDENTIAL_KEYS:
            default, kind, help_text = SCHEMA[key]
            widget = self._make_widget(key, kind, default)
            widget.setToolTip(help_text)
            self.widgets[key] = widget
            form.addRow(_pretty(key), widget)
        return box

    def _build_provider_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(self._build_credentials_box())

        select_box = QGroupBox('Language model - active provider')
        select_form = QFormLayout(select_box)
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(self.settings.provider_names())
        self.provider_combo.currentTextChanged.connect(self._provider_changed)
        select_form.addRow('Provider', self.provider_combo)
        layout.addWidget(select_box)

        detail_box = QGroupBox('Connection')
        detail_form = QFormLayout(detail_box)
        for field, label, placeholder in (
            ('BASE_URL', 'Base URL', 'https://host:port/v1'),
            ('API_KEY', 'API key', 'sk-...'),
            ('AUTH_STYLE', 'Auth style', 'bearer | x-api-key | none'),
            ('EXTRA_BODY', 'Extra JSON', '{"enable_tools": false}'),
        ):
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            if field == 'API_KEY':
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.provider_widgets[field] = edit
            detail_form.addRow(label, edit)

        # The model is picked from what GET /models advertises, never typed by hand -
        # a typo'd id fails at request time with an opaque provider error.
        self.model_combo = QComboBox()
        self.model_combo.setToolTip('Populated from the provider\'s /models endpoint.')
        # A provider can advertise fifty models; ten visible rows is a peephole.
        self.model_combo.setMaxVisibleItems(18)
        self.model_combo.currentIndexChanged.connect(self._model_selected)
        self.model_refresh = QPushButton('Refresh')
        self.model_refresh.setFixedWidth(90)
        self.model_refresh.clicked.connect(lambda: self._fetch_models(announce=True))

        model_row = QWidget()
        model_layout = QHBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.setSpacing(6)
        model_layout.addWidget(self.model_combo, stretch=1)
        model_layout.addWidget(self.model_refresh)
        detail_form.addRow('Model', model_row)

        self.model_hint = QLabel('')
        self.model_hint.setWordWrap(True)
        self.model_hint.setStyleSheet(f'color: {TEXT_DIM}; font-size: 11px;')
        detail_form.addRow('', self.model_hint)
        layout.addWidget(detail_box)

        tuning_box = QGroupBox('Generation')
        tuning_form = QFormLayout(tuning_box)
        for key in ('AO_TEMPERATURE', 'AO_MAX_TOKENS', 'AO_TIMEOUT', 'AO_MAX_RETRIES'):
            default, kind, help_text = SCHEMA[key]
            widget = self._make_widget(key, kind, default)
            widget.setToolTip(help_text)
            self.widgets[key] = widget
            tuning_form.addRow(_pretty(key), widget)
        layout.addWidget(tuning_box)

        actions = QHBoxLayout()
        test_button = QPushButton('Test connection')
        test_button.clicked.connect(self._test_connection)
        actions.addWidget(test_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.provider_status = QLabel('')
        self.provider_status.setWordWrap(True)
        self.provider_status.setStyleSheet(f'color: {TEXT_DIM};')
        layout.addWidget(self.provider_status)

        layout.addStretch(1)

        # Scrolled, exactly like every schema tab. Unwrapped, this page's 737px of
        # content became the whole dialog's minimum height - and when the window ends
        # up shorter than that anyway (short screen, display scaling, a restored
        # geometry) the layout has nowhere to take the space from except the fields
        # themselves, which collapse to slivers a few pixels tall. A scroll area gives
        # the page somewhere to overflow to.
        scroll = QScrollArea()
        scroll.setWidget(container)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        return scroll

    @staticmethod
    def _reveal_secret(edit: QLineEdit, button: QPushButton, shown: bool) -> None:
        edit.setEchoMode(QLineEdit.EchoMode.Normal if shown
                         else QLineEdit.EchoMode.Password)
        button.setText('Hide' if shown else 'Show')

    def _secret_status(self, key: str) -> Optional[QLabel]:
        """The feedback label inside a 'secret' field's composite widget."""
        widget = self.widgets.get(key)
        return widget.findChild(QLabel, 'status') if widget is not None else None

    def _say(self, key: str, message: str, mood: str = '') -> None:
        colour = STATUS_TEXT[mood] if mood else TEXT_DIM
        if (label := self._secret_status(key)) is not None:
            label.setText(message)
            label.setStyleSheet(f'color: {colour}; font-size: 11px;')
            # It may have been built hidden (no hint text). A label hidden on the
            # widget stays hidden until something shows it again, so a Test result
            # would otherwise be written into an invisible label.
            label.setVisible(bool(message))

    def _test_secret(self, key: str) -> None:
        """Spend one real request proving a key works, here rather than mid-scan."""
        widget = self.widgets.get(key)
        edit = widget.findChild(QLineEdit, 'value') if widget is not None else None
        typed = edit.text().strip() if edit is not None else ''
        if not typed:
            self._say(key, 'Enter a key first - there is nothing to test.', 'risky')
            return

        self._say(key, 'Testing...')
        QApplication.processEvents()
        try:
            ok, message = self._probe_secret(key, typed)
        except Exception as exc:  # a broken probe must not take the dialog with it
            self._say(key, f'Could not test it: {exc}', 'rejected')
            return
        self._say(key, message, 'approved' if ok else 'rejected')

    def _probe_secret(self, key: str, typed: str) -> tuple:
        """Make the one request that proves ``key``, and describe what came back."""
        if key == 'AO_SEARCH_BRAVE_KEY':
            from ..web_search import WebSearchClient

            client = WebSearchClient(cache=None, timeout=15)
            client.brave_key = typed
            try:
                rows = client._brave('Dune Frank Herbert goodreads series')
            except Exception as exc:
                return False, f'Brave refused: {exc}'
            if rows:
                return True, (f'Works - Brave answered with {len(rows)} result(s) '
                              f'for a test search.')
            return False, ('The key was accepted but the search matched nothing, '
                           'which is odd for Dune - see the log.')

        if key == 'AO_GOOGLE_BOOKS_KEY':
            from ..api_query import BookAPIClient

            client = BookAPIClient(cache=None, sources=['googlebooks'], timeout=15,
                                   google_key=typed)
            rows = client._search_googlebooks({'title': 'Dune',
                                               'author': 'Frank Herbert'})
            if rows:
                return True, (f'Works - Google Books answered with {len(rows)} '
                              f'row(s) for a test lookup of Dune.')
            if error := client.last_errors.get('googlebooks'):
                # "Set a key in Settings" is right advice elsewhere. Here you are in
                # Settings, looking at the field.
                return False, error.replace(
                    ' Set a Google Books API key in Settings to use this source.',
                    ' Paste one into the field above.')
            return False, ('The request went through but matched nothing, which is '
                           'odd for Dune - see the log.')

        return False, f'No test is wired up for {key}.'

    def _show_example(self, key: str) -> None:
        """Render the template being typed against a stand-in book."""
        from ..paths import render_template

        label = self._examples.get(key)
        if label is None:
            return
        widget = self.widgets.get(key)
        template = widget.text() if isinstance(widget, QLineEdit) else ''
        values = {'author': 'Brandon Sanderson', 'series': 'Mistborn',
                  'series_index': 2, 'title': 'The Well of Ascension',
                  'file_index': '03', 'extension': 'mp3'}
        try:
            rendered = render_template(template, values)
        except Exception as exc:            # a half-typed template is not an error
            label.setText(f'...  ({exc})')
            return
        if key == 'AO_FILE_TEMPLATE' and not rendered.lower().endswith('.mp3'):
            rendered += '.mp3'
        label.setText('→  ' + (rendered + ('/' if key == 'AO_OUTPUT_TEMPLATE' else '')))

    # -------------------------------------------------------- live behaviour

    def _watch_for_changes(self) -> None:
        """Track edits so the buttons can tell you whether anything is pending.

        Interface settings also apply as you change them: you are choosing how the
        window should look, and you cannot judge that from a combo box - you judge it
        by looking at the window. They are still only *written* when you press Save.
        """
        for key, widget in self.widgets.items():
            live = key.startswith('AO_UI_')
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(lambda _=False, k=key: self._changed(k))
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(
                    lambda _=0, k=key: self._changed(k))
            elif isinstance(widget, QLineEdit):
                widget.textChanged.connect(lambda _='', k=key: self._changed(k))
            else:
                edit = widget.findChild(QLineEdit, 'value')
                if edit is not None:
                    edit.textChanged.connect(lambda _='', k=key: self._changed(k))
        self.toolbar_list.model().rowsMoved.connect(lambda *_: self._changed(''))
        self.toolbar_list.itemChanged.connect(lambda *_: self._changed(''))

    def _changed(self, key: str) -> None:
        if getattr(self, '_loading', False):
            return
        self._dirty = True
        self._update_buttons()
        if key.startswith('AO_UI_') and self.live_preview is not None:
            # Push the value into the running window without saving it.
            self.settings.update(self._collect())
            self.live_preview()

    def _update_buttons(self) -> None:
        dirty = getattr(self, '_dirty', False)
        self.save_button.setEnabled(dirty)
        self.close_button.setText('Close' if not dirty else 'Cancel')
        self.close_button.setToolTip(
            'Close the settings page' if not dirty
            else 'Discard the unsaved changes and close')

    def _close_requested(self) -> None:
        if getattr(self, '_dirty', False):
            if QMessageBox.question(
                    self, 'Discard changes',
                    'Close without saving the changes you made?'
            ) != QMessageBox.StandardButton.Yes:
                return
            # Interface settings were previewed live, so put the saved ones back.
            self.settings.reload()
            if self.live_preview is not None:
                self.live_preview()
        self.reject()

    # ---------------------------------------------------------- toolbar tab

    def _build_toolbar_tab(self) -> QWidget:
        """One ordered list: ticked means visible, and the order is the order."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        hint = QLabel('Tick a button to show it on the toolbar, and use the arrows to '
                      'reorder. Separators draw a divider between groups.')
        hint.setWordWrap(True)
        hint.setStyleSheet(f'color: {TEXT_DIM};')
        layout.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(10)

        self.toolbar_list = QListWidget()
        self.toolbar_list.setToolTip(
            'The toolbar, top to bottom = left to right.\n'
            'Untick a button to hide it; it stays in the list so you can bring it back.')
        row.addWidget(self.toolbar_list, stretch=1)

        buttons = QVBoxLayout()
        buttons.setSpacing(6)
        for label, tooltip, slot in (
            ('Move up', 'Move the selected button one place to the left',
             lambda: self._move_tool(-1)),
            ('Move down', 'Move the selected button one place to the right',
             lambda: self._move_tool(1)),
            ('Add separator', 'Insert a divider above the selected button',
             self._add_separator),
            ('Remove separator', 'Delete the selected divider',
             self._remove_separator),
            ('Reset', 'Restore the default toolbar layout',
             lambda: self._load_toolbar(DEFAULT_LAYOUT)),
        ):
            button = QPushButton(label)
            button.setToolTip(tooltip)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        row.addLayout(buttons)

        layout.addLayout(row, stretch=1)
        return container

    def _load_toolbar(self, layout: str) -> None:
        """Visible items first, in order, then everything currently hidden."""
        self.toolbar_list.clear()
        keys = parse_layout(layout)
        for key in keys:
            self._add_tool_row(key, checked=True)
        for item in TOOL_ITEMS:
            if item.key not in keys:
                self._add_tool_row(item.key, checked=False)

    def _add_tool_row(self, key: str, checked: bool, at: int = -1) -> QListWidgetItem:
        if key == SEPARATOR:
            item = QListWidgetItem('──────  separator  ──────')
            item.setToolTip('A divider between groups of buttons')
        else:
            spec = ITEMS_BY_KEY[key]
            item = QListWidgetItem(make_icon(spec.key, TEXT, 20), spec.label)
            item.setToolTip(spec.tooltip)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if checked
                               else Qt.CheckState.Unchecked)
        item.setData(Qt.ItemDataRole.UserRole, key)
        if at < 0:
            self.toolbar_list.addItem(item)
        else:
            self.toolbar_list.insertItem(at, item)
        return item

    def _toolbar_layout(self) -> list:
        """Read the list back out - ticked buttons and separators, in list order."""
        keys = []
        for index in range(self.toolbar_list.count()):
            item = self.toolbar_list.item(index)
            key = item.data(Qt.ItemDataRole.UserRole)
            if key == SEPARATOR or item.checkState() == Qt.CheckState.Checked:
                keys.append(key)
        return parse_layout(','.join(keys))

    def _move_tool(self, delta: int) -> None:
        row = self.toolbar_list.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.toolbar_list.count():
            return
        item = self.toolbar_list.takeItem(row)
        self.toolbar_list.insertItem(target, item)
        self.toolbar_list.setCurrentRow(target)

    def _add_separator(self) -> None:
        row = max(0, self.toolbar_list.currentRow())
        self._add_tool_row(SEPARATOR, checked=True, at=row)
        self.toolbar_list.setCurrentRow(row)

    def _remove_separator(self) -> None:
        row = self.toolbar_list.currentRow()
        if row < 0:
            return
        if self.toolbar_list.item(row).data(Qt.ItemDataRole.UserRole) != SEPARATOR:
            # Buttons are hidden by unticking them, never deleted - otherwise a
            # hidden button has no way back onto the list.
            QMessageBox.information(
                self, 'Not a separator',
                'Only separators can be removed. Untick a button to hide it.')
            return
        self.toolbar_list.takeItem(row)

    # ------------------------------------------------------------- load/save

    def _load(self) -> None:
        self._loading = True
        try:
            self._load_values()
        finally:
            self._loading = False
        self._dirty = False
        self._update_buttons()

    def _load_values(self) -> None:
        for key, widget in self.widgets.items():
            _, kind, _ = SCHEMA[key]
            value = self.settings.get(key)
            if key == 'AO_API_SOURCES':
                enabled = {s.strip().lower() for s in value.split(',') if s.strip()}
                for source, box in self._sources_boxes(widget):
                    box.setChecked(source in enabled)
            elif kind == 'bool':
                widget.setChecked(self.settings.get_bool(key))
            elif kind == 'int':
                widget.setText(str(self.settings.get_int(key)))
            elif kind == 'float':
                widget.setText(str(round(self.settings.get_float(key), 4)))
            elif kind.startswith('choice:'):
                widget.setCurrentIndex(max(0, widget.findData(value)))
            elif kind == 'percent':
                widget.findChild(QLineEdit, 'value').setText(
                    str(int(round(self.settings.get_float(key) * 100))))
            elif kind in ('path', 'secret'):
                widget.findChild(QLineEdit, 'value').setText(value)
            else:
                widget.setText(value)

        self._load_toolbar(self.settings.get('AO_TOOLBAR'))

        for key in self._examples:
            self._show_example(key)

        active = self.settings.get('AO_PROVIDER')
        index = self.provider_combo.findText(active)
        if index >= 0:
            self.provider_combo.setCurrentIndex(index)
        self._provider_changed(self.provider_combo.currentText())

    def _provider_changed(self, name: str) -> None:
        if not name:
            return
        fields = self.settings.provider(name)
        for field, widget in self.provider_widgets.items():
            widget.setText(fields.get(field, ''))
        self.provider_status.setText('')
        self._set_model_items([], selected=fields.get('MODEL', ''))
        self._fetch_models(announce=False)

    # ------------------------------------------------------------ model picker

    def _provider_values(self) -> Dict[str, str]:
        """Everything the provider form currently holds, including the model."""
        values = {field: widget.text().strip()
                  for field, widget in self.provider_widgets.items()}
        values['MODEL'] = self._selected_model()
        return values

    def _selected_model(self) -> str:
        return str(self.model_combo.currentData() or '')

    def _set_model_items(self, models: list, selected: str = '') -> None:
        """Fill the combo. Falls back to whatever is saved when the fetch found nothing."""
        selected = selected or self._selected_model()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()

        # Order is display order from the server (see docs/api_sanctum.md) - never sort.
        for model in models:
            label = model['label']
            if model['is_collection']:
                count = model.get('member_count')
                label = (f'{label}  (collection'
                         + (f', {count} models' if count is not None else '') + ')')
            self.model_combo.addItem(label, model['id'])

        ids = [m['id'] for m in models]
        if selected and selected not in ids:
            # Keep a configured id that this server no longer lists, so opening the
            # page offline doesn't silently reassign the provider's model.
            self.model_combo.insertItem(0, f'{selected}  (not listed by the server)',
                                        selected)
        if not self.model_combo.count():
            self.model_combo.addItem('(server default)', '')

        index = self.model_combo.findData(selected)
        self.model_combo.setCurrentIndex(max(0, index))
        self.model_combo.blockSignals(False)
        self._model_selected()

    def _model_selected(self) -> None:
        model = next((m for m in self._models
                      if m['id'] == self._selected_model()), None)
        if model is None:
            self.model_hint.setText('')
            return
        parts = [model['id']]
        if model['description']:
            parts.append(model['description'])
        if model['is_collection'] and model['members']:
            parts.append('Resolves to: ' + ', '.join(model['members'][:4])
                         + ('...' if len(model['members']) > 4 else ''))
        self.model_hint.setText('  -  '.join(parts))

    def _fetch_models(self, announce: bool) -> None:
        """Ask the provider what it serves. Runs off-thread; the dialog stays usable."""
        name = self.provider_combo.currentText()
        if not name or self._fetching:
            return

        scratch = Settings(self.settings.env_path)
        scratch.update(self._collect())
        scratch.set_provider(name, self._provider_values())

        self._fetching = True
        self.model_refresh.setEnabled(False)
        if announce:
            self.provider_status.setText('Fetching models...')

        def work():
            from ..api_engine import APIEngine
            return APIEngine(provider=name, settings=scratch).list_models_detailed()

        # Held on self: QThreadPool owns the C++ runnable, but nothing keeps the Python
        # wrapper (and with it the signals object) alive, so it would be collected
        # before the pool ever ran it.
        self._worker = FunctionWorker(work)
        self._worker.signals.finished.connect(
            lambda models: self._models_arrived(name, models, announce))
        self._worker.signals.error.connect(
            lambda text: self._models_failed(text, announce))
        QThreadPool.globalInstance().start(self._worker)

    def _models_arrived(self, provider: str, models: list, announce: bool) -> None:
        self._fetching = False
        self.model_refresh.setEnabled(True)
        if provider != self.provider_combo.currentText():
            return  # the user moved on while we were waiting

        self._models = models
        self._set_model_items(models)
        if not models:
            self.model_hint.setText(
                'This provider did not return a model list - the saved id is kept.')
            if announce:
                self.provider_status.setText('No models returned by this provider.')
        elif announce:
            self.provider_status.setText(f'{len(models)} models available.')

    def _models_failed(self, text: str, announce: bool) -> None:
        self._fetching = False
        self.model_refresh.setEnabled(True)
        self.model_hint.setText('Could not reach the provider - the saved id is kept.')
        if announce:
            self.provider_status.setText(
                f'<span style="color:#f2777a">Could not list models:</span> '
                f'{text.splitlines()[0]}')

    def _collect(self) -> Dict[str, str]:
        values: Dict[str, str] = {}
        for key, widget in self.widgets.items():
            _, kind, _ = SCHEMA[key]
            if key == 'AO_API_SOURCES':
                chosen = [s for s, box in self._sources_boxes(widget) if box.isChecked()]
                # Every source off would silently disable the tier; fall back instead.
                values[key] = ','.join(chosen) or SCHEMA[key][0]
            elif kind == 'bool':
                values[key] = 'true' if widget.isChecked() else 'false'
            elif kind in ('int', 'float'):
                # An empty or half-typed box falls back to the default rather than
                # writing something the loader would reject on the next start.
                default = SCHEMA[key][0]
                text = widget.text().strip().replace(',', '.')
                try:
                    values[key] = (str(int(float(text))) if kind == 'int'
                                   else str(round(float(text), 4)))
                except ValueError:
                    values[key] = default
            elif kind.startswith('choice:'):
                values[key] = str(widget.currentData() or SCHEMA[key][0])
            elif kind == 'percent':
                text = widget.findChild(QLineEdit, 'value').text().strip()
                try:
                    values[key] = str(round(min(100, max(0, int(text))) / 100.0, 4))
                except ValueError:
                    values[key] = SCHEMA[key][0]
            elif kind in ('path', 'secret'):
                values[key] = widget.findChild(QLineEdit, 'value').text().strip()
            else:
                values[key] = widget.text().strip()

        values['AO_TOOLBAR'] = format_layout(self._toolbar_layout())
        return values

    def _apply_to_settings(self) -> None:
        self.settings.update(self._collect())
        self.settings.set('AO_PROVIDER', self.provider_combo.currentText())
        self.settings.set_provider(self.provider_combo.currentText(),
                                   self._provider_values())

    def _save(self) -> None:
        """Write to .env and stay open."""
        extra = self.provider_widgets['EXTRA_BODY'].text().strip()
        if extra:
            import json
            try:
                parsed = json.loads(extra)
                if not isinstance(parsed, dict):
                    raise ValueError('must be a JSON object')
            except ValueError as exc:
                QMessageBox.warning(self, 'Invalid JSON',
                                    f'The "Extra JSON" field is not a JSON object:\n{exc}')
                return

        self._apply_to_settings()
        try:
            self.settings.save()
        except OSError as exc:
            QMessageBox.critical(self, 'Could not save',
                                 f'Failed to write {self.settings.env_path}:\n{exc}')
            return

        # Deliberately not accept(): saving is not a reason to close the page. You
        # save, look at the result, and keep going.
        self._dirty = False
        self._update_buttons()
        self.saved.emit()

    def _restore_defaults(self) -> None:
        if QMessageBox.question(
                self, 'Restore defaults',
                'Reset every setting on all tabs to its default value?\n'
                'Your API keys are kept.') != QMessageBox.StandardButton.Yes:
            return
        for key, widget in self.widgets.items():
            default, kind, _ = SCHEMA[key]
            if key == 'AO_API_SOURCES':
                enabled = {s.strip() for s in default.split(',')}
                for source, box in self._sources_boxes(widget):
                    box.setChecked(source in enabled)
            elif kind == 'bool':
                widget.setChecked(default.lower() == 'true')
            elif kind in ('int', 'float'):
                widget.setText(default)
            elif kind.startswith('choice:'):
                widget.setCurrentIndex(max(0, widget.findData(default)))
            elif kind == 'percent':
                widget.findChild(QLineEdit, 'value').setText(
                    str(int(round(float(default) * 100))))
            elif kind in ('path', 'secret'):
                widget.findChild(QLineEdit, 'value').setText(default)
            else:
                widget.setText(default)
        self._load_toolbar(DEFAULT_LAYOUT)

    # The saved layout is not on any tab as a field, so it is not covered by Restore
    # Defaults - these keys hold pixel counts written by the window when it closes.
    LAYOUT_KEYS = ('AO_UI_WINDOW', 'AO_UI_COLUMN_WIDTHS', 'AO_UI_HIDDEN_COLUMNS')

    def _reset_layout(self) -> None:
        """Throw away the remembered window size, split and column widths.

        Written to .env immediately rather than staged behind Save: the point of the
        button is to see the window snap back, and a reset you have to remember to
        save is a reset that half-works.
        """
        if QMessageBox.question(
                self, 'Reset layout',
                'Forget the saved window size, panel width, column widths and hidden '
                'columns?\nNo other setting is changed.'
        ) != QMessageBox.StandardButton.Yes:
            return
        for key in self.LAYOUT_KEYS:
            self.settings.set(key, '')
        try:
            self.settings.save()
        except OSError as exc:
            QMessageBox.warning(self, 'Reset layout',
                                f'Could not write the settings file:\n{exc}')
            return
        self.layout_reset.emit()
        if self.live_preview is not None:
            self.live_preview()
        self.saved.emit()

    # --------------------------------------------------------------- actions

    def _browse(self, edit: QLineEdit, key: str) -> None:
        if key == 'AO_CACHE_DB':
            path, _ = QFileDialog.getSaveFileName(self, 'Cache database', edit.text(),
                                                  'SQLite (*.sqlite3 *.db);;All files (*)')
        else:
            path = QFileDialog.getExistingDirectory(self, 'Select folder', edit.text())
        if path:
            # A folder inside the program directory is recorded relative to it. The
            # file dialog only ever hands back an absolute path, and storing that turns
            # "input" into a drive-specific path that then shows up on every screen.
            chosen = Path(path)
            try:
                chosen = chosen.relative_to(PROJECT_ROOT)
            except ValueError:
                pass
            edit.setText(str(chosen))

    def _test_connection(self) -> None:
        """Round-trip a tiny prompt using the values currently in the form."""
        self.provider_status.setText('Testing...')
        self.provider_status.repaint()

        scratch = Settings(self.settings.env_path)
        scratch.update(self._collect())
        name = self.provider_combo.currentText()
        scratch.set_provider(name, self._provider_values())
        scratch.set('AO_PROVIDER', name)

        try:
            from ..api_engine import APIEngine
            reply = APIEngine(provider=name, settings=scratch).test_connection()
            self.provider_status.setText(
                f'<span style="color:{ACCENT}">Connected.</span> Reply: {reply[:120]}')
        except Exception as exc:
            self.provider_status.setText(
                f'<span style="color:#f2777a">Failed:</span> {exc}')


# Words that are abbreviations or proper nouns, and must not be title-cased naively:
# "Ui Accent" and "Ffmpeg Path" both read as typos.
_FIXED_WORDS = {
    'llm': 'LLM', 'api': 'API', 'ttl': 'TTL', 'db': 'Database', 'ui': 'UI',
    'ffmpeg': 'FFmpeg', 'dir': 'Folder', 'opf': 'OPF', 'url': 'URL', 'id': 'ID',
}

# Small words that stay lower-case unless they lead the label.
_SMALL_WORDS = {'a', 'an', 'and', 'as', 'at', 'by', 'for', 'from', 'in', 'of', 'on',
                'or', 'per', 'the', 'to', 'with'}


# A handful of choice ids read badly under plain title-casing, or are worth a few more
# words than the id carries on its own.
_CHOICE_LABELS = {
    'suffix': 'Add a Suffix - "Title (2)"',
    'skip': 'Skip the Book',
    'merge': 'Merge Into the Existing Folder',
    'overwrite': 'Overwrite What Is There',
    'smart': 'Smart Look-Alikes',
    'dash': 'Dash  -',
    'underscore': 'Underscore  _',
    'space': 'Space',
    'remove': 'Remove Them',
    'same': 'Same as the Source Files',
    'compact': 'Compact',
    'normal': 'Normal',
    'comfortable': 'Comfortable',
    'large': 'Large',
}


def _link_coloured(html: str) -> str:
    """Force every anchor to the theme's link colour.

    Qt's built-in default is a dark blue intended for white backgrounds; on this
    palette it renders as near-black on near-black. The palette's Link role covers
    most of it, but a QLabel carrying its own ``color:`` in a stylesheet can still
    win, so the colour goes on the anchors themselves.
    """
    return re.sub(r'<a\s+href=', f'<a style="color: {LINK}" href=', html or '',
                  flags=re.I)


def _choice_label(value: str) -> str:
    """How a stored choice id is written in a drop-down.

    Ids are lower-case because they live in a .env file; showing them raw put a list
    of variable names in front of the user. Acronyms and things that are already
    capitalised (DEBUG, 320k) are left exactly as they are.
    """
    if value in _CHOICE_LABELS:
        return _CHOICE_LABELS[value]
    if value.isupper() or any(character.isdigit() for character in value):
        return value
    return ' '.join(word[:1].upper() + word[1:] for word in value.split('_'))


def _pretty(key: str, drop: str = '') -> str:
    """AO_MAX_TOKENS -> "Max Tokens"; AO_UI_ACCENT -> "Accent" on the Interface tab.

    `drop` removes a leading word that the tab already implies - every setting on the
    Interface page is a UI setting, so repeating "UI" in eleven labels is noise.
    """
    words = key.replace('AO_', '').replace('_', ' ').lower().split()
    if drop and words and words[0] == drop.lower():
        words = words[1:]

    parts = []
    for index, word in enumerate(words):
        if word in _FIXED_WORDS:
            parts.append(_FIXED_WORDS[word])
        elif index > 0 and word in _SMALL_WORDS:
            parts.append(word)
        else:
            parts.append(word[:1].upper() + word[1:])
    return ' '.join(parts)
