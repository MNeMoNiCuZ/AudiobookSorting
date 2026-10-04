"""Spotting values that look like a scrape went wrong ("dirty output").

A web search or a loosely-matched database row can return something that is
*syntactically* a title and *obviously* wrong to a human: an unclosed bracket, a
half-stripped HTML entity, "(Unabridged)" left on the end, an author in shouting
capitals. None of that is an error anywhere in the pipeline - every tier did what it
was told - so nothing catches it, and it gets filed under that name.

So it is checked here, once, at the end of resolution. Each finding is a short
sentence naming the field and what is odd about it, plus a confidence penalty. The
penalty matters more than the message: a dirty value that keeps its 90% sails past
the review threshold, and a book you never look at is a book filed wrong.

Nothing here rewrites a value. Guessing at what the author *meant* to scrape is how
you turn a visible problem into an invisible one.
"""

from __future__ import annotations

import re
import html as html_lib
from typing import Dict, List, NamedTuple

# Bracket pairs that must balance within one field.
_PAIRS = (('(', ')'), ('[', ']'), ('{', '}'))

# Left over from HTML that was never decoded, or was decoded halfway.
_ENTITY = re.compile(r'&(?:[a-zA-Z]{2,10}|#\d{2,5});')
_TAG = re.compile(r'</?[a-zA-Z][^>]{0,20}>')

# Words that describe the *file*, not the book. They belong in no identity field.
_FILE_WORDS = re.compile(
    r'\b(unabridged|abridged|audiobook|audio\s*book|mp3|m4b|m4a|flac|aac|ogg|'
    r'\d{2,3}\s*kbps|\d{2,3}k|vbr|cbr|retail|rip|torrent|part\s*\d+\s*of\s*\d+|'
    r'narrated\s+by|read\s+by|www\.|https?://)\b', re.I)

# Three or more of the same punctuation mark in a row, or punctuation soup.
_PUNCT_RUN = re.compile(r'([-–—_.,;:!?*+/\\])\1{2,}')
_ODD_EDGE = re.compile(r'^[\s\-–—_,;:.!?)\]}]+|[\s\-–—_,;:(\[{]+$')

# A truncated scrape almost always ends in one of these.
_TRUNCATED = re.compile(r'(\.{3}|…|\bet al\b|\betc\.?$)', re.I)
_TITLE_FILE_SEGMENT = re.compile(
    r'\b(?:disc|disk|cd|track|file)\s*\d+\b', re.I)
_CHAPTER_FRACTION = re.compile(r'(?:^|[-_\s])\d{1,3}\s*/\s*\d{1,3}(?:$|\s)')
_SPECIAL_SEPARATOR = re.compile(r'[|/\\"]')
_EMBEDDED_FILE_WORD = re.compile(
    r'(?:unabridged|abridged|audiobook|mp3\d*|m4[ab]|flac)', re.I)
# A title ending in a bare, unpadded number ("Mistborn 2") has the book number stuck
# on it; the numbering belongs in the series index, and "02" is the padded form.
_TRAILING_NUMBER = re.compile(r'(?<!\bpart )(?<!\bvolume )(?<!\bvol\. )(?<=\s)[1-9]\d{0,2}$', re.I)
# A title that is nothing but a label and a number names no book: "Volume 3",
# "Book 2", "Chapter 01", "Part 4". A bare number is left alone: "1984" is a title.
PLACEHOLDER_TITLE = re.compile(
    r'^(?:(?:book|bk|volume|vol|part|pt|chapter|ch|episode|ep|disc|disk|cd|track|'
    r'section|file|number|no)\.?\s*#?\s*)(?:\d{1,4}|[ivxlc]{1,6}|one|two|three|four|'
    r'five|six|seven|eight|nine|ten)$', re.I)
_JOINED_WORDS = re.compile(r'^[A-Za-z]{24,}$')
_SINGLE_LETTER_NAME = re.compile(r'(?<![\w.])([A-Za-z])(?![\w.])')
_SPACED_INITIALS = re.compile(r'(?<=\b[A-Za-z]\.)\s+(?=[A-Za-z]\.)')
_COMPACT_INITIALS = re.compile(r'(?<=\b[A-Za-z]\.)(?=[A-Za-z]\.)')
# The surname run onto the last initial: "J.R.R.Tolkien". The lookbehind wants a
# single letter, so "St.Clair" and "Dr.Who" are none of its business.
_INITIAL_THEN_NAME = re.compile(r'(?<=\b[A-Za-z]\.)(?=[A-Za-z]{2,})')
_author_initial_style = 'compact'
# Anything that joins two names: "A, B", "A; B", "A & B", "A and B". One author per book.
_AUTHOR_JOIN = re.compile(r',|;|&|\band\b', re.I)

# How much of the field's confidence each kind of finding costs. Multiplicative, so
# two problems on one field compound rather than cancelling out.
_PENALTY = {
    'brackets': 0.65,
    'html': 0.55,
    'file_words': 0.70,
    'punctuation': 0.80,
    'edges': 0.85,
    'truncated': 0.70,
    'shouting': 0.70,
    'trailing_number': 0.60,
    'placeholder_title': 0.30,
    'length': 0.70,
    'file_segment': 0.75,
    'chapter_fraction': 0.55,
    'separator': 0.70,
    'joined_words': 0.70,
    'author_initials': 0.85,
    'multiple_authors': 0.50,
    'duplicate_identity': 1.0,
}

# Fields where an unusual character is genuinely unusual. Titles legitimately carry
# brackets and colons far more often than an author or a series name does, so a title
# is judged more leniently - see `strict` below.
_STRICT_FIELDS = ('author', 'series')


class Finding(NamedTuple):
    """One thing that looks wrong with one field."""

    field: str
    kind: str
    message: str

    @property
    def penalty(self) -> float:
        return _PENALTY.get(self.kind, 0.85)


def set_author_initial_style(style: str) -> None:
    """Choose compact consecutive initials or a space between each initial."""
    global _author_initial_style
    _author_initial_style = style if style in ('compact', 'spaced') else 'compact'


def author_initial_style() -> str:
    """Return the active style so cached warning checks include the preference."""
    return _author_initial_style


def format_author_initials(author: str) -> str:
    """Add missing periods and apply the configured consecutive-initial style."""
    text = _SINGLE_LETTER_NAME.sub(r'\1.', str(author or '').strip())
    if _author_initial_style == 'spaced':
        text = _COMPACT_INITIALS.sub(' ', text)
    else:
        text = _SPACED_INITIALS.sub('', text)
    return _INITIAL_THEN_NAME.sub(' ', text)


def has_multiple_authors(author: str) -> bool:
    """True when the author field names more than one person."""
    return bool(_AUTHOR_JOIN.search(str(author or '')))


def inspect_value(field: str, value: str) -> List[Finding]:
    """Everything suspicious about one field's value. Empty list means it looks fine."""
    text = str(value or '').strip()
    if not text:
        return []

    strict = field in _STRICT_FIELDS
    found: List[Finding] = []
    letters = [c for c in text if c.isalpha()]

    if field == 'author' and format_author_initials(text) != text:
        style = 'spaces between consecutive initials' if _author_initial_style == 'spaced' \
            else 'no spaces between consecutive initials'
        found.append(Finding(
            field, 'author_initials',
            f'Author initials should use periods and {style}'))

    if field == 'author' and has_multiple_authors(text):
        found.append(Finding(
            field, 'multiple_authors',
            'Author contains a comma - only one author is allowed' if ',' in text
            else 'Author lists more than one author - only one is allowed'))

    for opener, closer in _PAIRS:
        if text.count(opener) != text.count(closer):
            found.append(Finding(
                field, 'brackets',
                f'{field.replace("_", " ").title()} has unclosed brackets'))
            break

    if _ENTITY.search(text) or _TAG.search(text):
        found.append(Finding(
            field, 'html',
            f'{field.replace("_", " ").title()} contains HTML'))

    match = _FILE_WORDS.search(text) or _EMBEDDED_FILE_WORD.search(text)
    if match:
        found.append(Finding(
            field, 'file_words',
            f'{field.replace("_", " ").title()} contains file details'))

    if _PUNCT_RUN.search(text):
        found.append(Finding(
            field, 'punctuation',
            f'{field.replace("_", " ").title()} has repeated punctuation'))

    if _ODD_EDGE.search(text):
        found.append(Finding(
            field, 'edges',
            f'{field.replace("_", " ").title()} has stray punctuation'))

    if _TRUNCATED.search(text):
        found.append(Finding(
            field, 'truncated',
            f'{field.replace("_", " ").title()} looks incomplete'))

    # A title that is only the label gets the stronger finding below instead.
    if (field == 'title' and _TITLE_FILE_SEGMENT.search(text)
            and not PLACEHOLDER_TITLE.fullmatch(text)):
        found.append(Finding(
            field, 'file_segment', 'Title looks like a file segment'))

    if field == 'title' and _CHAPTER_FRACTION.search(text):
        found.append(Finding(
            field, 'chapter_fraction', 'Title looks like a chapter count'))

    if _SPECIAL_SEPARATOR.search(text):
        found.append(Finding(
            field, 'separator',
            f'{field.replace("_", " ").title()} contains unusual separators'))

    if field == 'title' and _JOINED_WORDS.fullmatch(text) and any(c.isupper() for c in text[1:]):
        found.append(Finding(
            field, 'joined_words', 'Title may contain joined words'))

    if field == 'title' and PLACEHOLDER_TITLE.fullmatch(text):
        found.append(Finding(
            field, 'placeholder_title',
            'Title is only a label and a number - it does not name the book'))

    if (field == 'title' and _TRAILING_NUMBER.search(text)
            and not PLACEHOLDER_TITLE.fullmatch(text)):
        found.append(Finding(
            field, 'trailing_number',
            'Title ends in a book number - move it to the series index or pad it (02)'))

    if (len(letters) > 3 and all(c.isupper() for c in letters)
            and (strict or ' ' in text or len(letters) > 5)):
        found.append(Finding(
            field, 'shouting',
            f'{field.replace("_", " ").title()} is all uppercase - likely wrong'))

    if len(text) > (60 if strict else 180):
        found.append(Finding(
            field, 'length',
            f'{field.replace("_", " ").title()} is very long'))

    return found


def identity_key(entry):
    """A complete book identity; empty series and number allow standalone books."""
    values = tuple(' '.join(str(entry.value(name) or '').split()).casefold()
                   for name in ('author', 'series', 'series_index', 'title'))
    return values if values[0] and values[3] else None


def inspect_entry(entry, entries=None, *, duplicate_ids=None) -> List[Finding]:
    """Every finding across a book's four identity fields."""
    findings: List[Finding] = []
    for name in ('author', 'series', 'title'):
        findings.extend(inspect_value(name, entry.value(name)))
    if duplicate_ids is None:
        key = identity_key(entry)
        duplicate_ids = [other.entry_id for other in entries or ()
                         if key is not None and other.entry_id != entry.entry_id
                         and identity_key(other) == key]
    if duplicate_ids:
        findings.append(Finding(
            'title', 'duplicate_identity',
            'Duplicate book identity: Author, Series, # and Title match '
            f'{len(duplicate_ids)} other book(s)'))
    return findings


def penalties(findings: List[Finding]) -> Dict[str, float]:
    """field -> the multiplier its confidence should be scaled by (0..1)."""
    factors: Dict[str, float] = {}
    for finding in findings:
        factors[finding.field] = factors.get(finding.field, 1.0) * finding.penalty
    return factors


def suggest_fix(field: str, value: str, kind: str) -> str:
    """Conservative, editable cleanup proposed for one quality finding."""
    text = str(value or '')
    if kind == 'duplicate_identity':
        return text
    if kind == 'brackets':
        for opener, closer in _PAIRS:
            if text.count(opener) != text.count(closer):
                text = text.replace(opener, '').replace(closer, '')
    elif kind == 'html':
        text = html_lib.unescape(_TAG.sub('', text))
    elif kind == 'file_words':
        text = _EMBEDDED_FILE_WORD.sub(' ', _FILE_WORDS.sub(' ', text))
    elif kind == 'punctuation':
        text = _PUNCT_RUN.sub(lambda match: match.group(1), text)
    elif kind == 'edges':
        text = _ODD_EDGE.sub('', text)
    elif kind == 'truncated':
        text = _TRUNCATED.sub('', text)
    elif kind == 'shouting':
        text = text.title()
    elif kind == 'trailing_number':
        text = _TRAILING_NUMBER.sub(lambda m: m.group(0).zfill(2), text)
    elif kind == 'file_segment':
        text = _TITLE_FILE_SEGMENT.sub(' ', text)
    elif kind == 'chapter_fraction':
        text = _CHAPTER_FRACTION.sub(' ', text)
    elif kind == 'separator':
        text = _SPECIAL_SEPARATOR.sub(' ', text)
    elif kind == 'author_initials':
        text = format_author_initials(text)
    elif kind == 'multiple_authors':
        text = _AUTHOR_JOIN.split(text)[0]
    empty_brackets = re.compile(r'\(\s*\)|\[\s*\]|\{\s*\}')
    while empty_brackets.search(text):
        text = empty_brackets.sub(' ', text)
    text = re.sub(r'\s+([,;:.!?])', r'\1', text)
    return re.sub(r'\s+', ' ', text).strip(' -_,;:.')
