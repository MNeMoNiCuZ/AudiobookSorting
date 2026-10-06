"""Loose books in the root of the input folder -> one folder per book.

Every other book in the input folder is a folder, and the folder is the unit of work
(see file_scanner). Audio dropped straight into the root is the exception: it all
shares the root as its "folder", so the books in it are told apart only by the
scanner's grouping, and the root's name gets read as if it said something about them.
Folderizing gives each loose book a folder of its own, named after it, and moves its
audio - plus any file that shares an audio file's name, such as "Book.jpg" or
"Book.cue" - into it.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from .file_scanner import FileScanner, _album_of
from .models import BookEntry
from .paths import long_path, sanitize_component

logger = logging.getLogger(__name__)


class LooseBook(NamedTuple):
    folder: str            # the new folder's name, directly under the input folder
    files: List[str]       # every file name in the root that goes into it


def loose_audio(input_dir: Path, scanner: Optional[FileScanner] = None) -> List[str]:
    """Names of the audio files sitting directly in the input folder."""
    scanner = scanner or FileScanner(str(input_dir))
    try:
        names = os.listdir(input_dir)
    except OSError:
        return []
    return sorted(name for name in names
                  if name.lower().endswith(scanner.supported_audio)
                  and (Path(input_dir) / name).is_file())


def plan(input_dir: Path, scanner: Optional[FileScanner] = None) -> List[LooseBook]:
    """The folders folderizing would make, and what goes in each. Touches nothing.

    The books are grouped exactly as a scan groups them, so each folder holds what the
    list would have shown as one book.
    """
    root = Path(input_dir)
    scanner = scanner or FileScanner(str(root))
    audio = loose_audio(root, scanner)
    if not audio:
        return []
    try:
        others = sorted(name for name in os.listdir(root)
                        if (root / name).is_file() and name not in set(audio))
    except OSError:
        others = []

    taken = {name.lower() for name in os.listdir(root)}
    books: List[LooseBook] = []
    for names in scanner.book_sets(root, audio):
        stems = {Path(name).stem.lower() for name in names}
        companions = [name for name in others
                      if Path(name).stem.lower() in stems]
        folder = _free_name(root, _folder_name(root, names), taken)
        taken.add(folder.lower())
        books.append(LooseBook(folder, sorted(names + companions)))
    return books


def move(input_dir: Path, books: List[LooseBook]) -> Dict[str, str]:
    """Make each book's folder and move its files in. Returns old path -> new path.

    Stops at the first file that cannot be moved, raising OSError. What was moved
    before it stays moved, and is on the exception as ``moved`` so the list can still
    follow it.
    """
    root = Path(input_dir)
    moved: Dict[str, str] = {}
    try:
        for book in books:
            target = root / book.folder
            os.makedirs(long_path(target), exist_ok=True)
            for name in book.files:
                source, destination = root / name, target / name
                if destination.exists():
                    raise FileExistsError(f'{destination} already exists')
                shutil.move(long_path(source), long_path(destination))
                moved[str(source)] = str(destination)
                logger.info('Folderized %s -> %s', source, destination)
    except OSError as exc:
        exc.moved = moved
        raise
    return moved


def relocate_entries(entries: List[BookEntry], input_dir: Path,
                     moved: Dict[str, str]) -> Dict[str, BookEntry]:
    """Point listed books at the folders their files were moved to.

    Returns old entry id -> the updated entry, under the id a scan of the new folder
    would give it, so identification, edits and decisions carry over. A book whose
    files ended up in more than one folder is left alone: it shows as missing, and its
    files as new, which is the truth.
    """
    root = os.path.normcase(os.path.normpath(str(input_dir)))
    where = {os.path.normcase(os.path.normpath(old)): Path(new).parent
             for old, new in moved.items()}
    relocated: Dict[str, BookEntry] = {}
    for entry in entries:
        if os.path.normcase(os.path.normpath(entry.folder)) != root:
            continue
        targets = {where.get(os.path.normcase(os.path.normpath(str(path))))
                   for path in entry.absolute_files()}
        if len(targets) != 1 or None in targets:
            continue
        folder = next(iter(targets))
        old_id = entry.entry_id
        entry.entry_id = folder.name
        entry.folder = str(folder)
        entry.relative_path = folder.name
        entry.primary_audio = str(folder / entry.audio_files[0])
        entry.image_files = sorted(Path(new).name for old, new in moved.items()
                                   if Path(new).parent == folder
                                   and new.lower().endswith(
                                       ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif')))
        entry.is_multi_book_folder = False
        entry.log('user', f'Moved from the root of the input folder into "{folder.name}"')
        relocated[old_id] = entry
    return relocated


def _folder_name(root: Path, names: List[str]) -> str:
    """What a loose book's folder is called: its file's name, or what its files share."""
    if len(names) == 1:
        return sanitize_component(Path(names[0]).stem, fallback='Book')

    album = _shared_album(root, names)
    if album:
        return sanitize_component(album, fallback='Book')

    prefix = os.path.commonprefix([Path(name).stem for name in names])
    # "Dune - 01", "Dune - 02" share "Dune - 0": the part number and what leads up to
    # it are not the title.
    prefix = re.sub(r'[\s._\-#(\[]*(?:part|pt|cd|disc|chapter|ch)?[\s._#-]*\d*$', '',
                    prefix, flags=re.I).strip()
    if re.search(r'[A-Za-z]', prefix):
        return sanitize_component(prefix, fallback='Book')
    return sanitize_component(Path(names[0]).stem, fallback='Book')


def _shared_album(root: Path, names: List[str]) -> str:
    """The album tag every file carries, as written in the first one, or ''."""
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        return ''
    first, seen = '', set()
    for name in names[:24]:
        try:
            audio = MutagenFile(long_path(root / name))
        except Exception:
            return ''
        tags = getattr(audio, 'tags', None) if audio is not None else None
        if not tags or not _album_of(tags):
            return ''
        seen.add(_album_of(tags))
        if not first:
            for key in ('album', 'TALB', '\xa9alb', 'WM/AlbumTitle'):
                try:
                    value = tags[key]
                except (KeyError, TypeError, ValueError):
                    continue
                first = str(value[0] if isinstance(value, list) else value).strip()
                if first:
                    break
    return first if len(seen) == 1 else ''


def _free_name(root: Path, name: str, taken) -> str:
    """``name``, or "name (2)", "name (3)"... - whichever is not already in the root."""
    if name.lower() not in taken and not (root / name).exists():
        return name
    for counter in range(2, 1000):
        candidate = f'{name} ({counter})'
        if candidate.lower() not in taken and not (root / candidate).exists():
            return candidate
    raise OSError(f'Could not find a free folder name near {root / name}')
