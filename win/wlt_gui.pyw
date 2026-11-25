#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WinLinuxTunnel 图形界面（Windows 侧，中文）。

正规 Windows 桌面软件体验：
  * 应用图标 + 标题，正常最小化到任务栏；
  * 关闭按钮默认最小化到系统托盘（通知区域），双击托盘恢复，右键菜单退出；
  * 仪表盘式布局：左侧导航（概览/隧道/桌面/日志/设置）+ 状态卡片。

是本地控制代理的 HTTP 客户端（127.0.0.1:<WLT_AGENT_PORT>），独立进程，
GUI 崩溃不影响桥。仅标准库 Tkinter + ctypes 托盘。随包运行时含 Tcl/Tk。
Python 3.8+。
"""

import base64
import json
import os
import queue
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Bundled-runtime bootstrap (embeddable ._pth omits script dir + Tcl/Tk paths).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
_root = os.path.dirname(sys.executable)
_tcl = os.path.join(_root, "tcl", "tcl8.6")
if os.path.isdir(_tcl):
    os.environ.setdefault("TCL_LIBRARY", _tcl)
    os.environ.setdefault("TK_LIBRARY", os.path.join(_root, "tcl", "tk8.6"))
    try:
        os.add_dll_directory(_root)
    except (AttributeError, OSError):
        pass

import tkinter as tk
from tkinter import ttk, messagebox

import wlt_tray

APP_DIR = Path(os.environ.get("WLT_APP_DIR", str(Path.home() / ".wlt")))
WIN_DIR = Path(_HERE)
ICON = str(WIN_DIR / "wlt.ico")
ACCENT = "#2563eb"
OK = "#16a34a"
BAD = "#dc2626"
MUTE = "#6b7280"


def load_conf():
    conf = {"WLT_AGENT_PORT": "8788", "WLT_TUNNEL_PORT": "18081",
            "WLT_LINUX_HOST": "", "WLT_LINUX_USER": "", "WLT_SSH_PORT": "22",
            "WLT_REMOTE_ROOT": "~/wlt"}
    p = APP_DIR / "wlt.conf"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    return conf


CONF = load_conf()
BASE = f"http://127.0.0.1:{CONF.get('WLT_AGENT_PORT', '8788')}"
TOKEN = (APP_DIR / "agent.token").read_text(encoding="utf-8").strip() \
    if (APP_DIR / "agent.token").exists() else ""


def api(method, path, body=None, timeout=15):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class App:
    def __init__(self, root):
        self.root = root
        self.rules = []
        self.connected = False
        self._refreshing = False
        self._tray_events = queue.Queue()
        self._tray = None
        self.close_to_tray = tk.BooleanVar(value=True)
        self._shot_img = None

        root.title("WinLinuxTunnel")
        root.geometry("940x640")
        root.minsize(780, 540)
        try:
            root.iconbitmap(ICON)
        except tk.TclError:
            pass
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Sidebar.TButton", anchor="w", padding=(14, 9))
        style.configure("Accent.TButton", foreground="white", background=ACCENT)
        style.configure("Card.TFrame", background="white")
        style.configure("Treeview", rowheight=26)

        self._build_header()
        body = ttk.Frame(root)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self._build_sidebar(body)
        self.content = ttk.Frame(body)
        self.content.pack(side="left", fill="both", expand=True, padx=(10, 0))

        self.pages = {}
        self._build_dashboard()
        self._build_tunnel()
        self._build_desktop()
        self._build_log()
        self._build_settings()
        self._build_statusbar()
        self.show_page("dash")

        self._init_tray()
        self.refresh()
        self._auto()
        self._poll_tray()

    # -- 框架 ------------------------------------------------------------
    def _build_header(self):
        h = ttk.Frame(self.root)
        h.pack(fill="x", padx=12, pady=(10, 6))
        ttk.Label(h, text="WinLinuxTunnel", font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Label(h, text="  Windows ↔ Linux 双向桥", foreground=MUTE,
                  font=("Segoe UI", 10)).pack(side="left", pady=(6, 0))
        self.conn_pill = tk.Label(h, text="● 检测中", fg=MUTE, font=("Segoe UI", 10, "bold"))
        self.conn_pill.pack(side="right")

    def _build_sidebar(self, parent):
        bar = ttk.Frame(parent, width=150)
        bar.pack(side="left", fill="y")
        bar.pack_propagate(False)
        self.nav_btns = {}
        for key, text in (("dash", "概览"), ("tunnel", "隧道"),
                          ("desktop", "桌面"), ("log", "日志"),
                          ("settings", "设置")):
            b = ttk.Button(bar, text=text, style="Sidebar.TButton",
                           command=lambda k=key: self.show_page(k))
            b.pack(fill="x", pady=2)
            self.nav_btns[key] = b

    def _build_statusbar(self):
        self.statusbar = ttk.Label(self.root, text="就绪", foreground=MUTE, anchor="w")
        self.statusbar.pack(fill="x", padx=12, pady=(0, 6))

    def show_page(self, key):
        for p in self.pages.values():
            p.pack_forget()
        self.pages[key].pack(fill="both", expand=True)

    def _card(self, parent, title):
        wrap = tk.Frame(parent, bg="white", bd=1, relief="solid",
                        highlightbackground="#e5e7eb", highlightthickness=1)
        tk.Label(wrap, text=title, bg="white", fg=MUTE,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(10, 0))
        val = tk.Label(wrap, text="—", bg="white", fg="#111827",
                       font=("Segoe UI", 18, "bold"))
        val.pack(anchor="w", padx=12)
        det = tk.Label(wrap, text="", bg="white", fg=MUTE,
                       font=("Segoe UI", 9), justify="left", wraplength=200)
        det.pack(anchor="w", padx=12, pady=(0, 10))
        return wrap, val, det

    # -- 概览 ------------------------------------------------------------
    def _build_dashboard(self):
        page = ttk.Frame(self.content)
        self.pages["dash"] = page
        ttk.Label(page, text="运行概览", font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(4, 10))
        cards = ttk.Frame(page)
        cards.pack(fill="x")
        self.card_agent = self._card(cards, "控制代理")
        self.card_control = self._card(cards, "控制隧道")
        self.card_user = self._card(cards, "用户隧道")
        self.card_rules = self._card(cards, "隧道规则")
        for i, (w, _, _) in enumerate((self.card_agent, self.card_control,
                                       self.card_user, self.card_rules)):
            w.grid(row=0, column=i, sticky="nsew", padx=6)
            cards.columnconfigure(i, weight=1)
        self.dash_summary = ttk.Label(page, text="", foreground=MUTE, wraplength=820,
                                      justify="left")
        self.dash_summary.pack(anchor="w", padx=6, pady=14)
        bar = ttk.Frame(page)
        bar.pack(anchor="w", padx=6)
        ttk.Button(bar, text="刷新状态", command=self.refresh).pack(side="left", padx=4)
        ttk.Button(bar, text="管理隧道 →", command=lambda: self.show_page("tunnel")).pack(side="left", padx=4)
        ttk.Button(bar, text="截一张图 →", command=self.do_shot).pack(side="left", padx=4)

    # -- 隧道 ------------------------------------------------------------
    def _build_tunnel(self):
        page = ttk.Frame(self.content)
        self.pages["tunnel"] = page
        head = ttk.Frame(page)
        head.pack(fill="x", pady=(4, 8))
        ttk.Label(head, text="隧道规则", font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Label(head, text="  L = Windows 访问 Linux    R = Linux 访问 Windows",
                  foreground=MUTE).pack(side="left", pady=(4, 0))
        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 6))
        for text, cmd in (("＋ 添加", self.add_rule), ("编辑", self.edit_rule),
                          ("删除", self.del_rule), ("启用/停用", self.toggle_rule),
                          ("探测", self.probe_rule), ("同步到 Linux", self.sync_rules)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=3)
        cols = ("enabled", "dir", "listen", "target", "state", "note")
        self.tree = ttk.Treeview(page, columns=cols, show="headings", height=14)
        for c, t, w in (("enabled", "启用", 50), ("dir", "方向", 50), ("listen", "监听", 170),
                        ("target", "目标", 170), ("state", "状态", 80), ("note", "备注", 200)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("up", foreground=OK)
        self.tree.tag_configure("down", foreground=BAD)
        self.tree.tag_configure("off", foreground=MUTE)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit_rule())

    def _selected_rule(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选择一条规则")
            return None
        # item iid == rule id (set on insert)
        return next((r for r in self.rules if r.get("id") == sel[0]), None)

    def _apply(self, rules):
        try:
            res = api("POST", "/tunnels/apply", {"rules": rules, "sync_linux": True}, timeout=40)
        except (urllib.error.URLError, OSError) as exc:
            messagebox.showerror("代理不可达", f"无法连接代理：{exc}\n请确认 WinLinuxTunnel 代理已启动。")
            return False
        if not res.get("ok"):
            messagebox.showwarning("应用未完全成功", res.get("error") or res.get("note", ""))
        self.refresh()
        return True

    def add_rule(self):
        self._rule_dialog(None)

    def edit_rule(self):
        r = self._selected_rule()
        if r:
            self._rule_dialog(r)

    def _rule_dialog(self, rule):
        d = tk.Toplevel(self.root)
        d.title("编辑规则" if rule else "添加隧道规则")
        d.geometry("460x340")
        d.transient(self.root)
        d.grab_set()
        try:
            d.iconbitmap(ICON)
        except tk.TclError:
            pass
        pad = {"padx": 12, "pady": 5}
        ttk.Label(d, text="方向（谁发起访问，监听口就在谁那边）",
                  font=("Segoe UI", 9, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        dir_var = tk.StringVar(value=(rule or {}).get("dir", "L"))
        ttk.Radiobutton(d, text="L：Windows 访问 Linux 服务", variable=dir_var, value="L").grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        ttk.Radiobutton(d, text="R：Linux 访问 Windows 服务", variable=dir_var, value="R").grid(row=2, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(d, text="监听端口").grid(row=3, column=0, sticky="w", **pad)
        listen_e = ttk.Entry(d, width=26)
        listen_e.grid(row=3, column=1, sticky="w", **pad)
        listen_e.insert(0, (rule or {}).get("listen", ""))
        ttk.Label(d, text="目标地址").grid(row=4, column=0, sticky="w", **pad)
        target_e = ttk.Entry(d, width=26)
        target_e.grid(row=4, column=1, sticky="w", **pad)
        target_e.insert(0, (rule or {}).get("target", ""))
        ttk.Label(d, text="备注").grid(row=5, column=0, sticky="w", **pad)
        note_e = ttk.Entry(d, width=26)
        note_e.grid(row=5, column=1, sticky="w", **pad)
        note_e.insert(0, (rule or {}).get("note", ""))
        en_var = tk.BooleanVar(value=(rule or {}).get("enabled", True))
        ttk.Checkbutton(d, text="启用", variable=en_var).grid(row=6, column=0, sticky="w", **pad)
        lan_var = tk.BooleanVar(value=(rule or {}).get("lan", False))
        ttk.Checkbutton(d, text="允许非回环监听", variable=lan_var).grid(row=6, column=1, sticky="w", **pad)
        ttk.Label(d, text="例：监听 3080，目标 127.0.0.1:3080", foreground=MUTE).grid(row=7, column=0, columnspan=2, sticky="w", **pad)

        def save():
            listen, target = listen_e.get().strip(), target_e.get().strip()
            if not listen or not target:
                messagebox.showinfo("提示", "监听端口和目标地址都不能为空", parent=d)
                return
            direction = dir_var.get()
            if rule:
                rid = rule["id"]
            else:
                rid = f"{direction.lower()}{listen.rsplit(':', 1)[-1]}"
                if any(r.get("id") == rid for r in self.rules):
                    messagebox.showinfo("提示", f"规则 {rid} 已存在，请改用编辑", parent=d)
                    return
            new = {"id": rid, "dir": direction, "listen": listen, "target": target,
                   "enabled": bool(en_var.get()), "lan": bool(lan_var.get()),
                   "note": note_e.get().strip()}
            rules = ([new if r.get("id") == rid else r for r in self.rules]
                     if rule else self.rules + [new])
            d.destroy()
            self._apply(rules)

        ttk.Button(d, text="保存并应用", command=save).grid(row=8, column=0, columnspan=2, pady=12)

    def del_rule(self):
        r = self._selected_rule()
        if r and messagebox.askyesno("确认", f"删除规则 {r['id']}？"):
            self._apply([x for x in self.rules if x.get("id") != r["id"]])

    def toggle_rule(self):
        r = self._selected_rule()
        if r:
            self._apply([dict(x, enabled=not x.get("enabled", True)) if x.get("id") == r["id"] else x
                         for x in self.rules])

    def probe_rule(self):
        r = self._selected_rule()
        if not r:
            return
        hp, label = ((r["listen"], "端到端（Windows 监听口 → Linux 目标）") if r["dir"] == "L"
                     else (r["target"], "目标侧检查（Windows 本地目标）"))
        host, port = hp.rsplit(":", 1)
        try:
            with socket.create_connection((host, int(port)), timeout=5):
                messagebox.showinfo("探测成功", f"{r['id']}：OK\n{label}")
        except OSError as exc:
            messagebox.showwarning("探测失败", f"{r['id']}：FAIL\n{label}\n{exc}")

    def sync_rules(self):
        self._apply(self.rules)

    # -- 桌面 ------------------------------------------------------------
    def _build_desktop(self):
        page = ttk.Frame(self.content)
        self.pages["desktop"] = page
        ttk.Label(page, text="桌面操作", font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(4, 8))
        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="截图并预览", command=self.do_shot).pack(side="left", padx=3)
        ttk.Button(bar, text="保存截图到…", command=self.do_shot_save).pack(side="left", padx=3)
        ttk.Button(bar, text="读取剪贴板", command=self.do_clipboard).pack(side="left", padx=3)
        ttk.Button(bar, text="刷新窗口列表", command=self.do_windows).pack(side="left", padx=3)
        paned = ttk.PanedWindow(page, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned)
        self.shot_label = ttk.Label(left, text="（截图预览）", anchor="center")
        self.shot_label.pack(fill="both", expand=True)
        paned.add(left, weight=1)
        right = ttk.Frame(paned)
        cols = ("hwnd", "pid", "process", "title")
        self.win_tree = ttk.Treeview(right, columns=cols, show="headings", height=12)
        for c, t, w in (("hwnd", "HWND", 80), ("pid", "PID", 60),
                        ("process", "进程", 130), ("title", "标题", 220)):
            self.win_tree.heading(c, text=t)
            self.win_tree.column(c, width=w, anchor="w")
        self.win_tree.pack(fill="both", expand=True)
        paned.add(right, weight=2)

    def do_shot(self):
        try:
            res = api("POST", "/screenshot", {"mode": "b64"}, timeout=30)
            raw = base64.b64decode(res.get("b64", ""))
            img = tk.PhotoImage(data=base64.b64encode(raw).decode("ascii"))
            factor = max(1, img.width() // 460)
            if factor > 1:
                img = img.subsample(factor, factor)
            self._shot_img = img
            self.shot_label.configure(image=img, text="")
            self.statusbar.config(text=f"截图 {res.get('width')}x{res.get('height')}，{res.get('size')} 字节")
        except (urllib.error.URLError, OSError, tk.TclError) as exc:
            messagebox.showerror("截图失败", str(exc))

    def do_shot_save(self):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(defaultextension=".png",
                                            filetypes=[("PNG 图片", "*.png")])
        if not path:
            return
        try:
            res = api("POST", "/screenshot", {"mode": "b64"}, timeout=30)
            Path(path).write_bytes(base64.b64decode(res.get("b64", "")))
            self.statusbar.config(text=f"截图已保存：{path}")
        except (urllib.error.URLError, OSError) as exc:
            messagebox.showerror("截图失败", str(exc))

    def do_clipboard(self):
        try:
            res = api("POST", "/clipboard", {"op": "get"})
            text = res.get("text", "")
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            messagebox.showinfo("剪贴板", f"已读取 {len(text)} 字符并复制到本机剪贴板：\n\n{text[:300]}")
        except (urllib.error.URLError, OSError) as exc:
            messagebox.showerror("读取失败", str(exc))

    def do_windows(self):
        try:
            res = api("GET", "/windows")
            self.win_tree.delete(*self.win_tree.get_children())
            for w in res.get("windows", []):
                self.win_tree.insert("", "end", values=(
                    w.get("hwnd"), w.get("pid"), w.get("process", ""), w.get("title", "")))
        except (urllib.error.URLError, OSError) as exc:
            messagebox.showerror("获取窗口失败", str(exc))

    # -- 日志 ------------------------------------------------------------
    def _build_log(self):
        page = ttk.Frame(self.content)
        self.pages["log"] = page
        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(4, 6))
        ttk.Label(bar, text="日志", font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Button(bar, text="刷新", command=self.refresh_log).pack(side="right")
        self.log_sel = tk.StringVar(value="agent.log")
        for name in ("agent.log", "tunnel-control.log", "tunnel-user.log"):
            ttk.Radiobutton(bar, text=name, variable=self.log_sel, value=name,
                            command=self.refresh_log).pack(side="right", padx=6)
        self.log_text = tk.Text(page, wrap="none", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        self.refresh_log()

    def refresh_log(self):
        p = APP_DIR / "logs" / self.log_sel.get()
        self.log_text.delete("1.0", "end")
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            self.log_text.insert("end", "\n".join(lines[-300:]))
        except OSError:
            self.log_text.insert("end", f"（无日志：{p}）")
        self.log_text.see("end")

    # -- 设置 ------------------------------------------------------------
    def _build_settings(self):
        page = ttk.Frame(self.content)
        self.pages["settings"] = page
        ttk.Label(page, text="设置", font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(4, 8))
        box = ttk.LabelFrame(page, text="当前连接配置（%USERPROFILE%\\.wlt\\wlt.conf）")
        box.pack(fill="x", padx=4, pady=6)
        for i, k in enumerate(("WLT_LINUX_HOST", "WLT_LINUX_USER", "WLT_SSH_PORT",
                               "WLT_REMOTE_ROOT", "WLT_AGENT_PORT", "WLT_TUNNEL_PORT")):
            ttk.Label(box, text=k).grid(row=i, column=0, sticky="w", padx=10, pady=2)
            ttk.Label(box, text=CONF.get(k, "") or "（未设置）", foreground=ACCENT).grid(
                row=i, column=1, sticky="w", padx=10, pady=2)
        opt = ttk.LabelFrame(page, text="行为")
        opt.pack(fill="x", padx=4, pady=6)
        ttk.Checkbutton(opt, text="关闭主窗口时最小化到系统托盘（而不是退出）",
                        variable=self.close_to_tray).pack(anchor="w", padx=10, pady=6)
        bar = ttk.Frame(page)
        bar.pack(fill="x", padx=4, pady=10)
        ttk.Button(bar, text="运行安装向导（配置 Linux 连接）", command=self.run_installer).pack(side="left", padx=4)
        ttk.Button(bar, text="打开配置目录", command=self.open_appdir).pack(side="left", padx=4)
        ttk.Button(bar, text="退出程序", command=self.quit_app).pack(side="right", padx=4)
        ttk.Label(page, text="安装向导会在一个控制台窗口中运行，按提示输入一次 Linux 密码即可。\n"
                             "首次使用请先运行安装向导；之后可在「隧道」页直接增删规则。",
                  foreground=MUTE, justify="left").pack(anchor="w", padx=8, pady=6)

    def run_installer(self):
        ps1 = WIN_DIR / "install-win.ps1"
        if not ps1.exists():
            messagebox.showerror("缺失", f"找不到安装脚本：{ps1}")
            return
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", str(ps1)], creationflags=subprocess.CREATE_NEW_CONSOLE)
        messagebox.showinfo("已启动", "安装向导已在新窗口打开，请按提示完成。\n完成后点「刷新」查看状态。")

    def open_appdir(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(APP_DIR))

    # -- 托盘 ------------------------------------------------------------
    def _init_tray(self):
        try:
            self._tray = wlt_tray.TrayIcon(ICON, "WinLinuxTunnel", self._tray_events)
            self._tray.start()
        except Exception:
            self._tray = None

    def _poll_tray(self):
        try:
            while True:
                ev = self._tray_events.get_nowait()
                kind = ev[0]
                if kind in ("click", "dblclick"):
                    self.restore_from_tray()
                elif kind == "menu":
                    self._tray_menu()
        except queue.Empty:
            pass
        self.root.after(150, self._poll_tray)

    def _tray_menu(self):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="显示主界面", command=self.restore_from_tray)
        m.add_separator()
        m.add_command(label="退出 WinLinuxTunnel", command=self.quit_app)
        try:
            m.tk_popup(*self.root.winfo_pointerxy())
        finally:
            m.grab_release()

    def on_close(self):
        if self.close_to_tray.get() and self._tray and self._tray.ok:
            self.root.withdraw()
            self._tray.set_tooltip("WinLinuxTunnel — 已最小化到托盘，双击恢复")
        else:
            self.quit_app()

    def restore_from_tray(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.refresh()

    def quit_app(self):
        if self._tray:
            self._tray.stop()
        self.root.destroy()

    # -- 刷新 ------------------------------------------------------------
    def refresh(self):
        if self._refreshing:
            return
        self._refreshing = True
        try:
            ping = api("GET", "/ping", timeout=5)
            st = api("GET", "/tunnels/status", timeout=5)
            self.connected = True
            self.conn_pill.config(text="● 已连接", fg=OK)
            c, u = st.get("control", {}), st.get("user", {})
            self._set_card(self.card_agent, "运行中", OK,
                           f"v{ping.get('version')}  {ping.get('host')}\n用户 {ping.get('user')}")
            self._set_card(self.card_control, "UP" if c.get("alive") else "DOWN",
                           OK if c.get("alive") else BAD,
                           ",".join(c.get("forwards", [])) or (c.get("last_error", "")[:60]))
            self._set_card(self.card_user, "UP" if u.get("alive") else "DOWN",
                           OK if u.get("alive") else BAD,
                           u.get("last_error", "")[:60] or f"{len(self.rules)} 条规则")
            self.rules = st.get("rules", [])
            up_n = sum(1 for r in self.rules if r.get("state") == "up")
            self._set_card(self.card_rules, str(len(self.rules)), ACCENT, f"{up_n} 条 up")
            self.dash_summary.config(
                text=f"代理版本 {ping.get('version')}（Python {ping.get('python')}）。"
                     f"控制隧道把 Linux 的 127.0.0.1:{CONF.get('WLT_TUNNEL_PORT')} 接到 Windows 代理；"
                     f"用户隧道按规则双向转发。规则增删即时生效，断线自动重连。")
            self.tree.delete(*self.tree.get_children())
            for r in self.rules:
                state = r.get("state", "?")
                tag = "up" if state == "up" else "off" if not r.get("enabled", True) else "down"
                self.tree.insert("", "end", iid=r.get("id"), tags=(tag,), values=(
                    "✓" if r.get("enabled", True) else "—", r.get("dir"),
                    r.get("listen"), r.get("target"), state, r.get("note", "")))
            self.statusbar.config(text=f"已连接 · 规则 {len(self.rules)} 条 · 端口 {CONF.get('WLT_AGENT_PORT')}")
            if self._tray:
                self._tray.set_tooltip(f"WinLinuxTunnel — 已连接，{len(self.rules)} 条规则")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.connected = False
            self.conn_pill.config(text="● 未连接", fg=BAD)
            for card in (self.card_agent, self.card_control, self.card_user, self.card_rules):
                self._set_card(card, "—", MUTE, "")
            self.dash_summary.config(text=f"无法连接代理（{exc}）。请确认 WinLinuxTunnel 代理已启动"
                                          f"（双击 win\\start-win.vbs），或先在「设置」页运行安装向导。")
            self.statusbar.config(text="未连接代理")
            if self._tray:
                self._tray.set_tooltip("WinLinuxTunnel — 未连接")
        finally:
            self._refreshing = False

    def _set_card(self, card, value, color, detail):
        _, val, det = card
        val.config(text=value, fg=color)
        det.config(text=detail)

    def _auto(self):
        self.refresh()
        self.root.after(5000, self._auto)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()