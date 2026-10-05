# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for AudiobookOrganizer.exe. Run through build.bat.
#
# A spec rather than command-line flags, because what bloats the executable can only
# be removed here: PyInstaller's Qt hook bundles every DLL Qt might want, whether or
# not this program touches it.
import os

from PyInstaller.utils.hooks import collect_submodules

SRC = SPECPATH

hiddenimports = ['scripts.gui.app_icon']
# mutagen is imported lazily in places, so static analysis does not find all of it.
hiddenimports += collect_submodules('mutagen')

a = Analysis(
    [os.path.join(SRC, 'main.py')],
    pathex=[SRC],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Pillow only draws the .ico at build time (app_icon.write_ico); the program
    # never imports it.
    excludes=['PIL'],
    noarchive=False,
    optimize=0,
)

# Qt files this program never loads.
#   opengl32sw.dll  20 MB software OpenGL fallback - a widgets app does not render
#                   through OpenGL
#   Qt6Pdf.dll      and the qpdf image plugin that pulls it in - no PDFs here
#   translations    Qt's own dialog translations; the interface is English only
UNUSED = ('opengl32sw.dll', 'qt6pdf.dll', 'qpdf.dll')


def wanted(entry):
    name = entry[0].replace('\\', '/').lower()
    if os.path.basename(name) in UNUSED:
        return False
    return '/qt6/translations/' not in name


a.binaries = [entry for entry in a.binaries if wanted(entry)]
a.datas = [entry for entry in a.datas if wanted(entry)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AudiobookOrganizer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(SRC, 'junk', 'build', 'audiobook_organizer.ico')],
)
