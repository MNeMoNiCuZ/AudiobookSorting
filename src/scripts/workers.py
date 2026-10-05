"""Qt background workers (#19).

Every network call - lookups, LLM queries - and every filesystem batch runs here
rather than on the GUI thread, so the window never freezes and everything is
cancellable.
"""

from __future__ import annotations

import logging
import traceback
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot

from .models import BookEntry

logger = logging.getLogger(__name__)


class WorkerSignals(QObject):
    """Signals a worker can emit. Must live on a QObject, hence the separate class."""

    started = pyqtSignal()
    # done, total, message. Both counts are floats: a chapter merge reports fractional
    # chapters, so the bar can move at the rate the audio is actually converting
    # instead of jumping once per finished chapter. Whole numbers still arrive intact.
    progress = pyqtSignal(float, float, str)
    entry_started = pyqtSignal(object)       # BookEntry
    entry_progress = pyqtSignal(object, float, str)  # entry, fraction, tier
    entry_done = pyqtSignal(object)          # BookEntry
    message = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal(object)            # summary payload
    cancelled = pyqtSignal()


class CancellableWorker(QRunnable):
    """Base worker with cooperative cancellation."""

    def __init__(self):
        super().__init__()
        self.signals = WorkerSignals()
        self._cancelled = False
        # Set before the terminal signal is emitted, so a handler running *during*
        # that signal can already tell that this job is over. Slot order is otherwise
        # unknowable, and "am I still busy?" must not depend on it.
        self.done = False
        # Shown in the queue menu. Set by each worker to something the user recognises.
        self.label = ''
        # Coarse type, so the window can count "how many identifications are waiting"
        # separately from everything else - that is what the Identify badge shows.
        self.kind = 'job'
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def should_cancel(self) -> bool:
        return self._cancelled

    @pyqtSlot()
    def run(self) -> None:
        try:
            self.signals.started.emit()
            result = self.work()
            self.done = True
            if self._cancelled:
                self.signals.cancelled.emit()
            else:
                self.signals.finished.emit(result)
        except RuntimeError as exc:
            # The receiving widget was destroyed while we were working - closing the
            # Settings page during a model fetch does this. Nobody is listening, so
            # there is nothing to report and nothing wrong.
            if 'has been deleted' not in str(exc):
                raise
            logger.debug('Worker finished after its receiver was closed')
        except Exception as exc:
            self.done = True
            logger.exception('Worker failed')
            try:
                self.signals.error.emit(f'{exc}\n{traceback.format_exc(limit=3)}')
            except RuntimeError:
                pass

    def work(self):
        raise NotImplementedError


class ScanWorker(CancellableWorker):
    """Walk the input tree and produce entries. Fast, but I/O bound on big libraries."""

    def __init__(self, scanner, data_manager, resume: bool = True, resolver=None,
                 dedupe: bool = False, cache=None):
        super().__init__()
        self.scanner = scanner
        self.data_manager = data_manager
        self.resume = resume
        # When given, the offline tiers run as part of the scan - see work().
        self.resolver = resolver
        # Duplicate detection reads bytes off the disk, so it belongs on this thread
        # rather than in the finished handler, which runs on the GUI's.
        self.dedupe = dedupe
        self.cache = cache
        self.label = 'Scan the input folder'

    def work(self) -> Dict:
        self.signals.progress.emit(0, 0, 'Scanning...')
        entries = self.scanner.scan_directory(should_stop=lambda: self._cancelled)
        if self._cancelled:
            return {}

        entries = self.data_manager.merge_scanned(
            entries, resume=self.resume, input_root=self.scanner.input_dir)
        total = len(entries)
        for index, entry in enumerate(entries, start=1):
            if self._cancelled:
                break
            # Tags and filename parsing cost nothing but local I/O, and the answer is
            # already sitting in the file - so a freshly scanned table arrives filled
            # in, and identification is only ever needed for what is genuinely unknown.
            #
            # The test is `resolved`, which the resolver sets and a load that clears a
            # book unsets. It used to be "has no trace", and that was wrong the moment
            # clearing started writing a "Cleared on load" line into the trace: the
            # books that most needed re-reading were the only ones skipped, and they
            # arrived in the table blank, at zero confidence.
            if self.resolver is not None and not entry.resolved:
                try:
                    self.resolver.resolve(entry, tiers=['metadata', 'regex'])
                except Exception:
                    logger.exception('Offline pass failed for %s', entry.entry_id)
            # No entry_done per book: the finished handler puts the whole list in the
            # table in one go, and a row-by-row insert of thousands of books is what
            # froze the window.
            if index % 25 == 0 or index == total:
                self.signals.progress.emit(index, total, f'Read {index} of {total}')

        if self.resolver is not None and not self._cancelled:
            self.resolver.normalize_titles(entries)

        flagged = 0
        if self.dedupe and not self._cancelled:
            self.signals.progress.emit(total, total, 'Checking for duplicate copies...')
            from .dedupe import mark_duplicates
            try:
                flagged = mark_duplicates(entries, cache=self.cache)
            except Exception:
                logger.exception('Duplicate detection failed')
        return {'entries': entries, 'count': total, 'duplicates': flagged}


class ResolveWorker(CancellableWorker):
    """Run the resolution chain over a set of entries, folder by folder."""

    def __init__(self, resolver, entries: List[BookEntry],
                 tiers: Optional[List[str]] = None, only_incomplete: bool = False,
                 fresh: bool = False, skip_done: bool = False):
        super().__init__()
        self.resolver = resolver
        self.entries = list(entries)
        self.tiers = tiers
        self.only_incomplete = only_incomplete
        # Skip the lookup cache - set only for "Run again" on a source's card.
        self.fresh = fresh
        # Pass over tiers a book already finished - set by the Identify button.
        self.skip_done = skip_done
        # The controller uses this to distinguish members of a bundled identify job
        # that are still waiting from the folder currently being processed.
        self.started_entry_ids = set()
        # Books taken back out of the queue before their turn came. Checked as each
        # folder comes up, because the running job has already grouped its entries.
        self.removed_entry_ids = set()
        self._using = ', '.join(tiers) if tiers else 'the configured sources'
        self._set_label()
        self.kind = 'identify'

    def _set_label(self) -> None:
        self.label = (f'Identify {len(self.entries)} '
                      f'book{"" if len(self.entries) == 1 else "s"} using {self._using}')

    def waiting_entry_ids(self) -> set:
        """Books in this job that have not been started yet."""
        return {entry.entry_id for entry in self.entries
                if entry.entry_id not in self.started_entry_ids}

    def remove_entries(self, entry_ids: set) -> int:
        """Take books that have not been started out of this job. Returns how many."""
        dropped = self.waiting_entry_ids() & set(entry_ids)
        if dropped:
            self.removed_entry_ids |= dropped
            self.entries = [e for e in self.entries if e.entry_id not in dropped]
            self._set_label()
        return len(dropped)

    def work(self) -> Dict:
        targets = self.entries
        if self.only_incomplete:
            targets = [e for e in targets if e.missing_fields() or not e.resolved]
        if not targets:
            self.signals.message.emit('Nothing to resolve - all entries are complete.')
            return {'resolved': 0}

        # Group by folder so series-level reasoning can see siblings together (#11).
        groups: Dict[str, List[BookEntry]] = {}
        for entry in targets:
            groups.setdefault(entry.folder, []).append(entry)

        total = len(targets)
        done = 0
        for index, (folder, group) in enumerate(groups.items(), start=1):
            if self._cancelled:
                break
            group = [e for e in group if e.entry_id not in self.removed_entry_ids]
            if not group:
                continue
            name = folder.rsplit('\\', 1)[-1].rsplit('/', 1)[-1]
            for entry in group:
                self.started_entry_ids.add(entry.entry_id)
                self.signals.entry_started.emit(entry)
            # One sentence, same shape every time: what is happening, to what.
            self.signals.progress.emit(done, total, f'Identifying {name}')
            try:
                if len(group) == 1:
                    entry = group[0]
                    self.resolver.resolve(entry, tiers=self.tiers,
                                          fresh=self.fresh, skip_done=self.skip_done,
                                          should_cancel=self.should_cancel,
                                          on_tier=lambda name, done_, total_, e=entry:
                                          self.signals.entry_progress.emit(
                                              e, done_ / total_ if total_ else 0.0,
                                              name))
                    self.signals.entry_done.emit(entry)
                    done += 1
                    self.signals.progress.emit(
                        done, total, f'Identified {entry.value("title") or name}')
                else:
                    # Each book reports done the moment it finishes, not when the
                    # whole folder does.
                    def book_done(entry, folder_name=name):
                        nonlocal done
                        self.signals.entry_done.emit(entry)
                        done += 1
                        self.signals.progress.emit(
                            done, total,
                            f'Identified {entry.value("title") or folder_name}')

                    self.resolver.resolve_folder(group, tiers=self.tiers,
                                                 fresh=self.fresh,
                                                 should_cancel=self.should_cancel,
                                                 on_tier=lambda name, done_, total_, e:
                                                 self.signals.entry_progress.emit(
                                                     e, done_ / total_ if total_ else 0.0,
                                                     name),
                                                 on_entry_done=book_done,
                                                 skip_done=self.skip_done)
            except Exception as exc:
                logger.exception('Failed to resolve %s', folder)
                self.signals.message.emit(f'{name}: {exc}')
                done += sum(1 for entry in group if not entry.resolved)

        if not self._cancelled:
            for entry in self.resolver.normalize_titles(targets):
                self.signals.entry_done.emit(entry)
        return {'resolved': done, 'cancelled': self._cancelled}


class ApplyWorker(CancellableWorker):
    """Apply (or preview) a batch of entries."""

    def __init__(self, file_ops, entries: List[BookEntry], preview: bool = False):
        super().__init__()
        self.file_ops = file_ops
        self.entries = list(entries)
        self.preview = preview
        # Rejected books ride along to be reported as skipped; they are not written.
        count = sum(1 for entry in self.entries if entry.status != 'rejected')
        self.label = (f'{"Preview" if preview else "Write"} {count} '
                      f'book{"" if count == 1 else "s"}')

    def work(self) -> Dict:
        self.file_ops.reset_batch()
        results = []
        total = len(self.entries)

        for index, entry in enumerate(self.entries, start=1):
            if self._cancelled:
                break
            self.signals.progress.emit(index - 1, total,
                                       f'{"Previewing" if self.preview else "Applying"} '
                                       f'{entry.value("title") or entry.entry_id}')
            result = (self.file_ops.preview(entry) if self.preview
                      else self.file_ops.apply_entry(entry))
            results.append(result)

            if not self.preview and result.ok:
                entry.status = 'applied'
                entry.applied_path = str(result.destination)
                self.signals.entry_done.emit(entry)
            elif result.error:
                self.signals.message.emit(result.describe())

        self.signals.progress.emit(len(results), total, 'Done')
        return {'results': results, 'cancelled': self._cancelled,
                'preview': self.preview}


class FunctionWorker(CancellableWorker):
    """Run an arbitrary callable off-thread (chapter merges, undo, tag writes)."""

    def __init__(self, func: Callable, *args, label: str = '', kind: str = 'job',
                 **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.kind = kind
        # A docstring's first line is a decent fallback name for a job, but it is the
        # author talking to the next programmer, not to the person watching the queue.
        # Callers that know what the job *is* pass a label.
        self.label = label or str(
            getattr(func, '__doc__', '') or getattr(func, '__name__', 'Job')
        ).strip().splitlines()[0][:60]

    def work(self):
        names = getattr(getattr(self.func, '__code__', None), 'co_varnames', ())
        if 'on_progress' in names:
            self.kwargs.setdefault(
                'on_progress',
                lambda done, total, msg: self.signals.progress.emit(done, total, msg))
        # A job that can be stopped mid-flight - a chapter merge is minutes of ffmpeg -
        # takes the cancel check the same way, so Cancel actually stops it instead of
        # only marking the result as discarded once it finishes anyway.
        if 'should_cancel' in names:
            self.kwargs.setdefault('should_cancel', self.should_cancel)
        return self.func(*self.args, **self.kwargs)


class WorkerManager:
    """Owns the thread pool, the running worker, and the jobs waiting behind it.

    Long jobs are serialised - two identification runs over the same entries would
    fight each other - but asking for one while another is running queues it instead
    of being refused. You can line work up and keep reviewing while it drains.
    """

    def __init__(self, max_threads: int = 4):
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(max(1, max_threads))
        self.current: Optional[CancellableWorker] = None
        self.queue: List[CancellableWorker] = []
        self.logger = logging.getLogger(__name__)
        # Called with (queued_count) whenever anything about the queue changes, so the
        # window can say "2 jobs waiting" without polling.
        self.on_queue_change: Optional[Callable[[int], None]] = None
        # Called once whenever the queue has drained - the end of a run.
        self.on_idle: Optional[Callable[[], None]] = None
        # A progress bar for the *whole* queue needs a denominator that survives jobs
        # finishing, so the run is counted rather than the list measured: `accepted` is
        # every job taken on since the queue was last empty, `completed` how many of
        # them are done. Both reset to zero the moment everything has drained, so the
        # bar starts from empty on the next batch instead of from 8 of 8.
        self.accepted = 0
        self.completed = 0

    def start(self, worker: CancellableWorker) -> CancellableWorker:
        worker.signals.finished.connect(lambda _: self._clear(worker))
        worker.signals.cancelled.connect(lambda: self._clear(worker))
        worker.signals.error.connect(lambda _: self._clear(worker))

        self.accepted += 1
        if self.current is not None:
            self.queue.append(worker)
        else:
            self._launch(worker)
        self._notify()
        return worker

    def _launch(self, worker: CancellableWorker) -> None:
        self.current = worker
        self.pool.start(worker)

    def _clear(self, worker: CancellableWorker) -> None:
        if self.current is not worker:
            return
        self.current = None
        self.completed += 1
        if self.queue:
            self._launch(self.queue.pop(0))
        else:
            self._reset_counters()
        self._notify()
        if self.current is None and self.on_idle is not None:
            self.on_idle()

    def _reset_counters(self) -> None:
        self.accepted = 0
        self.completed = 0

    def cancel(self) -> None:
        """Cancel the running job and drop anything queued behind it."""
        dropped = len(self.queue)
        self.queue.clear()
        # Jobs that were dropped were still accepted, so the denominator has to lose
        # them or the bar sticks at "3 of 8" for ever.
        self.accepted = max(self.completed, self.accepted - dropped)
        if dropped:
            self._notify()
        self.cancel_current()

    def cancel_current(self) -> None:
        """Cancel only the job that is running, leaving the queue intact.

        This is what the Queue window's "Cancel the running job" means, and it did not
        mean it: it went through cancel(), which binned everything waiting as well. So
        cancelling a two-minute encode also silently threw away the four identifications
        lined up behind it.
        """
        worker = self.current
        if worker is None or worker.done:
            return
        # Cooperative cancellation only lands when the job next checks, which can be
        # the far side of a slow web or model request. So the job is let go now: it is
        # marked over, reported cancelled, and silenced, and the queue moves on while
        # its thread winds down in the background.
        worker.cancel()
        worker.done = True
        worker.signals.cancelled.emit()
        worker.signals.blockSignals(True)

    def status(self) -> Dict:
        """Everything the queue view draws, in one snapshot."""
        current = self.current
        return {
            'running': None if current is None or current.done
            else (getattr(current, 'label', '') or type(current).__name__),
            'queued': self.labels(),
            'completed': self.completed,
            'accepted': self.accepted,
            'identifying': self.count_of_kind('identify'),
        }

    def count_of_kind(self, kind: str) -> int:
        """Jobs of one kind still outstanding, the running one included.

        The running job counts: a badge that drops to zero the instant the last
        identification starts says "nothing is happening" while something is.
        """
        total = sum(1 for worker in self.queue
                    if getattr(worker, 'kind', '') == kind)
        if (self.current is not None and not self.current.done
                and getattr(self.current, 'kind', '') == kind):
            total += 1
        return total

    def labels(self) -> List[str]:
        """A human name per queued job, in the order they will run."""
        return [getattr(w, 'label', None) or type(w).__name__.replace('Worker', '')
                for w in self.queue]

    def remove(self, index: int) -> bool:
        if 0 <= index < len(self.queue):
            self.queue.pop(index)
            self.accepted = max(self.completed, self.accepted - 1)
            self._notify()
            return True
        return False

    def waiting_entry_ids(self) -> set:
        """Books in an identification job that has not reached them yet."""
        workers = list(self.queue)
        if self.current is not None and not self.current.done:
            workers.insert(0, self.current)
        waiting = set()
        for worker in workers:
            if getattr(worker, 'kind', '') == 'identify':
                waiting |= worker.waiting_entry_ids()
        return waiting

    def remove_entries(self, entry_ids) -> int:
        """Take books out of every identification job still waiting to reach them.

        A queued job left with no books is dropped altogether. Returns how many
        books were removed.
        """
        removed = 0
        workers = list(self.queue)
        if self.current is not None and not self.current.done:
            workers.insert(0, self.current)
        for worker in workers:
            if getattr(worker, 'kind', '') != 'identify':
                continue
            removed += worker.remove_entries(entry_ids)
            if not worker.entries and worker in self.queue:
                self.queue.remove(worker)
                self.accepted = max(self.completed, self.accepted - 1)
        if removed:
            self._notify()
        return removed

    def clear_queue(self) -> None:
        """Drop everything waiting, leaving the running job alone."""
        if self.queue:
            self.accepted = max(self.completed, self.accepted - len(self.queue))
            self.queue.clear()
            self._notify()

    def _notify(self) -> None:
        if self.on_queue_change is not None:
            self.on_queue_change(len(self.queue))

    @property
    def busy(self) -> bool:
        if self.queue:
            return True
        return self.current is not None and not self.current.done

    @property
    def queued(self) -> int:
        return len(self.queue)

    def wait(self, timeout_ms: int = 5000) -> bool:
        return self.pool.waitForDone(timeout_ms)
