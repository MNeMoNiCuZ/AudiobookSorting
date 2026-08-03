"""Persistence for reviewed entries (#5, #27).

The old version reserialised the entire file on every single field update, which is
O(n^2) over a scan. This one marks dirty and flushes on a timer or on demand, and every
write is atomic so a crash mid-save can't leave a truncated `book_entries.json`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .models import BookEntry

logger = logging.getLogger(__name__)


class DataManager:
    def __init__(self, save_file: Optional[Path] = None, autosave_seconds: float = 5.0):
        from .paths import PROJECT_ROOT
        self.save_file = (Path(save_file) if save_file
                          else PROJECT_ROOT / 'book_entries.json')
        self.entries: Dict[str, BookEntry] = {}
        self.logger = logging.getLogger(__name__)
        self._dirty = False
        self._lock = threading.RLock()
        self._autosave_seconds = autosave_seconds
        self._timer: Optional[threading.Timer] = None
        self.load()

    # ------------------------------------------------------------------- load

    def load(self) -> None:
        if not self.save_file.exists():
            return
        try:
            raw = json.loads(self.save_file.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            self.logger.error('Could not read %s: %s', self.save_file, exc)
            self._backup_corrupt()
            return

        if not isinstance(raw, dict):
            return
        for entry_id, data in raw.items():
            try:
                entry = BookEntry.from_dict(data)
                entry.entry_id = entry.entry_id or entry_id
                self.entries[entry_id] = entry
            except Exception as exc:
                self.logger.warning('Skipping unreadable entry %s: %s', entry_id, exc)
        self.logger.info('Loaded %d entries from %s', len(self.entries), self.save_file)

    def _backup_corrupt(self) -> None:
        try:
            backup = self.save_file.with_suffix('.corrupt.json')
            self.save_file.replace(backup)
            self.logger.warning('Moved unreadable save file to %s', backup)
        except OSError:
            pass

    # ------------------------------------------------------------------- save

    def save(self, force: bool = False) -> bool:
        """Write to disk if anything changed. Atomic: temp file then replace."""
        with self._lock:
            if not self._dirty and not force:
                return False
            payload = {eid: entry.to_dict() for eid, entry in self.entries.items()}
            self._dirty = False

        try:
            self.save_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.save_file.with_suffix('.tmp')
            with open(tmp, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(self.save_file)
            self.logger.debug('Saved %d entries', len(payload))
            return True
        except OSError as exc:
            self.logger.error('Could not save entries: %s', exc)
            with self._lock:
                self._dirty = True  # try again on the next flush
            return False

    def mark_dirty(self) -> None:
        """Note that something changed and schedule a background flush."""
        with self._lock:
            self._dirty = True
            if self._timer is None and self._autosave_seconds > 0:
                self._timer = threading.Timer(self._autosave_seconds, self._autosave)
                self._timer.daemon = True
                self._timer.start()

    def _autosave(self) -> None:
        with self._lock:
            self._timer = None
        self.save()

    def flush(self) -> None:
        """Cancel any pending timer and write immediately."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self.save()

    # ---------------------------------------------------------------- entries

    def add(self, entry: BookEntry) -> None:
        with self._lock:
            self.entries[entry.entry_id] = entry
        self.mark_dirty()

    def add_many(self, entries: Iterable[BookEntry]) -> None:
        with self._lock:
            for entry in entries:
                self.entries[entry.entry_id] = entry
        self.mark_dirty()

    def update(self, entry: BookEntry) -> None:
        self.add(entry)

    def get(self, entry_id: str) -> Optional[BookEntry]:
        return self.entries.get(entry_id)

    def all(self) -> List[BookEntry]:
        return list(self.entries.values())

    def remove(self, entry_id: str) -> None:
        with self._lock:
            self.entries.pop(entry_id, None)
        self.mark_dirty()

    def set_status(self, entry_id: str, status: str) -> Optional[BookEntry]:
        entry = self.entries.get(entry_id)
        if entry is not None:
            entry.status = status
            self.mark_dirty()
        return entry

    def merge_scanned(self, scanned: Iterable[BookEntry], resume: bool = True,
                      input_root: Optional[Path] = None) -> List[BookEntry]:
        """Reconcile a fresh scan with what we already know (#27).

        Entries already resolved keep their resolved values and review status, so
        relaunching doesn't re-query the network for the whole library.

        The scan is authoritative about what exists under ``input_root``: anything we
        remember from there that this scan did not report is stale and is dropped.
        Without that, a folder whose grouping flips between "one book in chapters" and
        "several books" gets a new entry_id while the old one lingers, and the same
        book shows up twice in the table. Entries outside the root - notably ones
        already applied and moved to the output tree - are never pruned.
        """
        result: List[BookEntry] = []
        seen: Dict[str, BookEntry] = {}

        with self._lock:
            for entry in scanned:
                existing = self.entries.get(entry.entry_id)
                if existing is not None and resume:
                    # Refresh what's derived from disk, keep everything decided.
                    existing.folder = entry.folder
                    existing.relative_path = entry.relative_path
                    existing.audio_files = entry.audio_files
                    existing.primary_audio = entry.primary_audio
                    existing.image_files = entry.image_files
                    existing.is_multi_book_folder = entry.is_multi_book_folder
                    kept = existing
                else:
                    self.entries[entry.entry_id] = entry
                    kept = entry
                seen[kept.entry_id] = kept
                result.append(kept)

            for entry_id in self._stale_ids(seen, input_root):
                self.logger.info('Dropping stale entry %s (no longer on disk)', entry_id)
                self.entries.pop(entry_id, None)

        self.mark_dirty()
        return result

    def _stale_ids(self, seen: Dict[str, BookEntry],
                   input_root: Optional[Path]) -> List[str]:
        """Remembered ids under `input_root` that the current scan did not produce."""
        if input_root is None:
            return []
        try:
            root = Path(input_root).resolve()
        except OSError:
            return []

        stale: List[str] = []
        for entry_id, entry in self.entries.items():
            if entry_id in seen or entry.status == 'applied':
                continue
            try:
                folder = Path(entry.folder).resolve()
            except OSError:
                continue
            if folder == root or root in folder.parents:
                stale.append(entry_id)
        return stale

    def stats(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for entry in self.entries.values():
            counts[entry.status] = counts.get(entry.status, 0) + 1
        counts['total'] = len(self.entries)
        return counts

    def close(self) -> None:
        self.flush()
