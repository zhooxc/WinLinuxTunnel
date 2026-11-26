#!/usr/bin/env python3
"""Package the portable zero-dependency zip (second release form).

Alongside the PyInstaller installer (build-windows.ps1), this produces an
extract-and-run tree with a bundled python.org embeddable CPython + Tk:
for machines where a software-whitelist policy blocks packed executables,
or for users who prefer a green portable layout. Same sources, same
features; only the runtime form differs.

Prerequisite: win/runtime built by tools/build-runtime.ps1.

Usage:
  powershell -ExecutionPolicy Bypass -File tools\\build-runtime.ps1
  python tools/package_portable.py

Output: dist/WinLinuxTunnel-<VERSION>-portable.zip
The zip ships win/start-gui-portable.vbs (double-click entry) and
win/start-agent-portable.cmd. Secrets and local state never ship
(same allowlist as wlt_payload).

ASCII comments. Python 3.8+.
"""

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wlt_payload

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
RUNTIME = ROOT / "win" / "runtime"
OUT = ROOT / "dist" / ("WinLinuxTunnel-%s-portable.zip" % VERSION)


def main():
    if not (RUNTIME / "python.exe").is_file():
        raise SystemExit(
            "win/runtime missing. Build it first:\n"
            "  powershell -ExecutionPolicy Bypass -File tools\\build-runtime.ps1")
    count = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        # Sources: win (without runtime), linux, tools, docs + root docs.
        # The portable launchers live in win/ and ride along here.
        for path in wlt_payload.iter_source_files(ROOT):
            rel = path.relative_to(ROOT).as_posix()
            zf.write(path, rel)
            count += 1
        # Tests are not in wlt_payload.SOURCE_DIRS; ship them for verification.
        tests = ROOT / "tests"
        if tests.is_dir():
            for path in sorted(tests.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(ROOT)
                if wlt_payload.excluded(rel):
                    continue
                zf.write(path, rel.as_posix())
                count += 1
        # Bundled runtime: the one tree excluded() deliberately drops.
        for path in sorted(RUNTIME.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            if "__pycache__" in rel.parts:
                continue
            zf.write(path, rel.as_posix())
            count += 1
    # Safety re-check: unique names, no secrets, required entries present.
    with zipfile.ZipFile(OUT) as zf:
        names = zf.namelist()
    if len(names) != len(set(names)):
        raise SystemExit("duplicate entries in portable zip")
    bad = [n for n in names
           if Path(n).name in wlt_payload.EXCLUDE_FILE_NAMES
           or n.lower().endswith(".pem")]
    if bad:
        raise SystemExit("forbidden entries: %s" % bad[:5])
    for required in ("win/runtime/python.exe", "win/wlt_main.py",
                     "win/start-gui-portable.vbs", "linux/wlt", "VERSION"):
        if required not in names:
            raise SystemExit("portable zip missing: %s" % required)
    print("portable zip: %s (%d files, %.1f MB)"
          % (OUT, count, OUT.stat().st_size / 1048576))
    return 0


if __name__ == "__main__":
    sys.exit(main())