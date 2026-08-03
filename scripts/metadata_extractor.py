"""Tier 1: read tags embedded in the audio files themselves.

Handles every container mutagen supports plus DRM'd Audible files - for those the tag
atoms are usually still readable even though the audio isn't decodable, which is all we
need. Anything unreadable degrades to "no tags", and the resolver moves on to the next
tier rather than failing.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

# Tag names vary by container; these are the ones that mean the same thing.
_AUTHOR_KEYS = ('\xa9ART', 'aART', 'artist', 'albumartist', 'author', 'composer',
                'TPE1', 'TPE2', '----:com.apple.iTunes:AUTHOR')
_TITLE_KEYS = ('\xa9nam', 'title', 'TIT2')
_ALBUM_KEYS = ('\xa9alb', 'album', 'TALB')
_SERIES_KEYS = ('----:com.apple.iTunes:SERIES', 'series', 'mvnm', '\xa9mvn',
                'TXXX:SERIES', 'show', 'grouping', '\xa9grp', 'TIT1')
_INDEX_KEYS = ('----:com.apple.iTunes:SERIES-PART', 'series-part', 'mvi', '\xa9mvi',
               'TXXX:SERIES-PART', 'movementnumber', 'TXXX:SERIES_INDEX')
_SORT_KEYS = ('soal', 'sonm', 'TSOA', 'albumsort')

# Patterns for pulling "Series, Book 3" out of an album/title string.
_SERIES_PATTERNS = (
    re.compile(r'^(?P<series>.+?),?\s*(?:book|bk|volume|vol|part|pt|#)\s*(?P<index>\d{1,3})\b', re.I),
    re.compile(r'^(?P<series>.+?)\s*[,:]\s*(?:book|bk|volume|vol)\s*(?P<index>\d{1,3})\b', re.I),
    re.compile(r'^(?P<series>.+?)\s+(?P<index>\d{1,3})\s*$'),
    re.compile(r'^(?P<series>.+?)\s*\(\s*(?:book|vol(?:ume)?|#)?\s*(?P<index>\d{1,3})\s*\)\s*$', re.I),
)


class MetadataExtractor:
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def extract(self, file_path: str) -> Dict[str, str]:
        """Return normalised metadata plus the raw tags, for the "why" panel."""
        result: Dict[str, str] = {}
        raw = self.read_raw_tags(file_path)
        if not raw:
            return result

        author = _first(raw, _AUTHOR_KEYS)
        title = _first(raw, _TITLE_KEYS)
        album = _first(raw, _ALBUM_KEYS)
        series = _first(raw, _SERIES_KEYS)
        index = _first(raw, _INDEX_KEYS)

        if author:
            result['author'] = author
        if series:
            result['series'] = series
        if index:
            result['series_index'] = index

        # The album field is where series info usually hides when there's no real
        # series tag: "Mistborn, Book 1".
        if not series and album:
            parsed_series, parsed_index = self.split_series(album)
            if parsed_series:
                result['series'] = parsed_series
                if parsed_index and not index:
                    result['series_index'] = parsed_index

        # Title: prefer the track title, but if it's a chapter name ("Chapter 3") the
        # album is the real book title.
        if title and not _looks_like_chapter_name(title):
            result['title'] = title
        elif album:
            book_title, _ = self.split_series(album)
            result['title'] = album if not book_title else album
        elif title:
            result['title'] = title

        # If the title itself carries series info, split it out.
        if 'series' not in result and result.get('title'):
            parsed_series, parsed_index = self.split_series(result['title'])
            if parsed_series and parsed_index:
                result['series'] = parsed_series
                result.setdefault('series_index', parsed_index)

        return {k: v for k, v in result.items() if v}

    def read_raw_tags(self, file_path: str) -> Dict[str, str]:
        """Every readable tag, flattened to strings. Empty dict if unreadable."""
        try:
            from mutagen import File as MutagenFile
        except ImportError:
            self.logger.error('mutagen is not installed - cannot read tags')
            return {}

        try:
            audio = MutagenFile(str(file_path))
        except Exception as exc:
            # DRM'd or corrupt: expected for .aax/.aa, not worth a stack trace.
            self.logger.debug('Could not open %s: %s', file_path, exc)
            return {}

        if audio is None or not getattr(audio, 'tags', None):
            return {}

        raw: Dict[str, str] = {}
        try:
            for key, value in dict(audio.tags).items():
                text = _stringify(value)
                if text:
                    raw[str(key)] = text
        except Exception as exc:
            self.logger.debug('Could not enumerate tags of %s: %s', file_path, exc)

        try:
            if getattr(audio, 'info', None) and getattr(audio.info, 'length', 0):
                raw['_duration_seconds'] = str(int(audio.info.length))
        except Exception:
            pass
        return raw

    @staticmethod
    def split_series(text: str) -> Tuple[str, str]:
        """Split "Mistborn, Book 1" into ("Mistborn", "1"). ("", "") if no match."""
        if not text:
            return '', ''
        for pattern in _SERIES_PATTERNS:
            match = pattern.match(text.strip())
            if match:
                series = match.group('series').strip(' ,:-')
                index = match.group('index')
                # A 4-digit trailing number is a year, not a book number.
                if series and index and not (len(index) == 4 and index.startswith(('1', '2'))):
                    return series, index
        return '', ''

    def extract_cover(self, file_path: str) -> Optional[bytes]:
        """Raw bytes of the embedded cover image, if there is one."""
        try:
            from mutagen import File as MutagenFile
            from mutagen.flac import FLAC
            from mutagen.mp4 import MP4
        except ImportError:
            return None

        try:
            audio = MutagenFile(str(file_path))
            if audio is None:
                return None

            if isinstance(audio, MP4):
                covers = audio.tags.get('covr') if audio.tags else None
                if covers:
                    return bytes(covers[0])
            elif isinstance(audio, FLAC):
                if audio.pictures:
                    return audio.pictures[0].data
            elif getattr(audio, 'tags', None):
                for key in audio.tags.keys():
                    if str(key).startswith('APIC'):
                        return audio.tags[key].data
        except Exception as exc:
            self.logger.debug('No cover in %s: %s', file_path, exc)
        return None

    def duration_seconds(self, file_path: str) -> int:
        try:
            from mutagen import File as MutagenFile
            audio = MutagenFile(str(file_path))
            if audio is not None and getattr(audio, 'info', None):
                return int(getattr(audio.info, 'length', 0) or 0)
        except Exception:
            pass
        return 0


def _stringify(value) -> str:
    """Flatten mutagen's many value shapes into one readable string."""
    if isinstance(value, (list, tuple)):
        if not value:
            return ''
        return _stringify(value[0])
    if isinstance(value, bytes):
        for encoding in ('utf-8', 'utf-16', 'latin-1'):
            try:
                return value.decode(encoding).strip('\x00').strip()
            except (UnicodeDecodeError, AttributeError):
                continue
        return ''
    if hasattr(value, 'text'):
        return _stringify(value.text)
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    # Skip binary blobs (cover art) that stringify into noise.
    return '' if len(text) > 400 or '\x00' in text else text


def _first(raw: Dict[str, str], keys) -> str:
    """First non-empty value among `keys`, matched case-insensitively."""
    lowered = {k.lower(): v for k, v in raw.items()}
    for key in keys:
        value = raw.get(key) or lowered.get(key.lower())
        if value and str(value).strip():
            return str(value).strip()
    return ''


def _looks_like_chapter_name(title: str) -> bool:
    return bool(re.match(
        r'^\s*(?:chapter|chap|ch|track|part|pt|section|disc|cd)\s*\d+\s*$|^\s*\d{1,3}\s*$',
        title, re.I))
