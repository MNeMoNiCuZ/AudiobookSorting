"""Scanner: the layouts from spec.md must each produce the right entry count."""

from __future__ import annotations

from pathlib import Path

from scripts.file_scanner import FileScanner


def test_multi_book_folder_splits_per_file(tmp_library):
    """Four books in one folder are four entries, not one entry plus extras (#3, #12)."""
    entries = FileScanner(str(tmp_library)).scan_directory()
    saga = [e for e in entries if 'Bladeborn' in e.folder]

    assert len(saga) == 4
    assert all(e.is_multi_book_folder for e in saga)
    assert all(len(e.audio_files) == 1 for e in saga)
    # Every book present exactly once.
    assert len({e.primary_audio for e in saga}) == 4


def test_chapter_folder_is_one_entry(tmp_library):
    """Five numbered chapters are one book (#12)."""
    entries = FileScanner(str(tmp_library)).scan_directory()
    wind = [e for e in entries if 'Name of the Wind' in e.folder]

    assert len(wind) == 1
    assert len(wind[0].audio_files) == 5
    assert not wind[0].is_multi_book_folder


def test_entries_are_fully_populated(tmp_library):
    """The old scanner left audio_files empty, starving the LLM of context (#2)."""
    entries = FileScanner(str(tmp_library)).scan_directory()

    for entry in entries:
        assert entry.entry_id
        assert entry.folder
        assert entry.audio_files, f'{entry.entry_id} has no audio_files'
        assert entry.primary_audio
        assert Path(entry.primary_audio).exists()
        assert entry.status == 'pending'


def test_cover_image_is_recorded(tmp_library):
    entries = FileScanner(str(tmp_library)).scan_directory()
    saga = next(e for e in entries if 'Bladeborn' in e.folder)
    assert 'cover.jpg' in saga.image_files


def test_entry_ids_are_stable_across_scans(tmp_library):
    """Resume depends on ids not changing between runs (#27)."""
    first = {e.entry_id for e in FileScanner(str(tmp_library)).scan_directory()}
    second = {e.entry_id for e in FileScanner(str(tmp_library)).scan_directory()}
    assert first == second


def test_empty_input_directory(tmp_path):
    assert FileScanner(str(tmp_path / 'nope')).scan_directory() == []


def test_names_chapter_heuristic():
    """Numbered runs are chapters; distinct titles per file are separate books."""
    check = FileScanner._names_look_like_chapters

    assert check(['01.mp3', '02.mp3', '03.mp3'])
    assert check(['Chapter 01.mp3', 'Chapter 02.mp3'])
    assert check(['Track 1 of 12.mp3', 'Track 2 of 12.mp3'])
    assert not check(['Book 1 - Alpha.m4b', 'Book 2 - Beta.m4b', 'Book 3 - Gamma.m4b'])
    assert not check(['The Twisted Ones.m4b', 'What Moves the Dead.m4b'])


def test_extra_formats_are_found(tmp_path):
    from tests.conftest import make_stub

    root = tmp_path / 'formats'
    for name in ('a.flac', 'b.opus', 'c.m4a', 'd.aax', 'e.wma'):
        make_stub(root / f'Book {name}' / name)

    entries = FileScanner(str(root)).scan_directory()
    found = {Path(e.primary_audio).suffix for e in entries}
    assert found == {'.flac', '.opus', '.m4a', '.aax', '.wma'}
