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
