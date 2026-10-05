"""Split a file name into the part it shares with its folder siblings and its own part.

Ten files named "01 The Chalk Closet - Even More Tales..." through "10 The Thumbprint
of Doom - Even More Tales..." say, by their shape alone, that "Even More Tales..." is
what the books have in common and "The Chalk Closet" is what makes this one itself.
A model shown the raw names tends to collapse every one of them into the shared name;
handing it the split as evidence works whatever the series is.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_EDGE_JUNK = ' \t-_–—.,;:()[]{}&+'


def _tokens(text: str) -> List[Tuple[str, int, int]]:
    """(normalised word, start, end) for every word in `text`."""
    return [(m.group(0).lower().replace('’', "'"), m.start(), m.end())
            for m in _TOKEN.finditer(text)]


def _clean(text: str) -> str:
    text = re.sub(r'\s+', ' ', text)
    for opening, closing in ('()', '[]', '{}'):
        if text.count(opening) != text.count(closing):
            text = text.replace(opening, ' ').replace(closing, ' ')
    return re.sub(r'\s+', ' ', text).strip(_EDGE_JUNK).strip()


def compare_names(name: str, siblings: Sequence[str]) -> Dict[str, str]:
    """Return ``{"own", "shared", "number"}`` for `name` against its siblings.

    `own` is the text found only in this name, `shared` the text most siblings carry
    too, `number` a leading track or book number. Empty dict when there are too few
    siblings to tell anything apart, or when nothing is shared or nothing is unique.
    """
    others = [set(word for word, _s, _e in _tokens(sibling)) for sibling in siblings]
    others = [words for words in others if words]
    if not others:
        return {}
    # A word is "shared" when most siblings have it. Requiring all of them would let
    # one oddly named file ("08 For the Birds - ... Audio Book Reading" without the
    # brackets) hide the pattern; a bare majority would let "the" in two titles count.
    needed = max(1, -(-len(others) * 2 // 3))

    tokens = _tokens(name)
    if not tokens:
        return {}

    marks: List[Optional[bool]] = [sum(word in words for words in others) >= needed
                                   for word, _s, _e in tokens]

    # The first number that differs between siblings is this file's position:
    # "01 The Chalk Closet", "Mistborn 02 - The Well of Ascension".
    number = ''
    for i, (word, _s, _e) in enumerate(tokens):
        if word.isdigit() and not marks[i]:
            number = word.lstrip('0') or '0'
            marks[i] = None  # neither part: it splits the runs on either side
            break

    # A lone shared word between unique words ("Attack OF THE Christmas Present") is
    # part of the unique run, not a separate shared fragment. So is a short word such
    # as "The" that only happens to start many of the titles.
    for i, mark in enumerate(marks):
        if mark is not True:
            continue
        before = i == 0 or marks[i - 1] is not True
        after = i == len(marks) - 1 or marks[i + 1] is not True
        if before and after and (0 < i < len(marks) - 1 or len(tokens[i][0]) <= 3):
            marks[i] = False
    runs: List[Tuple[Optional[bool], int, int]] = []
    for mark, (_word, start, end) in zip(marks, tokens):
        if runs and runs[-1][0] is mark:
            runs[-1] = (mark, runs[-1][1], end)
        else:
            runs.append((mark, start, end))

    own = [_clean(name[start:end]) for mark, start, end in runs if mark is False]
    shared = [_clean(name[start:end]) for mark, start, end in runs if mark is True]
    own = [part for part in own if part]
    shared = [part for part in shared if part]
    if not own or not shared:
        return {}
    return {'own': ' / '.join(own), 'shared': ' / '.join(shared), 'number': number}
