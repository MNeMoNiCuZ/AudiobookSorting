"""What the merge progress bar is allowed to claim.

The bar used to be driven by weighted phases and by ffmpeg's own timestamps, and it
reached 90% within seconds of a long encode starting and stayed there. Then it counted
whole finished chapters, which was honest but stood still for the minutes an encode
spends inside one - while the message beside it counted smoothly up through the audio.

These tests pin what it does now: the denominator is a count of chapters, the numerator
is how much of the measured audio has been through the encoder - so it advances at the
same rate as the percentage in the message - and it never goes backwards.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from scripts import chapter_merge as cm


@pytest.fixture
def sources(tmp_path):
    """Four chapter files, one minute each as far as everything downstream knows."""
    files = []
    for index in range(1, 5):
        path = tmp_path / f'{index:02d}.mp3'
        path.write_bytes(b'\x00' * 64)
        files.append(path)
    return files


def _stub_ffmpeg(monkeypatch, chapters, encode_calls=None):
    """Replace everything that shells out, so only the progress logic is under test."""
    monkeypatch.setattr(cm, 'ffmpeg_available', lambda _exe: True)

    def duration(path, _exe='ffmpeg'):
        # The finished book is as long as its chapters put together; a copy-merge
        # verifies exactly that before calling itself a success.
        return 60.0 * chapters if Path(path).name == 'book.m4b' else 60.0

    monkeypatch.setattr(cm, 'probe_duration', duration)
    monkeypatch.setattr(cm, 'aac_sources', lambda _f: False)
    monkeypatch.setattr(cm, 'choose_bitrate', lambda _f, _b: '64k')
    monkeypatch.setattr(cm, 'common_format', lambda _f, _e: (44100, 2))

    def fake_streaming(command, cancelled, on_time_ms=None):
        # Every stubbed ffmpeg run reports its way through one minute of audio.
        if on_time_ms is not None:
            for ms in (15000, 30000, 45000, 60000):
                on_time_ms(ms)
        output = Path(command[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'\x00' * 4096)
        if encode_calls is not None:
            encode_calls.append(output.name)
        return True, ''

    monkeypatch.setattr(cm, '_run_streaming', fake_streaming)


def test_the_denominator_is_chapters_plus_a_step_either_side(sources, tmp_path,
                                                             monkeypatch):
    _stub_ffmpeg(monkeypatch, len(sources))
    seen = []
    ok, _message = cm.merge_to_m4b(
        sources, tmp_path / 'out' / 'book.m4b',
        on_progress=lambda done, total, msg: seen.append((done, total, msg)))

    assert ok
    assert {total for _done, total, _msg in seen} == {len(sources) + 2}


def test_reading_the_durations_does_not_move_the_bar(sources, tmp_path, monkeypatch):
    """The cheapest phase used to spend a fifth of the bar on itself."""
    _stub_ffmpeg(monkeypatch, len(sources))
    seen = []
    cm.merge_to_m4b(sources, tmp_path / 'out' / 'book.m4b',
                    on_progress=lambda done, total, msg: seen.append((done, msg)))

    reading = [done for done, msg in seen if msg.startswith('Reading chapter')]
    assert reading and set(reading) == {0}


def test_the_bar_finishes_at_the_last_step(sources, tmp_path, monkeypatch):
    _stub_ffmpeg(monkeypatch, len(sources))
    seen = []
    cm.merge_to_m4b(sources, tmp_path / 'out' / 'book.m4b',
                    on_progress=lambda done, total, msg: seen.append(done))

    assert seen[-1] == len(sources) + 2


def test_the_bar_never_goes_backwards(sources, tmp_path, monkeypatch):
    """Chapters encode concurrently and finish out of order."""
    _stub_ffmpeg(monkeypatch, len(sources))
    seen = []
    cm.merge_to_m4b(sources, tmp_path / 'out' / 'book.m4b',
                    on_progress=lambda done, total, msg: seen.append(done))

    assert seen == sorted(seen)


def test_the_bar_moves_inside_a_chapter(sources, tmp_path, monkeypatch):
    """The whole point of the fractional numerator.

    Each stubbed chapter reports four quarters of its own minute, so a bar that only
    counted finished chapters would emit nothing but whole numbers during the encode.
    """
    _stub_ffmpeg(monkeypatch, len(sources))
    seen = []
    cm.merge_to_m4b(sources, tmp_path / 'out' / 'book.m4b',
                    on_progress=lambda done, total, msg: seen.append(done))

    during = [done for done in seen if 1 < done < len(sources) + 1]
    assert any(abs(done - round(done)) > 1e-9 for done in during), (
        'the bar only ever landed on whole chapters')


def test_the_bar_tracks_the_share_of_the_audio_encoded(sources, tmp_path, monkeypatch):
    """Where it ends up per chapter is the audio measure, not a phase weighting."""
    _stub_ffmpeg(monkeypatch, len(sources))
    seen = []
    cm.merge_to_m4b(sources, tmp_path / 'out' / 'book.m4b',
                    on_progress=lambda done, total, msg: seen.append(done))

    # Preparing is step 1 and the chapters span steps 1..n+1, so once every chapter's
    # audio has been through the encoder the bar is at n+1 - and it got there smoothly.
    encoding = [done for done in seen if 1 <= done <= len(sources) + 1]
    assert max(encoding) == pytest.approx(len(sources) + 1)
    assert len(set(encoding)) > len(sources) + 1


def test_a_copy_only_merge_counts_chapters_off_the_timestamps(sources, tmp_path,
                                                              monkeypatch):
    """No per-chapter encode happens on this path - one ffmpeg run does the lot."""
    _stub_ffmpeg(monkeypatch, len(sources))
    monkeypatch.setattr(cm, 'aac_sources', lambda _f: True)

    def whole_book(command, cancelled, on_time_ms=None):
        if on_time_ms is not None:
            # Four one-minute chapters, walked end to end.
            for ms in (60000, 120000, 180000, 240000):
                on_time_ms(ms)
        output = Path(command[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'\x00' * 4096)
        return True, ''

    monkeypatch.setattr(cm, '_run_streaming', whole_book)
    seen = []
    ok, _msg = cm.merge_to_m4b(
        sources, tmp_path / 'out' / 'book.m4b',
        on_progress=lambda done, total, msg: seen.append(done))

    assert ok
    assert seen == sorted(seen)
    assert max(seen) == len(sources) + 2
    # The chapter steps were actually walked, not jumped over.
    assert {2, 3, 4, 5} <= set(seen)
