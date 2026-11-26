#!/usr/bin/env python3
"""Process inventory and bounded tree kill (Windows side).

`wlt proc` is a process table (the replacement for shelling out to tasklist).
Window inventory stays on `wlt windows`. `wlt kill` ends one process tree with
taskkill /F /T /PID and never targets the agent, an ancestor of the agent, or
a protected system process.

The policy functions take an injected process table and killer so they can be
unit-tested without Win32. snapshot_processes() / taskkill_tree() are the only
Windows entry points. No admin rights, no third-party packages.
ASCII comments. Python 3.8+.
"""

import locale
import os
import subprocess

DEFAULT_PROC_LIMIT = 200
MAX_PROC_LIMIT = 500

# Session-critical images. Killing any of these (or a tree that contains one)
# is refused before taskkill runs. Matched case-insensitively on the basename
# Toolhelp reports.
PROTECTED_NAMES = frozenset({
    "system",
    "registry",
    "idle",
    "memory compression",
    "secure system",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "lsaiso.exe",
    "dwm.exe",
    "fontdrvhost.exe",
})

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def parse_pid(value):
    try:
        pid = int(value)
    except (TypeError, ValueError):
        raise ValueError("pid must be a positive integer")
    if pid < 1 or pid > 0xFFFFFFFF:
        raise ValueError("pid must be a positive integer")
    return pid


def normalize_processes(processes):
    rows = []
    for item in processes or []:
        rows.append({
            "pid": int(item["pid"]),
            "ppid": int(item.get("ppid") or 0),
            "name": str(item.get("name") or ""),
            "threads": int(item.get("threads") or 0),
        })
    rows.sort(key=lambda row: (row["pid"], row["name"].lower()))
    return rows


def _children_map(rows):
    children = {}
    for row in rows:
        children.setdefault(row["ppid"], []).append(row["pid"])
    return children


def descendants(root, rows):
    """root plus every descendant. Cycles terminate."""
    children = _children_map(rows)
    out = []
    stack = [root]
    seen = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        out.append(cur)
        stack.extend(children.get(cur, ()))
    return out


def ancestors(pid, rows):
    """Parent chain of pid, not including pid. Cycles terminate."""
    by_pid = {row["pid"]: row for row in rows}
    out = []
    seen = set()
    cur = by_pid.get(pid, {}).get("ppid", 0)
    while cur and cur not in seen:
        seen.add(cur)
        out.append(cur)
        cur = by_pid.get(cur, {}).get("ppid", 0)
    return out


def assert_kill_allowed(pid, agent_pid, processes):
    """Raise unless pid may be killed. Return (tree_pids, image_name).

    The tree is the set taskkill /T would signal. The agent must appear in
    the snapshot; otherwise the tree cannot be proved safe and the kill is
    refused. pid <= 4 (Idle / System) is always refused.
    """
    pid = parse_pid(pid)
    if pid <= 4:
        raise PermissionError(f"refusing to kill protected pid {pid}")
    try:
        agent_pid = int(agent_pid)
    except (TypeError, ValueError):
        raise PermissionError("agent pid unknown; refusing to kill")
    rows = normalize_processes(processes)
    by_pid = {row["pid"]: row for row in rows}
    if agent_pid not in by_pid:
        raise PermissionError("agent process is not in the snapshot; refusing to kill")
    target = by_pid.get(pid)
    if target is None:
        raise LookupError(f"no such process: {pid}")
    if pid == agent_pid:
        raise PermissionError("refusing to kill the wlt agent process")
    if pid in ancestors(agent_pid, rows):
        raise PermissionError("refusing to kill an ancestor of the wlt agent")
    tree = descendants(pid, rows)
    if agent_pid in tree:
        raise PermissionError("refusing to kill a process tree that contains the wlt agent")
    for member in tree:
        name = by_pid.get(member, {}).get("name", "")
        if name.lower() in PROTECTED_NAMES:
            raise PermissionError(
                f"refusing to kill protected process {name} (pid {member})")
    return tree, target["name"]


def filter_processes(rows, name="", pid=None, limit=DEFAULT_PROC_LIMIT):
    try:
        limit = int(DEFAULT_PROC_LIMIT if limit in (None, "") else limit)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    limit = min(limit, MAX_PROC_LIMIT)
    selected = list(rows)
    if pid not in (None, ""):
        want = parse_pid(pid)
        selected = [row for row in selected if row["pid"] == want]
    if name:
        needle = str(name).lower()
        selected = [row for row in selected if needle in row["name"].lower()]
    total = len(selected)
    shown = selected[:limit]
    return shown, total, total > len(shown)


def list_processes(payload, agent_pid, processes):
    """Filter a snapshot. `self` marks the agent so callers can see it."""
    payload = payload or {}
    rows = normalize_processes(processes)
    try:
        agent_pid = int(agent_pid)
    except (TypeError, ValueError):
        agent_pid = None
    shown, total, truncated = filter_processes(
        rows, payload.get("name") or "", payload.get("pid"), payload.get("limit"))
    out = []
    for row in shown:
        item = dict(row)
        item["self"] = agent_pid is not None and item["pid"] == agent_pid
        out.append(item)
    return {
        "ok": True,
        "agent_pid": agent_pid,
        "count": total,
        "truncated": truncated,
        "processes": out,
    }


def kill_process(payload, agent_pid, processes, killer):
    """Apply the safety policy, then call killer(pid) for the root only.

    killer is not invoked when the policy refuses. It returns (rc, detail).
    """
    payload = payload or {}
    tree, name = assert_kill_allowed(payload.get("pid"), agent_pid, processes)
    pid = parse_pid(payload.get("pid"))
    rc, detail = killer(pid)
    detail = str(detail or "")[:500]
    result = {
        "ok": rc == 0,
        "pid": pid,
        "name": name,
        "tree": tree,
        "taskkill_rc": rc,
        "detail": detail,
    }
    if rc != 0:
        result["error_code"] = "KILL_FAILED"
    return result


def snapshot_processes():
    """Toolhelp32 process list. Image basename only; no command line."""
    if os.name != "nt":
        raise OSError("process snapshot is only available on Windows")
    import ctypes
    from ctypes import wintypes as wt

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", wt.LONG),
            ("dwFlags", wt.DWORD),
            ("szExeFile", wt.WCHAR * 260),
        ]

    TH32CS_SNAPPROCESS = 0x00000002
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wt.BOOL
    kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wt.BOOL
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap is None or snap == wt.HANDLE(-1).value:
        raise OSError("CreateToolhelp32Snapshot failed")
    rows = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            rows.append({
                "pid": int(entry.th32ProcessID),
                "ppid": int(entry.th32ParentProcessID),
                "name": str(entry.szExeFile),
                "threads": int(entry.cntThreads),
            })
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return rows


def taskkill_tree(pid):
    """taskkill /F /T /PID. The pid is an int; no image-name kill."""
    pid = parse_pid(pid)
    try:
        proc = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)
    raw = proc.stderr or proc.stdout or b""
    encoding = locale.getpreferredencoding(False) or "utf-8"
    return proc.returncode, raw.decode(encoding, errors="replace")


def kill_pid(payload, agent_pid):
    """Windows entry: live snapshot + taskkill. Tests use kill_process()."""
    if os.name != "nt":
        raise OSError("process kill is only available on Windows")
    return kill_process(payload, agent_pid, snapshot_processes(), taskkill_tree)
