"""Tier 3: look the book up in online databases.

Sources are tried in the configured order and their results are *merged*, not raced -
Audnexus knows series and index, Google Books has the widest coverage, OpenLibrary is
the fallback. Every candidate is scored by fuzzy similarity against what we already
know, so a near-miss title no longer throws the match away.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

from .models import normalize

logger = logging.getLogger(__name__)

USER_AGENT = 'AudiobookOrganizer/2.0 (+https://github.com/MNeMoNiCuZ/AudiobookSorting)'

# id -> (label, what it is good for). The Settings page renders one checkbox per entry,
# so adding a source here is all it takes to expose it. Order is the query order:
# audiobook-native sources first, general book catalogues after.
AVAILABLE_SOURCES = [
    ('audnexus', 'Audible / Audnexus', 'Audiobook catalogue. Best series data.'),
    ('itunes', 'Apple Books', 'Audiobook catalogue. Good coverage of non-Audible titles.'),
    ('googlebooks', 'Google Books', 'Widest general coverage; series data is patchy.'),
    ('openlibrary', 'Open Library', 'Free catalogue, decent series records.'),
    ('librivox', 'LibriVox', 'Public-domain audiobooks only. Narrow but exact.'),
]

DEFAULT_SOURCES = ['audnexus', 'itunes', 'googlebooks', 'openlibrary']


# --------------------------------------------------------------------- similarity

def similarity(a: str, b: str) -> float:
    """Token-set similarity in 0..1, order- and subtitle-insensitive.

    "The Hobbit" vs "The Hobbit: Or There and Back Again" scores high, which is the
    whole point - the old exact-match test rejected it.
    """
    if not a or not b:
        return 0.0
    tokens_a = set(normalize(a).split())
    tokens_b = set(normalize(b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    if tokens_a == tokens_b:
        return 1.0

    overlap = len(tokens_a & tokens_b)
    # Containment matters more than symmetric difference: a subtitle shouldn't be
    # punished, so we score against the *smaller* token set.
    containment = overlap / min(len(tokens_a), len(tokens_b))
    ratio = SequenceMatcher(None, normalize(a), normalize(b)).ratio()
    return round(max(containment * 0.9 + ratio * 0.1, ratio), 3)


def _freetext_score(query: str, candidate: str) -> float:
    """Score a candidate against a whole filename-derived query.

    Containment alone is not enough here and quietly picked the wrong book. A short
    query - "Harry potter hallows" - is *entirely* contained in "Harry Potter and the
    Deathly Hallows" and equally entirely contained in "Harry Potter and the Deathly
    Hallows Ultimate Trivia Test", so both scored ~97% and which one won came down to
    noise. The trivia book won, and the second pass then searched for more of it.

    So how much of the *candidate* the query accounts for is scored too: the real book
    is three-quarters accounted for, the trivia test barely a quarter. Padding a title
    with words we never asked for now costs something.
    """
    if not query or not candidate:
        return 0.0
    tokens_q = set(normalize(query).split())
    tokens_c = set(normalize(candidate).split())
    if not tokens_q or not tokens_c:
        return 0.0

    overlap = len(tokens_q & tokens_c)
    contained = overlap / len(tokens_q)          # how much of the query was found
    accounted = overlap / len(tokens_c)          # how much of the candidate we asked for
    ratio = SequenceMatcher(None, normalize(query), normalize(candidate)).ratio()
    return round(min(1.0, contained * 0.75 + accounted * 0.15 + ratio * 0.10), 3)


def author_similarity(a: str, b: str) -> float:
    """Compare author names, tolerating "J.R.R. Tolkien" vs "John Ronald Reuel Tolkien"."""
    if not a or not b:
        return 0.0
    na, nb = normalize(a), normalize(b)
    if na == nb:
        return 1.0
    # Surnames match and initials are compatible -> treat as the same person.
    parts_a, parts_b = na.split(), nb.split()
    if parts_a and parts_b and parts_a[-1] == parts_b[-1]:
        initials_a = ''.join(p[0] for p in parts_a[:-1])
        initials_b = ''.join(p[0] for p in parts_b[:-1])
        if initials_a.startswith(initials_b) or initials_b.startswith(initials_a):
            return 0.95
        return 0.8
    return similarity(a, b)


# ------------------------------------------------------------------------ client

class BookAPIClient:
    """Queries the configured book databases, with caching and rate limiting."""

    def __init__(self, cache=None, sources: Optional[List[str]] = None,
                 threshold: float = 0.80, timeout: int = 20,
                 require_cover: bool = False, query_all: bool = False,
                 google_key: str = ''):
        self.logger = logging.getLogger(__name__)
        self.cache = cache
        self.sources = sources or list(DEFAULT_SOURCES)
        self.threshold = threshold
        self.timeout = timeout
        # Google Books stopped serving anonymous callers: the shared project's
        # "Queries per day" limit is now 0, so every unauthenticated request comes back
        # HTTP 429 for ever. A key is the only way to use that source at all, and
        # without one it has to say so rather than report "no rows".
        self.google_key = (google_key or '').strip()
        self._last_request: Dict[str, float] = {}
        self._lock = threading.Lock()
        self.min_interval = 0.25
        # Stop only when the answer is actually complete. A source that knows the
        # title and author but not the series has not finished the job, and moving on
        # to the next source costs one request.
        self.require_cover = require_cover
        # Set for an explicit, user-initiated run: query every source regardless.
        self.query_all = query_all
        # The best candidate that *didn't* clear the threshold, kept so the "why" panel
        # can show a near miss instead of a bare "no match".
        self.last_rejected: Optional[Dict[str, Any]] = None
        # Every scored candidate from the last search, accepted or not. "No confident
        # match" is not a useful thing to be told on its own - you need to see what the
        # sources actually said to judge whether the threshold or the query was wrong.
        self.last_candidates: List[Dict[str, Any]] = []
        # source id -> what that source returned, so a manual run can report every
        # database separately instead of collapsing them into one winner.
        self.last_by_source: Dict[str, List[Dict[str, Any]]] = {}
        # Which sources were actually asked on the last search.
        self.last_sources: List[str] = []
        # source id -> why it produced nothing, when the reason was an error rather than
        # an empty result set. "Nothing returned" and "quota exceeded" are not the same
        # thing and only one of them is worth re-running.
        self.last_errors: Dict[str, str] = {}
        # The title/author the second pass searched with, if there was one. See _refine.
        self.last_refined_with: Optional[Dict[str, str]] = None
        # (source, title, author) already recorded, so the second pass re-asking a
        # source cannot list the same book twice.
        self._seen: set = set()
        # One record per HTTP request of the last search: url, status, size, timing and
        # which pass made it. The demo script prints these rather than reimplementing the
        # request layer, which is how it came to miss the retries entirely.
        self.last_requests: List[Dict[str, Any]] = []
        # Set by the demo's --raw: keep each response body on its request record.
        self.keep_raw = False
        self._pass = 1
        # Sources that failed this search in a way no repeat request can fix.
        self._dead: set = set()
        # url -> the answer it gave during this search, so a repeat costs nothing.
        self._responses: Dict[str, Any] = {}

    # ------------------------------------------------------------------ public

    def search(self, hints: Dict[str, str], sources: Optional[List[str]] = None,
               force: bool = False) -> Optional[Dict[str, Any]]:
        """Best match across the configured sources, or None.

        `hints` may contain any of author / title / series / series_index, or - when
        the local tiers came up empty - a free-text `query` built from the file's own
        name. A keyword search on a messy filename still beats not searching at all.

        `sources` restricts the run to particular databases - that is what "Identify
        using > Google Books" passes. `force` skips the cache, because a run you asked
        for by name must actually make the request; replaying a cached miss looks
        identical to a source being broken.
        """
        self.last_rejected = None
        self.last_candidates = []
        self.last_by_source = {}
        self.last_errors = {}
        self.last_refined_with = None
        self.last_requests = []
        self._seen = set()
        self._pass = 1
        self._dead = set()
        self._responses = {}
        chosen = [s for s in (sources or self.sources) if s]
        self.last_sources = chosen
        title = (hints.get('title') or '').strip()
        author = (hints.get('author') or '').strip()
        query = (hints.get('query') or '').strip()
        if not title and not author and not query:
            return None

        cache_key = (f"{normalize(author)}|{normalize(title)}|{normalize(query)}"
                     f"|{'.'.join(chosen)}")
        self.last_from_cache = False
        if self.cache is not None and not force:
            cached = self.cache.get('booklookup', cache_key, default=_UNSET)
            if cached is not _UNSET:
                # A cached *miss* is why a search can report nothing without a single
                # request being made. Say so, or it looks like the sources were queried
                # and lied.
                self.last_from_cache = True
                if cached is not None:
                    self.logger.debug('Cache hit for %r', title)
                return cached

        best = self._collect(hints, chosen)
        best = self._refine(hints, chosen, best)

        if best is not None and not self._is_complete(best):
            # Fill the gaps from lower-scoring candidates rather than leaving them
            # empty: a series name from a source that got the title slightly wrong is
            # still the right series.
            best = self._merge_gaps(best, hints)

        if best is None or best['score'] < self.threshold:
            if best:
                self.last_rejected = {k: v for k, v in best.items() if k != 'raw'}
                self.logger.info('Best match for %r scored %.2f, below threshold %.2f',
                                 title or query, best['score'], self.threshold)
            if self.cache is not None:
                self.cache.set_miss('booklookup', cache_key)
            return None

        if self.cache is not None:
            self.cache.set('booklookup', cache_key, best)
        return best

    def _collect(self, score_hints: Dict[str, str], sources: List[str],
                 query_hints: Optional[Dict[str, str]] = None
                 ) -> Optional[Dict[str, Any]]:
        """Ask each source, score everything it says, and return the best candidate.

        `query_hints` is what the sources are *asked*; `score_hints` is what candidates
        are scored against. They differ on the second pass, where we search with a title
        a source gave us but still have to score against the file we actually have.
        """
        asked = query_hints or score_hints
        best: Optional[Dict[str, Any]] = None
        for source in sources:
            handler = getattr(self, f'_search_{source}', None)
            if handler is None:
                self.logger.warning('Unknown book source: %s', source)
                continue
            if source in self._dead:
                continue          # already refused this search; asking again is theatre
            try:
                candidates = handler(asked) or []
            except Exception as exc:
                self.logger.warning('%s lookup failed: %s', source, exc)
                self.last_errors.setdefault(source, str(exc))
                self.last_by_source.setdefault(source, [])
                continue

            self.last_by_source.setdefault(source, [])
            for candidate in candidates:
                # Series and position are part of the identity: cleaning the titles
                # makes "…, Book 7" and the full-cast edition of the same book identical
                # strings, and collapsing them loses the better series record.
                fingerprint = (source, normalize(candidate.get('title', '')),
                               normalize(candidate.get('author', '')),
                               normalize(candidate.get('series', '')),
                               str(candidate.get('series_index', '')))
                if fingerprint in self._seen:
                    continue
                self._seen.add(fingerprint)
                candidate['score'] = self._score(candidate, score_hints)
                trimmed = {k: v for k, v in candidate.items() if k != 'raw'}
                self.last_candidates.append(trimmed)
                self.last_by_source[source].append(trimmed)
                if best is None or candidate['score'] > best['score']:
                    best = candidate

            # A strong hit ends the search only if it is also a *complete* one.
            # Audnexus routinely returns a perfect title/author match with no series
            # at all, and stopping there threw away the series that Google Books or
            # Open Library would have supplied.
            if (not self.query_all and best and best['score'] >= 0.95
                    and self._is_complete(best)):
                break
        return best

    def _refine(self, hints: Dict[str, str], chosen: List[str],
                best: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Second pass: search again using the best title the first pass turned up.

        This is the difference between finding a book and not finding it. When all we
        have is a filename - "Dumb Luck and Dead Heroes 09 - The Worst Admiral in the
        Star Cluster" - that whole string goes to every source as keywords, and it is a
        query no catalogue is built to answer: Audible returns the series omnibus at 56%,
        Open Library returns nothing, and LibriVox is not called at all because it needs
        a title and we never had one.

        But one source usually *does* recognise the mess. Apple returned the right book
        at 88%. Feeding its title and author back to the others turns the same request
        into "The Worst Admiral in the Star Cluster / Skyler Ramirez", and Audible then
        answers with the exact book and its series position - #9, which nothing in the
        first pass knew. Without this, a book we have no metadata for could never be
        found, which is precisely the case where we need the databases most.

        Re-asked: every source when we had no title of our own (they were all working
        from a filename), otherwise only the ones that came back empty.
        """
        # Refining from a candidate nobody believes in is just a second guess at random.
        if best is None or not best.get('title') or best.get('score', 0) < 0.5:
            return best

        had_title = bool((hints.get('title') or '').strip())
        empty = [s for s in chosen if not self.last_by_source.get(s)]
        targets = list(chosen) if not had_title else empty
        if not targets:
            return best

        refined = {'title': _clean_title(best['title']),
                   'author': (best.get('author') or '').strip()}
        if not refined['title']:
            return best
        # If the refined query is the query we already sent, the second pass is a second
        # copy of the first one's requests for nothing.
        if normalize(_keywords(refined)) == normalize(_keywords(hints)):
            return best

        self.last_refined_with = dict(refined)
        self.logger.info('Refining the search with %r by %r (from %s at %.0f%%)',
                         refined['title'], refined['author'],
                         best.get('source'), best.get('score', 0) * 100)
        self._pass = 2
        second = self._collect(hints, targets, query_hints=refined)
        if second is not None and (best is None or second['score'] > best['score']):
            return second
        return best

    def _is_complete(self, candidate: Dict[str, Any]) -> bool:
        """Whether a candidate answers everything we ask a book database for."""
        if not candidate.get('title') or not candidate.get('author'):
            return False
        # A series without its position in that series is half an answer.
        if candidate.get('series') and not candidate.get('series_index'):
            return False
        if self.require_cover and not candidate.get('cover_url'):
            return False
        return True

    def _merge_gaps(self, best: Dict[str, Any],
                    hints: Dict[str, str]) -> Dict[str, Any]:
        """Fill empty fields on the winner from the other candidates, by consensus.

        Taking the highest-scoring donor is not good enough and produced a wrong answer
        that looked authoritative. Searching for "The Hobbit" wins with an Open Library
        record that carries a title and no author; the next-best row with an author was a
        film companion book credited to Jude Fisher, so the book came out as The Hobbit
        by Jude Fisher - while three other candidates, from three different sources, all
        said Tolkien.

        So the value the most sources agree on wins, and the best-scoring donor only
        breaks a tie. Agreement across independent catalogues is the strongest signal
        available here, and it costs nothing to count.
        """
        merged = dict(best)
        others = sorted((c for c in self.last_candidates if c is not best),
                        key=lambda c: -c.get('score', 0))
        for field in ('series', 'series_index', 'cover_url', 'author', 'title'):
            if merged.get(field):
                continue
            # Only candidates that are plausibly the same book get a vote.
            donors = [c for c in others if c.get(field) and similarity(
                merged.get('title', ''), c.get('title', '')) >= 0.6]
            if not donors:
                continue
            votes: Dict[str, Dict[str, Any]] = {}
            for donor in donors:
                key = _agreement_key(field, str(donor[field]))
                bucket = votes.setdefault(key, {'count': 0, 'donor': donor})
                bucket['count'] += 1        # donors are score-ordered, so the first
                                            # one kept per key is the best of its group
            # Most agreed-on first; the donor's own score breaks a tie.
            winner = max(votes.values(),
                         key=lambda v: (v['count'], v['donor'].get('score', 0)))
            merged[field] = winner['donor'][field]
            merged.setdefault('filled_from', {})[field] = (
                f"{winner['donor'].get('source')}"
                + (f" (agreed by {winner['count']})" if winner['count'] > 1 else ''))
        return merged

    def _score(self, candidate: Dict[str, Any], hints: Dict[str, str]) -> float:
        """How well a candidate matches what we already believe."""
        title_score = similarity(hints.get('title', ''), candidate.get('title', ''))
        author_score = author_similarity(hints.get('author', ''), candidate.get('author', ''))

        # Free-text mode: we have no parsed fields, only the file's own name. Score the
        # candidate against that whole string - "sanderson mistborn 01" overlaps
        # "Mistborn" by "Brandon Sanderson" strongly enough to rank correctly.
        if not hints.get('title') and not hints.get('author') and hints.get('query'):
            joined = ' '.join(filter(None, [candidate.get('title', ''),
                                            candidate.get('author', ''),
                                            candidate.get('series', '')]))
            # Scored against the *cleaned* query, the same string the sources were
            # asked. Demanding a candidate account for "01" or "{64kbps}" cost real
            # matches: The Final Empire missed the threshold by one junk token.
            score = _freetext_score(_clean_query(hints['query']), joined)
            if candidate.get('source') == 'audnexus':
                score = min(1.0, score + 0.03)
            return round(score * _derivative_penalty(candidate), 3)

        # Weight by which hints we actually have.
        if hints.get('title') and hints.get('author'):
            score = title_score * 0.65 + author_score * 0.35
        elif hints.get('title'):
            score = title_score
        else:
            score = author_score * 0.7  # author alone can't identify a book

        if hints.get('series') and candidate.get('series'):
            score = min(1.0, score + 0.1 * similarity(hints['series'], candidate['series']))
        # An audiobook-native source is more trustworthy for series data.
        if candidate.get('source') == 'audnexus':
            score = min(1.0, score + 0.03)
        return round(score * _derivative_penalty(candidate), 3)

    # ----------------------------------------------------------------- sources

    def _search_audnexus(self, hints: Dict[str, str]) -> List[Dict[str, Any]]:
        """Audnexus mirrors Audible's catalogue: the best series data available."""
        query = _keywords(hints)
        if not query:
            return []

        # Audnexus books are addressed by ASIN, so we search Audible's own suggestion
        # endpoint for the ASIN first, then enrich from Audnexus.
        results = []
        data = self._get_json(
            'audible',
            'https://api.audible.com/1.0/catalog/products'
            f'?keywords={quote_plus(query)}&num_results=5&products_sort_by=Relevance'
            '&response_groups=product_desc,series,contributors,product_attrs'
        )
        for product in (data or {}).get('products', []):
            series_name, series_index = '', ''
            series_list = product.get('series') or []
            if series_list:
                series_name = series_list[0].get('title', '')
                series_index = str(series_list[0].get('sequence', '') or '')
            authors = product.get('authors') or []
            results.append({
                # Audible spells the position into the title as well ("..., Book 7").
                # It is already in `series_index`, and leaving it in the title is what
                # got written to the folder name.
                'title': _clean_title(product.get('title', '')),
                'author': authors[0].get('name', '') if authors else '',
                'series': series_name,
                'series_index': series_index,
                'cover_url': (product.get('product_images') or {}).get('500', ''),
                'source': 'audnexus',
                'raw': {'asin': product.get('asin')},
            })
        return results

    def _search_googlebooks(self, hints: Dict[str, str]) -> List[Dict[str, Any]]:
        parts = []
        if hints.get('title'):
            parts.append(f'intitle:{hints["title"]}')
        if hints.get('author'):
            parts.append(f'inauthor:{hints["author"]}')
        if not parts:
            # No parsed fields - fall back to an unqualified keyword search.
            if not _keywords(hints):
                return []
            parts.append(_keywords(hints))

        data = self._get_json(
            'googlebooks',
            f'https://www.googleapis.com/books/v1/volumes?q={quote_plus(" ".join(parts))}'
            '&maxResults=10&printType=books'
            + (f'&key={quote_plus(self.google_key)}' if self.google_key else '')
        )
        results = []
        for item in (data or {}).get('items', []):
            info = item.get('volumeInfo', {})
            series, index = _series_from_text(info.get('subtitle', '') or info.get('title', ''))
            results.append({
                'title': _strip_series_suffix(info.get('title', '')),
                'author': (info.get('authors') or [''])[0],
                'series': series,
                'series_index': index,
                'cover_url': (info.get('imageLinks') or {}).get('thumbnail', ''),
                'source': 'googlebooks',
                'raw': {'id': item.get('id')},
            })
        return results

    def _search_openlibrary(self, hints: Dict[str, str]) -> List[Dict[str, Any]]:
        parts = []
        if hints.get('title'):
            parts.append(f'title:({hints["title"]})')
        if hints.get('author'):
            parts.append(f'author:({hints["author"]})')
        if not parts:
            if not _keywords(hints):
                return []
            parts.append(_keywords(hints))

        data = self._get_json(
            'openlibrary',
            f'https://openlibrary.org/search.json?q={quote_plus(" AND ".join(parts))}'
            '&fields=title,author_name,series,first_publish_year,cover_i,key&limit=10'
        )
        results = []
        for doc in (data or {}).get('docs', []):
            series_raw = doc.get('series') or []
            series_text = series_raw[0] if isinstance(series_raw, list) and series_raw else ''
            series, index = _series_from_text(str(series_text))
            cover_id = doc.get('cover_i')
            results.append({
                'title': doc.get('title', ''),
                'author': (doc.get('author_name') or [''])[0],
                'series': series or str(series_text),
                'series_index': index,
                'cover_url': (f'https://covers.openlibrary.org/b/id/{cover_id}-L.jpg'
                              if cover_id else ''),
                'source': 'openlibrary',
                'raw': {'key': doc.get('key')},
            })
        return results

    def _search_itunes(self, hints: Dict[str, str]) -> List[Dict[str, Any]]:
        """Apple's catalogue - a second audiobook-native source, no key required.

        Worth having alongside Audnexus: Apple carries plenty of titles Audible never
        licensed, and its `collectionName` usually spells the series out in full.
        """
        query = _keywords(hints)
        if not query:
            return []

        data = self._get_json(
            'itunes',
            f'https://itunes.apple.com/search?term={quote_plus(query)}'
            '&entity=audiobook&limit=10')
        results = []
        for item in (data or {}).get('results', []):
            name = item.get('collectionName', '') or item.get('trackName', '')
            title, series, index = _split_title_series(name)
            results.append({
                'title': title,
                'author': item.get('artistName', ''),
                'series': series,
                'series_index': index,
                'cover_url': item.get('artworkUrl100', '').replace('100x100', '600x600'),
                'source': 'itunes',
                'raw': {'id': item.get('collectionId')},
            })
        return results

    def _search_librivox(self, hints: Dict[str, str]) -> List[Dict[str, Any]]:
        """Public-domain audiobooks. Narrow, but authoritative for what it does have.

        Two things had to be right here or this source could never match anything.
        `?title=Alice` is an *exact* title match and answers HTTP 404; the API only
        does partial matching when the value is prefixed with `^`, so `?title=^Alice`
        is what finds "Alice's Adventures in Wonderland". And it accepts `author=`,
        so having no title is no longer a reason not to ask it at all.
        """
        title = _clean_title((hints.get('title') or '').strip())
        author = (hints.get('author') or '').strip()
        if title:
            field, value = 'title', f'^{title}'
        elif author:
            field, value = 'author', author.split()[-1]   # surname matches best
        else:
            return []

        data = self._get_json(
            'librivox',
            f'https://librivox.org/api/feed/audiobooks/?{field}={quote_plus(value)}'
            '&format=json&limit=10')
        results = []
        for book in (data or {}).get('books', []):
            authors = book.get('authors') or []
            name = ' '.join(filter(None, [authors[0].get('first_name', ''),
                                          authors[0].get('last_name', '')])) \
                if authors else ''
            results.append({
                'title': book.get('title', ''),
                'author': name,
                'series': '',
                'series_index': '',
                'cover_url': '',
                'source': 'librivox',
                'raw': {'id': book.get('id')},
            })
        return results

    # ---------------------------------------------------------------- plumbing

    def _get_json(self, source: str, url: str, attempts: int = 3) -> Optional[Dict]:
        """The request, retried on the failures that are worth retrying.

        A 429 or a 5xx is usually a moment's rate limiting and is worth one more try
        after a pause. Whatever is left over is recorded in `last_errors`, because a
        source that was throttled, unreachable or out of quota did not "return no rows",
        and telling you it did sends you looking for a better query that does not exist.

        LibriVox answers HTTP 404 when a search matches nothing, so that one really is
        an empty result rather than a failure.
        """
        import requests

        # The second pass often lands on a query one source has already been asked -
        # LibriVox only ever uses the title, so a refined author changes nothing for it.
        # An identical URL cannot produce a different answer within one search.
        if url in self._responses:
            self.last_requests.append({'source': source, 'url': url, 'pass': self._pass,
                                       'attempt': 1,
                                       'note': 'already asked this exact URL in this '
                                               'search - reusing the answer'})
            return self._responses[url]

        for attempt in range(1, attempts + 1):
            self._rate_limit(source)
            record: Dict[str, Any] = {'source': source, 'url': url,
                                      'pass': self._pass, 'attempt': attempt}
            self.last_requests.append(record)
            started = time.time()
            try:
                response = requests.get(url, timeout=self.timeout,
                                        headers={'User-Agent': USER_AGENT,
                                                 'Accept': 'application/json'})
            except requests.RequestException as exc:
                record.update(error=str(exc), elapsed=time.time() - started)
                self.logger.warning('%s request failed: %s', source, exc)
                if attempt == attempts:
                    self.last_errors[source] = f'could not be reached: {exc}'
                    return None
                time.sleep(0.4 * attempt)
                continue

            record.update(status=response.status_code, bytes=len(response.content),
                          elapsed=time.time() - started)
            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError:
                    record['error'] = 'the response body was not JSON'
                    self.last_errors[source] = 'answered with something that is not JSON'
                    return None
                if self.keep_raw:
                    record['raw'] = body
                self._responses[url] = body
                return body

            if source == 'librivox' and response.status_code == 404:
                record['note'] = 'no matches (LibriVox answers 404 for an empty search)'
                self._responses[url] = None
                return None

            record['error'] = _http_reason(source, response)
            # A momentary throttle is worth another go; a quota that resets tomorrow is
            # not, and neither is a refusal. Retrying those three times per pass just
            # made every search slower for an answer that cannot change.
            permanent = ('out of quota' in record['error']
                         or response.status_code in (401, 403))
            retryable = (not permanent
                         and (response.status_code >= 500
                              or response.status_code == 429))
            if permanent:
                self._dead.add(source)
            if retryable and attempt < attempts:
                record['note'] = 'retrying'
                time.sleep(0.6 * attempt)
                continue

            self.last_errors[source] = record['error']
            self._responses[url] = None
            self.logger.debug('%s returned HTTP %d', source, response.status_code)
            return None
        return None

    def _rate_limit(self, source: str) -> None:
        """Keep a polite gap between calls to the same host."""
        with self._lock:
            last = self._last_request.get(source, 0.0)
            wait = self.min_interval - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            self._last_request[source] = time.time()


class _Unset:
    def __repr__(self):
        return '<unset>'


_UNSET = _Unset()


def _agreement_key(field: str, value: str) -> str:
    """The form of a value used to decide whether two sources said the same thing.

    Author names are compared with their spacing removed, so "J.R.R. Tolkien" and
    "J. R. R. Tolkien" are one vote for one man rather than two for two.
    """
    key = normalize(value)
    if field == 'author':
        return key.replace(' ', '')
    return key


def _http_reason(source: str, response) -> str:
    """A sentence saying what the source actually refused to do."""
    code = response.status_code
    detail = ''
    try:
        error = (response.json() or {}).get('error')
        if isinstance(error, dict):
            detail = str(error.get('message') or '')
        elif error:
            detail = str(error)
    except Exception:
        detail = ''

    if code == 429:
        if source == 'googlebooks' and 'per day' in detail.lower():
            return ('out of quota: Google Books no longer serves anonymous callers '
                    '(the shared daily limit is zero). Set a Google Books API key in '
                    'Settings to use this source.')
        return f'rate-limited (HTTP 429){": " + detail if detail else ""}'
    if code in (401, 403):
        return f'refused the request (HTTP {code}){": " + detail if detail else ""}'
    return f'HTTP {code}{": " + detail if detail else ""}'


def _keywords(hints: Dict[str, str]) -> str:
    """The plain keyword string to send a search endpoint.

    Parsed fields when we have them, the raw filename-derived query when we don't.
    """
    parsed = ' '.join(filter(None, [hints.get('title'), hints.get('author')])).strip()
    return parsed or _clean_query(hints.get('query') or '')


# Rip decoration that is never part of a title and only ever costs us matches.
_RIP_NOISE = re.compile(
    r'\b(?:un)?abridged\b|\b(?:m4b|m4a|mp3|flac|aac|opus|ogg)\b|\b\d+\s?kbps\b'
    # A volume marker goes with its number: dropping "4" and leaving "Book" behind
    # turns "Book 4 - The Winds of War" into a search for "Book The Winds of War".
    r'|\b(?:cd|disc|disk|track|part|pt|book|bk|vol|volume|episode|ep)\s?\d{1,3}\b'
    r'|\bnarrated by\b|\bread by\b'
    r'|\baudiobook\b|\bretail\b', re.I)


def _clean_query(text: str) -> str:
    """Turn a filename into something a catalogue search can actually answer.

    A search endpoint treats every word as a term it would like to match, so the
    bookkeeping in a filename works directly against us. "Sanderson - Mistborn 01 - The
    Final Empire" returns the series omnibus, because nothing in Audible's catalogue is
    called "01"; drop that one token and the same request returns The Final Empire
    first. The number is not lost - the filename parser reads the series position from
    the name, and this only governs what we *search* with.

    A bare four-digit number is left alone - 1984 and Fahrenheit 451 are titles - but a
    parenthesised one is a publication year, which no catalogue indexes as part of a
    title and which costs us the match if we insist a candidate account for it.
    """
    if not text:
        return ''
    cleaned = re.sub(r'[\[\{<][^\]\}>]*[\]\}>]', ' ', str(text))   # [64kbps] {retail}
    cleaned = re.sub(r'\([^\)]*\)', ' ', cleaned)                  # (1937) (Unabridged)
    cleaned = _RIP_NOISE.sub(' ', cleaned)
    cleaned = re.sub(r'[_\.\-]+', ' ', cleaned)

    words = [w for w in cleaned.split() if w.strip()]
    # A bare 1-3 digit number is a volume index. Drop it only while enough real words
    # remain to search with, so a book genuinely called "11" is still searchable.
    keep = [w for w in words if not re.fullmatch(r'#?\d{1,3}', w)]
    if len([w for w in keep if len(w) > 1]) < 2:
        keep = words
    return ' '.join(keep).strip() or str(text).strip()


# Books *about* a book. They match its title almost perfectly and are never what a
# library of audiobooks wants; left unpenalised, "Harry Potter and the Deathly Hallows
# Ultimate Trivia Test" outscored the novel itself.
_DERIVATIVE = re.compile(
    r'\b(?:trivia|quiz(?:zes)?|summar(?:y|ies)|analysis|study guide|workbook'
    r'|companion|unofficial|sparknotes|cliffs?notes|conversation starters'
    r'|book club (?:questions|guide)|a guide to|discussion questions'
    r'|(?:un)?authorized guide)\b', re.I)


def _derivative_penalty(candidate: Dict[str, Any]) -> float:
    """A multiplier: 1.0 for a book, less for something written about one."""
    text = ' '.join(filter(None, [str(candidate.get('title') or ''),
                                  str(candidate.get('series') or '')]))
    return 0.72 if _DERIVATIVE.search(text) else 1.0


def _series_from_text(text: str) -> tuple:
    """Pull ("Mistborn", "1") out of strings like "Mistborn, Book 1"."""
    if not text:
        return '', ''
    patterns = (
        r'^(?P<series>.+?)[,;:]?\s*(?:book|bk|volume|vol|part|pt|#)\s*(?P<index>\d{1,3})\b',
        r'^(?P<series>.+?)\s*\(\s*(?:book|vol(?:ume)?)?\s*(?P<index>\d{1,3})\s*\)',
        r'^(?P<series>.+?)\s+(?P<index>\d{1,3})\s*$',
    )
    for pattern in patterns:
        match = re.match(pattern, text.strip(), re.I)
        if match:
            index = match.group('index')
            if len(index) == 4:  # a year
                continue
            return match.group('series').strip(' ,;:-'), index
    return text.strip(), ''


def _strip_series_suffix(title: str) -> str:
    """"The Final Empire (Mistborn, Book 1)" -> "The Final Empire"."""
    return re.sub(r'\s*\([^)]*(?:book|vol(?:ume)?|#)\s*\d+[^)]*\)\s*$', '', title,
                  flags=re.I).strip() or title


# "(Unabridged)", "[Dramatized Adaptation]" - edition noise that is not part of any
# title, and which wrecks a title used as the next query's search term.
_QUALIFIER = re.compile(
    r'\s*[\(\[](?:un)?abridged[^\)\]]*[\)\]]|\s*[\(\[][^\)\]]*(?:audiobook|audio '
    r'edition|dramatized|dramatised|narrated by|full-?cast)[^\)\]]*[\)\]]', re.I)

# A trailing position, with or without the series in front of it: ", Book 9",
# ": Dumb Luck and Dead Heroes, Book 9", " - Book 9".
_POSITION = re.compile(
    r'[,:;\-]?\s*(?:book|bk|volume|vol|part|pt|#)\s*(?P<index>\d{1,3})\s*$', re.I)


def _clean_title(title: str) -> str:
    """The book's own title, without the catalogue's decoration.

    Apple lists the book above as "The Worst Admiral in the Star Cluster: Dumb Luck and
    Dead Heroes, Book 9 (Unabridged)" and Audible as "..., Book 9". Both belong in
    `series` and `series_index`, and a title still carrying them is both wrong in the
    folder name and useless as the next source's search term.
    """
    return _split_title_series(title)[0]


def _split_title_series(name: str) -> tuple:
    """("The Worst Admiral in the Star Cluster", "Dumb Luck and Dead Heroes", "9").

    `_series_from_text` alone gets this shape wrong: it matches from the left, so it
    reads the whole "Title: Series" run as the series name. The position is what marks
    the end of the string, so it is stripped from the right first, and only then is the
    remainder split on its last colon into title and series.
    """
    if not name:
        return '', '', ''
    text = _QUALIFIER.sub('', str(name)).strip()
    text = _strip_series_suffix(text)

    index = ''
    match = _POSITION.search(text)
    if match:
        index = match.group('index')
        text = text[:match.start()].strip(' ,;:-')

    title, series = text, ''
    if index and ':' in text:
        # "Title: Series, Book 9" - the series is the part next to the position.
        head, _, tail = text.rpartition(':')
        if head.strip() and tail.strip():
            title, series = head.strip(), tail.strip()
    if not index:
        # No position, so nothing tells us a colon separates a series from a title.
        # "Harry Potter and the Philosopher's Stone" must not become a series.
        return title.strip(), '', ''
    return title.strip(), series, index
