"""Turns a directory tree into :class:`BookEntry` objects.

The rule is: **the folder that directly contains audio files is the unit of work.**

- One audio file in a folder            -> one entry
- Many numbered chapter files           -> one entry (the folder is the book)
- Many full-length books in one folder  -> one entry per file, sharing the folder

Distinguishing the last two is the whole trick, and is done by
:meth:`book_sets`, which asks the files what release they belong to - album tag first,
then embedded cover, then what the filenames say once the numbers come out. Never their
duration: a long file is not a book and a short one is not a chapter.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .models import BookEntry

# Anything mutagen has a chance of reading, plus the DRM'd Audible formats - we attempt
# those too and simply fall back to filename parsing when the tags are unreadable.
AUDIO_EXTENSIONS = ('.m4b', '.mp3', '.m4a', '.flac', '.ogg', '.opus', '.wma', '.aac',
                    '.aax', '.aa', '.wav', '.mp4', '.webm')
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif')

# How many files of a big folder are read before the rest are taken to agree with them.
# See FileScanner._read_tag.
_TAG_SAMPLE = 24


def _grouped(files: List[str], key: Dict[str, str]) -> List[List[str]]:
    """Files bucketed by ``key``, groups in a stable order and sorted within."""
    groups: Dict[str, List[str]] = {}
    for name in files:
        groups.setdefault(key[name], []).append(name)
    return [sorted(names) for _, names in sorted(groups.items())]


def _album_of(tags) -> str:
    """The album this file says it belongs to, normalised for comparison.

    "Ceremony in Death CD 01" and "Ceremony in Death CD 02" are one release in two
    volumes, so the disc marker comes off before comparing - otherwise a two-disc rip
    reads as two books.
    """
    for key in ('album', 'TALB', '\xa9alb', 'WM/AlbumTitle'):
        try:
            value = tags[key]
        except (KeyError, TypeError):
            continue
        text = str(value[0] if isinstance(value, list) else value)
        text = re.sub(r'[\s,\-_(\[]*\b(?:cd|disc|disk|volume|vol|part|pt)\b[\s._#-]*\d+'
                      r'[\s)\]]*$', '', text, flags=re.I)
        text = _norm_tag(text)
        if text:
            return text
    return ''


def _cover_of(tags) -> str:
    """A digest of the embedded artwork, or '' when there is none."""
    pictures = []
    if hasattr(tags, 'getall'):
        pictures = [picture.data for picture in tags.getall('APIC')]
    if not pictures:
        for key in ('covr', 'WM/Picture'):
            try:
                value = tags[key]
            except (KeyError, TypeError):
                continue
            pictures = [bytes(item) for item in
                        (value if isinstance(value, list) else [value])]
            break
    if not pictures and getattr(tags, 'pictures', None):
        pictures = [picture.data for picture in tags.pictures]
    if not pictures or not pictures[0]:
        return ''
    return hashlib.sha256(pictures[0]).hexdigest()


def _norm_tag(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(text).lower()).strip()


def _skeleton(stem: str) -> str:
    """What a filename says once every number is taken out of it - i.e. the title.

    Returns '' when nothing but digits and punctuation is left, which means the name
    identifies no book at all and cannot be grouped on.
    """
    text = re.sub(r'\d+', '', stem)
    text = re.sub(r'[\s._\-\[\]()]+', ' ', text).strip().lower()
    return text if re.search(r'[a-z]', text) else ''


def _numbers_run(files: List[str]) -> bool:
    """True when these same-titled files are numbered like the parts of one book.

    Every number in each name is read, not just the first: "06 - Vengeance in Death-3"
    carries the book's number *and* the part's, and it is the part that has to count up.
    So a run is looked for at each position in turn, and one is enough.
    """
    numbers = [tuple(int(n) for n in re.findall(r'\d+', Path(name).stem))
               for name in files]
    if not all(numbers) or len(set(numbers)) != len(numbers):
        return False        # unnumbered, or two files claiming the same position

    width = min(len(group) for group in numbers)
    for position in range(width):
        values = sorted({group[position] for group in numbers})
        if len(values) != len(numbers):
            continue        # this position repeats - it is the book number, not the part
        if values[-1] - values[0] + 1 <= len(values) * 2:
            return True     # counts up with few enough gaps to be one book's parts
    return False


class FileScanner:
    def __init__(self, input_dir: str, audio_extensions: Sequence[str] = AUDIO_EXTENSIONS):
        self.input_dir = Path(input_dir)
        self.supported_audio = tuple(e.lower() for e in audio_extensions)
        self.supported_images = IMAGE_EXTENSIONS
        self.logger = logging.getLogger(__name__)

    # ---------------------------------------------------------------- scanning

    def scan_directory(self) -> List[BookEntry]:
        """Walk the input tree and return one entry per detected book."""
        if not self.input_dir.exists():
            self.logger.error('Input directory does not exist: %s', self.input_dir)
            return []

        entries: List[BookEntry] = []
        for root, dirs, files in os.walk(self.input_dir):
            dirs.sort()
            root_path = Path(root)
            audio_files = sorted(f for f in files
                                 if f.lower().endswith(self.supported_audio))
            if not audio_files:
                continue

            image_files = sorted(f for f in files
                                 if f.lower().endswith(self.supported_images))
            entries.extend(self._entries_for_folder(root_path, audio_files, image_files))

        self.logger.info('Scanned %s: %d entr%s found',
                         self.input_dir, len(entries), 'y' if len(entries) == 1 else 'ies')
        return entries

    def audio_index(self) -> Optional[Dict[str, int]]:
        """Every audio file under the input folder -> its size. None if unreadable.

        Directory listings only, no tag reads: enough to answer "does the table still
        describe what is on disk?" in well under a second on a large library, which is
        why it can run at start-up.
        """
        if not self.input_dir.exists():
            return None
        index: Dict[str, int] = {}
        for root, dirs, files in os.walk(self.input_dir):
            dirs.sort()
            for name in files:
                if not name.lower().endswith(self.supported_audio):
                    continue
                path = Path(root) / name
                try:
                    index[os.path.normcase(str(path))] = path.stat().st_size
                except OSError:
                    index[os.path.normcase(str(path))] = -1
        return index

    def compare_to_entries(self, entries: List[BookEntry]) -> Dict[str, int]:
        """How far the loaded entries have drifted from what is on disk now.

        Returns counts of files ``added``, ``missing`` and ``changed`` (same path, new
        size). All zero means a rescan would find nothing new - which is the only case
        in which carrying on with saved entries is safe rather than merely convenient.
        """
        index = self.audio_index()
        if index is None:
            return {'added': 0, 'missing': 0, 'changed': 0, 'unreadable': 1}

        # The size the file had *when it was scanned*, not the size it has now - the
        # whole question is whether those two still agree. An entry saved before sizes
        # were recorded reports None and is compared on paths alone.
        known: Dict[str, Optional[int]] = {}
        for entry in entries:
            sizes = list(entry.audio_sizes)
            for position, path in enumerate(entry.absolute_files()):
                known[os.path.normcase(str(path))] = (sizes[position]
                                                      if position < len(sizes) else None)

        # Entries can point outside the input folder - anything already applied and
        # moved does - and those are not "missing from disk", they are simply not here.
        inside = {key: size for key, size in known.items()
                  if key.startswith(os.path.normcase(str(self.input_dir)))}

        added = [key for key in index if key not in inside]
        missing = [key for key in inside if key not in index]
        changed = [key for key, size in inside.items()
                   if size is not None and key in index and index[key] != size]
        return {'added': len(added), 'missing': len(missing),
                'changed': len(changed), 'unreadable': 0}

    def _entries_for_folder(self, folder: Path, audio_files: List[str],
                            image_files: List[str]) -> List[BookEntry]:
        """One folder of audio -> one entry, or N entries if it holds N books."""
        if len(audio_files) > 1:
            # A whole-book file dropped in among the chapter files - the same book twice,
            # in two forms. It is its own entry; the rest are still one chapter set.
            whole, audio_files = self._split_whole_books(folder, audio_files)
            if whole:
                entries = [self._make_entry(folder, [name], image_files, multi_book=True)
                           for name in whole]
                if audio_files:
                    entries.extend(self._entries_for_folder(folder, audio_files,
                                                            image_files)
                                   if len(audio_files) > 1
                                   else [self._make_entry(folder, audio_files,
                                                          image_files, multi_book=True)])
                return entries

            sets = self.book_sets(folder, audio_files)
            if len(sets) > 1:
                # Several books in one folder. Each keeps the shared folder so the
                # resolver can still reason about them together as a series.
                return [self._make_entry(folder, names, image_files, multi_book=True)
                        for names in sets]
            audio_files = sets[0]
        return [self._make_entry(folder, audio_files, image_files, multi_book=False)]

    def _split_whole_books(self, folder: Path,
                           files: List[str]) -> Tuple[List[str], List[str]]:
        """Separate any file that is plainly the entire book from its own chapters.

        "J.D. Robb - 05 Ceremony in Death" holds nine 68-minute parts *and* a single
        608-minute file of the whole thing. Left together they became one entry whose
        chapter list plays the book twice; the merge would have produced exactly that.

        Judged on file size, which costs a stat rather than a tag read: a file is the
        whole book when it dwarfs the median (4x) and is at least half as big as
        everything else in the folder put together. A chapter set with one long chapter
        in it fails the second test, which is what that test is for.
        """
        if len(files) < 3:
            return [], files

        sizes = {}
        for name in files:
            try:
                sizes[name] = (folder / name).stat().st_size
            except OSError:
                return [], files
        if not all(sizes.values()):
            return [], files

        ordered = sorted(sizes.values())
        median = ordered[len(ordered) // 2]
        total = sum(ordered)

        whole = [name for name, size in sizes.items()
                 if size >= 4 * median and size * 2 >= total - size]
        if not whole or len(whole) == len(files):
            return [], files

        self.logger.debug('%s: %s look like the whole book, not chapters',
                          folder, ', '.join(whole))
        return sorted(whole), [name for name in files if name not in set(whole)]

    def _make_entry(self, folder: Path, audio_files: List[str], image_files: List[str],
                    multi_book: bool) -> BookEntry:
        primary = folder / audio_files[0]
        try:
            relative = folder.relative_to(self.input_dir)
        except ValueError:
            relative = folder
        # Entry id must be stable across runs so scans can resume (#27) - it is derived
        # purely from paths, never from resolved metadata.
        entry_id = str(Path(relative) / audio_files[0]) if multi_book else str(relative)
        if entry_id in ('.', ''):
            entry_id = folder.name

        sizes = []
        for name in audio_files:
            try:
                sizes.append((folder / name).stat().st_size)
            except OSError:
                sizes.append(-1)

        return BookEntry(
            entry_id=entry_id,
            folder=str(folder),
            relative_path=str(relative),
            audio_files=list(audio_files),
            audio_sizes=sizes,
            primary_audio=str(primary),
            image_files=list(image_files),
            is_multi_book_folder=multi_book,
        )

    # ------------------------------------------------------------- heuristics

    def book_sets(self, folder: Path, files: List[str]) -> List[List[str]]:
        """Group a folder's audio files into one list per book. Never empty.

        A folder can hold one book's parts, several whole books, or several books' parts
        loose together, and the same three names can mean any of them. So the question
        asked is never "how long is this file" - it is **which of these files say they
        belong to the same release**, in order of how much the answer can be trusted:

        1. **The album tag.** Two files tagged ``album=Primal Fury`` are two parts of
           *Primal Fury*, whether they run four minutes or seven hours. Two files tagged
           with different albums are different books at any length.
        2. **The embedded cover.** Same artwork, same release. Different artwork,
           different releases.
        3. **The filenames.** What is left of a name once the numbers come out is the
           title: files that differ only by a number are parts of one book, files
           carrying different titles are different books.

        There is deliberately no rule about duration. "A file over N hours is a book,
        under N is a chapter" is an arbitrary line that splits a two-part audiobook into
        two books for no reason other than that its halves are long, and length is not
        evidence of anything - a 14-hour book in two 7-hour files and two 7-hour books in
        one folder are the same numbers.

        When nothing here can tell, the answer is one book. Wrongly joining a folder is
        one entry to split; wrongly splitting is N entries to find and merge, and it is
        the error that made a chapter look like a duplicate of its own book.
        """
        if len(files) < 2:
            return [list(files)]

        for evidence, groups in (('album tag', self._group_by_album(folder, files)),
                                 ('embedded cover', self._group_by_cover(folder, files)),
                                 ('filenames', self._group_by_name(files))):
            if groups is None:
                continue
            self.logger.debug('%s: %d book(s) by %s', folder, len(groups), evidence)
            return groups

        self.logger.debug('%s: nothing distinguishes these files - treating as one book',
                          folder)
        return [list(files)]

    # -- the three kinds of evidence. Each returns None for "cannot tell", so the next
    #    one is asked; a list of groups is an answer, including a single group meaning
    #    "all of this is one book".

    def _group_by_album(self, folder: Path,
                        files: List[str]) -> Optional[List[List[str]]]:
        """Group on the album tag, which is where a release names itself."""
        albums = self._read_tag(folder, files, _album_of)
        if not albums or len(albums) < len(files):
            return None            # some file would not say, so this cannot decide
        return _grouped(files, albums)

    def _group_by_cover(self, folder: Path,
                        files: List[str]) -> Optional[List[List[str]]]:
        """Group on the embedded artwork, hashed."""
        covers = self._read_tag(folder, files, _cover_of)
        if not covers or len(covers) < len(files):
            return None
        return _grouped(files, covers)

    def _group_by_name(self, files: List[str]) -> Optional[List[List[str]]]:
        """Group on what the filenames say once their numbers are removed."""
        titles = {}
        for name in files:
            skeleton = _skeleton(Path(name).stem)
            # "13-17.mp3" is nothing but numbers: it names no book, so the names as a
            # whole cannot answer and guessing would merge unrelated books.
            if not skeleton:
                return None
            titles[name] = skeleton

        groups = _grouped(files, titles)
        # A group of several files that does not number like a run is not one book's
        # parts - it is different books that happen to reduce to the same stem.
        for group in groups:
            if len(group) > 1 and not _numbers_run(group):
                return None
        return groups

    def _read_tag(self, folder: Path, files: List[str], reader) -> Dict[str, str]:
        """``reader`` applied to each file's tags, skipping whatever cannot be read.

        Big folders are sampled first: when the first two dozen files all agree, reading
        the remaining two hundred cannot change the answer, and a chapter-per-file book
        is exactly the shape where that matters.
        """
        try:
            from mutagen import File as MutagenFile
        except ImportError:
            return {}

        def read(names: List[str]) -> Dict[str, str]:
            found = {}
            for name in names:
                try:
                    tags = MutagenFile(str(folder / name))
                except Exception:              # unreadable or DRM'd - no opinion
                    continue
                if tags is None:
                    continue
                try:
                    value = reader(tags)
                except Exception:
                    continue
                if value:
                    found[name] = value
            return found

        if len(files) <= _TAG_SAMPLE:
            return read(files)

        sample = read(files[:_TAG_SAMPLE])
        if len(sample) == _TAG_SAMPLE and len(set(sample.values())) == 1:
            agreed = next(iter(sample.values()))
            return {name: agreed for name in files}
        sample.update(read(files[_TAG_SAMPLE:]))
        return sample

    @staticmethod
    def _names_look_like_chapters(files: List[str]) -> bool:
        """True when these filenames are parts of one book rather than separate books.

        Which number counts is decided by :func:`_numbers_run`, not by the first one in
        the name. "06 - Vengeance in Death-1.mp3" through "-8.mp3" carries the book's
        number first and the part's second; reading only the first found the same 6
        eight times over, called that "too many duplicate indices to be a chapter run",
        and split one book into eight - which is why every part after the first was then
        called a duplicate of part one.
        """
        stems = [Path(f).stem for f in files]
        # Remove all digits: what remains is the shared naming skeleton. Chapters share
        # one ("Chapter", "" or "Track"); books each carry their own title in the name,
        # producing many distinct skeletons.
        skeletons = {re.sub(r'\d+', '', stem).strip(' -_.[]()').lower()
                     for stem in stems}
        if len(skeletons) > max(1, len(stems) // 4):
            return False
        return _numbers_run(files)

    # ---------------------------------------------------------------- display

    def folder_structure(self, entry: BookEntry) -> str:
        """Human-readable tree for the entry, shown in the GUI's first column."""
        lines = [entry.relative_path or entry.folder]
        for name in entry.audio_files:
            lines.append(f'  {name}')
        for name in entry.image_files:
            lines.append(f'  {name}')
        return '\n'.join(lines)

    def sibling_context(self, entries: List[BookEntry]) -> Dict[str, List[BookEntry]]:
        """Group entries by folder, so a folder can be resolved as one series (#11)."""
        groups: Dict[str, List[BookEntry]] = {}
        for entry in entries:
            groups.setdefault(entry.folder, []).append(entry)
        return groups
