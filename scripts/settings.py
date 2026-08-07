"""All application configuration, stored in a ``.env`` file at the project root.

The Settings page in the GUI reads and writes this same file, so anything
configurable in the UI is a key here and vice versa.

Values resolve in this order:  process environment  >  .env file  >  built-in default.
Writing only ever touches the .env file, and preserves comments/ordering of keys that
are already present.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import PROJECT_ROOT  # noqa: E402  (the frozen-aware program directory)

ENV_PATH = PROJECT_ROOT / '.env'

logger = logging.getLogger(__name__)

# key -> (default, type, help text shown in the Settings page)
SCHEMA: Dict[str, tuple] = {
    # --- paths
    # Deliberately empty. A default of "input" quietly points a fresh install at a
    # folder inside the program directory, so the first Scan appears to work and finds
    # nothing - and every later run keeps aiming at the wrong place because nobody was
    # ever asked. Empty means unset, the window says so, and it links to this page.
    'AO_INPUT_DIR': ('', 'path', 'Folder containing unsorted audiobooks.'),
    'AO_OUTPUT_DIR': ('', 'path', 'Folder for organised audiobooks.'),

    # --- LLM provider
    'AO_PROVIDER': ('sanctum', 'str', 'Language model service to use.'),
    'AO_TEMPERATURE': ('0.1', 'float', 'Randomness of language model responses.'),
    'AO_MAX_TOKENS': ('2048', 'int', 'Maximum response length from the language model.'),
    'AO_TIMEOUT': ('120', 'int', 'Seconds to wait for a response.'),
    'AO_MAX_RETRIES': ('3', 'int', 'Number of retries after a failed request.'),

    # --- resolution chain
    'AO_ENABLE_METADATA': ('true', 'bool', 'Read metadata embedded in audio files.'),
    'AO_ENABLE_REGEX': ('true', 'bool', 'Read details from file and folder names.'),
    'AO_ENABLE_API': ('true', 'bool', 'Search enabled book databases.'),
    'AO_ENABLE_SEARCH': ('true', 'bool', 'Use web search when more information is needed.'),
    'AO_ENABLE_LLM': ('true', 'bool', 'Use a language model to fill missing details.'),
    'AO_API_SOURCES': ('audnexus,itunes,googlebooks,openlibrary,librivox', 'str',
                       'Book databases used for identification.'),
    'AO_GOOGLE_BOOKS_KEY': ('', 'secret',
                            'API key for Google Books.'),
    'AO_SEARCH_BRAVE_KEY': ('', 'secret',
                            'API key for Brave Search.'),
    'AO_CONFIDENCE_SCORE': ('0.80', 'percent',
                            'Stop identification when confidence reaches this value.'),
    'AO_ALWAYS_SEARCH_TO_TIER': ('3', 'choice:1|2|3|4|5',
                                 'Minimum identification stage to run.'),
    'AO_REQUIRE_COVER': ('false', 'bool',
                         'Keep searching when cover art is missing.'),
    'AO_FOLDER_REASONING': ('true', 'bool',
                            'Identify books in the same folder together.'),

    # --- caching
    'AO_CACHE_DB': ('cache.sqlite3', 'path', 'File used to cache lookup results.'),
    'AO_CACHE_MISS_TTL': ('86400', 'int',
                          'Seconds to remember a failed lookup.'),
    'AO_RESUME_SCANS': ('true', 'bool', 'Reuse previously identified books.'),

    # --- what a load keeps. The Load Input dialog writes these back, so the boxes
    # you tick are the boxes you get next time.
    'AO_LOAD_KEEP_MANUAL': ('true', 'bool',
                            'Keep manually edited values when loading again.'),
    'AO_LOAD_KEEP_CONFIDENT': ('false', 'bool',
                               'Keep confident values when loading again.'),
    'AO_LOAD_KEEP_ABOVE': ('75', 'int',
                           'Minimum confidence for values kept when loading again.'),
    'AO_LOAD_KEEP_DECISIONS': ('false', 'bool',
                               'Keep approval and rejection decisions when loading again.'),

    # --- review
    'AO_REVIEW_APPROVE_THRESHOLD': ('', 'percent',
                                    'Confidence used by middle-click Approve; leave empty to disable.'),
    'AO_REVIEW_REJECT_THRESHOLD': ('', 'percent',
                                   'Confidence used by middle-click Reject; leave empty to disable.'),
    'AO_DETECT_DUPLICATES': ('true', 'bool',
                             'Flag exact duplicate audio.'),

    # --- apply behaviour
    'AO_COPY_MODE': ('true', 'bool',
                     'Copy books to the output folder instead of moving them.'),
    'AO_OUTPUT_TEMPLATE': ('{author}/{series} {series_index} - {title}', 'str',
                           'Folder naming pattern for organised books.'),
    'AO_RENAME_FILES': ('true', 'bool',
                        'Rename audio files using the file naming pattern.'),
    'AO_FILE_TEMPLATE': ('{series} {series_index} - {title} {file_index:03d}', 'str',
                         'Naming pattern for audio files.'),
    'AO_INDEX_PAD': ('2', 'int',
                     'Digits to pad the book number to: 2 makes Book 5 into Book 05, '
                     'and a 1-3 bundle into 01-03.'),
    'AO_RENAME_SUPPORT_FILES': ('true', 'bool',
                                'Rename companion files to match the audio files.'),
    'AO_COLLISION_POLICY': ('suffix', 'choice:suffix|skip|merge|overwrite',
                            'What to do when the destination already exists.'),
    'AO_BLOCKED_WORDS': ('unabridged, audiobook, series, full-cast, 2nd edition', 'str',
                         'Comma-separated words removed from author, series and '
                         'title. Example: series, unabridged, audiobook.'),
    'AO_STRIP_PARENTHESES': ('true', 'bool',
                             'Remove bracketed asides such as (Unabridged) from names.'),
    'AO_TIDY_PUNCTUATION': ('true', 'bool',
                            'Last step: close the seams the removals leave - empty '
                            'brackets, stray commas and dashes.'),
    'AO_ILLEGAL_CHARS': ('smart', 'choice:smart|dash|underscore|space|remove',
                         'How unsupported filename characters are replaced.'),
    'AO_WARN_DIRTY_OUTPUT': ('true', 'bool',
                             'Flag suspicious metadata for review.'),
    'AO_WRITE_TAGS': ('true', 'bool', 'Write corrected metadata into audio files.'),
    'AO_WRITE_SIDECAR': ('false', 'bool', 'Write metadata sidecar files.'),

    # --- interface
    'AO_UI_DENSITY': ('normal', 'choice:compact|normal|comfortable',
                      'Review table row height.'),
    'AO_UI_ICON_SIZE': ('large', 'choice:compact|normal|comfortable|large',
                        'Toolbar button size.'),
    'AO_UI_TOOLBAR_LABELS': ('true', 'bool',
                             'Show each button name underneath its icon.'),
    'AO_UI_COLOR_BY_SOURCE': ('true', 'bool',
                              'Colour fields by their identification source.'),
    'AO_UI_SHOW_COVERS': ('true', 'bool', 'Show cover art in the review table.'),
    'AO_UI_STATUS_STRIPE': ('true', 'bool',
                            'Mark each row with a colour stripe for its review status.'),
    'AO_UI_ROW_TINT': ('true', 'bool',
                       'Tint each row with its review status colour.'),
    'AO_UI_CONFIDENCE_COLOR': ('true', 'bool',
                               'Colour confidence as red, amber or green.'),
    'AO_UI_CONFIDENT_THRESHOLD': ('0.80', 'percent',
                                  'Minimum confidence shown in green.'),
    'AO_UI_DOUBTFUL_THRESHOLD': ('0.50', 'percent',
                                 'Confidence below this value is shown in red.'),
    'AO_UI_COPY_RECENTS': ('1', 'int',
                           'Recent copy actions shown directly in the right-click menu.'),
    'AO_UI_SHOW_FILTERS': ('true', 'bool', 'Show the filter and search bar.'),
    'AO_UI_SHOW_PANEL': ('true', 'bool',
                         'Show identification details for the selected row.'),
    'AO_UI_HIDDEN_COLUMNS': ('', 'str',
                             'Columns hidden in the review table.'),
    'AO_UI_RESORT_LIVE': ('false', 'bool',
                          'Re-sort the table whenever values change.'),
    'AO_UI_ADVANCE_AFTER_DECISION': ('true', 'bool',
                                     'Select the next row after approving or rejecting.'),
    'AO_UI_CONFIRM_APPLY': ('true', 'bool',
                            'Ask for confirmation before saving files.'),
    'AO_UI_REMEMBER_LAYOUT': ('true', 'bool',
                              'Reopen with the last window size and panel split.'),
    'AO_UI_WINDOW': ('', 'str', 'Saved window layout.'),
    'AO_UI_COLUMN_WIDTHS': ('', 'str', 'Saved table column widths.'),
    'AO_UI_COPY_RECENT_LIST': ('', 'str', 'Recently used copy actions.'),

    # --- chapter merging (the modal remembers what you chose last time)
    'AO_MERGE_TEMPLATE': ('{series} {series_index} - {title}', 'str',
                          'Naming pattern for merged books.'),
    'AO_MERGE_IN_PLACE': ('true', 'bool',
                          'Write merged books beside their chapter files.'),
    'AO_MERGE_DELETE_ORIGINALS': ('false', 'bool',
                                  'Delete chapter files after a successful merge.'),
    'AO_MERGE_BITRATE': ('same', 'choice:same|320k|256k|192k|128k|96k|64k|32k',
                         'Audio bitrate for merged books.'),
    'AO_MERGE_REPLACE_ENTRY': ('true', 'bool',
                               'Replace the chapter entry with the merged book.'),
    'AO_MERGE_OVERWRITE': ('false', 'bool',
                           'Overwrite an existing merged book with the same name.'),
    'AO_TOOLBAR': ('scan,sources,identify,|,approve,reject,reset,|,'
                   'goodreads,preview,apply,undo,|,settings', 'str',
                   'Saved toolbar layout.'),

    # --- misc
    'AO_FFMPEG_PATH': ('ffmpeg', 'str',
                       'ffmpeg executable used for chapter merging.'),
    'AO_LOG_LEVEL': ('DEBUG', 'choice:DEBUG|INFO|WARNING|ERROR', 'Log detail level.'),
    'AO_THREADS': ('4', 'int', 'Maximum number of parallel jobs.'),
}

# Providers are stored flat so they fit .env:
#   AO_PROVIDERS=sanctum,openai
#   AO_PROVIDER_SANCTUM_BASE_URL=...
PROVIDER_FIELDS = ('BASE_URL', 'API_KEY', 'MODEL', 'AUTH_STYLE', 'EXTRA_BODY',
                   'SUPPORTS_JSON_MODE', 'SUPPORTS_SEED')

DEFAULT_PROVIDERS: Dict[str, Dict[str, str]] = {
    # No base URL. This is a private gateway on somebody's LAN - hard-coding the
    # address here bakes one person's network into every install and into the repo.
    # It is a field you fill in, like the API key.
    'sanctum': {
        'BASE_URL': '',
        'API_KEY': '',
        'MODEL': '__free_priority__/any_free_in_priority',
        'AUTH_STYLE': 'bearer',
        'EXTRA_BODY': '{"enable_tools": false}',
    },
    'openai': {'BASE_URL': 'https://api.openai.com/v1', 'API_KEY': '', 'MODEL': 'gpt-4o-mini'},
    'groq': {'BASE_URL': 'https://api.groq.com/openai/v1', 'API_KEY': '',
             'MODEL': 'llama-3.3-70b-versatile'},
    'openrouter': {'BASE_URL': 'https://openrouter.ai/api/v1', 'API_KEY': '',
                   'MODEL': 'meta-llama/llama-3.3-70b-instruct'},
    'mistral': {'BASE_URL': 'https://api.mistral.ai/v1', 'API_KEY': '',
                'MODEL': 'ministral-8b-latest', 'SUPPORTS_SEED': 'false'},
    'anthropic': {'BASE_URL': 'https://api.anthropic.com/v1', 'API_KEY': '',
                  'MODEL': 'claude-sonnet-4-5', 'SUPPORTS_SEED': 'false',
                  'SUPPORTS_JSON_MODE': 'false'},
    'ollama': {'BASE_URL': 'http://localhost:11434/v1', 'API_KEY': '',
               'MODEL': 'llama3.2:3b', 'AUTH_STYLE': 'none'},
    'lmstudio': {'BASE_URL': 'http://localhost:1234/v1', 'API_KEY': '', 'MODEL': '',
                 'AUTH_STYLE': 'none'},
}

_TRUE = {'true', 'yes', '1', 'on'}
_FALSE = {'false', 'no', '0', 'off'}


class Settings:
    """Reads and writes the project's ``.env``."""

    def __init__(self, env_path: Optional[Path] = None):
        self.env_path = Path(env_path) if env_path else ENV_PATH
        self._values: Dict[str, str] = {}
        self.reload()

    # -------------------------------------------------------------- load / save

    def reload(self) -> None:
        self._values = self._parse(self.env_path)
        self._migrate()

    def _migrate(self) -> None:
        """Bring values written by an older version in line with what they mean now.

        The book number used to be padded by the template itself, so every .env
        carries ``{series_index:02d}`` - a width spelled out in the pattern, which by
        design overrides AO_INDEX_PAD. Someone changing the new padding setting would
        watch it do nothing. Only the exact old default is rewritten, so a width you
        chose deliberately is left alone.
        """
        for key in ('AO_OUTPUT_TEMPLATE', 'AO_FILE_TEMPLATE', 'AO_MERGE_TEMPLATE'):
            value = self._values.get(key, '')
            if value and value == SCHEMA[key][0].replace('{series_index}',
                                                         '{series_index:02d}'):
                self._values[key] = SCHEMA[key][0]

    @staticmethod
    def _parse(path: Path) -> Dict[str, str]:
        values: Dict[str, str] = {}
        if not path.exists():
            return values
        for raw in path.read_text(encoding='utf-8').splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            values[key.strip()] = _unquote(value.strip())
        return values

    def get(self, key: str, default: Any = None) -> str:
        """Environment wins over the .env file, which wins over the schema default."""
        if key in os.environ and os.environ[key] != '':
            return os.environ[key]
        if key in self._values:
            return self._values[key]
        if default is not None:
            return str(default)
        spec = SCHEMA.get(key)
        return spec[0] if spec else ''

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key).strip().lower()
        if value in _TRUE:
            return True
        if value in _FALSE:
            return False
        return default

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.get(key)))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key))
        except (TypeError, ValueError):
            return default

    def get_list(self, key: str) -> List[str]:
        return [p.strip() for p in self.get(key).split(',') if p.strip()]

    def is_set(self, key: str) -> bool:
        """True when a value has actually been chosen, rather than left blank.

        ``get_path`` has to return *something*, and for an empty value that something
        is the project directory - which is exactly the folder nothing should be
        scanned from or written into. Callers that act on a path ask this first.
        """
        return bool(str(self.get(key)).strip())

    def get_path(self, key: str) -> Path:
        value = self.get(key)
        path = Path(value)
        return path if path.is_absolute() else PROJECT_ROOT / path

    def display_path(self, path: Any) -> str:
        """A path written the way it is configured, for showing to the user.

        ``get_path`` resolves a relatively-configured folder against the program
        directory, so every path handed around inside the program is absolute. Printing
        that leaks the whole drive layout for a library the user described as
        ``input/``. This maps it back: a path under a folder whose setting is relative
        is shown relative, and only a folder the user actually typed as absolute is
        shown absolute.
        """
        target = Path(path)
        for key in ('AO_INPUT_DIR', 'AO_OUTPUT_DIR'):
            raw = str(self.get(key)).strip()
            if not raw or Path(raw).is_absolute():
                continue
            try:
                relative = target.relative_to(self.get_path(key))
            except (ValueError, TypeError, OSError):
                continue
            return str(Path(raw) / relative) if relative.parts else raw
        try:
            return str(target.relative_to(PROJECT_ROOT))
        except ValueError:
            return str(target)

    def set(self, key: str, value: Any) -> None:
        """Stage a value in memory. Call :meth:`save` to persist."""
        if isinstance(value, bool):
            value = 'true' if value else 'false'
        elif isinstance(value, (list, tuple)):
            value = ','.join(str(v) for v in value)
        self._values[key] = str(value)

    def update(self, values: Dict[str, Any]) -> None:
        for key, value in values.items():
            self.set(key, value)

    def save(self) -> None:
        """Rewrite .env, keeping existing comments and key order intact."""
        remaining = dict(self._values)
        lines: List[str] = []

        if self.env_path.exists():
            for raw in self.env_path.read_text(encoding='utf-8').splitlines():
                stripped = raw.strip()
                if not stripped or stripped.startswith('#') or '=' not in stripped:
                    lines.append(raw)
                    continue
                key = stripped.partition('=')[0].strip()
                if key in remaining:
                    lines.append(f'{key}={_quote(remaining.pop(key))}')
                # a key deleted from memory is dropped from the file
        else:
            lines.append('# Audiobook Organizer settings. Managed by the Settings page.')

        if remaining:
            if lines and lines[-1].strip():
                lines.append('')
            for key, value in remaining.items():
                lines.append(f'{key}={_quote(value)}')

        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        self._write('\n'.join(lines) + '\n')

    def _write(self, text: str) -> None:
        """Replace .env atomically, so an interrupted save cannot truncate it."""
        # with_suffix() is wrong for dotfiles: Path('.env').with_suffix('.tmp') is
        # '.tmp', not '.env.tmp'. Build the sibling name explicitly.
        tmp = self.env_path.with_name(self.env_path.name + '.tmp')
        tmp.write_text(text, encoding='utf-8')
        tmp.replace(self.env_path)
        logger.info('Saved settings to %s', self.env_path)

    # ---------------------------------------------------------------- providers

    def provider_names(self) -> List[str]:
        names = self.get_list('AO_PROVIDERS')
        if names:
            return names
        # Infer from whatever provider keys exist, else fall back to the built-ins.
        found = sorted({m.group(1).lower() for key in self._values
                        if (m := re.match(r'AO_PROVIDER_(.+?)_BASE_URL$', key))})
        return found or list(DEFAULT_PROVIDERS)

    def provider(self, name: str) -> Dict[str, str]:
        """Merged provider definition: .env values over built-in defaults."""
        base = dict(DEFAULT_PROVIDERS.get(name.lower(), {}))
        for field in PROVIDER_FIELDS:
            value = self.get(f'AO_PROVIDER_{name.upper()}_{field}', default='')
            if value:
                base[field] = value
        return base

    def set_provider(self, name: str, values: Dict[str, str]) -> None:
        for field in PROVIDER_FIELDS:
            if field in values:
                self.set(f'AO_PROVIDER_{name.upper()}_{field}', values[field])
        names = self.provider_names()
        if name not in names:
            names.append(name)
        self.set('AO_PROVIDERS', names)

    # ------------------------------------------------------------------ helpers

    def as_dict(self) -> Dict[str, str]:
        return {key: self.get(key) for key in SCHEMA}

    def ensure_file(self) -> None:
        """Create .env from the built-in defaults if it does not exist yet.

        Written through the shared layout, so a fresh install's file is already
        sectioned and commented rather than a flat wall of keys. :meth:`save`
        preserves that structure from then on.
        """
        if self.env_path.exists():
            return
        self.set('AO_PROVIDERS', list(DEFAULT_PROVIDERS))
        for name, fields in DEFAULT_PROVIDERS.items():
            self.set_provider(name, fields)
        for key, (default, _type, _help) in SCHEMA.items():
            self._values.setdefault(key, default)

        from .env_layout import render
        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        self._write(render(self._values))


def _quote(value: str) -> str:
    """Quote only when needed, so the file stays readable."""
    if value == '' or re.fullmatch(r'[^\s#\'"][^#\'"]*', value) and value == value.strip():
        return value
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        inner = value[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", '\\')
    # strip trailing inline comment on unquoted values
    return value.split(' #', 1)[0].strip()


_settings: Optional[Settings] = None


def get_settings(reload: bool = False) -> Settings:
    """Process-wide Settings singleton."""
    global _settings
    if _settings is None or reload:
        _settings = Settings()
        _settings.ensure_file()
    return _settings


def display_path(path: Any) -> str:
    """:meth:`Settings.display_path` for callers that hold no Settings of their own."""
    try:
        return get_settings().display_path(path)
    except OSError:
        return str(path)
