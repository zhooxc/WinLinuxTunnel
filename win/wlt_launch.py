#!/usr/bin/env python3
"""How the Windows app starts the resident agent.

Two layouts, one rule:

  * Packaged (PyInstaller onedir): sys.executable is WinLinuxTunnel.exe.
    The GUI spawns that same exe with --agent, hidden.
  * Unpackaged / dev: spawn this same interpreter (sys.executable) on
    wlt_main.py --agent, with no new console.

The agent process is separate from the GUI. A GUI crash does not stop it.
The agent's own file lock (wlt_agent.acquire_lock) makes a second start a
no-op, so the GUI can try to spawn whenever the HTTP port is down.

ASCII comments. Python 3.8+.
"""

import os
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def executable_path():
    return Path(sys.executable).resolve()


def source_dir():
    """Directory of the win/ sources (dev) or the PyInstaller bundle root."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", executable_path().parent))
    return Path(__file__).resolve().parent


def install_root():
    """Directory that holds linux/, docs/ and install-win.ps1.

    Dev: the repo root (parent of win/).
    Packaged: files staged next to WinLinuxTunnel.exe by tools/build-windows.ps1.
    """
    if is_frozen():
        exe_dir = executable_path().parent
        if (exe_dir / "install-win.ps1").exists() or (exe_dir / "linux" / "wlt").exists():
            return exe_dir
        return source_dir()
    return Path(__file__).resolve().parent.parent


def install_script():
    """Path to install-win.ps1, packaged layout first, then the source tree."""
    root = install_root()
    candidates = [
        root / "install-win.ps1",
        root / "win" / "install-win.ps1",
        source_dir() / "install-win.ps1",
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def icon_path():
    """wlt.ico for Tk and the tray. PyInstaller extracts datas into _MEIPASS."""
    names = ("wlt.ico",)
    bases = []
    if is_frozen():
        bases.append(Path(getattr(sys, "_MEIPASS", executable_path().parent)))
        bases.append(executable_path().parent)
    bases.append(Path(__file__).resolve().parent)
    for base in bases:
        for name in names:
            candidate = base / name
            if candidate.exists():
                return candidate
    return bases[-1] / "wlt.ico"


def agent_command():
    """argv of the resident agent. Same exe when frozen, same interpreter in dev."""
    if is_frozen():
        return [str(executable_path()), "--agent"]
    main_py = Path(__file__).resolve().parent / "wlt_main.py"
    if main_py.exists():
        return [sys.executable, str(main_py), "--agent"]
    return [sys.executable, str(Path(__file__).resolve().parent / "wlt_agent.py")]


def _child_env():
    """Environment for a GUI-spawned agent.

    Drop WLT_AGENT_TOKEN so the child reads agent.token. A long-lived GUI
    process can inherit a stale user-env token from logon; the file is what
    install-win.ps1 just wrote and what the Linux side was given.
    """
    env = os.environ.copy()
    env.pop("WLT_AGENT_TOKEN", None)
    return env


def _hidden_kwargs():
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": _child_env(),
    }
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= STARTF_USESHOWWINDOW
        startup.wShowWindow = SW_HIDE
        kwargs["startupinfo"] = startup
        # CREATE_NO_WINDOW: no console for python.exe. Do not combine it with
        # DETACHED_PROCESS; that pair returns "parameter is incorrect".
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return kwargs


def spawn_agent():
    """Start the resident agent, hidden. Returns the Popen handle.

    If an agent already holds the single-instance lock, the new process
    exits immediately and the existing one keeps running. Callers should
    treat that as success and then poll the HTTP port.
    """
    cmd = agent_command()
    if is_frozen():
        cwd = str(executable_path().parent)
    else:
        cwd = str(Path(cmd[1]).resolve().parent)
    return subprocess.Popen(cmd, cwd=cwd, **_hidden_kwargs())


def app_dir():
    return Path(os.environ.get("WLT_APP_DIR", str(Path.home() / ".wlt")))


def _looks_like_agent(command_line):
    text = (command_line or "").lower()
    return ("wlt_main" in text or "wlt_agent" in text
            or "winlinuxtunnel" in text or "--agent" in text)


def _command_line(pid):
    if os.name != "nt":
        try:
            raw = Path("/proc/%d/cmdline" % pid).read_bytes()
        except OSError:
            return ""
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace")
    env = os.environ.copy()
    env["WLT_QUERY_PID"] = str(int(pid))
    script = "(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $env:WLT_QUERY_PID)).CommandLine"
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=15, env=env,
            creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or "").strip()


def stop_recorded_agent():
    """Force-stop the process recorded in agent.pid, if it still looks like wlt.

    Prefer POST /shutdown so the agent can close its ssh children. This is
    the fallback when that request cannot be delivered (token mismatch,
    agent wedged). Returns True when a signal was sent.
    """
    pid_path = app_dir() / "agent.pid"
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    if not _looks_like_agent(_command_line(pid)):
        return False
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, creationflags=CREATE_NO_WINDOW,
            )
        except OSError:
            return False
        return True
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True
