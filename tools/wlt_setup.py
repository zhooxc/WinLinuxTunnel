#!/usr/bin/env python3
"""Install-time wrapper for the WinLinuxTunnel onedir build.

End users run WinLinuxTunnel-Setup.exe (a PyInstaller *onefile* of this
script, with payload.zip embedded). It runs once:

  1. Extracts the onedir payload into %LOCALAPPDATA%\\WinLinuxTunnel
     (WinLinuxTunnel.exe stays there; it does not unpack on later launches).
  2. Creates Start Menu and desktop shortcuts to WinLinuxTunnel.exe.
  3. Registers current-user autostart of "WinLinuxTunnel.exe --agent".
  4. Launches the GUI.

No admin rights. Config and state stay under %USERPROFILE%\\.wlt, not in
the install directory.

Dev / tests:
  python tools/wlt_setup.py --payload dist/payload.zip --dest <dir> --no-launch

ASCII comments. User-facing messages are Chinese. Python 3.8+.
"""

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def _zip_mode(info):
    return (info.external_attr >> 16) & 0o170000


def _safe_target(dest, name):
    """Resolve a zip member under dest, or raise ValueError (zip-slip)."""
    raw = name.replace("\\", "/").strip()
    if not raw or raw.endswith("/"):
        return None
    if raw.startswith("/") or raw.startswith("../") or "/../" in ("/" + raw):
        raise ValueError("unsafe path in payload: %s" % name)
    if len(raw) >= 2 and raw[1] == ":":
        raise ValueError("unsafe path in payload: %s" % name)
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError("unsafe path in payload: %s" % name)
    dest = dest.resolve()
    target = dest.joinpath(*parts).resolve()
    if target != dest and dest not in target.parents:
        raise ValueError("unsafe path in payload: %s" % name)
    return target


def extract_zip(zip_path, dest):
    """Extract zip_path into dest. Refuses symlinks and paths that escape dest."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if _zip_mode(info) == stat.S_IFLNK:
                raise ValueError("symlink in payload refused: %s" % info.filename)
            target = _safe_target(dest, info.filename)
            if target is None:
                # Directory entry. Still reject traversal in the name.
                raw = info.filename.replace("\\", "/").strip().rstrip("/")
                if raw:
                    _safe_target(dest, raw + "/x")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return dest


def _can_replace(path):
    """False when a running image holds the file open (Windows)."""
    flags = os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return False
    os.close(fd)
    return True


def copy_tree(staged, dest):
    """Copy staged files onto dest. Leave dest in place if the exe is locked."""
    staged = Path(staged)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    exe = dest / "WinLinuxTunnel.exe"
    if exe.exists() and not _can_replace(exe):
        raise RuntimeError(
            "WinLinuxTunnel 正在运行，无法覆盖程序文件。"
            "请先从托盘图标退出程序，然后重新运行安装程序。"
        )
    for src in sorted(staged.rglob("*")):
        rel = src.relative_to(staged)
        target = dest / rel
        if src.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not src.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    return dest


def default_dest():
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        raise RuntimeError("LOCALAPPDATA 未设置，无法确定安装目录。")
    return Path(local) / "WinLinuxTunnel"


def _ps_quote_env(script):
    """Run a PowerShell snippet. Paths travel in the environment, not in the script text."""
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, env=os.environ.copy(),
        creationflags=flags,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or "powershell failed")
    return (result.stdout or "").strip()


def win_folder(name):
    """A .NET SpecialFolder (Desktop, Programs, Startup). Windows only."""
    os.environ["WLT_FOLDER_NAME"] = name
    out = _ps_quote_env("[Environment]::GetFolderPath($env:WLT_FOLDER_NAME)")
    if not out:
        raise RuntimeError("无法解析文件夹: %s" % name)
    return Path(out)


def create_shortcut(lnk_path, target, arguments, workdir, description):
    os.environ["WLT_LNK"] = str(lnk_path)
    os.environ["WLT_TARGET"] = str(target)
    os.environ["WLT_ARGS"] = arguments or ""
    os.environ["WLT_WORKDIR"] = str(workdir)
    os.environ["WLT_DESC"] = description or "WinLinuxTunnel"
    Path(lnk_path).parent.mkdir(parents=True, exist_ok=True)
    _ps_quote_env(
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:WLT_LNK); "
        "$s.TargetPath = $env:WLT_TARGET; "
        "$s.Arguments = $env:WLT_ARGS; "
        "$s.WorkingDirectory = $env:WLT_WORKDIR; "
        "$s.Description = $env:WLT_DESC; "
        "$s.WindowStyle = 1; "
        "$s.Save()"
    )


def remove_legacy_startup():
    """Drop the old wlt.vbs Startup launcher if a previous install left one."""
    try:
        startup = win_folder("Startup")
    except (OSError, RuntimeError):
        return
    legacy = startup / "wlt.vbs"
    try:
        if legacy.exists():
            legacy.unlink()
    except OSError:
        pass


def register_autostart(exe):
    startup = win_folder("Startup")
    remove_legacy_startup()
    create_shortcut(
        startup / "WinLinuxTunnel.lnk",
        exe,
        "--agent",
        exe.parent,
        "WinLinuxTunnel agent",
    )


def create_user_shortcuts(exe):
    made = []
    programs = win_folder("Programs")
    desktop = win_folder("Desktop")
    for folder, label in ((programs, "开始菜单"), (desktop, "桌面")):
        lnk = folder / "WinLinuxTunnel.lnk"
        create_shortcut(lnk, exe, "", exe.parent, "WinLinuxTunnel")
        made.append((label, lnk))
    return made


def launch_gui(exe):
    kwargs = {"cwd": str(exe.parent), "close_fds": True}
    if os.name == "nt":
        # Detach from the installer console. The GUI subsystem still shows its window.
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(exe)], **kwargs)


def install(payload, dest, launch=True, shortcuts=True, autostart=True):
    """Extract payload into dest and (on Windows) create shortcuts. Returns dest."""
    payload = Path(payload)
    dest = Path(dest)
    if not payload.is_file():
        raise RuntimeError("找不到安装包数据: %s" % payload)
    tmp = Path(tempfile.mkdtemp(prefix="wlt-setup-"))
    try:
        extract_zip(payload, tmp)
        exe_staged = tmp / "WinLinuxTunnel.exe"
        if not exe_staged.is_file():
            raise RuntimeError("安装包里没有 WinLinuxTunnel.exe")
        copy_tree(tmp, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    exe = dest / "WinLinuxTunnel.exe"
    if os.name == "nt":
        if shortcuts:
            create_user_shortcuts(exe)
        if autostart:
            register_autostart(exe)
        if launch:
            launch_gui(exe)
    return dest


def _hold_console():
    """Keep a double-clicked console open long enough to read an error."""
    if not is_frozen() or os.name != "nt":
        return
    try:
        input("按回车键关闭…")
    except EOFError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install WinLinuxTunnel into LOCALAPPDATA")
    parser.add_argument("--payload", default="", help="payload zip (embedded when frozen)")
    parser.add_argument("--dest", default="", help="install directory (default: %%LOCALAPPDATA%%\\WinLinuxTunnel)")
    parser.add_argument("--no-launch", action="store_true")
    parser.add_argument("--no-shortcuts", action="store_true")
    parser.add_argument("--no-autostart", action="store_true")
    args = parser.parse_args(argv)

    if args.payload:
        payload = Path(args.payload)
    elif is_frozen():
        payload = Path(getattr(sys, "_MEIPASS", "")) / "payload.zip"
    else:
        parser.print_help()
        print("\n需要 --payload，或使用打包后的 WinLinuxTunnel-Setup.exe。", file=sys.stderr)
        return 2

    try:
        dest = Path(args.dest) if args.dest else default_dest()
        install(
            payload,
            dest,
            launch=not args.no_launch,
            shortcuts=not args.no_shortcuts,
            autostart=not args.no_autostart,
        )
    except (RuntimeError, ValueError, OSError, zipfile.BadZipFile) as exc:
        print("安装失败: %s" % exc, file=sys.stderr)
        _hold_console()
        return 1
    print("已安装到 %s" % dest)
    print("从开始菜单或桌面的 WinLinuxTunnel 打开。代理会随窗口自动启动。")
    if args.no_launch:
        _hold_console()
    return 0


if __name__ == "__main__":
    sys.exit(main())
