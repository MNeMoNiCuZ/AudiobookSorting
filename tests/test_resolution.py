"""Regex tier, provenance model, resolver chain, paths, dedupe and caching."""

from __future__ import annotations

import pytest

from scripts.api_query import author_similarity, similarity
from scripts.cache import Cache
from scripts.dedupe import find_duplicates
from scripts.models import SOURCE_CONFIDENCE, BookEntry, Field, clean_value
from scripts.paths import render_template, sanitize_component, shorten_path
from scripts.quality import inspect_entry, inspect_value
from scripts.regex_parser import parse_name, parse_path


# ------------------------------------------------------------------ regex tier

@pytest.mark.parametrize('name,expected', [
    ('Brandon Sanderson - Mistborn 01 - The Final Empire',
     {'author': 'Brandon Sanderson', 'series': 'Mistborn',
      'series_index': '1', 'title': 'The Final Empire'}),
    ('[Mistborn 02] The Well of Ascension',
     {'series': 'Mistborn', 'series_index': '2', 'title': 'The Well of Ascension'}),
    ('The Final Empire (Mistborn Book 1)',
     {'series': 'Mistborn', 'series_index': '1', 'title': 'The Final Empire'}),
    ('Book 3-An Echo of Titans',
     {'series_index': '3', 'title': 'An Echo of Titans'}),
    ('Book 15.5 - A Short Detour',
     {'series_index': '15.5', 'title': 'A Short Detour'}),
    ('Book 12,5 - Another Detour',
     {'series_index': '12.5', 'title': 'Another Detour'}),
    ('01. The Beginning', {'series_index': '1', 'title': 'The Beginning'}),
])
def test_regex_patterns(name, expected):
    result = parse_name(name)
    result.pop('_pattern', None)
    for key, value in expected.items():
        assert result.get(key) == value, f'{name}: {key}'


def test_regex_strips_noise():
    result = parse_name('The Hobbit [Narrated by Rob Inglis] {64kbps} (1937)')
    assert result['title'] == 'The Hobbit'


def test_generic_filenames_yield_nothing():
    """"book.m4b" must not become a title called "Book"."""
    assert parse_name('book') == {}
    assert parse_name('audiobook') == {}


def test_parse_path_prefers_folder_author(tmp_path):
    result = parse_path(
        str(tmp_path / 'Brandon Sanderson' / 'Mistborn 01 - The Final Empire' / 'book.m4b'),
        str(tmp_path))
    assert result['author'] == 'Brandon Sanderson'
    assert result['series'] == 'Mistborn'
    assert result['series_index'] == '1'
    assert result['title'] == 'The Final Empire'


def test_series_folder_is_not_treated_as_author(tmp_path):
    """"The Bladeborn Saga" is capitalised like a name but is a series."""
    result = parse_path(
        str(tmp_path / 'The Bladeborn Saga' / 'Book 2 - Ghost of the Shadowfort.m4b'),
        str(tmp_path))
    assert result.get('series') == 'The Bladeborn Saga'
    assert result.get('author') != 'The Bladeborn Saga'
    assert result['series_index'] == '2'


# --------------------------------------------------------------- provenance

def test_better_source_overwrites_worse():
    entry = BookEntry()
    assert entry.set_field('title', 'Guess', 'regex')
    assert entry.set_field('title', 'Real Title', 'metadata')
    assert entry.value('title') == 'Real Title'


def test_worse_source_does_not_overwrite_better():
    entry = BookEntry()
    entry.set_field('title', 'Real Title', 'audnexus')
    entry.set_field('title', 'Bad Guess', 'regex')
    assert entry.value('title') == 'Real Title'


def test_high_scoring_database_match_replaces_weaker_metadata(settings):
    from scripts.resolver import Resolver

    entry = BookEntry(entry_id='book')
    entry.set_field('author', 'Wrong Metadata Author', 'metadata')
    entry.set_field('title', 'The Right Book', 'metadata')
    resolver = Resolver(settings)
    resolver.api.search = lambda *args, **kwargs: {
        'author': 'Correct Database Author', 'title': 'The Right Book',
        'source': 'googlebooks', 'score': 0.96,
    }
    resolver.api.last_sources = ['googlebooks']
    resolver.api.last_candidates = []
    resolver.api.last_by_source = {}
    resolver.api.last_errors = {}

    resolver._tier_api(entry, sources=['googlebooks'], forced=True)

    assert entry.value('author') == 'Correct Database Author'
    assert entry.author.source == 'googlebooks'
    assert entry.author.confidence == pytest.approx(0.96)


def test_agreement_raises_confidence():
    """Two independent tiers agreeing is stronger evidence than either alone (#9)."""
    entry = BookEntry()
    entry.set_field('author', 'Brandon Sanderson', 'regex')
    before = entry.author.confidence

    entry.set_field('author', 'Brandon Sanderson', 'googlebooks')
    assert entry.author.confidence > before
    assert 'googlebooks' in entry.author.corroborated_by


def test_user_edits_are_absolute():
    entry = BookEntry()
    entry.set_field('title', 'From API', 'audnexus')
    entry.title = Field('Typed By Hand', 'user', 1.0)
    entry.set_field('title', 'From API Again', 'audnexus')
    assert entry.value('title') == 'Typed By Hand'


def test_confidence_ignores_series_when_standalone():
    entry = BookEntry()
    entry.set_field('author', 'A', 'metadata')
    entry.set_field('title', 'T', 'metadata')
    assert entry.confidence() == pytest.approx(SOURCE_CONFIDENCE['metadata'])


def test_one_lower_confidence_series_does_not_replace_the_whole_score():
    entry = BookEntry(
        author=Field('Myles Christensen', 'metadata', 0.80),
        title=Field("She Stole My Buddy's Starship", 'metadata', 0.80),
        series=Field("She Stole My Buddy's Starship", 'regex', 0.45),
        series_index=Field('2', 'regex', 0.45))

    assert entry.confidence() == pytest.approx(0.73)


def test_tags_alone_never_exceed_seventy_five_percent_confidence():
    entry = BookEntry()
    for name, value in (('author', 'A'), ('title', 'T'), ('series', 'S'),
                        ('series_index', '1')):
        entry.set_field(name, value, 'metadata')

    assert entry.confidence() == pytest.approx(0.75)


def test_confidence_penalises_series_without_index():
    """A missing index costs confidence; it does not annihilate it.

    Zeroing the whole entry meant the table could read 0% while every field card said
    45%, which is a contradiction the user has to resolve rather than the program.
    """
    entry = BookEntry()
    entry.set_field('author', 'A', 'metadata')
    entry.set_field('title', 'T', 'metadata')
    complete = entry.confidence()

    entry.set_field('series', 'S', 'metadata')
    assert 0.0 < entry.confidence() < complete


@pytest.mark.parametrize('field,value,expected', [
    ('series_index', 'Book 3', '3'),
    ('series_index', '03', '3'),
    ('series_index', '2.5', '2.5'),
    ('series_index', '12,5', '12.5'),
    ('series_index', 'none', ''),
    ('title', 'unknown', ''),
    ('author', '  Spaced   Out  ', 'Spaced Out'),
])
def test_clean_value(field, value, expected):
    assert clean_value(field, value) == expected


def test_round_trip_serialisation():
    entry = BookEntry(entry_id='x', folder='/f', primary_audio='/f/a.m4b')
    entry.set_field('title', 'T', 'metadata')
    restored = BookEntry.from_dict(entry.to_dict())
    assert restored.value('title') == 'T'
    assert restored.title.source == 'metadata'


def test_legacy_save_file_still_loads():
    """Old flat-string entries must not crash the loader."""
    restored = BookEntry.from_dict({
        'entry_id': 'old', 'title': 'Plain String', 'author': 'Someone',
        'full_audio_path': '/x/y.m4b', 'status': 'approved',
    })
    assert restored.value('title') == 'Plain String'
    assert restored.primary_audio == '/x/y.m4b'
    assert restored.status == 'approved'


# -------------------------------------------------------------------- resolver

def test_resolver_populates_from_names(entries):
    """Local tiers alone should identify the well-named books (#4)."""
    saga = [e for e in entries if 'Bladeborn' in e.folder]
    assert all(e.value('series') == 'The Bladeborn Saga' for e in saga)
    assert sorted(e.value('series_index') for e in saga) == ['1', '2', '3', '4']
    assert all(e.value('title') for e in saga)


def test_resolver_records_a_trace(entries):
    """Every entry must be able to explain itself (#29)."""
    for entry in entries:
        assert entry.trace
        assert any(step['tier'] == 'regex' for step in entry.trace)


def test_folder_siblings_share_author(settings, tmp_path):
    """One well-tagged file should rescue its badly-named siblings (#11)."""
    from scripts.file_scanner import FileScanner
    from scripts.resolver import Resolver
    from tests.conftest import make_stub

    root = tmp_path / 'shared'
    for name in ('Alpha.m4b', 'Beta.m4b'):
        make_stub(root / 'Some Series Saga' / name)

    settings.set('AO_INPUT_DIR', str(root))
    scanned = FileScanner(str(root)).scan_directory()
    resolver = Resolver(settings)
    resolver.resolve_folder(scanned)

    # Only one of them knows the author; the other should inherit it.
    scanned[0].set_field('author', 'Known Author', 'metadata')
    resolver._share_within_folder(scanned)
    assert scanned[1].value('author') == 'Known Author'


# ----------------------------------------------------------------------- paths

@pytest.mark.parametrize('values,expected', [
    ({'author': 'A', 'series': 'S', 'series_index': '1', 'title': 'T'}, 'A/S 01 - T'),
    ({'author': 'A', 'series': '', 'series_index': '', 'title': 'T'}, 'A/T'),
    ({'author': '', 'series': 'S', 'series_index': '3', 'title': 'T'}, 'S 03 - T'),
])
def test_template_collapses_empty_fields(values, expected):
    assert render_template('{author}/{series} {series_index:02d} - {title}',
                           values) == expected


@pytest.mark.parametrize('typed,stored', [
    ('1-03', '1-3'), ('01 - 03', '1-3'), ('1 to 3', '1-3'),
    ('5', '5'), ('003', '3'), ('2.5', '2.5'), ('7-7', '7'), ('none', ''),
])
def test_bundled_book_numbers_accept_a_range(typed, stored):
    from scripts.models import clean_value

    assert clean_value('series_index', typed) == stored


def test_index_padding_applies_to_both_ends_of_a_range():
    from scripts.paths import set_index_pad

    values = {'series': 'S', 'series_index': '1-3', 'title': 'T'}
    try:
        set_index_pad(2)
        assert render_template('{series} {series_index} - {title}', values) == 'S 01-03 - T'
        set_index_pad(3)
        assert render_template('{series} {series_index} - {title}', values) == 'S 001-003 - T'
        set_index_pad(0)
        assert render_template('{series} {series_index} - {title}', values) == 'S 1-3 - T'
        # A template stating its own width still wins over the setting.
        set_index_pad(2)
        assert render_template('{series_index:04d}', values) == '0001-0003'
        # A half-book keeps its fraction rather than being rounded into the padding.
        assert render_template('{series_index}',
                               {'series_index': '2.5'}) == '02.5'
        assert render_template('{series_index}',
                               {'series_index': '12,5'}) == '12.5'
    finally:
        set_index_pad(2)


def test_blocked_words_and_brackets_are_stripped():
    from scripts.models import clean_value, set_text_filters

    try:
        set_text_filters('series, unabridged', strip_parentheses=True)
        assert clean_value('series', 'The Expanse Series') == 'The Expanse'
        assert clean_value('title', 'Mistborn (Unabridged) [Audiobook]') == 'Mistborn'
        # Whole words only, and numbers are never filtered.
        assert clean_value('title', 'Seriously Funny') == 'Seriously Funny'
        assert clean_value('series_index', '2') == '2'
        # Nothing left but punctuation is an empty field, not a stub.
        assert clean_value('series', 'Series') == ''

        set_text_filters('', strip_parentheses=False)
        assert clean_value('title', 'Mistborn (Unabridged)') == 'Mistborn (Unabridged)'
    finally:
        set_text_filters('', strip_parentheses=True)


def test_reserved_names_are_escaped():
    assert sanitize_component('CON') == '_CON'
    assert sanitize_component('NUL') == '_NUL'


def test_illegal_characters_are_replaced():
    assert '?' not in sanitize_component('What? A Title')
    assert ':' not in sanitize_component('Title: Subtitle')


def test_trailing_dots_stripped():
    assert sanitize_component('Title... ') == 'Title'


def test_long_paths_are_shortened(tmp_path):
    long_title = 'Very Long Title Segment ' * 20
    result = shorten_path(tmp_path / 'Author' / long_title, 120)
    assert len(str(result)) <= 120
    assert result.name


# ---------------------------------------------------------------- fuzzy match

def test_subtitle_still_matches():
    """The exact-match bug (#16): these are the same book."""
    assert similarity('The Hobbit', 'The Hobbit: Or There and Back Again') > 0.82


def test_unrelated_titles_do_not_match():
    assert similarity('The Hobbit', 'Dune') < 0.4


def test_initials_match_full_names():
    assert author_similarity('J.R.R. Tolkien', 'John Ronald Reuel Tolkien') > 0.9


def test_warning_finds_inconsistent_author_initial_spacing():
    spaced = BookEntry(author=Field('N. S. Wikarski', 'metadata', 0.8))
    compact = BookEntry(author=Field('N.S. Wikarski', 'metadata', 0.8))

    findings = inspect_entry(spaced, [spaced, compact])

    assert any(finding.kind == 'initial_spacing' for finding in findings)


# --------------------------------------------------------------------- caching

def test_cache_hits_never_expire(tmp_path):
    cache = Cache(tmp_path / 'c.sqlite3', miss_ttl=0)
    cache.set('ns', 'key', {'a': 1})
    assert cache.get('ns', 'key') == {'a': 1}
    cache.close()


def test_cache_remembers_misses_distinctly(tmp_path):
    cache = Cache(tmp_path / 'c.sqlite3', miss_ttl=3600)
    cache.set_miss('ns', 'gone')
    assert cache.has('ns', 'gone')       # we did look
    assert cache.get('ns', 'gone') is None
    assert not cache.has('ns', 'never')  # we never looked
    cache.close()


# -------------------------------------------------------------------- dedupe

def _book(folder, entry_id, files, author='', title=''):
    entry = BookEntry(entry_id=entry_id, folder=str(folder), audio_files=list(files))
    if author:
        entry.set_field('author', author, 'metadata')
    if title:
        entry.set_field('title', title, 'metadata')
    return entry


def _write(path, payload, size=200_000):
    """A file of `size` bytes whose content is decided by `payload`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (payload * (size // len(payload) + 1))[:size]
    path.write_bytes(body)


def test_identical_copies_are_duplicates(tmp_path):
    """The same audio in two places, filed under different names, is one duplicate."""
    for folder in ('library/final empire', 'downloads/BS - Mistborn 1'):
        _write(tmp_path / folder / 'book.m4b', b'the final empire audio ')

    found = find_duplicates([
        _book(tmp_path / 'library/final empire', 'a', ['book.m4b']),
        _book(tmp_path / 'downloads/BS - Mistborn 1', 'b', ['book.m4b']),
    ])
    assert list(found) == ['b']          # the shorter path is the copy we keep
    assert found['b'] == ['a']


def test_chapters_of_one_book_are_not_duplicates(tmp_path):
    """The bug in the screenshot: every chapter resolved to the same author+title.

    Identity is identical across all three, and they sit in one folder. None of that
    makes them duplicates - only identical bytes would, and chapters differ.
    """
    folder = tmp_path / '1-20 and SS'
    entries = []
    for index in range(1, 4):
        name = f'06 - Vengeance in Death-{index}.mp3'
        _write(folder / name, f'chapter {index} audio '.encode())
        entries.append(_book(folder, name, [name],
                             author='J.D. Robb', title='06 Vengeance in Death'))

    assert find_duplicates(entries) == {}


def test_same_title_different_edition_is_not_a_duplicate(tmp_path):
    """Two encodes of one book agree on every field and differ on disk. Not duplicates."""
    _write(tmp_path / 'a' / 'book.m4b', b'narrator one ', size=200_000)
    _write(tmp_path / 'b' / 'book.m4b', b'narrator two ', size=300_000)

    assert find_duplicates([
        _book(tmp_path / 'a', 'a', ['book.m4b'], 'Brandon Sanderson', 'The Final Empire'),
        _book(tmp_path / 'b', 'b', ['book.m4b'], 'Brandon Sanderson', 'the final empire'),
    ]) == {}


def test_same_size_different_content_is_not_a_duplicate(tmp_path):
    """Matching sizes get as far as the hash and no further."""
    _write(tmp_path / 'a' / 'book.m4b', b'aaaaaaaa', size=200_000)
    _write(tmp_path / 'b' / 'book.m4b', b'bbbbbbbb', size=200_000)

    assert find_duplicates([_book(tmp_path / 'a', 'a', ['book.m4b']),
                            _book(tmp_path / 'b', 'b', ['book.m4b'])]) == {}


def test_duplicate_multi_file_copies(tmp_path):
    """A whole chapter run copied twice is one duplicate, not one per chapter."""
    names = [f'{index:02d}.mp3' for index in range(1, 6)]
    for folder in ('lib/book', 'dupes/book copy'):
        for name in names:
            _write(tmp_path / folder / name, f'chapter {name} '.encode())

    found = find_duplicates([_book(tmp_path / 'lib/book', 'a', names),
                             _book(tmp_path / 'dupes/book copy', 'b', names)])
    assert found == {'b': ['a']}


def test_only_a_full_sha_match_counts(tmp_path):
    """Files that agree on size and on both ends, but differ in the middle, are not
    duplicates. The cheap tests can only rule a pair out; the full hash decides."""
    size = 4 * 1024 * 1024
    head_and_tail = (b'H' * (1024 * 1024), b'T' * (1024 * 1024))
    middle = size - 2 * 1024 * 1024

    for folder, filler in (('a', b''), ('b', b''), ('c', b'')):
        path = tmp_path / folder / 'book.m4b'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(head_and_tail[0] + filler * middle + head_and_tail[1])

    entries = [_book(tmp_path / name, name, ['book.m4b']) for name in ('a', 'b', 'c')]
    # All three are the same size with identical first and last megabytes, so all three
    # survive to the full hash - and only the two that really match are flagged.
    assert find_duplicates(entries) == {'b': ['a']}


def test_missing_files_are_never_duplicates(tmp_path):
    """An entry whose files are gone cannot be compared, so it is left alone."""
    assert find_duplicates([_book(tmp_path / 'gone', 'a', ['book.m4b']),
                            _book(tmp_path / 'also gone', 'b', ['book.m4b'])]) == {}


def test_stale_duplicate_flag_is_cleared(tmp_path):
    """A flag left by the old name-based check does not survive a rescan."""
    from scripts.dedupe import mark_duplicates
    from scripts.models import STATUS_DUPLICATE, STATUS_PENDING

    _write(tmp_path / 'a' / 'book.m4b', b'unique audio ')
    entry = _book(tmp_path / 'a', 'a', ['book.m4b'])
    entry.status = STATUS_DUPLICATE
    entry.duplicate_of = 'something else'

    assert mark_duplicates([entry]) == 0
    assert entry.status == STATUS_PENDING
    assert entry.duplicate_of == ''


def test_normal_title_numbers_are_not_suspicious():
    assert not inspect_value('title', 'A Story - Part 1')
    assert not inspect_value('title', 'A Story, Volume Two')
    assert not inspect_value('title', 'The Sheriff 2')
    assert not inspect_value('title', 'Books 1-3')
    assert not inspect_value('author', 'adastra339')
    assert any(finding.kind == 'file_segment'
               for finding in inspect_value('title', 'A Story - Track 03'))
    assert not inspect_value('title', 'A Story-01')
    assert not inspect_value('title', '003')
    assert not inspect_value('title', 'The Parting Glass')


@pytest.mark.parametrize('value,kinds', [
    ('01/80', {'chapter_fraction', 'separator'}),
    ('A chapter - 1/9', {'chapter_fraction', 'separator'}),
    ('ObsessioninDeathUnabridgedPart1 Mp332 Aequz5bhhjgtu',
     {'file_words'}),
    ('Title | Alternate', {'separator'}),
    ('Title \\ Alternate', {'separator'}),
    ('Title "Alternate"', {'separator'}),
])
def test_suspicious_identity_patterns(value, kinds):
    found = {finding.kind for finding in inspect_value('title', value)}
    assert kinds <= found


def test_suggested_fix_removes_empty_brackets_after_file_details():
    from scripts.quality import suggest_fix

    assert suggest_fix(
        'title', 'Constitution (Unabridged)', 'file_words') == 'Constitution'
