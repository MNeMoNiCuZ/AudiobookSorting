"""What a load keeps, and what it throws away.

Loading the input folder is one action. On an empty list it is simply a read; on a
list you have already worked on it overwrites that work, and *what survives* is the
only question worth asking. So there is one answer to it, in one place: a KeepOptions
telling the load which values live, and the two functions that count and apply it.

Counting and applying share the same rule on purpose - the dialog's "31 values will be
cleared" is produced by the same code that then clears them, so it cannot be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Dict, Iterable, List, Optional

from .models import (IDENTITY_FIELDS, STATUS_APPLIED, STATUS_APPROVED,
                     STATUS_PENDING, BookEntry, Field)


@dataclass
class KeepOptions:
    """What survives a load. Every flag is phrased as something kept, never lost."""

    # Values you typed yourself - Field.source == 'user'. Ticked, nothing you wrote by
    # hand is ever lost to a load, whatever the threshold below says.
    manual: bool = True
    # Keep any value at least this confident, 0-100. 0 keeps everything, 100 keeps only
    # what is certain (which in practice is again your own edits).
    above: int = 75
    # Keep Approved / Rejected. Unticked, every row a load touches returns to Pending.
    decisions: bool = True

    @classmethod
    def keep_everything(cls) -> 'KeepOptions':
        """The non-destructive load: nothing is cleared, only disk facts refresh."""
        return cls(manual=True, above=0, decisions=True)

    def keeps_everything(self) -> bool:
        return self.manual and self.above <= 0 and self.decisions

    def keeps(self, field: Field) -> bool:
        """Does this value survive?"""
        if field.is_empty():
            return True                      # nothing there to throw away
        if self.manual and field.source == 'user':
            return True
        return round(field.confidence * 100) >= self.above


@dataclass
class Tally:
    """One field's fate across the books being loaded."""

    kept: int = 0
    cleared: int = 0


@dataclass
class LoadPlan:
    """What applying a KeepOptions to a set of entries would do."""

    books: int = 0            # books that would lose at least one value
    cleared: int = 0          # values that would be cleared
    kept: int = 0             # values that would survive
    unreviewed: int = 0       # books that would return to Pending
    skipped_applied: int = 0  # books left alone because they are already saved
    # The same counts split by field, because "31 values" is a number you cannot act
    # on and "every series number goes" is one you can.
    per_field: Dict[str, Tally] = dataclass_field(default_factory=dict)

    def tally(self, name: str) -> Tally:
        return self.per_field.setdefault(name, Tally())

    def is_destructive(self) -> bool:
        return bool(self.cleared or self.unreviewed)


def is_applied(entry: BookEntry) -> bool:
    """Already written to the output folder, so a load leaves it alone.

    Clearing the author of a book that has been filed under that author leaves the
    table disagreeing with the disk, and undo reads the entry to find its way back.
    """
    return entry.status == STATUS_APPLIED or bool(entry.applied_path)


def _assess(entry: BookEntry, keep: KeepOptions,
            plan: LoadPlan) -> Optional[List[str]]:
    """Count one book into `plan` and name the fields it loses.

    The single place where "what happens to this book" is decided, so counting it and
    doing it cannot disagree. None means the book is already saved and untouchable.
    """
    if is_applied(entry):
        plan.skipped_applied += 1
        return None

    losing = []
    for name in IDENTITY_FIELDS:
        value = entry.get_field(name)
        if not keep.keeps(value):
            losing.append(name)
            plan.cleared += 1
            plan.tally(name).cleared += 1
        elif not value.is_empty():
            plan.kept += 1
            plan.tally(name).kept += 1
        else:
            plan.tally(name)  # an empty field still gets a row in the table
    if losing:
        plan.books += 1
        if entry.status != STATUS_PENDING and not keep.decisions:
            plan.unreviewed += 1
    return losing


def plan_load(entries: Iterable[BookEntry], keep: KeepOptions) -> LoadPlan:
    """Count what `apply_keep` would do, without doing any of it."""
    plan = LoadPlan()
    for entry in entries:
        _assess(entry, keep, plan)
    return plan


def apply_keep(entries: Iterable[BookEntry], keep: KeepOptions) -> LoadPlan:
    """Throw away everything `keep` does not protect. Returns what was done.

    An entry that loses nothing is not touched at all - not its trace, not its status -
    so a load with everything ticked is exactly the old resumable scan.
    """
    plan = LoadPlan()
    for entry in entries:
        losing = _assess(entry, keep, plan)
        if not losing:
            continue

        for name in losing:
            setattr(entry, name, Field())

        # The trace and the evidence explain values that no longer exist, so they go
        # with them. Cleared first, then re-earned by the offline pass this load runs.
        entry.trace = []
        entry.evidence = {}
        entry.raw_tags = {}
        entry.pending_overwrites = []
        entry.quality_penalties = {}
        entry.warnings = []
        entry.resolved = False
        if not keep.decisions and entry.status != STATUS_PENDING:
            entry.status = STATUS_PENDING  # already counted by _assess
        entry.log('user', f'Cleared on load: {", ".join(losing)}')
    return plan


def unsaved_entries(entries: Iterable[BookEntry]) -> List[BookEntry]:
    """Books carrying work that has not been written to the output folder yet.

    "Saved" is what the Save button does - files moved into place - so unsaved means
    a book you typed into, or approved, that is still only a row in this table.
    Rejected rows are decided but are never written anywhere, so they are not waiting
    on anything and do not count.
    """
    out = []
    for entry in entries:
        if is_applied(entry):
            continue
        typed = any(entry.get_field(name).source == 'user'
                    for name in IDENTITY_FIELDS)
        if typed or entry.status == STATUS_APPROVED:
            out.append(entry)
    return out
