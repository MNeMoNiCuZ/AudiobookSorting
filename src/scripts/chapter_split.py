"""Split one chaptered .m4b into several books, each in a folder of its own.

The reverse of chapter_merge. An omnibus that arrived as a single file is several
books, and the list can only file it as one. The chapters already mark where each
book starts, so each new book is a run of chapters cut out with ``-c copy`` - no
re-encode, no quality lost, and seconds rather than minutes.

Requires ffmpeg on PATH (or `AO_FFMPEG_PATH`).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from .chapter_merge import (ProgressCallback, _discard, _metadata_text,
                            _run_streaming, ffmpeg_available, probe_duration)
from .paths import temp_dir

logger = logging.getLogger(__name__)

SPLITTABLE = ('.m4b', '.m4a', '.mp4')


class Chapter(NamedTuple):
    start: float        # seconds
    end: float
    title: str


class SplitPart(NamedTuple):
    """One new book: a span of the source, and the file it is written to."""

    start: float
    end: float
    destination: Path
    values: Dict[str, str]      # author / series / series_index / title
    # The source chapters this book is made of. Kept as the new book's own chapter
    # marks, so a book cut from an omnibus can still be skipped through.
    chapters: Tuple[Chapter, ...] = ()


def read_chapters(path: Path, ffmpeg: str = 'ffmpeg') -> List[Chapter]:
    """The chapters of an MP4-family file, in order. Empty when it has none.

    mutagen first - it reads the chapter list without spawning anything - and
    ffprobe for the files mutagen cannot parse.
    """
    path = Path(path)
    if path.suffix.lower() not in SPLITTABLE:
        return []
    chapters = _mutagen_chapters(path)
    if not chapters:
        chapters = _ffprobe_chapters(path, ffmpeg)
    return chapters


def _mutagen_chapters(path: Path) -> List[Chapter]:
    try:
        from mutagen.mp4 import MP4
        audio = MP4(str(path))
    except Exception:
        return []
    marks = list(getattr(audio, 'chapters', None) or [])
    length = float(getattr(getattr(audio, 'info', None), 'length', 0) or 0)
    if not marks or not length:
        return []
    out = []
    for index, mark in enumerate(marks):
        start = float(mark.start)
        end = float(marks[index + 1].start) if index + 1 < len(marks) else length
        if end > start:
            out.append(Chapter(start, end, str(mark.title or '').strip()
                               or f'Chapter {index + 1}'))
    return out


def _ffprobe_chapters(path: Path, ffmpeg: str) -> List[Chapter]:
    ffprobe = 'ffprobe' if ffmpeg == 'ffmpeg' else str(Path(ffmpeg).with_name('ffprobe'))
    if not shutil.which(ffprobe):
        return []
    creation = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        output = subprocess.run(
            [ffprobe, '-v', 'error', '-show_chapters', '-of', 'json', str(path)],
            capture_output=True, text=True, timeout=60, creationflags=creation)
        data = json.loads(output.stdout or '{}')
    except (subprocess.SubprocessError, OSError, ValueError):
        return []
    out = []
    for index, chapter in enumerate(data.get('chapters') or []):
        try:
            start = float(chapter.get('start_time', 0))
            end = float(chapter.get('end_time', 0))
        except (TypeError, ValueError):
            continue
        title = str((chapter.get('tags') or {}).get('title') or '').strip()
        if end > start:
            out.append(Chapter(start, end, title or f'Chapter {index + 1}'))
    return out


def split_m4b(source: Path, parts: List[SplitPart], ffmpeg: str = 'ffmpeg',
              on_progress: ProgressCallback = None,
              should_cancel: Optional[Callable[[], bool]] = None
              ) -> Tuple[bool, str, List[Path]]:
    """Write every part. Returns ``(success, message, files written)``.

    All or nothing: if any part fails or the run is cancelled, every file and folder
    this run created is taken away again, so a half-split book never sits beside the
    original looking finished.
    """
    source = Path(source)
    if not parts:
        return False, 'Nothing to split', []
    if not ffmpeg_available(ffmpeg):
        return False, (f'ffmpeg not found (looked for {ffmpeg!r}). Set AO_FFMPEG_PATH '
                       f'on the Settings page or install ffmpeg.'), []
    if not source.is_file():
        return False, f'Missing source file: {source}', []

    cancelled = should_cancel or (lambda: False)
    total = len(parts)
    written: List[Path] = []
    made_folders: List[Path] = []
    workdir = temp_dir('split')
    try:
        return _split(source, parts, ffmpeg, report_to=on_progress,
                      cancelled=cancelled, workdir=workdir, written=written,
                      made_folders=made_folders)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _split(source: Path, parts: List[SplitPart], ffmpeg: str, report_to, cancelled,
           workdir: Path, written: List[Path],
           made_folders: List[Path]) -> Tuple[bool, str, List[Path]]:
    total = len(parts)

    def report(step: float, message: str) -> None:
        if report_to:
            report_to(step, total, message)

    def undo(detail: str) -> Tuple[bool, str, List[Path]]:
        for path in written:
            _discard(path)
        for folder in reversed(made_folders):
            try:
                folder.rmdir()
            except OSError:
                pass
        return False, detail, []

    for index, part in enumerate(parts):
        if cancelled():
            return undo('Cancelled - the books already written were removed')
        destination = Path(part.destination)
        folder = destination.parent
        if not folder.exists():
            try:
                folder.mkdir(parents=True)
            except OSError as exc:
                return undo(f'Could not create {folder}: {exc}')
            made_folders.append(folder)

        name = destination.name
        length = max(0.001, part.end - part.start)
        report(index, f'Writing book {index + 1} of {total}: {name}')

        def on_time(done_ms: float, index=index, name=name, length=length) -> None:
            fraction = min(1.0, done_ms / 1000.0 / length)
            report(index + fraction,
                   f'Writing book {index + 1} of {total}: {name}  -  {fraction:.0%}')

        marks = _chapter_file(workdir, index, part)
        ok, detail = _cut(source, part, ffmpeg, cancelled, on_time, marks, cover=True)
        if not ok and not cancelled():
            # Not every cover survives being copied into a cut - a PNG cover in some
            # files is refused by the mp4 muxer. The book matters more than its picture.
            logger.info('Split of %s with cover failed (%s); retrying without',
                        name, detail)
            _discard(destination)
            ok, detail = _cut(source, part, ffmpeg, cancelled, on_time, marks,
                              cover=False)
        if not ok:
            _discard(destination)
            return undo(detail or f'Could not write {name}')
        written.append(destination)
        if not destination.exists() or destination.stat().st_size == 0:
            return undo(f'ffmpeg produced no output for {name}')
        # Stream copy cuts on packet boundaries, so a few hundred milliseconds either
        # way is expected. Minutes missing is not.
        written_s = probe_duration(destination, ffmpeg)
        if written_s and written_s < length - 5:
            return undo(f'{name} came out {written_s / 60:.0f} min long; '
                        f'expected {length / 60:.0f} min')

    report(total, f'Split into {total} books')
    return True, f'Split {source.name} into {total} book{"" if total == 1 else "s"}', \
        written


def _chapter_file(workdir: Path, index: int, part: SplitPart) -> Optional[Path]:
    """An ffmetadata file of the part's chapters, re-timed from its own start."""
    if len(part.chapters) < 2:
        return None
    lengths, names = [], []
    for chapter in part.chapters:
        start = max(chapter.start, part.start)
        end = min(chapter.end, part.end)
        if end > start:
            lengths.append(max(1, int(round((end - start) * 1000))))
            names.append(chapter.title)
    if len(lengths) < 2:
        return None
    path = workdir / f'chapters-{index:03d}.txt'
    path.write_text(_metadata_text('', '', names, lengths), encoding='utf-8')
    return path


def _cut(source: Path, part: SplitPart, ffmpeg: str, cancelled,
         on_time, marks: Optional[Path], cover: bool) -> Tuple[bool, str]:
    values = part.values
    title = values.get('title', '')
    author = values.get('author', '')
    metadata = []
    for key, value in (('title', title), ('album', title), ('artist', author),
                       ('album_artist', author)):
        if value:
            metadata += ['-metadata', f'{key}={value}']
    if values.get('series_index'):
        metadata += ['-metadata', f'track={values["series_index"]}']

    maps = ['-map', '0:a:0']
    if cover:
        maps += ['-map', '0:v?', '-disposition:v', 'attached_pic']
    command = [
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-progress', 'pipe:1', '-nostats',
        # -ss before -i seeks the input rather than decoding up to the start, which
        # is what makes cutting book five out of a 40-hour file take seconds.
        '-ss', f'{part.start:.3f}', '-i', str(source),
        *(['-i', str(marks)] if marks else []),
        '-t', f'{part.end - part.start:.3f}',
        # The chapter file carries no book tags, so taking metadata from it drops the
        # omnibus's own tags as -1 would, and keeps the chapter names, which -1 does not.
        *maps, '-c', 'copy', '-map_metadata', '1' if marks else '-1',
        '-map_chapters', '1' if marks else '-1',
        *metadata, '-f', 'mp4', str(part.destination),
    ]
    return _run_streaming(command, cancelled, on_time)
