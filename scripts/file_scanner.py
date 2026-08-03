"""Turns a directory tree into :class:`BookEntry` objects.

The rule is: **the folder that directly contains audio files is the unit of work.**

- One audio file in a folder            -> one entry
- Many numbered chapter files           -> one entry (the folder is the book)
- Many full-length books in one folder  -> one entry per file, sharing the folder

Distinguishing the last two is the whole trick, and is done by
:meth:`_looks_like_chapters`.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .models import BookEntry

# Anything mutagen has a chance of reading, plus the DRM'd Audible formats - we attempt
# those too and simply fall back to filename parsing when the tags are unreadable.
AUDIO_EXTENSIONS = ('.m4b', '.mp3', '.m4a', '.flac', '.ogg', '.opus', '.wma', '.aac',
                    '.aax', '.aa', '.wav', '.mp4', '.webm')
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif')

# Files whose names are a bare number, or number + title, in a consistent run.
_CHAPTER_PATTERNS = (
    re.compile(r'^\s*(\d{1,3})\s*$'),
    re.compile(r'^\s*(?:track|chapter|chap|ch|part|pt|cd|disc|disk)[\s._-]*(\d{1,3})\b', re.I),
    re.compile(r'^\s*(\d{1,3})\s*[-._) ]'),
    re.compile(r'[\s._-](\d{1,3})\s*(?:of|/)\s*\d{1,3}\s*$', re.I),
)


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

    def _entries_for_folder(self, folder: Path, audio_files: List[str],
                            image_files: List[str]) -> List[BookEntry]:
        """One folder of audio -> one entry, or N entries if it holds N books."""
        if len(audio_files) > 1 and not self.looks_like_chapters(folder, audio_files):
            # A folder of several distinct books: each file is its own entry, but they
            # keep a shared folder so the resolver can reason about them as a series.
            return [self._make_entry(folder, [name], image_files, multi_book=True)
                    for name in audio_files]
        return [self._make_entry(folder, audio_files, image_files, multi_book=False)]

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

        return BookEntry(
            entry_id=entry_id,
            folder=str(folder),
            relative_path=str(relative),
            audio_files=list(audio_files),
            primary_audio=str(primary),
            image_files=list(image_files),
            is_multi_book_folder=multi_book,
        )

    # ------------------------------------------------------------- heuristics

    def looks_like_chapters(self, folder: Path, files: List[str]) -> bool:
        """True when these files are parts of one book rather than separate books.

        Duration is the honest signal: a chapter is minutes long, a whole audiobook is
        hours. Names are only consulted when durations can't be read (DRM, odd codecs).
        """
        minutes = self._median_duration_minutes(folder, files)
        if minutes is not None:
            if minutes <= 75:
                self.logger.debug('%s: median %.0f min -> chapters', folder, minutes)
                return True
            if minutes >= 150:
                self.logger.debug('%s: median %.0f min -> separate books', folder, minutes)
                return False
            # 75-150 min is ambiguous (long chapters? short books?) - ask the names.

        return self._names_look_like_chapters(files)

    @staticmethod
    def _median_duration_minutes(folder: Path, files: List[str]) -> Optional[float]:
        """Median playtime in minutes, or None if we couldn't read enough of them."""
        try:
            from mutagen import File as MutagenFile
        except ImportError:
            return None

        durations: List[float] = []
        # Sampling a few is enough for a median and keeps big folders fast.
        for name in files[:8]:
            try:
                audio = MutagenFile(str(folder / name))
                if audio is not None and getattr(audio, 'info', None):
                    length = getattr(audio.info, 'length', 0)
                    if length:
                        durations.append(length / 60.0)
            except Exception:  # unreadable/DRM file - just skip it
                continue

        if len(durations) < max(2, min(len(files), 3)) - 1:
            return None
        durations.sort()
        return durations[len(durations) // 2]

    @staticmethod
    def _names_look_like_chapters(files: List[str]) -> bool:
        """True when these files are parts of one book rather than separate books.

        Chapter sets are recognised by a consistent numeric run (1, 2, 3, ... with few
        gaps) across most of the files. Four books named "Book 1 - X", "Book 2 - Y" also
        number consistently, so we additionally require the non-numeric part of the
        names to be near-identical - chapters share a stem, separate books don't.
        """
        stems = [Path(f).stem for f in files]
        numbers: List[int] = []
        skeletons = set()

        for stem in stems:
            found = None
            for pattern in _CHAPTER_PATTERNS:
                match = pattern.search(stem)
                if match:
                    found = int(match.group(1))
                    break
            if found is None:
                return False
            numbers.append(found)
            # Remove all digits: what remains is the shared naming skeleton.
            skeletons.add(re.sub(r'\d+', '', stem).strip(' -_.[]()').lower())

        if len(set(numbers)) < len(numbers) * 0.8:
            return False  # too many duplicate indices to be a chapter run

        ordered = sorted(set(numbers))
        span = ordered[-1] - ordered[0] + 1
        if span > len(ordered) * 2:
            return False  # sparse numbering: more likely unrelated books

        # Chapters share one skeleton ("Chapter", "" or "Track"); books each carry
        # their own title in the name, producing many distinct skeletons.
        return len(skeletons) <= max(1, len(stems) // 4)

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
