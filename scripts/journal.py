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
        # Load the cache before writing, or the first read would pick up this line
        # from the file and the append below would list it twice.
        transactions = self.all()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(transaction.to_dict(), ensure_ascii=False) + '\n')
        transactions.append(transaction)

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

    def undo(self, transaction: Transaction,
             only: Optional[List[int]] = None) -> List[str]:
        """Reverse one transaction, or just the moves at the indices in `only`.

        Returns a list of human-readable problems. The transaction counts as undone
        once every one of its moves is.
        """
        problems: List[str] = []

        # Reverse order, so files come back before their directories are removed.
        for index in reversed(range(len(transaction.moves))):
            move = transaction.moves[index]
            if move.undone or (only is not None and index not in only):
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

        transaction.undone = all(move.undone for move in transaction.moves)
        self._rewrite()
        return problems

    def undo_selected(self, selection: Dict[int, Optional[List[int]]]) -> tuple:
        """Revert chosen transactions, or chosen files within them, in any order.

        `selection` maps an index into ``pending()`` to the move indices to revert,
        or None for the whole transaction. A later transaction that moved one of the
        selected files onward is reverted first (that file only), otherwise the file
        would not be where the earlier transaction expects to find it.
        Returns (files reverted, problems).
        """
        pending = self.pending()
        wanted: Dict[int, set] = {}
        for index, moves in selection.items():
            if 0 <= index < len(pending):
                transaction = pending[index]
                wanted[index] = set(range(len(transaction.moves))
                                    if moves is None else moves)

        # Pull in later moves that took a selected file further. Ascending, so a
        # move pulled in here is itself checked when its own index comes round.
        for index in range(len(pending)):
            for position in list(wanted.get(index, ())):
                move = pending[index].moves[position]
                for later in range(index + 1, len(pending)):
                    for other_position, other in enumerate(pending[later].moves):
                        if (not other.undone and other.operation == 'move'
                                and Path(other.source) == Path(move.destination)):
                            wanted.setdefault(later, set()).add(other_position)

        reverted, problems = 0, []
        for index in sorted(wanted, reverse=True):
            transaction = pending[index]
            before = sum(1 for m in transaction.moves if m.undone)
            problems.extend(self.undo(transaction, only=sorted(wanted[index])))
            reverted += sum(1 for m in transaction.moves if m.undone) - before
        return reverted, problems

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
