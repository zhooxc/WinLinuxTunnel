#!/usr/bin/env python3
"""wlt desktop operations (Windows side) - ctypes only, no third-party deps.

Faithful reimplementation of the WinLinuxBridge desktop layer (MIT, see NOTICE)
plus additions:
  * multi-monitor capture (monitor=N / region / window=hwnd)
  * window inventory includes the owning process name
  * mouse drag, scroll wheel, double-click
  * wait_window polling primitive for automation

All Win32 access is via ctypes.windll; importing this module on non-Windows
raises ImportError at call time, not import time (the agent stays importable).
ASCII comments. Python 3.8+.
"""

import base64
import ctypes
import json
import os
import re
import shutil
import struct
import time
import urllib.error
import urllib.request
import zlib
from ctypes import wintypes as wt
from datetime import datetime
from pathlib import Path

# Module config, set by init().
SHOT_DIR = Path.home() / ".wlt" / "shots"
ALLOWED_ROOTS = []          # whitelist for file ops
MAX_FILE = 16 * 1024 * 1024  # 16 MB cap for file read/write and http bodies


def init(app_dir, allowed_roots):
    global SHOT_DIR, ALLOWED_ROOTS
    SHOT_DIR = Path(app_dir) / "shots"
    ALLOWED_ROOTS = [os.path.normcase(os.path.abspath(str(r))) for r in allowed_roots]


def _u32():
    return ctypes.windll.user32


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# GDI screen capture -> PNG (standard-library zlib, no Pillow)
# ---------------------------------------------------------------------------

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


def virtual_screen():
    u = _u32()
    return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
            u.GetSystemMetrics(78), u.GetSystemMetrics(79))


def gdi_capture_png(path, region=None):
    """Capture the screen (or a region) via GDI BitBlt, encode as PNG."""
    user32, gdi32 = _u32(), ctypes.windll.gdi32
    if region:
        x, y, w, h = [int(v) for v in region]
    else:
        x, y, w, h = virtual_screen()
    if w <= 0 or h <= 0:
        return 0, 0
    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)
    gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, 0x00CC0020)  # SRCCOPY
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h  # top-down rows
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(w * 4 * h)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)
    _write_png(path, w, h, buf.raw)
    return w, h


def _write_png(path, w, h, bgra):
    """Encode a top-down BGRA buffer as PNG using only struct + zlib."""
    stride = w * 4
    rows = []
    for y in range(h):
        row = bgra[y * stride:(y + 1) * stride]
        rgb = bytearray(w * 3)
        rgb[0::3] = row[2::4]   # R
        rgb[1::3] = row[1::4]   # G
        rgb[2::3] = row[0::4]   # B
        rows.append(b"\x00" + bytes(rgb))
    payload = zlib.compress(b"".join(rows), 6)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", ihdr))
        fh.write(chunk(b"IDAT", payload))
        fh.write(chunk(b"IEND", b""))


def list_monitors():
    """Enumerate physical monitors via EnumDisplayMonitors (addition)."""
    user32 = _u32()
    monitors = []

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                    ("rcWork", wt.RECT), ("dwFlags", wt.DWORD),
                    ("szDevice", wt.WCHAR * 32)]

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HANDLE, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
    def cb(hmon, hdc, lprc, lparam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        monitors.append({
            "index": len(monitors),
            "device": mi.szDevice,
            "primary": bool(mi.dwFlags & 1),  # MONITORINFOF_PRIMARY
            "rect": [r.left, r.top, r.right - r.left, r.bottom - r.top],
        })
        return True

    user32.EnumDisplayMonitors(0, 0, cb, 0)
    return monitors


def screenshot(payload):
    """mode=path|b64; region=[x,y,w,h]; monitor=N; window=hwnd (additions)."""
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    region = payload.get("region")
    if payload.get("monitor") is not None:
        mons = list_monitors()
        idx = int(payload["monitor"])
        if not (0 <= idx < len(mons)):
            raise ValueError(f"monitor {idx} out of range (have {len(mons)})")
        region = mons[idx]["rect"]
    elif payload.get("window"):
        w = next((x for x in list_windows() if x["hwnd"] == int(payload["window"])), None)
        if not w:
            raise LookupError(f"no such window hwnd: {payload['window']}")
        region = w["rect"]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = str(SHOT_DIR / f"win_{stamp}.png")
    w, h = gdi_capture_png(path, region)
    if (w, h) == (0, 0):
        raise RuntimeError("screen capture returned empty image")
    size = os.path.getsize(path)
    result = {"path": path, "width": w, "height": h, "size": size, "ts": _now_iso()}
    if str(payload.get("mode") or "path") == "b64":
        result["b64"] = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    if size < 40 * 1024:
        result["warning"] = "image is very small; the desktop may be locked or black"
    return result


# ---------------------------------------------------------------------------
# SendInput mouse / keyboard
# ---------------------------------------------------------------------------

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


VK_MAP = {
    "CTRL": 0x11, "CONTROL": 0x11, "SHIFT": 0x10, "ALT": 0x12, "WIN": 0x5B,
    "ENTER": 0x0D, "RETURN": 0x0D, "TAB": 0x09, "ESC": 0x1B, "ESCAPE": 0x1B,
    "SPACE": 0x20, "BACKSPACE": 0x08, "DELETE": 0x2E, "DEL": 0x2E,
    "HOME": 0x24, "END": 0x23, "PGUP": 0x21, "PAGEDOWN": 0x22,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "INSERT": 0x2D, "PRINTSCREEN": 0x2C,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    sent = _u32().SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise RuntimeError(f"SendInput sent {sent}/{len(inputs)} events (err={ctypes.get_last_error()})")


def _mouse(flags, dx=0, dy=0, data=0):
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, None)
    return inp


def _key_vk(vk, up):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
    return inp


def _key_unicode(ch, up):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, None)
    return inp


def _abs(x, y):
    vx, vy, vw, vh = virtual_screen()
    return int((x - vx) * 65535 / max(vw - 1, 1)), int((y - vy) * 65535 / max(vh - 1, 1))


def cursor_pos():
    pt = wt.POINT()
    _u32().GetCursorPos(ctypes.byref(pt))
    return {"x": pt.x, "y": pt.y}


def input_action(payload):
    action = str(payload.get("action") or "").lower()
    if action == "move":
        x, y = int(payload["x"]), int(payload["y"])
        _u32().SetCursorPos(x, y)
        nx, ny = _abs(x, y)
        _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, nx, ny))
        return {"ok": True, "cursor": cursor_pos()}
    if action in ("click", "double_click", "right_click", "middle_click"):
        if payload.get("x") is not None:
            _u32().SetCursorPos(int(payload["x"]), int(payload["y"]))
        down, up = {
            "click": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "double_click": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right_click": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle_click": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        }[action]
        for _ in range(2 if action == "double_click" else 1):
            _send(_mouse(down))
            time.sleep(0.03)
            _send(_mouse(up))
            time.sleep(0.08)
        return {"ok": True, "cursor": cursor_pos()}
    if action == "drag":
        # Addition: press at (x1,y1), glide to (x2,y2), release.
        x1, y1 = int(payload["x1"]), int(payload["y1"])
        x2, y2 = int(payload["x2"]), int(payload["y2"])
        steps = max(int(payload.get("steps", 12)), 2)
        _u32().SetCursorPos(x1, y1)
        nx, ny = _abs(x1, y1)
        _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, nx, ny))
        _send(_mouse(MOUSEEVENTF_LEFTDOWN))
        for i in range(1, steps + 1):
            ix = x1 + (x2 - x1) * i // steps
            iy = y1 + (y2 - y1) * i // steps
            nx, ny = _abs(ix, iy)
            _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, nx, ny))
            time.sleep(0.01)
        _send(_mouse(MOUSEEVENTF_LEFTUP))
        return {"ok": True, "cursor": cursor_pos()}
    if action in ("wheel", "scroll"):
        delta = int(payload.get("delta") or 120)
        _send(_mouse(MOUSEEVENTF_WHEEL, 0, 0, delta))
        return {"ok": True}
    if action == "key":
        combo = str(payload.get("key") or payload.get("combo") or "")
        return press_combo(combo, repeat=int(payload.get("repeat") or 1))
    if action == "type":
        return type_text(str(payload.get("text") or ""))
    raise ValueError(f"unknown action: {action}")


def press_combo(combo, repeat=1):
    parts = [p.strip() for p in re.split(r"[+]", combo) if p.strip()]
    if not parts:
        raise ValueError("empty key combo")
    vks = []
    for part in parts:
        upper = part.upper()
        if upper in VK_MAP:
            vks.append(VK_MAP[upper])
        elif len(part) == 1:
            vks.append(_u32().VkKeyScanW(part) & 0xFF)
        else:
            raise ValueError(f"unknown key name: {part}")
    for _ in range(max(repeat, 1)):
        for vk in vks:
            _send(_key_vk(vk, False))
            time.sleep(0.01)
        for vk in reversed(vks):
            _send(_key_vk(vk, True))
            time.sleep(0.01)
    return {"ok": True, "combo": combo}


def type_text(text):
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:  # surrogate pair for astral chars
            hi = 0xD800 + ((code - 0x10000) >> 10)
            lo = 0xDC00 + ((code - 0x10000) & 0x3FF)
            _send(_key_unicode(chr(hi), False), _key_unicode(chr(lo), False))
            _send(_key_unicode(chr(hi), True), _key_unicode(chr(lo), True))
        elif ch == "\n":
            _send(_key_vk(0x0D, False), _key_vk(0x0D, True))
        else:
            _send(_key_unicode(ch, False), _key_unicode(ch, True))
        time.sleep(0.004)
    return {"ok": True, "chars": len(text)}


# ---------------------------------------------------------------------------
# Window inventory / control
# ---------------------------------------------------------------------------

def _process_name(pid):
    """Addition: owning process basename via QueryFullProcessImageNameW."""
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def list_windows():
    user32 = _u32()
    result = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        result.append({
            "hwnd": int(hwnd), "title": buf.value, "pid": int(pid.value),
            "process": _process_name(pid.value),
            "rect": [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top],
            "minimized": bool(user32.IsIconic(hwnd)),
        })
        return True

    user32.EnumWindows(cb, 0)
    return result


def _find_window(payload):
    hwnd = payload.get("hwnd")
    title = payload.get("title")
    if hwnd:
        return int(hwnd)
    if title:
        wanted = str(title).lower()
        match = [w for w in list_windows() if wanted in w["title"].lower()]
        if not match:
            raise LookupError(f"no window title contains: {title}")
        return match[0]["hwnd"]
    raise ValueError("hwnd or title is required")


def window_action(payload):
    user32 = _u32()
    action = str(payload.get("action") or "focus").lower()
    hwnd = _find_window(payload)
    if action == "focus":
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    elif action == "minimize":
        user32.ShowWindow(hwnd, 6)
    elif action == "maximize":
        user32.ShowWindow(hwnd, 3)
    elif action == "close":
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    else:
        raise ValueError(f"unknown window action: {action}")
    return {"ok": True, "hwnd": hwnd}


def wait_window(payload):
    """Addition: poll until a window whose title contains `title` appears."""
    title = str(payload.get("title") or "")
    if not title:
        raise ValueError("'title' is required")
    timeout = float(payload.get("timeout") or 30)
    interval = float(payload.get("interval") or 0.5)
    deadline = time.time() + timeout
    wanted = title.lower()
    while time.time() < deadline:
        match = [w for w in list_windows() if wanted in w["title"].lower()]
        if match:
            return {"ok": True, "found": True, "window": match[0]}
        time.sleep(interval)
    return {"ok": True, "found": False, "timed_out": True}


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

def clipboard(payload):
    user32, kernel32 = _u32(), ctypes.windll.kernel32
    # Correct prototypes are MANDATORY: GlobalLock/GlobalAlloc return
    # pointer-sized values. ctypes' default c_int restype truncates them on
    # x64, producing a wild pointer that hard-crashes the whole process.
    user32.OpenClipboard.argtypes = [wt.HWND]
    user32.OpenClipboard.restype = wt.BOOL
    user32.CloseClipboard.restype = wt.BOOL
    user32.EmptyClipboard.restype = wt.BOOL
    user32.GetClipboardData.argtypes = [wt.UINT]
    user32.GetClipboardData.restype = wt.HANDLE
    user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
    user32.SetClipboardData.restype = wt.HANDLE
    kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wt.HGLOBAL
    kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
    kernel32.GlobalUnlock.restype = wt.BOOL
    op = str(payload.get("op") or "get").lower()
    CF_UNICODETEXT = 13
    if op == "get":
        if not user32.OpenClipboard(0):
            raise RuntimeError("OpenClipboard failed")
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return {"text": ""}
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return {"text": ""}
            try:
                return {"text": ctypes.c_wchar_p(ptr).value or ""}
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    if op == "set":
        text = str(payload.get("text") or "")
        if not user32.OpenClipboard(0):
            raise RuntimeError("OpenClipboard failed")
        try:
            user32.EmptyClipboard()
            size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
            handle = kernel32.GlobalAlloc(0x0042, size)  # GMEM_MOVEABLE|ZEROINIT
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, ctypes.create_unicode_buffer(text), size)
            kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(CF_UNICODETEXT, handle)
        finally:
            user32.CloseClipboard()
        return {"ok": True, "length": len(text)}
    raise ValueError(f"unknown clipboard op: {op}")


# ---------------------------------------------------------------------------
# HTTP proxy to Windows-local services
# ---------------------------------------------------------------------------

def http_call(payload):
    url = str(payload.get("url") or "")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    method = str(payload.get("method") or "GET").upper()
    headers = {str(k): str(v) for k, v in (payload.get("headers") or {}).items()}
    body = payload.get("body")
    if body is not None and not isinstance(body, str):
        body = json.dumps(body)
        headers.setdefault("Content-Type", "application/json")
    data = body.encode("utf-8") if isinstance(body, str) else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    timeout = float(payload.get("timeout") or 30)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_FILE)
            status = resp.status
            rheaders = dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_FILE)
        status = exc.code
        rheaders = dict(exc.headers.items())
    out = {"status": status, "headers": rheaders, "size": len(raw)}
    try:
        out["text"] = raw.decode("utf-8")
    except UnicodeDecodeError:
        out["b64"] = base64.b64encode(raw).decode("ascii")
    return out


# ---------------------------------------------------------------------------
# File operations, confined to the whitelist roots
# ---------------------------------------------------------------------------

def _confined(path):
    raw = str(path or "")
    if not raw:
        raise ValueError("'path' is required")
    base = ALLOWED_ROOTS[0] if ALLOWED_ROOTS else os.path.expanduser("~")
    candidate = raw if os.path.isabs(raw) else os.path.join(base, raw)
    resolved = os.path.abspath(candidate)
    norm = os.path.normcase(resolved)
    for root in ALLOWED_ROOTS:
        if norm == root or norm.startswith(root + os.sep):
            return resolved
    raise PermissionError(f"path is outside the allowed roots: {resolved}")


def read_file(payload):
    path = _confined(payload.get("path") or "")
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    size = os.path.getsize(path)
    if size > MAX_FILE:
        raise ValueError(f"file too large: {size} bytes (limit {MAX_FILE})")
    raw = Path(path).read_bytes()
    return {
        "path": path, "size": size,
        "mtime": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds"),
        "b64": base64.b64encode(raw).decode("ascii"),
    }


def fs_op(payload):
    op = str(payload.get("op") or "list").lower()
    target = _confined(payload.get("path") or ".")
    if op == "list":
        if not os.path.isdir(target):
            raise FileNotFoundError(target)
        entries = []
        with os.scandir(target) as it:
            for e in it:
                try:
                    st = e.stat()
                except OSError:
                    continue
                entries.append({
                    "name": e.name, "type": "dir" if e.is_dir() else "file",
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                })
        entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        return {"path": target, "count": len(entries), "entries": entries}
    if op == "stat":
        if not os.path.exists(target):
            raise FileNotFoundError(target)
        st = os.stat(target)
        return {"path": target, "type": "dir" if os.path.isdir(target) else "file",
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")}
    if op == "read":
        if not os.path.isfile(target):
            raise FileNotFoundError(target)
        size = os.path.getsize(target)
        if size > MAX_FILE:
            raise ValueError(f"file too large: {size} bytes (limit {MAX_FILE})")
        raw = Path(target).read_bytes()
        out = {"path": target, "size": size}
        try:
            out["text"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            out["b64"] = base64.b64encode(raw).decode("ascii")
        return out
    if op == "write":
        os.makedirs(os.path.dirname(target) or target, exist_ok=True)
        raw = (base64.b64decode(payload["b64"]) if payload.get("b64") is not None
               else str(payload.get("text") or "").encode("utf-8"))
        if len(raw) > MAX_FILE:
            raise ValueError(f"payload too large: {len(raw)} bytes (limit {MAX_FILE})")
        Path(target).write_bytes(raw)
        return {"path": target, "size": len(raw), "ok": True}
    if op == "mkdir":
        os.makedirs(target, exist_ok=True)
        return {"path": target, "ok": True}
    if op == "rm":
        if not os.path.exists(target):
            raise FileNotFoundError(target)
        if os.path.isdir(target):
            shutil.rmtree(target) if payload.get("recursive") else os.rmdir(target)
        else:
            os.remove(target)
        return {"path": target, "ok": True}
    raise ValueError(f"unknown fs op: {op}")