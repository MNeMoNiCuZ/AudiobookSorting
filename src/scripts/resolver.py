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
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .models import (STATUS_PENDING, STATUS_RISKY, BookEntry, Field, _llm_overrules,
                     normalize)
from .name_compare import compare_names

SERIES_FIELDS = ("series", "series_index")

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
        # Books per step when a folder is identified: each batch is searched, sent to
        # the model and finished before the next starts.
        self.folder_batch_size = settings.get_int('AO_FOLDER_BATCH_SIZE', 8)
        # Run at least this many tiers whatever the confidence. Tags and a filename
        # agreeing is not proof - they are both just the name someone gave the file -
        # so the default keeps going as far as the book databases.
        self.always_to_tier = settings.get_int('AO_ALWAYS_SEARCH_TO_TIER', 3)
        self.input_root = str(settings.get_path('AO_INPUT_DIR'))

        self._metadata = metadata_extractor
        self._api = api_client
        self._search = search_client
        self._llm = llm_client
        self._llm_down = False
        # Set by the controller: the queue run's record of source trouble. Sources
        # that keep refusing are switched off for the rest of the run through it.
        self.run_issues = None
        # Set by the controller: every loaded entry, so the model can be shown the
        # other books sharing a folder even when only one of them is being identified.
        self.all_entries: Optional[Callable[[], List[BookEntry]]] = None
        self._cancel: Optional[CancelCheck] = None
        self._track_stage = False

    def _cancelled(self) -> bool:
        """For the clients' backoff waits, so Cancel does not sit out a minute."""
        return bool(self._cancel and self._cancel())

    @property
    def _llm_failed(self) -> bool:
        if self.run_issues is not None:
            return self.run_issues.is_disabled('llm')
        return self._llm_down

    def _llm_unavailable(self, entries: List[BookEntry], exc: Exception) -> None:
        self._llm_down = True
        if self.run_issues is not None:
            self.run_issues.limited('llm', f'unavailable: {exc}', permanent=True)
            for entry in entries:
                self.run_issues.failed('llm', entry.entry_id, f'unavailable: {exc}')
        for entry in entries:
            self._stage_failed(entry, 'llm')

    def _llm_available(self) -> None:
        """The provider answered, so a forced retry switches it back on for the run."""
        self._llm_down = False
        if self.run_issues is not None:
            self.run_issues.restore('llm')

    def _note_sources(self, entry: BookEntry, prefix: str, failures: Dict[str, str],
                      skipped: List[str]) -> None:
        """Tell the run which sources failed or were skipped for this book."""
        if failures or skipped:
            self._stage_failed(entry, prefix)
        if self.run_issues is None:
            return
        for name, why in (failures or {}).items():
            self.run_issues.failed(f'{prefix}:{name}', entry.entry_id, why)
        for name in skipped or []:
            self.run_issues.skipped(f'{prefix}:{name}', entry.entry_id)

    # ------------------------------------------------- stage, for the Status column

    # Only a run that reaches for an online source or the model is tracked: the
    # initial tags-and-filename scan finishing is not "Done".

    def _stage(self, entry: BookEntry, name: str) -> None:
        if self._track_stage:
            entry.identify_stage = name
            entry.identify_state = 'running'

    @staticmethod
    def _stage_failed(entry: BookEntry, name: str) -> None:
        if name not in entry.identify_failed:
            entry.identify_failed = list(entry.identify_failed) + [name]

    def _stage_start(self, entries: List[BookEntry], selected: List[str]) -> None:
        self._track_stage = any(name in ('api', 'search', 'llm') for name in selected)
        if self._track_stage:
            for entry in entries:
                entry.identify_failed = []

    def _stage_end(self, entry: BookEntry) -> None:
        if self._track_stage:
            entry.identify_state = 'failed' if entry.identify_failed else 'done'

    @staticmethod
    def _stage_stopped(entries: List[BookEntry]) -> None:
        for entry in entries:
            if entry.identify_state == 'running':
                entry.identify_state = 'stopped'

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
            self._api = api = BookAPIClient(
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
            api.run_issues = self.run_issues
            api.should_cancel = self._cancelled
        return self._api

    @property
    def search(self):
        if self._search is None:
            from .web_search import WebSearchClient
            self._search = WebSearchClient(
                cache=self.cache, timeout=self.settings.get_int('AO_TIMEOUT', 20),
                settings=self.settings)
            self._search.run_issues = self.run_issues
            self._search.should_cancel = self._cancelled
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
                should_cancel: Optional[CancelCheck] = None,
                on_tier: Optional[Callable[[str, int, int], None]] = None,
                fresh: bool = False, skip_done: bool = False) -> BookEntry:
        """Run the chain over one entry, in place. Returns the same entry.

        `fresh` skips the lookup cache and goes to the network. Only "Run again" on a
        source's card asks for that; Identify reuses whatever was already looked up.

        `skip_done` passes over the network tiers this book already finished on an
        earlier run (see BookEntry.tiers_done), so Identify continues a stopped run.
        """
        self._cancel = should_cancel
        tiers, api_sources = self._split_tiers(tiers)
        steps = [
            ('metadata', self.enable_metadata, self._tier_metadata),
            ('regex', self.enable_regex, self._tier_regex),
            ('api', self.enable_api,
             lambda e: self._tier_api(e, sources=api_sources,
                                      forced=tiers is not None, fresh=fresh)),
            ('search', self.enable_search,
             lambda e: self._tier_search(e, fresh=fresh)),
            ('llm', self.enable_llm, lambda e: self._tier_llm(e, fresh=fresh)),
        ]
        selected = [(name, enabled, handler) for name, enabled, handler in steps
                    if (name in tiers if tiers is not None else enabled)]
        tier_total = len(selected)
        tier_done = 0
        self._stage_start([entry], [name for name, _e, _h in selected])

        for name, enabled, handler in selected:
            if should_cancel and should_cancel():
                entry.log('cancelled', f'Stopped before the {name} tier')
                self._stage_stopped([entry])
                return entry
            if self._already_done(entry, name, skip_done, fresh):
                tier_done += 1
                if on_tier is not None:
                    on_tier(name, tier_done, tier_total)
                continue
            if on_tier is not None:
                on_tier(name, tier_done, tier_total)
            # This tier is about to speak, so retire what it said on the last run -
            # otherwise re-running appends a duplicate of the same paragraph.
            entry.begin_tier(name)
            self._stage(entry, name)
            self._forget_done(entry, name)
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
                tier_done += 1
                if on_tier is not None:
                    on_tier(name, tier_done, tier_total)
                continue
            try:
                handler(entry)
            except Exception as exc:
                self.logger.exception('%s tier failed for %s', name, entry.entry_id)
                entry.log(name, f'Failed: {exc}')
                self._stage_failed(entry, name)
            else:
                if name in ('api', 'search'):
                    self._mark_done(entry, name)
            tier_done += 1
            if on_tier is not None:
                on_tier(name, tier_done, tier_total)

        entry.resolved = True
        self._stage_end(entry)
        self._finalise(entry)
        return entry

    def resolve_folder(self, entries: List[BookEntry], tiers: Optional[List[str]] = None,
                       should_cancel: Optional[CancelCheck] = None,
                       on_tier: Optional[Callable[[str, int, int, BookEntry],
                                                  None]] = None,
                       fresh: bool = False,
                       on_entry_done: Optional[Callable[[BookEntry], None]] = None,
                       skip_done: bool = False) -> List[BookEntry]:
        """Resolve every entry in one folder, sharing what we learn between them (#11).

        `tiers` overrides the enabled-tier settings for this run, which is what the
        mode checkboxes on the main window pass in.

        `on_tier` is called per entry. The network tiers run one book at a time, so
        only the book being worked on reports them; the rest of the folder reports
        "waiting" until its turn. Reporting every step to every book made a folder of
        forty look like forty searches running at once.
        """
        self._cancel = should_cancel
        if not entries:
            return entries

        tiers, api_sources = self._split_tiers(tiers)

        def wanted(name: str, enabled: bool) -> bool:
            return name in tiers if tiers is not None else enabled

        selected = [name for name, enabled in (
            ('metadata', self.enable_metadata), ('regex', self.enable_regex),
            ('api', self.enable_api), ('search', self.enable_search),
            ('llm', self.enable_llm)) if wanted(name, enabled)]
        completed: Dict[str, set] = {entry.entry_id: set() for entry in entries}
        self._stage_start(entries, selected)

        def report(name: str, finished: bool = False,
                   only: Optional[BookEntry] = None) -> None:
            for target in ([only] if only is not None else entries):
                done = completed[target.entry_id]
                if finished:
                    done.add(name)
                if on_tier is not None:
                    on_tier(name, len(done), len(selected), target)

        # Local tiers first, per entry - they're free and inform the shared step.
        for entry in entries:
            if should_cancel and should_cancel():
                self._stage_stopped(entries)
                return entries
            if wanted('metadata', self.enable_metadata):
                if entry is entries[0]:
                    report('metadata')
                entry.begin_tier('metadata')
                self._stage(entry, 'metadata')
                self._tier_metadata(entry)
            if wanted('regex', self.enable_regex):
                if entry is entries[0]:
                    report('regex')
                entry.begin_tier('regex')
                self._stage(entry, 'regex')
                self._tier_regex(entry)

        if 'metadata' in selected:
            report('metadata', True)
        if 'regex' in selected:
            report('regex', True)

        self._share_within_folder(entries)

        # Then the network tiers and the model, a batch of books at a time. Running the
        # whole folder through each step before any book finished meant a folder of
        # sixty showed nothing for most of an hour, and a cancel threw all of it away.
        # The model still sees the rest of the folder: _tier_llm_folder passes every
        # sibling outside the batch along as context.
        forced = tiers is not None
        networked = [name for name in ('api', 'search') if name in selected]
        if networked or 'llm' in selected:
            report('waiting')
        finished: set = set()

        def finish(entry: BookEntry) -> None:
            entry.resolved = True
            self._stage_end(entry)
            self._finalise(entry, entries)
            completed[entry.entry_id].update(selected)
            finished.add(entry.entry_id)
            report(selected[-1] if selected else 'waiting', only=entry)
            if on_entry_done is not None:
                on_entry_done(entry)

        def llm_wanted() -> bool:
            return wanted('llm', self.enable_llm) and (fresh or not self._llm_failed)

        size = max(1, self.folder_batch_size)
        for start in range(0, len(entries), size):
            batch = entries[start:start + size]
            needs_help: List[BookEntry] = []
            for entry in batch:
                if should_cancel and should_cancel():
                    self._stage_stopped(entries)
                    return entries
                if forced or not self._is_satisfied(entry):
                    if wanted('api', self.enable_api) and not self._already_done(
                            entry, 'api', skip_done, fresh):
                        report('api', only=entry)
                        entry.begin_tier('api')
                        self._stage(entry, 'api')
                        self._forget_done(entry, 'api')
                        self._tier_api(entry, sources=api_sources, forced=forced,
                                       fresh=fresh)
                        self._mark_done(entry, 'api')
                        report('api', True, only=entry)
                    if wanted('search', self.enable_search) and (
                            forced or not self._is_satisfied(entry)) and (
                            not self._already_done(entry, 'search', skip_done, fresh)):
                        report('search', only=entry)
                        entry.begin_tier('search')
                        self._stage(entry, 'search')
                        self._forget_done(entry, 'search')
                        self._tier_search(entry, fresh=fresh)
                        self._mark_done(entry, 'search')
                        report('search', True, only=entry)
                for name in networked:
                    completed[entry.entry_id].add(name)
                if (llm_wanted() and (forced or not self._is_satisfied(entry))
                        and not self._already_done(entry, 'llm', skip_done, fresh)):
                    needs_help.append(entry)
                else:
                    # Nothing left to do for this book - finish it now, not with the
                    # rest of the folder.
                    finish(entry)

            if not needs_help:
                continue
            if should_cancel and should_cancel():
                self._stage_stopped(entries)
                return entries
            # The model must see the database and search evidence from this run.
            self._share_within_folder(entries)
            if self.folder_reasoning and len(needs_help) > 1:
                # One request covers the batch; the rest of the folder rides along.
                for entry in needs_help:
                    report('llm', only=entry)
                    self._stage(entry, 'llm')
                self._tier_llm_folder(needs_help)
                self._share_within_folder(entries)
                for entry in needs_help:
                    finish(entry)
            else:
                for entry in needs_help:
                    if should_cancel and should_cancel():
                        self._stage_stopped(entries)
                        return entries
                    report('llm', only=entry)
                    entry.begin_tier('llm')
                    self._stage(entry, 'llm')
                    self._tier_llm(entry, fresh=fresh)
                    finish(entry)

        for entry in entries:
            if entry.entry_id not in finished:
                finish(entry)

        self._share_within_folder(entries)
        self.normalize_titles(entries)
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
                          'No book identity tags',
                          {'tags': dict(raw)})
            else:
                entry.log('metadata', 'No embedded tags')
            return
        changed = [name for name in ('author', 'title', 'series', 'series_index')
                   if name in found and entry.set_field(name, found[name], 'metadata')]
        entry.log('metadata',
                  'Read embedded tags',
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
                      'No identity fields parsed',
                      {'path': relative, 'considered': considered})
            return
        changed = [name for name in ('author', 'title', 'series', 'series_index')
                   if name in found and entry.set_field(name, found[name], 'regex')]
        entry.log('regex', 'Parsed filename and folders',
                  {'applied': changed, 'path': relative, 'considered': considered,
                   'from': contributions, 'result': dict(found)})

    def _tier_api(self, entry: BookEntry, sources: Optional[List[str]] = None,
                  forced: bool = False, fresh: bool = False) -> None:
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

        # `forced` means "run even though the book looks resolved"; only `fresh`
        # bypasses the cache. Tying the two together made every Identify you started
        # re-query every database from scratch.
        result = self.api.search(hints, sources=sources or None, force=fresh)
        ran = list(getattr(self.api, 'last_sources', []) or self.api.sources)
        skipped = list(getattr(self.api, 'last_skipped', []) or [])
        if not getattr(self.api, 'last_from_cache', False):
            self._note_sources(entry, 'api', getattr(self.api, 'last_errors', {}),
                               skipped)
        if skipped:
            entry.log('api', 'Skipped ' + ', '.join(skipped) + ': switched off for the '
                      'rest of this run after it kept refusing requests.')
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
        # The candidate score is the confidence in this particular match. Using only
        # a source-wide default meant a 96% Google Books or iTunes match could lose to
        # unrelated 80% embedded metadata, even though the panel showed the database
        # result as stronger. Manual edits remain protected by BookEntry.set_field.
        match_confidence = float(result.get('score', 0.0) or 0.0)
        changed, held = self._apply_fields(
            entry, result, source, match_confidence or None)
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

    def _tier_search(self, entry: BookEntry, fresh: bool = False) -> None:
        hints = self._hints(entry)
        result = self.search.search(hints, force=fresh)
        raw = list(getattr(self.search, 'last_results', []))
        skipped = list(getattr(self.search, 'last_skipped', []) or [])
        if not getattr(self.search, 'last_from_cache', False):
            self._note_sources(entry, 'search',
                               getattr(self.search, 'last_failures', {}), skipped)
        if skipped:
            entry.log('search', 'Skipped ' + ', '.join(skipped) + ': switched off for '
                      'the rest of this run after it kept refusing requests.')
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
                  {'applied': changed, 'held': held, 'result': dict(result),
                   'results': raw[:10]})

    def _tier_llm(self, entry: BookEntry, fresh: bool = False) -> None:
        # "Run again" forces the call even after the provider was switched off.
        if self._llm_failed and not fresh:
            self._forget_done(entry, 'llm')
            entry.log('llm', 'Skipped: the LLM provider is unreachable')
            self._stage_failed(entry, 'llm')
            if self.run_issues is not None:
                self.run_issues.skipped('llm', entry.entry_id)
            return

        hints = self._hints(entry)
        hints['path'] = self._relative_path(entry)
        hints['human'] = _human_fields(entry)
        hints['sources'] = {name: entry.get_field(name).source
                            for name in ('title', 'author', 'series', 'series_index')}
        hints['findings'] = _findings(entry)
        hints['extent'] = _extent(entry)
        siblings = self._folder_siblings(entry)
        if siblings or entry.is_multi_book_folder:
            # The folder is shared with other entries, so the folder alone is not this
            # item's path - given only that, the model identifies the folder instead.
            hints['path'] = str(Path(hints['path']) / Path(entry.primary_audio).name)
        sibling_files = {name for sibling in siblings for name in sibling['files']}
        own_files = [Path(name).name for name in entry.audio_files]
        context = [name for name in self._folder_context(entry)
                   if name not in sibling_files and name not in own_files]
        if siblings:
            hints['name_parts'] = compare_names(
                Path(entry.primary_audio).stem,
                [Path(sibling['files'][0]).stem for sibling in siblings])
            # Stated as a fact, not a conclusion: the same guess on several entries
            # may be a shared collection name, or may be right for all of them.
            own_title = normalize(entry.value('title'))
            if own_title:
                hints['title_shared'] = sum(
                    normalize(sibling['title']) == own_title
                    and sibling['sources'].get('title') != 'llm'
                    for sibling in siblings)

        try:
            result = self.llm.query_book(hints, context, evidence=entry.evidence,
                                         book_files=own_files,
                                         sibling_books=siblings)
        except Exception as exc:
            self._forget_done(entry, 'llm')
            self._llm_unavailable([entry], exc)
            entry.log('llm', f'Provider unavailable: {exc}', self._exchange())
            return
        self._llm_available()
        if not result:
            self._forget_done(entry, 'llm')
            entry.log('llm', 'No usable answer from the model', self._exchange())
            return
        # The model answered - even "no name for this book" is its answer.
        self._mark_done(entry, 'llm')
        if not result.get('identified', True):
            entry.log('llm', 'The model found no name for this book: '
                      + (result.get('reasoning') or 'the evidence does not name it'),
                      self._exchange({'applied': [], 'held': [], 'result': result}))
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
                'human': _human_fields(entry),
                'files': [Path(name).name for name in entry.audio_files],
            })
        batch = {entry.entry_id for entry in entries}
        siblings = [sibling for sibling in self._folder_siblings(entries[0])
                    if sibling['entry_id'] not in batch]

        stems = [Path(book['file']).stem for book in books]
        stems += [Path(sibling['files'][0]).stem for sibling in siblings]
        for i, book in enumerate(books):
            book['name_parts'] = compare_names(stems[i], stems[:i] + stems[i + 1:])

        merged_evidence: Dict[str, Any] = {}
        for entry in entries:
            for tier, items in (entry.evidence or {}).items():
                merged_evidence.setdefault(tier, []).extend(items)

        for entry in entries:
            entry.begin_tier('llm')
            self._forget_done(entry, 'llm')

        try:
            result = self.llm.query_folder(folder_name, books,
                                           evidence=merged_evidence or None,
                                           sibling_books=siblings)
        except Exception as exc:
            self._llm_unavailable(entries, exc)
            for entry in entries:
                entry.log('llm', f'Provider unavailable: {exc}', self._exchange())
            return
        self._llm_available()
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
            # Only books the model actually answered for count as done.
            self._mark_done(entry, 'llm')
            if not item.get('identified', True):
                entry.log('llm', 'The model found no name for this book'
                          + (f': {reasoning}' if reasoning else ''),
                          self._exchange({'applied': [], 'held': [], 'result': item}))
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
                # The model answering "no series" is an answer: a series the filename
                # parser made out of the folder name is removed, not kept.
                before = entry.get_field(name)
                if (source == 'llm' and name in values and name in SERIES_FIELDS
                        and not before.is_empty()
                        and _llm_overrules(source, confidence or 0.0, before)):
                    setattr(entry, name, Field(source='llm',
                                               confidence=confidence or 0.0))
                    applied.append(name)
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

    def _folder_siblings(self, entry: BookEntry) -> List[Dict[str, Any]]:
        """The other books in this entry's folder, with what is believed about each.

        Only the same folder: books in the parent or in subfolders are a library, not
        a set of siblings, and would drown the model in unrelated names.
        """
        if self.all_entries is None or not entry.folder:
            return []
        try:
            others = list(self.all_entries())
        except Exception:
            self.logger.exception('Could not list the loaded entries')
            return []
        here = _folder_key(entry.folder)
        siblings = []
        for other in others:
            if other.entry_id == entry.entry_id or _folder_key(other.folder) != here:
                continue
            siblings.append({
                'entry_id': other.entry_id,
                'files': [Path(name).name for name in other.audio_files]
                         or [Path(other.primary_audio).name],
                'title': other.value('title'),
                'author': other.value('author'),
                'series': other.value('series'),
                'series_index': other.value('series_index'),
                'extent': _extent(other),
                'sources': {name: other.get_field(name).source
                            for name in ('title', 'author', 'series', 'series_index')},
            })
        siblings.sort(key=lambda sibling: sibling['files'][0].lower())
        return siblings

    @staticmethod
    def _already_done(entry: BookEntry, name: str, skip_done: bool,
                      fresh: bool) -> bool:
        """True when this tier finished on an earlier run and may be passed over.

        The tier's trace is left alone, so the panel still shows what that run found.
        """
        return (skip_done and not fresh and name in (entry.tiers_done or []))

    @staticmethod
    def _forget_done(entry: BookEntry, name: str) -> None:
        if name in (entry.tiers_done or []):
            entry.tiers_done = [tier for tier in entry.tiers_done if tier != name]

    def _mark_done(self, entry: BookEntry, name: str) -> None:
        """Record a finished tier, unless one of its sources failed this book."""
        if (self.run_issues is not None
                and self.run_issues.troubled(name, entry.entry_id)):
            self._forget_done(entry, name)
            return
        if name not in (entry.tiers_done or []):
            entry.tiers_done = list(entry.tiers_done or []) + [name]

    @staticmethod
    def _is_satisfied(entry: BookEntry) -> bool:
        """True when there's nothing worth spending a network call on."""
        if not entry.is_complete():
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

    def _title_context(self, entries: List[BookEntry]) -> List[BookEntry]:
        context = list(entries)
        known = {id(entry) for entry in entries}
        if self.all_entries is not None:
            for other in self.all_entries():
                if id(other) not in known:
                    context.append(other)
                    known.add(id(other))
        return context

    @staticmethod
    def _automatic_title(entry: BookEntry, entries: List[BookEntry]) -> str:
        """Pad automatic title numbers and complete a confirmed numbered series."""
        from .quality import PLACEHOLDER_TITLE, _TRAILING_NUMBER

        title = entry.value('title')
        if entry.title.source == 'user' or PLACEHOLDER_TITLE.fullmatch(title):
            return title
        padded = _TRAILING_NUMBER.sub(lambda match: match.group().zfill(2), title)
        if padded != title:
            return padded
        series = entry.value('series')
        author = entry.value('author')
        if (not series or not author or entry.value('series_index') != '1'
                or normalize(title) != normalize(series)):
            return title
        for other in entries:
            if (other is entry or normalize(other.value('author')) != normalize(author)
                    or normalize(other.value('series')) != normalize(series)):
                continue
            match = re.fullmatch(r'(.+)\s+(\d{1,3})', other.value('title'))
            if (match and normalize(match.group(1)) == normalize(title)
                    and int(match.group(2)) > 1
                    and other.value('series_index') == str(int(match.group(2)))):
                return f'{title} 01'
        return title

    def normalize_titles(self, entries: List[BookEntry]) -> List[BookEntry]:
        """Revisit earlier books after the remaining series titles are known."""
        context = self._title_context(entries)
        changed = []
        for entry in context:
            if self._automatic_title(entry, context) != entry.value('title'):
                self._finalise(entry, context)
                changed.append(entry)
        return changed

    def _check_quality(self, entry: BookEntry,
                       entries: Optional[List[BookEntry]] = None) -> None:
        """Dock the confidence of anything that reads like a botched scrape.

        Automatic title numbers are normalised before inspection. Other suspicious
        values are kept, but their confidence is reduced, so a book with
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

        title = self._automatic_title(entry, self._title_context(entries or [entry]))
        if title != entry.value('title'):
            entry.title.value = title
        entry.warnings_checked_values = {
            name: entry.value(name) for name in ('author', 'series', 'title')}

        if not self.settings.get_bool('AO_WARN_DIRTY_OUTPUT', True):
            return

        from .quality import inspect_entry, penalties

        findings = inspect_entry(entry)
        if not findings:
            return

        entry.warnings = [finding.message for finding in findings]
        entry.warnings_silenced = False
        for name, factor in penalties(findings).items():
            field = entry.get_field(name)
            # A value you typed yourself is never second-guessed: you looked at the
            # book. Everything else is a guess, and this one looks like a bad guess.
            if field.source == 'user':
                continue
            field.confidence = round(field.confidence * factor, 3)
            entry.quality_penalties[name] = [str(field.value), factor]
        entry.log('quality',
                  '\n'.join(entry.warnings),
                  {'findings': entry.warnings})

    def _finalise(self, entry: BookEntry,
                  entries: Optional[List[BookEntry]] = None) -> None:
        """Set the review status implied by what we ended up with."""
        entry.begin_tier('quality')
        self._check_quality(entry, entries)
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


def _human_fields(entry: BookEntry) -> List[str]:
    """The fields a person typed. The model is told these are fact, not a guess."""
    return [name for name in ('title', 'author', 'series', 'series_index')
            if entry.get_field(name).source == 'user' and entry.value(name)]


def _folder_key(folder: str) -> str:
    """A folder path in a form two spellings of the same directory compare equal in."""
    return os.path.normcase(os.path.normpath(folder)) if folder else ''


def _findings(entry: BookEntry) -> List[tuple]:
    """What each tier found on its own this run, labelled, before any merging.

    The combined values hide where each piece came from: a number the file name
    carries and a different one a web result claimed collapse into a single guess, and
    the model can no longer weigh one against the other.
    """
    fields = ('title', 'author', 'series', 'series_index', 'isbn')
    file_name = Path(entry.primary_audio).name
    found = []
    for step in entry.trace:
        data = step.get('data')
        tier = step.get('tier')
        if not isinstance(data, dict) or not isinstance(data.get('result'), dict):
            continue
        result = {key: str(data['result'][key]) for key in fields
                  if data['result'].get(key)}
        if not result:
            continue
        notes: Dict[str, str] = {}
        if tier == 'metadata':
            label = 'File tags'
        elif tier == 'regex':
            label = 'Parsed from the file and folder names'
            for key, origin in (data.get('from') or {}).items():
                if key in result:
                    notes[key] = ('from the file name' if origin == file_name
                                  else f'from the folder "{origin}"')
        elif tier == 'api':
            row = data['result']
            label = (f'Book database {row.get("source", "")}, best match '
                     f'(word-similarity score {float(row.get("score", 0) or 0):.2f})')
        elif tier == 'search':
            label = 'Web search, best parsed result'
        else:
            continue
        found.append((label, result, notes))
    return found


def _extent(entry: BookEntry) -> str:
    """How much audio an entry holds - "512 MB, 9 h 05 min" - for the model to weigh.

    The length is only known for the file whose tags were read, so it is given only
    when that one file is the whole entry.
    """
    sizes = [size for size in entry.audio_sizes if size and size > 0]
    parts = []
    if sizes:
        parts.append(f'{sum(sizes) / (1024 * 1024):.0f} MB')
    seconds = (entry.raw_tags or {}).get('_duration_seconds')
    if len(entry.audio_files) == 1 and seconds:
        try:
            minutes = int(float(seconds)) // 60
        except (TypeError, ValueError):
            minutes = 0
        if minutes:
            parts.append(f'{minutes // 60} h {minutes % 60:02d} min')
    return ', '.join(parts)
