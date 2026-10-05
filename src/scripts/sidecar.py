"""Sidecar metadata files for library scanners (#32).

Audiobookshelf reads ``metadata.json``; Calibre and several other tools read ``.opf``.
Both are cheap to emit and make the organised output self-describing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List
from xml.sax.saxutils import escape

from .models import BookEntry

logger = logging.getLogger(__name__)


def write_sidecars(entry: BookEntry, folder: Path) -> List[Path]:
    """Write metadata.json and metadata.opf. Returns the paths created."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    written = []

    try:
        path = folder / 'metadata.json'
        path.write_text(json.dumps(_as_abs_metadata(entry), indent=2, ensure_ascii=False),
                        encoding='utf-8')
        written.append(path)
    except OSError as exc:
        logger.warning('Could not write metadata.json: %s', exc)

    try:
        path = folder / 'metadata.opf'
        path.write_text(_as_opf(entry), encoding='utf-8')
        written.append(path)
    except OSError as exc:
        logger.warning('Could not write metadata.opf: %s', exc)

    return written


def _as_abs_metadata(entry: BookEntry) -> dict:
    """Audiobookshelf's metadata.json shape."""
    data = {
        'title': entry.value('title'),
        'authors': [entry.value('author')] if entry.value('author') else [],
        'series': [],
        'tags': [],
        'description': '',
    }
    series = entry.value('series')
    if series:
        index = entry.value('series_index')
        data['series'] = [f'{series} #{index}' if index else series]
    return data


def _as_opf(entry: BookEntry) -> str:
    title = escape(entry.value('title') or 'Unknown')
    author = escape(entry.value('author') or 'Unknown')
    series = escape(entry.value('series'))
    index = escape(entry.value('series_index'))

    meta = []
    if series:
        meta.append(f'    <meta name="calibre:series" content="{series}"/>')
    if index:
        meta.append(f'    <meta name="calibre:series_index" content="{index}"/>')

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uuid_id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>{title}</dc:title>
    <dc:creator opf:role="aut" opf:file-as="{author}">{author}</dc:creator>
    <dc:language>eng</dc:language>
{chr(10).join(meta)}
  </metadata>
</package>
'''
