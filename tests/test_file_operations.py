"""File operations - above all, that an entry never touches a sibling's files."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.file_operations import FileOperations
from scripts.file_scanner import FileScanner
from scripts.models import Field


def _bladeborn(entries):
    return sorted([e for e in entries if 'Bladeborn' in e.folder],
                  key=lambda e: e.entry_id)


def test_apply_moves_only_its_own_files(settings, entries, tmp_library):
    """The headline bug (#1): applying one book must not drag its siblings along."""
    settings.set('AO_COPY_MODE', 'false')      # move mode - the destructive case

    saga = _bladeborn(entries)
    first = saga[0]
    for entry in saga:
        entry.author = Field('Test Author', 'user', 1.0)

    folder = Path(first.folder)
    before = {p.name for p in folder.iterdir()}

    result = FileOperations(settings).apply_entry(first)
    assert result.ok, result.error

    after = {p.name for p in folder.iterdir()}
    moved = before - after

    # Exactly one file left the folder: the one we applied.
    assert moved == {Path(first.primary_audio).name}
    # The other three books and the shared cover are untouched.
    assert len(after) == 4
    assert 'cover.jpg' in after


def test_single_book_folder_takes_its_sidecars(settings, entries):
    """A book that owns its folder should bring the cover art with it."""
    wind = next(e for e in entries if 'Name of the Wind' in e.folder)
    wind.author = Field('Patrick Rothfuss', 'user', 1.0)

    result = FileOperations(settings).apply_entry(wind)
    assert result.ok
    assert len(list(result.destination.iterdir())) == 5


def test_collision_policy_suffix(settings, entries):
    """Two books resolving to the same folder must not silently merge (#6)."""
    settings.set('AO_COLLISION_POLICY', 'suffix')

    saga = _bladeborn(entries)
    ops = FileOperations(settings)
    # Force both entries to the same destination.
    for entry in saga[:2]:
        entry.author = Field('Same Author', 'user', 1.0)
        entry.title = Field('Same Title', 'user', 1.0)
        entry.series = Field('', 'user', 1.0)
        entry.series_index = Field('', 'user', 1.0)

    first = ops.apply_entry(saga[0])
    second = ops.apply_entry(saga[1])

    assert first.ok and second.ok
    assert first.destination != second.destination
    assert second.destination.name.endswith('(2)')


def test_collision_policy_skip(settings, entries):
    settings.set('AO_COLLISION_POLICY', 'skip')

    saga = _bladeborn(entries)
    ops = FileOperations(settings)
    # Both must resolve to the *same* destination, so clear the index too.
    for entry in saga[:2]:
        entry.author = Field('A', 'user', 1.0)
        entry.title = Field('T', 'user', 1.0)
        entry.series = Field('', 'user', 1.0)
        entry.series_index = Field('', 'user', 1.0)

    first = ops.apply_entry(saga[0])
    second = ops.apply_entry(saga[1])
    assert first.ok
    assert second.skipped
    assert second.destination == first.destination


def test_preview_writes_nothing(settings, entries):
    """Preview plans the whole move and touches nothing.

    This replaced the AO_DRY_RUN setting: previewing is an explicit call, so Apply
    can no longer silently do nothing because of a checkbox left on weeks ago.
    """
    output = settings.get_path('AO_OUTPUT_DIR')

    ops = FileOperations(settings)
    for entry in entries:
        entry.author = Field('Author', 'user', 1.0)
        result = ops.preview(entry)
        assert result.dry_run
        assert result.operations

    assert not output.exists() or not any(output.rglob('*.m4b'))


def test_undo_restores_moved_files(settings, entries):
    """Undo is the safety net for move mode (#20)."""
    settings.set('AO_COPY_MODE', 'false')

    wind = next(e for e in entries if 'Name of the Wind' in e.folder)
    wind.author = Field('Patrick Rothfuss', 'user', 1.0)
    source = Path(wind.folder)
    originals = sorted(p.name for p in source.iterdir())

    ops = FileOperations(settings)
    result = ops.apply_entry(wind)
    assert result.ok
    assert not any(source.iterdir()) if source.exists() else True

    transaction, problems = ops.journal.undo_last()
    assert transaction is not None
    assert problems == []
    assert sorted(p.name for p in source.iterdir()) == originals


def test_copy_mode_leaves_source_intact(settings, entries):
    settings.set('AO_COPY_MODE', 'true')

    wind = next(e for e in entries if 'Name of the Wind' in e.folder)
    wind.author = Field('Patrick Rothfuss', 'user', 1.0)
    before = sorted(p.name for p in Path(wind.folder).iterdir())

    assert FileOperations(settings).apply_entry(wind).ok
    assert sorted(p.name for p in Path(wind.folder).iterdir()) == before


def test_rename_files_uses_template(settings, entries):
    settings.set('AO_RENAME_FILES', 'true')
    settings.set('AO_FILE_TEMPLATE', '{title}')

    wind = next(e for e in entries if 'Name of the Wind' in e.folder)
    wind.author = Field('Patrick Rothfuss', 'user', 1.0)
    wind.title = Field('The Name of the Wind', 'user', 1.0)

    result = FileOperations(settings).apply_entry(wind)
    names = sorted(p.name for p in result.destination.iterdir())
    assert names[0].startswith('The Name of the Wind - Part 01')


def test_files_for_never_includes_siblings(settings, entries):
    saga = _bladeborn(entries)
    ops = FileOperations(settings)
    for entry in saga:
        owned = ops.files_for(entry)
        assert len(owned) == 1
        assert owned[0].name == Path(entry.primary_audio).name


def test_a_rejected_book_is_never_written(settings, entries):
    """Whoever asks, and whatever list it arrives in: rejected means not written."""
    from scripts.models import STATUS_REJECTED

    entry = _bladeborn(entries)[0]
    entry.author = Field('Test Author', 'user', 1.0)
    entry.status = STATUS_REJECTED

    ops = FileOperations(settings)
    result = ops.apply_entry(entry)

    assert result.skipped and not result.ok
    assert 'rejected' in result.reason
    assert not result.operations
    assert not settings.get_path('AO_OUTPUT_DIR').exists()


def test_a_rejected_book_can_still_be_previewed(settings, entries):
    """Looking at where a book would have gone is not putting it there."""
    from scripts.models import STATUS_REJECTED

    entry = _bladeborn(entries)[0]
    entry.author = Field('Test Author', 'user', 1.0)
    entry.status = STATUS_REJECTED

    result = FileOperations(settings).preview(entry)
    assert result.skipped and result.rejected and not result.ok
    assert result.operations, 'the preview still shows where it would have gone'


def test_move_removes_the_emptied_source_folder(settings, entries, tmp_library):
    """Moving a book that owns its folder takes the folder away too, not just the files."""
    settings.set('AO_COPY_MODE', 'false')
    wind = next(e for e in entries if 'Name of the Wind' in e.folder)
    wind.author = Field('Patrick Rothfuss', 'user', 1.0)
    folder = Path(wind.folder)
    (folder / 'Thumbs.db').write_bytes(b'')

    result = FileOperations(settings).apply_entry(wind)
    assert result.ok, result.error
    assert not folder.exists()
    assert tmp_library.is_dir()


def test_move_keeps_a_source_folder_that_still_holds_books(settings, entries):
    settings.set('AO_COPY_MODE', 'false')
    first = _bladeborn(entries)[0]
    first.author = Field('Test Author', 'user', 1.0)

    assert FileOperations(settings).apply_entry(first).ok
    assert Path(first.folder).is_dir()


def test_preview_dialog_groups_rejected_books_last_as_skipped(qt_app, settings, entries):
    from scripts.gui.preview_dialog import PreviewDialog
    from scripts.models import STATUS_APPROVED, STATUS_REJECTED

    books = _bladeborn(entries)
    kept = next(e for e in entries if e not in books)
    kept.author = Field('Kept Author', 'user', 1.0)
    kept.status = STATUS_APPROVED
    turned_down = books[0]
    turned_down.author = Field('Test Author', 'user', 1.0)
    turned_down.status = STATUS_REJECTED

    ops = FileOperations(settings)
    results = [ops.preview(turned_down), ops.preview(kept)]
    dialog = PreviewDialog(results, settings.get_path('AO_OUTPUT_DIR'),
                           settings=settings)
    last = dialog.tree.topLevelItem(dialog.tree.topLevelItemCount() - 1)
    assert last.text(0) == 'Skipped - rejected (1)'
    assert last.childCount() == 1 and last.child(0).childCount() > 0, \
        'the rejected book still shows where its files would have gone'
    assert not any(dialog.tree.topLevelItem(i).text(0).startswith('Not applied')
                   for i in range(dialog.tree.topLevelItemCount()))
    dialog.done(0)


def test_a_book_zero_point_five_is_not_padded_to_double_zero():
    """"0.5" is the number; "00.5" only adds a zero. Whole books still pad."""
    from scripts.paths import display_index, render_template

    assert display_index('0.5') == '0.5'
    assert display_index('3.5') == '03.5'
    assert display_index('1') == '01'
    assert render_template('{series} {series_index:02d} - {title}',
                           {'series': 'S', 'series_index': '0.5', 'title': 'T'}) == 'S 0.5 - T'


def test_move_takes_every_companion_file_and_extras_folder(settings, entries):
    """E-books of any format, and a subfolder of extras, go with the book."""
    settings.set('AO_COPY_MODE', 'false')
    settings.set('AO_RENAME_FILES', 'false')
    settings.set('AO_RENAME_SUPPORT_FILES', 'false')
    wind = next(e for e in entries if 'Name of the Wind' in e.folder)
    wind.author = Field('Patrick Rothfuss', 'user', 1.0)
    folder = Path(wind.folder)
    for name in ('book.mobi', 'book.azw3', 'book.epub', 'Thumbs.db'):
        (folder / name).write_bytes(b'x')
    (folder / 'Extras').mkdir()
    (folder / 'Extras' / 'map.pdf').write_bytes(b'x')

    result = FileOperations(settings).apply_entry(wind)
    assert result.ok, result.error
    names = {p.relative_to(result.destination).as_posix()
             for p in result.destination.rglob('*') if p.is_file()}
    assert {'book.mobi', 'book.azw3', 'book.epub', 'Extras/map.pdf'} <= names
    assert 'Thumbs.db' not in names
    assert not folder.exists()


def test_companion_files_are_renamed_without_renaming_audio(settings, entries):
    settings.set('AO_RENAME_FILES', 'false')
    settings.set('AO_RENAME_SUPPORT_FILES', 'true')
    settings.set('AO_FILE_TEMPLATE', '{title} {file_index:03d}')
    wind = next(e for e in entries if 'Name of the Wind' in e.folder)
    wind.author = Field('Patrick Rothfuss', 'user', 1.0)
    (Path(wind.folder) / 'whatever.mobi').write_bytes(b'x')

    result = FileOperations(settings).apply_entry(wind)
    assert result.ok, result.error
    names = {p.name for p in result.destination.iterdir()}
    assert 'The Name of the Wind.mobi' in names
    assert '01.mp3' in names


def test_shared_folder_takes_the_ebook_named_like_its_book(settings, entries):
    saga = _bladeborn(entries)
    first = saga[0]
    folder = Path(first.folder)
    stem = Path(first.primary_audio).stem
    (folder / f'{stem}.mobi').write_bytes(b'x')
    (folder / f'{Path(saga[1].primary_audio).stem}.epub').write_bytes(b'x')

    owned = {p.name for p in FileOperations(settings).files_for(first)}
    assert owned == {Path(first.primary_audio).name, f'{stem}.mobi'}


def test_only_whitelisted_companion_formats_travel(settings, entries):
    wind = next(e for e in entries if 'Name of the Wind' in e.folder)
    folder = Path(wind.folder)
    for name in ('book.kfx', 'book.fb2.zip', 'book.cbz', 'tracks.m3u', 'setup.exe'):
        (folder / name).write_bytes(b'x')

    owned = {p.name for p in FileOperations(settings).files_for(wind)}
    assert {'book.kfx', 'book.fb2.zip', 'book.cbz', 'tracks.m3u'} <= owned
    assert 'setup.exe' not in owned
