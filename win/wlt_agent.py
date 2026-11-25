#!/usr/bin/env python3
"""wlt agent (Windows side) - single-process control agent + tunnel manager.

Responsibilities:
  * HTTP control API on 127.0.0.1:<WLT_AGENT_PORT> (Bearer token auth).
  * Keep the CONTROL reverse SSH tunnel alive so Linux can reach this API.
  * Keep the USER tunnel alive: an arbitrary set of -L/-R forwards driven by
    a rule list mirrored from the Linux source of truth.
  * M0 endpoints: /ping /run /doctor /tunnels/status /tunnels/apply.
    Desktop ops, persistent jobs and file transfer land in M1.

Design notes (see DESIGN.md):
  * Control and user tunnels are TWO separate ssh processes. With
    ExitOnForwardFailure=yes a single bad -R rule kills its whole connection;
    isolating them means a broken user rule can never take down the control
    plane you need to fix it.
  * Rule changes restart the user ssh process (Win32-OpenSSH ControlMaster
    multiplexing is unreliable). Jobs and rules are persisted, so the 1-2s
    blip loses nothing.
  * Retry uses exponential backoff (10s -> 120s) to avoid retry storms when a
    forward fails the same way every tick (inherited from WinLinuxBridge).

Standard library only. ASCII comments. Python 3.8+.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The embeddable runtime's ._pth does NOT add the script directory to sys.path
# (system Python does). Insert it explicitly so sibling modules import under
# both the bundled runtime and a normal interpreter.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wlt_desktop as desktop
from wlt_jobs import JobManager

VERSION = "0.2.1"

# --------------------------------------------------------------------------
# Paths & configuration
# --------------------------------------------------------------------------

APP_DIR = Path(os.environ.get("WLT_APP_DIR", str(Path.home() / ".wlt")))
CONF_PATH = APP_DIR / "wlt.conf"
TOKEN_PATH = APP_DIR / "agent.token"
RULES_PATH = APP_DIR / "tunnels.json"
KEYS_DIR = APP_DIR / "keys"
LOGS_DIR = APP_DIR / "logs"

DEFAULTS = {
    "WLT_LINUX_HOST": "",
    "WLT_LINUX_USER": "",
    "WLT_SSH_PORT": "22",
    "WLT_REMOTE_ROOT": "",          # empty -> ~/wlt on Linux
    "WLT_AGENT_PORT": "8788",
    "WLT_TUNNEL_PORT": "18081",
    "WLT_TICK_SECONDS": "10",
}

log = logging.getLogger("wlt.agent")


def load_conf():
    """Read key=value conf; environment variables override the file."""
    conf = dict(DEFAULTS)
    if CONF_PATH.exists():
        for line in CONF_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip()
    for k in list(conf):
        if os.environ.get(k):
            conf[k] = os.environ[k]
    return conf


def load_token():
    """Token from env (installer sets it) or the ACL-protected file."""
    tok = os.environ.get("WLT_AGENT_TOKEN", "")
    if not tok and TOKEN_PATH.exists():
        tok = TOKEN_PATH.read_text(encoding="utf-8").strip()
    return tok


def setup_logging():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(LOGS_DIR / "agent.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(fh)
    log.addHandler(sh)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


# --------------------------------------------------------------------------
# Rule helpers (shared shape with the Linux CLI; validation lives on Linux)
# --------------------------------------------------------------------------

def rules_hash(rules):
    """Stable hash of the enabled rule set, for drift detection."""
    payload = json.dumps(rules, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def split_hostport(s, default_host="127.0.0.1"):
    """'3080' -> (127.0.0.1, 3080); '1.2.3.4:80' -> (1.2.3.4, 80)."""
    s = str(s).strip()
    if ":" in s:
        host, port = s.rsplit(":", 1)
        return (host or default_host), int(port)
    return default_host, int(s)


def user_forward_args(rules):
    """Build -L/-R arguments from the enabled rule list.

    L: listen on Windows, target resolved from Linux  -> -L listen:target
    R: listen on Linux, target resolved from Windows  -> -R listen:target
    """
    args = []
    for r in rules:
        if not r.get("enabled", True):
            continue
        d = r.get("dir")
        listen = r.get("listen", "")
        target = r.get("target", "")
        if d == "L":
            args += ["-L", f"{listen}:{target}"]
        elif d == "R":
            args += ["-R", f"{listen}:{target}"]
    return args


# --------------------------------------------------------------------------
# Tunnel manager
# --------------------------------------------------------------------------

# ssh options common to both tunnels. ExitOnForwardFailure makes a failed
# bind fatal (so we never report a tunnel "up" that leads nowhere);
# ServerAlive* detect a dead peer within ~60s.
SSH_BASE_OPTS = [
    "-N", "-T",
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ServerAliveInterval=20",
    "-o", "ServerAliveCountMax=3",
    "-o", "ConnectTimeout=10",
    "-o", "IdentitiesOnly=yes",
]

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class TunnelSlot:
    """One managed ssh process (control or user) with its own backoff state."""

    def __init__(self, name, conf):
        self.name = name
        self.conf = conf
        self.proc = None
        self.log_path = LOGS_DIR / f"tunnel-{name}.log"
        self.failures = 0
        self.retry_at = 0.0
        self.last_error = ""
        self.started_at = 0.0
        self.forwards = []          # forward strings currently requested

    # -- process lifecycle -------------------------------------------------

    def _ssh_exe(self):
        return self.conf.get("WLT_SSH_EXE") or "ssh"

    def _key(self):
        return str(KEYS_DIR / "tunnel_ed25519")

    def _target(self):
        host = self.conf["WLT_LINUX_HOST"]
        user = self.conf["WLT_LINUX_USER"]
        port = self.conf["WLT_SSH_PORT"]
        return f"{user}@{host}" if user else host, port

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, forwards):
        """Spawn ssh with the given forward args. Return True if it survived
        the settle window (proof the forwards bound; ssh -N aborts at once on
        a failed forward)."""
        self.stop()
        self.forwards = list(forwards)
        key = self._key()
        if not Path(key).exists():
            self.last_error = f"tunnel key missing: {key} - run install-win.ps1"
            log.warning("%s: %s", self.name, self.last_error)
            return False
        if not self.conf["WLT_LINUX_HOST"]:
            self.last_error = "WLT_LINUX_HOST not configured"
            return False
        target, port = self._target()
        known_hosts = str(Path.home() / ".ssh" / "known_hosts")
        args = [self._ssh_exe()] + SSH_BASE_OPTS + [
            "-i", key,
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-p", str(port),
        ] + forwards + [target]
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            # Truncate per attempt; the failure hint reads the latest text.
            lf = open(self.log_path, "w", encoding="utf-8", errors="replace")
            self.proc = subprocess.Popen(
                args, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            lf.close()  # child keeps its own handle
        except OSError as exc:
            self.last_error = f"spawn failed: {exc}"
            log.warning("%s: %s", self.name, self.last_error)
            return False
        time.sleep(1.2)  # settle: ssh -N exits immediately on forward failure
        if self.proc.poll() is not None:
            self.last_error = self.failure_hint()
            log.warning("%s: ssh exited during setup: %s", self.name, self.last_error)
            return False
        self.started_at = time.time()
        self.last_error = ""
        log.info("%s: started (%s)", self.name, " ".join(forwards) or "no forwards")
        return True

    def stop(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None

    # -- diagnostics -------------------------------------------------------

    def read_log(self):
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def failure_hint(self):
        """Turn ssh stderr into one actionable sentence (idea inherited from
        WinLinuxBridge: a failed forward used to be invisible)."""
        raw = self.read_log()
        if not raw.strip():
            return "ssh exited without an error message"
        m = re.search(r"remote port forwarding failed for listen port (\d+)", raw)
        if m:
            p = m.group(1)
            return (f"port {p} is still held on the Linux side by an earlier tunnel "
                    f"that never closed. On Linux: ss -tlnp | grep :{p}")
        if re.search(r"Permission denied|publickey|Too many authentication", raw):
            return "authentication refused - tunnel key not authorised; re-run install-win.ps1"
        if re.search(r"Could not resolve hostname|No route to host|Connection timed out|Connection refused", raw):
            return (f"cannot reach {self.conf['WLT_LINUX_HOST']}:{self.conf['WLT_SSH_PORT']} "
                    f"- check host is up and port is correct")
        if "Host key verification failed" in raw:
            return "host key mismatch - remove the stale entry from ~/.ssh/known_hosts and retry"
        if "local port forwarding failed" in raw or "bind" in raw.lower():
            return "a local listen port is already in use on this Windows machine"
        last = [ln for ln in raw.splitlines() if ln.strip()]
        return "ssh said: " + (last[-1] if last else "?")

    # -- backoff -----------------------------------------------------------

    def note_failure(self):
        self.failures += 1
        delay = min(10 * (2 ** (self.failures - 1)), 120)
        self.retry_at = time.time() + delay
        if self.failures == 1 or self.failures % 5 == 0:
            log.info("%s: retrying in %ds (attempt %d)", self.name, delay, self.failures + 1)

    def note_success(self):
        if self.failures:
            log.info("%s: recovered after %d failed attempt(s)", self.name, self.failures)
        self.failures = 0
        self.retry_at = 0.0

    def retry_due(self):
        return time.time() >= self.retry_at

    def status(self):
        return {
            "name": self.name,
            "alive": self.alive(),
            "forwards": self.forwards,
            "failures": self.failures,
            "last_error": self.last_error,
            "uptime_s": round(time.time() - self.started_at) if self.alive() else 0,
        }


class TunnelManager(threading.Thread):
    """Watchdog thread: keeps both tunnels alive, applies rule changes."""

    def __init__(self, conf):
        super().__init__(daemon=True, name="wlt-tunnels")
        self.conf = conf
        self.control = TunnelSlot("control", conf)
        self.user = TunnelSlot("user", conf)
        self.rules = self._load_rules()
        self._apply_event = threading.Event()
        self._apply_result = None
        self._apply_lock = threading.Lock()
        self._stop = threading.Event()

    def _load_rules(self):
        if RULES_PATH.exists():
            try:
                data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
                return data.get("rules", [])
            except (OSError, ValueError):
                log.warning("tunnels.json unreadable; starting with no user rules")
        return []

    def _save_rules(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "rules": self.rules}
        RULES_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # -- control plane -----------------------------------------------------

    def control_forwards(self):
        tp = self.conf["WLT_TUNNEL_PORT"]
        ap = self.conf["WLT_AGENT_PORT"]
        return ["-R", f"127.0.0.1:{tp}:127.0.0.1:{ap}"]

    def apply_rules(self, rules):
        """Called from the HTTP thread. Persist mirror, restart user tunnel,
        wait for the watchdog to settle it, return per-rule status."""
        with self._apply_lock:
            self.rules = rules
            self._save_rules()
            self._apply_result = None
            self._apply_event.set()
            # Wait for the watchdog to process (bounded).
            deadline = time.time() + 15
            while time.time() < deadline:
                if self._apply_result is not None:
                    return self._apply_result
                time.sleep(0.1)
            return {"ok": False, "error": "apply timed out waiting for tunnel manager"}

    def _do_apply(self):
        forwards = user_forward_args(self.rules)
        if not forwards:
            self.user.stop()
            self.user.forwards = []
            self.user.note_success()
            self._apply_result = {"ok": True, "rules": self._rule_states(),
                                  "note": "no enabled rules; user tunnel stopped"}
            return
        ok = self.user.start(forwards)
        if ok:
            self.user.note_success()
        else:
            self.user.note_failure()
        self._apply_result = {"ok": ok, "rules": self._rule_states(),
                              "error": self.user.last_error if not ok else ""}

    def _rule_states(self):
        """Per-rule view. 'up' means the user ssh is alive and the rule is
        enabled (its forward was requested); -L targets are lazy so a refused
        target only shows on actual use / probe."""
        up = self.user.alive()
        out = []
        for r in self.rules:
            enabled = r.get("enabled", True)
            out.append({
                "id": r.get("id"),
                "dir": r.get("dir"),
                "listen": r.get("listen"),
                "target": r.get("target"),
                "enabled": enabled,
                "state": ("up" if (up and enabled) else
                          "off" if not enabled else "down"),
                "note": r.get("note", ""),
            })
        return out

    def status(self):
        return {
            "control": self.control.status(),
            "user": self.user.status(),
            "rules": self._rule_states(),
            "rules_hash": rules_hash(self.rules),
        }

    # -- watchdog loop -----------------------------------------------------

    def run(self):
        tick = 0
        tick_s = max(int(self.conf.get("WLT_TICK_SECONDS", "10")), 1)
        # Start both immediately on boot.
        if self.control.start(self.control_forwards()):
            self.control.note_success()
        else:
            self.control.note_failure()
        if user_forward_args(self.rules):
            if self.user.start(user_forward_args(self.rules)):
                self.user.note_success()
            else:
                self.user.note_failure()
        while not self._stop.is_set():
            try:
                if self._apply_event.is_set():
                    self._apply_event.clear()
                    self._do_apply()
                # Control tunnel: keep alive.
                if not self.control.alive():
                    if self.control.retry_due():
                        if self.control.start(self.control_forwards()):
                            self.control.note_success()
                        else:
                            self.control.note_failure()
                # User tunnel: keep alive only if there are enabled rules.
                user_needed = bool(user_forward_args(self.rules))
                if user_needed and not self.user.alive() and not self._apply_event.is_set():
                    if self.user.retry_due():
                        if self.user.start(user_forward_args(self.rules)):
                            self.user.note_success()
                        else:
                            self.user.note_failure()
            except Exception as exc:  # never let the watchdog die
                log.error("tunnel loop error: %s", exc)
            tick += 1
            # Wake IMMEDIATELY when a rule apply is pending (event-driven),
            # otherwise after one tick. Waiting only on the tick made every
            # /tunnels/apply block up to tick_s (10s) -> the GUI felt frozen.
            self._apply_event.wait(tick_s)

    def shutdown(self):
        self._stop.set()
        self.control.stop()
        self.user.stop()


# --------------------------------------------------------------------------
# Command execution (M0: synchronous bounded /run; persistent jobs in M1)
# --------------------------------------------------------------------------

OUTPUT_CAP = 32 * 1024 * 1024      # 32 MB hard cap on captured output
SUMMARY_BYTES = 8 * 1024           # bounded inline summary per stream


def _bounded(data):
    """Return (summary_text, total_bytes, sha256) for a byte blob."""
    n = len(data)
    digest = hashlib.sha256(data).hexdigest()
    if n <= SUMMARY_BYTES:
        return data.decode("utf-8", errors="replace"), n, digest
    head = data[: SUMMARY_BYTES // 2].decode("utf-8", errors="replace")
    tail = data[-(SUMMARY_BYTES // 2):].decode("utf-8", errors="replace")
    return (head + f"\n... [{n - SUMMARY_BYTES} bytes omitted] ...\n" + tail), n, digest


def run_command(payload):
    """Synchronous exec. Structured (exe+argv) avoids all shell quoting;
    cmd string runs through cmd.exe /c (or powershell)."""
    timeout = float(payload.get("timeout", 120))
    cwd = payload.get("cwd") or None
    env = None
    if payload.get("env"):
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in payload["env"].items()})
    if payload.get("exe"):
        argv = [payload["exe"]] + [str(a) for a in payload.get("argv", [])]
        shell = False
    else:
        cmd = payload.get("cmd", "")
        if not cmd:
            raise ValueError("provide 'cmd' or 'exe'")
        sh = payload.get("shell", "cmd")
        if sh == "powershell":
            argv = ["powershell", "-NoProfile", "-Command", cmd]
        else:
            argv = ["cmd.exe", "/c", cmd]
        shell = False
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=env, shell=shell,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
        out_b = proc.stdout[:OUTPUT_CAP]
        err_b = proc.stderr[:OUTPUT_CAP]
        out, out_n, out_sha = _bounded(out_b)
        err, err_n, err_sha = _bounded(err_b)
        return {
            "ok": True, "rc": proc.returncode,
            "out": out, "out_bytes": out_n, "out_sha256": out_sha,
            "err": err, "err_bytes": err_n, "err_sha256": err_sha,
            "timed_out": False,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        }
    except subprocess.TimeoutExpired as exc:
        out_b = (exc.stdout or b"")[:OUTPUT_CAP]
        err_b = (exc.stderr or b"")[:OUTPUT_CAP]
        out, out_n, out_sha = _bounded(out_b)
        err, err_n, err_sha = _bounded(err_b)
        return {
            "ok": True, "rc": None, "timed_out": True,
            "out": out, "out_bytes": out_n, "out_sha256": out_sha,
            "err": err, "err_bytes": err_n, "err_sha256": err_sha,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
            "error_code": "TIMEOUT_KILLED",
        }
    except FileNotFoundError as exc:
        return {"ok": False, "error_code": "SPAWN_FAILED", "detail": str(exc)}


# --------------------------------------------------------------------------
# Rule sync to Linux (for GUI-originated changes)
# --------------------------------------------------------------------------

def push_rules_to_linux(conf, rules):
    """Push a rule set to the Linux source of truth over the management key.

    The Linux CLI (`wlt tunnel import`) is the single authoritative validator
    and the only writer of authorized_keys. GUI-originated changes MUST go
    through it, otherwise sshd would refuse the new forwards (permitopen /
    permitlisten not updated). CLI-originated changes already did this locally
    and call /tunnels/apply with sync_linux=false.
    """
    key = str(KEYS_DIR / "mgmt_ed25519")
    if not Path(key).exists():
        return False, "management key missing; cannot sync rules to Linux"
    host = conf["WLT_LINUX_HOST"]
    user = conf["WLT_LINUX_USER"]
    port = conf["WLT_SSH_PORT"]
    if not host:
        return False, "WLT_LINUX_HOST not configured"
    remote_root = conf.get("WLT_REMOTE_ROOT") or "~/wlt"
    target = f"{user}@{host}" if user else host
    args = ["ssh", "-i", key, "-p", str(port),
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10",
            target, f"{remote_root}/linux/wlt tunnel import"]
    payload = json.dumps({"rules": rules}, ensure_ascii=False).encode("utf-8")
    try:
        r = subprocess.run(args, input=payload, capture_output=True, timeout=30,
                           creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            return False, (r.stderr.decode("utf-8", "replace").strip()
                           or f"import exit {r.returncode}")
        return True, r.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wlt/" + VERSION

    # injected by main()
    conf = None
    token = None
    manager = None
    jobs = None

    def log_message(self, fmt, *args):
        log.debug("http: " + fmt, *args)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not self.token:
            return True  # no token configured yet (pre-install); loopback only
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Bearer "):
            return False
        return hmac.compare_digest(hdr[7:].strip(), self.token)

    def _payload(self):
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        return json.loads(raw.decode("utf-8"))

    def _dispatch(self, method):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if not self._authorized():
            self._json(401, {"error": "unauthorized",
                             "hint": "Authorization: Bearer <WLT_AGENT_TOKEN>"})
            return
        routes = {
            "/ping": self._ping,
            "/run": lambda p: run_command(p),
            "/doctor": self._doctor,
            "/tunnels/status": lambda p: self.manager.status(),
            "/tunnels/apply": self._tunnels_apply,
            # persistent jobs
            "/job/start": lambda p: self.jobs.start(p),
            "/job/status": lambda p: self.jobs.status(p.get("job_id", ""), float(p.get("wait") or 0)),
            "/job/logs": lambda p: self.jobs.read_log(p.get("job_id", ""), p.get("which", "stdout"),
                                                      int(p.get("since") or 0), int(p.get("limit") or 65536)),
            "/job/result": lambda p: self.jobs.read_result(p.get("job_id", "")),
            "/job/cancel": lambda p: self.jobs.cancel(p.get("job_id", ""), p.get("note", "")),
            "/job/list": lambda p: self.jobs.list_jobs(int(p.get("limit") or 50), p.get("request_id", "")),
            "/job/stats": lambda p: self.jobs.stats(),
            # desktop
            "/screenshot": lambda p: desktop.screenshot(p),
            "/monitors": lambda p: {"monitors": desktop.list_monitors()},
            "/input": lambda p: desktop.input_action(p),
            "/windows": lambda p: {"windows": desktop.list_windows()},
            "/window": lambda p: desktop.window_action(p),
            "/wait-window": lambda p: desktop.wait_window(p),
            "/clipboard": lambda p: desktop.clipboard(p),
            "/http": lambda p: desktop.http_call(p),
            "/file": lambda p: desktop.read_file(p),
            "/fs": lambda p: desktop.fs_op(p),
        }
        handler = routes.get(path)
        if handler is None:
            self._json(404, {"error": "no such endpoint", "path": path,
                             "endpoints": sorted(routes)})
            return
        try:
            payload = self._payload() if method == "POST" else {}
            result = handler(payload)
            self._json(200, result if isinstance(result, dict) else {"result": result})
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True
        except PermissionError as exc:
            self._json(403, {"error": "permission denied", "detail": str(exc)})
        except FileNotFoundError as exc:
            self._json(404, {"error": "not found", "detail": str(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            self._json(400, {"error": type(exc).__name__, "detail": str(exc)})
        except LookupError as exc:
            self._json(404, {"error": "not found", "detail": str(exc)})
        except Exception as exc:
            log.exception("handler error on %s", path)
            self._json(500, {"error": type(exc).__name__, "detail": str(exc)})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # -- endpoints ---------------------------------------------------------

    def _ping(self, _):
        return {
            "ok": True, "version": VERSION, "ts": now_iso(),
            "host": os.environ.get("COMPUTERNAME", os.uname().nodename if hasattr(os, "uname") else ""),
            "user": os.environ.get("USERNAME", ""),
            "python": sys.version.split()[0],
            "app_dir": str(APP_DIR),
            "rules_hash": rules_hash(self.manager.rules),
        }

    def _doctor(self, _):
        ssh_ver = ""
        try:
            r = subprocess.run(["ssh", "-V"], capture_output=True, timeout=5,
                               creationflags=CREATE_NO_WINDOW)
            ssh_ver = (r.stderr or r.stdout).decode("utf-8", errors="replace").strip()
        except OSError:
            ssh_ver = "ssh.exe not found"
        return {
            "version": VERSION,
            "agent_port": self.conf["WLT_AGENT_PORT"],
            "tunnel_port": self.conf["WLT_TUNNEL_PORT"],
            "linux_host": self.conf["WLT_LINUX_HOST"],
            "ssh_port": self.conf["WLT_SSH_PORT"],
            "ssh_version": ssh_ver,
            "python": sys.version.split()[0],
            "app_dir": str(APP_DIR),
            "token_configured": bool(self.token),
            "tunnels": self.manager.status(),
        }

    def _tunnels_apply(self, payload):
        rules = payload.get("rules")
        if not isinstance(rules, list):
            raise ValueError("'rules' must be a list")
        # Light shape check; authoritative validation lives in the Linux CLI.
        for r in rules:
            if r.get("dir") not in ("L", "R"):
                raise ValueError(f"rule {r.get('id')}: dir must be L or R")
            if not r.get("listen") or not r.get("target"):
                raise ValueError(f"rule {r.get('id')}: listen/target required")
        if payload.get("sync_linux"):
            ok, detail = push_rules_to_linux(self.conf, rules)
            if not ok:
                return {"ok": False, "error": f"linux sync failed: {detail}"}
        return self.manager.apply_rules(rules)


# --------------------------------------------------------------------------
# Single instance + main
# --------------------------------------------------------------------------

def acquire_lock():
    """Cross-platform single-instance lock via an OS file lock."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = APP_DIR / "agent.lock"
    fh = open(lock_path, "w")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh  # keep open for process lifetime


def main():
    setup_logging()
    lock = acquire_lock()
    if lock is None:
        log.info("another wlt agent is already running; exiting")
        return
    conf = load_conf()
    token = load_token()
    if not token:
        # Pre-install convenience: generate a token so the API is never open.
        token = secrets.token_urlsafe(32)
        APP_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(token, encoding="utf-8")
        log.info("generated new agent token at %s", TOKEN_PATH)

    manager = TunnelManager(conf)
    desktop.init(APP_DIR, [APP_DIR, Path.home()])
    jobs = JobManager(APP_DIR / "jobs")
    lost = jobs.recover()
    if lost:
        log.info("jobs: marked %d orphaned job(s) LOST on startup", lost)
    jobs.prune(int(os.environ.get("WLT_JOB_KEEP_DAYS", "7")))
    Handler.conf = conf
    Handler.token = token
    Handler.manager = manager
    Handler.jobs = jobs

    port = int(conf["WLT_AGENT_PORT"])
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    manager.start()
    log.info("wlt agent %s listening on 127.0.0.1:%d (app_dir=%s)", VERSION, port, APP_DIR)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown()
        httpd.server_close()
        log.info("wlt agent exit")


if __name__ == "__main__":
    main()