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
    """The folder the program lives in - and writes its .env, cache and temp into.

    ``Path(__file__).parent.parent`` is right when running from source and wrong in
    every way that matters once PyInstaller has packed this into a one-file .exe: the
    sources are unpacked into ``%TEMP%\\_MEIxxxxx``, which is deleted when the process
    exits. Settings written there vanish on close, and a relative input folder like
    "input" resolves into the unpacked bundle, which is why Scan found nothing.
    Frozen, the root is the directory holding the executable.
    """
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = project_root()

# Scratch space for anything that has to hit disk mid-job - ffmpeg segment files, the
# generated combo-box arrow. It lives in the project rather than in %TEMP% so that a
# crashed run leaves its debris somewhere you can see it and delete it, instead of
# scattering half-written audio through the system temp folder. Gitignored.
TEMP_ROOT = PROJECT_ROOT / 'temp'


def temp_dir(prefix: str = 'ao') -> Path:
    """A fresh, empty working directory under the project's own ``temp/``."""
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
    """A fixed path under the project's ``temp/``, for a single reusable artefact."""
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
        text = _render_number(text, key, values.get(key, ''))

    for key in ('author', 'series', 'title', 'extension'):
        value = sanitize_component(str(values.get(key, '') or ''), fallback='')
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


def _render_number(text: str, key: str, raw: Any) -> str:
    """Substitute a numeric placeholder, honouring an optional format spec.

    ``{file_index:03d}`` -> "007". An empty value removes the placeholder entirely so
    single-file books do not end up called "Title 000".
    """
    value = str(raw or '').strip()
    pattern = r'\{%s(:[^}]+)?\}' % key
    if not value:
        return re.sub(pattern, '', text)

    try:
        as_int = int(float(value))
    except ValueError:
        as_int = None

    for match in set(re.findall(pattern, text)):
        spec = match or ''
        try:
            rendered = (format(as_int if 'd' in spec and as_int is not None else value,
                               spec.lstrip(':'))
                        if spec else value)
        except (ValueError, TypeError):
            rendered = value
        text = text.replace(f'{{{key}{spec}}}', rendered)
    return text


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
