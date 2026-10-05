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
from copy import deepcopy
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .models import STATUS_DUPLICATE, STATUS_PENDING, BookEntry, Field, IDENTITY_FIELDS

logger = logging.getLogger(__name__)


class DataManager:
    def __init__(self, save_file: Optional[Path] = None, autosave_seconds: float = 5.0,
                 change_log_mb: int = 200):
        from .paths import USER_DIR
        self.save_file = (Path(save_file) if save_file
                          else USER_DIR / 'book_entries.json')
        self.entries: Dict[str, BookEntry] = {}
        self.logger = logging.getLogger(__name__)
        self._dirty = False
        self._lock = threading.RLock()
        self._autosave_seconds = autosave_seconds
        self._timer: Optional[threading.Timer] = None
        # Size at which the change log is rotated; AO_CHANGE_LOG_MB.
        self.change_log_limit = max(1, int(change_log_mb)) * 1024 * 1024
        # What every entry looked like at the last save, so each save can log exactly
        # which values changed. See _log_changes.
        self._last_saved: Dict[str, Dict] = {}
        self.load()
        self._last_saved = self._snapshot()

    # ------------------------------------------------------------------- load

    def load(self, source: Optional[Path] = None) -> None:
        source = Path(source) if source else self.save_file
        if not source.exists():
            return
        try:
            raw = json.loads(source.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            self.logger.error('Could not read %s: %s', self.save_file, exc)
            self._backup_corrupt()
            return

        if not isinstance(raw, dict):
            return
        stale_duplicates = 0
        stripped_artwork = 0
        for entry_id, data in raw.items():
            try:
                entry = BookEntry.from_dict(data)
                entry.entry_id = entry.entry_id or entry_id
                for key in list(entry.raw_tags):
                    normal_key = str(key).casefold()
                    if any(name in normal_key for name in
                           ('covr', 'apic', 'metadata_block_picture')):
                        entry.raw_tags.pop(key, None)
                        stripped_artwork += 1
                # "Duplicate" is derived, not decided: it is recomputed from the files
                # on disk by every scan, and the files may have moved or been deleted
                # since this was written. Trusting a saved flag is how a red row
                # outlived the check that produced it - including the ones the old
                # name-based check got wrong, which no rescan of the new one would ever
                # have visited to clear. It is dropped on load and earned again.
                if entry.status == STATUS_DUPLICATE:
                    entry.status = STATUS_PENDING
                    entry.duplicate_of = ''
                    entry.trace = [step for step in entry.trace
                                   if step.get('tier') != 'dedupe']
                    stale_duplicates += 1
                self.entries[entry_id] = entry
            except Exception as exc:
                self.logger.warning('Skipping unreadable entry %s: %s', entry_id, exc)
        self.logger.info('Loaded %d entries from %s', len(self.entries), source)
        if stale_duplicates:
            self.logger.info('Dropped %d saved duplicate flag(s) - they are recomputed '
                             'from the files on disk, never restored', stale_duplicates)
        if stripped_artwork:
            self.logger.info('Removed %d embedded cover-art value(s) from saved raw tags',
                             stripped_artwork)
            self.mark_dirty()

    def _backup_corrupt(self) -> None:
        try:
            backup = self.save_file.with_suffix('.corrupt.json')
            self.save_file.replace(backup)
            self.logger.warning('Moved unreadable save file to %s', backup)
        except OSError:
            pass

    # ------------------------------------------------------------------- save

    BACKUPS_KEPT = 20

    def backup(self, reason: str) -> Optional[Path]:
        """Copy the list as it stands to backups/, before something rewrites it.

        Taken before every load, clear and removal: those are the actions that throw
        away identified values and decisions, and the save file is the only record
        of them. The newest BACKUPS_KEPT copies are kept.
        """
        import time

        self.flush()
        with self._lock:
            payload = {eid: entry.to_dict() for eid, entry in self.entries.items()}
        if not payload:
            return None
        folder = self.save_file.parent / 'backups'
        stamp = time.strftime('%Y-%m-%d_%H-%M-%S')
        target = folder / f'{self.save_file.stem}.{stamp}.{reason}.json'
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                              encoding='utf-8')
            old = sorted(folder.glob(f'{self.save_file.stem}.*.json'))
            for stale in old[:-self.BACKUPS_KEPT]:
                stale.unlink()
        except OSError as exc:
            self.logger.error('Could not back up the list before %s: %s', reason, exc)
            return None
        self.logger.info('Backed up %d entries to %s', len(payload), target)
        return target

    def restore(self, backup: Path) -> int:
        """Replace the list with a backup. The list as it stands is backed up first."""
        self.backup('before-restore')
        with self._lock:
            self.entries = {}
        self.load(backup)
        self.mark_dirty()
        self.flush()
        return len(self.entries)

    # ------------------------------------------------------------ change log

    LOGGED_FIELDS = ('author', 'series', 'series_index', 'title')

    def _snapshot(self) -> Dict[str, Dict]:
        """The values the change log compares: each field with its source, and status."""
        with self._lock:
            entries = list(self.entries.values())
        snap = {}
        for entry in entries:
            row = {'status': entry.status,
                   'where': str(Path(entry.folder) / entry.primary_audio)
                   if entry.primary_audio and not Path(entry.primary_audio).is_absolute()
                   else entry.primary_audio or entry.folder}
            for name in self.LOGGED_FIELDS:
                field = entry.get_field(name)
                row[name] = [str(field.value or ''), field.source or '']
            snap[entry.entry_id] = row
        return snap

    def change_log_path(self) -> Path:
        return self.save_file.parent / 'logs' / 'changes.jsonl'

    def _log_changes(self) -> None:
        """Append every value that changed since the last save to user/logs/changes.jsonl.

        One line per change, old and new value with their sources, so anything a load,
        an edit or an identification overwrote can be read back and put back.
        """
        import time

        now = self._snapshot()
        before = self._last_saved
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        lines = []
        for entry_id in sorted(set(before) | set(now)):
            old, new = before.get(entry_id), now.get(entry_id)
            if old == new:
                continue
            where = (new or old).get('where', '')
            if old is None or new is None:
                row = old or new
                lines.append({'time': stamp, 'entry': entry_id, 'file': where,
                              'change': 'added' if old is None else 'removed',
                              'status': row['status'],
                              **{name: row[name][0] for name in self.LOGGED_FIELDS}})
                continue
            for key in ('status',) + self.LOGGED_FIELDS:
                if old.get(key) == new.get(key):
                    continue
                if key == 'status':
                    lines.append({'time': stamp, 'entry': entry_id, 'file': where,
                                  'field': 'status', 'before': old[key],
                                  'after': new[key]})
                else:
                    lines.append({'time': stamp, 'entry': entry_id, 'file': where,
                                  'field': key, 'before': old[key][0],
                                  'before_source': old[key][1], 'after': new[key][0],
                                  'after_source': new[key][1]})
        self._last_saved = now
        if not lines:
            return
        path = self.change_log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > self.change_log_limit:
                path.replace(path.with_suffix('.1.jsonl'))
            with open(path, 'a', encoding='utf-8') as handle:
                for line in lines:
                    handle.write(json.dumps(line, ensure_ascii=False) + '\n')
        except OSError as exc:
            self.logger.error('Could not write the change log: %s', exc)

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
            self._log_changes()
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
            self._release_duplicates()
        self.mark_dirty()

    def _release_duplicates(self) -> None:
        """Clear the Duplicate flag on every book whose original is no longer listed.

        Removing the copy a book duplicates leaves nothing for it to duplicate, so it
        goes back to Pending instead of staying red until the next scan.
        """
        for entry in self.entries.values():
            if entry.status == STATUS_DUPLICATE and entry.duplicate_of not in self.entries:
                entry.status = STATUS_PENDING
                entry.duplicate_of = ''
                entry.trace = [step for step in entry.trace
                               if step.get('tier') != 'dedupe']

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
            if resume:
                scanned = self._apply_user_combines(list(scanned), input_root)
            for entry in scanned:
                existing = self.entries.get(entry.entry_id)
                if (existing is not None and resume
                        and self._belongs_to_root(existing, input_root)):
                    # Refresh what's derived from disk, keep everything decided.
                    existing.folder = entry.folder
                    existing.relative_path = entry.relative_path
                    existing.audio_files = entry.audio_files
                    existing.audio_sizes = entry.audio_sizes
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
            self._release_duplicates()

        self.mark_dirty()
        return result

    def combine(self, entries: List[BookEntry],
                fields: Optional[Dict[str, Field]] = None) -> Optional[BookEntry]:
        """Fold ``entries`` into the first of them: one book, every file.

        Only entries in the same folder can be combined - an entry's files are names
        relative to its one folder. The others are removed. Returns the combined entry,
        or None when there is nothing to combine.
        """
        if len(entries) < 2:
            return None
        folder = os.path.normcase(os.path.normpath(entries[0].folder))
        if any(os.path.normcase(os.path.normpath(e.folder)) != folder for e in entries):
            return None

        keep = entries[0]
        sizes: Dict[str, int] = {}
        for entry in entries:
            for position, name in enumerate(entry.audio_files):
                size = (entry.audio_sizes[position]
                        if position < len(entry.audio_sizes) else -1)
                sizes.setdefault(name, size)
        names = sorted(sizes)
        images = sorted({name for entry in entries for name in entry.image_files})

        with self._lock:
            for name in IDENTITY_FIELDS:
                if fields and name in fields:
                    setattr(keep, name, deepcopy(fields[name]))
            keep.audio_files = names
            keep.audio_sizes = [sizes[name] for name in names]
            keep.primary_audio = str(Path(keep.folder) / names[0])
            keep.image_files = images
            keep.combined_by_user = True
            keep.is_multi_book_folder = False
            for entry in entries[1:]:
                self.entries.pop(entry.entry_id, None)
            self.entries[keep.entry_id] = keep
            self._release_duplicates()
        keep.log('user', f'Combined {len(entries)} entries into one book '
                         f'({len(names)} files)')
        self.mark_dirty()
        return keep

    def _apply_user_combines(self, scanned: List[BookEntry],
                             input_root: Optional[Path]) -> List[BookEntry]:
        """Put files you combined back into their combined entry.

        The scanner does not know you joined them, so it splits them again. Every
        scanned file owned by a combined entry goes to that entry; a scanned entry left
        with nothing is dropped.
        """
        owner: Dict[str, BookEntry] = {}
        for entry in self.entries.values():
            if not entry.combined_by_user or not self._belongs_to_root(entry,
                                                                       input_root):
                continue
            for path in entry.absolute_files():
                owner[os.path.normcase(os.path.normpath(str(path)))] = entry

        if not owner:
            return scanned

        combined_ids = {entry.entry_id for entry in owner.values()}
        claimed: Dict[str, Dict[str, int]] = {}
        combined: Dict[str, BookEntry] = {}
        result: List[BookEntry] = []
        for entry in scanned:
            kept_names, kept_sizes = [], []
            for position, name in enumerate(entry.audio_files):
                size = (entry.audio_sizes[position]
                        if position < len(entry.audio_sizes) else -1)
                key = os.path.normcase(os.path.normpath(str(Path(entry.folder) / name)))
                target = owner.get(key)
                if target is None:
                    kept_names.append(name)
                    kept_sizes.append(size)
                    continue
                claimed.setdefault(target.entry_id, {})[name] = size
                if target.entry_id not in combined:
                    combined[target.entry_id] = target
                    result.append(target)
            if not kept_names:
                continue
            if len(kept_names) != len(entry.audio_files):
                entry.audio_files = kept_names
                entry.audio_sizes = kept_sizes
                entry.primary_audio = str(Path(entry.folder) / kept_names[0])
                if entry.entry_id in combined_ids:
                    entry.entry_id = str(Path(entry.relative_path) / kept_names[0])
                    entry.is_multi_book_folder = True
            result.append(entry)

        # The combined entries stand in for the scanned ones, so they carry what the
        # scan found on disk: the files still there, at their current sizes.
        refreshed: List[BookEntry] = []
        for entry in result:
            files = claimed.get(entry.entry_id) if entry.entry_id in combined else None
            if files is not None and entry is combined[entry.entry_id]:
                names = sorted(files)
                refreshed.append(BookEntry(
                    entry_id=entry.entry_id, folder=entry.folder,
                    relative_path=entry.relative_path, image_files=entry.image_files,
                    audio_files=names, audio_sizes=[files[name] for name in names],
                    primary_audio=str(Path(entry.folder) / names[0]),
                    is_multi_book_folder=False))
            else:
                refreshed.append(entry)
        return refreshed

    @staticmethod
    def _belongs_to_root(entry: BookEntry, input_root: Optional[Path]) -> bool:
        """Whether saved state belongs to the input tree currently being scanned."""
        if input_root is None:
            return True
        try:
            root = Path(input_root).resolve()
            folder = Path(entry.folder).resolve()
        except OSError:
            return False
        return folder == root or root in folder.parents

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
