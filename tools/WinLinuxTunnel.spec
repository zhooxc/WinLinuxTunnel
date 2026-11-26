# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir spec for the resident WinLinuxTunnel app.

Build with tools/build-windows.ps1 (do not use --onefile for this exe).
console=False: the GUI and `--agent` are both console-less.
UPX is off: packing the bootloader increases antivirus false positives.
"""

import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
WIN = os.path.join(ROOT, "win")

a = Analysis(
    [os.path.join(WIN, "wlt_main.py")],
    pathex=[WIN],
    binaries=[],
    datas=[
        (os.path.join(WIN, "wlt.ico"), "."),
        (os.path.join(WIN, "wlt.png"), "."),
    ],
    hiddenimports=[
        "wlt_agent",
        "wlt_gui",
        "wlt_jobs",
        "wlt_desktop",
        "wlt_tray",
        "wlt_launch",
        "tkinter",
        "tkinter.ttk",
        "tkinter.messagebox",
        "tkinter.filedialog",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "numpy", "PIL", "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WinLinuxTunnel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(WIN, "wlt.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WinLinuxTunnel",
)
