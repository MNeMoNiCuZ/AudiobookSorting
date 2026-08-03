"""Shared fixtures - synthetic libraries covering the tricky layouts from spec.md."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.settings import Settings  # noqa: E402


# Real MP3 frame headers, so mutagen can open these without decoding anything.
# 32 frames of silence at 32kbps/44.1kHz - enough for a readable duration.
_MP3_FRAME = bytes([0xFF, 0xFB, 0x10, 0xC4]) + b'\x00' * 100


def make_audio(path: Path, frames: int = 40) -> Path:
    """Write a minimal but genuinely parseable MP3."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_MP3_FRAME * frames)
    return path


def make_stub(path: Path, size: int = 64) -> Path:
    """A file with the right extension but no valid audio - the unreadable case."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\x00' * size)
    return path


@pytest.fixture
def tmp_library(tmp_path: Path) -> Path:
    """A library exercising every layout the scanner has to tell apart."""
    root = tmp_path / 'input'

    # 1. Several distinct books sharing one folder (spec.md's Bladeborn case).
    saga = root / 'The Bladeborn Saga'
    for name in ('Book 1 - The Song of the First Blade.m4b',
                 'Book 2 - Ghost of the Shadowfort.m4b',
                 'Book 3-An Echo of Titans.m4b',
                 'Book 4 -The Winds of War The Bladeborn Saga.m4b'):
        make_stub(saga / name)
    (saga / 'cover.jpg').write_bytes(b'\xff\xd8\xff')

    # 2. One book split into numbered chapters.
    chapters = root / 'Patrick Rothfuss' / 'The Name of the Wind'
    for index in range(1, 6):
        make_audio(chapters / f'{index:02d}.mp3')

    # 3. Author folder / series folder / single file.
    single = root / 'Brandon Sanderson' / 'Mistborn 01 - The Final Empire'
    make_stub(single / 'book.m4b')

    # 4. A standalone book at the top of an author folder.
    make_stub(root / 'T. Kingfisher' / 'The Twisted Ones.m4b')

    # 5. Noisy scene-style naming.
    make_stub(root / 'Misc' / 'The Hobbit [Narrated by Rob Inglis] {64kbps} (1937).m4b')

    return root


@pytest.fixture
def settings(tmp_path: Path, tmp_library: Path) -> Settings:
    """Settings pointed at the synthetic library, with all network tiers disabled."""
    config = Settings(tmp_path / '.env')
    config.update({
        'AO_INPUT_DIR': str(tmp_library),
        'AO_OUTPUT_DIR': str(tmp_path / 'output'),
        'AO_CACHE_DB': str(tmp_path / 'cache.sqlite3'),
        'AO_ENABLE_API': 'false',
        'AO_ENABLE_SEARCH': 'false',
        'AO_ENABLE_LLM': 'false',
        'AO_COPY_MODE': 'true',
        'AO_DETECT_DUPLICATES': 'true',
    })
    config.save()
    return config


@pytest.fixture
def entries(settings, tmp_library):
    """A scanned and locally-resolved library."""
    from scripts.file_scanner import FileScanner
    from scripts.resolver import Resolver

    scanned = FileScanner(str(tmp_library)).scan_directory()
    resolver = Resolver(settings)
    groups = {}
    for entry in scanned:
        groups.setdefault(entry.folder, []).append(entry)
    for group in groups.values():
        resolver.resolve_folder(group)
    return scanned


class FakeResponse:
    """Stand-in for requests.Response."""

    def __init__(self, payload=None, status_code=200, text=''):
        self._payload = payload
        self.status_code = status_code
        self.text = text or ''

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly if any test reaches for the network."""
    def blocked(*args, **kwargs):
        raise AssertionError(f'Unexpected network call: {args[:1]}')

    import requests
    monkeypatch.setattr(requests, 'get', blocked)
    monkeypatch.setattr(requests, 'post', blocked)
    return blocked
