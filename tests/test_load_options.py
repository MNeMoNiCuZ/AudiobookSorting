"""What a load is allowed to throw away.

Loading the input folder over work you have already done is the one destructive thing
this program does to the *list* (as opposed to your files), so what survives it is
pinned here: your own edits, confident values, review decisions, and books already
written to the output folder.
"""

from __future__ import annotations

from scripts.load_options import (KeepOptions, Tally, apply_keep, plan_load,
                                  unsaved_entries)
from scripts.models import (STATUS_APPLIED, STATUS_APPROVED, STATUS_PENDING,
                            BookEntry, Field)


def book(entry_id='b', **fields) -> BookEntry:
    entry = BookEntry(entry_id=entry_id, folder=f'/library/{entry_id}',
                      audio_files=['01.mp3'])
    for name, (value, source, confidence) in fields.items():
        setattr(entry, name, Field(value=value, source=source,
                                   confidence=confidence))
    return entry


def test_a_typed_value_survives_a_load():
    """The one value here that is known rather than guessed."""
    entry = book(author=('Le Guin', 'user', 1.0), title=('A Wizard', 'regex', 0.45))

    apply_keep([entry], KeepOptions(manual=True, above=101, decisions=True))

    assert entry.value('author') == 'Le Guin'
    assert entry.value('title') == ''


def test_untick_manual_and_your_own_edits_go_too():
    """There is exactly one way to lose them, and it is asking for it."""
    entry = book(author=('Le Guin', 'user', 1.0))

    apply_keep([entry], KeepOptions(manual=False, above=101, decisions=True))

    assert entry.value('author') == ''


def test_the_threshold_keeps_the_confident_and_clears_the_guesses():
    entry = book(author=('Sanderson', 'audnexus', 0.90),
                 title=('Mistborn', 'regex', 0.45))

    apply_keep([entry], KeepOptions(manual=True, above=75, decisions=True))

    assert entry.value('author') == 'Sanderson'
    assert entry.value('title') == ''


def test_keeping_everything_touches_nothing():
    """The non-destructive load: the same read the old resumable scan did."""
    entry = book(author=('Sanderson', 'regex', 0.45))
    entry.status = STATUS_APPROVED
    entry.trace = [{'tier': 'regex', 'message': 'parsed'}]

    plan = apply_keep([entry], KeepOptions.keep_everything())

    assert plan.cleared == 0
    assert entry.status == STATUS_APPROVED
    assert entry.trace, 'an untouched entry kept its trace'


def test_clearing_a_book_drops_the_reasoning_that_explained_it():
    """A trace describing values that no longer exist is worse than none."""
    entry = book(title=('Guess', 'guess', 0.25))
    entry.trace = [{'tier': 'guess', 'message': 'from the folder name'}]
    entry.evidence = {'search': ['something']}
    entry.resolved = True

    apply_keep([entry], KeepOptions(manual=True, above=75, decisions=True))

    assert entry.evidence == {}
    assert not entry.resolved
    # The clearing itself is recorded - the trace is not simply empty.
    assert [step for step in entry.trace if step['tier'] == 'user']


def test_decisions_are_kept_or_dropped_as_asked():
    kept, dropped = book('a', title=('X', 'guess', 0.25)), book('b', title=('X', 'guess', 0.25))
    kept.status = dropped.status = STATUS_APPROVED

    apply_keep([kept], KeepOptions(manual=True, above=75, decisions=True))
    apply_keep([dropped], KeepOptions(manual=True, above=75, decisions=False))

    assert kept.status == STATUS_APPROVED
    assert dropped.status == STATUS_PENDING


def test_books_already_written_to_disk_are_left_alone():
    """Clearing the author of a book filed under that author breaks undo."""
    entry = book(author=('Sanderson', 'guess', 0.25))
    entry.status = STATUS_APPLIED
    entry.applied_path = '/output/Sanderson/Mistborn'

    plan = apply_keep([entry], KeepOptions(manual=False, above=101, decisions=False))

    assert entry.value('author') == 'Sanderson'
    assert plan.skipped_applied == 1 and plan.cleared == 0


def test_the_dialog_count_is_the_count_that_happens():
    """plan_load promises, apply_keep delivers - they must not drift apart."""
    entries = [book('a', author=('Le Guin', 'user', 1.0),
                    title=('A Wizard', 'regex', 0.45)),
               book('b', author=('Sanderson', 'audnexus', 0.9)),
               book('c', title=('Guess', 'guess', 0.25))]
    entries[2].status = STATUS_APPROVED
    keep = KeepOptions(manual=True, above=75, decisions=False)

    promised = plan_load(entries, keep)
    done = apply_keep(entries, keep)

    assert (promised.cleared, promised.kept, promised.books, promised.unreviewed) == \
           (done.cleared, done.kept, done.books, done.unreviewed)
    assert promised.per_field == done.per_field, 'the per-field table too'
    assert done.cleared == 2 and done.kept == 2 and done.unreviewed == 1


def test_the_plan_says_which_field_is_being_thrown_away():
    """The dialog's table: a total of 6 tells you nothing, 'every series #' does."""
    entries = [book('a', author=('Sanderson', 'audnexus', 0.9),
                    series=('Mistborn', 'regex', 0.4)),
               book('b', author=('Le Guin', 'user', 1.0),
                    series=('Earthsea', 'guess', 0.25))]

    plan = plan_load(entries, KeepOptions(manual=True, above=75, decisions=True))

    assert (plan.tally('author').kept, plan.tally('author').cleared) == (2, 0)
    assert (plan.tally('series').kept, plan.tally('series').cleared) == (0, 2)
    # Fields nobody has a value for are still rows, with nothing in either column.
    assert plan.tally('title') == Tally(kept=0, cleared=0)
    assert sum(t.cleared for t in plan.per_field.values()) == plan.cleared


def test_a_cleared_book_is_read_from_disk_again_by_the_same_load(qt_app, settings,
                                                                 tmp_library):
    """The whole point of clearing: the values come back, from tags and filenames.

    They did not. The scan's offline pass ran on books "with no trace", and clearing
    writes a "Cleared on load" line into the trace - so every book the load emptied
    was the one book it refused to re-read, and the table came back blank at zero
    confidence. The gate is `resolved` now, which is what the clearing actually says.
    """
    from scripts.data_manager import DataManager
    from scripts.file_scanner import FileScanner
    from scripts.resolver import Resolver
    from scripts.workers import ScanWorker

    data = DataManager(save_file=tmp_library.parent / 'entries.json')
    scanner = FileScanner(str(tmp_library))
    worker = lambda: ScanWorker(scanner, data, resume=True,  # noqa: E731
                                resolver=Resolver(settings)).work()['entries']

    entries = worker()
    before = {e.entry_id: e.value('title') for e in entries}
    assert any(before.values()), 'the first scan reads the books'

    # A load that keeps nothing - the new default, and the report that found this.
    apply_keep(data.all(), KeepOptions(manual=False, above=101, decisions=False))
    assert all(not e.value('title') for e in data.all()), 'cleared, as asked'

    entries = worker()

    assert {e.entry_id: e.value('title') for e in entries} == before
    assert any(e.confidence() > 0 for e in entries), 'and with confidence, not zero'


def test_unsaved_is_typed_or_approved_and_not_yet_written():
    typed = book('typed', author=('Le Guin', 'user', 1.0))
    approved = book('approved', author=('Sanderson', 'audnexus', 0.9))
    approved.status = STATUS_APPROVED
    guessed = book('guessed', author=('Sanderson', 'regex', 0.45))
    written = book('written', author=('Le Guin', 'user', 1.0))
    written.status = STATUS_APPLIED
    written.applied_path = '/output/Le Guin'

    waiting = {e.entry_id for e in unsaved_entries([typed, approved, guessed, written])}

    assert waiting == {'typed', 'approved'}
