"""The book-database tier: what it searches with, and how it scores what comes back.

Every test here is offline. `_get_json` is replaced with a table of canned bodies keyed
by a fragment of the URL, which also lets each test assert on the URLs that were
actually requested - the queries are the thing that was wrong.
"""

from __future__ import annotations

import pytest

from scripts.api_query import (BookAPIClient, _clean_query, _clean_title,
                               _freetext_score, _split_title_series)


class FakeAPI(BookAPIClient):
    """A client that answers from a canned table instead of the network."""

    def __init__(self, bodies, **kwargs):
        super().__init__(cache=None, **kwargs)
        self.bodies = bodies
        self.urls = []

    def _get_json(self, source, url, attempts=3):
        self.urls.append(url)
        for fragment, body in self.bodies.items():
            if fragment in url:
                return body
        return None

    def _rate_limit(self, source):
        pass                       # no sleeping in tests


def audible(*products):
    return {'products': list(products)}


def product(title, author, series='', sequence=''):
    return {'title': title, 'authors': [{'name': author}],
            'series': ([{'title': series, 'sequence': sequence}] if series else [])}


def itunes(*names):
    return {'results': [{'collectionName': name, 'artistName': author}
                        for name, author in names]}


# ------------------------------------------------------------------ query cleaning

@pytest.mark.parametrize('raw, expected', [
    # The bug: "01" is not in any catalogue, and asking for it returned the omnibus.
    ('Sanderson - Mistborn 01 - The Final Empire',
     'Sanderson Mistborn The Final Empire'),
    ('The Hobbit [Narrated by Rob Inglis] {64kbps} (1937)', 'The Hobbit'),
    ('Dumb Luck and Dead Heroes 09 - The Worst Admiral in the Star Cluster',
     'Dumb Luck and Dead Heroes The Worst Admiral in the Star Cluster'),
    ('Book 4 -The Winds of War', 'The Winds of War'),
    # A title that is genuinely a number keeps it: nothing else is left to search with.
    ('1984', '1984'),
    ('11', '11'),
])
def test_the_search_string_drops_bookkeeping_and_keeps_the_book(raw, expected):
    assert _clean_query(raw) == expected


@pytest.mark.parametrize('raw, title, series, index', [
    ('The Worst Admiral in the Star Cluster: Dumb Luck and Dead Heroes, Book 9 '
     '(Unabridged)',
     'The Worst Admiral in the Star Cluster', 'Dumb Luck and Dead Heroes', '9'),
    ('Harry Potter and the Deathly Hallows, Book 7',
     'Harry Potter and the Deathly Hallows', '', '7'),
    # No position, so a colon is a subtitle and must not be read as a series.
    ("Harry Potter and the Philosopher's Stone",
     "Harry Potter and the Philosopher's Stone", '', ''),
    ('The Hobbit: Or There and Back Again', 'The Hobbit: Or There and Back Again',
     '', ''),
])
def test_a_catalogue_name_splits_into_title_series_and_position(raw, title, series,
                                                                index):
    assert _split_title_series(raw) == (title, series, index)


def test_the_position_never_survives_into_the_title():
    assert _clean_title('Harry Potter and the Deathly Hallows, Book 7') == \
        'Harry Potter and the Deathly Hallows'


# ------------------------------------------------------------------------ scoring

def test_a_book_about_a_book_scores_below_the_book():
    """The regression: the trivia test outscored the novel and won the search."""
    real = _freetext_score('harry potter hallows',
                           'Harry Potter and the Deathly Hallows J.K. Rowling')
    trivia = _freetext_score(
        'harry potter hallows',
        'Harry Potter and the Deathly Hallows Ultimate Trivia Test Melanie Evans')
    assert real > trivia


def test_the_derivative_penalty_applies_to_a_scored_candidate():
    client = FakeAPI({})
    hints = {'query': 'harry potter hallows'}
    novel = {'title': 'Harry Potter and the Deathly Hallows', 'author': 'J.K. Rowling',
             'series': '', 'source': 'itunes'}
    guide = {'title': 'Harry Potter and the Deathly Hallows: A Study Guide',
             'author': 'Anon', 'series': '', 'source': 'itunes'}
    assert client._score(novel, hints) > client._score(guide, hints)


# -------------------------------------------------------------------- second pass

def test_a_filename_only_search_asks_again_with_the_title_it_learned():
    """The whole point: with no metadata of our own, one source recognises the mess and
    the rest are then asked something they can actually answer."""
    bodies = {
        # Audible cannot answer the filename, but answers the refined title exactly.
        'keywords=Dumb+Luck': audible(product(
            'Dumb Luck and Dead Heroes Omnibus, Books 1-3', 'Skyler Ramirez',
            'Dumb Luck and Dead Heroes', '1-3')),
        'keywords=The+Worst+Admiral': audible(product(
            'The Worst Admiral in the Star Cluster', 'Skyler Ramirez',
            'Dumb Luck and Dead Heroes', '9')),
        'itunes.apple.com': itunes(
            ('The Worst Admiral in the Star Cluster: Dumb Luck and Dead Heroes, '
             'Book 9 (Unabridged)', 'Skyler Ramirez')),
    }
    client = FakeAPI(bodies, sources=['audnexus', 'itunes'], query_all=True)
    best = client.search({'query': 'Dumb Luck and Dead Heroes 09 - The Worst Admiral '
                                   'in the Star Cluster'})

    assert client.last_refined_with == {
        'title': 'The Worst Admiral in the Star Cluster', 'author': 'Skyler Ramirez'}
    # Audible was asked twice: once with the filename, once with what Apple knew.
    assert any('keywords=Dumb+Luck' in u for u in client.urls)
    assert any('keywords=The+Worst+Admiral' in u for u in client.urls)
    # And the answer is the actual book, with the series position nothing else knew.
    assert best['title'] == 'The Worst Admiral in the Star Cluster'
    assert (best['series'], best['series_index']) == ('Dumb Luck and Dead Heroes', '9')


def test_no_second_pass_when_the_refined_query_is_the_one_already_sent():
    bodies = {'keywords=The+Hobbit': audible(product('The Hobbit', 'J.R.R. Tolkien'))}
    client = FakeAPI(bodies, sources=['audnexus'], query_all=True)
    client.search({'title': 'The Hobbit', 'author': 'J.R.R. Tolkien'})
    assert client.last_refined_with is None
    assert len(client.urls) == 1


def test_a_source_that_returned_nothing_is_re_asked_with_the_learned_title():
    """LibriVox needs a title. Given only a filename it was never called at all."""
    bodies = {
        'itunes.apple.com': itunes(('Alice in Wonderland', 'Lewis Carroll')),
        'librivox.org': {'books': [{'title': "Alice's Adventures in Wonderland",
                                    'authors': [{'first_name': 'Lewis',
                                                 'last_name': 'Carroll'}]}]},
    }
    client = FakeAPI(bodies, sources=['itunes', 'librivox'], query_all=True)
    client.search({'query': 'alice in wonderland audiobook'})
    assert client.last_by_source['librivox'], 'LibriVox was never asked'


# ------------------------------------------------------------------- source quirks

def test_librivox_asks_for_a_partial_title_match():
    """?title=Alice is an exact match and answers 404; only ^Alice searches."""
    client = FakeAPI({'librivox.org': {'books': []}}, sources=['librivox'])
    client.search({'title': 'Alice in Wonderland'})
    assert 'title=%5EAlice' in client.urls[0], client.urls[0]


def test_librivox_falls_back_to_the_author_when_there_is_no_title():
    client = FakeAPI({'librivox.org': {'books': []}}, sources=['librivox'])
    client.search({'author': 'Lewis Carroll'})
    assert 'author=Carroll' in client.urls[0], client.urls[0]


def test_google_books_sends_the_key_when_there_is_one():
    client = FakeAPI({'googleapis.com': {'items': []}}, sources=['googlebooks'],
                     google_key='abc123')
    client.search({'title': 'Dune'})
    assert 'key=abc123' in client.urls[0]


# --------------------------------------------------------------- filling the gaps

def test_a_missing_author_is_filled_by_agreement_not_by_the_best_score():
    """The regression: The Hobbit came out credited to a film-companion author.

    The winning record had a title and no author. The next-best row with an author was
    a different book that happened to share the title, and it was taken on the strength
    of its score alone - while three other candidates all said Tolkien.
    """
    client = FakeAPI({}, sources=['openlibrary'])
    client.last_candidates = [
        {'title': 'The Hobbit', 'author': '', 'score': 1.0, 'source': 'openlibrary'},
        {'title': 'The Hobbit', 'author': 'Jude Fisher', 'score': 0.85,
         'source': 'openlibrary'},
        {'title': 'The Hobbit', 'author': 'J. R. R. Tolkien', 'score': 0.83,
         'source': 'itunes'},
        {'title': 'The Hobbit', 'author': 'J.R.R. Tolkien', 'score': 0.82,
         'source': 'openlibrary'},
    ]
    best = client.last_candidates[0]
    merged = client._merge_gaps(best, {'query': 'the hobbit'})
    assert 'Tolkien' in merged['author'], merged['author']
    assert 'agreed by 2' in merged['filled_from']['author']
