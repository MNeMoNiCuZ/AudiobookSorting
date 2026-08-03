"""Regex tier, provenance model, resolver chain, paths, dedupe and caching."""

from __future__ import annotations

import pytest

from scripts.api_query import author_similarity, similarity
from scripts.cache import Cache
from scripts.dedupe import find_duplicates
from scripts.models import SOURCE_CONFIDENCE, BookEntry, Field, clean_value
from scripts.paths import render_template, sanitize_component, shorten_path
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

def test_duplicate_detection():
    def book(entry_id, author, title):
        entry = BookEntry(entry_id=entry_id)
        entry.set_field('author', author, 'metadata')
        entry.set_field('title', title, 'metadata')
        return entry

    found = find_duplicates([
        book('a', 'Brandon Sanderson', 'The Final Empire'),
        book('b', 'Brandon Sanderson', 'the final empire'),
        book('c', 'Patrick Rothfuss', 'The Name of the Wind'),
    ])
    assert 'b' in found
    assert 'c' not in found
