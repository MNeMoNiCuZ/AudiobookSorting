"""The resolution chain: metadata -> regex -> API -> web search -> LLM.

This is the piece the old code documented but never wired up. Each tier contributes
what it can; :meth:`BookEntry.set_field` decides whether a value is good enough to
overwrite what an earlier tier found, and records agreement between tiers as raised
confidence. Every step is written to the entry's trace, which is what the "why" panel
displays.

Tiers stop early: if metadata and regex already agree on all four fields at high
confidence, no network call is made at all.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .models import (STATUS_PENDING, STATUS_RISKY, BookEntry, normalize)

logger = logging.getLogger(__name__)

CancelCheck = Callable[[], bool]

# The chain, in order. The index into this list is the "tier number" the settings talk
# about: tier 1 is tags, tier 3 is the book databases, tier 5 is the model.
TIER_ORDER = ['metadata', 'regex', 'api', 'search', 'llm']


class Resolver:
    """Runs the tier chain over entries."""

    def __init__(self, settings, cache=None, metadata_extractor=None,
                 api_client=None, search_client=None, llm_client=None):
        self.settings = settings
        self.cache = cache
        self.logger = logging.getLogger(__name__)

        self.enable_metadata = settings.get_bool('AO_ENABLE_METADATA', True)
        self.enable_regex = settings.get_bool('AO_ENABLE_REGEX', True)
        self.enable_api = settings.get_bool('AO_ENABLE_API', True)
        self.enable_search = settings.get_bool('AO_ENABLE_SEARCH', False)
        self.enable_llm = settings.get_bool('AO_ENABLE_LLM', True)
        self.folder_reasoning = settings.get_bool('AO_FOLDER_REASONING', True)
        # Run at least this many tiers whatever the confidence. Tags and a filename
        # agreeing is not proof - they are both just the name someone gave the file -
        # so the default keeps going as far as the book databases.
        self.always_to_tier = settings.get_int('AO_ALWAYS_SEARCH_TO_TIER', 3)
        self.input_root = str(settings.get_path('AO_INPUT_DIR'))

        self._metadata = metadata_extractor
        self._api = api_client
        self._search = search_client
        self._llm = llm_client
        self._llm_failed = False

    # ----------------------------------------------------------- lazy clients

    @property
    def metadata(self):
        if self._metadata is None:
            from .metadata_extractor import MetadataExtractor
            self._metadata = MetadataExtractor()
        return self._metadata

    @property
    def api(self):
        if self._api is None:
            from .api_query import BookAPIClient
            self._api = BookAPIClient(
                cache=self.cache,
                sources=self.settings.get_list('AO_API_SOURCES'),
                threshold=self.settings.get_float('AO_CONFIDENCE_SCORE', 0.80),
                timeout=self.settings.get_int('AO_TIMEOUT', 20),
                require_cover=self.settings.get_bool('AO_REQUIRE_COVER', False),
                google_key=self.settings.get('AO_GOOGLE_BOOKS_KEY', ''),
                # A manual run queries every configured source. Stopping at the first
                # good-enough hit is a bandwidth optimisation, and you did not press
                # the button to save bandwidth.
                query_all=True)
        return self._api

    @property
    def search(self):
        if self._search is None:
            from .web_search import WebSearchClient
            self._search = WebSearchClient(
                cache=self.cache, timeout=self.settings.get_int('AO_TIMEOUT', 20),
                settings=self.settings)
        return self._search

    @property
    def llm(self):
        if self._llm is None:
            from .llm_query import LLMQueryClient
            self._llm = LLMQueryClient(settings=self.settings)
        return self._llm

    # -------------------------------------------------------------- entry API

    @staticmethod
    def _split_tiers(tiers: Optional[List[str]]):
        """Separate a requested tier list into tier names and API source overrides.

        "Identify using > Google Books" asks for one particular database, not for the
        whole book-database tier, so it passes ``api:googlebooks``. Everything else is
        a plain tier name. Returns ``(names, api_sources)`` where an empty
        ``api_sources`` means "whatever is configured".
        """
        if tiers is None:
            return None, []
        names, api_sources = [], []
        for raw in tiers:
            name, _, source = str(raw).partition(':')
            if name not in names:
                names.append(name)
            if source:
                api_sources.append(source)
        return names, api_sources

    def resolve(self, entry: BookEntry, tiers: Optional[List[str]] = None,
                should_cancel: Optional[CancelCheck] = None) -> BookEntry:
        """Run the chain over one entry, in place. Returns the same entry."""
        tiers, api_sources = self._split_tiers(tiers)
        steps = [
            ('metadata', self.enable_metadata, self._tier_metadata),
            ('regex', self.enable_regex, self._tier_regex),
            ('api', self.enable_api,
             lambda e: self._tier_api(e, sources=api_sources,
                                      forced=tiers is not None)),
            ('search', self.enable_search, self._tier_search),
            ('llm', self.enable_llm, self._tier_llm),
        ]

        for name, enabled, handler in steps:
            if should_cancel and should_cancel():
                entry.log('cancelled', f'Stopped before the {name} tier')
                return entry
            if tiers is not None and name not in tiers:
                continue
            if tiers is None and not enabled:
                continue
            # This tier is about to speak, so retire what it said on the last run -
            # otherwise re-running appends a duplicate of the same paragraph.
            entry.begin_tier(name)
            # Skipping is an optimisation for the *automatic* chain, never an answer to
            # someone who pressed the button. `tiers` is only ever set by an explicit
            # request, and an explicit request always runs - refusing to do the thing
            # the program is for, because we already have a partial answer, is absurd.
            tier_number = TIER_ORDER.index(name) + 1
            if (tiers is None and name in ('api', 'search', 'llm')
                    and tier_number > self.always_to_tier
                    and self._is_satisfied(entry)):
                entry.log(name, f'Skipped automatically: already resolved at high '
                                f'confidence, and tier {tier_number} is past the '
                                f'"always search to tier {self.always_to_tier}" '
                                f'setting. Run this source from the panel to force it.')
                continue
            try:
                handler(entry)
            except Exception as exc:
                self.logger.exception('%s tier failed for %s', name, entry.entry_id)
                entry.log(name, f'Failed: {exc}')

        entry.resolved = True
        self._finalise(entry)
        return entry

    def resolve_folder(self, entries: List[BookEntry], tiers: Optional[List[str]] = None,
                       should_cancel: Optional[CancelCheck] = None) -> List[BookEntry]:
        """Resolve every entry in one folder, sharing what we learn between them (#11).

        `tiers` overrides the enabled-tier settings for this run, which is what the
        mode checkboxes on the main window pass in.
        """
        if not entries:
            return entries

        tiers, api_sources = self._split_tiers(tiers)

        def wanted(name: str, enabled: bool) -> bool:
            return name in tiers if tiers is not None else enabled

        # Local tiers first, per entry - they're free and inform the shared step.
        for entry in entries:
            if should_cancel and should_cancel():
                return entries
            if wanted('metadata', self.enable_metadata):
                entry.begin_tier('metadata')
                self._tier_metadata(entry)
            if wanted('regex', self.enable_regex):
                entry.begin_tier('regex')
                self._tier_regex(entry)

        self._share_within_folder(entries)

        # One LLM call for the whole folder, when there's still something missing -
        # or whenever the user asked for the LLM specifically.
        needs_help = [e for e in entries
                      if tiers is not None or not self._is_satisfied(e)]
        if (needs_help and wanted('llm', self.enable_llm) and self.folder_reasoning
                and len(entries) > 1 and not self._llm_failed):
            if not (should_cancel and should_cancel()):
                self._tier_llm_folder(entries)
                self._share_within_folder(entries)

        # Then per-entry network tiers. An explicit request (`tiers` given) runs them
        # whatever we already know; only the automatic chain is allowed to skip.
        forced = tiers is not None
        for entry in entries:
            if should_cancel and should_cancel():
                return entries
            if forced or not self._is_satisfied(entry):
                if wanted('api', self.enable_api):
                    entry.begin_tier('api')
                    self._tier_api(entry, sources=api_sources, forced=forced)
                if wanted('search', self.enable_search) and (
                        forced or not self._is_satisfied(entry)):
                    entry.begin_tier('search')
                    self._tier_search(entry)
                if (wanted('llm', self.enable_llm)
                        and (forced or not self._is_satisfied(entry))
                        and not self.folder_reasoning):
                    entry.begin_tier('llm')
                    self._tier_llm(entry)
            entry.resolved = True
            self._finalise(entry)

        self._share_within_folder(entries)
        return entries

    # ----------------------------------------------------------------- tiers

    def _tier_metadata(self, entry: BookEntry) -> None:
        raw = self.metadata.read_raw_tags(entry.primary_audio)
        if raw:
            entry.raw_tags = raw
        found = self.metadata.extract(entry.primary_audio)
        if not found:
            # Say which of the two cases this is - an untagged file and a file whose
            # tags we couldn't map are very different problems for the user.
            if raw:
                entry.log('metadata',
                          f'{Path(entry.primary_audio).name} has {len(raw)} tag(s), none '
                          f'naming a title/author/series: {", ".join(sorted(raw))}',
                          {'tags': dict(raw)})
            else:
                entry.log('metadata',
                          f'{Path(entry.primary_audio).name} carries no tags at all')
            return
        changed = [name for name in ('author', 'title', 'series', 'series_index')
                   if name in found and entry.set_field(name, found[name], 'metadata')]
        entry.log('metadata',
                  'Read from the tags embedded in '
                  + Path(entry.primary_audio).name + ':\n'
                  + '\n'.join(f'  {name}: "{value}"' for name, value in found.items()),
                  {'applied': changed, 'result': dict(found)})

    def _tier_regex(self, entry: BookEntry) -> None:
        """Parse the *whole* path below the input root, not just the filename.

        The author is very often only in a grandparent folder and the series only in a
        parent, so every component from the library root down is a candidate. What each
        component contributed is recorded, because "parsed from the path" is useless
        when you cannot see which part of the path said what.
        """
        from .regex_parser import parse_path

        relative = self._relative_path(entry)
        found = parse_path(entry.primary_audio, self.input_root)
        pattern = found.pop('_pattern', '')
        considered = found.pop('_considered', [])
        contributions = found.pop('_from', {})
        if not found:
            entry.log('regex',
                      f'Nothing parseable in "{relative}". Every component was '
                      f'examined ({" / ".join(considered) or "none"}) and none of them '
                      f'has an author/series/title structure. The path is still used '
                      f'as a search query by the tiers below.',
                      {'path': relative, 'considered': considered})
            return
        changed = [name for name in ('author', 'title', 'series', 'series_index')
                   if name in found and entry.set_field(name, found[name], 'regex')]
        detail = '\n'.join(
            f'  {name}: "{value}"'
            + (f'   (from "{contributions[name]}")' if contributions.get(name) else '')
            for name, value in found.items())
        entry.log('regex',
                  f'Parsed "{relative}" as {pattern or "a bare title"}:\n{detail}',
                  {'applied': changed, 'path': relative, 'considered': considered,
                   'from': contributions, 'result': dict(found)})

    def _tier_api(self, entry: BookEntry, sources: Optional[List[str]] = None,
                  forced: bool = False) -> None:
        """Query the book databases and report what every one of them said.

        A manual run always queries every configured database and always goes to the
        network. Reporting only the winner - and only the first database that produced
        one - made a five-source tier look like a one-source tier, which is exactly the
        complaint: you pressed "Book databases" and were told about audnexus.
        """
        hints = self._hints(entry)
        asked = hints.get('title') or hints.get('author') or hints.get('query')
        if not asked:
            entry.log('api', f'Nothing to search with: neither the tags nor the path '
                             f'yielded a title, an author, or even a usable filename')
            return

        result = self.api.search(hints, sources=sources or None, force=forced)
        ran = list(getattr(self.api, 'last_sources', []) or self.api.sources)
        sources_text = ', '.join(ran)
        candidates = list(getattr(self.api, 'last_candidates', []))
        by_source = dict(getattr(self.api, 'last_by_source', {}))
        # Keep everything the sources said, match or not - the LLM tier reads this.
        if candidates:
            entry.evidence['api'] = candidates[:10]

        # Every database gets its own section in the panel, built from `by_source`.
        # The message stays a single sentence: cramming five databases' rows into one
        # paragraph of a trace line is what made this unreadable. The structure lives
        # in the data, and the "why" panel draws it as one collapsible box per source.
        common = {'by_source': by_source, 'sources': ran, 'query': asked,
                  'threshold': self.api.threshold, 'forced': forced,
                  # Why a source produced nothing, when the reason was not "no rows" -
                  # a throttled or out-of-quota source is a different problem from a
                  # query that genuinely matches nothing, and only one is worth retrying.
                  'errors': dict(getattr(self.api, 'last_errors', {}) or {}),
                  # The title a second pass searched with, when the first pass had none.
                  'refined_with': getattr(self.api, 'last_refined_with', None)}

        if not result:
            if getattr(self.api, 'last_from_cache', False):
                entry.log('api', f'Searched {sources_text} for {asked!r} - no match. '
                                 f'This answer came from the cache, so nothing was '
                                 f'queried just now. Run this source from the panel to '
                                 f'force a fresh lookup.',
                          dict(common, from_cache=True))
                return
            if not candidates:
                failed = common['errors']
                blame = ('; '.join(f'{name} {why}' for name, why in failed.items())
                         if failed else '')
                entry.log('api',
                          f'Asked {len(ran)} database(s) for {asked!r}: every one of '
                          f'them returned zero rows.'
                          + (f' {len(failed)} of them did not actually answer - {blame}.'
                             if blame else
                             ' The query matched nothing anywhere, so it is probably '
                             'not a title any catalogue carries.'),
                          dict(common))
                return
            best = getattr(self.api, 'last_rejected', None)
            entry.log('api',
                      f'Asked {len(ran)} database(s) for {asked!r} and got '
                      f'{len(candidates)} candidate(s), but none reached the '
                      f'{self.api.threshold:.0%} threshold, so nothing was used.',
                      dict(common, rejected=best, candidates=candidates[:10]))
            return

        source = result.get('source', 'api')
        changed, held = self._apply_fields(entry, result, source)
        if result.get('cover_url'):
            entry.raw_tags.setdefault('_cover_url', result['cover_url'])
        summary = (f'{source} matched "{result.get("title", "")}" by '
                   f'"{result.get("author", "")}" (score {result.get("score", 0):.2f}) '
                   f'out of {len(ran)} database(s) asked.')
        refined = common.get('refined_with') or {}
        if refined:
            summary += (f'\nAsked again using "{refined.get("title", "")}" by '
                        f'"{refined.get("author", "")}" - the first pass only had the '
                        f'filename to search with.')
        filled = result.get('filled_from') or {}
        if filled:
            summary += ('\nFilled the gaps it left from '
                        + ', '.join(f'{field} via {where}'
                                    for field, where in filled.items()))
        if held:
            summary += '\nKept the existing value for ' + '; '.join(held)
        entry.log('api', summary,
                  dict(common, applied=changed, held=held,
                       candidates=candidates[:10], winner=source,
                       result={k: v for k, v in result.items() if k != 'raw'}))

    @staticmethod
    def _api_breakdown(by_source: Dict[str, List[Dict]], ran: List[str]) -> str:
        """Each database that was asked, and its top rows, as readable text."""
        if not ran:
            return ''
        blocks = []
        for source in ran:
            rows = sorted(by_source.get(source) or [],
                          key=lambda c: -c.get('score', 0))
            if not rows:
                blocks.append(f'{source}: nothing returned')
                continue
            listing = '\n'.join(
                f'    {row.get("score", 0):.2f}  {row.get("title") or "?"} - '
                f'{row.get("author") or "?"}'
                + (f' ({row.get("series")} #{row.get("series_index")})'
                   if row.get('series') else '')
                for row in rows[:5])
            more = (f'\n    ...and {len(rows) - 5} more' if len(rows) > 5 else '')
            blocks.append(f'{source}: {len(rows)} result(s)\n{listing}{more}')
        return '\n'.join(blocks)

    def _tier_search(self, entry: BookEntry) -> None:
        hints = self._hints(entry)
        result = self.search.search(hints)
        raw = list(getattr(self.search, 'last_results', []))
        if raw:
            # Snippets are the single richest context the LLM tier gets: they routinely
            # spell out "Title by Author" even when no pattern here could parse them.
            entry.evidence['search'] = raw[:8]

        if not result:
            asked = getattr(self.search, 'last_query', '') or '(nothing)'
            tried = ', '.join(dict.fromkeys(getattr(self.search, 'last_sites', [])))
            error = getattr(self.search, 'last_error', '')
            if getattr(self.search, 'last_from_cache', False):
                entry.log('search', f'Web search for {asked!r} - cached "no result" from '
                                    f'an earlier run; nothing was fetched just now.')
                return
            if not raw:
                entry.log('search',
                          f'Web search for {asked!r} via {tried or "no engine"} came back '
                          f'completely empty. {error or "No reason reported."} This is a '
                          f'search-engine problem, not an absence of the book.',
                          {'error': error})
                return
            listing = '\n'.join(f'  - {item.get("title", "")}\n    {item.get("body", "")}'
                                for item in raw[:8])
            entry.log('search',
                      f'Web search for {asked!r} via {tried} returned {len(raw)} result(s), '
                      f'none in a shape this tier could parse. They are kept as context '
                      f'for the model:\n{listing}',
                      {'results': raw[:8]})
            return
        changed, held = self._apply_fields(entry, result, 'search')
        summary = ('Web search found ' +
                   ', '.join(f'{k}="{v}"' for k, v in result.items()
                             if k in ('title', 'author', 'series', 'series_index') and v))
        if held:
            summary += '\nKept the existing value for ' + '; '.join(held)
        entry.log('search', summary,
                  {'applied': changed, 'held': held, 'results': raw[:8]})

    def _tier_llm(self, entry: BookEntry) -> None:
        if self._llm_failed:
            entry.log('llm', 'Skipped: the LLM provider is unreachable')
            return

        hints = self._hints(entry)
        hints['path'] = self._relative_path(entry)
        context = self._folder_context(entry)

        try:
            result = self.llm.query_book(hints, context, evidence=entry.evidence)
        except Exception as exc:
            self._llm_failed = True
            entry.log('llm', f'Provider unavailable: {exc}', self._exchange())
            return
        if not result:
            entry.log('llm', 'No usable answer from the model', self._exchange())
            return

        confidence = result.get('confidence', 0.6)
        changed, held = self._apply_fields(entry, result, 'llm', confidence)
        summary = result.get('reasoning') or 'Model answered'
        if held:
            # The model *did* answer; the answer just lost to a better-sourced value.
            # Silently showing nothing here is what made this look broken.
            summary += ('\nKept the existing value for ' + '; '.join(held))
        entry.log('llm', summary,
                  self._exchange({'applied': changed, 'held': held,
                                  'confidence': confidence, 'result': result}))

    def _tier_llm_folder(self, entries: List[BookEntry]) -> None:
        folder_name = Path(entries[0].folder).name
        books = []
        for entry in entries:
            books.append({
                'file': Path(entry.primary_audio).name,
                'title': entry.value('title'),
                'author': entry.value('author'),
                'series': entry.value('series'),
                'series_index': entry.value('series_index'),
            })

        merged_evidence: Dict[str, Any] = {}
        for entry in entries:
            for tier, items in (entry.evidence or {}).items():
                merged_evidence.setdefault(tier, []).extend(items)

        for entry in entries:
            entry.begin_tier('llm')

        try:
            result = self.llm.query_folder(folder_name, books,
                                           evidence=merged_evidence or None)
        except Exception as exc:
            self._llm_failed = True
            for entry in entries:
                entry.log('llm', f'Provider unavailable: {exc}', self._exchange())
            return
        if not result:
            for entry in entries:
                entry.log('llm', 'No usable answer from the model', self._exchange())
            return

        by_file = {Path(entry.primary_audio).name: entry for entry in entries}
        shared_series = result.get('series', '')
        shared_author = result.get('author', '')
        reasoning = result.get('reasoning', '')

        for item in result.get('books', []):
            entry = by_file.get(item.get('file', ''))
            if entry is None:
                # Models sometimes lightly reword the filename; match loosely.
                entry = next((e for name, e in by_file.items()
                              if normalize(name) == normalize(item.get('file', ''))), None)
            if entry is None:
                continue
            confidence = item.get('confidence', 0.6)
            offered = dict(item)
            offered.setdefault('series', shared_series)
            offered.setdefault('author', shared_author)
            if not offered.get('series'):
                offered['series'] = shared_series
            if not offered.get('author'):
                offered['author'] = shared_author

            changed, held = self._apply_fields(entry, offered, 'llm', confidence)
            summary = reasoning or 'Identified as part of a folder-wide analysis'
            if held:
                summary += '\nKept the existing value for ' + '; '.join(held)
            entry.log('llm', summary,
                      self._exchange({'applied': changed, 'held': held,
                                      'confidence': confidence, 'result': item,
                                      'folder_series': shared_series}))

    # --------------------------------------------------------------- helpers

    def _apply_fields(self, entry: BookEntry, values: Dict[str, Any], source: str,
                      confidence: Optional[float] = None):
        """Write what a tier found, and report what it was *not* allowed to write.

        Returns ``(applied, held)``. `held` explains, per field, why an offered value
        lost - a tier producing an answer that vanishes without explanation is the
        single most confusing thing this program can do.
        """
        applied, held = [], []
        for name in ('author', 'title', 'series', 'series_index'):
            offered = values.get(name)
            if not offered:
                continue
            before = entry.get_field(name)
            if entry.set_field(name, offered, source, confidence):
                applied.append(name)
            elif normalize(str(offered)) != normalize(before.value):
                held.append(f'{name}: "{before.value}" ({before.source} '
                            f'{before.confidence:.0%}) beat "{offered}" '
                            f'({source} {(confidence if confidence is not None else 0):.0%})')
        return applied, held

    def _exchange(self, data: Optional[Dict] = None) -> Dict:
        """Attach the exact prompt/response of the last LLM call to a trace step.

        The "why" cards show this verbatim, so a bad identification can be read
        rather than guessed at.
        """
        step = dict(data or {})
        exchange = getattr(self._llm, 'last_exchange', None)
        if exchange:
            step['exchange'] = dict(exchange)
        return step

    def _hints(self, entry: BookEntry) -> Dict[str, str]:
        hints = {
            'title': entry.value('title'),
            'author': entry.value('author'),
            'series': entry.value('series'),
            'series_index': entry.value('series_index'),
        }
        # When the local tiers found nothing, the names themselves are still the best
        # question we can ask: "Sanderson_MB01_128k" is not a title, but it is a
        # perfectly good keyword search. Without this the network tiers used to bail
        # out with "nothing to search with" precisely when they were needed most.
        if not hints['title'] and not hints['author']:
            hints['query'] = self._raw_query(entry)
        return hints

    @staticmethod
    def _raw_query(entry: BookEntry) -> str:
        """Free-text query built from the folder and file names, noise stripped."""
        from .regex_parser import strip_noise

        path = Path(entry.primary_audio)
        parts = []
        for text in (path.parent.name, path.stem):
            cleaned = strip_noise(text)
            # A stem that merely repeats the folder, or is "01"/"part 3", adds nothing.
            if cleaned and not cleaned.isdigit() and normalize(cleaned) not in {
                    normalize(p) for p in parts}:
                parts.append(cleaned)
        return ' '.join(parts).strip()

    def _relative_path(self, entry: BookEntry) -> str:
        """The path as the library sees it - never the absolute one."""
        if entry.relative_path:
            return entry.relative_path
        try:
            return str(Path(entry.primary_audio).relative_to(self.input_root))
        except (ValueError, TypeError):
            path = Path(entry.primary_audio)
            return str(Path(path.parent.name) / path.name)

    def _folder_context(self, entry: BookEntry) -> List[str]:
        try:
            folder = Path(entry.folder)
            if folder.is_dir():
                return sorted(p.name for p in folder.iterdir() if p.is_file())
        except OSError:
            pass
        return list(entry.audio_files)

    @staticmethod
    def _is_satisfied(entry: BookEntry) -> bool:
        """True when there's nothing worth spending a network call on."""
        if entry.author.is_empty() or entry.title.is_empty():
            return False
        # A book that claims a series but has no index is still incomplete.
        if not entry.series.is_empty() and entry.series_index.is_empty():
            return False
        return entry.confidence() >= 0.8

    def _share_within_folder(self, entries: List[BookEntry]) -> None:
        """Propagate a confidently-known author/series to siblings that lack one.

        Books sitting in the same folder overwhelmingly share an author and series;
        this is what lets one well-tagged file rescue three badly-named ones.
        """
        if len(entries) < 2:
            return

        for name in ('author', 'series'):
            values: Dict[str, List[BookEntry]] = {}
            for entry in entries:
                value = entry.value(name)
                if value:
                    values.setdefault(normalize(value), []).append(entry)
            if len(values) != 1:
                continue  # siblings disagree - propagating would spread an error

            holders = next(iter(values.values()))
            best = max(holders, key=lambda e: e.get_field(name).confidence)
            if best.get_field(name).confidence < 0.6:
                continue
            for entry in entries:
                if entry.get_field(name).is_empty():
                    # Slightly discounted: inherited, not independently observed.
                    if entry.set_field(name, best.value(name), 'metadata',
                                       best.get_field(name).confidence * 0.85):
                        entry.log('folder', f'Inherited {name} '
                                            f'"{best.value(name)}" from folder siblings')

    def _check_quality(self, entry: BookEntry) -> None:
        """Dock the confidence of anything that reads like a botched scrape.

        The value is left exactly as it was - rewriting a guess into a different guess
        helps nobody. What changes is how sure we claim to be about it, so a book with
        "The Deep Sky (Unabridged" in its title stops clearing a review threshold
        threshold and turns up in the review queue where a person can look at it.
        """
        entry.warnings = []
        # Whatever was docked last time is given back first, so this is a fresh
        # judgement of the values as they stand rather than another round of the same
        # punishment. Re-running identification must not erode a field's confidence.
        for name, record in (entry.quality_penalties or {}).items():
            try:
                value, factor = record
            except (TypeError, ValueError):
                continue
            field = entry.get_field(name)
            # Only refund a field that still holds the value that was docked. If a
            # later tier replaced it, that Field arrived with its own confidence and
            # dividing it by an old penalty would inflate a number nobody discounted.
            if factor and str(field.value) == str(value):
                field.confidence = round(min(1.0, field.confidence / factor), 3)
        entry.quality_penalties = {}

        if not self.settings.get_bool('AO_WARN_DIRTY_OUTPUT', True):
            return

        from .quality import inspect_entry, penalties

        findings = inspect_entry(entry)
        if not findings:
            return

        entry.warnings = [finding.message for finding in findings]
        for name, factor in penalties(findings).items():
            field = entry.get_field(name)
            # A value you typed yourself is never second-guessed: you looked at the
            # book. Everything else is a guess, and this one looks like a bad guess.
            if field.source == 'user':
                continue
            field.confidence = round(field.confidence * factor, 3)
            entry.quality_penalties[name] = [str(field.value), factor]
        entry.log('quality',
                  'These values look like a search result that came back malformed. '
                  'Their confidence has been lowered so they surface for review:\n'
                  + '\n'.join(f'  - {message}' for message in entry.warnings),
                  {'findings': entry.warnings})

    def _finalise(self, entry: BookEntry) -> None:
        """Set the review status implied by what we ended up with."""
        entry.begin_tier('quality')
        self._check_quality(entry)
        if entry.status in ('approved', 'rejected', 'applied'):
            return
        confidence = entry.confidence()

        if not entry.is_complete():
            entry.status = STATUS_RISKY
        elif confidence < 0.6 or entry.warnings:
            # A malformed-looking value is exactly what "risky" is for.
            entry.status = STATUS_RISKY
        else:
            entry.status = STATUS_PENDING
