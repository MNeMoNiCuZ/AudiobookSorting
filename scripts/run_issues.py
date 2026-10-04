"""What went wrong with the online sources during one run of the queue.

A run is everything the queue does from the moment it starts until it is empty again.
When a source starts throttling, the waits between its requests grow; if it keeps
refusing - or refuses in a way that cannot get better, like an exhausted daily quota
or a rejected key - it is switched off for the rest of the run, and every book that
then skips it is noted. When the queue drains, the window shows one summary of all of
it, and you choose which books to re-queue with which source.

Source keys: ``api:<database>``, ``search:<site>`` and ``llm``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

# Consecutive rate-limited requests (after a source's own retries) before it is
# switched off for the rest of the run.
LIMIT_BEFORE_OFF = 3
BACKOFF_START = 2.0
BACKOFF_MAX = 60.0

LABELS = {
    'api:openlibrary': 'Open Library', 'api:googlebooks': 'Google Books',
    'api:librivox': 'LibriVox', 'search:goodreads': 'Goodreads',
    'search:brave': 'Brave Search', 'search:duckduckgo': 'DuckDuckGo',
    'llm': 'Language model',
}


def label_for(key: str) -> str:
    if key in LABELS:
        return LABELS[key]
    kind, _, name = key.partition(':')
    return name.title() if name else kind.title()


def requeue_tiers(key: str) -> List[str]:
    """The tier list that re-asks this source - what Identify with... would pass."""
    if key.startswith('api:'):
        return [key]
    if key.startswith('search:'):
        return ['search']
    return [key]


@dataclass
class SourceIssue:
    key: str
    message: str = ''
    disabled: bool = False
    backoff: float = 0.0
    streak: int = 0
    failed: Set[str] = field(default_factory=set)     # entry ids the source failed
    skipped: Set[str] = field(default_factory=set)    # entry ids it was not asked for


class RunIssues:
    """Thread-safe record of source trouble for the current queue run."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sources: Dict[str, SourceIssue] = {}

    def _get(self, key: str) -> SourceIssue:
        issue = self._sources.get(key)
        if issue is None:
            issue = self._sources[key] = SourceIssue(key)
        return issue

    # ------------------------------------------------------- called by clients

    def is_disabled(self, key: str) -> bool:
        with self._lock:
            issue = self._sources.get(key)
            return bool(issue and issue.disabled)

    def wait(self, key: str, should_cancel: Optional[Callable[[], bool]] = None) -> None:
        """Sleep off this source's current backoff before asking it again."""
        with self._lock:
            issue = self._sources.get(key)
            delay = issue.backoff if issue else 0.0
        end = time.time() + delay
        while time.time() < end:
            if should_cancel is not None and should_cancel():
                return
            time.sleep(max(0.0, min(0.25, end - time.time())))

    def limited(self, key: str, message: str, permanent: bool = False) -> None:
        """The source throttled or refused us. Grow the wait, or switch it off."""
        with self._lock:
            issue = self._get(key)
            issue.message = message
            issue.streak += 1
            issue.backoff = min(BACKOFF_MAX, max(BACKOFF_START, issue.backoff * 2))
            if permanent or issue.streak >= LIMIT_BEFORE_OFF:
                issue.disabled = True

    def ok(self, key: str) -> None:
        """The source answered. Its wait shrinks back and the streak resets."""
        with self._lock:
            issue = self._sources.get(key)
            if issue is not None and not issue.disabled:
                issue.streak = 0
                issue.backoff = issue.backoff / 2 if issue.backoff > BACKOFF_START else 0.0

    def restore(self, key: str) -> None:
        """A forced call got through: the source is usable again for this run."""
        with self._lock:
            issue = self._sources.get(key)
            if issue is not None:
                issue.disabled = False
                issue.streak = 0
                issue.backoff = 0.0

    # ------------------------------------------------------ called by resolver

    def failed(self, key: str, entry_id: str, message: str = '') -> None:
        with self._lock:
            issue = self._get(key)
            if message and not issue.message:
                issue.message = message
            if entry_id not in issue.skipped:
                issue.failed.add(entry_id)

    def skipped(self, key: str, entry_id: str) -> None:
        with self._lock:
            issue = self._get(key)
            issue.failed.discard(entry_id)
            issue.skipped.add(entry_id)

    # ----------------------------------------------------------- queue drained

    def summary(self) -> List[Dict]:
        """One dict per source that had trouble affecting at least one book."""
        with self._lock:
            rows = []
            for key, issue in sorted(self._sources.items()):
                if not issue.failed and not issue.skipped:
                    continue
                rows.append({
                    'key': key, 'label': label_for(key), 'message': issue.message,
                    'disabled': issue.disabled, 'failed': sorted(issue.failed),
                    'skipped': sorted(issue.skipped), 'tiers': requeue_tiers(key),
                })
            return rows

    def reset(self) -> None:
        with self._lock:
            self._sources.clear()
