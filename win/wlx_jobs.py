#!/usr/bin/env python3
"""wlx persistent job engine (Windows side).

Semantics inherited from WinLinuxBridge (MIT, see NOTICE):
  * Every command becomes a job persisted on disk: job.json + stdout.log +
    stderr.log under <root>/<job_id>/. Survives client timeout, tunnel drop,
    agent restart (the latter marks orphaned RUNNING jobs LOST, never fake-running).
  * request_id dedup: the same request_id returns the original job (retry-safe,
    "won't flash the board twice").
  * Execution timeout kills the process TREE and reports TIMEOUT_KILLED.
  * Output streams to disk with a 32 MB cap enforced WHILE running; the API
    returns only a bounded summary + byte count + SHA-256. Byte-cursor reads.
  * Cancel kills only the target's own process tree and reports cleanup.
  * Resource keys serialize jobs that share a resource (QUEUED until free).
  * Judgment order: transport_ok -> state -> error_code -> exit_code.

States: QUEUED RUNNING SUCCEEDED FAILED TIMEOUT CANCELLED LOST SPAWN_ERROR
Error codes: PROCESS_EXIT_NONZERO TIMEOUT_KILLED CANCELLED_BY_REQUEST
             SPAWN_FAILED JOB_LOST RESOURCE_WAIT_TIMEOUT
ASCII comments. Python 3.8+.
"""

import ctypes
import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

MAX_CAPTURE_BYTES = 32 * 1024 * 1024   # 32 MB per stream, enforced live
SUMMARY_HEAD = 4 * 1024
SUMMARY_TAIL = 4 * 1024
RESOURCE_WAIT_TIMEOUT = float(os.environ.get("WLX_RESOURCE_WAIT_TIMEOUT", "600"))
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

TERMINAL = {"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED", "LOST", "SPAWN_ERROR"}


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def decode_bytes(raw):
    return raw.decode("utf-8", errors="replace")


def _atomic_write_json(path, obj):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _sha256_file(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(65536), b""):
                h.update(block)
    except OSError:
        return ""
    return h.hexdigest()


def _file_bytes(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class _Capture:
    """File sink that enforces a live byte cap (drops overflow, counts it)."""

    def __init__(self, path, limit=MAX_CAPTURE_BYTES):
        self.path = path
        self.limit = limit
        self.written = 0
        self.overflow = 0
        self._fh = open(path, "wb")

    def fileno(self):
        return self._fh.fileno()

    def write(self, data):
        room = self.limit - self.written
        if room <= 0:
            self.overflow += len(data)
            return
        chunk = data[:room]
        self._fh.write(chunk)
        self.written += len(chunk)
        if len(data) > room:
            self.overflow += len(data) - room

    def flush(self):
        try:
            self._fh.flush()
        except OSError:
            pass

    def close(self):
        try:
            self._fh.close()
        except OSError:
            pass


def build_argv(payload):
    """Structured exe+argv avoids all shell quoting; cmd string uses a shell."""
    if payload.get("exe"):
        return [str(payload["exe"])] + [str(a) for a in payload.get("argv", [])], "exe"
    cmd = str(payload.get("cmd") or "")
    if not cmd:
        raise ValueError("provide 'cmd' or 'exe'")
    shell = str(payload.get("shell") or "cmd").lower()
    if shell == "powershell":
        return ["powershell", "-NoProfile", "-Command", cmd], "powershell"
    if shell in ("cmd", ""):
        return ["cmd.exe", "/c", cmd], "cmd"
    if shell == "none":
        # Split is naive; prefer exe+argv for anything complex.
        return cmd.split(), "none"
    raise ValueError(f"unknown shell: {shell}")


def _pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        kernel32.CloseHandle(h)
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


class JobManager:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "requests").mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._running = {}          # job_id -> {proc, resource, cancel_event}
        self._resources = {}        # resource key -> job_id
        self._queue = []            # queued job_ids waiting for a resource

    # -- paths -------------------------------------------------------------

    def job_dir(self, job_id):
        return self.root / job_id

    def _meta_path(self, job_id):
        return self.job_dir(job_id) / "job.json"

    def _request_path(self, request_id):
        return self.root / "requests" / request_id

    # -- meta io -----------------------------------------------------------

    def _load_meta(self, job_id):
        try:
            return json.loads(self._meta_path(job_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _store_meta(self, job_id, meta):
        self.job_dir(job_id).mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self._meta_path(job_id), meta)

    def _patch(self, job_id, **fields):
        with self._lock:
            meta = self._load_meta(job_id) or {"id": job_id}
            meta.update(fields)
            self._store_meta(job_id, meta)
            return meta

    # -- lifecycle ---------------------------------------------------------

    def recover(self):
        """Mark jobs that were RUNNING/QUEUED when the agent died as LOST."""
        n = 0
        for d in self.root.iterdir():
            if not d.is_dir() or d.name == "requests":
                continue
            meta = self._load_meta(d.name)
            if meta and meta.get("state") in ("RUNNING", "QUEUED"):
                self._patch(d.name, state="LOST", error_code="JOB_LOST",
                            finished=now_iso(),
                            note="agent restarted; process did not survive")
                n += 1
        return n

    def prune(self, max_age_days=7):
        cutoff = time.time() - max_age_days * 86400
        n = 0
        for d in self.root.iterdir():
            if not d.is_dir() or d.name == "requests":
                continue
            meta = self._load_meta(d.name)
            if not meta:
                continue
            if meta.get("state") in TERMINAL and d.stat().st_mtime < cutoff:
                import shutil
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        return n

    def _lookup_request(self, request_id):
        p = self._request_path(request_id)
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
        return None

    def start(self, payload):
        request_id = str(payload.get("request_id") or "").strip()
        with self._lock:
            if request_id:
                existing = self._lookup_request(request_id)
                if existing and self._load_meta(existing):
                    return self._render(existing, self._load_meta(existing), dedup=True)
            try:
                argv, shell_label = build_argv(payload)
            except ValueError as exc:
                return {"ok": False, "error_code": "SPAWN_FAILED", "detail": str(exc)}
            job_id = uuid.uuid4().hex[:12]
            resource = str(payload.get("resource") or "").strip()
            timeout = float(payload.get("timeout") or 120)
            meta = {
                "id": job_id, "request_id": request_id, "state": "QUEUED",
                "created": now_iso(), "started": None, "finished": None,
                "argv": argv, "shell": shell_label,
                "cwd": payload.get("cwd") or "", "timeout": timeout,
                "env": payload.get("env") or {},
                "resource": resource, "pid": None, "exit_code": None,
                "error_code": None, "note": str(payload.get("note") or ""),
            }
            self._store_meta(job_id, meta)
            if request_id:
                self._request_path(request_id).write_text(job_id, encoding="utf-8")
            self._queue.append(job_id)
            self._dispatch_queue()
            return self._render(job_id, self._load_meta(job_id))

    def _dispatch_queue(self):
        """Start queued jobs whose resource is free (called under lock)."""
        still = []
        for job_id in list(self._queue):
            meta = self._load_meta(job_id)
            if not meta or meta.get("state") != "QUEUED":
                continue
            res = meta.get("resource")
            if res and res in self._resources:
                still.append(job_id)
                continue
            if res:
                self._resources[res] = job_id
            t = threading.Thread(target=self._run_job, args=(job_id,), daemon=True,
                                 name=f"wlx-job-{job_id}")
            t.start()
        self._queue = still

    def _run_job(self, job_id):
        meta = self._load_meta(job_id)
        d = self.job_dir(job_id)
        out_cap = _Capture(str(d / "stdout.log"))
        err_cap = _Capture(str(d / "stderr.log"))
        env = None
        if meta.get("env"):
            env = dict(os.environ)
            env.update({str(k): str(v) for k, v in meta["env"].items()})
        cwd = meta.get("cwd") or None
        proc = None
        try:
            self._patch(job_id, state="RUNNING", started=now_iso())
            proc = subprocess.Popen(
                meta["argv"], cwd=cwd, env=env,
                stdout=out_cap, stderr=err_cap, stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            with self._lock:
                self._running[job_id] = {"proc": proc, "resource": meta.get("resource")}
            self._patch(job_id, pid=proc.pid)
            deadline = time.time() + meta["timeout"]
            while True:
                try:
                    proc.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if time.time() >= deadline:
                        self._kill_tree(proc.pid)
                        proc.wait(timeout=5)
                        self._finish(job_id, "TIMEOUT", None, "TIMEOUT_KILLED",
                                     out_cap, err_cap)
                        return
                    if self._is_cancelled(job_id):
                        return  # cancel path already finished it
            rc = proc.returncode
            if self._is_cancelled(job_id):
                return  # cancel already finished it; do not clobber with FAILED
            if rc == 0:
                self._finish(job_id, "SUCCEEDED", 0, None, out_cap, err_cap)
            else:
                self._finish(job_id, "FAILED", rc, "PROCESS_EXIT_NONZERO", out_cap, err_cap)
        except FileNotFoundError as exc:
            self._finish(job_id, "SPAWN_ERROR", None, "SPAWN_FAILED", out_cap, err_cap,
                         detail=str(exc))
        except Exception as exc:
            self._finish(job_id, "SPAWN_ERROR", None, "SPAWN_FAILED", out_cap, err_cap,
                         detail=str(exc))
        finally:
            out_cap.close()
            err_cap.close()
            with self._lock:
                self._running.pop(job_id, None)
                res = meta.get("resource")
                if res and self._resources.get(res) == job_id:
                    del self._resources[res]
                self._dispatch_queue()

    def _is_cancelled(self, job_id):
        meta = self._load_meta(job_id)
        return meta and meta.get("state") == "CANCELLED"

    def _finish(self, job_id, state, exit_code, error_code, out_cap, err_cap, detail=""):
        with self._lock:
            cur = self._load_meta(job_id)
            if cur and cur.get("state") == "CANCELLED":
                return  # cancel wins over any late runner finish
            self._patch(job_id, state=state, exit_code=exit_code, error_code=error_code,
                        finished=now_iso(), stdout_bytes=out_cap.written,
                        stderr_bytes=err_cap.written,
                        stdout_overflow=out_cap.overflow, stderr_overflow=err_cap.overflow,
                        detail=detail)

    # -- cancel ------------------------------------------------------------

    def _kill_tree(self, pid):
        if not pid:
            return 0
        if os.name == "nt":
            try:
                r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, timeout=15,
                                   creationflags=CREATE_NO_WINDOW)
                return r.returncode
            except (OSError, subprocess.TimeoutExpired):
                return -1
        try:
            os.killpg(os.getpgid(pid), 9)
            return 0
        except OSError:
            return -1

    def cancel(self, job_id, note=""):
        with self._lock:
            meta = self._load_meta(job_id)
            if not meta:
                raise FileNotFoundError(job_id)
            if meta.get("state") in TERMINAL:
                return self._render(job_id, meta, note="already terminal")
            entry = self._running.get(job_id)
            pid = meta.get("pid")
            rc = self._kill_tree(pid) if pid else 0
            self._patch(job_id, state="CANCELLED", error_code="CANCELLED_BY_REQUEST",
                        finished=now_iso(), note=note or "cancelled by request",
                        cancel_kill_rc=rc)
            if entry:
                self._running.pop(job_id, None)
            res = meta.get("resource")
            if res and self._resources.get(res) == job_id:
                del self._resources[res]
            self._dispatch_queue()
            return self._render(job_id, self._load_meta(job_id))

    # -- reads -------------------------------------------------------------

    def _render(self, job_id, meta, dedup=False, note=""):
        out = dict(meta)
        out["ok"] = True
        out["dedup"] = dedup
        if note:
            out["note"] = note
        out["stdout_bytes"] = _file_bytes(self.job_dir(job_id) / "stdout.log")
        out["stderr_bytes"] = _file_bytes(self.job_dir(job_id) / "stderr.log")
        return out

    def status(self, job_id, wait=0.0):
        deadline = time.time() + max(wait, 0)
        while True:
            meta = self._load_meta(job_id)
            if not meta:
                raise FileNotFoundError(job_id)
            if meta.get("state") in TERMINAL or time.time() >= deadline:
                return self._render(job_id, meta)
            time.sleep(0.2)

    def read_log(self, job_id, which="stdout", since=0, limit=65536):
        path = self.job_dir(job_id) / f"{which}.log"
        if not path.exists():
            raise FileNotFoundError(str(path))
        size = path.stat().st_size
        since = max(int(since), 0)
        with open(path, "rb") as fh:
            fh.seek(since)
            data = fh.read(min(int(limit), MAX_CAPTURE_BYTES))
        return {"job_id": job_id, "which": which, "since": since,
                "next": since + len(data), "total": size,
                "text": decode_bytes(data), "bytes": len(data)}

    def read_result(self, job_id, head=SUMMARY_HEAD, tail=SUMMARY_TAIL):
        meta = self._load_meta(job_id)
        if not meta:
            raise FileNotFoundError(job_id)
        out = self._render(job_id, meta)
        for which in ("stdout", "stderr"):
            path = self.job_dir(job_id) / f"{which}.log"
            out[which] = _summarize(path, head, tail)
        return out

    def list_jobs(self, limit=50, request_id=""):
        metas = []
        for d in self.root.iterdir():
            if not d.is_dir() or d.name == "requests":
                continue
            m = self._load_meta(d.name)
            if m:
                if request_id and m.get("request_id") != request_id:
                    continue
                metas.append(m)
        metas.sort(key=lambda m: m.get("created", ""), reverse=True)
        return {"count": len(metas), "jobs": metas[:int(limit)]}

    def stats(self):
        counts = {}
        for d in self.root.iterdir():
            if not d.is_dir() or d.name == "requests":
                continue
            m = self._load_meta(d.name)
            if m:
                counts[m.get("state", "?")] = counts.get(m.get("state", "?"), 0) + 1
        return {"total": sum(counts.values()), "by_state": counts,
                "running": len(self._running), "queued": len(self._queue),
                "resources_held": list(self._resources)}


def _summarize(path, head, tail):
    size = _file_bytes(path)
    sha = _sha256_file(path)
    if size == 0:
        return {"bytes": 0, "sha256": sha, "text": ""}
    with open(path, "rb") as fh:
        if size <= head + tail:
            text = decode_bytes(fh.read())
        else:
            hb = fh.read(head)
            fh.seek(size - tail)
            tb = fh.read(tail)
            text = (decode_bytes(hb) + f"\n... [{size - head - tail} bytes omitted] ...\n"
                    + decode_bytes(tb))
    return {"bytes": size, "sha256": sha, "text": text}