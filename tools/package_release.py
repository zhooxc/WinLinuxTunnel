#!/usr/bin/env python3
"""Zip sources for the Linux CLI and for developers.

The Windows app is not this zip. Build it on Windows:

    powershell -ExecutionPolicy Bypass -File tools\\build-windows.ps1

That writes:
  dist/WinLinuxTunnel/            onedir resident app (WinLinuxTunnel.exe)
  dist/WinLinuxTunnel-Setup.exe   single-file installer (runs once)

This script never embeds win/runtime (the old embeddable CPython tree) and
never ships secrets or local state. A Linux CI machine is not expected to
produce the .exe.

Usage: python tools/package_release.py
ASCII comments. Python 3.8+.
"""

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wlt_payload

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
DIST = ROOT / "dist"


def main():
    DIST.mkdir(exist_ok=True)
    out = DIST / ("WinLinuxTunnel-%s-src.zip" % VERSION)
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in wlt_payload.iter_source_files(ROOT):
            rel = path.relative_to(ROOT).as_posix()
            zf.write(path, rel)
            count += 1
    setup = DIST / "WinLinuxTunnel-Setup.exe"
    print("source archive: %s (%d files, %.1f KB)" % (out, count, out.stat().st_size / 1024))
    if setup.exists():
        print("windows installer present: %s (%.1f MB)" % (setup, setup.stat().st_size / 1024 / 1024))
    else:
        print("windows installer not built here.")
        print("On Windows: powershell -ExecutionPolicy Bypass -File tools\\build-windows.ps1")
        print("Users run dist\\WinLinuxTunnel-Setup.exe, not this source zip.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
