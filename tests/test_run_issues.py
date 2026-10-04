"""A source that keeps refusing is switched off for the rest of the queue run,
and the run's problems are offered back as a notice you can re-queue from."""

from __future__ import annotations

import os

import pytest

from scripts import run_issues as run_issues_module
from scripts.api_query import BookAPIClient
from scripts.run_issues import RunIssues
from tests.conftest import FakeResponse


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(run_issues_module, 'BACKOFF_START', 0.0)
    monkeypatch.setattr('time.sleep', lambda _s: None)


def test_a_throttling_database_is_switched_off_for_the_rest_of_the_run(
        monkeypatch, fast):
    import requests
    calls = []

    def get(url, **_kwargs):
        calls.append(url)
        response = FakeResponse({}, status_code=429)
        response.content = b'{}'
        return response

    monkeypatch.setattr(requests, 'get', get)
    issues = RunIssues()
    client = BookAPIClient(sources=['openlibrary'])
    client.run_issues = issues

    for index in range(5):
        client.search({'title': f'Book {index}', 'author': 'Someone'})
        for source, why in client.last_errors.items():
            issues.failed(f'api:{source}', f'e{index}', why)
        for source in client.last_skipped:
            issues.skipped(f'api:{source}', f'e{index}')

    asked = len(calls)
    assert client.last_skipped == ['openlibrary']
    client.search({'title': 'One more', 'author': 'Someone'})
    assert len(calls) == asked, 'a switched-off source must not be asked again'

    [row] = issues.summary()
    assert row['disabled'] and row['tiers'] == ['api:openlibrary']
    assert row['failed'] == ['e0', 'e1', 'e2']
    assert row['skipped'] == ['e3', 'e4']


def test_a_quota_refusal_switches_off_at_once(fast):
    issues = RunIssues()
    issues.limited('search:brave', 'key rejected', permanent=True)
    assert issues.is_disabled('search:brave')


def test_a_success_resets_the_streak(fast):
    issues = RunIssues()
    issues.limited('api:librivox', 'rate-limited')
    issues.limited('api:librivox', 'rate-limited')
    issues.ok('api:librivox')
    issues.limited('api:librivox', 'rate-limited')
    assert not issues.is_disabled('api:librivox')


def test_the_notice_selects_and_requeues_the_affected_books(qt_app, settings):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from scripts.gui.main_window import MainWindow
    from scripts.models import BookEntry

    window = MainWindow(settings)
    window.show()
    books = [BookEntry(folder=f'/lib/{n}', audio_files=[f'{n}.mp3']) for n in 'abc']
    window.set_entries(books)
    ids = [b.entry_id for b in books]
    window.show_run_issues([{
        'key': 'api:googlebooks', 'label': 'Google Books', 'message': 'rate-limited',
        'disabled': True, 'failed': [ids[0]], 'skipped': [ids[1]],
        'tiers': ['api:googlebooks']}])
    assert window._run_issues_panel.isVisible()

    queued = []
    window.resolve_requested.connect(lambda e, t: queued.append((e, t)))
    window._run_issues_panel.requeue_requested.emit(['api:googlebooks'])
    [(entries, tiers)] = queued
    assert {e.entry_id for e in entries} == set(ids[:2])
    assert tiers == ['api:googlebooks']

    window._select_run_issue(['api:googlebooks'])
    assert {e.entry_id for e in window.selected_entries()} == set(ids[:2])

    # Identifying a book again with that source takes it off the notice.
    window.clear_run_failures(ids[0], ['api'])
    window.clear_run_failures(ids[1], ['api:googlebooks'])
    assert not window._run_issue_rows()
    assert not window._run_issues_panel.isVisible()
    window.close()
