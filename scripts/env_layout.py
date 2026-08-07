"""The one and only layout for .env.

A fresh install's ``.env`` is rendered from this module, so it arrives already
sectioned and commented rather than as a flat wall of keys. There is no committed
``.env.example``: the app writes its own ``.env`` from the built-in defaults, with
these same comments, the first time it starts, so a checked-in copy of that same
output was only ever a second file to keep in step.

Per-key comments are the help strings in :data:`scripts.settings.SCHEMA`, the same
text the Settings page shows as a tooltip, so the documentation has one source too.
This module only decides *order*, *grouping* and the extra prose that belongs to a
section rather than to any single key.

Credentials come first: they are the only part nobody can fill in for you.
"""

from __future__ import annotations

import re
import textwrap
from typing import Dict, Iterable, List, Optional, Tuple

WIDTH = 78

HEADER = """\
Audiobook Organizer configuration.

The Settings page (F12) reads and writes this file, and preserves the comments and
key order below - so this layout survives being saved from the GUI. Values resolve
as  process environment  >  this file  >  built-in default, which means a key you
delete keeps working on its default.

Sections match the Settings tabs. Credentials come first because they are the only
part that cannot be defaulted for you; everything else already has a sane value.
"""


# Any key matching these is a secret: never given a built-in default anywhere, and
# never written anywhere but the user's own .env.
_CREDENTIAL = re.compile(r'(?:_API_KEY|_KEY)$')

# Notes on a whole provider block, where the reason a field is blank is not
# obvious from the field itself.
PROVIDER_NOTES: Dict[str, str] = {
    'sanctum': 'A provider-agnostic gateway; the model id routes to whichever '
               'backend the admin configured. No base URL ships with it - that '
               'would bake one person\'s LAN address into every install - so it '
               'is a field you fill in, like the key.',
    'ollama': 'Local server. Keyless, so AUTH_STYLE is none.',
    'lmstudio': 'Local server. Set MODEL to whatever you have loaded.',
}

# Extra prose for keys whose SCHEMA help is too terse to act on. Appended to the
# help text, so anything already said there does not need repeating.
EXTRA_NOTES: Dict[str, List[str]] = {
    'AO_INPUT_DIR': [
        'Absolute, or relative to this folder. Empty means unset.',
    ],
}

# (section title, section prose, [keys]). '*credentials' and '*providers' expand.
SECTIONS: List[Tuple[str, str, List[str]]] = [
    ('Credentials',
     'All optional - the app runs with every one blank, it just has fewer places '
     'to look. Each is explained again beside the feature that uses it.',
     ['AO_SEARCH_BRAVE_KEY', 'AO_GOOGLE_BOOKS_KEY', '*credential_providers']),

    ('Folders', '', ['AO_INPUT_DIR', 'AO_OUTPUT_DIR']),

    ('Language model',
     'The active provider, and the generation settings applied to whichever one '
     'is active.',
     ['AO_PROVIDER', 'AO_PROVIDERS', 'AO_TEMPERATURE', 'AO_MAX_TOKENS',
      'AO_TIMEOUT', 'AO_MAX_RETRIES']),

    ('Providers',
     'One block per entry in AO_PROVIDERS - any endpoint speaking the OpenAI '
     'chat-completions format works. To add your own, append its name there and '
     'define at least _BASE_URL and _MODEL here.\n\n'
     'Per-provider fields: _BASE_URL, _MODEL, _AUTH_STYLE (bearer | x-api-key | '
     'none), _EXTRA_BODY (JSON merged into every request), _SUPPORTS_JSON_MODE, '
     '_SUPPORTS_SEED. The _API_KEY of each is in the Credentials section at the '
     'top of this file.',
     ['*providers']),

    ('Identification',
     'The five tiers, in the order they run. Each fills in only what the earlier '
     'ones could not.',
     ['AO_ENABLE_METADATA', 'AO_ENABLE_REGEX', 'AO_ENABLE_API', 'AO_ENABLE_SEARCH',
      'AO_ENABLE_LLM', 'AO_API_SOURCES', 'AO_CONFIDENCE_SCORE',
      'AO_ALWAYS_SEARCH_TO_TIER', 'AO_REQUIRE_COVER', 'AO_FOLDER_REASONING',
      'AO_REVIEW_APPROVE_THRESHOLD', 'AO_REVIEW_REJECT_THRESHOLD',
      'AO_DETECT_DUPLICATES', 'AO_WARN_DIRTY_OUTPUT']),

    ('Output', '',
     ['AO_COPY_MODE', 'AO_OUTPUT_TEMPLATE', 'AO_RENAME_FILES', 'AO_FILE_TEMPLATE',
      'AO_INDEX_PAD', 'AO_RENAME_SUPPORT_FILES', 'AO_BLOCKED_WORDS',
      'AO_STRIP_PARENTHESES', 'AO_ILLEGAL_CHARS', 'AO_TIDY_PUNCTUATION',
      'AO_COLLISION_POLICY',
      'AO_WRITE_TAGS', 'AO_WRITE_SIDECAR']),

    ('Merging', '',
     ['AO_MERGE_TEMPLATE', 'AO_MERGE_BITRATE', 'AO_MERGE_IN_PLACE',
      'AO_MERGE_DELETE_ORIGINALS', 'AO_MERGE_REPLACE_ENTRY', 'AO_MERGE_OVERWRITE',
      'AO_FFMPEG_PATH']),

    ('Loading',
     'What survives when you load the input folder over work you have already done. '
     'The Load Input dialog sets these as you use it.',
     ['AO_RESUME_SCANS', 'AO_LOAD_KEEP_MANUAL', 'AO_LOAD_KEEP_CONFIDENT',
      'AO_LOAD_KEEP_ABOVE', 'AO_LOAD_KEEP_DECISIONS']),

    ('Cache and logging', '',
     ['AO_CACHE_DB', 'AO_CACHE_MISS_TTL', 'AO_LOG_LEVEL', 'AO_THREADS']),

    ('Interface', '',
     ['AO_UI_DENSITY', 'AO_UI_ICON_SIZE', 'AO_UI_TOOLBAR_LABELS',
      'AO_UI_SHOW_COVERS', 'AO_UI_STATUS_STRIPE', 'AO_UI_ROW_TINT',
      'AO_UI_CONFIDENCE_COLOR', 'AO_UI_CONFIDENT_THRESHOLD',
      'AO_UI_DOUBTFUL_THRESHOLD', 'AO_UI_COLOR_BY_SOURCE', 'AO_UI_SHOW_FILTERS',
      'AO_UI_SHOW_PANEL', 'AO_UI_RESORT_LIVE', 'AO_UI_ADVANCE_AFTER_DECISION',
      'AO_UI_CONFIRM_APPLY', 'AO_UI_REMEMBER_LAYOUT', 'AO_UI_COPY_RECENTS',
      'AO_TOOLBAR']),

    ('Remembered layout',
     'Written by the app as you use the window. Editing these by hand achieves '
     'nothing - move the window instead.',
     ['AO_UI_WINDOW', 'AO_UI_COLUMN_WIDTHS', 'AO_UI_HIDDEN_COLUMNS',
      'AO_UI_GRID_WINDOW', 'AO_UI_GRID_COLUMNS', 'AO_UI_COPY_RECENT_LIST']),
]


def is_credential(key: str) -> bool:
    """True for keys whose value is a secret.

    ``AO_MAX_TOKENS`` is the trap here - it ends in a word that looks like one.
    """
    return bool(_CREDENTIAL.search(key)) and key != 'AO_MAX_TOKENS'


def _comment(chunks: Iterable[str]) -> List[str]:
    """Comment-wrap prose, re-flowing each paragraph as a whole.

    Wrapping the chunks one line at a time would preserve the incidental line
    breaks of the source string and produce a ragged block of two-word lines.
    """
    out: List[str] = []
    for chunk in chunks:
        if not chunk or not chunk.strip():
            continue
        for paragraph in re.split(r'\n\s*\n', chunk.strip()):
            if out:
                out.append('#')  # keep the author's paragraph breaks
            flowed = ' '.join(paragraph.split())
            out.extend(f'# {w}' for w in textwrap.wrap(flowed, WIDTH - 2))
    return out


def _key_comment(key: str) -> List[str]:
    """A key's own documentation: its SCHEMA help, plus any extra notes."""
    from .settings import SCHEMA

    parts: List[str] = []
    if key in SCHEMA:
        parts.append(SCHEMA[key][2])
    parts.extend(EXTRA_NOTES.get(key, []))
    return _comment(parts)


def _expand(token: str, providers: Iterable[str]) -> List[str]:
    """Turn a '*...' placeholder into the keys it stands for."""
    from .settings import DEFAULT_PROVIDERS, PROVIDER_FIELDS

    names = list(providers)
    if token == '*credential_providers':
        return [f'AO_PROVIDER_{n.upper()}_API_KEY' for n in names]
    if token == '*providers':
        keys: List[str] = []
        for name in names:
            for field in PROVIDER_FIELDS:
                if field == 'API_KEY':
                    continue  # lives in the Credentials section
                # Only fields the provider actually defines, so a block stays short.
                if field in DEFAULT_PROVIDERS.get(name.lower(), {}):
                    keys.append(f'AO_PROVIDER_{name.upper()}_{field}')
        return keys
    raise ValueError(f'unknown layout placeholder {token!r}')


def layout_keys(providers: Optional[Iterable[str]] = None) -> List[str]:
    """Every key the layout places, in file order."""
    from .settings import DEFAULT_PROVIDERS

    names = list(providers or DEFAULT_PROVIDERS)
    keys: List[str] = []
    for _title, _prose, entries in SECTIONS:
        for entry in entries:
            keys.extend(_expand(entry, names) if entry.startswith('*') else [entry])
    return keys


def render(values: Dict[str, str], *, header: str = HEADER,
           providers: Optional[Iterable[str]] = None) -> str:
    """Render a .env from ``values``, in the layout above.

    Keys present in ``values`` but absent from the layout are appended under
    "Other" rather than dropped - losing somebody's setting to make the file tidy
    would be a poor trade.
    """
    from .settings import DEFAULT_PROVIDERS, _quote

    names = list(providers or values.get('AO_PROVIDERS', '').split(',') or [])
    names = [n.strip() for n in names if n.strip()] or list(DEFAULT_PROVIDERS)

    # One chunk, not one per line: _comment re-flows paragraphs, and splitting the
    # header into lines first would make every line its own paragraph.
    out: List[str] = _comment([header])
    placed: set = set()

    for title, prose, entries in SECTIONS:
        keys = []
        for entry in entries:
            keys.extend(_expand(entry, names) if entry.startswith('*') else [entry])
        keys = [k for k in keys if k in values or is_credential(k)]
        if not keys:
            continue

        out.append('')
        out.append('# ' + '=' * (WIDTH - 2))
        out.append(f'# {title}')
        out.append('# ' + '=' * (WIDTH - 2))
        if prose:
            out.extend(_comment([prose]))

        # Only the Providers section is grouped into per-provider blocks. In
        # Credentials the key name already names its provider, so a sub-header
        # there would be eight lines of noise.
        grouped = title == 'Providers'
        provider_seen: Optional[str] = None
        for key in keys:
            match = re.match(r'^AO_PROVIDER_([A-Z0-9]+)_', key) if grouped else None
            if match:
                # One "--- name" header per block instead of a note on every
                # field: the fields are documented once in the section prose, and
                # repeating them for all eight providers buries the values.
                if match.group(1) != provider_seen:
                    provider_seen = match.group(1)
                    out.append('')
                    out.append(f'# --- {provider_seen.lower()}')
                    out.extend(_comment(PROVIDER_NOTES.get(provider_seen.lower(), '')
                                        and [PROVIDER_NOTES[provider_seen.lower()]]))
            else:
                if comment := _key_comment(key):
                    out.append('')
                    out.extend(comment)
            out.append(f'{key}={_quote(values.get(key, ""))}')
            placed.add(key)

    leftover = [k for k in values if k not in placed]
    if leftover:
        out.append('')
        out.append('# ' + '=' * (WIDTH - 2))
        out.append('# Other')
        out.append('# ' + '=' * (WIDTH - 2))
        out.extend(_comment(['Not part of the current layout - either set by hand or '
                             'left over from an older version. Nothing reads a key '
                             'the app does not define.']))
        out.extend(f'{k}={_quote(values[k])}' for k in leftover)

    return '\n'.join(out) + '\n'


def relayout_env(path) -> str:
    """Rewrite an existing .env into the canonical layout, values untouched.

    Refuses rather than writes if a key would be lost or a value altered - tidying
    a config file is not worth the chance of silently dropping somebody's key.
    """
    from .settings import Settings

    before = Settings(env_path=path)._values
    text = render(before)

    after = dict(re.findall(r'^([A-Z0-9_]+)=(.*)$', text, re.M))
    from .settings import _unquote
    after = {k: _unquote(v) for k, v in after.items()}
    lost = {k for k in before if k not in after}
    changed = {k for k in before if k in after and after[k] != before[k]}
    if lost or changed:
        raise RuntimeError(
            f'refusing to rewrite {path}: lost={sorted(lost)} changed={sorted(changed)}')

    Settings(env_path=path)._write(text)
    return text


if __name__ == '__main__':
    from .paths import PROJECT_ROOT

    # The only thing there is to generate: rewrite an existing .env into the canonical
    # layout, values untouched. There is no .env.example - the app writes its own .env
    # from the built-in defaults, with these same comments, the first time it starts,
    # so a committed copy of it was a second file to keep in step for no gain.
    target = PROJECT_ROOT / '.env'
    relayout_env(target)
    print(f'relaid out {target} - values unchanged')
