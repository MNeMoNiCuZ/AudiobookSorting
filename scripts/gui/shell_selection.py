"""Open Explorer with all requested items selected in their containing folder."""

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import List


def open_folder_selection(folder: Path, items: List[Path]) -> None:
    """Use the Shell selection API, which supports multiple items in one window."""
    shell = ctypes.WinDLL('shell32')
    ole = ctypes.WinDLL('ole32')
    pointer = ctypes.c_void_p
    hresult = ctypes.c_long
    uint = ctypes.c_uint32
    shell.SHParseDisplayName.argtypes = [
        ctypes.c_wchar_p, pointer, ctypes.POINTER(pointer), uint, pointer]
    shell.SHParseDisplayName.restype = hresult
    shell.ILFindLastID.argtypes = [pointer]
    shell.ILFindLastID.restype = pointer
    shell.SHOpenFolderAndSelectItems.argtypes = [
        pointer, uint, ctypes.POINTER(pointer), uint]
    shell.SHOpenFolderAndSelectItems.restype = hresult
    ole.CoInitialize.argtypes = [pointer]
    ole.CoInitialize.restype = hresult
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoTaskMemFree.argtypes = [pointer]
    ole.CoTaskMemFree.restype = None

    def check(result: int) -> None:
        if result < 0:
            raise OSError(f'Windows Shell error 0x{result & 0xffffffff:08X}')

    initialized = ole.CoInitialize(None)
    # Qt may have already initialized this thread with a different apartment model.
    if initialized != -2147417850:  # RPC_E_CHANGED_MODE
        check(initialized)
    allocated = []

    def parse(path: Path):
        pidl = pointer()
        result = shell.SHParseDisplayName(str(path), None, ctypes.byref(pidl), 0, None)
        if pidl.value:
            allocated.append(pidl)
        check(result)
        if not pidl.value:
            raise OSError(f'Could not locate {path}')
        return pidl

    try:
        if not items:
            # With zero children the API selects the folder in its parent.
            check(shell.SHOpenFolderAndSelectItems(parse(folder), 0, None, 0))
            return
        folder_pidl = parse(folder)
        children = (pointer * len(items))(
            *(shell.ILFindLastID(parse(item)) for item in items))
        check(shell.SHOpenFolderAndSelectItems(folder_pidl, len(items), children, 0))
    finally:
        for pidl in allocated:
            ole.CoTaskMemFree(pidl)
        if initialized >= 0:
            ole.CoUninitialize()
