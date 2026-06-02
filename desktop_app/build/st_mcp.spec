# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for ST MCP Desktop App.

Produces: dist/ST_MCP_Launcher.exe  (single-file, no console)

Run with:
    pyinstaller build\st_mcp.spec
from the desktop_app\ directory.
"""

import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH).parent  # desktop_app\build\ → desktop_app\

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # Include version.py as data (updater rewrites it at runtime)
        (str(ROOT / "version.py"),   "."),
        (str(ROOT / "updater.py"),   "."),
    ],
    hiddenimports=[
        "customtkinter",
        "pystray",
        "PIL._tkinter_finder",
        "PIL.Image",
        "PIL.ImageDraw",
        "PIL.ImageFont",
        "packaging.version",
        "requests",
        "tkinter",
        "tkinter.ttk",
        "version",
        "updater",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ST_MCP_Launcher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # No console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,               # Add icon path here once you have one: "assets\\icon.ico"
    version=None,            # Add version file here: "build\\version_info.txt"
)
