"""Audiobook Organizer - entry point for both the GUI and the CLI (#39).

    python main.py                                  launch the GUI
    python main.py --scan --dry-run                 preview what would happen
    python main.py --scan --auto-approve 0.9 --apply
    python main.py --undo-last
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

from scripts.cache import Cache
from scripts.data_manager import DataManager
from scripts.dedupe import mark_duplicates
from scripts.file_operations import FileOperations
from scripts.file_scanner import FileScanner
from scripts.journal import ApplyJournal
from scripts.models import (STATUS_APPROVED, STATUS_PENDING, STATUS_RISKY, BookEntry)
from scripts.paths import PROJECT_ROOT, USER_DIR, clean_temp, migrate_user_files
from scripts.resolver import Resolver
from scripts.run_issues import RunIssues
from scripts.settings import get_settings
from scripts.utils import setup_logging
from scripts.version import APP_VERSION

logger = logging.getLogger('audiobook_organizer')


class Application:
    """Everything the GUI and CLI both need. No Qt imports in here."""

    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        self.cache = Cache(self.settings.get_path('AO_CACHE_DB'),
                           miss_ttl=self.settings.get_int('AO_CACHE_MISS_TTL', 86400))
        self.data = DataManager(
            change_log_mb=self.settings.get_int('AO_CHANGE_LOG_MB', 200))
        # PROJECT_ROOT, not __file__: frozen into a one-file .exe the sources live in
        # a temp folder that is deleted on exit, and the undo journal with them.
        self.journal = ApplyJournal(USER_DIR / 'logs' / 'apply_journal.jsonl')
        self.scanner = FileScanner(str(self.settings.get_path('AO_INPUT_DIR')))
        # Source trouble during one queue run, summarised when the queue drains.
        self.run_issues = RunIssues()
        self.resolver = Resolver(self.settings, cache=self.cache)
        self.resolver.run_issues = self.run_issues
        self.resolver.all_entries = lambda: list(self.data.entries.values())
        self.file_ops = FileOperations(self.settings, journal=self.journal)
        self.file_ops.known_entries = self._known_entries
        self._apply_global_settings()

    def _known_entries(self):
        """Every book on the list, for FileOperations."""
        return self.data.all()

    def _apply_global_settings(self) -> None:
        """Settings that live in a module rather than on an object.

        Filename sanitising is called from a dozen places that have no Settings to
        hand, so the chosen strategy is pushed into the module instead of threaded
        through every call site.
        """
        from scripts.paths import set_illegal_char_mode, set_index_pad
        set_illegal_char_mode(self.settings.get('AO_ILLEGAL_CHARS', 'smart'))
        set_index_pad(self.settings.get_int('AO_INDEX_PAD', 2))
        from scripts.models import set_text_filters
        set_text_filters(self.settings.get('AO_BLOCKED_WORDS'),
                         self.settings.get_bool('AO_STRIP_PARENTHESES', True),
                         self.settings.get_bool('AO_TIDY_PUNCTUATION', True))
        from scripts.quality import set_author_initial_style
        set_author_initial_style(self.settings.get('AO_AUTHOR_INITIAL_STYLE', 'compact'))

    def reload_settings(self) -> None:
        """Re-read .env after the Settings page saved, and rebuild what depends on it."""
        self.settings.reload()
        self.scanner = FileScanner(str(self.settings.get_path('AO_INPUT_DIR')))
        self.resolver = Resolver(self.settings, cache=self.cache)
        self.resolver.run_issues = self.run_issues
        self.resolver.all_entries = lambda: list(self.data.entries.values())
        self.file_ops = FileOperations(self.settings, journal=self.journal)
        self.file_ops.known_entries = self._known_entries
        self.data.change_log_limit = (
            max(1, self.settings.get_int('AO_CHANGE_LOG_MB', 200)) * 1024 * 1024)
        self._apply_global_settings()

    def scan(self) -> List[BookEntry]:
        entries = self.scanner.scan_directory()
        return self.data.merge_scanned(
            entries, resume=self.settings.get_bool('AO_RESUME_SCANS', True),
            input_root=self.scanner.input_dir)

    def close(self) -> None:
        self.data.close()
        self.cache.close()


# ------------------------------------------------------------------------- CLI

def run_cli(args, app: Application) -> int:
    # Unset resolves to the program's own directory, which is the one folder that
    # must not be scanned. Say so and stop rather than walking it.
    if not app.settings.is_set('AO_INPUT_DIR'):
        print('No input folder is set. Put AO_INPUT_DIR in .env, or choose one on '
              'the General tab of the Settings page.')
        return 2
    if args.apply and not app.settings.is_set('AO_OUTPUT_DIR'):
        print('No output folder is set, so --apply has nowhere to write. Set '
              'AO_OUTPUT_DIR in .env first.')
        return 2

    entries = app.scan()
    print(f'Found {len(entries)} entries in '
          f'{app.settings.display_path(app.settings.get_path("AO_INPUT_DIR"))}')
    if not entries:
        return 0

    if not args.no_identify:
        groups = {}
        for entry in entries:
            groups.setdefault(entry.folder, []).append(entry)
        for index, (folder, group) in enumerate(groups.items(), start=1):
            print(f'  [{index}/{len(groups)}] {Path(folder).name}', flush=True)
            app.resolver.resolve_folder(group)
        app.data.mark_dirty()

    if app.settings.get_bool('AO_DETECT_DUPLICATES', True):
        flagged = mark_duplicates(entries, cache=app.cache)
        if flagged:
            print(f'{flagged} duplicate(s) flagged')

    if args.auto_approve is not None:
        approved = 0
        for entry in entries:
            if (entry.status in (STATUS_PENDING, STATUS_RISKY) and entry.is_complete()
                    and entry.confidence() >= args.auto_approve):
                entry.status = STATUS_APPROVED
                approved += 1
        print(f'Auto-approved {approved} entries at >= {args.auto_approve:.0%}')
        app.data.mark_dirty()

    print()
    print(f'{"CONFIDENCE":>10}  {"STATUS":<10} {"AUTHOR":<24} {"SERIES":<22} #    TITLE')
    print('-' * 118)
    for entry in sorted(entries, key=lambda e: (-e.confidence(), e.entry_id)):
        print(f'{entry.confidence():>9.0%}  {entry.status:<10} '
              f'{_fit(entry.value("author"), 24)} {_fit(entry.value("series"), 22)} '
              f'{_fit(entry.value("series_index"), 4)} {entry.value("title")}')

    counts = app.data.stats()
    print()
    print('  '.join(f'{name}: {count}' for name, count in sorted(counts.items())))

    if args.apply or args.dry_run:
        targets = [e for e in entries if e.status == STATUS_APPROVED]
        if not targets:
            print('\nNothing approved, so nothing to apply. '
                  'Use --auto-approve to approve by confidence.')
        else:
            preview = args.dry_run
            print(f'\n{"Previewing" if preview else "Applying"} {len(targets)} entries:')
            app.file_ops.reset_batch()
            failures = 0
            for entry in targets:
                result = (app.file_ops.preview(entry) if preview
                          else app.file_ops.apply_entry(entry))
                print(result.describe())
                if result.error:
                    failures += 1
                elif not preview and result.ok:
                    entry.status = 'applied'
            app.data.mark_dirty()
            if preview:
                print('\nNothing was written. Re-run with --apply (and without '
                      '--dry-run) to perform these operations.')
            elif failures:
                print(f'\n{failures} entr{"y" if failures == 1 else "ies"} failed.')

    app.data.flush()
    return 0


def run_undo(args, app: Application) -> int:
    if args.undo_all:
        count, problems = app.journal.undo_all()
        print(f'Undid {count} transaction(s)')
    else:
        transaction, problems = app.journal.undo_last()
        if transaction is None:
            print('Nothing to undo')
            return 1
        print(f'Undid the apply of {transaction.entry_id}')

    for problem in problems:
        print(f'  ! {problem}')
    return 0


def _fit(text: str, width: int) -> str:
    text = str(text or '')
    if len(text) <= width:
        return text.ljust(width)
    return text[:width - 1] + '…'


# ------------------------------------------------------------------------- GUI

def check_scan_freshness(app: 'Application', window, log: bool = True) -> Optional[dict]:
    """Ask whether the table still describes the input folder, and badge Scan if not.

    Returns what drifted, or None when the two are in step. Cheap enough to run at
    start-up and after every scan: directory listings and sizes, no tag reads.
    """
    if not app.settings.is_set('AO_INPUT_DIR'):
        window.set_scan_stale(None)
        return None
    try:
        drift = app.scanner.compare_to_entries(app.data.all())
    except Exception:
        logger.exception('Could not compare the saved entries to the input folder')
        window.set_scan_stale(None)
        return None

    stale = any(drift.get(key) for key in ('added', 'missing', 'changed', 'unreadable',
                                                'empty', 'loose'))
    if stale and log:
        logger.info('Input folder has drifted from the saved entries: %s',
                    {key: value for key, value in drift.items() if key != 'files'})
        for kind, items in drift.get('files', {}).items():
            for item in items:
                logger.info('  %s: %s', kind, item)
    window.set_scan_stale(drift if stale else None)
    return drift if stale else None


def install_crash_handler() -> None:
    """Turn an unhandled exception into a logged report instead of a vanished window.

    PyQt does not carry an exception out of a slot: it prints it and, on a windowed
    build with no console to print to, the process simply dies. A crash reported as
    "it closed when I clicked a book" is a crash nobody can fix, so anything that
    escapes a slot is written to user/logs/audiobook_organizer.log with its full traceback and
    put on screen with the path to that log. The application keeps running - the
    failure was in one click, not in the program's state.
    """
    import traceback

    from PyQt6.QtWidgets import QMessageBox

    log_path = USER_DIR / 'logs' / 'audiobook_organizer.log'
    shown = {'count': 0}

    def handle(kind, value, tb) -> None:
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, value, tb)
            return
        text = ''.join(traceback.format_exception(kind, value, tb))
        logger.error('Unhandled exception:\n%s', text)

        # Only the first few get a dialog: a fault inside a paint event repeats on every
        # repaint, and a stack of identical message boxes is its own kind of crash.
        shown['count'] += 1
        if shown['count'] > 3:
            return
        try:
            box = QMessageBox(QMessageBox.Icon.Critical, 'Something went wrong',
                              f'{kind.__name__}: {value}\n\n'
                              f'The full details were written to:\n{log_path}\n\n'
                              f'The application is still running, but the action you '
                              f'just took did not complete.')
            box.setDetailedText(text)
            box.exec()
        except Exception:
            pass

    sys.excepthook = handle
    try:
        import threading
        threading.excepthook = lambda args: handle(
            args.exc_type, args.exc_value, args.exc_traceback)
    except Exception:
        pass


def _under(folder: str, root: Path) -> bool:
    """Is `folder` the input root or somewhere inside it?

    Path comparison only - no disk access, so it can be asked about every entry in a
    large library without costing anything.
    """
    try:
        target, base = Path(folder).resolve(), Path(root).resolve()
    except (OSError, TypeError, ValueError):
        return False
    return target == base or base in target.parents


def run_gui(app: Application) -> int:
    from PyQt6.QtWidgets import (QApplication, QDialog, QFileDialog, QMessageBox)

    from scripts.gui import MainWindow, PreviewDialog, SettingsDialog, apply_theme
    from scripts.workers import (ApplyWorker, FunctionWorker, ResolveWorker, ScanWorker,
                                 WorkerManager)

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName('Audiobook Organizer')
    qt_app.setApplicationVersion(APP_VERSION)
    apply_theme(qt_app)
    install_crash_handler()

    # Title bar, alt-tab and the taskbar. On Windows the taskbar groups by AppUserModel
    # ID, and without one set it groups under "python.exe" and shows the Python icon
    # however good ours is - so the ID is claimed before the first window appears.
    from scripts.gui.app_icon import app_icon
    icon = app_icon()
    qt_app.setWindowIcon(icon)
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                'MNeMoNiCuZ.AudiobookOrganizer')
        except Exception as exc:
            logger.debug('Could not set the taskbar identity: %s', exc)

    window = MainWindow(app.settings)
    window.setWindowIcon(icon)
    workers = WorkerManager(app.settings.get_int('AO_THREADS', 4))

    def persist(entry: BookEntry) -> None:
        app.data.update(entry)

    window.entry_changed = persist
    window.preview_provider = lambda entry: app.file_ops.preview(entry)
    # A setting changed directly from the main window, so rebuild the components that
    # keep their own settings-derived state.
    window.settings_changed.connect(app.reload_settings)
    # The queue view reads the manager directly rather than being fed a copy, so the
    # badge, the toolbar count and the Queue window can never disagree about it.
    window.queue_provider = workers.status
    queued_rows = set()

    def sync_queue_rows(_count=0):
        """Keep row markers aligned with identification jobs still waiting."""
        nonlocal queued_rows
        identification_workers = list(workers.queue)
        if (workers.current is not None and not workers.current.done
                and getattr(workers.current, 'kind', '') == 'identify'):
            identification_workers.insert(0, workers.current)
        queued_identifications = [
            (entry, list(getattr(worker, 'tiers', []) or []),
             entry.entry_id not in getattr(worker, 'started_entry_ids', set()))
            for worker in identification_workers
            if getattr(worker, 'kind', '') == 'identify'
            for entry in getattr(worker, 'entries', [])]
        current = {entry.entry_id for entry, _tiers, _waiting
                   in queued_identifications}
        for entry_id in queued_rows - current:
            window.set_row_progress(entry_id, None)
            window.clear_identification_queued(entry_id)
        for entry, tiers, waiting in queued_identifications:
            if not waiting:
                continue
            window.set_row_progress(entry.entry_id, 0.0, 'Queued')
            window.clear_identification_queued(entry.entry_id)
            if len(tiers) == 1:
                window.set_identification_queued(entry, tiers[0])
        queued_rows = current
        window.show_queue(workers.labels())

    workers.on_queue_change = sync_queue_rows

    def run_finished():
        """The queue drained: show what the sources had trouble with, if anything."""
        summary = app.run_issues.summary()
        app.run_issues.reset()
        if summary:
            window.show_run_issues(summary)

    workers.on_idle = run_finished
    window.queue_remove_requested.connect(lambda index: (
        workers.remove(index), window.show_queue(workers.labels()),
        window.show_message('Removed a queued job')))
    window.waiting_entries_provider = workers.waiting_entry_ids

    def dequeue_entries(entry_ids):
        removed = workers.remove_entries(entry_ids)
        window.show_queue(workers.labels())
        if removed:
            window.show_message(
                f'Removed {removed} book' + ('' if removed == 1 else 's')
                + ' from the queue')

    window.dequeue_entries_requested.connect(dequeue_entries)
    window.queue_clear_requested.connect(lambda: (
        workers.clear_queue(), window.show_queue(workers.labels()),
        window.show_message('Cleared the queue')))

    def wire(worker):
        worker.signals.progress.connect(window.show_progress)
        worker.signals.message.connect(window.show_message)
        worker.signals.entry_done.connect(window.upsert_entry)
        worker.signals.error.connect(lambda text: (
            window.set_busy(workers.busy), window.show_message('Failed - see the log'),
            QMessageBox.critical(window, 'Something went wrong', text)))
        worker.signals.cancelled.connect(lambda: (
            window.set_busy(workers.busy), window.show_message('Cancelled')))
        window.set_busy(True)
        started = workers.start(worker)
        window.show_queue(workers.labels())
        if workers.queued:
            count = workers.queued
            window.show_message(
                f'Queued behind {count} other job' + ('' if count == 1 else 's'))
        return started

    def require_path(key: str, what: str) -> bool:
        """Refuse to act on a folder nobody chose, and say where to choose one.

        An unset path resolves to the program's own directory, which is the one
        folder that must never be scanned or written into. Offering the Settings
        page is the whole of the fix, so the question offers it.
        """
        if not app.settings.is_set(key):
            window.show_message(f'No {what} folder is set')
            if QMessageBox.question(
                    window, f'No {what} folder',
                    f'No {what} folder has been chosen yet, so there is nothing to '
                    f'{"read" if what == "input" else "write to"}.\n\nOpen Settings and '
                    f'choose one now?') == QMessageBox.StandardButton.Yes:
                do_settings('General')
            return False

        # A relative path like "input" is resolved against the program's own folder.
        # That is a useful default, but only if it says so: "Scanned 0 entries" for a
        # folder full of books is the least helpful thing this could report, and it is
        # what a typo in the path looked like.
        resolved = app.settings.get_path(key)
        if what == 'input' and not resolved.is_dir():
            typed = app.settings.get(key)
            window.show_message(f'The {what} folder does not exist')
            QMessageBox.warning(
                window, f'No such {what} folder',
                f'The {what} folder is set to:\n\n    {typed}\n\n'
                + (f'which is relative, so it means:\n\n    {resolved}\n\n'
                   if not Path(typed).is_absolute() else '')
                + 'That folder does not exist, so there is nothing to scan.')
            return False
        return True

    # --- load
    def do_load(targets=None, keep=None, list_only=False):
        """The one load action: read the input folder, keeping what was asked for.

        `targets` is the books to load, or None for the whole input folder. `keep` is
        a KeepOptions; the window decides it (from the dialog, or "keep everything"
        when the list is empty and there is nothing to decide). `list_only` puts the
        books in the list and stops there: no tag and filename pass, so nothing is
        processed until you ask for it.
        """
        from scripts.load_options import KeepOptions, apply_keep

        app.reload_settings()
        window.refresh_mode_label()
        if not require_path('AO_INPUT_DIR', 'input'):
            return
        keep = keep or KeepOptions.keep_everything()
        app.data.backup('before-load')

        def cleared_note(plan):
            if not plan.cleared:
                return ''
            return (f', cleared {plan.cleared} value'
                    f'{"" if plan.cleared == 1 else "s"} on {plan.books} book'
                    f'{"" if plan.books == 1 else "s"}')

        def unsaved_note(count):
            """One sentence, not two: the load count and the warning are one event."""
            return (f' - {count} with unsaved changes, highlighted' if count else '')

        # Only some rows: no walk of the folder, just those books read again. The
        # rest of the list is not touched, which is the whole point of the scope.
        if targets:
            plan = apply_keep(targets, keep)
            for entry in targets:
                window.upsert_entry(entry)
                app.data.update(entry)
            waiting = window.flash_unsaved(announce=False, only=targets)
            window.show_message(f'{"Listed" if list_only else "Loading"} '
                                f'{len(targets)} book'
                                f'{"" if len(targets) == 1 else "s"}'
                                + cleared_note(plan) + unsaved_note(waiting))
            if not list_only:
                do_resolve(targets, ['metadata', 'regex'], explicit=False)
            return

        # The whole folder. Clearing happens before the scan, because the scan's own
        # offline pass only re-reads a book that is not marked resolved - which is
        # exactly what apply_keep has just made true of everything it cleared.
        plan = apply_keep(app.data.all(), keep)
        app.data.mark_dirty()
        worker = ScanWorker(app.scanner, app.data,
                            resume=app.settings.get_bool('AO_RESUME_SCANS', True),
                            resolver=None if list_only else app.resolver,
                            dedupe=app.settings.get_bool('AO_DETECT_DUPLICATES', True),
                            cache=app.cache)

        def done(result):
            entries = result.get('entries', [])
            flagged = result.get('duplicates', 0)
            # One rebuild after the duplicate pass, so the table and the library card
            # on the right both reflect the final statuses. The whole list, not only
            # what the scan found: finalized books and books from another folder are
            # kept by a load, so they stay in view.
            if entries:
                entries = app.data.all()
            window.set_entries(entries)
            window.set_busy(workers.busy)
            # The list has just been rebuilt from disk, so both reasons the badge can
            # be lit are answered - asked rather than assumed, because a cancelled
            # load proves nothing.
            window.set_input_folder_changed(None)
            check_scan_freshness(app, window)
            waiting = window.flash_unsaved(announce=False)
            window.show_message(f'Loaded {len(entries)} book'
                                f'{"" if len(entries) == 1 else "s"}'
                                + cleared_note(plan)
                                + (f' ({flagged} identical '
                                   f'{"copy" if flagged == 1 else "copies"})'
                                   if flagged else '')
                                + unsaved_note(waiting))

        worker.signals.finished.connect(done)
        wire(worker)

    # --- identify
    def do_resolve(entries, tiers, explicit=True, fresh=False, skip_done=False):
        entries = window.in_view_order(entries)
        worker = ResolveWorker(app.resolver, entries, tiers=list(tiers), fresh=fresh,
                               skip_done=skip_done)
        if set(tiers) == {'metadata', 'regex'}:
            worker.label = (f'Initial scan of {len(entries)} '
                            f'book{"" if len(entries) == 1 else "s"}')

        def started(entry):
            if explicit:
                entry.explicit_work_pending = True
                app.data.update(entry)
            window.clear_identification_queued(entry.entry_id)
            window.clear_run_failures(entry.entry_id, tiers)
            window.set_row_progress(entry.entry_id, 0.03, 'Identifying')

        worker.signals.entry_started.connect(started)
        worker.signals.entry_done.connect(
            window.finish_identification_entry)
        tier_labels = {'metadata': 'Reading metadata', 'regex': 'Parsing filename',
                       'api': 'Checking book databases',
                       'search': 'Searching the web', 'llm': 'Asking the AI'}
        def progress(entry, fraction, tier):
            # A book in a folder waits while the others ahead of it are looked up.
            # It is not being worked on, so it is drawn queued - not as a running
            # job - and the panel is not told it is running.
            if tier == 'waiting':
                if fraction >= 1.0:
                    window.set_row_progress(entry.entry_id, 1.0, 'Done')
                else:
                    window.set_row_progress(entry.entry_id, 0.0, 'Queued')
                window.why_panel.clear_running(entry)
            else:
                window.update_identification_progress(
                    entry, fraction, tier, tier_labels.get(tier, tier.title()))

        worker.signals.entry_progress.connect(progress)

        def done(result):
            for entry in entries:
                window.finish_identification_entry(entry)
            app.data.mark_dirty()
            window.set_busy(workers.busy)
            window.refresh_stats()
            window.show_message(f'Identified {result.get("resolved", 0)} entries')
            # Anything a tier wanted to write over a manual edit is put to the user
            # now, in one dialog, rather than silently discarded or silently applied.
            window.review_overwrites(entries)

        worker.signals.error.connect(
            lambda _text: [window.finish_identification_entry(entry)
                           for entry in entries])
        def cancelled():
            for entry in entries:
                window.finish_identification_entry(entry)
            # Books that finished before the cancel keep their results.
            app.data.mark_dirty()
            window.refresh_stats()

        worker.signals.cancelled.connect(cancelled)
        worker.signals.finished.connect(done)
        wire(worker)

    # --- apply / preview
    def do_apply(entries, preview):
        # A preview writes nothing and touches no network: it renders the templates
        # against the entries we already have in memory. Putting it behind a running
        # encode meant pressing Preview did nothing visible for two minutes, so it runs
        # inline. Only the jobs that are genuinely long - identification, applying,
        # merging, undo - go through the queue.
        if not require_path('AO_OUTPUT_DIR', 'output'):
            return

        if preview:
            do_preview(entries)
            return

        # The window already asks for confirmation (AO_UI_CONFIRM_APPLY). A second
        # dialog for the same click is just something to dismiss twice.
        worker = ApplyWorker(app.file_ops, entries, preview=preview)

        def done(result):
            app.data.mark_dirty()
            window.set_busy(workers.busy)
            window.refresh_stats()
            results = result.get('results', [])
            for r in results:
                if r.ok:
                    last_plans[r.entry_id] = r
            failures = sum(1 for r in results if r.error)
            skipped = sum(1 for r in results if r.skipped)
            preview = result.get('preview')

            if preview:
                window.show_message(f'Previewed {len(results)} entries')
            else:
                window.show_message(
                    f'Applied {len(results) - failures - skipped} of {len(results)}'
                    + (f', {failures} failed' if failures else '')
                    + (f', {skipped} skipped' if skipped else ''))

            if preview or failures or skipped:
                dialog = PreviewDialog(results, app.settings.get_path('AO_OUTPUT_DIR'),
                                       dry_run=preview, parent=window,
                                       settings=app.settings)
                dialog.settings_requested.connect(do_settings)
                dialog.exec()
                app.reload_settings()

        worker.signals.finished.connect(done)
        wire(worker)

    # The last plan seen for each book, by entry id: the preview, or what Finalize
    # actually did. Once Finalize has moved a book its sources are gone and a fresh
    # preview has nothing to show, so the preview keeps showing this instead.
    last_plans = {}

    def do_preview(entries, fresh=False):
        """Render the preview here and now - no worker, no queue, no waiting."""
        app.file_ops.reset_batch()
        results, stale = [], set()
        for entry in entries:
            result = app.file_ops.preview(entry)
            kept = last_plans.get(entry.entry_id)
            if not fresh and result.error and kept is not None:
                result = kept
                stale.add(entry.entry_id)
            elif result.ok:
                last_plans[entry.entry_id] = result
            results.append(result)
        window.show_message(f'Previewed {len(results)} entries')
        dialog = PreviewDialog(results, app.settings.get_path('AO_OUTPUT_DIR'),
                               dry_run=True, parent=window, settings=app.settings,
                               stale_ids=stale)
        dialog.settings_requested.connect(do_settings)
        refresh = []
        dialog.refresh_requested.connect(lambda: (refresh.append(True), dialog.accept()))
        dialog.exec()
        app.reload_settings()
        if refresh:
            do_preview(entries, fresh=True)

    # --- undo
    def history_labels():
        """One label per undoable apply, oldest first - what the history menu shows."""
        labels = []
        for transaction in app.journal.pending():
            when = time.strftime('%H:%M:%S', time.localtime(transaction.timestamp))
            name = Path(transaction.destination).name or transaction.entry_id
            labels.append(f'{when}  {name}')
        return labels

    window.history_provider = history_labels
    # Undo is greyed out until it has something to reach, and it cannot know that
    # until the provider above is in place - a journal left over from the last run
    # counts.
    window.refresh_action_states()

    def do_undo(index):
        history = history_labels()
        if not history or index < 0 or index >= len(history):
            window.show_message('Nothing to undo')
            return

        count = len(history) - index
        target = (f'the most recent apply' if count == 1
                  else f'the {count} most recent applies')
        if QMessageBox.question(
                window, 'Undo', f'Reverse {target}?\nFiles will be moved back to where '
                                f'they came from.') != QMessageBox.StandardButton.Yes:
            return

        def work():
            undone, problems = app.journal.undo_through(index)
            return f'Undid {undone} apply/applies', problems

        worker = FunctionWorker(work)

        def done(result):
            window.set_busy(workers.busy)
            message, problems = result
            window.show_message(message)
            if problems:
                window.show_report('Undo finished with problems', '\n'.join(problems))

        worker.signals.finished.connect(done)
        wire(worker)

    def finalize_log():
        """Every Finalize transaction, oldest first, for the Finalize log window."""
        pending = app.journal.pending()
        records = []
        for transaction in app.journal.all():
            index = next((i for i, t in enumerate(pending) if t is transaction), None)
            records.append({
                'key': f'{transaction.entry_id}@{transaction.timestamp}',
                'index': index,
                'when': time.strftime('%Y-%m-%d %H:%M:%S',
                                      time.localtime(transaction.timestamp)),
                'name': Path(transaction.destination).name or transaction.entry_id,
                'destination': transaction.destination,
                'undone': transaction.undone,
                'moves': [{'source': m.source, 'destination': m.destination,
                           'operation': m.operation, 'undone': m.undone}
                          for m in transaction.moves],
            })
        return records

    window.finalize_log_provider = finalize_log

    def do_revert(selection):
        if not selection:
            return
        if QMessageBox.question(
                window, 'Revert', 'Move the selected files back to where they came '
                                  'from?') != QMessageBox.StandardButton.Yes:
            return

        def work():
            reverted, problems = app.journal.undo_selected(selection)
            return f'Reverted {reverted} file(s)', problems

        worker = FunctionWorker(work)

        def done(result):
            window.set_busy(workers.busy)
            message, problems = result
            window.show_message(message)
            if problems:
                window.show_report('Revert finished with problems', '\n'.join(problems))

        worker.signals.finished.connect(done)
        wire(worker)

    # --- chapter merge
    def do_merge(plan):
        """One book's chapters into one .m4b, queued like every other long job.

        The name was decided in the merge dialog, from the same template the rest of
        the library is filed under - nothing is asked for here. Progress is reported
        twice: on the toolbar like any job, and as a bar across the book's own row,
        because a two-minute encode of one book out of forty needs to say which one.
        """
        from scripts.chapter_merge import merge_to_m4b

        entry = plan.entry
        files = entry.absolute_files()
        worker = FunctionWorker(
            merge_to_m4b, files, plan.destination,
            label=f'Merge {len(files)} chapters into {plan.destination.name}',
            kind='merge',
            ffmpeg=app.settings.get('AO_FFMPEG_PATH'), bitrate=plan.bitrate,
            title=entry.value('title'), author=entry.value('author'))

        worker.signals.progress.connect(
            lambda done_, total_, message: window.set_row_progress(
                entry.entry_id, (done_ / total_) if total_ else 0.03, message))

        def done(result):
            window.set_row_progress(entry.entry_id, None)
            window.set_busy(workers.busy)
            ok, message = result
            window.show_message(message)
            if not ok:
                QMessageBox.warning(window, 'Merge failed', message)
                return
            _finish_merge(plan)

        worker.signals.error.connect(
            lambda _text: window.set_row_progress(entry.entry_id, None))
        worker.signals.cancelled.connect(
            lambda: window.set_row_progress(entry.entry_id, None))
        worker.signals.finished.connect(done)
        wire(worker)

    def _finish_merge(plan) -> None:
        """Tidy up after a successful merge: delete originals, re-point the entry."""
        entry = plan.entry
        removed = 0
        if plan.delete_originals:
            for path in entry.absolute_files():
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning('Could not delete %s: %s', path, exc)

        if plan.replace_entry and plan.destination.exists():
            # The book is one file now, so the entry has to agree - otherwise the
            # table keeps offering to merge chapters that are gone.
            entry.folder = str(plan.destination.parent)
            entry.audio_files = [plan.destination.name]
            entry.primary_audio = str(plan.destination)
            entry.log('user', f'Merged into {plan.destination.name}'
                              + (f'; deleted {removed} chapter files' if removed
                                 else ''))
            window.upsert_entry(entry)
            app.data.update(entry)
        elif removed:
            window.show_message(f'Merged, and deleted {removed} chapter files')

    # --- chapter split
    def do_split(entry, books):
        """Cut one .m4b into the books named in the split dialog, queued like a merge."""
        from scripts.chapter_split import SplitPart, split_m4b

        source = entry.absolute_files()[0]
        parts = [SplitPart(start=book.chapters[0].start, end=book.chapters[-1].end,
                           destination=book.folder / book.filename,
                           values=book.values, chapters=tuple(book.chapters))
                 for book in books]
        worker = FunctionWorker(
            split_m4b, source, parts,
            label=f'Split {source.name} into {len(parts)} books', kind='merge',
            ffmpeg=app.settings.get('AO_FFMPEG_PATH') or 'ffmpeg')

        worker.signals.progress.connect(
            lambda done_, total_, message: window.set_row_progress(
                entry.entry_id, (done_ / total_) if total_ else 0.03, message))

        def done(result):
            window.set_row_progress(entry.entry_id, None)
            window.set_busy(workers.busy)
            ok, message, written = result
            window.show_message(message)
            if not ok:
                QMessageBox.warning(window, 'Split failed', message)
                return
            _finish_split(entry, books, written)

        worker.signals.error.connect(
            lambda _text: window.set_row_progress(entry.entry_id, None))
        worker.signals.cancelled.connect(
            lambda: window.set_row_progress(entry.entry_id, None))
        worker.signals.finished.connect(done)
        wire(worker)

    def _finish_split(entry, books, written) -> None:
        """List the new books, then ask what happens to the original."""
        import shutil

        from scripts.gui.main_window import _move_to_trash
        from scripts.models import Field, clean_value

        source = entry.absolute_files()[0]
        app.data.backup('before-split')
        added = []
        for book, path in zip(books, written):
            # The original's cover images go with every book cut from it.
            images = []
            for name in entry.image_files:
                try:
                    shutil.copy2(Path(entry.folder) / name, path.parent / name)
                    images.append(name)
                except OSError as exc:
                    logger.warning('Could not copy %s into %s: %s', name, path.parent, exc)
            new = app.scanner._make_entry(path.parent, [path.name], images,
                                          multi_book=False)
            for name, value in book.values.items():
                cleaned = clean_value(name, value)
                if cleaned:
                    setattr(new, name, Field(value=cleaned, source='user', confidence=1.0))
            new.log('user', f'Split from {source.name}, chapters '
                            f'{entry_chapter_range(book)}')
            app.data.add(new)
            window.upsert_entry(new)
            added.append(new)
        app.data.flush()
        logger.info('Split %s into %d books', source, len(added))

        box = QMessageBox(window)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle('Split finished')
        box.setText(f'{source.name} was split into {len(added)} books, now in the '
                    f'list.\n\nWhat should happen to the original?')
        keep = box.addButton('Keep it', QMessageBox.ButtonRole.RejectRole)
        remove = box.addButton('Remove from list', QMessageBox.ButtonRole.AcceptRole)
        trash = box.addButton('Delete file (Recycle Bin)',
                              QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.exec()
        clicked = box.clickedButton()
        # The new books get the local pass - tags and names - straight away, as any
        # book read in does; the values typed in the split dialog are yours and stay.
        do_resolve(added, ['metadata', 'regex'], explicit=False)
        if clicked is remove:
            # Still on disk, but off the list on purpose: kept out of "new" too.
            app.data.add_set_aside(source)
            do_remove([entry.entry_id], f'Split into {len(added)} books; removed '
                                        f'{source.name} from the list')
        elif clicked is trash:
            if not _move_to_trash(source):
                QMessageBox.warning(window, 'Split finished',
                                    f'Could not move {source} to the Recycle Bin. '
                                    f'It stays on the list.')
                return
            folder = Path(entry.folder)
            try:
                # Its folder goes too when nothing but the cover images is left in it.
                leftover = [child for child in folder.iterdir()
                            if child.name not in entry.image_files]
                if (not entry.is_multi_book_folder and not leftover
                        and folder.resolve() != app.scanner.input_dir.resolve()):
                    _move_to_trash(folder)
            except OSError:
                pass
            do_remove([entry.entry_id], f'Split into {len(added)} books; moved '
                                        f'{source.name} to the Recycle Bin')
        else:
            check_scan_freshness(app, window, log=False)
            window.show_message(f'Split {source.name} into {len(added)} books')

    def entry_chapter_range(book) -> str:
        return (f'"{book.chapters[0].title}" to "{book.chapters[-1].title}"'
                if len(book.chapters) > 1 else f'"{book.chapters[0].title}"')

    # --- settings
    def input_folder_changed(previous: str) -> None:
        """The input folder was changed in Settings. Say so, and offer to act on it.

        Nothing about the books already in the list is wrong - they are all still
        exactly where the list says they are - they are simply somewhere nobody is
        pointing at any more. No file comparison can detect that, so it is said here,
        once, at the moment it happens, and the Inputs button carries it
        afterwards.
        """
        from scripts.load_options import KeepOptions, unsaved_entries

        logger.info('Input folder changed: %s -> %s', previous or '(nothing)',
                    app.settings.get('AO_INPUT_DIR'))
        window.set_input_folder_changed(previous or '(nothing)')
        stale = [entry for entry in app.data.all()
                 if not _under(entry.folder, app.scanner.input_dir)]
        if not stale:
            # Nothing is read in on its own: the "new" button beside Inputs offers it.
            # No book is from the old folder, so there is nothing to warn about.
            window.set_input_folder_changed(None)
            check_scan_freshness(app, window)
            return

        from scripts.gui.item_list import books_lines, list_dialog

        unsaved_books = unsaved_entries(stale)
        unsaved = len(unsaved_books)
        load = f'Remove {len(stale)} and load the new folder'
        chosen = list_dialog(
            window, 'Input folder changed',
            f'The input folder is now:\n\n'
            f'    {app.settings.display_path(app.scanner.input_dir)}\n\n'
            f'The list holds {len(stale)} book'
            f'{"" if len(stale) == 1 else "s"} from the old folder. '
            f'Loading the new folder removes them from the list and '
            f'from the saved data, along with everything identified, '
            f'edited and decided for them.'
            + (f'\n\n{unsaved} of them have changes you have not saved '
               f'yet, and those changes will be lost.' if unsaved else '')
            + '\n\nThe books in the new folder are only listed - nothing '
              'is identified until you start it.',
            [(f'Unsaved changes lost ({unsaved}):', books_lines(unsaved_books, files=False)),
             (f'Removed from the list ({len(stale)}):', books_lines(stale, files=False))],
            [load, 'Later'], default=0)

        if chosen != load:
            return
        for entry in stale:
            app.data.remove(entry.entry_id)
        app.data.flush()
        window.set_entries(app.data.all())
        logger.info('Removed %d books from the old input folder', len(stale))
        window.show_message(f'Removed {len(stale)} book'
                            f'{"" if len(stale) == 1 else "s"} from the old folder')
        do_load(None, KeepOptions.keep_everything(), list_only=True)

    def do_settings(tab: str = ''):
        def restyle():
            """Interface settings preview live, before anything is written."""
            window.refresh_toolbar()
            window.apply_ui_settings()

        before = {'input': app.settings.get('AO_INPUT_DIR')}
        dialog = SettingsDialog(app.settings, window, live_preview=restyle)
        if tab:
            dialog.show_tab(tab)

        def saved():
            app.reload_settings()
            window.refresh_mode_label()
            restyle()
            # The naming settings are applied as a value is set, so books already in
            # the list keep the old shape until they are re-cleaned. Do it here, or
            # changing the style looks like it did nothing.
            from scripts.models import renormalize
            renamed = [entry for entry in app.data.all() if renormalize(entry)]
            for entry in renamed:
                app.data.update(entry)
            if renamed:
                window.set_entries(app.data.all())
            window._validate_entries()
            window.show_message('Settings saved to .env'
                                + (f', {len(renamed)} name'
                                   f'{"" if len(renamed) == 1 else "s"} re-cleaned'
                                   if renamed else ''))
            if app.settings.get('AO_INPUT_DIR') != before['input']:
                previous = before['input']
                # Saving twice in one visit to Settings must not ask about the same
                # change twice.
                before['input'] = app.settings.get('AO_INPUT_DIR')
                input_folder_changed(previous)

        dialog.saved.connect(saved)
        dialog.layout_reset.connect(window.reset_layout)
        dialog.exec()
        # Whatever happened - saved, or cancelled and rolled back - the window has to
        # end up matching the settings that are actually in force.
        restyle()
        window.refresh_mode_label()

    def do_clear():
        """Empty the list: every book and everything known about it. Disk is untouched."""
        workers.cancel()
        app.data.backup('before-clear')
        count = len(app.data.all())
        for entry in app.data.all():
            app.data.remove(entry.entry_id)
        app.data.flush()
        window.set_entries([])
        window.set_input_folder_changed(None)
        window.set_scan_stale(None)
        window.show_message(f'Cleared {count} book{"" if count == 1 else "s"} '
                            f'from the list')

    def do_purge(entry_ids):
        """Drop books whose files are all gone from the list. Disk is untouched."""
        app.data.backup('before-remove')
        for entry_id in entry_ids:
            app.data.remove(entry_id)
        app.data.flush()
        window.set_entries(app.data.all())
        check_scan_freshness(app, window, log=False)
        logger.info('Removed %d books with no files left on disk', len(entry_ids))
        window.show_message(f'Removed {len(entry_ids)} book'
                            f'{"" if len(entry_ids) == 1 else "s"} with no files on disk')

    def do_remove(entry_ids, message):
        """Drop the given books from the list, after taking a backup."""
        app.data.backup('before-remove')
        for entry_id in entry_ids:
            app.data.remove(entry_id)
        app.data.flush()
        window.set_entries(app.data.all())
        check_scan_freshness(app, window, log=False)
        logger.info('Removed %d books from the list', len(entry_ids))
        window.show_message(message)

    def do_folderize(books):
        """Move each loose book in the input folder's root into a folder of its own."""
        from scripts import folderize

        if workers.busy:
            QMessageBox.information(
                window, 'Folderize loose books',
                'Wait for the running job to finish first - it may be reading the '
                'files that would move.')
            return
        root = app.settings.get_path('AO_INPUT_DIR')
        app.data.backup('before-folderize')
        moved, error = {}, None
        try:
            moved = folderize.move(Path(root), books)
        except OSError as exc:
            error = exc
            logger.exception('Folderizing loose books stopped')
            # What did move before the failure is still followed below.
            moved = getattr(exc, 'moved', moved)
        relocated = folderize.relocate_entries(app.data.all(), Path(root), moved)
        for old_id, entry in relocated.items():
            app.data.remove(old_id)
            app.data.add(entry)
        app.data.flush()
        window.set_entries(app.data.all())
        check_scan_freshness(app, window, log=False)
        folders = len({str(Path(new).parent) for new in moved.values()})
        logger.info('Folderized %d files into %d folders; %d listed books followed',
                    len(moved), folders, len(relocated))
        window.show_message(f'Moved {len(moved)} loose file{"" if len(moved) == 1 else "s"}'
                            f' into {folders} folder{"" if folders == 1 else "s"}')
        if error is not None:
            QMessageBox.warning(window, 'Folderize stopped',
                                f'Stopped after {len(moved)} files:\n\n{error}')

    def do_restore():
        """Replace the list with one of the backups taken before loads and clears."""
        folder = app.data.save_file.parent / 'backups'
        path, _ = QFileDialog.getOpenFileName(window, 'Restore a backup', str(folder),
                                              'List backups (*.json)')
        if not path:
            return
        if QMessageBox.question(
                window, 'Are you sure?',
                f'Replace the list with this backup?\n\n{Path(path).name}\n\nThe list '
                f'as it is now is backed up first.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        workers.cancel()
        count = app.data.restore(Path(path))
        window.set_entries(app.data.all())
        check_scan_freshness(app, window)
        logger.info('Restored %d entries from %s', count, path)
        window.show_message(f'Restored {count} book{"" if count == 1 else "s"} from '
                            f'{Path(path).name}')

    window.load_requested.connect(do_load)
    window.restore_requested.connect(do_restore)
    window.clear_requested.connect(do_clear)
    window.purge_requested.connect(do_purge)
    window.freshness_check_requested.connect(
        lambda: None if workers.busy else check_scan_freshness(app, window, log=False))
    window.remove_entries_requested.connect(do_remove)
    window.folderize_requested.connect(do_folderize)
    window.resolve_requested.connect(do_resolve)
    window.refresh_requested.connect(
        lambda entries, tiers: do_resolve(entries, tiers, fresh=True))
    window.continue_requested.connect(
        lambda entries, tiers: do_resolve(entries, tiers, skip_done=True))
    window.apply_requested.connect(do_apply)
    window.undo_requested.connect(do_undo)
    window.revert_requested.connect(do_revert)
    window.merge_requested.connect(do_merge)
    window.split_requested.connect(do_split)

    def release_set_aside():
        released = app.data.clear_set_aside()
        check_scan_freshness(app, window, log=False)
        window.show_message(f'{len(released)} set-aside file'
                            f'{"" if len(released) == 1 else "s"} can be found again')

    window.release_set_aside_requested.connect(release_set_aside)

    def do_combine(entries, fields):
        combined = app.data.combine(entries, fields)
        if combined is None:
            window.show_message('Only entries in the same folder can be combined')
            return
        window.show_combined_entry(combined, entries)
        window.show_message(f'Combined {len(entries)} entries into one book '
                            f'({len(combined.audio_files)} files)')

    window.combine_requested.connect(do_combine)
    window.settings_requested.connect(lambda: do_settings())
    window.settings_requested_on_tab.connect(do_settings)
    window.cancel_requested.connect(workers.cancel)
    window.cancel_current_requested.connect(workers.cancel_current)
    # There is no Save button: identification results and review decisions are
    # written continuously (DataManager autosaves on a debounce), and flushed on exit.

    window.show()

    # Show whatever the last session left behind, then look for anything new.
    existing = app.data.all()
    if existing:
        window.set_entries(existing)
        # A saved list describes the input folder as it was when it was written. If
        # files have come or gone since, every decision made from here rests on stale
        # data - so it is checked now, once, and Inputs carries the answer.
        drift = check_scan_freshness(app, window)
        # The other way a saved list can be wrong, and the one no file comparison can
        # see: the input folder was changed to somewhere else entirely, so not one of
        # these books is in it.
        moved = (app.settings.is_set('AO_INPUT_DIR')
                 and not any(_under(entry.folder, app.scanner.input_dir)
                             for entry in existing))
        if moved:
            window.set_input_folder_changed(
                app.settings.display_path(Path(existing[0].folder).parent))
        window.show_message(
            f'Loaded {len(existing)} book'
            f'{"" if len(existing) == 1 else "s"} from the last session - '
            + ('none of them are in the input folder; press Ctrl+R to load it'
               if moved else
               'the input folder has changed since; press Ctrl+R to load it'
               if drift else 'press Ctrl+R to load the input folder'))
    elif app.settings.is_set('AO_INPUT_DIR'):
        # Not loaded on its own: the "new" button beside Inputs offers what is there.
        check_scan_freshness(app, window)
    else:
        # A first run has no input folder yet. The banner above the table says so and
        # links to the page that fixes it; a modal on top of an empty window that the
        # user has not asked anything of yet would be shouting.
        window.show_message('Set an input folder to get started')

    exit_code = qt_app.exec()
    # A load still walking the folder would keep the process alive, invisibly, after
    # the window has closed - and write its results over the next session's.
    workers.cancel()
    app.data.flush()
    return exit_code


# ------------------------------------------------------------------------ main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='main.py', description='Organise a messy audiobook library.')
    parser.add_argument('--scan', action='store_true',
                        help='run headless: scan, identify and report')
    parser.add_argument('--no-identify', action='store_true',
                        help='skip the identification chain (scan only)')
    parser.add_argument('--auto-approve', type=float, metavar='CONF',
                        help='approve every entry at or above this confidence (0-1)')
    parser.add_argument('--apply', action='store_true',
                        help='apply approved entries - writes to the output folder')
    parser.add_argument('--dry-run', action='store_true',
                        help='print what --apply would do, without writing anything')
    parser.add_argument('--undo-last', action='store_true', help='reverse the last apply')
    parser.add_argument('--undo-all', action='store_true', help='reverse every apply')
    parser.add_argument('--input', metavar='DIR', help='override the input folder')
    parser.add_argument('--output', metavar='DIR', help='override the output folder')
    parser.add_argument('--provider', metavar='NAME', help='override the LLM provider')
    parser.add_argument('--log-level', default=None,
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    migrate_user_files()
    settings = get_settings()

    # Command-line overrides win over .env, but are never written back to it.
    if args.input:
        settings.set('AO_INPUT_DIR', args.input)
    if args.output:
        settings.set('AO_OUTPUT_DIR', args.output)
    if args.provider:
        settings.set('AO_PROVIDER', args.provider)

    setup_logging(args.log_level or settings.get('AO_LOG_LEVEL', 'INFO'))

    # Stated at every start-up, because "settings do not save" and "settings save to a
    # file you are not looking at" produce identical symptoms and cannot be told apart
    # without this line. Frozen, PROJECT_ROOT is the folder holding the .exe - so an
    # .exe copied elsewhere reads and writes the .env beside *it*, not the one in the
    # source tree.
    logger.info('Program folder: %s  (frozen=%s)', PROJECT_ROOT,
                getattr(sys, 'frozen', False))
    logger.info('Settings file:  %s  (exists=%s)', settings.env_path,
                settings.env_path.exists())

    # A run that was killed mid-encode leaves its segment files behind. Nothing else
    # is going to clear them, and they are whole chapters of audio.
    stale = clean_temp()
    if stale:
        logger.info('Removed %d leftover working folder(s) from src/junk/temp/', stale)

    app = Application(settings)
    try:
        if args.undo_last or args.undo_all:
            return run_undo(args, app)
        if args.scan:
            return run_cli(args, app)
        return run_gui(app)
    except KeyboardInterrupt:
        print('\nInterrupted')
        return 130
    finally:
        app.close()


if __name__ == '__main__':
    sys.exit(main())
