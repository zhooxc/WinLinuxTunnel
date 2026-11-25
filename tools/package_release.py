#!/usr/bin/env python3
"""Package the wlt release zip.

Zero-dependency when win/runtime/python.exe is present (built by
tools/build-runtime.ps1). Without it, produces a source-only zip and warns
that the target machine needs Python. Never ships secrets or runtime state.

Usage: python tools/package_release.py
ASCII comments. Python 3.8+.
"""

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
DIST = ROOT / "dist"

INCLUDE_DIRS = ["win", "linux", "tools", "docs"]
INCLUDE_FILES = ["README.md", "VERSION", "LICENSE", "NOTICE", "CHANGELOG.md", "DESIGN.md"]

EXCLUDE_PARTS = {".git", "__pycache__", "jobs", "logs", "keys", ".wlt", "dist", "refs", ".pytest_cache"}
EXCLUDE_NAMES = {"wlt.conf", "agent.token", "bridge.env", "tunnels.json",
                 "tunnels.audit.jsonl", "agent.lock"}
EXCLUDE_SUFFIX = {".pyc", ".lock", ".tmp"}


def excluded(rel):
    if set(rel.parts) & EXCLUDE_PARTS:
        return True
    if rel.name in EXCLUDE_NAMES:
        return True
    if rel.suffix in EXCLUDE_SUFFIX:
        return True
    return False


def main():
    runtime = ROOT / "win" / "runtime" / "python.exe"
    zero_dep = runtime.exists()
    if not zero_dep:
        print("WARNING: win/runtime/python.exe absent -> source-only zip "
              "(target machine will need Python). Run tools/build-runtime.ps1 first "
              "for a zero-dependency package.", file=sys.stderr)
    DIST.mkdir(exist_ok=True)
    out = DIST / f"WinLinuxTunnel-{VERSION}.zip"
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in INCLUDE_DIRS:
            base = ROOT / d
            if not base.exists():
                continue
            for p in sorted(base.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(ROOT)
                if excluded(rel):
                    continue
                zf.write(p, rel.as_posix())
                count += 1
        for f in INCLUDE_FILES:
            p = ROOT / f
            if p.exists():
                zf.write(p, f)
                count += 1
    size = out.stat().st_size
    print(f"packaged {count} files -> {out}")
    print(f"size: {size / 1024 / 1024:.1f} MB  zero-dependency: {zero_dep}")


if __name__ == "__main__":
    main()