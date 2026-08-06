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


def _touch(folder: Path, names) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b'audio')


def test_flat_folder_of_several_books_chapters(tmp_path):
    """The mess from the screenshot: many books' chapters loose in one folder.

    Each title becomes one entry holding its own chapters - not one entry per chapter
    (which made every chapter a book, and every chapter after the first a "duplicate")
    and not one entry for the folder (which would be twenty books in a trench coat).
    """
    folder = tmp_path / 'J.D. Robb - 1-20 and SS'
    _touch(folder, [f'06 - Vengeance in Death-{i}.mp3' for i in range(1, 9)]
                   + [f'04 - Rapture in Death-{i}.mp3' for i in range(1, 10)])

    entries = FileScanner(str(tmp_path)).scan_directory()
    assert len(entries) == 2
    by_size = sorted(entries, key=lambda e: len(e.audio_files))
    assert [len(e.audio_files) for e in by_size] == [8, 9]
    assert all(e.is_multi_book_folder for e in entries)
    assert len({e.entry_id for e in entries}) == 2


def test_flat_folder_of_whole_books_still_splits_per_file(tmp_path):
    """One file per book, distinct titles: still one entry each, as before."""
    folder = tmp_path / 'assorted'
    _touch(folder, ['Book 1 - The Song of the First Blade.m4b',
                    'Book 2 - Ghost of the Shadowfort.m4b',
                    'Book 3 - An Echo of Titans.m4b'])

    entries = FileScanner(str(tmp_path)).scan_directory()
    assert len(entries) == 3
    assert all(len(e.audio_files) == 1 for e in entries)


def test_one_books_chapters_are_not_split_by_title(tmp_path):
    """A single book's chapter run stays one entry - one group is not "several books"."""
    folder = tmp_path / 'The Name of the Wind'
    _touch(folder, [f'Part {i} - The Name of the Wind.mp3' for i in range(1, 6)])

    entries = FileScanner(str(tmp_path)).scan_directory()
    assert len(entries) == 1
    assert len(entries[0].audio_files) == 5


def test_numeric_only_names_are_left_alone(tmp_path):
    """"13-17.mp3" names no book, so the folder is judged the old way, not guessed at."""
    folder = tmp_path / 'loose'
    _touch(folder, ['13-17.mp3', '15-21.mp3', '18-22.mp3', '19-22.mp3'])

    entries = FileScanner(str(tmp_path)).scan_directory()
    assert len(entries) == 1
    assert len(entries[0].audio_files) == 4


def test_book_number_in_the_name_does_not_split_the_chapters(tmp_path):
    """The bug behind the screenshot: "06 - Vengeance in Death-1..8" is ONE book.

    Reading only the first number in each name found the book number - 6, eight times
    over - called that "too many duplicate indices to be a chapter run" and produced
    eight one-file books. Every part after the first was then flagged a duplicate of
    part one, because all eight resolved to the same author and title.
    """
    folder = tmp_path / 'J.D. Robb - 06 Vengeance in Death'
    _touch(folder, [f'06 - Vengeance in Death-{i}.mp3' for i in range(1, 9)])

    entries = FileScanner(str(tmp_path)).scan_directory()
    assert len(entries) == 1
    assert len(entries[0].audio_files) == 8


def test_whole_book_beside_its_own_chapters_is_its_own_entry(tmp_path):
    """A folder holding nine parts *and* the complete file is two entries, not one.

    As one entry its chapter list plays the book, then plays it again - which is what
    a merge of that entry would have produced.
    """
    folder = tmp_path / 'J.D. Robb - 05 Ceremony in Death'
    folder.mkdir(parents=True)
    for index in range(1, 10):
        (folder / f'05 - Ceremony in Death-{index}.mp3').write_bytes(b'x' * 32_000)
    (folder / '05 - Ceremony in Death.mp3').write_bytes(b'x' * 292_000)

    entries = FileScanner(str(tmp_path)).scan_directory()
    assert len(entries) == 2
    whole = next(e for e in entries if len(e.audio_files) == 1)
    parts = next(e for e in entries if len(e.audio_files) == 9)
    assert whole.audio_files == ['05 - Ceremony in Death.mp3']
    assert '05 - Ceremony in Death.mp3' not in parts.audio_files


def test_one_long_chapter_does_not_count_as_a_whole_book(tmp_path):
    """A chapter set with one longer chapter in it stays a single entry."""
    folder = tmp_path / 'A Book'
    folder.mkdir(parents=True)
    for index in range(1, 21):
        (folder / f'{index:02d}.mp3').write_bytes(b'x' * 10_000)
    (folder / '21.mp3').write_bytes(b'x' * 45_000)

    entries = FileScanner(str(tmp_path)).scan_directory()
    assert len(entries) == 1
    assert len(entries[0].audio_files) == 21


def test_freshness_check_sees_what_changed(tmp_path):
    """The check behind the "!" on Scan: added, missing and resized files."""
    folder = tmp_path / 'book'
    _touch(folder, ['01.mp3', '02.mp3'])
    scanner = FileScanner(str(tmp_path))
    entries = scanner.scan_directory()
    assert scanner.compare_to_entries(entries) == {
        'added': 0, 'missing': 0, 'changed': 0, 'unreadable': 0}

    (folder / '03.mp3').write_bytes(b'audio')
    assert scanner.compare_to_entries(entries)['added'] == 1

    (folder / '01.mp3').write_bytes(b'a different length entirely')
    assert scanner.compare_to_entries(entries)['changed'] == 1

    gone = FileScanner(str(tmp_path / 'nowhere'))
    assert gone.compare_to_entries(entries)['unreadable'] == 1


# ------------------------------------------------------------ what decides a book

def _tagged(path: Path, album: str, cover: bytes = b'') -> Path:
    """A parseable mp3 carrying an album tag, and optionally embedded artwork."""
    from mutagen.id3 import APIC, TALB, ID3NoHeaderError
    from mutagen.mp3 import MP3

    from tests.conftest import make_audio

    make_audio(path)
    audio = MP3(str(path))
    try:
        audio.add_tags()
    except (ID3NoHeaderError, Exception):
        pass
    audio.tags.add(TALB(encoding=3, text=[album]))
    if cover:
        audio.tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='', data=cover))
    audio.save()
    return path


def test_two_long_parts_of_one_book_are_one_entry(tmp_path):
    """A 14-hour book in two 7-hour files is one book.

    This is the case an arbitrary "over N hours means it is a book" rule got wrong, and
    it is why that rule is gone: length is not evidence of anything. Both files say
    ``album=Primal Fury``, so both belong to Primal Fury.
    """
    folder = tmp_path / '04 Primal Fury'
    _tagged(folder / '01 Primal Fury.mp3', 'Primal Fury')
    _tagged(folder / '02 Primal Fury.mp3', 'Primal Fury')

    entries = FileScanner(str(tmp_path)).scan_directory()
    assert len(entries) == 1
    assert len(entries[0].audio_files) == 2


def test_different_albums_in_one_folder_are_different_books(tmp_path):
    """And the same tag that joins those two separates these, at any length."""
    folder = tmp_path / 'loose'
    _tagged(folder / 'a.mp3', 'Vengeance in Death')
    _tagged(folder / 'b.mp3', 'Vengeance in Death')
    _tagged(folder / 'c.mp3', 'Holiday in Death')

    entries = FileScanner(str(tmp_path)).scan_directory()
    assert sorted(len(e.audio_files) for e in entries) == [1, 2]


def test_disc_markers_do_not_split_a_release(tmp_path):
    """"Ceremony in Death CD 01" and "CD 02" are one book in two volumes."""
    folder = tmp_path / 'Ceremony'
    _tagged(folder / '1.mp3', 'Ceremony in Death CD 01')
    _tagged(folder / '2.mp3', 'Ceremony in Death CD 02')

    entries = FileScanner(str(tmp_path)).scan_directory()
    assert len(entries) == 1


def test_the_cover_decides_when_the_album_tag_cannot(tmp_path):
    """Same artwork, same release - the user's suggestion, and the second-best signal."""
    folder = tmp_path / 'untagged'
    red, blue = b'\xff\xd8\xff' + b'R' * 300, b'\xff\xd8\xff' + b'B' * 300
    _tagged(folder / 'x1.mp3', '', cover=red)
    _tagged(folder / 'x2.mp3', '', cover=red)
    _tagged(folder / 'y1.mp3', '', cover=blue)

    entries = FileScanner(str(tmp_path)).scan_directory()
    assert sorted(len(e.audio_files) for e in entries) == [1, 2]


def test_no_duration_rule_anywhere(tmp_path):
    """There must be no "a file this long is a book" test left in the scanner."""
    import inspect

    from scripts import file_scanner

    # Playtime is never read: `.length` is the only way mutagen reports it, so its
    # absence from the source is the whole guarantee. Comments may still explain why.
    code = ''.join(line.split('#')[0] for line in
                   inspect.getsource(file_scanner).splitlines(keepends=True))
    assert '.length' not in code
    assert 'audio.info' not in code and 'getattr(audio' not in code
    assert not hasattr(FileScanner, 'looks_like_chapters')
    assert not hasattr(FileScanner, '_median_duration_minutes')
