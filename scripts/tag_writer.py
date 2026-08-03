"""Write corrected metadata back into the audio files (#31).

Folder names are lost the moment someone imports the library into Audiobookshelf,
Plex or Prologue - those read tags. Writing the tags back makes the organisation
portable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from .models import BookEntry

logger = logging.getLogger(__name__)

AUDIO_SUFFIXES = ('.m4b', '.m4a', '.mp4', '.mp3', '.flac', '.ogg', '.opus')


def write_tags(entry: BookEntry, folder: Optional[Path] = None) -> int:
    """Write the entry's identity into its audio files. Returns the count written."""
    folder = Path(folder) if folder else Path(entry.applied_path or entry.folder)
    if not folder.is_dir():
        logger.warning('Cannot write tags, no such folder: %s', folder)
        return 0

    targets = [p for p in sorted(folder.iterdir())
               if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES]
    written = 0
    for path in targets:
        if _write_one(path, entry):
            written += 1
    return written


def _write_one(path: Path, entry: BookEntry) -> bool:
    try:
        from mutagen import File as MutagenFile
        from mutagen.easyid3 import EasyID3
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3NoHeaderError
        from mutagen.mp4 import MP4
        from mutagen.oggopus import OggOpus
        from mutagen.oggvorbis import OggVorbis
    except ImportError:
        logger.error('mutagen is required to write tags')
        return False

    author = entry.value('author')
    title = entry.value('title')
    series = entry.value('series')
    index = entry.value('series_index')
    # The album is where every audiobook player looks for the series.
    album = f'{series}, Book {index}' if series and index else (series or title)

    suffix = path.suffix.lower()
    try:
        if suffix in ('.m4b', '.m4a', '.mp4'):
            audio = MP4(str(path))
            if audio.tags is None:
                audio.add_tags()
            if author:
                audio.tags['\xa9ART'] = [author]
                audio.tags['aART'] = [author]
            if title:
                audio.tags['\xa9nam'] = [title]
            if album:
                audio.tags['\xa9alb'] = [album]
            if series:
                # Freeform atoms are what Audiobookshelf and Plex actually read.
                audio.tags['----:com.apple.iTunes:SERIES'] = [series.encode('utf-8')]
                audio.tags['\xa9mvn'] = [series]
            if index:
                audio.tags['----:com.apple.iTunes:SERIES-PART'] = [index.encode('utf-8')]
                try:
                    audio.tags['\xa9mvi'] = [int(float(index))]
                except (ValueError, TypeError):
                    pass
            audio.save()

        elif suffix == '.mp3':
            try:
                tags = EasyID3(str(path))
            except ID3NoHeaderError:
                audio = MutagenFile(str(path), easy=True)
                if audio is None:
                    return False
                audio.add_tags()
                audio.save()
                tags = EasyID3(str(path))
            if author:
                tags['artist'] = author
                tags['albumartist'] = author
            if title:
                tags['title'] = title
            if album:
                tags['album'] = album
            if series:
                tags['grouping'] = series  # TIT1, read as series by most players
            tags.save()

        elif suffix in ('.flac', '.ogg', '.opus'):
            audio = {'flac': FLAC, 'ogg': OggVorbis, 'opus': OggOpus}[suffix.lstrip('.')](str(path))
            if author:
                audio['artist'] = author
                audio['albumartist'] = author
            if title:
                audio['title'] = title
            if album:
                audio['album'] = album
            if series:
                audio['series'] = series
            if index:
                audio['series-part'] = index
            audio.save()
        else:
            return False

    except Exception as exc:
        # A DRM'd or read-only file is not a reason to fail the whole apply.
        logger.warning('Could not write tags to %s: %s', path.name, exc)
        return False

    logger.debug('Wrote tags to %s', path.name)
    return True
