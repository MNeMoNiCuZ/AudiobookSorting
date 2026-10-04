"""Cover thumbnails read off the GUI thread.

Every row of the table shows its cover, and finding one means opening the audio file
and parsing its tags. Done inline while the table is built, a library of three
thousand books froze the window for as long as it took to open three thousand files.
The table is now built without them and each thumbnail is filled in as it arrives.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QImage

logger = logging.getLogger(__name__)

THUMB_SIZE = 48


def read_cover(primary_audio: str, folder: str, image_files) -> QImage:
    """Embedded cover art, else a folder image, else a null image (#26)."""
    image = QImage()
    try:
        from ..metadata_extractor import MetadataExtractor
        data = MetadataExtractor().extract_cover(primary_audio)
        if data:
            image.loadFromData(data)
        if image.isNull() and image_files:
            image.load(str(Path(folder) / image_files[0]))
    except Exception as exc:
        logger.debug('No cover for %s: %s', primary_audio, exc)
    if not image.isNull():
        image = image.scaled(THUMB_SIZE, THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    return image


class CoverLoader(QObject):
    """One background thread working through a queue of books that need a cover."""

    # entry_id, thumbnail (null when the book has none)
    loaded = pyqtSignal(str, QImage)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: deque = deque()
        self._queued: set = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name='cover-loader',
                                        daemon=True)
        self._thread.start()

    def request(self, entry) -> None:
        with self._lock:
            if entry.entry_id in self._queued:
                return
            self._queued.add(entry.entry_id)
            self._queue.append((entry.entry_id, entry.primary_audio, entry.folder,
                                list(entry.image_files or [])))
        self._wake.set()

    def clear(self) -> None:
        """Forget everything still waiting - the table it was for has been replaced."""
        with self._lock:
            self._queue.clear()
            self._queued.clear()

    def _run(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                if not self._queue:
                    self._wake.clear()
                    continue
                entry_id, primary, folder, images = self._queue.popleft()
            image = read_cover(primary, folder, images)
            with self._lock:
                self._queued.discard(entry_id)
            try:
                self.loaded.emit(entry_id, image)
            except RuntimeError:
                return      # the window is gone
