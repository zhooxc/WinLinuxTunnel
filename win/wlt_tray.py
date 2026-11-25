#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wlt 系统托盘（Windows，纯 ctypes，零第三方依赖）。

用独立的消息窗口线程承载 Shell_NotifyIcon 回调，事件经线程安全队列回传给
Tk 主线程（root.after 轮询），避免跨线程直接操作 Tk。托盘失败时静默降级，
主窗口仍可作为普通程序使用（正常最小化到任务栏）。

ASCII comments. Python 3.8+.
"""

import ctypes
import queue
import threading
from ctypes import wintypes as wt

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_shell32 = ctypes.windll.shell32

WM_USER = 0x0400
WM_TRAY = WM_USER + 1
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
HWND_MESSAGE = -3
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
        ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128), ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
    ]


def _setup_prototypes():
    _user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    _user32.DefWindowProcW.restype = LRESULT
    _user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    _user32.CreateWindowExW.restype = wt.HWND
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    _user32.DispatchMessageW.restype = LRESULT
    _user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
    _user32.LoadImageW.restype = wt.HANDLE
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    _shell32.Shell_NotifyIconW.restype = wt.BOOL
    _kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wt.HINSTANCE


class TrayIcon:
    """System-tray icon with click / double-click / right-click events."""

    def __init__(self, icon_path, tooltip, events):
        self.icon_path = str(icon_path)
        self.tooltip = tooltip
        self.events = events          # thread-safe queue.Queue
        self._thread = None
        self._hwnd = None
        self._hicon = None
        self._nid = None
        self._wndproc = None          # keep reference (avoid GC)
        self._ready = threading.Event()
        self.ok = False

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="wlt-tray")
        self._thread.start()
        self._ready.wait(timeout=3)
        return self.ok

    def _run(self):
        try:
            _setup_prototypes()
            hinst = _kernel32.GetModuleHandleW(None)
            cls_name = "wltTrayMsgClass"

            def wndproc(hwnd, msg, wparam, lparam):
                if msg == WM_TRAY:
                    ev = lparam & 0xFFFF
                    if ev == WM_LBUTTONUP:
                        self.events.put(("click",))
                    elif ev == WM_LBUTTONDBLCLK:
                        self.events.put(("dblclick",))
                    elif ev == WM_RBUTTONUP:
                        self.events.put(("menu",))
                    return 0
                if msg == WM_DESTROY:
                    _user32.PostQuitMessage(0)
                    return 0
                return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wndproc = WNDPROC(wndproc)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            if not _user32.RegisterClassW(ctypes.byref(wc)):
                return
            self._hwnd = _user32.CreateWindowExW(
                0, cls_name, "wlt tray", 0, 0, 0, 0, 0,
                wt.HWND(HWND_MESSAGE), None, hinst, None)
            if not self._hwnd:
                return
            self._hicon = _user32.LoadImageW(
                None, self.icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self._hicon
            nid.szTip = self.tooltip[:127]
            self._nid = nid
            if not _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                return
            self.ok = True
            self._ready.set()
            msg = wt.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            self.ok = False
        finally:
            self._ready.set()

    def set_tooltip(self, text):
        if not self.ok or not self._nid:
            return
        try:
            self._nid.szTip = text[:127]
            self._nid.uFlags = NIF_TIP
            _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
        except Exception:
            pass

    def stop(self):
        if not self.ok:
            return
        try:
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            if self._hwnd:
                _user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
        except Exception:
            pass
        self.ok = False