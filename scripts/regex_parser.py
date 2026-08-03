"""Tier: parse author / series / index / title straight out of names.

This runs before any network call and resolves a surprising share of a real library
for free. It works on the folder path as well as the filename, because the useful
signal is often split across both (`/Brandon Sanderson/Mistborn 01 - The Final Empire/`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

# Noise that appears in scene-release style names and never belongs in a title.
_NOISE_PATTERNS = [
    re.compile(r'\[[^\]]*\]'),                     # [Narrated by X], [64kbps]
    re.compile(r'\{[^}]*\}'),
    re.compile(r'\((?:19|20)\d{2}\)'),             # (2013)
    re.compile(r'\b(?:19|20)\d{2}\b(?=\s*$)'),     # trailing year
    re.compile(r'\b\d{1,3}\s?kbps\b', re.I),
    re.compile(r'\b(?:mp3|m4b|m4a|flac|ogg|opus|aax|aac)\b', re.I),
    re.compile(r'\b(?:unabridged|abridged|audiobook|audio\s?book|complete)\b', re.I),
    re.compile(r'\b(?:narrated\s+by|read\s+by|narrator)\s+[^-_,]+', re.I),
    re.compile(r'\b(?:64|96|128|192|256|320)\s?k\b', re.I),
    re.compile(r'\b(?:vbr|cbr|stereo|mono)\b', re.I),
]

_SERIES_WORD = r'(?:book|bk|volume|vol|part|pt|#|no\.?|episode|ep)'

# Ordered most-specific first; the first match wins.
_PATTERNS: List[tuple] = [
    # Author - Series 03 - Title
    (re.compile(rf'^(?P<author>.+?)\s+-\s+(?P<series>.+?)\s+{_SERIES_WORD}?\s*'
                rf'(?P<index>\d{{1,3}})\s+-\s+(?P<title>.+)$', re.I), 'author-series-index-title'),
    # Author - Title (Series Book 3)  |  Author - Title (Series, #3)
    (re.compile(rf'^(?P<author>.+?)\s+-\s+(?P<title>.+?)\s*\((?P<series>[^)]+?)[,\s]+'
                rf'{_SERIES_WORD}?\s*(?P<index>\d{{1,3}})\)\s*$', re.I), 'author-title-(series-index)'),
    # Series 03 - Title
    (re.compile(rf'^(?P<series>.+?)\s+{_SERIES_WORD}?\s*(?P<index>\d{{1,3}})\s*[-–—:]\s*'
                rf'(?P<title>.+)$', re.I), 'series-index-title'),
    # [Series 03] Title
    (re.compile(rf'^\[(?P<series>[^\]]+?)\s+{_SERIES_WORD}?\s*(?P<index>\d{{1,3}})\]\s*'
                rf'(?P<title>.+)$', re.I), '[series-index]-title'),
    # Title (Series Book 3)
    (re.compile(rf'^(?P<title>.+?)\s*\((?P<series>[^)]+?)[,\s]+{_SERIES_WORD}\s*'
                rf'(?P<index>\d{{1,3}})\)\s*$', re.I), 'title-(series-book-index)'),
    # Book 3 - Title   /   Book 3 Title
    (re.compile(rf'^{_SERIES_WORD}\s*(?P<index>\d{{1,3}})\s*[-–—:.]?\s*(?P<title>.+)$', re.I),
     'book-index-title'),
    # Author - Title
    (re.compile(r'^(?P<author>[^-]+?)\s+-\s+(?P<title>.+)$'), 'author-title'),
    # 01. Title  /  01 - Title
    (re.compile(r'^(?P<index>\d{1,3})\s*[-.)]\s*(?P<title>.+)$'), 'index-title'),
]

# A folder named exactly like a person: "Brandon Sanderson", "J. R. R. Tolkien"
_AUTHOR_FOLDER = re.compile(
    r'^[A-Z][\w.\'-]*(?:\s+[A-Z][\w.\'-]*){1,3}$'
)

# Words that mark a name as a series/collection rather than a person, so that
# "The Bladeborn Saga" is not mistaken for an author.
_SERIES_WORDS = re.compile(
    r'\b(?:saga|series|trilogy|chronicles?|cycle|collection|duology|quartet|'
    r'omnibus|universe|adventures|tales|books|novels)\b', re.I)

# Filenames that carry no information - the folder is the real source of truth.
_GENERIC_STEMS = re.compile(
    r'^(?:book|audiobook|audio|full|complete|main|unabridged|abridged|part|pt|'
    r'disc|disk|cd|track|chapter|file|output|untitled|\d+)$', re.I)


def strip_noise(text: str) -> str:
    """Remove bitrates, years, narrator credits and format tags."""
    # Underscores first. Every pattern below is anchored on word boundaries, and an
    # underscore is a word character - so "random_file_128kbps" hid its bitrate from
    # the bitrate pattern, and "Author_-_Title" hid its separator, purely because the
    # separator happened to be "_" rather than " ".
    text = re.sub(r'[_]+', ' ', text)
    for pattern in _NOISE_PATTERNS:
        text = pattern.sub(' ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip(' -–—_.,')


def titlecase(text: str) -> str:
    """Title-case while leaving deliberate capitalisation and small words alone."""
    small = {'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'from', 'in', 'nor', 'of',
             'on', 'or', 'the', 'to', 'up', 'vs', 'via', 'with'}
    words = text.split()
    out = []
    for i, word in enumerate(words):
        # Leave anything already mixed-case or all-caps acronyms untouched.
        if not word.islower() and not word.isupper():
            out.append(word)
        elif word.isupper() and len(word) <= 4:
            out.append(word)
        elif i not in (0, len(words) - 1) and word.lower() in small:
            out.append(word.lower())
        else:
            out.append(word[:1].upper() + word[1:].lower() if word else word)
    return ' '.join(out)


def parse_name(name: str) -> Dict[str, str]:
    """Parse one folder or file name. Returns only the fields it is confident about."""
    raw = Path(name).stem if '.' in name[-6:] else name
    cleaned = strip_noise(raw)
    if not cleaned:
        return {}
    if _GENERIC_STEMS.match(cleaned):
        return {}  # "book.m4b" tells us nothing; let the folder speak instead

    # Try the raw name first: noise-stripping removes [brackets], which would destroy
    # a real "[Mistborn 02] Title" before it ever reached the patterns.
    for candidate in ([raw, cleaned] if raw != cleaned else [cleaned]):
        result = _match_patterns(candidate)
        if result:
            return result

    # No structure recognised - the whole thing is probably just a title.
    return {'title': titlecase(cleaned), '_pattern': 'bare-title'}


def _match_patterns(cleaned: str) -> Dict[str, str]:
    for pattern, label in _PATTERNS:
        match = pattern.match(cleaned.strip())
        if not match:
            continue
        groups = {k: v.strip(' -–—_.,') for k, v in match.groupdict().items() if v}
        # "02 - Ghost of the Shadowfort" matches the Author-Title shape, and nobody is
        # called 02. A numeric leading token is the position in a series, which is what
        # the index-title pattern would have said had it been tried first.
        if groups.get('author', '').isdigit() and not groups.get('index'):
            groups['index'] = groups.pop('author')
        result: Dict[str, str] = {}
        if groups.get('author') and not groups['author'].isdigit():
            result['author'] = titlecase(groups['author'])
        if groups.get('series'):
            series = re.sub(rf'\b{_SERIES_WORD}\b\s*$', '', groups['series'], flags=re.I).strip()
            if series:
                result['series'] = titlecase(series)
        if groups.get('index'):
            result['series_index'] = groups['index'].lstrip('0') or '0'
        if groups.get('title'):
            title = strip_noise(groups['title'])
            if title and not _GENERIC_STEMS.match(title):
                result['title'] = titlecase(title)
        if result:
            result['_pattern'] = label
            return result
    return {}


def parse_path(file_path: str, input_root: Optional[str] = None) -> Dict[str, str]:
    """Parse a full path, combining hints from the filename and its parent folders.

    Every component from the file up to (but excluding) the scan root is examined, not
    just the file and its immediate parent: in a real library the author lives two or
    three folders up and the series one, so throwing the rest of the path away throws
    away most of what is knowable for free.

    The filename is most specific, so it wins; parent folders fill in what it left
    empty, nearest folder first.

    Alongside the fields, the result carries three private keys the caller strips off:
    ``_pattern`` (which patterns matched), ``_considered`` (every component that was
    looked at, file first) and ``_from`` (field -> the component that supplied it), so
    the explanation panel can say *which* part of the path said what.
    """
    path = Path(file_path)
    result: Dict[str, str] = {}
    patterns: List[str] = []
    considered: List[str] = []
    origin: Dict[str, str] = {}

    def take(field: str, value: str, source: str) -> None:
        if value and field not in result:
            result[field] = value
            origin[field] = source

    considered.append(path.name)
    from_file = parse_name(path.stem)
    if from_file:
        patterns.append(f"file:{from_file.pop('_pattern', '')}")
        for key, value in from_file.items():
            take(key, value, path.name)

    # Walk outwards through parent folders, stopping at the scan root.
    root = Path(input_root).resolve() if input_root else None
    for parent in path.parents:
        if root and (parent == root or not _is_within(parent, root)):
            break

        name = parent.name
        if not name or name in ('/', '\\'):
            break
        considered.append(name)

        # A folder that is just a person's name is an author folder - unless it names a
        # series ("The Bladeborn Saga" is capitalised like a name but is not one).
        if (_AUTHOR_FOLDER.match(name) and not _has_digits(name)
                and not _SERIES_WORDS.search(name)):
            if 'author' not in result:
                take('author', titlecase(name), name)
                patterns.append('folder:author-name')
            continue

        if _SERIES_WORDS.search(name) and 'series' not in result:
            take('series', titlecase(strip_noise(name)), name)
            patterns.append('folder:series-name')
            continue

        from_folder = parse_name(name)
        folder_pattern = from_folder.pop('_pattern', '')
        folder_title = from_folder.pop('title', '')
        for key, value in from_folder.items():
            take(key, value, name)
        # A folder's "title" names the book only if we don't have one; if we already
        # have a title from the filename, the folder is naming the series instead.
        if folder_title:
            if 'title' not in result:
                take('title', folder_title, name)
            elif 'series' not in result and folder_title.lower() != result['title'].lower():
                take('series', folder_title, name)
        if folder_pattern and any(o == name for o in origin.values()):
            patterns.append(f'folder:{folder_pattern}')

    result['_pattern'] = ' | '.join(p for p in patterns if p.strip(': '))
    result['_considered'] = considered
    result['_from'] = origin
    return result


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _has_digits(text: str) -> bool:
    return any(c.isdigit() for c in text)
