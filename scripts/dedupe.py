"""Duplicate detection across the library (#28).

Two entries are the same book when their normalised author+title agree. Duration is
used as a tie-breaker: the same title from the same author at wildly different lengths
is usually an abridged edition or a different work, not a duplicate.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Tuple

from .api_query import author_similarity, similarity
from .models import STATUS_DUPLICATE, BookEntry, normalize

logger = logging.getLogger(__name__)

# Two recordings of one book rarely differ by more than a third in length.
_DURATION_TOLERANCE = 0.35


def find_duplicates(entries: Iterable[BookEntry],
                    fuzzy: bool = True) -> Dict[str, List[str]]:
    """Map each duplicate entry_id to the ids of the entries it duplicates."""
    entries = [e for e in entries if e.is_complete()]
    duplicates: Dict[str, List[str]] = {}

    # Exact bucket first: normalised author+title.
    buckets: Dict[str, List[BookEntry]] = {}
    for entry in entries:
        buckets.setdefault(entry.dedupe_key(), []).append(entry)

    for group in buckets.values():
        if len(group) < 2:
            continue
        for entry in group[1:]:
            if _durations_compatible(group[0], entry):
                duplicates.setdefault(entry.entry_id, []).append(group[0].entry_id)

    if not fuzzy:
        return duplicates

    # Fuzzy pass across bucket representatives, to catch spelling variations.
    representatives = [group[0] for group in buckets.values()]
    for i, left in enumerate(representatives):
        for right in representatives[i + 1:]:
            if left.dedupe_key() == right.dedupe_key():
                continue
            if (similarity(left.value('title'), right.value('title')) >= 0.92
                    and author_similarity(left.value('author'),
                                          right.value('author')) >= 0.85
                    and _durations_compatible(left, right)):
                # Keep the higher-confidence one as the original.
                original, duplicate = ((left, right)
                                       if left.confidence() >= right.confidence()
                                       else (right, left))
                duplicates.setdefault(duplicate.entry_id, []).append(original.entry_id)

    return duplicates


def mark_duplicates(entries: Iterable[BookEntry], fuzzy: bool = True) -> int:
    """Flag duplicates in place. Returns how many were flagged."""
    entries = list(entries)
    found = find_duplicates(entries, fuzzy=fuzzy)
    by_id = {e.entry_id: e for e in entries}

    for entry_id, originals in found.items():
        entry = by_id.get(entry_id)
        if entry is None or entry.status in ('applied', 'rejected'):
            continue
        entry.duplicate_of = originals[0]
        entry.status = STATUS_DUPLICATE
        entry.log('dedupe', f'Looks like a duplicate of {originals[0]}')
    return len(found)


def _durations_compatible(a: BookEntry, b: BookEntry) -> bool:
    """True unless we know the durations and they're far apart."""
    da, db = _duration(a), _duration(b)
    if not da or not db:
        return True  # unknown - don't let a missing value block detection
    longer, shorter = max(da, db), min(da, db)
    return (longer - shorter) / longer <= _DURATION_TOLERANCE


def _duration(entry: BookEntry) -> int:
    try:
        return int(entry.raw_tags.get('_duration_seconds', 0) or 0)
    except (TypeError, ValueError):
        return 0
