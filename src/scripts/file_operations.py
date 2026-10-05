"""Moving/copying books into the organised output tree.

The critical correctness rule: **an entry owns only its own files.** The previous
version iterated the whole source directory, so applying one book out of a four-book
folder physically moved the other three with it. Here, the file list comes from the
entry, and companion files (e-books, cover art, cue sheets) are only claimed when they
unambiguously belong to it.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .journal import ApplyJournal, FileMove, Transaction
from .models import STATUS_REJECTED, BookEntry
from .paths import USER_DIR, build_destination, long_path, sanitize_component, unique_path
from .settings import display_path

logger = logging.getLogger(__name__)

# Non-audio files that travel with a book: e-books and documents, cover art and
# scans, cue sheets, chapter and metadata files, transcripts, playlists, checksums.
# Matched against the end of the name, so compound ones like ".fb2.zip" work.
COMPANION_EXTENSIONS = (
    # E-books and documents
    '.epub', '.kepub', '.kepub.epub', '.mobi', '.azw', '.azw1', '.azw3', '.azw4',
    '.azw8', '.kfx', '.kfx-zip', '.kf8', '.prc', '.pdb', '.lit', '.lrf', '.lrx',
    '.fb2', '.fb2.zip', '.fbz', '.fb3', '.djvu', '.djv', '.pdf', '.xps', '.oxps',
    '.cbz', '.cbr', '.cb7', '.cbt', '.cba', '.ibooks', '.iba', '.chm', '.tcr', '.snb',
    '.rb', '.pml', '.pmlz', '.txtz', '.htmlz', '.rtf', '.doc', '.docx', '.odt',
    '.pages', '.wpd', '.ps', '.txt', '.md', '.html', '.htm', '.xhtml', '.mht',
    '.mhtml', '.daisy', '.dtb', '.ncx', '.smil',
    # Cover art and scans
    '.jpg', '.jpeg', '.jpe', '.jfif', '.png', '.webp', '.bmp', '.gif', '.tif',
    '.tiff', '.heic', '.heif', '.avif',
    # Audiobook metadata, chapters and cue sheets
    '.cue', '.nfo', '.opf', '.json', '.xml', '.yaml', '.yml', '.abs', '.chapters',
    '.ffmetadata', '.metadata', '.id3', '.toc',
    # Transcripts, lyrics and subtitles
    '.srt', '.vtt', '.lrc', '.ass', '.ssa', '.sub', '.sbv', '.ttml',
    # Playlists
    '.m3u', '.m3u8', '.pls', '.xspf', '.wpl', '.asx',
    # Checksums, parity and rip logs
    '.sfv', '.md5', '.sha1', '.sha256', '.sha512', '.par2', '.log', '.accurip',
    # Links and notes
    '.url', '.webloc', '.desktop', '.csv', '.ini',
)
# Files the OS drops into folders by itself. A source folder holding only these is
# empty as far as a move is concerned.
CLUTTER_NAMES = ('thumbs.db', 'desktop.ini', '.ds_store', 'ehthumbs.db')
# Download leftovers (AO_JUNK_PATTERNS): torrent padding folders and files, the
# .torrent itself. Deleted from a source folder a move has emptied (AO_REMOVE_JUNK),
# and never carried along as a companion.
DEFAULT_JUNK_PATTERNS = ('.pad', '*.torrent', '_____padding_file_*')
COVER_NAMES = ('cover', 'folder', 'front', 'albumart', 'thumb', 'poster')


# The skip reason of a book you rejected - the preview groups these on their own.
REJECTED_REASON = 'rejected'


def _name_key(name: str) -> str:
    """A filename stem reduced to letters and digits, for matching a book's files."""
    return re.sub(r'[^0-9a-z]+', '', name.lower())


def _book_keys(entry: BookEntry) -> Set[str]:
    """The name keys a companion file named after this book would have."""
    keys = {_name_key(Path(name).stem) for name in entry.audio_files}
    keys.add(_name_key(entry.value('title') or ''))
    keys.discard('')
    return keys


def _title_key(entry: BookEntry) -> str:
    """The title without a trailing version marker: "The Hobbit v2" is The Hobbit."""
    title = re.sub(r'[\s(\[]*\bv(?:er(?:sion)?)?\.?\s*\d+[)\]]?\s*$', '',
                   entry.value('title') or '', flags=re.IGNORECASE)
    return _name_key(title)


class ApplyResult:
    """Outcome of applying one entry - also the dry-run preview payload."""

    def __init__(self, entry_id: str, destination: Path, operations: List[Dict],
                 skipped: bool = False, error: str = '', dry_run: bool = False,
                 reason: str = ''):
        self.entry_id = entry_id
        self.destination = destination
        self.operations = operations
        self.skipped = skipped
        self.error = error
        self.dry_run = dry_run
        # Why it was skipped. A skip is not a failure, so it carries a reason rather
        # than an error - "the destination exists" and "you rejected this book" are
        # both ordinary outcomes, and they are not the same outcome.
        self.reason = reason or ('destination exists' if skipped else '')

    @property
    def ok(self) -> bool:
        return not self.error and not self.skipped

    @property
    def rejected(self) -> bool:
        return self.skipped and self.reason == REJECTED_REASON

    def describe(self) -> str:
        if self.error:
            return f'ERROR  {self.entry_id}: {self.error}'
        if self.skipped:
            return f'SKIP   {self.entry_id}: {self.reason}'
        verb = 'WOULD ' if self.dry_run else ''
        lines = [f'{verb}APPLY {self.entry_id} -> {display_path(self.destination)}']
        for op in self.operations:
            lines.append(f'    {op["operation"]:6s} {Path(op["source"]).name}')
        return '\n'.join(lines)


class FileOperations:
    def __init__(self, settings, journal: Optional[ApplyJournal] = None):
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        self.output_dir = settings.get_path('AO_OUTPUT_DIR')
        self.journal = journal or ApplyJournal(
            USER_DIR / 'logs' / 'apply_journal.jsonl')
        # Destinations claimed earlier in this same batch, so two entries resolving to
        # the same folder don't collide mid-run.
        self._claimed: Set[Path] = set()
        # Every book on the list, set by the application. Files shared by several
        # books (a cover in a folder of two halves of one book) go to each of them.
        self.known_entries: Callable[[], Iterable[BookEntry]] = lambda: []

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
    def junk_patterns(self) -> Tuple[str, ...]:
        """The AO_JUNK_PATTERNS names, lower-cased; empty when AO_REMOVE_JUNK is off."""
        if not self.settings.get_bool('AO_REMOVE_JUNK', True):
            return ()
        raw = self.settings.get('AO_JUNK_PATTERNS', ', '.join(DEFAULT_JUNK_PATTERNS))
        return tuple(p.strip().lower() for p in str(raw or '').split(',') if p.strip())

    def is_junk(self, path: Path) -> bool:
        name = path.name.lower()
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.junk_patterns)

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

        Audio files come from the entry itself. Companion files (COMPANION_EXTENSIONS:
        e-books, covers, cue sheets, playlists...) come from the book's folder, from
        its subfolders that hold no audio ("Extras", "Scans"), and from the parent
        folders above it that hold only this book ("Book/Cover.jpg" beside
        "Book/Audiobook/part.m4b"). See companions_for for files several books share.
        """
        return self._owned(entry)[0]

    def _owned(self, entry: BookEntry) -> Tuple[List[Path], Dict[Path, List[BookEntry]]]:
        """files_for, plus the companion files shared with other books, and with whom."""
        folder = Path(entry.folder)
        files: List[Path] = []
        for name in entry.audio_files:
            path = folder / name
            if path.is_file():
                files.append(path)
        companions, shared = self._companions(entry, exclude=files)
        return files + companions, shared

    def companions_for(self, entry: BookEntry,
                       exclude: Iterable[Path] = ()) -> List[Path]:
        """The non-audio files that belong to this book, wherever they sit.

        A folder holding several books - or a parent above several entries of the
        same book (the halves of one recording, its v2 and v3) - has files that are
        all of theirs: one cover, one e-book. A file named after one of those books
        goes to that book alone; any other is shared, and goes to every one of them.

        Works on a book already finalized too: its audio is gone from the source, but
        the folder it came from is still on the entry, and whatever it left behind
        there is found the same way.
        """
        return self._companions(entry, exclude)[0]

    def _companions(self, entry: BookEntry, exclude: Iterable[Path] = ()
                    ) -> Tuple[List[Path], Dict[Path, List[BookEntry]]]:
        from .file_scanner import AUDIO_EXTENSIONS

        files: List[Path] = []
        shared: Dict[Path, List[BookEntry]] = {}
        taken = set(exclude)
        junk = self.junk_patterns

        def companion(path: Path) -> bool:
            name = path.name.lower()
            return (path.is_file() and path not in taken and path not in files
                    and path.suffix.lower() not in AUDIO_EXTENSIONS
                    and name not in CLUTTER_NAMES
                    and not any(fnmatch.fnmatchcase(name, p) for p in junk)
                    and name.endswith(COMPANION_EXTENSIONS))

        # Folders where other books live. Their files are found from there, even once
        # their audio has gone and the folder looks like a book's "Extras".
        book_folders = set()
        for other in self.known_entries():
            if other.entry_id != entry.entry_id and other.folder:
                try:
                    book_folders.add(Path(other.folder).resolve())
                except (OSError, ValueError):
                    pass

        def hosts_book(path: Path) -> bool:
            try:
                resolved = path.resolve()
            except (OSError, ValueError):
                return True
            return any(f == resolved or resolved in f.parents for f in book_folders)

        def candidates(where: Path, skip: Optional[Path] = None) -> List[Path]:
            """Loose companions, and those in subfolders holding no audio."""
            found: List[Path] = []
            try:
                children = sorted(where.iterdir())
            except OSError as exc:
                self.logger.warning('Could not list %s: %s', where, exc)
                return found
            for path in children:
                if skip is not None and path == skip:
                    continue
                if companion(path):
                    found.append(path)
                elif (path.is_dir() and not self._holds_audio(path)
                      and not hosts_book(path)):
                    found.extend(p for p in sorted(path.rglob('*')) if companion(p))
            return found

        own = Path(entry.folder)
        levels = self._levels(entry)
        if levels is None:
            # Other books share this folder but we were not told which: only a file
            # named after this book can be attributed to it.
            keys = _book_keys(entry)
            files.extend(p for p in candidates(own)
                         if p.parent == own and _name_key(p.stem) in keys)
            return files, shared

        previous: Optional[Path] = None
        for where, members in levels:
            others = [m for m in members if m.entry_id != entry.entry_id]
            mine = _book_keys(entry)
            theirs = set().union(*(_book_keys(m) for m in others)) if others else set()
            for path in candidates(where, skip=previous):
                key = _name_key(path.stem)
                if others and key not in mine and key in theirs:
                    continue        # named after one of the other books
                files.append(path)
                if others and key not in mine:
                    shared[path] = others
            previous = where
        return files, shared

    def _levels(self, entry: BookEntry) -> Optional[List[Tuple[Path, List[BookEntry]]]]:
        """The folders this book draws companions from, nearest first, and the books
        under each. None when other books share its folder and the list is unknown.

        The book's own folder always counts. A parent counts while it is inside the
        input folder (never the input folder itself), holds no audio but that of the
        books under it, and every book under it is the same title - the halves or
        versions of one book, not a series or an author's shelf. The first parent that
        fails ends the climb.
        """
        own = Path(entry.folder)
        known = [e for e in self.known_entries() if e.folder]
        if not known:
            if entry.is_multi_book_folder:
                return None
            known = [entry]

        def resolve(path: Path) -> Optional[Path]:
            try:
                return path.resolve()
            except (OSError, ValueError):
                return None

        own_resolved = resolve(own)
        if own_resolved is None:
            return []
        located = [(e, resolve(Path(e.folder))) for e in known
                   if e.entry_id != entry.entry_id]
        located = [(e, f) for e, f in located if f is not None]
        located.append((entry, own_resolved))

        levels: List[Tuple[Path, List[BookEntry]]] = []
        if own.is_dir():
            levels.append((own, [e for e, f in located if f == own_resolved]))

        try:
            root = self.settings.get_path('AO_INPUT_DIR').resolve()
            own_resolved.relative_to(root)
        except (OSError, ValueError):
            return levels

        current, resolved = own, own_resolved
        title = _title_key(entry)
        while True:
            parent, parent_resolved = current.parent, resolved.parent
            if parent_resolved == resolved or parent_resolved == root:
                break
            try:
                parent_resolved.relative_to(root)
            except ValueError:
                break
            members = [e for e, f in located
                       if f == parent_resolved or parent_resolved in f.parents]
            if any(_title_key(m) != title for m in members):
                break
            current, resolved = parent, parent_resolved
            if not parent.is_dir():
                continue    # already emptied and removed; what is above may remain
            folders = {f for e, f in located if e in members}
            if self._holds_audio(parent, besides=folders):
                break
            levels.append((parent, members))
        return levels

    def _needs(self, other: BookEntry, path: Path) -> bool:
        """Whether another book sharing ``path`` still has to receive a copy of it."""
        folder = Path(other.folder)
        if any((folder / name).is_file() for name in other.audio_files):
            return True     # not written yet
        if other.applied_path and Path(other.applied_path).is_dir():
            return not self._has_copy(Path(other.applied_path), path)
        return False

    @staticmethod
    def _has_copy(folder: Path, path: Path) -> bool:
        """Whether ``folder`` already holds this file, under whatever name."""
        try:
            size = path.stat().st_size
            suffix = path.suffix.lower()
            return any(p.suffix.lower() == suffix and p.is_file()
                       and p.stat().st_size == size for p in folder.rglob('*'))
        except OSError:
            return False

    def _operation_for(self, path: Path,
                       shared: Dict[Path, List[BookEntry]]) -> str:
        """Copy a shared file while another book still needs it; the last one moves it."""
        if self.copy_mode:
            return 'copy'
        if any(self._needs(other, path) for other in shared.get(path, ())):
            return 'copy'
        return 'move'

    @staticmethod
    def _holds_audio(folder: Path, besides: Iterable[Path] = ()) -> bool:
        """Whether any audio sits in this folder or below - that is another book.

        Audio directly in one of ``besides`` (books' own folders, resolved) does not
        count.
        """
        from .file_scanner import AUDIO_EXTENSIONS
        besides = set(besides)
        try:
            for path in folder.rglob('*'):
                if path.suffix.lower() not in AUDIO_EXTENSIONS or not path.is_file():
                    continue
                if besides and path.resolve().parent in besides:
                    continue
                return True
            return False
        except OSError:
            return True

    @staticmethod
    def _relative_name(entry: BookEntry, path: Path) -> str:
        """The file's path below the book's folder - its name, or "Extras/x.pdf".

        A file collected from a parent folder is placed relative to that parent, so
        "Book/Scans/back.jpg" still lands in "Scans".
        """
        folder = Path(entry.folder)
        for base in (folder, *folder.parents):
            try:
                return path.relative_to(base).as_posix()
            except ValueError:
                continue
        return path.name

    def preview(self, entry: BookEntry) -> ApplyResult:
        """What applying this entry would do. No filesystem writes."""
        return self._apply(entry, force_dry_run=True)

    def apply_entry(self, entry: BookEntry) -> ApplyResult:
        """Move or copy this entry's files into the output tree."""
        return self._apply(entry, force_dry_run=self.dry_run)

    def _apply(self, entry: BookEntry, force_dry_run: bool) -> ApplyResult:
        dry_run = force_dry_run

        # A rejected book is never written, whoever asked. The window already sends
        # only approved rows, but "rejected" is a decision about the book itself, not
        # about one caller's list, and the place to enforce it is the one function
        # that actually touches the disk. A preview still shows where it would have
        # gone - looking is not putting it there - but as skipped, and without
        # claiming the destination from a book that will really be written.
        if entry.status == STATUS_REJECTED:
            planned: List[Dict] = []
            destination = Path()
            if dry_run:
                files = self.files_for(entry)
                if files:
                    destination = self.destination_for(entry)
                    rename_map = self._rename_map(entry, files)
                    operation = 'copy' if self.copy_mode else 'move'
                    planned = [{'source': str(path),
                                'destination': str(destination
                                                   / rename_map.get(
                                                   path, self._relative_name(entry,
                                                                             path))),
                                'operation': operation} for path in files]
            return ApplyResult(entry.entry_id, destination, planned, skipped=True,
                               dry_run=dry_run, reason=REJECTED_REASON)

        files, shared = self._owned(entry)
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
            target = destination / rename_map.get(path,
                                                  self._relative_name(entry, path))
            planned.append({'source': str(path), 'destination': str(target),
                            'operation': self._operation_for(path, shared)
                            if path in shared else operation})

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
                if target.parent != destination and not target.parent.exists():
                    for folder in self._existing_ancestors(target.parent):
                        transaction.created_dirs.append(str(folder))
                    target.parent.mkdir(parents=True, exist_ok=True)
                final = self._transfer(source, target, plan['operation'])
                transaction.moves.append(
                    FileMove(source=str(source), destination=str(final),
                             operation=plan['operation']))
                done.append({**plan, 'destination': str(final)})
            except OSError as exc:
                self.logger.error('Failed to %s %s: %s', plan['operation'], source, exc)
                self.journal.record(transaction)  # keep what did happen, so it's undoable
                return ApplyResult(entry.entry_id, destination, done,
                                   error=f'{plan["operation"]} failed on '
                                         f'{source.name}: {exc}')

        self.journal.record(transaction)
        self._claimed.add(destination)
        entry.applied_path = str(destination)

        self._write_extras(entry, destination, transaction)
        if operation == 'move':
            self._remove_emptied_source(Path(entry.folder))
        return ApplyResult(entry.entry_id, destination, done)

    def leftovers_for(self, entry: BookEntry) -> List[Path]:
        """Companion files a finalized book left behind in its source folders."""
        return self._leftovers(entry)[0]

    def _leftovers(self, entry: BookEntry
                   ) -> Tuple[List[Path], Dict[Path, List[BookEntry]]]:
        if not entry.applied_path or not Path(entry.applied_path).is_dir():
            return [], {}
        destination = Path(entry.applied_path)
        files, shared = self._companions(entry)
        # Already there - a shared file a sibling's run copied in, or a second click.
        files = [p for p in files if not self._has_copy(destination, p)]
        return files, shared

    def collect_leftovers(self, entry: BookEntry) -> ApplyResult:
        """Bring the files a finalized book left behind into the folder it went to.

        Named and placed exactly as Finalize would have, journaled the same way so it
        can be undone, and the source folders removed once they are empty.
        """
        destination = Path(entry.applied_path or '')
        files, shared = self._leftovers(entry)
        if not files:
            return ApplyResult(entry.entry_id, destination, [], skipped=True,
                               reason='nothing left behind')

        rename_map = self._rename_map(entry, files)
        transaction = Transaction(entry_id=entry.entry_id, destination=str(destination))
        done: List[Dict] = []
        error = ''
        for source in files:
            target = destination / rename_map.get(source,
                                                  self._relative_name(entry, source))
            operation = self._operation_for(source, shared)
            try:
                if not target.parent.exists():
                    for folder in self._existing_ancestors(target.parent):
                        transaction.created_dirs.append(str(folder))
                    target.parent.mkdir(parents=True, exist_ok=True)
                final = self._transfer(source, target, operation)
            except OSError as exc:
                self.logger.error('Failed to %s %s: %s', operation, source, exc)
                error = f'{operation} failed on {source.name}: {exc}'
                break
            transaction.moves.append(FileMove(source=str(source), destination=str(final),
                                              operation=operation))
            done.append({'source': str(source), 'destination': str(final),
                         'operation': operation})

        if transaction.moves:
            self.journal.record(transaction)
        if not self.copy_mode and not error:
            start = Path(entry.folder)
            while not start.exists() and start.parent != start:
                start = start.parent
            try:
                start.resolve().relative_to(
                    self.settings.get_path('AO_INPUT_DIR').resolve())
                self._remove_emptied_source(start)
            except (OSError, ValueError):
                pass
        return ApplyResult(entry.entry_id, destination, done, error=error)

    # --------------------------------------------------------------- internals

    def _rename_map(self, entry: BookEntry, files: List[Path]) -> Dict[Path, str]:
        """New filenames, when file renaming is enabled (#22).

        The audio and the companion files are two separate switches: renaming the
        e-book and the cover to match does not depend on renaming the audio.
        """
        from .paths import render_template

        from .file_scanner import AUDIO_EXTENSIONS

        template = self.settings.get('AO_FILE_TEMPLATE')
        mapping: Dict[Path, str] = {}
        mapping.update(self._support_rename_map(entry, files, template))
        if not self.settings.get_bool('AO_RENAME_FILES', False):
            return mapping
        # Audio is what the audio template names. Everything else is a companion file,
        # whatever its extension - testing against the sidecar list left anything
        # unlisted (.m3u, .sfv, .log) to be renamed as though it were a chapter.
        audio = [p for p in files if p.suffix.lower() in AUDIO_EXTENSIONS]
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

        folder = Path(entry.folder)
        # Loose in the book's folder, or loose in a parent folder it owns.
        homes = {folder, *folder.parents}
        support = [p for p in files if p.suffix.lower() not in AUDIO_EXTENSIONS
                   and p.parent in homes]
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

    def _remove_emptied_source(self, folder: Path) -> None:
        """Remove the book's source folder once a move has left it empty.

        Then its parents, while they are empty too, up to - never including - the
        input folder. Only OS clutter (Thumbs.db, desktop.ini, .DS_Store), junk
        (AO_JUNK_PATTERNS, while AO_REMOVE_JUNK is on) and empty subfolders count as
        empty; anything else keeps the folder.
        """
        try:
            root = self.settings.get_path('AO_INPUT_DIR').resolve()
        except (OSError, ValueError):
            root = None
        current = folder
        while True:
            try:
                resolved = current.resolve()
            except OSError:
                return
            if root is not None and resolved == root:
                return
            if not self._clear_if_empty(current):
                return
            try:
                current.rmdir()
            except OSError as exc:
                self.logger.info('Could not remove %s: %s', current, exc)
                return
            self.logger.info('Removed emptied source folder %s', current)
            parent = current.parent
            # Past the book's own folder, only climb inside the input folder.
            if root is None or parent == current:
                return
            try:
                parent.resolve().relative_to(root)
            except (OSError, ValueError):
                return
            current = parent

    def _clear_if_empty(self, folder: Path) -> bool:
        """Delete clutter, junk and empty subfolders; True if the folder is now empty.

        A junk folder (".pad") goes whole, unless audio is somehow inside it.
        """
        try:
            children = list(folder.iterdir())
        except OSError:
            return False
        junk = []
        for child in children:
            if self.is_junk(child) and not (child.is_dir() and self._holds_audio(child)):
                junk.append(child)
            elif child.is_dir():
                if not self._clear_if_empty(child):
                    return False
            elif child.name.lower() not in CLUTTER_NAMES:
                return False
        for child in children:
            try:
                if child in junk and child.is_dir():
                    shutil.rmtree(long_path(child))
                elif child.is_dir():
                    child.rmdir()
                else:
                    child.unlink()
            except OSError:
                return False
            if child in junk:
                self.logger.info('Deleted junk %s', child)
        return True

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
