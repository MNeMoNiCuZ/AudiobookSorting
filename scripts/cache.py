"""SQLite-backed lookup cache.

A book's author and series index do not change, so a successful lookup is cached
forever. A *failed* lookup only means "not found today" - maybe the database gained the
book, maybe the network was down - so misses expire on a short TTL.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lookups (
    namespace TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT,
    hit       INTEGER NOT NULL,
    created   REAL NOT NULL,
    PRIMARY KEY (namespace, key)
);
CREATE INDEX IF NOT EXISTS idx_lookups_created ON lookups(created);
"""

_MISSING = object()


class Cache:
    """Thread-safe key/value cache. Values are JSON-serialisable."""

    def __init__(self, db_path: Path, miss_ttl: int = 86400):
        self.db_path = Path(db_path)
        self.miss_ttl = miss_ttl
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + our own lock: workers share one connection.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        """Cached value, or `default` if absent or expired."""
        with self._lock:
            row = self._conn.execute(
                'SELECT value, hit, created FROM lookups WHERE namespace=? AND key=?',
                (namespace, key)).fetchone()
        if row is None:
            return default

        value_json, hit, created = row
        if not hit and (time.time() - created) > self.miss_ttl:
            self.delete(namespace, key)
            return default
        if not hit:
            return None  # a remembered miss - distinct from "never looked up"

        try:
            return json.loads(value_json)
        except (TypeError, ValueError):
            return default

    def has(self, namespace: str, key: str) -> bool:
        """True if we've looked this up before, whether or not it was found."""
        return self.get(namespace, key, _MISSING) is not _MISSING

    def set(self, namespace: str, key: str, value: Any) -> None:
        self._write(namespace, key, json.dumps(value), hit=1)

    def set_miss(self, namespace: str, key: str) -> None:
        """Remember that this lookup found nothing, so we don't hammer the API."""
        self._write(namespace, key, None, hit=0)

    def _write(self, namespace: str, key: str, value_json: Optional[str], hit: int) -> None:
        with self._lock:
            self._conn.execute(
                'INSERT OR REPLACE INTO lookups (namespace, key, value, hit, created) '
                'VALUES (?, ?, ?, ?, ?)',
                (namespace, key, value_json, hit, time.time()))
            self._conn.commit()

    def delete(self, namespace: str, key: str) -> None:
        with self._lock:
            self._conn.execute('DELETE FROM lookups WHERE namespace=? AND key=?',
                               (namespace, key))
            self._conn.commit()

    def clear(self, namespace: Optional[str] = None) -> int:
        with self._lock:
            if namespace:
                cursor = self._conn.execute('DELETE FROM lookups WHERE namespace=?',
                                            (namespace,))
            else:
                cursor = self._conn.execute('DELETE FROM lookups')
            self._conn.commit()
            return cursor.rowcount

    def stats(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                'SELECT namespace, SUM(hit), COUNT(*) FROM lookups GROUP BY namespace'
            ).fetchall()
        return {ns: {'hits': hits or 0, 'total': total} for ns, hits, total in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
