"""Chapter merging - chapter naming, and the format rules the fast paths depend on.

The format tests here are not fussiness. ffmpeg's concat demuxer joins streams by
copying, and a chapter whose sample rate or channel count differs from the first one is
*dropped without an error*: two 60-second chapters at different rates copy into a
60-second book that looks perfectly finished. Every guard against that is tested.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from scripts import chapter_merge as cm


class FakeInfo:
    def __init__(self, codec='mp4a.40.2', sample_rate=44100, channels=2, length=60.0,
                 bitrate=128000):
        self.codec = codec
        self.sample_rate = sample_rate
        self.channels = channels
        self.length = length
        self.bitrate = bitrate


def fake_mutagen(monkeypatch, by_name):
    """Make mutagen report `by_name[filename] -> FakeInfo` for both entry points."""
    def load(path):
        info = by_name.get(Path(path).name)
        if info is None:
            raise ValueError('unreadable')
        return types.SimpleNamespace(info=info)

    fake_mp4 = types.ModuleType('mutagen.mp4')
    fake_mp4.MP4 = load
    fake_root = types.ModuleType('mutagen')
    fake_root.File = load
    fake_root.mp4 = fake_mp4
    monkeypatch.setitem(sys.modules, 'mutagen', fake_root)
    monkeypatch.setitem(sys.modules, 'mutagen.mp4', fake_mp4)


# ------------------------------------------------------------------ chapter naming

def test_chapter_titles_strip_the_book_name_shared_by_every_file():
    """The bug that produced twelve chapters all called "Rich Man's Sky"."""
    files = [Path(f"{i:02d} - Wil McCarthy - Rich Man's Sky.mp3") for i in range(1, 4)]
    assert cm.chapter_titles(files) == ['Chapter 1', 'Chapter 2', 'Chapter 3']


def test_chapter_titles_keep_what_actually_differs():
    files = [Path('01 - The Arrival.mp3'), Path('02 - The Departure.mp3')]
    assert cm.chapter_titles(files) == ['The Arrival', 'The Departure']


def test_chapter_titles_number_bare_numeric_names():
    assert cm.chapter_titles([Path('01.mp3'), Path('02.mp3')]) == \
        ['Chapter 1', 'Chapter 2']


# ------------------------------------------------------------------ the copy path

def test_aac_sources_accepts_matching_aac_chapters(tmp_path, monkeypatch):
    names = ['a.m4a', 'b.m4a']
    fake_mutagen(monkeypatch, {name: FakeInfo() for name in names})
    files = [tmp_path / name for name in names]
    assert cm.aac_sources(files) is True


def test_aac_sources_refuses_mp3(tmp_path, monkeypatch):
    fake_mutagen(monkeypatch, {'a.mp3': FakeInfo(codec='mp3')})
    assert cm.aac_sources([tmp_path / 'a.mp3']) is False


def test_aac_sources_refuses_alac_in_an_mp4_container(tmp_path, monkeypatch):
    """ALAC lives in .m4a too, and a .m4b cannot hold it - it has to be encoded."""
    fake_mutagen(monkeypatch, {'a.m4a': FakeInfo(codec='alac')})
    assert cm.aac_sources([tmp_path / 'a.m4a']) is False


@pytest.mark.parametrize('differing', [
    {'sample_rate': 22050},
    {'channels': 1},
])
def test_aac_sources_refuses_chapters_that_do_not_share_a_format(
        tmp_path, monkeypatch, differing):
    """The silent-drop case: copying these would lose a chapter without any error."""
    fake_mutagen(monkeypatch, {'a.m4a': FakeInfo(),
                              'b.m4a': FakeInfo(**differing)})
    assert cm.aac_sources([tmp_path / 'a.m4a', tmp_path / 'b.m4a']) is False


def test_aac_sources_refuses_an_unreadable_file(tmp_path, monkeypatch):
    fake_mutagen(monkeypatch, {'a.m4a': FakeInfo()})
    assert cm.aac_sources([tmp_path / 'a.m4a', tmp_path / 'broken.m4a']) is False


def test_aac_sources_refuses_an_empty_list():
    assert cm.aac_sources([]) is False


# -------------------------------------------------------------- segment normalising

def test_common_format_takes_the_highest_of_each(tmp_path, monkeypatch):
    """A mono chapter among stereo ones must not downmix the whole book to mono."""
    fake_mutagen(monkeypatch, {
        'a.mp3': FakeInfo(sample_rate=22050, channels=1),
        'b.mp3': FakeInfo(sample_rate=44100, channels=2),
    })
    assert cm.common_format([tmp_path / 'a.mp3', tmp_path / 'b.mp3']) == (44100, 2)


def test_common_format_falls_back_when_nothing_is_readable(tmp_path, monkeypatch):
    fake_mutagen(monkeypatch, {})
    assert cm.common_format([tmp_path / 'a.mp3']) == (44100, 2)


def test_common_format_never_asks_for_more_than_stereo(tmp_path, monkeypatch):
    fake_mutagen(monkeypatch, {'a.m4a': FakeInfo(channels=6)})
    assert cm.common_format([tmp_path / 'a.m4a'])[1] == 2


# --------------------------------------------------------------------- bitrate

def test_choose_bitrate_keeps_the_best_source_rate(tmp_path, monkeypatch):
    """"same" must not quietly downgrade a 192k book to the spoken-word default."""
    fake_mutagen(monkeypatch, {'a.mp3': FakeInfo(bitrate=128000),
                              'b.mp3': FakeInfo(bitrate=192000)})
    assert cm.choose_bitrate([tmp_path / 'a.mp3', tmp_path / 'b.mp3'], 'same') == '192k'


def test_choose_bitrate_passes_an_explicit_choice_through(tmp_path):
    assert cm.choose_bitrate([tmp_path / 'a.mp3'], '64k') == '64k'


# -------------------------------------------------------------------- metadata

def test_metadata_offsets_are_cumulative():
    text = cm._metadata_text('The Book', 'An Author', ['One', 'Two'], [1000, 2500])
    assert 'title=The Book' in text
    assert 'artist=An Author' in text
    assert 'START=0' in text and 'END=1000' in text
    assert 'START=1000' in text and 'END=3500' in text


def test_metadata_escapes_the_characters_ffmetadata_treats_as_syntax():
    text = cm._metadata_text('A=B;C', '', ['x'], [10])
    assert r'title=A\=B\;C' in text


# ------------------------------------------------------------------ early refusals

def test_merge_refuses_an_empty_list(tmp_path):
    ok, message = cm.merge_to_m4b([], tmp_path / 'out.m4b')
    assert not ok and 'No files' in message


def test_merge_refuses_a_missing_source(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, 'ffmpeg_available', lambda ffmpeg='ffmpeg': True)
    ok, message = cm.merge_to_m4b([tmp_path / 'nope.mp3'], tmp_path / 'out.m4b')
    assert not ok and 'Missing source file' in message


def test_merge_says_so_when_ffmpeg_is_absent(tmp_path, monkeypatch):
    source = tmp_path / 'a.mp3'
    source.write_bytes(b'\x00')
    monkeypatch.setattr(cm, 'ffmpeg_available', lambda ffmpeg='ffmpeg': False)
    ok, message = cm.merge_to_m4b([source], tmp_path / 'out.m4b')
    assert not ok and 'ffmpeg not found' in message
