#!/usr/bin/env python3
"""Stage the Windows onedir payload zip consumed by WinLinuxTunnel-Setup.

The resident app is a PyInstaller *directory* build (WinLinuxTunnel.exe plus
_internal). This zip is extracted once, at install time, into
%LOCALAPPDATA%\\WinLinuxTunnel. It is not a onefile process that unpacks
itself on every launch.

Usage:
  python tools/wlt_payload.py --onedir dist/WinLinuxTunnel --zip dist/payload.zip

ASCII comments. Python 3.8+.
"""

import argparse
import sys
import zipfile
from pathlib import Path

ROOT_FILES = [
    "README.md", "VERSION", "LICENSE", "NOTICE", "CHANGELOG.md", "DESIGN.md",
]
ROOT_DIRS = ["linux", "docs"]

# Names that must never ship, even if a build machine left them next to sources.
EXCLUDE_DIR_NAMES = {
    ".git", "__pycache__", "jobs", "logs", "keys", ".wlt", "dist", "refs",
    ".pytest_cache", "build", ".venv-build", "runtime", "vendor",
}
EXCLUDE_FILE_NAMES = {
    "wlt.conf", "agent.token", "bridge.env", "tunnels.json",
    "tunnels.audit.jsonl", "agent.lock", "agent.pid",
}
EXCLUDE_SUFFIX = {".pyc", ".lock", ".tmp", ".pem"}

SOURCE_DIRS = ["win", "linux", "tools", "docs"]
SOURCE_FILES = list(ROOT_FILES)


def excluded(rel):
    """True when a repo-relative path must not enter the source archive."""
    parts = set(rel.parts)
    if parts & EXCLUDE_DIR_NAMES:
        return True
    if rel.name in EXCLUDE_FILE_NAMES:
        return True
    if rel.suffix.lower() in EXCLUDE_SUFFIX or rel.suffix.lower() == ".exe":
        return True
    return False


def iter_source_files(root):
    """Files for the source zip (Linux CLI + sources). No runtime, no secrets."""
    root = Path(root)
    for name in SOURCE_FILES:
        path = root / name
        if path.is_file() and not excluded(path.relative_to(root)):
            yield path
    for directory in SOURCE_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if excluded(rel):
                continue
            yield path


def _skip_staged(rel):
    if set(rel.parts) & EXCLUDE_DIR_NAMES:
        return True
    if rel.name in EXCLUDE_FILE_NAMES:
        return True
    if rel.suffix.lower() in EXCLUDE_SUFFIX or rel.suffix.lower() == ".pem":
        return True
    return False


def _write_tree(zf, src_root, dest_prefix=""):
    src_root = Path(src_root)
    for path in sorted(src_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src_root)
        if _skip_staged(rel):
            continue
        arc = (Path(dest_prefix) / rel).as_posix() if dest_prefix else rel.as_posix()
        zf.write(path, arc)


def build_payload_zip(onedir, zip_path, root):
    """Zip the onedir app plus the installer script and the Linux tree.

    Layout inside the zip (extracted directly into the install directory):

      WinLinuxTunnel.exe
      _internal/...
      install-win.ps1
      wlt.conf.example
      linux/...
      docs/...
      README.md VERSION LICENSE NOTICE CHANGELOG.md DESIGN.md
    """
    onedir = Path(onedir)
    root = Path(root)
    zip_path = Path(zip_path)
    exe = onedir / "WinLinuxTunnel.exe"
    if not exe.is_file():
        raise SystemExit("onedir is missing WinLinuxTunnel.exe: %s" % onedir)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_tree(zf, onedir)
        script = root / "win" / "install-win.ps1"
        example = root / "win" / "wlt.conf.example"
        if script.is_file():
            zf.write(script, "install-win.ps1")
        if example.is_file():
            zf.write(example, "wlt.conf.example")
        for name in ROOT_FILES:
            path = root / name
            if path.is_file():
                zf.write(path, name)
        for directory in ROOT_DIRS:
            base = root / directory
            if base.is_dir():
                _write_tree(zf, base, directory)
    return zip_path


def verify_payload_zip(zip_path):
    """Raise SystemExit if the payload is missing required files or has secrets."""
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    required = ["WinLinuxTunnel.exe", "install-win.ps1", "linux/wlt", "VERSION"]
    missing = [item for item in required if item not in names]
    if missing:
        raise SystemExit("payload missing: %s" % ", ".join(missing))
    banned_parts = EXCLUDE_FILE_NAMES | {".pem"}
    bad = []
    for name in names:
        rel = Path(name)
        if rel.name in banned_parts or rel.suffix.lower() == ".pem":
            bad.append(name)
        if "runtime/" in name.replace("\\", "/") or name.startswith("win/runtime"):
            bad.append(name)
    if bad:
        raise SystemExit("payload contains forbidden paths: %s" % ", ".join(bad[:10]))
    return names


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the WinLinuxTunnel onedir payload zip")
    parser.add_argument("--onedir", required=True, help="PyInstaller onedir output folder")
    parser.add_argument("--zip", required=True, dest="zip_path", help="Output zip path")
    parser.add_argument("--root", default="", help="Repo root (default: parent of tools/)")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent
    out = build_payload_zip(args.onedir, args.zip_path, root)
    names = verify_payload_zip(out)
    print("payload %d entries -> %s" % (len(names), out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
