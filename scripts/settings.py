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
    'AO_INPUT_DIR': ('', 'path', 'Folder to scan for unsorted audiobooks.'),
    'AO_OUTPUT_DIR': ('', 'path', 'Folder the organised library is written to.'),

    # --- LLM provider
    'AO_PROVIDER': ('sanctum', 'str', 'Active LLM provider (a name from the provider list).'),
    'AO_TEMPERATURE': ('0.1', 'float', 'Sampling temperature for LLM calls.'),
    'AO_MAX_TOKENS': ('2048', 'int', 'Maximum tokens per LLM response.'),
    'AO_TIMEOUT': ('120', 'int', 'HTTP timeout in seconds.'),
    'AO_MAX_RETRIES': ('3', 'int', 'Retries per failed HTTP call (exponential backoff).'),

    # --- resolution chain
    'AO_ENABLE_METADATA': ('true', 'bool', 'Tier 1: read tags embedded in the audio files.'),
    'AO_ENABLE_REGEX': ('true', 'bool', 'Tier 2: parse author/series/title out of filenames.'),
    'AO_ENABLE_API': ('true', 'bool', 'Tier 3: look the book up in online book databases.'),
    'AO_ENABLE_SEARCH': ('true', 'bool', 'Tier 4: fall back to a web search + page scrape.'),
    'AO_ENABLE_LLM': ('true', 'bool', 'Tier 5: ask a language model to fill the gaps.'),
    'AO_API_SOURCES': ('audnexus,itunes,googlebooks,openlibrary,librivox', 'str',
                       'Book databases to query, in order. Available: audnexus '
                       '(Audible), itunes (Apple Books), googlebooks, openlibrary, '
                       'librivox (public domain only).'),
    'AO_GOOGLE_BOOKS_KEY': ('', 'secret',
                            'Google Books API key. Without one that source cannot be '
                            'used at all: anonymous callers share a project whose daily '
                            'quota is zero, so every request comes back HTTP 429. A key '
                            'is free from console.cloud.google.com - enable the Books '
                            'API and create an API key.'),
    'AO_SEARCH_BRAVE_KEY': ('', 'secret',
                            'Brave Search API key, used by tier 4. Without one that '
                            'tier can only fall back to DuckDuckGo, which now answers '
                            'automated callers with an anti-bot challenge instead of '
                            'results - so in practice tier 4 is reduced to scraping '
                            'Goodreads directly. A key is free from '
                            'brave.com/search/api (the free plan allows 2,000 queries '
                            'a month, one per second).'),
    'AO_CONFIDENCE_SCORE': ('0.80', 'percent',
                            'How sure an identification must be before searching '
                            'stops. Lower it to accept looser matches, raise it to '
                            'keep digging. This is not a filter - it is the point at '
                            'which the tiers stop looking.'),
    'AO_ALWAYS_SEARCH_TO_TIER': ('3', 'choice:1|2|3|4|5',
                                 'Always run at least this many tiers, however '
                                 'confident we already are. 1 tags, 2 filenames, '
                                 '3 book databases, 4 web search, 5 the model.'),
    'AO_REQUIRE_COVER': ('false', 'bool',
                         'Treat a missing cover image as incomplete data, so the '
                         'next source is tried for it too.'),
    'AO_FOLDER_REASONING': ('true', 'bool',
                            'Resolve all books in a folder in one LLM call (cheaper, smarter).'),

    # --- caching
    'AO_CACHE_DB': ('cache.sqlite3', 'path', 'SQLite file holding cached lookups.'),
    'AO_CACHE_MISS_TTL': ('86400', 'int',
                          'Seconds to remember a failed lookup. Successful lookups never expire.'),
    'AO_RESUME_SCANS': ('true', 'bool', 'Skip entries already resolved in a previous run.'),

    # --- review
    'AO_AUTO_APPROVE_THRESHOLD': ('0.0', 'percent',
                                  'Auto-approve entries at or above this confidence. '
                                  '0 disables it.'),
    'AO_DETECT_DUPLICATES': ('true', 'bool', 'Flag books that look like ones you already have.'),

    # --- apply behaviour
    'AO_COPY_MODE': ('true', 'bool',
                     'How Save writes to the output folder. ON = copy: the originals '
                     'stay exactly where they are and a second copy is written to the '
                     'output folder, so your input library is never touched (uses twice '
                     'the disk space). OFF = move: the files are moved out of the input '
                     'folder into the output folder and the input copy is gone - faster, '
                     'instant on the same drive, and no duplicated data, but the only '
                     'way back is Undo. Either way the files are renamed and foldered '
                     'by the templates below.'),
    'AO_OUTPUT_TEMPLATE': ('{author}/{series} {series_index:02d} - {title}', 'str',
                           'Destination folder. Placeholders: {author} {series} '
                           '{series_index} {title}. Empty fields collapse gracefully.'),
    'AO_RENAME_FILES': ('true', 'bool',
                        'Rename the audio files themselves to match the file template '
                        'below. Off means the files keep their original names and only '
                        'the folders are organised.'),
    'AO_FILE_TEMPLATE': ('{series} {series_index:02d} - {title} {file_index:03d}', 'str',
                         'Name given to each audio file when renaming is on. '
                         'Placeholders: {author} {series} {series_index} (or {index}) '
                         '{title} {file_index} {extension}. Any number takes a format '
                         'spec: {series_index:02d} pads to two digits, '
                         '{file_index:03d} to three. Empty fields collapse without '
                         'leaving gaps, and the real file extension is appended if '
                         'the template does not end in one.'),
    'AO_RENAME_SUPPORT_FILES': ('true', 'bool',
                                'Rename every non-audio file that travels with the '
                                'book - cover art, .epub, .pdf, .nfo, .cue, .txt - to '
                                'the same name as the audio. A file is only renamed '
                                'when it is the only one of its extension in the '
                                'output folder, because two .jpgs renamed to one stem '
                                'would collide.'),
    'AO_COLLISION_POLICY': ('suffix', 'choice:suffix|skip|merge|overwrite',
                            'What to do when the destination already exists.'),
    'AO_ILLEGAL_CHARS': ('smart', 'choice:smart|dash|underscore|space|remove',
                         'How characters that a book title may contain but a filename '
                         'may not are replaced.\n'
                         'Smart - a look-alike per character:  :  becomes " -",  /  '
                         'and  \\  and  |  become "-",  <  >  become "(" ")",  "  '
                         'becomes \',  *  becomes +,  ?  is dropped.  '
                         '"Who Goes There? Vol 2: Rising" becomes '
                         '"Who Goes There Vol 2 - Rising".\n'
                         'Dash - every one of  < > : " / \\ | ? *  becomes "-":  '
                         '"Who Goes There- Vol 2- Rising".\n'
                         'Underscore - all of them become "_".  '
                         'Space - all of them become " ".  '
                         'Remove - all of them are deleted:  '
                         '"Who Goes There Vol 2 Rising".'),
    'AO_WARN_DIRTY_OUTPUT': ('true', 'bool',
                             'Flag values that look like a scrape went wrong - an '
                             'unclosed bracket, stray punctuation, HTML entities, an '
                             'author in ALL CAPS - and lower the confidence of the '
                             'fields involved so they surface for review instead of '
                             'being applied silently.'),
    'AO_WRITE_TAGS': ('true', 'bool', 'Write corrected metadata back into the audio files.'),
    'AO_WRITE_SIDECAR': ('false', 'bool', 'Write metadata.json + .opf next to the book.'),

    # --- interface
    'AO_UI_DENSITY': ('normal', 'choice:compact|normal|comfortable',
                      'Row height in the review table. Compact hides cover art.'),
    'AO_UI_ICON_SIZE': ('large', 'choice:compact|normal|comfortable|large',
                        'Size of the toolbar buttons. "Large" is half again bigger '
                        'than comfortable, icon included.'),
    'AO_UI_TOOLBAR_LABELS': ('true', 'bool',
                             'Show each button name underneath its icon.'),
    'AO_UI_COLOR_BY_SOURCE': ('true', 'bool',
                              'Tint each field by which source produced it. Off by '
                              'default: an approved row reads green throughout, and '
                              'mixing per-field hues into that hides the decision.'),
    'AO_UI_SHOW_COVERS': ('true', 'bool', 'Show cover art in the review table.'),
    'AO_UI_STATUS_STRIPE': ('true', 'bool',
                            'Mark each row with a colour stripe for its review status.'),
    'AO_UI_ROW_TINT': ('true', 'bool',
                       'Also wash the whole row in its status colour. Loud, and off '
                       'by default - the stripe and the status pill already say it.'),
    'AO_UI_CONFIDENCE_COLOR': ('true', 'bool',
                               'Colour the confidence bar by how confident we are - '
                               'red below 50%, amber below 80%, green above. Off draws '
                               'it in neutral grey.'),
    'AO_UI_COPY_RECENTS': ('1', 'int',
                           'How many recently-used copy actions to list directly in the '
                           'right-click menu, above the "Copy..." submenu. 0 shows none. '
                           'Set it to the number of copy actions or higher and they are '
                           'all listed, and the submenu disappears.'),
    'AO_UI_SHOW_FILTERS': ('true', 'bool', 'Show the filter and search bar.'),
    'AO_UI_SHOW_PANEL': ('true', 'bool',
                         'Show the panel explaining how the selected row was identified.'),
    'AO_UI_HIDDEN_COLUMNS': ('', 'str',
                             'Columns hidden in the review table. Right-click the '
                             'table header to change this.'),
    'AO_UI_RESORT_LIVE': ('false', 'bool',
                          'Re-sort the table as values change. Off by default: rows '
                          'jumping to a new position the moment you approve them '
                          'loses your place. Rows move on sort, rescan or restart.'),
    'AO_UI_ADVANCE_AFTER_DECISION': ('true', 'bool',
                                     'Move to the next row after approving or '
                                     'rejecting, so reviewing flows without the mouse.'),
    'AO_UI_CONFIRM_APPLY': ('true', 'bool',
                            'Ask for confirmation before applying to the filesystem.'),
    'AO_UI_REMEMBER_LAYOUT': ('true', 'bool',
                              'Reopen with the last window size and panel split.'),
    'AO_UI_WINDOW': ('', 'str', 'Saved window size and split position.'),
    'AO_UI_COLUMN_WIDTHS': ('', 'str', 'Saved review-table column widths.'),
    'AO_UI_COPY_RECENT_LIST': ('', 'str', 'Recently used copy actions, most recent first.'),

    # --- chapter merging (the modal remembers what you chose last time)
    'AO_MERGE_TEMPLATE': ('{series} {series_index:02d} - {title}', 'str',
                          'Name pattern for a merged .m4b. Empty falls back to the '
                          'file template on the Output tab.'),
    'AO_MERGE_IN_PLACE': ('true', 'bool',
                          'Write the merged .m4b next to the chapter files. Off writes '
                          'it into the output folder instead.'),
    'AO_MERGE_DELETE_ORIGINALS': ('false', 'bool',
                                  'Delete the chapter files once the merge succeeded.'),
    'AO_MERGE_BITRATE': ('same', 'choice:same|320k|256k|192k|128k|96k|64k|32k',
                         'Bitrate for a merged .m4b. "Same" reads the bitrate of the '
                         'chapter files and encodes at that, so a merge never throws '
                         'quality away - it is the right answer almost always. The '
                         'fixed rates re-encode to that number whatever the source '
                         'was; 64k is the usual choice for spoken word.'),
    'AO_MERGE_REPLACE_ENTRY': ('true', 'bool',
                               'Point the library entry at the merged .m4b afterwards, '
                               'so it is treated as a single-file book.'),
    'AO_MERGE_OVERWRITE': ('false', 'bool',
                           'Overwrite an existing .m4b of the same name instead of '
                           'appending " (2)" to the new one.'),
    'AO_TOOLBAR': ('scan,sources,identify,|,approve,reject,reset,|,'
                   'goodreads,preview,apply,undo,|,settings', 'str',
                   'Toolbar buttons in display order; "|" is a separator. '
                   'Anything left out is hidden. Edit this on the Toolbar tab.'),

    # --- misc
    'AO_FFMPEG_PATH': ('ffmpeg', 'str',
                       'Path to the ffmpeg executable. Only needed for "Merge chapters '
                       'into one .m4b" in the right-click menu; leave it as "ffmpeg" if '
                       'ffmpeg is on your PATH.'),
    'AO_LOG_LEVEL': ('DEBUG', 'choice:DEBUG|INFO|WARNING|ERROR', 'Console log verbosity.'),
    'AO_THREADS': ('4', 'int', 'Parallel workers for lookups.'),
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
