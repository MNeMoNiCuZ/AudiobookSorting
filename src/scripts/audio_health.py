"""Audio files whose header lies about how long they are.

An MP3 made by gluing several MP3s together (an Audible intro plus the book is the
usual case) keeps the first piece's Xing/LAME header. That header carries a frame
count, and players trust it: the book shows up as 19 seconds long and stops after
the intro, although all ten hours of audio are in the file.

The check compares what the header claims with what the file's size says at its
bitrate. The repair is a stream copy through ffmpeg - nothing is re-encoded - which
writes one header that describes the whole file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .chapter_merge import _run_streaming, ffmpeg_available

logger = logging.getLogger(__name__)

# A header has to be short by both of these before the file is called broken: a
# minute absorbs tags and padding on a short file, the ratio a VBR average on a long one.
_MIN_MISSING_SECONDS = 60
_MIN_RATIO = 1.5


def _clock(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes}:{secs:02d}'


def mp3_lengths(path: Path) -> Optional[Tuple[float, float]]:
    """(seconds the header claims, seconds the audio data holds), or None if unreadable."""
    try:
        from mutagen.mp3 import MP3
        audio = MP3(str(path))
        size = path.stat().st_size
    except Exception:
        return None
    info = audio.info
    if not info.bitrate or info.length <= 0:
        return None
    tag_bytes = getattr(audio.tags, 'size', 0) or 0
    return info.length, max(0, size - tag_bytes) * 8 / info.bitrate


def broken_header(path: Path) -> str:
    """Why this file's length header is wrong, or '' when it is fine (or not an MP3)."""
    path = Path(path)
    if path.suffix.lower() != '.mp3':
        return ''
    lengths = mp3_lengths(path)
    if lengths is None:
        return ''
    claimed, actual = lengths
    if actual - claimed < _MIN_MISSING_SECONDS or actual < claimed * _MIN_RATIO:
        return ''
    return (f'BROKEN AUDIO: {path.name} reports {_clock(claimed)} but holds about '
            f'{_clock(actual)} of audio. Players will stop after {_clock(claimed)}.')


def check_files(paths) -> List[str]:
    """One message per broken file among `paths`."""
    return [message for message in (broken_header(Path(p)) for p in paths) if message]


def repair_header(path: Path, ffmpeg: str = 'ffmpeg',
                  discard: Optional[Callable[[Path], bool]] = None,
                  should_cancel: Optional[Callable[[], bool]] = None,
                  on_progress: Optional[Callable[[int, int, str], None]] = None
                  ) -> Tuple[bool, str]:
    """Rewrite one MP3 with a header that covers all of its audio.

    The copy is written beside the original and checked before it takes the
    original's place. `discard` disposes of the original (the Recycle Bin, from the
    window); when it is missing or fails, the checked copy simply replaces it.
    """
    path = Path(path)
    if not ffmpeg_available(ffmpeg):
        return False, 'ffmpeg was not found - set its path in Settings'
    lengths = mp3_lengths(path)
    if lengths is None:
        return False, f'Could not read {path.name}'
    expected = lengths[1]
    temp = path.with_name(path.stem + '.repairing.mp3')
    command = [ffmpeg, '-hide_banner', '-nostats', '-loglevel', 'error', '-y',
               '-progress', 'pipe:1', '-i', str(path), '-map', '0', '-c', 'copy',
               '-map_metadata', '0', '-id3v2_version', '3', '-write_xing', '1',
               str(temp)]

    def progress(ms: float) -> None:
        if on_progress is not None and expected > 0:
            on_progress(int(ms), int(expected * 1000), f'Repairing {path.name}')

    ok, message = _run_streaming(command, should_cancel or (lambda: False), progress)
    if not ok:
        temp.unlink(missing_ok=True)
        return False, message
    fixed = mp3_lengths(temp)
    if fixed is None or broken_header(temp) or fixed[0] < expected * 0.97:
        temp.unlink(missing_ok=True)
        return False, f'The repaired copy of {path.name} was still wrong; original kept'
    try:
        if discard is None or not discard(path):
            os.replace(temp, path)
        else:
            temp.rename(path)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        return False, f'Could not replace {path.name}: {exc}'
    logger.info('Repaired the MP3 header of %s: %s -> %s', path,
                _clock(lengths[0]), _clock(fixed[0]))
    return True, f'Repaired {path.name}: now {_clock(fixed[0])}'
