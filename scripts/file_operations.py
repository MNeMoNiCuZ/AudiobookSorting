"""Moving/copying books into the organised output tree.

The critical correctness rule: **an entry owns only its own files.** The previous
version iterated the whole source directory, so applying one book out of a four-book
folder physically moved the other three with it. Here, the file list comes from the
entry, and sidecar files (cover art, cue sheets) are only claimed when they
unambiguously belong to it.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Set

from .journal import ApplyJournal, FileMove, Transaction
from .models import BookEntry
from .paths import build_destination, long_path, sanitize_component, unique_path

logger = logging.getLogger(__name__)

# Non-audio files worth carrying along with the book.
SIDECAR_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif',
                      '.cue', '.nfo', '.txt', '.opf', '.pdf', '.epub', '.json')
COVER_NAMES = ('cover', 'folder', 'front', 'albumart', 'thumb', 'poster')


class ApplyResult:
    """Outcome of applying one entry - also the dry-run preview payload."""

    def __init__(self, entry_id: str, destination: Path, operations: List[Dict],
                 skipped: bool = False, error: str = '', dry_run: bool = False):
        self.entry_id = entry_id
        self.destination = destination
        self.operations = operations
        self.skipped = skipped
        self.error = error
        self.dry_run = dry_run

    @property
    def ok(self) -> bool:
        return not self.error and not self.skipped

    def describe(self) -> str:
        if self.error:
            return f'ERROR  {self.entry_id}: {self.error}'
        if self.skipped:
            return f'SKIP   {self.entry_id}: destination exists'
        verb = 'WOULD ' if self.dry_run else ''
        lines = [f'{verb}APPLY {self.entry_id} -> {self.destination}']
        for op in self.operations:
            lines.append(f'    {op["operation"]:6s} {Path(op["source"]).name}')
        return '\n'.join(lines)


class FileOperations:
    def __init__(self, settings, journal: Optional[ApplyJournal] = None):
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        self.output_dir = settings.get_path('AO_OUTPUT_DIR')
        self.journal = journal or ApplyJournal(
            settings.get_path('AO_OUTPUT_DIR').parent / 'apply_journal.jsonl')
        # Destinations claimed earlier in this same batch, so two entries resolving to
        # the same folder don't collide mid-run.
        self._claimed: Set[Path] = set()

    # ------------------------------------------------------------------ config

    @property
    def dry_run(self) -> bool:
        """Applying always writes. Previewing is an explicit call, not a mode.

        There used to be an AO_DRY_RUN setting, which meant "Apply" sometimes wrote
        files and sometimes silently did not, depending on a checkbox three tabs deep
        in the settings. Preview is a button now; Apply applies.
        """
        return False

    @property
    def copy_mode(self) -> bool:
        return self.settings.get_bool('AO_COPY_MODE', True)

    @property
    def collision_policy(self) -> str:
        return self.settings.get('AO_COLLISION_POLICY', 'suffix').strip().lower()

    # ------------------------------------------------------------------- apply

    def destination_for(self, entry: BookEntry) -> Path:
        """Where this entry would go, per the configured template."""
        return build_destination(
            self.output_dir,
            self.settings.get('AO_OUTPUT_TEMPLATE'),
            {
                'author': entry.value('author') or 'Unknown Author',
                'series': entry.value('series'),
                'series_index': entry.value('series_index'),
                'title': entry.value('title') or Path(entry.primary_audio).stem,
            },
            # No setting: the platform's real limit is detected. See
            # paths.platform_path_limit().
        )

    def files_for(self, entry: BookEntry) -> List[Path]:
        """Exactly the files this entry owns - never a sibling's.

        Audio files come from the entry itself. Sidecars are included only when the
        folder holds a single book; in a multi-book folder a shared "cover.jpg" can't
        be attributed to any one entry, so a per-book image is matched by name instead.
        """
        folder = Path(entry.folder)
        files: List[Path] = []

        for name in entry.audio_files:
            path = folder / name
            if path.is_file():
                files.append(path)

        if not entry.is_multi_book_folder:
            # Sole occupant: take the loose extras with it.
            try:
                for path in sorted(folder.iterdir()):
                    if (path.is_file() and path not in files
                            and path.suffix.lower() in SIDECAR_EXTENSIONS):
                        files.append(path)
            except OSError as exc:
                self.logger.warning('Could not list %s: %s', folder, exc)
        else:
            # Shared folder: only take an image whose name matches this book's file.
            stem = Path(entry.primary_audio).stem.lower()
            try:
                for path in sorted(folder.iterdir()):
                    if (path.is_file() and path.suffix.lower() in SIDECAR_EXTENSIONS
                            and path.stem.lower() == stem):
                        files.append(path)
            except OSError:
                pass

        return files

    def preview(self, entry: BookEntry) -> ApplyResult:
        """What applying this entry would do. No filesystem writes."""
        return self._apply(entry, force_dry_run=True)

    def apply_entry(self, entry: BookEntry) -> ApplyResult:
        """Move or copy this entry's files into the output tree."""
        return self._apply(entry, force_dry_run=self.dry_run)

    def _apply(self, entry: BookEntry, force_dry_run: bool) -> ApplyResult:
        dry_run = force_dry_run
        files = self.files_for(entry)
        if not files:
            return ApplyResult(entry.entry_id, Path(), [],
                               error='No files found for this entry', dry_run=dry_run)

        destination = self.destination_for(entry)
        destination, skipped = self._resolve_collision(destination, dry_run)
        if skipped:
            return ApplyResult(entry.entry_id, destination, [], skipped=True,
                               dry_run=dry_run)

        operation = 'copy' if self.copy_mode else 'move'
        planned: List[Dict] = []
        rename_map = self._rename_map(entry, files)

        for path in files:
            target = destination / rename_map.get(path, path.name)
            planned.append({'source': str(path), 'destination': str(target),
                            'operation': operation})

        if dry_run:
            self._claimed.add(destination)
            return ApplyResult(entry.entry_id, destination, planned, dry_run=True)

        transaction = Transaction(entry_id=entry.entry_id, destination=str(destination))
        created_dirs = self._existing_ancestors(destination)

        try:
            destination.mkdir(parents=True, exist_ok=True)
            transaction.created_dirs = [str(d) for d in created_dirs]
        except OSError as exc:
            return ApplyResult(entry.entry_id, destination, [],
                               error=f'Could not create {destination}: {exc}')

        done: List[Dict] = []
        for plan in planned:
            source, target = Path(plan['source']), Path(plan['destination'])
            try:
                final = self._transfer(source, target, operation)
                transaction.moves.append(
                    FileMove(source=str(source), destination=str(final),
                             operation=operation))
                done.append({**plan, 'destination': str(final)})
            except OSError as exc:
                self.logger.error('Failed to %s %s: %s', operation, source, exc)
                self.journal.record(transaction)  # keep what did happen, so it's undoable
                return ApplyResult(entry.entry_id, destination, done,
                                   error=f'{operation} failed on {source.name}: {exc}')

        self.journal.record(transaction)
        self._claimed.add(destination)
        entry.applied_path = str(destination)

        self._write_extras(entry, destination, transaction)
        return ApplyResult(entry.entry_id, destination, done)

    # --------------------------------------------------------------- internals

    def _rename_map(self, entry: BookEntry, files: List[Path]) -> Dict[Path, str]:
        """New filenames, when file renaming is enabled (#22)."""
        if not self.settings.get_bool('AO_RENAME_FILES', False):
            return {}

        from .paths import render_template

        from .file_scanner import AUDIO_EXTENSIONS

        template = self.settings.get('AO_FILE_TEMPLATE')
        # Audio is what the audio template names. Everything else is a companion file,
        # whatever its extension - testing against the sidecar list left anything
        # unlisted (.m3u, .sfv, .log) to be renamed as though it were a chapter.
        audio = [p for p in files if p.suffix.lower() in AUDIO_EXTENSIONS]
        mapping: Dict[Path, str] = {}
        width = max(2, len(str(len(audio))))
        # A multi-part book whose template has no per-file placeholder would render
        # the same name for every part, and they would collide into "(2)", "(3)"...
        # in playback-scrambling order. Add the part number the template forgot.
        needs_part = len(audio) > 1 and '{file_index' not in template

        for number, path in enumerate(sorted(audio), start=1):
            values = {
                'author': entry.value('author'),
                'series': entry.value('series'),
                'series_index': entry.value('series_index'),
                'title': entry.value('title'),
                # Per-file placeholders. A single-file book has no part number, so
                # {file_index} renders empty rather than a pointless "01". The raw
                # number is passed through: padding belongs to the template's format
                # spec ({file_index:03d}), not to this function.
                'file_index': str(number) if len(audio) > 1 else '',
                'extension': path.suffix.lower().lstrip('.'),
            }
            name = render_template(template, values).replace('/', ' - ').strip()

            # The template usually ends in ".{extension}"; if the user removed it, or
            # it collapsed away, put the real suffix back rather than writing a file
            # the operating system cannot open.
            suffix = path.suffix.lower()
            if name.lower().endswith(suffix):
                stem, name_suffix = name[:-len(suffix)], suffix
            else:
                stem, name_suffix = name, suffix
            stem = sanitize_component(stem.strip(' .'), fallback=path.stem)
            if needs_part:
                stem = f'{stem} - Part {number:0{width}d}'
            mapping[path] = f'{stem}{name_suffix}'

        mapping.update(self._support_rename_map(entry, files, template))
        return mapping

    def _support_rename_map(self, entry: BookEntry, files: List[Path],
                            template: str) -> Dict[Path, str]:
        """Rename *every* non-audio file travelling with the book to match it.

        Not just covers and e-books: the .cue, the .nfo, the .txt, the .m3u, anything.
        The rule is not "which extensions are on a list", it is "is this the only file
        of its kind here" - two .jpgs might be "cover" and "back", and renaming both to
        the same stem would collide, so an extension with more than one file is left
        exactly as it is and everything else follows the audio.
        """
        if not self.settings.get_bool('AO_RENAME_SUPPORT_FILES', False):
            return {}

        from .file_scanner import AUDIO_EXTENSIONS
        from .paths import render_template

        support = [p for p in files if p.suffix.lower() not in AUDIO_EXTENSIONS]
        by_extension: Dict[str, List[Path]] = {}
        for path in support:
            by_extension.setdefault(path.suffix.lower(), []).append(path)

        base = render_template(template, {
            'author': entry.value('author'),
            'series': entry.value('series'),
            'series_index': entry.value('series_index'),
            'title': entry.value('title'),
            'file_index': '',
            'extension': '',
        }).replace('/', ' - ').strip(' .')

        mapping: Dict[Path, str] = {}
        if not base:
            return mapping
        for extension, paths in by_extension.items():
            if len(paths) != 1:
                continue    # ambiguous - leave them alone
            stem = sanitize_component(base, fallback=paths[0].stem)
            mapping[paths[0]] = f'{stem}{extension}'
        return mapping

    def _resolve_collision(self, destination: Path, dry_run: bool) -> tuple:
        """Apply the configured collision policy. Returns (path, skipped)."""
        taken = destination.exists() or destination in self._claimed
        if not taken:
            return destination, False

        policy = self.collision_policy
        if policy == 'skip':
            self.logger.info('Skipping, destination exists: %s', destination)
            return destination, True
        if policy in ('merge', 'overwrite'):
            return destination, False
        # Default: park it alongside as "Title (2)" rather than merging two books.
        resolved = unique_path(destination, existing=self._claimed)
        self.logger.info('Destination exists, using %s instead', resolved)
        return resolved, False

    def _transfer(self, source: Path, target: Path, operation: str) -> Path:
        """Copy or move one file, handling same-volume fast paths and long paths."""
        if target.exists() and self.collision_policy != 'overwrite':
            target = unique_path(target)

        src, dst = long_path(source), long_path(target)

        if operation == 'copy':
            shutil.copy2(src, dst)
            return target

        # Move: os.replace is instantaneous within a volume; across volumes it raises
        # and we fall back to a full copy+delete (#34).
        try:
            os.replace(src, dst)
        except OSError:
            shutil.move(src, dst)
        return target

    @staticmethod
    def _existing_ancestors(destination: Path) -> List[Path]:
        """Directories that don't exist yet and so will be created by this apply."""
        missing = []
        current = destination
        while not current.exists() and current != current.parent:
            missing.append(current)
            current = current.parent
        return missing

    def _write_extras(self, entry: BookEntry, destination: Path,
                      transaction: Transaction) -> None:
        """Optional post-apply steps: tag writing and sidecar files."""
        if self.settings.get_bool('AO_WRITE_TAGS', False):
            try:
                from .tag_writer import write_tags
                written = write_tags(entry, destination)
                if written:
                    self.logger.info('Wrote tags to %d file(s)', written)
            except Exception as exc:
                self.logger.warning('Could not write tags: %s', exc)

        if self.settings.get_bool('AO_WRITE_SIDECAR', False):
            try:
                from .sidecar import write_sidecars
                for path in write_sidecars(entry, destination):
                    transaction.moves.append(
                        FileMove(source='', destination=str(path), operation='write'))
            except Exception as exc:
                self.logger.warning('Could not write sidecars: %s', exc)

    # ------------------------------------------------------------------- batch

    def reset_batch(self) -> None:
        """Forget destinations claimed by a previous run."""
        self._claimed.clear()

    def cleanup_empty_source_dirs(self, root: Path) -> int:
        """After moving, remove folders left behind empty. Never touches `root`."""
        if self.dry_run or self.copy_mode:
            return 0
        removed = 0
        for path in sorted(Path(root).rglob('*'), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and path != Path(root):
                try:
                    if not any(path.iterdir()):
                        path.rmdir()
                        removed += 1
                except OSError:
                    pass
        return removed
