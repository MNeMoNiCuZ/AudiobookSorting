"""Filesystem-safe path construction (#22, #35).

Windows is the hostile case: reserved device names, a 260-character default limit,
trailing dots and spaces that silently vanish, and characters that are legal in a book
title but not in a filename.
"""

from __future__ import annotations

import os
import re
import string
import sys
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


def project_root() -> Path:
    """The folder the program lives in: the one holding launch.bat, src/ and the .exe.

    ``Path(__file__)`` is right when running from source and wrong in
    every way that matters once PyInstaller has packed this into a one-file .exe: the
    sources are unpacked into ``%TEMP%\\_MEIxxxxx``, which is deleted when the process
    exits. Settings written there vanish on close, and a relative input folder like
    "input" resolves into the unpacked bundle, which is why Scan found nothing.
    Frozen, the root is the directory holding the executable.
    """
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent  # src/scripts/paths.py


PROJECT_ROOT = project_root()

# Everything the program writes that belongs to the user: .env, book_entries.json, the
# lookup cache, logs and list backups. Gitignored.
USER_DIR = PROJECT_ROOT / 'user'

# Disposable output nobody needs to keep: build intermediates, bytecode, and the
# scratch space below. Gitignored, and safe to delete at any time.
JUNK_DIR = PROJECT_ROOT / 'src' / 'junk'

# Scratch space for anything that has to hit disk mid-job - ffmpeg segment files, the
# generated combo-box arrow. It lives in the project rather than in %TEMP% so that a
# crashed run leaves its debris somewhere you can see it and delete it, instead of
# scattering half-written audio through the system temp folder.
TEMP_ROOT = JUNK_DIR / 'temp'

# What older versions wrote straight into the program folder, and where it goes now.
_LEGACY_USER_FILES = {
    '.env': '.env',
    'book_entries.json': 'book_entries.json',
    'cache.sqlite3': 'cache.sqlite3',
    'backups': 'backups',
    'logs': 'logs',
    'apply_journal.jsonl': 'logs/apply_journal.jsonl',
}


def migrate_user_files() -> None:
    """Move user files an older version left in the program folder into ``user/``.

    Called once at start-up, before the settings or the log are opened. Anything that
    already exists under ``user/`` is left alone, and a file that cannot be moved
    (held open by another copy of the program) is simply tried again next start.
    """
    import shutil

    for old, new in _LEGACY_USER_FILES.items():
        source, target = PROJECT_ROOT / old, USER_DIR / new
        if not source.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir() and target.is_dir():
                for child in source.iterdir():
                    if not (target / child.name).exists():
                        shutil.move(str(child), str(target / child.name))
                if not any(source.iterdir()):
                    source.rmdir()
            elif not target.exists():
                shutil.move(str(source), str(target))
            elif source.suffix == '.jsonl':
                _merge_journal(source, target)
        except OSError:
            continue
    for log in PROJECT_ROOT.glob('audiobook_organizer.log*'):
        try:
            (USER_DIR / 'logs').mkdir(parents=True, exist_ok=True)
            if not (USER_DIR / 'logs' / log.name).exists():
                shutil.move(str(log), str(USER_DIR / 'logs' / log.name))
        except OSError:
            continue

    # The cache used to default to a path relative to the program folder.
    env = USER_DIR / '.env'
    try:
        text = env.read_text(encoding='utf-8')
    except OSError:
        return
    fixed = re.sub(r'(?m)^AO_CACHE_DB=cache\.sqlite3$', 'AO_CACHE_DB=user/cache.sqlite3', text)
    if fixed != text:
        env.write_text(fixed, encoding='utf-8')


def _merge_journal(source: Path, target: Path) -> None:
    """Fold a JSON-lines journal left in the program folder into the one under ``user/``.

    An older copy of the program keeps appending to the old location after the new one
    exists, so neither file can simply win. Lines from both are kept, duplicates
    dropped, and the result ordered by timestamp, since undo treats the last line as
    the newest transaction.
    """
    import json

    lines, seen = [], set()
    for path in (target, source):
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip() and line not in seen:
                seen.add(line)
                lines.append(line)

    def stamp(line: str) -> float:
        try:
            return float(json.loads(line).get('timestamp', 0.0))
        except (ValueError, TypeError, AttributeError):
            return 0.0

    lines.sort(key=stamp)
    scratch = target.with_name(target.name + '.merging')
    scratch.write_text(''.join(line + '\n' for line in lines), encoding='utf-8')
    scratch.replace(target)
    source.unlink()


def temp_dir(prefix: str = 'ao') -> Path:
    """A fresh, empty working directory under ``src/junk/temp/``."""
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TEMP_ROOT / f'{prefix}_{uuid.uuid4().hex[:10]}'
    path.mkdir(parents=True, exist_ok=False)
    return path


def clean_temp() -> int:
    """Delete working directories left behind by a run that was killed.

    Called once at start-up. Only the ``<prefix>_<hex>`` directories made by
    :func:`temp_dir` are touched; single files like the combo arrow are reused.
    """
    import shutil

    removed = 0
    if not TEMP_ROOT.is_dir():
        return 0
    for child in TEMP_ROOT.iterdir():
        if child.is_dir() and re.fullmatch(r'[a-z]+_[0-9a-f]{10}', child.name):
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def temp_file(name: str) -> Path:
    """A fixed path under ``src/junk/temp/``, for a single reusable artefact."""
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    return TEMP_ROOT / name

# Legal in a title, illegal in a Windows filename. Mapped to look-alikes where one
# exists, so "What's It All About? Part 1: Beginnings" stays readable.
_REPLACEMENTS = {
    '<': '(', '>': ')', ':': ' -', '"': "'", '/': '-', '\\': '-',
    '|': '-', '?': '', '*': '+',
}

ILLEGAL = '<>:"/\\|?*'

# The named strategies offered by AO_ILLEGAL_CHARS. "smart" is the table above - one
# look-alike per character, chosen so the title still reads. The others are blunt: the
# same replacement for every illegal character, which is what people expect from a
# setting called "replace illegal characters with".
_STRATEGIES = {
    'smart': _REPLACEMENTS,
    'dash': {c: '-' for c in ILLEGAL},
    'underscore': {c: '_' for c in ILLEGAL},
    'space': {c: ' ' for c in ILLEGAL},
    'remove': {c: '' for c in ILLEGAL},
}

# Set once at start-up from AO_ILLEGAL_CHARS. A module-level default keeps
# sanitize_component() callable from the dozen places that have no Settings to hand.
_mode = 'smart'


def set_illegal_char_mode(mode: str) -> None:
    """Choose how illegal filename characters are replaced, for the whole process."""
    global _mode
    _mode = mode if mode in _STRATEGIES else 'smart'


def illegal_char_mode() -> str:
    return _mode

# How wide a book number is padded when the template asks for no particular width.
# Set once at start-up from AO_INDEX_PAD, and module-level for the same reason the
# illegal-character strategy is: rendering happens far from any Settings object.
_index_pad = 2


def set_index_pad(width: int) -> None:
    """Pad plain ``{series_index}`` to this many digits, process-wide."""
    global _index_pad
    try:
        _index_pad = max(0, min(9, int(width)))
    except (TypeError, ValueError):
        _index_pad = 2


def index_pad() -> int:
    return _index_pad

_RESERVED = {
    'con', 'prn', 'aux', 'nul',
    *(f'com{i}' for i in range(1, 10)),
    *(f'lpt{i}' for i in range(1, 10)),
}

_CONTROL_CHARS = ''.join(map(chr, range(0, 32)))


def sanitize_component(text: str, fallback: str = 'Unknown',
                       mode: Optional[str] = None) -> str:
    """Make one path segment safe, without destroying its readability."""
    if not text:
        return fallback

    text = unicodedata.normalize('NFC', str(text))
    # A colon is always " - " whatever the strategy: "Title: Subtitle" reads as
    # "Title - Subtitle", never "Title- Subtitle" or "Title -Subtitle".
    text = re.sub(r'\s*:+\s*', ' - ', re.sub(r'^[\s:]*:|:[\s:]*$', '', text))
    for bad, good in _STRATEGIES.get(mode or _mode, _REPLACEMENTS).items():
        text = text.replace(bad, good)
    text = text.translate({ord(c): None for c in _CONTROL_CHARS})

    text = re.sub(r'\s{2,}', ' ', text)
    # Windows silently strips trailing dots and spaces, which breaks later lookups.
    text = text.strip().strip('.').strip()

    if text.split('.')[0].lower() in _RESERVED:
        text = f'_{text}'

    return text or fallback


def render_template(template: str, values: Dict[str, str]) -> str:
    """Render an output template, collapsing the gaps left by empty fields.

    ``"{author}/{series} {series_index:02d} - {title}"`` with no series yields
    ``"Author/Title"`` rather than ``"Author/ 00 - Title"``.
    """
    text = template

    # {index} is an alias for {series_index}: shorter, and what people type.
    text = re.sub(r'\{index(:[^}]+)?\}', lambda m: '{series_index%s}' % (m.group(1) or ''),
                  text)

    # Numeric formats only make sense when there is a number. Both the series index
    # and the per-file part number take a format spec, so "{file_index:03d}" pads the
    # same way "{series_index:02d}" always has.
    for key in ('series_index', 'file_index'):
        text = _render_number(text, key, values.get(key, ''),
                              default_spec=(f':0{_index_pad}d'
                                            if key == 'series_index' and _index_pad > 1
                                            else ''))

    for key in ('author', 'series', 'title', 'extension'):
        value = str(values.get(key, '') or '')
        if key == 'title':
            value = pad_title_number(value)
        value = sanitize_component(value, fallback='')
        text = text.replace(f'{{{key}}}', value)

    # ".{extension}" with nothing to put in it leaves a trailing dot behind.
    text = re.sub(r'\.\s*$', '', text)
    # A placeholder that rendered empty just before the extension leaves "Title .mp3".
    text = re.sub(r'\s+\.(?=[A-Za-z0-9]{1,5}$)', '.', text)

    # Any placeholder we don't know about is dropped rather than left as literal text.
    text = re.sub(r'\{[a-z_]+(:[^}]+)?\}', '', text)

    # Tidy the holes left behind by empty fields.
    parts = []
    for part in text.split('/'):
        part = re.sub(r'\s{2,}', ' ', part)
        part = re.sub(r'^[\s\-–—_,]+|[\s\-–—_,]+$', '', part)
        part = re.sub(r'\s+-\s+-\s+', ' - ', part)
        part = sanitize_component(part, fallback='')
        if part:
            parts.append(part)

    return '/'.join(parts) if parts else 'Unknown'


def display_index(value: Any) -> str:
    """A stored book number as the output will write it: "2" -> "02", "1-3" -> "01-03".

    The stored value stays unpadded (see ``clean_value``); this is for showing it, so
    the grid reads the same as the names that Apply produces.
    """
    text = str(value or '').strip()
    if not text or _index_pad < 2:
        return text
    return '-'.join(_format_number(part, f':0{_index_pad}d') for part in text.split('-'))


def pad_title_number(title: str) -> str:
    """Pad a book number the title ends in to the book-number width.

    Plenty of series title each book by the series name and a number -
    "Accidental Astronaut 2", "Arena 6" - so the number never passes through
    ``{series_index}`` and sorts "Arena 10" before "Arena 2". Only a standalone
    trailing number shorter than the width is touched: "Catch-22" and "1984" are left
    alone, and AO_INDEX_PAD below 2 switches this off with the rest of the padding.
    """
    if _index_pad < 2:
        return title
    match = re.search(r'(?<=\s)(\d+)$', title.rstrip())
    if not match or len(match.group(1)) >= _index_pad:
        return title
    return title.rstrip()[:match.start()] + match.group(1).zfill(_index_pad)


def _render_number(text: str, key: str, raw: Any, default_spec: str = '') -> str:
    """Substitute a numeric placeholder, honouring an optional format spec.

    ``{file_index:03d}`` -> "007". An empty value removes the placeholder entirely so
    single-file books do not end up called "Title 000". ``default_spec`` is used for
    the placeholders written without one, which is how the "pad book number" setting
    reaches a template that just says ``{series_index}``.

    A bundled omnibus carries a range - "1-3" - and both ends are padded to the same
    width, so "Book 01-03" rather than "Book 01-3".
    """
    value = str(raw or '').strip()
    pattern = r'\{%s(:[^}]+)?\}' % key
    if not value:
        return re.sub(pattern, '', text)

    for match in set(re.findall(pattern, text)):
        spec = match or default_spec
        rendered = '-'.join(_format_number(part, spec)
                            for part in value.split('-')) if spec else value
        text = text.replace(f'{{{key}{match or ""}}}', rendered)
    return text


def _format_number(value: str, spec: str) -> str:
    """One number through one format spec, falling back to the text unchanged."""
    value = value.strip().replace(',', '.')
    try:
        as_int = int(float(value))
    except ValueError:
        as_int = None
    # A half-book - "2.5", the novella between two novels - is still a number, and an
    # integer format would round it away to "02". Pad the whole part, keep the rest.
    whole, dot, fraction = value.partition('.')
    if dot and fraction.isdigit() and whole.isdigit() and 'd' in spec:
        # A prequel novella is "0.5", not "00.5": a leading zero alone is the number,
        # and padding it only adds a second one.
        if int(whole) == 0:
            return '0.' + fraction
        return _format_number(whole, spec) + '.' + fraction
    try:
        return format(as_int if 'd' in spec and as_int is not None else value,
                      spec.lstrip(':'))
    except (ValueError, TypeError):
        return value


def platform_path_limit() -> int:
    """The longest destination path this machine can actually take.

    This used to be a setting, which asked the user to know something the operating
    system already knows. Windows is 260 unless long paths are switched on in the
    registry, in which case it is effectively unlimited; POSIX gives us PATH_MAX. A
    margin is kept for the filenames that go *inside* the folder we are building.
    """
    if os.name != 'nt':
        try:
            return max(240, os.pathconf('/', 'PC_PATH_MAX') - 80)
        except (OSError, ValueError, AttributeError):
            return 3000

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'SYSTEM\CurrentControlSet\Control\FileSystem') as key:
            if winreg.QueryValueEx(key, 'LongPathsEnabled')[0]:
                return 30000
    except OSError:
        pass
    # 260 is the hard ceiling; leave room for "\Book 01 - Title.mp3" underneath.
    return 180


def build_destination(output_dir: Path, template: str, values: Dict[str, str],
                      max_path_length: Optional[int] = None) -> Path:
    """Full destination directory for a book, guaranteed to be a usable path."""
    relative = render_template(template, values)
    destination = Path(output_dir) / relative
    if max_path_length is None:
        max_path_length = platform_path_limit()
    return shorten_path(destination, max_path_length)


def shorten_path(path: Path, max_length: int = 240) -> Path:
    """Trim the deepest segments until the whole path fits (#35).

    Truncation happens at word boundaries where possible, and the last segment is
    always left non-empty.
    """
    text = str(path)
    if len(text) <= max_length:
        return path

    parts = list(path.parts)
    if len(parts) < 2:
        return path

    # Shorten from the deepest segment outwards - that's where the long title is.
    for index in range(len(parts) - 1, 0, -1):
        while len(str(Path(*parts))) > max_length and len(parts[index]) > 12:
            segment = parts[index]
            cut = segment.rfind(' ', 0, len(segment) - 4)
            parts[index] = (segment[:cut] if cut > 12 else segment[:-4]).rstrip(' .-_')
        if len(str(Path(*parts))) <= max_length:
            break

    return Path(*parts)


def long_path(path: Path) -> str:
    """Windows extended-length form, so >260-char paths still work at the syscall."""
    import os

    if os.name != 'nt':
        return str(path)
    resolved = os.path.abspath(str(path))
    if resolved.startswith('\\\\?\\'):
        return resolved
    if resolved.startswith('\\\\'):
        return '\\\\?\\UNC' + resolved[1:]
    return '\\\\?\\' + resolved


def unique_path(path: Path, existing=None) -> Path:
    """Append " (2)", " (3)"... until the path is free."""
    if not path.exists() and (existing is None or path not in existing):
        return path
    parent, stem, suffix = path.parent, path.stem, path.suffix
    for counter in range(2, 1000):
        candidate = parent / f'{stem} ({counter}){suffix}'
        if not candidate.exists() and (existing is None or candidate not in existing):
            return candidate
    raise OSError(f'Could not find a free name near {path}')
