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

# How much of the field's confidence each kind of finding costs. Multiplicative, so
# two problems on one field compound rather than cancelling out.
_PENALTY = {
    'brackets': 0.65,
    'html': 0.55,
    'file_words': 0.70,
    'punctuation': 0.80,
    'edges': 0.85,
    'truncated': 0.70,
    'shouting': 0.85,
    'numeric': 0.50,
    'length': 0.70,
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


def inspect_value(field: str, value: str) -> List[Finding]:
    """Everything suspicious about one field's value. Empty list means it looks fine."""
    text = str(value or '').strip()
    if not text:
        return []

    strict = field in _STRICT_FIELDS
    found: List[Finding] = []

    for opener, closer in _PAIRS:
        if text.count(opener) != text.count(closer):
            found.append(Finding(
                field, 'brackets',
                f'{field}: "{text}" has an unclosed {opener}{closer} - the search '
                f'result was probably cut off mid-phrase'))
            break

    if _ENTITY.search(text) or _TAG.search(text):
        found.append(Finding(
            field, 'html',
            f'{field}: "{text}" still contains raw HTML - it was scraped from a page '
            f'and never decoded'))

    match = _FILE_WORDS.search(text)
    if match:
        found.append(Finding(
            field, 'file_words',
            f'{field}: "{text}" contains "{match.group().strip()}", which describes '
            f'the file rather than the book'))

    if _PUNCT_RUN.search(text):
        found.append(Finding(
            field, 'punctuation',
            f'{field}: "{text}" has a run of repeated punctuation'))

    if _ODD_EDGE.search(text):
        found.append(Finding(
            field, 'edges',
            f'{field}: "{text}" begins or ends with stray punctuation'))

    if _TRUNCATED.search(text):
        found.append(Finding(
            field, 'truncated',
            f'{field}: "{text}" looks truncated'))

    letters = [c for c in text if c.isalpha()]
    if (strict and len(letters) > 4
            and all(c.isupper() for c in letters) and ' ' in text):
        found.append(Finding(
            field, 'shouting',
            f'{field}: "{text}" is entirely upper case - that is how a tag dump looks, '
            f'not how a name is written'))

    if strict and letters == [] and any(c.isdigit() for c in text):
        found.append(Finding(
            field, 'numeric',
            f'{field}: "{text}" is only digits, which is not a name'))

    if len(text) > (60 if strict else 180):
        found.append(Finding(
            field, 'length',
            f'{field}: "{text[:60]}..." is {len(text)} characters long - a whole '
            f'sentence was captured instead of a value'))

    return found


def inspect_entry(entry) -> List[Finding]:
    """Every finding across a book's four identity fields."""
    findings: List[Finding] = []
    for name in ('author', 'series', 'title'):
        findings.extend(inspect_value(name, entry.value(name)))
    return findings


def penalties(findings: List[Finding]) -> Dict[str, float]:
    """field -> the multiplier its confidence should be scaled by (0..1)."""
    factors: Dict[str, float] = {}
    for finding in findings:
        factors[finding.field] = factors.get(finding.field, 1.0) * finding.penalty
    return factors
