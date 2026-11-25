#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wlx 图形界面（Windows 侧，中文）。

是本地控制代理的 HTTP 客户端（127.0.0.1:<WLX_AGENT_PORT>），独立进程：
GUI 崩溃不影响桥。四个页签：状态 / 隧道 / 日志 / 配置。
隧道面板的规则增删改经 /tunnels/apply?sync_linux 同步到 Linux 真相源，
由 Linux CLI 统一校验并重写 authorized_keys。

仅用标准库 Tkinter。随包便携运行时含 Tcl/Tk。Python 3.8+。
"""

import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


# Bundled-runtime bootstrap: the embeddable Python omits Tcl/Tk search paths;
# point them at the grafted libraries and make the DLLs discoverable. No-op
# when running under a normal system Python.
def _runtime_bootstrap():
    root = os.path.dirname(sys.executable)
    tcl = os.path.join(root, "tcl", "tcl8.6")
    if os.path.isdir(tcl):
        os.environ.setdefault("TCL_LIBRARY", tcl)
        os.environ.setdefault("TK_LIBRARY", os.path.join(root, "tcl", "tk8.6"))
        try:
            os.add_dll_directory(root)
        except (AttributeError, OSError):
            pass


_runtime_bootstrap()

import tkinter as tk
from tkinter import ttk, messagebox

APP_DIR = Path(os.environ.get("WLX_APP_DIR", str(Path.home() / ".wlx")))
WIN_DIR = Path(__file__).resolve().parent


def load_conf():
    conf = {"WLX_AGENT_PORT": "8788", "WLX_TUNNEL_PORT": "18081",
            "WLX_LINUX_HOST": "", "WLX_LINUX_USER": "", "WLX_SSH_PORT": "22",
            "WLX_REMOTE_ROOT": "~/wlx"}
    p = APP_DIR / "wlx.conf"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    return conf


CONF = load_conf()
PORT = CONF.get("WLX_AGENT_PORT", "8788")
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = ""
_tp = APP_DIR / "agent.token"
if _tp.exists():
    TOKEN = _tp.read_text(encoding="utf-8").strip()


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
        root.title("wlx — Windows↔Linux 双向桥")
        root.geometry("820x560")
        self.rules = []
        self._refreshing = False

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.tab_status = ttk.Frame(nb)
        self.tab_tunnel = ttk.Frame(nb)
        self.tab_log = ttk.Frame(nb)
        self.tab_conf = ttk.Frame(nb)
        nb.add(self.tab_status, text="  状态  ")
        nb.add(self.tab_tunnel, text="  隧道  ")
        nb.add(self.tab_log, text="  日志  ")
        nb.add(self.tab_conf, text="  配置  ")

        self._build_status()
        self._build_tunnel()
        self._build_log()
        self._build_conf()
        self.refresh()
        self._auto()

    # -- 状态页 ----------------------------------------------------------
    def _build_status(self):
        f = self.tab_status
        top = ttk.Frame(f); top.pack(fill="x", padx=8, pady=8)
        self.lbl_agent = ttk.Label(top, text="代理：检测中…", font=("", 10, "bold"))
        self.lbl_agent.pack(anchor="w")
        self.lbl_control = ttk.Label(top, text="控制隧道：—")
        self.lbl_control.pack(anchor="w")
        self.lbl_user = ttk.Label(top, text="用户隧道：—")
        self.lbl_user.pack(anchor="w")
        self.lbl_hash = ttk.Label(top, text="规则：—")
        self.lbl_hash.pack(anchor="w")
        ttk.Button(top, text="刷新", command=self.refresh).pack(anchor="w", pady=4)

        ttk.Label(f, text="隧道规则（实时状态）").pack(anchor="w", padx=8)
        cols = ("id", "dir", "listen", "target", "state", "note")
        self.status_tree = ttk.Treeview(f, columns=cols, show="headings", height=10)
        for c, t, w in (("id", "ID", 110), ("dir", "方向", 50), ("listen", "监听", 170),
                        ("target", "目标", 170), ("state", "状态", 70), ("note", "备注", 180)):
            self.status_tree.heading(c, text=t)
            self.status_tree.column(c, width=w, anchor="w")
        self.status_tree.pack(fill="both", expand=True, padx=8, pady=4)

    # -- 隧道页 ----------------------------------------------------------
    def _build_tunnel(self):
        f = self.tab_tunnel
        bar = ttk.Frame(f); bar.pack(fill="x", padx=8, pady=6)
        for text, cmd in (("添加规则", self.add_rule), ("编辑", self.edit_rule),
                          ("删除", self.del_rule), ("启用/停用", self.toggle_rule),
                          ("探测", self.probe_rule), ("同步到Linux", self.sync_rules)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=3)
        ttk.Label(bar, text="提示：L=Windows访问Linux，R=Linux访问Windows").pack(side="right")

        cols = ("id", "dir", "listen", "target", "enabled", "state", "note")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=14)
        for c, t, w in (("id", "ID", 110), ("dir", "方向", 50), ("listen", "监听", 160),
                        ("target", "目标", 160), ("enabled", "启用", 50),
                        ("state", "状态", 70), ("note", "备注", 160)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree.bind("<Double-1>", lambda e: self.edit_rule())

    def _selected_rule(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选择一条规则")
            return None
        rid = self.tree.item(sel[0], "values")[0]
        return next((r for r in self.rules if r.get("id") == rid), None)

    def _apply(self, rules):
        try:
            res = api("POST", "/tunnels/apply", {"rules": rules, "sync_linux": True}, timeout=40)
        except (urllib.error.URLError, OSError) as exc:
            messagebox.showerror("代理不可达", f"无法连接代理：{exc}\n请确认 wlx 代理已启动。")
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
        d.title("编辑规则" if rule else "添加规则")
        d.geometry("420x300")
        d.transient(self.root)
        d.grab_set()
        pad = {"padx": 8, "pady": 4}
        ttk.Label(d, text="方向（谁发起访问，监听口就在谁那边）").grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        dir_var = tk.StringVar(value=(rule or {}).get("dir", "L"))
        ttk.Radiobutton(d, text="L：Windows 访问 Linux", variable=dir_var, value="L").grid(row=1, column=0, sticky="w", **pad)
        ttk.Radiobutton(d, text="R：Linux 访问 Windows", variable=dir_var, value="R").grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(d, text="监听（端口 或 host:端口）").grid(row=2, column=0, sticky="w", **pad)
        listen_e = ttk.Entry(d, width=24); listen_e.grid(row=2, column=1, sticky="w", **pad)
        listen_e.insert(0, (rule or {}).get("listen", ""))
        ttk.Label(d, text="目标（host:端口）").grid(row=3, column=0, sticky="w", **pad)
        target_e = ttk.Entry(d, width=24); target_e.grid(row=3, column=1, sticky="w", **pad)
        target_e.insert(0, (rule or {}).get("target", ""))
        ttk.Label(d, text="备注").grid(row=4, column=0, sticky="w", **pad)
        note_e = ttk.Entry(d, width=24); note_e.grid(row=4, column=1, sticky="w", **pad)
        note_e.insert(0, (rule or {}).get("note", ""))
        en_var = tk.BooleanVar(value=(rule or {}).get("enabled", True))
        ttk.Checkbutton(d, text="启用", variable=en_var).grid(row=5, column=0, sticky="w", **pad)
        lan_var = tk.BooleanVar(value=(rule or {}).get("lan", False))
        ttk.Checkbutton(d, text="允许非回环监听(--lan)", variable=lan_var).grid(row=5, column=1, sticky="w", **pad)

        def save():
            listen = listen_e.get().strip()
            target = target_e.get().strip()
            if not listen or not target:
                messagebox.showinfo("提示", "监听和目标都不能为空", parent=d)
                return
            direction = dir_var.get()
            if rule:
                rid = rule["id"]
            else:
                port = listen.rsplit(":", 1)[-1]
                rid = f"{direction.lower()}{port}"
                if any(r.get("id") == rid for r in self.rules):
                    messagebox.showinfo("提示", f"规则 ID {rid} 已存在，请改用编辑", parent=d)
                    return
            new = {"id": rid, "dir": direction, "listen": listen, "target": target,
                   "enabled": bool(en_var.get()), "lan": bool(lan_var.get()),
                   "note": note_e.get().strip()}
            rules = [new if r.get("id") == rid else r for r in self.rules]
            if not rule:
                rules = self.rules + [new]
            d.destroy()
            self._apply(rules)

        ttk.Button(d, text="保存并应用", command=save).grid(row=6, column=0, columnspan=2, pady=10)

    def del_rule(self):
        r = self._selected_rule()
        if not r:
            return
        if messagebox.askyesno("确认", f"删除规则 {r['id']}？"):
            self._apply([x for x in self.rules if x.get("id") != r["id"]])

    def toggle_rule(self):
        r = self._selected_rule()
        if not r:
            return
        rules = [dict(x, enabled=not x.get("enabled", True)) if x.get("id") == r["id"] else x
                 for x in self.rules]
        self._apply(rules)

    def probe_rule(self):
        r = self._selected_rule()
        if not r:
            return
        if r["dir"] == "L":
            hp, label = r["listen"], "端到端（Windows监听口→Linux目标）"
        else:
            hp, label = r["target"], "目标侧检查（Windows本地目标）"
        host, port = hp.rsplit(":", 1)
        try:
            with socket.create_connection((host, int(port)), timeout=5):
                messagebox.showinfo("探测成功", f"{r['id']}：OK\n{label}")
        except OSError as exc:
            messagebox.showwarning("探测失败", f"{r['id']}：FAIL\n{label}\n{exc}")

    def sync_rules(self):
        self._apply(self.rules)

    # -- 日志页 ----------------------------------------------------------
    def _build_log(self):
        f = self.tab_log
        bar = ttk.Frame(f); bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="刷新日志", command=self.refresh_log).pack(side="left")
        self.log_sel = tk.StringVar(value="agent.log")
        for name in ("agent.log", "tunnel-control.log", "tunnel-user.log"):
            ttk.Radiobutton(bar, text=name, variable=self.log_sel, value=name,
                            command=self.refresh_log).pack(side="left", padx=6)
        self.log_text = tk.Text(f, wrap="none", height=20)
        self.log_text.pack(fill="both", expand=True, padx=8, pady=4)
        self.refresh_log()

    def refresh_log(self):
        p = APP_DIR / "logs" / self.log_sel.get()
        self.log_text.delete("1.0", "end")
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            self.log_text.insert("end", "\n".join(lines[-200:]))
        except OSError:
            self.log_text.insert("end", f"（无日志：{p}）")
        self.log_text.see("end")

    # -- 配置页 ----------------------------------------------------------
    def _build_conf(self):
        f = self.tab_conf
        box = ttk.LabelFrame(f, text="当前配置（%USERPROFILE%\\.wlx\\wlx.conf）")
        box.pack(fill="x", padx=8, pady=8)
        self.conf_labels = {}
        for i, k in enumerate(("WLX_LINUX_HOST", "WLX_LINUX_USER", "WLX_SSH_PORT",
                               "WLX_REMOTE_ROOT", "WLX_AGENT_PORT", "WLX_TUNNEL_PORT")):
            ttk.Label(box, text=k).grid(row=i, column=0, sticky="w", padx=8, pady=2)
            lbl = ttk.Label(box, text=CONF.get(k, "") or "（未设置）")
            lbl.grid(row=i, column=1, sticky="w", padx=8, pady=2)
            self.conf_labels[k] = lbl
        bar = ttk.Frame(f); bar.pack(fill="x", padx=8, pady=8)
        ttk.Button(bar, text="运行安装向导（配置Linux连接）", command=self.run_installer).pack(side="left", padx=4)
        ttk.Button(bar, text="打开配置目录", command=self.open_appdir).pack(side="left", padx=4)
        ttk.Label(f, text="安装向导会在一个控制台窗口中运行，按提示输入一次 Linux 密码即可。\n"
                          "首次使用请先运行安装向导；之后可在「隧道」页直接增删规则。",
                  justify="left").pack(anchor="w", padx=12, pady=8)

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

    # -- 刷新 ------------------------------------------------------------
    def refresh(self):
        if self._refreshing:
            return
        self._refreshing = True
        try:
            ping = api("GET", "/ping", timeout=5)
            st = api("GET", "/tunnels/status", timeout=5)
            self.lbl_agent.config(text=f"代理：运行中  v{ping.get('version')}  "
                                     f"主机={ping.get('host')}  用户={ping.get('user')}",
                                  foreground="green")
            c, u = st.get("control", {}), st.get("user", {})
            self.lbl_control.config(
                text=f"控制隧道：{'UP' if c.get('alive') else 'DOWN'}  "
                     f"{c.get('last_error', '')}",
                foreground="green" if c.get("alive") else "red")
            self.lbl_user.config(
                text=f"用户隧道：{'UP' if u.get('alive') else 'DOWN'}  "
                     f"{u.get('last_error', '')}",
                foreground="green" if u.get("alive") else "red")
            self.rules = st.get("rules", [])
            self.lbl_hash.config(text=f"规则：{len(self.rules)} 条  hash={st.get('rules_hash')}")
            for tree in (self.status_tree, self.tree):
                tree.delete(*tree.get_children())
            for r in self.rules:
                en = "是" if r.get("enabled", True) else "否"
                self.status_tree.insert("", "end", values=(
                    r.get("id"), r.get("dir"), r.get("listen"), r.get("target"),
                    r.get("state"), r.get("note", "")))
                self.tree.insert("", "end", values=(
                    r.get("id"), r.get("dir"), r.get("listen"), r.get("target"),
                    en, r.get("state"), r.get("note", "")))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.lbl_agent.config(text=f"代理：未运行或不可达（{exc}）", foreground="red")
            self.lbl_control.config(text="控制隧道：—")
            self.lbl_user.config(text="用户隧道：—")
        finally:
            self._refreshing = False

    def _auto(self):
        self.refresh()
        self.root.after(5000, self._auto)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()