"""Tier 4: web search + light scraping, for books no database knows about.

Two complementary strategies:

1. **Structured scrape** - fetch Goodreads/Audible/StoryGraph result pages directly and
   pull metadata out of their JSON-LD blocks. Precise when it works.
2. **Search snippets** - DuckDuckGo results, whose titles are often literally
   "Title (Series, #3) by Author". Broad, noisier, but catches the long tail.

Everything here is best-effort: any failure returns nothing and the resolver moves on.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urljoin

from .models import normalize

logger = logging.getLogger(__name__)

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/122.0 Safari/537.36')

# "Title (Series, #3) by Author Name" - the shape Goodreads uses in page titles and
# what search engines therefore echo back in snippets.
_GOODREADS_TITLE = re.compile(
    r'^(?P<title>.+?)\s*\((?P<series>[^,)]+?),?\s*#(?P<index>\d+(?:\.\d+)?)\)'
    r'(?:\s*by\s+(?P<author>.+?))?$', re.I)
_BY_AUTHOR = re.compile(r'^(?P<title>.+?)\s+by\s+(?P<author>[A-Z][^|·—-]{2,60})$', re.I)
_SERIES_HASH = re.compile(r'\((?P<series>[^,)]+?),?\s*#(?P<index>\d+(?:\.\d+)?)\)')


class WebSearchClient:
    """Best-effort metadata recovery from the open web."""

    def __init__(self, cache=None, timeout: int = 20, scrape: bool = True):
        self.cache = cache
        self.timeout = timeout
        self.scrape = scrape
        self.logger = logging.getLogger(__name__)
        self._last_request = 0.0
        self.min_interval = 1.0  # be polite; these are not APIs
        # What the last call actually did, so a failure can be reported precisely
        # rather than as "found nothing usable".
        self.last_query = ''
        self.last_sites: List[str] = []
        # Raw result titles/snippets from the last run, kept whether or not anything
        # could be parsed out of them. A snippet reading "The Deep Sky by Yume Kitasei"
        # is obviously useful even when no pattern matched it - so it gets shown, and
        # handed to the LLM as context.
        self.last_results: List[Dict[str, str]] = []
        self.last_from_cache = False
        self.last_error = ''

    def search(self, hints: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Return recovered fields, or None."""
        self.last_sites = []
        self.last_results = []
        self.last_from_cache = False
        subject = _subject(hints)
        self.last_query = query = (f'{subject} audiobook series' if subject else '')
        if not subject:
            return None

        cache_key = normalize(query)
        if self.cache is not None:
            cached = self.cache.get('websearch', cache_key, default=_UNSET)
            if cached is not _UNSET:
                self.last_from_cache = True
                return cached

        result: Dict[str, Any] = {}
        for strategy in (self._from_goodreads, self._from_duckduckgo):
            if not self.scrape and strategy is self._from_goodreads:
                continue
            self.last_sites.append(
                'goodreads' if strategy is self._from_goodreads else 'duckduckgo')
            try:
                found = strategy(hints)
            except Exception as exc:
                self.logger.debug('%s failed: %s', strategy.__name__, exc)
                continue
            if found:
                # First strategy to supply a field wins; later ones only fill gaps.
                for key, value in found.items():
                    result.setdefault(key, value)
                if result.get('series') and result.get('author'):
                    break

        result = {k: v for k, v in result.items() if v}
        if not result:
            if self.cache is not None:
                self.cache.set_miss('websearch', cache_key)
            return None

        result['source'] = 'search'
        if self.cache is not None:
            self.cache.set('websearch', cache_key, result)
        return result

    # ------------------------------------------------------------- strategies

    def _from_goodreads(self, hints: Dict[str, str]) -> Dict[str, str]:
        """Scrape the first Goodreads search hit, which carries clean series data."""
        query = _subject(hints)
        if not query:
            return {}

        html = self._get_text(
            f'https://www.goodreads.com/search?q={quote_plus(query)}')
        if not html:
            return {}

        match = re.search(r'href="(/book/show/[^"?]+)', html)
        if not match:
            return {}

        book_html = self._get_text(urljoin('https://www.goodreads.com', match.group(1)))
        if not book_html:
            return {}
        return self._parse_book_page(book_html)

    def _parse_book_page(self, html: str) -> Dict[str, str]:
        """Pull fields out of a book page's JSON-LD, falling back to its <title>."""
        result: Dict[str, str] = {}

        for blob in re.findall(
                r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                html, re.S | re.I):
            try:
                data = json.loads(blob.strip())
            except ValueError:
                continue
            for node in (data if isinstance(data, list) else [data]):
                if not isinstance(node, dict):
                    continue
                if node.get('@type') in ('Book', 'Audiobook', 'Product'):
                    if node.get('name'):
                        result.setdefault('title', str(node['name']))
                    author = node.get('author')
                    if isinstance(author, list) and author:
                        author = author[0]
                    if isinstance(author, dict) and author.get('name'):
                        result.setdefault('author', str(author['name']))
                    elif isinstance(author, str):
                        result.setdefault('author', author)

        title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.S | re.I)
        if title_match:
            parsed = self._parse_result_title(_unescape(title_match.group(1)))
            for key, value in parsed.items():
                result.setdefault(key, value)

        # Goodreads renders the series as a link to /series/<id>-<name>
        if 'series' not in result:
            series_match = re.search(r'/series/\d+[-_]([A-Za-z0-9_]+)', html)
            if series_match:
                result['series'] = series_match.group(1).replace('_', ' ').strip()

        return result

    def _from_duckduckgo(self, hints: Dict[str, str]) -> Dict[str, str]:
        """Mine search-result titles, which frequently embed the series and index."""
        results = self._ddg(f'{_subject(hints)} goodreads series')
        self.last_results.extend(results[:8])
        merged: Dict[str, str] = {}

        wanted_title = normalize(hints.get('title', ''))
        for item in results[:8]:
            text = f"{item.get('title', '')} {item.get('body', '')}"
            parsed = self._parse_result_title(item.get('title', ''))
            if not parsed:
                parsed = {}
            # Only trust a result whose title actually resembles the book we asked about.
            if wanted_title and parsed.get('title'):
                if normalize(parsed['title']) not in wanted_title and \
                        wanted_title not in normalize(parsed['title']):
                    # Still allow it to contribute a series if the snippet names ours.
                    hash_match = _SERIES_HASH.search(text)
                    if hash_match and wanted_title in normalize(text):
                        merged.setdefault('series', hash_match.group('series').strip())
                        merged.setdefault('series_index', hash_match.group('index'))
                    continue
            for key, value in parsed.items():
                merged.setdefault(key, value)
            if merged.get('series') and merged.get('author'):
                break
        return merged

    def _ddg(self, query: str) -> List[Dict[str, str]]:
        """DuckDuckGo results via the library if present, else the HTML endpoint.

        Both routes get blocked or rate-limited in the real world. When that happens
        the caller must be able to say *"the engine refused"* rather than *"there are
        no results"* - they look identical from here, and only one of them is our
        fault.
        """
        self.last_error = ''
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=8)) or []
            if results:
                return results
            self.last_error = 'duckduckgo_search returned no rows'
        except ImportError:
            self.last_error = 'duckduckgo-search is not installed'
        except Exception as exc:
            self.last_error = f'duckduckgo_search failed: {exc}'
            self.logger.debug('duckduckgo_search failed (%s), falling back to HTML', exc)

        html = self._get_text(f'https://html.duckduckgo.com/html/?q={quote_plus(query)}')
        if not html:
            self.last_error = (f'{self.last_error}; the HTML endpoint returned nothing '
                               f'(blocked or rate-limited)').lstrip('; ')
            return []
        results = []
        for match in re.finditer(
                r'class="result__a"[^>]*>(?P<title>.*?)</a>.*?'
                r'(?:class="result__snippet"[^>]*>(?P<body>.*?)</a>)?',
                html, re.S):
            results.append({
                'title': _unescape(_strip_tags(match.group('title') or '')),
                'body': _unescape(_strip_tags(match.group('body') or '')),
            })
            if len(results) >= 8:
                break
        return results

    @staticmethod
    def _parse_result_title(text: str) -> Dict[str, str]:
        """Parse "Title (Series, #3) by Author" and friends."""
        text = re.sub(r'\s*[|·]\s*(?:Goodreads|Audible|Amazon|StoryGraph).*$', '',
                      text or '', flags=re.I).strip()
        if not text:
            return {}

        match = _GOODREADS_TITLE.match(text)
        if match:
            found = {
                'title': match.group('title').strip(),
                'series': match.group('series').strip(),
                'series_index': match.group('index'),
            }
            if match.group('author'):
                found['author'] = match.group('author').strip()
            return found

        found = {}
        hash_match = _SERIES_HASH.search(text)
        if hash_match:
            found['series'] = hash_match.group('series').strip()
            found['series_index'] = hash_match.group('index')
            text = _SERIES_HASH.sub('', text).strip()

        by_match = _BY_AUTHOR.match(text)
        if by_match:
            found['title'] = by_match.group('title').strip()
            found['author'] = by_match.group('author').strip()
        elif found:
            found['title'] = text.strip()
        return found

    # --------------------------------------------------------------- plumbing

    def _get_text(self, url: str) -> str:
        import requests

        wait = self.min_interval - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

        try:
            response = requests.get(
                url, timeout=self.timeout,
                headers={'User-Agent': USER_AGENT,
                         'Accept': 'text/html,application/xhtml+xml',
                         'Accept-Language': 'en-US,en;q=0.9'})
        except requests.RequestException as exc:
            self.logger.debug('Fetch failed for %s: %s', url, exc)
            return ''
        if response.status_code != 200:
            self.logger.debug('Fetch returned HTTP %d for %s', response.status_code, url)
            return ''
        return response.text


class _Unset:
    def __repr__(self):
        return '<unset>'


_UNSET = _Unset()


def _subject(hints: Dict[str, str]) -> str:
    """What we are actually searching for: parsed fields, else the raw file name."""
    parsed = ' '.join(filter(None, [hints.get('title', ''),
                                    hints.get('author', '')])).strip()
    return parsed or (hints.get('query') or '').strip()


def _strip_tags(html: str) -> str:
    return re.sub(r'<[^>]+>', '', html or '')


def _unescape(text: str) -> str:
    import html as html_module
    return html_module.unescape(text or '').strip()
