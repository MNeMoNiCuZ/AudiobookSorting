"""The web-search tier: what it can pull out of a scraped page, and how it reports
having pulled out nothing.

Every test here is offline - the page bodies are trimmed copies of the real
Goodreads shape, and the search engines are replaced by canned rows. The tier's
value is almost entirely in the parsing, which is what these cover.
"""

from __future__ import annotations

import json
import sys

import pytest

from scripts.web_search import WebSearchClient, _clean


def _goodreads_page(book: dict, state_extra: dict = None) -> str:
    """Wrap an Apollo store in the surrounding page, as Goodreads serves it."""
    state = {
        'Book:kca://book/x': book,
        'Contributor:kca://author/a': {
            '__typename': 'Contributor', 'name': 'Joshua   Reynolds'},
        'Series:kca://series/s': {'__typename': 'Series', 'title': 'Arkham Horror'},
    }
    state.update(state_extra or {})
    payload = {'props': {'pageProps': {'apolloState': state}}}
    return ('<html><head><title>Wrath of N&apos;kai by Joshua Reynolds | Goodreads'
            '</title></head><body>'
            '<script id="__NEXT_DATA__" type="application/json">'
            f'{json.dumps(payload)}</script></body></html>')


BOOK = {
    '__typename': 'Book',
    'title': "Wrath of N'kai",
    'primaryContributorEdge': {'node': {'__ref': 'Contributor:kca://author/a'}},
    'bookSeries': [{'userPosition': '10', 'series': {'__ref': 'Series:kca://series/s'}}],
    'details': {'isbn': '1839080116', 'publisher': 'Aconyte'},
}


def test_next_data_supplies_series_and_position():
    """The blob is the only place the position in the series is stated outright."""
    found = WebSearchClient()._parse_book_page(_goodreads_page(BOOK))
    assert found['title'] == "Wrath of N'kai"
    assert found['series'] == 'Arkham Horror'
    assert found['series_index'] == '10'
    assert found['author'] == 'Joshua Reynolds'  # doubled spaces collapsed
    assert found['publisher'] == 'Aconyte'


def test_stub_books_are_ignored():
    """"Readers also enjoyed" rails put other Book entities in the same store."""
    stubs = {f'Book:kca://book/stub{i}': {'__typename': 'Book'} for i in range(5)}
    found = WebSearchClient()._parse_book_page(_goodreads_page(BOOK, stubs))
    assert found['title'] == "Wrath of N'kai"


def test_unnumbered_series_yields_no_index():
    """An empty or ranged userPosition must not become a bogus index."""
    for position in ('', '1-3', 'None'):
        book = dict(BOOK, bookSeries=[
            {'userPosition': position, 'series': {'__ref': 'Series:kca://series/s'}}])
        found = WebSearchClient()._parse_book_page(_goodreads_page(book))
        assert found['series'] == 'Arkham Horror'
        assert 'series_index' not in found, position


def test_series_slug_fallback_keeps_the_whole_name():
    """Without a blob, the /series/<id>-<slug> link is all there is.

    The slug hyphenates the entire name, so a capture that stopped at the first
    hyphen turned "arkham-horror" into "arkham".
    """
    html = ('<html><head><title>Wrath of N&apos;kai by Josh Reynolds</title></head>'
            '<body><a href="/series/145951-arkham-horror">Arkham Horror</a></body></html>')
    found = WebSearchClient()._parse_book_page(html)
    assert found['series'] == 'Arkham Horror'
    assert found['title'] == "Wrath of N'kai"  # entity unescaped


def test_missing_blob_is_not_an_error():
    assert WebSearchClient()._parse_book_page('<html><body>nope</body></html>') == {}
    assert WebSearchClient()._parse_book_page('') == {}


def test_malformed_blob_is_not_an_error():
    html = ('<script id="__NEXT_DATA__" type="application/json">{not json'
            '</script>')
    assert WebSearchClient()._parse_book_page(html) == {}


def test_clean_collapses_whitespace_and_entities():
    assert _clean('Wrath of N&apos;kai') == "Wrath of N'kai"
    assert _clean('Joshua   Reynolds') == 'Joshua Reynolds'
    assert _clean(None) == ''


@pytest.fixture
def no_ddg(monkeypatch):
    """Make the DuckDuckGo library unimportable.

    Otherwise these tests depend on what the live engine happens to do today,
    which is exactly the flakiness they exist to describe.
    """
    monkeypatch.setitem(sys.modules, 'duckduckgo_search', None)


def test_no_brave_key_is_explained_rather_than_reported_as_no_results(no_ddg):
    """A blocked engine and a book nobody has heard of must not look identical."""
    client = WebSearchClient()
    client._get_text = lambda url: ''  # every HTTP route dead
    client._ddg('anything')
    assert 'AO_SEARCH_BRAVE_KEY' in client.last_error
    assert 'blocked or rate-limited' in client.last_error


def test_brave_is_preferred_when_a_key_is_set():
    client = WebSearchClient()
    client.brave_key = 'test-key'
    client._brave = lambda query: [{'title': 'Mistborn (Mistborn, #1) by Brandon '
                                            'Sanderson', 'body': '', 'href': ''}]
    rows = client._ddg('mistborn')
    assert len(rows) == 1
    assert not client.last_error


def test_brave_failure_falls_through_to_duckduckgo_and_says_so(no_ddg):
    client = WebSearchClient()
    client.brave_key = 'bad-key'

    def refuse(query):
        raise RuntimeError('HTTP 401 - the API key was rejected')

    client._brave = refuse
    client._get_text = lambda url: ''
    client._ddg('mistborn')
    assert 'HTTP 401' in client.last_error
