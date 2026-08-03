"""Append-only record of every filesystem change, so anything can be undone (#20).

This is the safety net for a tool whose non-default mode physically moves files. Each
apply writes one transaction listing every file operation it performed; undo replays
them backwards.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class FileMove:
    source: str
    destination: str
    operation: str = 'move'      # move | copy | mkdir | write
    undone: bool = False


@dataclass
class Transaction:
    entry_id: str
    timestamp: float = field(default_factory=time.time)
    moves: List[FileMove] = field(default_factory=list)
    created_dirs: List[str] = field(default_factory=list)
    destination: str = ''
    undone: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'Transaction':
        moves = [FileMove(**m) for m in data.get('moves', [])]
        return cls(
            entry_id=data.get('entry_id', ''),
            timestamp=data.get('timestamp', 0.0),
            moves=moves,
            created_dirs=data.get('created_dirs', []),
            destination=data.get('destination', ''),
            undone=data.get('undone', False),
        )


class ApplyJournal:
    """JSON-lines log of applied transactions."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.logger = logging.getLogger(__name__)
        self._transactions: Optional[List[Transaction]] = None

    def record(self, transaction: Transaction) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(transaction.to_dict(), ensure_ascii=False) + '\n')
        self.all().append(transaction)

    def all(self) -> List[Transaction]:
        """Every transaction ever recorded, oldest first. Cached after first read."""
        if self._transactions is not None:
            return self._transactions

        transactions: List[Transaction] = []
        if self.path.exists():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    transactions.append(Transaction.from_dict(json.loads(line)))
                except ValueError:
                    self.logger.warning('Skipping corrupt journal line')
        self._transactions = transactions
        return transactions

    def reload(self) -> None:
        self._transactions = None

    def pending(self) -> List[Transaction]:
        """Transactions that have not been undone, newest last."""
        return [t for t in self.all() if not t.undone]

    def last(self) -> Optional[Transaction]:
        pending = self.pending()
        return pending[-1] if pending else None

    def undo(self, transaction: Transaction) -> List[str]:
        """Reverse one transaction. Returns a list of human-readable problems."""
        problems: List[str] = []

        # Reverse order, so files come back before their directories are removed.
        for move in reversed(transaction.moves):
            if move.undone:
                continue
            source = Path(move.source)
            destination = Path(move.destination)
            try:
                if move.operation == 'move':
                    if not destination.exists():
                        problems.append(f'Missing, cannot restore: {destination}')
                        continue
                    if source.exists():
                        problems.append(f'Original is back already, skipped: {source}')
                        continue
                    source.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(destination), str(source))
                elif move.operation in ('copy', 'write'):
                    # The original was never touched; just remove what we created.
                    if destination.exists():
                        destination.unlink()
                move.undone = True
            except OSError as exc:
                problems.append(f'{destination}: {exc}')

        # Remove directories we created, deepest first, only if now empty.
        for directory in sorted(transaction.created_dirs, key=len, reverse=True):
            path = Path(directory)
            try:
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                pass  # not empty, or in use - harmless, leave it

        transaction.undone = True
        self._rewrite()
        return problems

    def undo_last(self) -> tuple:
        transaction = self.last()
        if transaction is None:
            return None, ['Nothing to undo']
        return transaction, self.undo(transaction)

    def undo_all(self) -> tuple:
        """Undo every outstanding transaction, newest first."""
        return self.undo_through(0)

    def undo_through(self, index: int) -> tuple:
        """Roll back to just before ``pending()[index]``.

        Undoing is only coherent newest-first - a transaction may have moved files a
        later one then moved again - so picking an entry in the history window undoes
        it *and* everything applied after it, not that one in isolation.
        """
        pending = self.pending()
        if not pending or index < 0 or index >= len(pending):
            return 0, ['Nothing to undo']

        undone, problems = 0, []
        for transaction in reversed(pending[index:]):
            problems.extend(self.undo(transaction))
            undone += 1
        return undone, problems

    def _rewrite(self) -> None:
        """Persist undone flags. Small file, so a full atomic rewrite is fine."""
        transactions = self.all()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as handle:
            for transaction in transactions:
                handle.write(json.dumps(transaction.to_dict(), ensure_ascii=False) + '\n')
        tmp.replace(self.path)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
        self._transactions = []
