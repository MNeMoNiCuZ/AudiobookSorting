"""Duplicate detection across the library (#28).

A duplicate is **the same audio on disk twice**, and nothing else. This used to be
decided on names: two entries whose normalised author+title agreed were called
duplicates. In a messy library that is wrong far more often than it is right - a flat
folder of chapter files resolves twenty ways to the same book title, and every chapter
after the first was flagged as a duplicate of chapter one. Chapters of one book are the
*opposite* of duplicates.

So identity is no longer consulted at all, and **nothing is ever flagged without a
full SHA-256 match on every byte of every file**. The cheap tests exist only to decide
what is worth hashing; they can rule a pair out, never in:

1. **Shape** - the same number of audio files with the same sizes. Two different
   chapters never collide here, and a genuine second copy always does. Rules out.
2. **Quick hash** - SHA-256 of the first and last megabyte of each file. Rules out.
3. **Full hash** - SHA-256 of every byte, always, no setting to skip it. This is the
   only test that can call something a duplicate.

Two editions of one book with different narrators are not duplicates and never reach
step 3. Neither are two parts of one book, whatever they are called or however long
they are.

Hashes are cached against path+size+mtime, so a rescan of an unchanged library pays the
I/O once, and only the files that survived steps 1 and 2 are ever read in full.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import STATUS_DUPLICATE, STATUS_PENDING, BookEntry

logger = logging.getLogger(__name__)

# How much of each end of a file the quick hash reads.
_QUICK_BYTES = 1 << 20          # 1 MiB
_READ_CHUNK = 1 << 20
# Below this, a "file" is a placeholder, a stub or an artefact - not evidence.
_MIN_TOTAL_BYTES = 64 * 1024


def find_duplicates(entries: Iterable[BookEntry],
                    cache: Any = None) -> Dict[str, List[str]]:
    """Map each duplicate entry_id to the ids of the entries it duplicates.

    Every reported pair has matching full-file SHA-256 digests. ``cache`` is an optional
    :class:`scripts.cache.Cache` used to remember hashes between runs.
    """
    entries = [e for e in entries if e.audio_files]
    duplicates: Dict[str, List[str]] = {}

    # --- 1. shape: file count and the exact set of file sizes.
    shapes: Dict[Tuple, List[BookEntry]] = {}
    for entry in entries:
        shape = _shape(entry)
        if shape is None:
            continue
        shapes.setdefault(shape, []).append(entry)

    for shape, group in shapes.items():
        if len(group) < 2:
            continue

        # --- 2. quick hash of each file's head and tail.
        quick: Dict[str, List[BookEntry]] = {}
        for entry in group:
            digest = _entry_digest(entry, cache, full=False)
            if digest:
                quick.setdefault(digest, []).append(entry)

        for digest, matched in quick.items():
            if len(matched) < 2:
                continue

            # --- 3. the whole file, every byte. The only test that can say "duplicate".
            full: Dict[str, List[BookEntry]] = {}
            for entry in matched:
                deep = _entry_digest(entry, cache, full=True)
                if deep:
                    full.setdefault(deep, []).append(entry)

            for members in [same for same in full.values() if len(same) > 1]:
                original = _pick_original(members)
                for entry in members:
                    if entry is original:
                        continue
                    # The same files reached twice - two scan roots overlapping, a
                    # junction, an id collision - are one copy, not two.
                    if _same_files(entry, original):
                        logger.debug('%s and %s are the same files on disk',
                                     entry.entry_id, original.entry_id)
                        continue
                    duplicates.setdefault(entry.entry_id, []).append(original.entry_id)
                    logger.info('Duplicate: %s == %s (%d file(s), %d bytes, identical '
                                'SHA-256)', entry.entry_id, original.entry_id,
                                shape[0], shape[1])

    return duplicates


def mark_duplicates(entries: Iterable[BookEntry], cache: Any = None) -> int:
    """Flag duplicates in place. Returns how many were flagged.

    A flag from a previous run that this run does not confirm is *removed*. Without
    that, an entry wrongly called a duplicate once stayed red forever - the status is
    saved with the entry, and nothing else would ever clear it.
    """
    entries = list(entries)
    found = find_duplicates(entries, cache=cache)
    by_id = {e.entry_id: e for e in entries}

    for entry in entries:
        if entry.status == STATUS_DUPLICATE and entry.entry_id not in found:
            entry.status = STATUS_PENDING
            entry.duplicate_of = ''
            entry.begin_tier('dedupe')
            logger.debug('Cleared stale duplicate flag on %s', entry.entry_id)

    for entry_id, originals in found.items():
        entry = by_id.get(entry_id)
        if entry is None or entry.status in ('applied', 'rejected'):
            continue
        entry.duplicate_of = originals[0]
        entry.status = STATUS_DUPLICATE
        entry.begin_tier('dedupe')
        entry.log('dedupe',
                  f'Byte-for-byte identical to {originals[0]}: same file count, same '
                  f'sizes, and a matching SHA-256 over every byte of every file.')
    return len(found)


# ------------------------------------------------------------------ evidence

def _files(entry: BookEntry) -> List[Path]:
    base = Path(entry.folder)
    return [base / name for name in entry.audio_files]


def _shape(entry: BookEntry) -> Optional[Tuple]:
    """(file count, total bytes, sorted per-file sizes), or None if unusable.

    An entry whose files cannot be measured is not compared at all: an unknown size is
    not evidence of a match, and treating it as one is how false positives got in.
    """
    sizes: List[int] = []
    for path in _files(entry):
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size <= 0:
            return None
        sizes.append(size)

    if not sizes or sum(sizes) < _MIN_TOTAL_BYTES:
        return None
    return (len(sizes), sum(sizes), tuple(sorted(sizes)))


def _entry_digest(entry: BookEntry, cache: Any, full: bool) -> str:
    """One digest covering every audio file of the entry, or '' if any is unreadable.

    Files are hashed in size order rather than name order, so the same content filed
    under different names still matches.
    """
    paths = sorted(_files(entry), key=lambda p: (_size_of(p), p.name))
    combined = hashlib.sha256()
    for path in paths:
        digest = _file_digest(path, cache, full=full)
        if not digest:
            return ''
        combined.update(digest.encode())
    return combined.hexdigest()


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _file_digest(path: Path, cache: Any, full: bool) -> str:
    """SHA-256 of the file, or of its first and last megabyte when ``full`` is false."""
    try:
        stat = path.stat()
    except OSError:
        return ''

    namespace = 'filehash_full' if full else 'filehash_quick'
    key = f'{path}|{stat.st_size}|{int(stat.st_mtime)}'
    if cache is not None:
        cached = cache.get(namespace, key)
        if isinstance(cached, str) and cached:
            return cached

    try:
        digest = (_hash_whole(path) if full
                  else _hash_ends(path, stat.st_size))
    except OSError as exc:
        logger.debug('Could not hash %s: %s', path, exc)
        return ''

    if cache is not None:
        try:
            cache.set(namespace, key, digest)
        except Exception as exc:            # a cache failure must never stop a scan
            logger.debug('Could not cache hash for %s: %s', path, exc)
    return digest


def _hash_whole(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_ends(path: Path, size: int) -> str:
    """Hash the first and last megabyte, with the size mixed in.

    The size is part of the digest so that a short file - where the two reads overlap -
    can never collide with a long one whose ends happen to match.
    """
    digest = hashlib.sha256()
    digest.update(str(size).encode())
    with open(path, 'rb') as handle:
        digest.update(handle.read(_QUICK_BYTES))
        if size > 2 * _QUICK_BYTES:
            handle.seek(-_QUICK_BYTES, os.SEEK_END)
            digest.update(handle.read(_QUICK_BYTES))
    return digest.hexdigest()


def _same_files(a: BookEntry, b: BookEntry) -> bool:
    """True when both entries point at the same files on disk."""
    def resolved(entry: BookEntry) -> set:
        paths = set()
        for path in _files(entry):
            try:
                paths.add(os.path.normcase(str(path.resolve())))
            except OSError:
                paths.add(os.path.normcase(str(path)))
        return paths

    return resolved(a) == resolved(b)


def _pick_original(members: Sequence[BookEntry]) -> BookEntry:
    """Which copy is "the one we already have".

    An entry that has already been filed wins, then the best-identified one, then the
    shortest path - so the copy nearest the library root is kept and the one buried in
    a "new downloads" folder is the one called a duplicate. Stable across runs.
    """
    def rank(entry: BookEntry) -> Tuple:
        return (0 if entry.status == 'applied' else 1,
                -entry.confidence(),
                len(entry.folder),
                entry.entry_id)

    return sorted(members, key=rank)[0]
