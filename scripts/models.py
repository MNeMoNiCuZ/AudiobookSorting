"""Typed data model shared by every module.

A book's identifying fields are not plain strings: each carries where it came from and
how much we trust it, so the UI can colour-code them, the resolver can decide whether a
later tier is allowed to overwrite an earlier one, and review threshold actions have
something real to use.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# Resolution tiers, ordered worst to best. A tier may only overwrite a field held by a
# tier it outranks, or one whose confidence is lower.
SOURCE_CONFIDENCE = {
    '': 0.0,
    'guess': 0.25,
    'regex': 0.45,
    'search': 0.55,
    'llm': 0.60,
    'openlibrary': 0.70,
    'googlebooks': 0.75,
    'metadata': 0.75,
    'audnexus': 0.90,
    'user': 1.0,
}

IDENTITY_FIELDS = ('author', 'series', 'series_index', 'title')

STATUS_PENDING = 'pending'
STATUS_APPROVED = 'approved'
STATUS_REJECTED = 'rejected'
STATUS_APPLIED = 'applied'
STATUS_RISKY = 'risky'
STATUS_DUPLICATE = 'duplicate'


@dataclass
class Field:
    """One resolved value plus its provenance."""

    value: str = ''
    source: str = ''
    confidence: float = 0.0
    # Every tier that independently produced this same value. Agreement raises confidence.
    corroborated_by: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not str(self.value).strip()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_any(cls, data: Any) -> 'Field':
        """Accept a Field, a provenance dict, or a bare string (legacy save files)."""
        if isinstance(data, Field):
            return data
        if isinstance(data, dict):
            return cls(
                value=str(data.get('value', '') or ''),
                source=str(data.get('source', '') or ''),
                confidence=float(data.get('confidence', 0.0) or 0.0),
                corroborated_by=list(data.get('corroborated_by', [])),
            )
        value = '' if data is None else str(data)
        return cls(value=value, source='metadata' if value else '',
                   confidence=SOURCE_CONFIDENCE['metadata'] if value else 0.0)


@dataclass
class BookEntry:
    """One audiobook: what we found on disk, and what we think it is."""

    entry_id: str = ''
    # --- on disk
    folder: str = ''                      # directory containing the audio
    relative_path: str = ''
    audio_files: List[str] = field(default_factory=list)   # names, relative to folder
    # Byte size of each file in audio_files, as it was when the folder was scanned.
    # Recorded so that "does the table still describe what is on disk?" can be answered
    # without re-reading anything: a file replaced by a different copy keeps its name
    # and its path, and its size is the only cheap thing about it that changes.
    audio_sizes: List[int] = field(default_factory=list)
    primary_audio: str = ''               # absolute path to the representative file
    image_files: List[str] = field(default_factory=list)
    is_multi_book_folder: bool = False    # siblings in this folder are other books

    # --- identity
    author: Field = field(default_factory=Field)
    series: Field = field(default_factory=Field)
    series_index: Field = field(default_factory=Field)
    title: Field = field(default_factory=Field)

    # --- review state
    status: str = STATUS_PENDING
    applied_path: str = ''
    duplicate_of: str = ''
    notes: str = ''

    # --- diagnostics, shown in the "why" panel
    raw_tags: Dict[str, str] = field(default_factory=dict)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    # What the database and web tiers turned up, kept even when nothing scored high
    # enough to apply. The LLM tier reads this: a rejected candidate or an unparsed
    # search snippet is still evidence, and throwing it away was wasteful.
    evidence: Dict[str, Any] = field(default_factory=dict)
    # Values a tier wanted to write over something you typed yourself. Never applied
    # silently - the window asks, one row per proposal, and defaults to "no".
    pending_overwrites: List[Dict[str, str]] = field(default_factory=list)
    # "Dirty output" notes from scripts.quality - values that parse fine but read
    # wrong (an unclosed bracket, "(Unabridged)", an author in capitals). Refreshed
    # on every resolve; the confidence of the fields involved is docked to match.
    warnings: List[str] = field(default_factory=list)
    warnings_silenced: bool = False
    warnings_checked_values: Dict[str, str] = field(default_factory=dict)
    # field name -> the factor its confidence has already been multiplied by for the
    # warnings above. Kept so re-running identification re-judges the value instead of
    # docking it a second time; without this, resolving the same book four times would
    # take a perfectly good 90% down to 30% on no new evidence.
    quality_penalties: Dict[str, Any] = field(default_factory=dict)
    resolved: bool = False
    # Set only when a user-requested Identify or individual source run actually starts.
    # The automatic metadata and filename pass performed by Load Input does not count.
    explicit_work_pending: bool = False

    # ------------------------------------------------------------------ helpers

    def get_field(self, name: str) -> Field:
        return getattr(self, name)

    def value(self, name: str) -> str:
        return str(getattr(self, name).value or '')

    def set_field(self, name: str, value: Any, source: str,
                  confidence: Optional[float] = None) -> bool:
        """Set a field if the new value is better-sourced than what's there.

        Returns True if the entry changed. A value from a tier that agrees with what we
        already have does not overwrite, it corroborates - and raises confidence.
        """
        value = clean_value(name, value)
        if not value:
            return False

        current: Field = getattr(self, name)
        new_confidence = SOURCE_CONFIDENCE.get(source, 0.5) if confidence is None else confidence

        if not current.is_empty():
            # A value you typed is the one value here that is known rather than
            # inferred. No tier may quietly replace it; the disagreement is recorded
            # and put to you instead.
            if (current.source == 'user' and source != 'user'
                    and _norm(current.value) != _norm(value)):
                proposal = {'field': name, 'before': str(current.value),
                            'after': value, 'source': source}
                if proposal not in self.pending_overwrites:
                    self.pending_overwrites.append(proposal)
                return False
            if _norm(current.value) == _norm(value):
                if source not in current.corroborated_by and source != current.source:
                    current.corroborated_by.append(source)
                    # Independent agreement closes part of the gap to certainty.
                    current.confidence = min(0.99, current.confidence +
                                             (1.0 - current.confidence) * 0.4)
                    return True
                return False
            if new_confidence <= current.confidence:
                return False  # keep the better-sourced value, but record the disagreement
        setattr(self, name, Field(value=value, source=source, confidence=new_confidence))
        return True

    def confidence(self) -> float:
        """How much we trust the identity we are about to file this book under.

        Author and title carry most of the score. Series context contributes without
        allowing one lower-confidence field to replace the confidence of the entire
        book. Missing core identity still matters; a missing optional series does not.
        """
        author = self.author.confidence if not self.author.is_empty() else 0.0
        title = self.title.confidence if not self.title.is_empty() else 0.0
        if self.series.is_empty():
            result = author * 0.5 + title * 0.5
        else:
            series = self.series.confidence
            index = (self.series_index.confidence
                     if not self.series_index.is_empty() else 0.0)
            result = author * 0.4 + title * 0.4 + series * 0.15 + index * 0.05
        return round(result, 3)

    def force_field(self, name: str, value: Any, source: str,
                    confidence: Optional[float] = None) -> bool:
        """Write a field even over a user edit. Only ever called after you agree."""
        value = clean_value(name, value)
        if not value:
            return False
        confidence = (SOURCE_CONFIDENCE.get(source, 0.5)
                      if confidence is None else confidence)
        setattr(self, name, Field(value=value, source=source, confidence=confidence))
        self.pending_overwrites = [p for p in self.pending_overwrites
                                   if p.get('field') != name]
        return True

    def missing_fields(self) -> List[str]:
        return [name for name in IDENTITY_FIELDS if getattr(self, name).is_empty()]

    def is_complete(self) -> bool:
        return not self.author.is_empty() and not self.title.is_empty()

    def dedupe_key(self) -> str:
        return f'{_norm(self.value("author"))}|{_norm(self.value("title"))}'

    def log(self, tier: str, message: str, data: Any = None) -> None:
        self.trace.append({'tier': tier, 'message': message, 'data': data})

    def begin_tier(self, tier: str) -> None:
        """Drop what this tier said last time, because it is about to say it again.

        Re-running a tier used to append a second identical paragraph, then a third,
        so the panel filled with repeats of the same sentence. A tier's trace is the
        result of its *most recent* run, not a history of every run.
        """
        self.trace = [step for step in self.trace if step.get('tier') != tier]

    def absolute_files(self) -> List[Path]:
        base = Path(self.folder)
        return [base / name for name in self.audio_files]

    # ---------------------------------------------------------- (de)serialisation

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        for name in IDENTITY_FIELDS:
            data[name] = getattr(self, name).to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BookEntry':
        known = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in data.items() if k in known}
        for name in IDENTITY_FIELDS:
            kwargs[name] = Field.from_any(data.get(name))
        # Tolerate save files written by the pre-dataclass version.
        if 'primary_audio' not in kwargs and data.get('full_audio_path'):
            kwargs['primary_audio'] = data['full_audio_path']
        if 'folder' not in kwargs and kwargs.get('primary_audio'):
            kwargs['folder'] = str(Path(kwargs['primary_audio']).parent)
        return cls(**kwargs)


def pretty_status(status: str) -> str:
    """The display form of a status. Stored lower-case, shown Title Case.

    The stored value is an identifier and stays lower-case forever; every place a
    human reads one goes through here so they all agree.
    """
    labels = {'risky': 'Unsure'}
    return labels.get(str(status or '').lower(),
                      str(status or '').replace('_', ' ').title())


# Text tidying that is a matter of taste rather than correctness: the words you never
# want to see in a name ("Series", "Unabridged"), and whether bracketed asides are
# kept. Module-level, and set once at start-up from AO_BLOCKED_WORDS and
# AO_STRIP_PARENTHESES, because clean_value is called from everywhere and has no
# Settings to hand - the same reasoning as paths.set_illegal_char_mode.
_blocked: tuple = ()
_strip_parentheses = True
_tidy_punctuation = True

# The fields a name filter has any business touching. Numbers are not names.
FILTERED_FIELDS = ('author', 'series', 'title')


def set_text_filters(blocked: Any = '', strip_parentheses: bool = True,
                     tidy_punctuation: bool = True) -> None:
    """Configure the blocked words and bracket stripping for the whole process."""
    global _blocked, _strip_parentheses, _tidy_punctuation
    _tidy_punctuation = bool(tidy_punctuation)
    if isinstance(blocked, str):
        # A comma-separated list is what people type; newlines are what they get when
        # they paste one. Both work, and a term may be several words long.
        terms = re.split(r'[,\n;]+', blocked)
    else:
        terms = list(blocked or ())
    _blocked = tuple(term.strip() for term in terms if term and term.strip())
    _strip_parentheses = bool(strip_parentheses)


def blocked_words() -> tuple:
    return _blocked


def strip_parentheses_enabled() -> bool:
    return _strip_parentheses


def tidy_punctuation_enabled() -> bool:
    return _tidy_punctuation


# Brackets, and the punctuation a removed word can leave stranded. The backslash is
# there so the hyphen is a literal inside the character classes these build.
_CLOSER_OF = {'(': ')', '[': ']', '{': '}'}
_OPENER_OF = {v: k for k, v in _CLOSER_OF.items()}
_SEPARATOR = ',;:\\-–—'


def _remove_brackets(text: str, inner: str) -> str:
    """Delete bracketed spans whose contents match ``inner``, innermost-out.

    Two passes per round because a bracket wedged between two words is a word
    boundary: dropping it from "Title(Unabridged)Sub" must leave "Title Sub", not
    "TitleSub", while the ordinary case takes the space in front of it with it.
    """
    span = r'[\(\[\{]%s[\)\]\}]' % inner
    while True:
        shrunk = re.sub(r'(?<=\w)\s*%s\s*(?=\w)' % span, ' ', text)
        shrunk = re.sub(r'\s*%s' % span, '', shrunk)
        if shrunk == text:
            return text
        text = shrunk


def _drop_unbalanced_brackets(text: str) -> str:
    """Remove brackets with no partner: "Mistborn (" is a wound, not a name."""
    stack: list = []
    doomed: set = set()
    for index, char in enumerate(text):
        if char in _CLOSER_OF:
            stack.append(index)
        elif char in _OPENER_OF:
            if stack and text[stack[-1]] == _OPENER_OF[char]:
                stack.pop()
            else:
                doomed.add(index)
    doomed.update(stack)
    if not doomed:
        return text
    return ''.join(c for i, c in enumerate(text) if i not in doomed)


def tidy_text(text: str) -> str:
    """The last-step tidy: what is left once words have been struck out of a name.

    Blocking "unabridged" in "Mistborn (Unabridged) - Book 1" empties the brackets
    and orphans the dash. This closes those seams - empty and half-open brackets,
    doubled separators, space before a comma - without touching punctuation that is
    part of the name, so "Spider-Man" and "Vol. 2" come through unchanged.
    """
    # Emptied brackets go first, innermost-out: clearing "(Book One ())" leaves
    # "(Book One)" on the first pass and needs a second to see nothing is left.
    text = _remove_brackets(text, r'[\s%s._·|/]*' % _SEPARATOR)
    text = _drop_unbalanced_brackets(text)
    if text.count('"') == 1:
        text = text.replace('"', '')

    # A run of separators is the scar of a removed word; the first one survives.
    text = re.sub(r'([%s])(?:\s*[%s])+' % (_SEPARATOR, _SEPARATOR), r'\1', text)

    # A separator running into a full stop loses to the full stop: "Smith,." is "Smith."
    text = re.sub(r'[%s]+\s*([.!?])' % _SEPARATOR, r'\1', text)
    text = re.sub(r'\s*([,;:])\s*', r'\1 ', text)
    text = re.sub(r'\s+([.!?])', r'\1', text)
    text = re.sub(r'([\(\[\{])\s+', r'\1', text)
    text = re.sub(r'\s+([\)\]\}])', r'\1', text)
    text = re.sub(r'(\w)([\(\[\{])', r'\1 \2', text)
    # A separator against a bracket edge had its other side removed with the word.
    text = re.sub(r'([\(\[\{])[\s%s]+' % _SEPARATOR, r'\1', text)
    text = re.sub(r'[\s%s]+([\)\]\}])' % _SEPARATOR, r'\1', text)
    text = re.sub(r'\s{2,}', ' ', text)
    # Trailing "." and "!" stay: "Jr." and "Hello!" are how the name is written.
    text = re.sub(r'^[\s%s._]+|[\s%s_]+$' % (_SEPARATOR, _SEPARATOR), '', text)
    return text.strip()


def apply_text_filters(text: str) -> str:
    """Drop bracketed asides and blocked terms from one name, then tidy the seam.

    "The Expanse Series" with "series" blocked is "The Expanse", not "The Expanse "
    or "The  Expanse". Removing every word of a value leaves nothing rather than
    punctuation, and the caller treats that as an empty field.
    """
    if _strip_parentheses:
        text = _remove_brackets(text, r'[^\(\)\[\]\{\}]*')

    for term in _blocked:
        # Whole words only: blocking "series" must not gut "Seriously".
        pattern = r'(?<!\w)%s(?!\w)' % r'\W+'.join(
            re.escape(word) for word in term.split())
        text = re.sub(pattern, ' ', text, flags=re.IGNORECASE)

    text = re.sub(r'\s{2,}', ' ', text)
    # Punctuation left hanging where the removed words used to be.
    text = re.sub(r'\s+([,;:.!?])', r'\1', text)
    text = re.sub(r'^[\s\-–—_,:;.]+|[\s\-–—_,:;]+$', '', text)
    return text.strip()


def clean_value(name: str, value: Any) -> str:
    """Normalise a candidate value, rejecting the junk models and tags like to emit."""
    if value is None:
        return ''
    text = str(value).strip().strip('"\'')
    if text.lower() in ('', 'none', 'null', 'unknown', 'n/a', 'na', 'undefined',
                        'unknown author', 'unknown title', 'various', 'untitled'):
        return ''

    if name == 'series_index':
        # A bundled omnibus is one entry covering several books, so the number is a
        # range: "1-3", "01 - 03", "1 to 3" all mean the same thing and are stored
        # unpadded as "1-3". Padding is an output concern, applied when the template
        # is rendered, so the stored value never carries someone's typed zeroes.
        span = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:-|–|—|to|through)\s*(\d+(?:[.,]\d+)?)',
                         text, re.IGNORECASE)
        if span:
            first, last = (_clean_index(span.group(1)), _clean_index(span.group(2)))
            if first and last:
                return first if first == last else f'{first}-{last}'
            return ''
        match = re.search(r'\d+(?:[.,]\d+)?', text)
        return _clean_index(match.group()) if match else ''

    text = re.sub(r'\s+', ' ', text)
    if name in FILTERED_FIELDS:
        text = apply_text_filters(text)
        if _tidy_punctuation:
            text = tidy_text(text)
    return text


def _clean_index(text: str) -> str:
    """One book number, normalised: no padding, no trailing ".0", 0-999 or nothing."""
    number = float(text.replace(',', '.'))
    if number < 0 or number > 999:
        return ''
    return str(int(number)) if number.is_integer() else str(number)


def _norm(text: str) -> str:
    """Aggressive normalisation used for comparison only, never for display."""
    text = unicodedata.normalize('NFKD', str(text)).encode('ascii', 'ignore').decode()
    text = text.lower()
    text = re.sub(r'^(the|a|an)\s+', '', text)
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    return text.strip()


def normalize(text: str) -> str:
    """Public alias of the comparison normaliser."""
    return _norm(text)
