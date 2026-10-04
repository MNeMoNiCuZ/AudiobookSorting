"""Explorer selections and the confirmation threshold for Open Path."""

from types import SimpleNamespace

import pytest

pytest.importorskip('PyQt6.QtWidgets')

from PyQt6.QtWidgets import QMessageBox

from scripts.gui import main_window, shell_selection
from scripts.models import BookEntry


@pytest.fixture
def opener(monkeypatch):
    opened, messages, questions = [], [], []
    monkeypatch.setattr(main_window, 'open_folder_selection',
                        lambda folder, items: opened.append((folder, items)))

    def confirm(*args):
        questions.append(args)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, 'question', confirm)
    window = SimpleNamespace(show_message=messages.append,
                             settings=SimpleNamespace(display_path=str))
    return window, opened, messages, questions


def test_selected_books_in_shared_folder_are_selected_together(opener, tmp_path):
    window, opened, _, questions = opener
    entries = [BookEntry(folder=str(tmp_path), audio_files=[f'{i}.m4b'],
                         is_multi_book_folder=True)
               for i in range(11)]
    main_window.MainWindow._open_folders(window, entries + entries[:1])
    assert opened == [(tmp_path, [tmp_path / f'{i}.m4b' for i in range(11)])]
    assert questions == []


def test_all_chapters_and_nested_files_are_selected(opener, tmp_path):
    window, opened, _, _ = opener
    main_window.MainWindow._open_folders(window, [
        BookEntry(folder=str(tmp_path),
                  audio_files=['01.mp3', '02.mp3', 'disc/03.mp3'],
                  is_multi_book_folder=True)])
    assert opened == [(tmp_path, [tmp_path / '01.mp3', tmp_path / '02.mp3']),
                      (tmp_path / 'disc', [tmp_path / 'disc/03.mp3'])]


@pytest.mark.parametrize('count', [10, 11])
@pytest.mark.parametrize('accept', [False, True])
def test_confirmation_counts_distinct_folders(opener, tmp_path, monkeypatch,
                                              count, accept):
    window, opened, messages, questions = opener

    def confirm(*args):
        questions.append(args)
        return (QMessageBox.StandardButton.Yes if accept
                else QMessageBox.StandardButton.No)

    monkeypatch.setattr(QMessageBox, 'question', confirm)
    entries = [BookEntry(folder=str(tmp_path / str(i) / 'Book'), audio_files=['book.m4b'])
               for i in range(count)]
    main_window.MainWindow._open_folders(window, entries)
    assert len(questions) == (1 if count > 10 else 0)
    if questions:
        assert questions[0][2] == '11 folders will be opened. Are you sure?'
        assert questions[0][4] == QMessageBox.StandardButton.No
    expected = count if count <= 10 or accept else 0
    assert len(opened) == expected
    assert messages == ([f'Opened {count} folder(s)'] if expected else [])


def test_output_path_and_empty_selection(opener, tmp_path):
    window, opened, messages, questions = opener
    main_window.MainWindow._open_folders(window, [])
    assert opened == messages == questions == []
    main_window.MainWindow._open_folders(window, [
        BookEntry(folder=str(tmp_path / 'input'), applied_path=str(tmp_path / 'output'),
                  audio_files=['book.m4b'])])
    assert opened == [(tmp_path, [tmp_path / 'output'])]


def test_shell_failure_is_reported(opener, tmp_path, monkeypatch):
    window, _, messages, _ = opener

    def fail(*_args):
        raise OSError('selection failed')

    monkeypatch.setattr(main_window, 'open_folder_selection', fail)
    main_window.MainWindow._open_folders(window, [
        BookEntry(folder=str(tmp_path), audio_files=['book.m4b'])])
    assert len(messages) == 1
    assert 'selection failed' in messages[0]


def test_written_book_selects_its_folder_even_if_input_was_shared(opener, tmp_path):
    window, opened, _, _ = opener
    output = tmp_path / 'output'
    output.mkdir()
    for name in ['Renamed 01.mp3', 'Renamed 02.mp3', 'cover.jpg']:
        (output / name).write_bytes(b'')
    main_window.MainWindow._open_folders(window, [
        BookEntry(folder=str(tmp_path / 'input'), applied_path=str(output),
                  audio_files=['01.mp3', '02.mp3'], is_multi_book_folder=True)])
    assert opened == [(tmp_path, [output])]


@pytest.mark.parametrize('audio_files', [['book.m4b'], ['01.mp3', '02.mp3', 'disc/03.mp3']])
def test_dedicated_book_selects_folder_without_disk_access(opener, tmp_path, monkeypatch,
                                                         audio_files):
    from pathlib import Path

    window, opened, _, _ = opener

    def blocked(*_args, **_kwargs):
        raise AssertionError('Choosing selection targets must not scan the disk')

    with monkeypatch.context() as guard:
        for method in ('resolve', 'stat', 'iterdir', 'rglob'):
            guard.setattr(Path, method, blocked)
        main_window.MainWindow._open_folders(window, [
            BookEntry(folder=str(tmp_path / 'Book'), audio_files=audio_files)])
    assert opened == [(tmp_path, [tmp_path / 'Book'])]


def test_dedicated_books_in_same_parent_are_selected_together(opener, tmp_path):
    window, opened, _, questions = opener
    entries = [BookEntry(folder=str(tmp_path / str(i)), audio_files=['book.m4b'])
               for i in range(11)]
    main_window.MainWindow._open_folders(window, entries)
    assert opened == [(tmp_path, [tmp_path / str(i) for i in range(11)])]
    assert questions == []


def test_book_without_audio_selects_its_folder(opener, tmp_path):
    window, opened, _, _ = opener
    main_window.MainWindow._open_folders(window, [BookEntry(folder=str(tmp_path))])
    assert opened == [(tmp_path.parent, [tmp_path])]


class NativeCall:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


@pytest.mark.parametrize('failure', ['', 'parse', 'open'])
@pytest.mark.parametrize('com_result', [0, 1, -2147417850])
def test_shell_selects_every_child_and_releases_native_resources(
        tmp_path, monkeypatch, failure, com_result):
    allocated, freed, opened, uninitialized = [], [], [], []

    def parse(path, _context, output, _flags, _attributes):
        # Pointers above 32 bits catch accidental truncation in the ctypes bindings.
        pointer = 0x100000000 + len(allocated) * 100
        allocated.append((path, pointer))
        output._obj.value = pointer
        return -2147467259 if failure == 'parse' and len(allocated) == 3 else 0

    def select(folder, count, children, flags):
        opened.append((folder.value, list(children), flags))
        assert count == 2
        return -2147467259 if failure == 'open' else 0

    shell = SimpleNamespace(
        SHParseDisplayName=NativeCall(parse),
        ILFindLastID=NativeCall(lambda pidl: pidl.value + 10),
        SHOpenFolderAndSelectItems=NativeCall(select))
    ole = SimpleNamespace(
        CoInitialize=NativeCall(lambda _: com_result),
        CoUninitialize=NativeCall(lambda: uninitialized.append(True)),
        CoTaskMemFree=NativeCall(lambda pidl: freed.append(pidl.value)))
    monkeypatch.setattr(shell_selection.ctypes, 'WinDLL',
                        lambda name: shell if name == 'shell32' else ole)
    items = [tmp_path / 'one.m4b', tmp_path / 'two.m4b']
    if failure:
        with pytest.raises(OSError, match='Windows Shell error'):
            shell_selection.open_folder_selection(tmp_path, items)
    else:
        shell_selection.open_folder_selection(tmp_path, items)
    assert freed == [pointer for _, pointer in allocated]
    assert uninitialized == ([] if com_result < 0 else [True])
    assert [path for path, _ in allocated] == [str(tmp_path), *map(str, items)]
    if failure != 'parse':
        assert opened == [(allocated[0][1], [pointer + 10
                                           for _, pointer in allocated[1:]], 0)]
