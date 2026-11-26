#!/usr/bin/env python3
"""Unit tests for the Linux CLI additions and the Windows safety boundaries.

Process snapshot / taskkill / WM_CLOSE need a Windows desktop, so those calls
are not executed here. The tests cover parsing, the job payload, request-id
filesystem safety, the kill policy (including that the killer is not invoked),
log redaction and tail limits, and the HTTP route map.
"""

import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WIN = ROOT / "win"
sys.path.insert(0, str(WIN))

import wlt_agent  # noqa: E402
import wlt_desktop  # noqa: E402
import wlt_jobs  # noqa: E402
import wlt_proc  # noqa: E402


def _load_cli():
    # linux/wlt has no .py suffix, so the default suffix-based finder skips it.
    path = str(ROOT / "linux" / "wlt")
    loader = importlib.machinery.SourceFileLoader("wlt_cli", path)
    spec = importlib.util.spec_from_loader("wlt_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


CLI = _load_cli()

REQUEST_IDS = [
    ("flash-20261125-001", True),
    ("A", True),
    ("job_1.2", True),
    ("", True),          # empty means "no id", not an error
    ("../evil", False),
    ("..", False),
    (".", False),
    ("a/b", False),
    ("a\\b", False),
    ("flash:001", False),  # colon is illegal in a Windows filename
    (" has space", False),
    ("x" * 129, False),
]


def _sample_table(agent_pid=100):
    return [
        {"pid": 4, "ppid": 0, "name": "System"},
        {"pid": 50, "ppid": 4, "name": "explorer.exe"},
        {"pid": agent_pid, "ppid": 50, "name": "python.exe"},
        {"pid": 200, "ppid": agent_pid, "name": "flash.exe"},
        {"pid": 201, "ppid": 200, "name": "helper.exe"},
        {"pid": 300, "ppid": 50, "name": "notepad.exe"},
        {"pid": 400, "ppid": 50, "name": "lsass.exe"},
    ]


class RequestIdTests(unittest.TestCase):
    def test_cli_and_agent_agree(self):
        for value, ok in REQUEST_IDS:
            with self.subTest(value=value):
                cli_err = CLI.request_id_error(value)
                agent_err = wlt_jobs.request_id_error(value)
                self.assertEqual(bool(cli_err), not ok)
                self.assertEqual(bool(agent_err), not ok)
                if not ok:
                    self.assertEqual(cli_err, agent_err)

    def test_traversal_does_not_create_a_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = wlt_jobs.JobManager(root)
            with self.assertRaises(ValueError):
                jobs.start({"cmd": "echo hi", "request_id": "../evil"})
            self.assertEqual([p.name for p in root.iterdir()], ["requests"])
            self.assertEqual(list((root / "requests").iterdir()), [])
            self.assertFalse((root.parent / "evil").exists())

    def test_dedup_and_stored_exe_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = wlt_jobs.JobManager(root)
            payload = {
                "exe": r"C:\tools\flash.exe",
                "argv": ["--port", "COM3"],
                "cwd": r"C:\fw",
                "env": {"MODE": "lab"},
                "request_id": "flash-001",
                "timeout": 5,
            }
            first = jobs.start(payload)
            second = jobs.start(dict(payload))
            self.assertTrue(first.get("ok"))
            self.assertFalse(first.get("dedup"))
            self.assertTrue(second.get("dedup"))
            self.assertEqual(first["id"], second["id"])
            meta = json.loads((root / first["id"] / "job.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["argv"], [r"C:\tools\flash.exe", "--port", "COM3"])
            self.assertEqual(meta["shell"], "exe")
            self.assertEqual(meta["cwd"], r"C:\fw")
            self.assertEqual(meta["env"], {"MODE": "lab"})
            self.assertEqual(meta["request_id"], "flash-001")
            for thread in threading.enumerate():
                if thread.name.startswith("wlt-job-"):
                    thread.join(timeout=2)


class CliPayloadTests(unittest.TestCase):
    def _parse(self, argv):
        return CLI.build_parser().parse_args(argv)

    def test_sync_run_exposes_cwd_env_exe(self):
        args = self._parse([
            "run", "--cwd", r"C:\fw", "--env", "MODE=lab", "--env", "A=b=c",
            "--timeout", "30", "--exe", r"C:\tools\flash.exe", "--", "--port", "COM3",
        ])
        mode, body = CLI.payload_for_run(args)
        self.assertEqual(mode, "sync")
        self.assertEqual(body["cwd"], r"C:\fw")
        self.assertEqual(body["env"], {"MODE": "lab", "A": "b=c"})
        self.assertEqual(body["exe"], r"C:\tools\flash.exe")
        self.assertEqual(body["argv"], ["--port", "COM3"])
        self.assertNotIn("cmd", body)
        self.assertNotIn("request_id", body)
        self.assertEqual(body["timeout"], 30.0)

    def test_request_id_no_wait_and_wait_select_the_job_engine(self):
        for extra in (["--request-id", "flash-001"], ["--no-wait"], ["--wait", "45"]):
            args = self._parse(["run", *extra, "echo", "hi"])
            mode, body = CLI.payload_for_run(args)
            self.assertEqual(mode, "job", extra)
            self.assertEqual(body["cmd"], "echo hi")
            self.assertNotIn("exe", body)
        waited = self._parse(["run", "--request-id", "flash-001", "--no-wait", "echo", "hi"])
        self.assertEqual(CLI.client_wait_seconds(waited, margin=30), 150.0)
        explicit = self._parse(["run", "--wait", "45", "--timeout", "300", "echo", "hi"])
        self.assertEqual(CLI.client_wait_seconds(explicit, margin=30), 45.0)
        self.assertEqual(explicit.timeout, 300.0)

    def test_plain_run_stays_synchronous(self):
        args = self._parse(["run", "--shell", "powershell", "Get-Date"])
        mode, body = CLI.payload_for_run(args)
        self.assertEqual(mode, "sync")
        self.assertEqual(body["shell"], "powershell")
        self.assertEqual(body["cmd"], "Get-Date")
        self.assertFalse(CLI.run_uses_job(args))

    def test_env_and_request_id_rejected(self):
        args = self._parse(["run", "--env", "NOEQUALS", "echo"])
        with self.assertRaises(ValueError):
            CLI.payload_for_run(args)
        args = self._parse(["run", "--request-id", "../x", "echo"])
        with self.assertRaises(ValueError):
            CLI.payload_for_run(args)
        args = self._parse(["run"])
        with self.assertRaises(ValueError):
            CLI.payload_for_run(args)

    def test_close_focus_proc_kill_log_parse(self):
        close = self._parse(["close", "--title", "Notepad"])
        self.assertEqual(CLI.window_target_body("close", close.title, close.hwnd),
                         {"action": "close", "title": "Notepad"})
        both = self._parse(["close", "--title", "Notepad", "--hwnd", "42"])
        body = CLI.window_target_body("close", both.title, both.hwnd)
        self.assertEqual(body, {"action": "close", "hwnd": "42"})
        with self.assertRaises(ValueError):
            CLI.window_target_body("focus", None, None)
        proc = self._parse(["proc", "--name", "note", "--pid", "9", "--limit", "10"])
        self.assertEqual(proc.name, "note")
        self.assertEqual(proc.pid, 9)
        self.assertEqual(self._parse(["kill", "4321"]).pid, 4321)
        log = self._parse(["log", "--source", "control", "--lines", "50"])
        self.assertEqual(log.source, "control")
        self.assertEqual(log.lines, 50)
        self.assertEqual(self._parse(["log"]).source, "all")
        commands = CLI.build_parser()._subparsers._group_actions[0].choices
        for name in ("close", "proc", "kill", "log", "windows", "run"):
            self.assertIn(name, commands)
        self.assertNotIn("lanes", commands)
        self.assertNotIn("ps", commands)


class WindowActionTests(unittest.TestCase):
    def test_close_is_a_real_action_and_unknown_is_not(self):
        self.assertEqual(wlt_desktop.normalize_window_action("CLOSE"), "close")
        self.assertIn("close", wlt_desktop.WINDOW_ACTIONS)
        with self.assertRaises(ValueError) as unknown:
            wlt_desktop.window_action({"action": "destroy", "hwnd": 1})
        self.assertIn("unknown window action", str(unknown.exception))
        with self.assertRaises(ValueError) as missing:
            wlt_desktop.window_action({"action": "close"})
        self.assertIn("hwnd or title", str(missing.exception))


class ProcessPolicyTests(unittest.TestCase):
    def test_kill_allows_only_the_target_tree(self):
        called = []

        def killer(pid):
            called.append(pid)
            return 0, "ok"

        result = wlt_proc.kill_process(
            {"pid": 200}, 100, _sample_table(), killer)
        self.assertTrue(result["ok"])
        self.assertEqual(called, [200])
        self.assertEqual(result["tree"], [200, 201])
        self.assertEqual(result["name"], "flash.exe")
        self.assertNotIn(100, result["tree"])

    def test_kill_refuses_agent_ancestors_and_protected_names(self):
        called = []

        def killer(pid):
            called.append(pid)
            return 0, "should not run"

        rows = _sample_table()
        for pid, exc in (
            (100, PermissionError),   # the agent
            (50, PermissionError),    # ancestor; its tree contains the agent
            (4, PermissionError),     # System
            (400, PermissionError),   # protected image
            (999, LookupError),       # not in the snapshot
            (0, ValueError),
            ("nope", ValueError),
        ):
            with self.subTest(pid=pid):
                with self.assertRaises(exc):
                    wlt_proc.kill_process({"pid": pid}, 100, rows, killer)
        self.assertEqual(called, [])

    def test_sibling_of_the_agent_is_allowed(self):
        called = []
        result = wlt_proc.kill_process(
            {"pid": 300}, 100, _sample_table(), lambda pid: called.append(pid) or (0, ""))
        self.assertTrue(result["ok"])
        self.assertEqual(called, [300])
        self.assertEqual(result["tree"], [300])

    def test_cycle_and_missing_agent_are_refused(self):
        cycled = [
            {"pid": 1, "ppid": 2, "name": "a.exe"},
            {"pid": 2, "ppid": 1, "name": "b.exe"},
        ]
        with self.assertRaises(PermissionError):
            wlt_proc.assert_kill_allowed(2, 1, cycled)
        with self.assertRaises(PermissionError):
            wlt_proc.assert_kill_allowed(300, 100, [
                {"pid": 300, "ppid": 50, "name": "notepad.exe"},
            ])

    def test_list_marks_the_agent_and_caps_the_page(self):
        page = wlt_proc.list_processes(
            {"name": "EXE", "limit": 2}, 100, _sample_table())
        self.assertTrue(page["truncated"])
        self.assertGreater(page["count"], 2)
        self.assertEqual(len(page["processes"]), 2)
        self.assertTrue(all("exe" in row["name"].lower() for row in page["processes"]))
        full = wlt_proc.list_processes({}, 100, _sample_table())
        marked = [row for row in full["processes"] if row["self"]]
        self.assertEqual([row["pid"] for row in marked], [100])
        self.assertEqual(full["agent_pid"], 100)
        with self.assertRaises(ValueError):
            wlt_proc.filter_processes(_sample_table(), limit=0)


class LogTests(unittest.TestCase):
    TOKEN = "super-secret-token-value"

    def test_redact_keeps_ordinary_log_lines(self):
        text = "\n".join([
            "tunnel key missing: C:\\Users\\a\\.wlt\\keys\\tunnel_ed25519",
            "generated new agent token at C:\\Users\\a\\.wlt\\agent.token",
            "Authorization: Bearer " + self.TOKEN,
            "WLT_AGENT_TOKEN=" + self.TOKEN,
            "password=hunter2",
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexamplepublickeymaterial",
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "b3BlbnNzaC1rZXkB",
            "-----END OPENSSH PRIVATE KEY-----",
            "plain progress line",
        ])
        cleaned, redacted = wlt_agent.redact_secrets(text, [self.TOKEN])
        self.assertTrue(redacted)
        self.assertNotIn(self.TOKEN, cleaned)
        self.assertNotIn("hunter2", cleaned)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", cleaned)
        self.assertNotIn("AAAAC3NzaC1lZDI1NTE5AAAAIexamplepublickeymaterial", cleaned)
        self.assertIn("tunnel key missing", cleaned)
        self.assertIn("agent.token", cleaned)
        self.assertIn("plain progress line", cleaned)
        self.assertIn("WLT_AGENT_TOKEN=***", cleaned)

    def test_tail_does_not_read_the_whole_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.log"
            head = ("STARTMARKER " + self.TOKEN + "\n") * 4000
            tail = "\n".join(f"tail-line-{i} token={self.TOKEN}" for i in range(30)) + "\n"
            path.write_bytes((head + tail).encode("utf-8"))
            self.assertGreater(path.stat().st_size, wlt_agent.MAX_LOG_READ_BYTES)
            info = wlt_agent.read_log_tail(
                path, max_lines=10, max_bytes=wlt_agent.MAX_LOG_RETURN_BYTES,
                read_bytes=wlt_agent.MAX_LOG_READ_BYTES, secrets=[self.TOKEN])
            self.assertTrue(info["truncated"])
            self.assertTrue(info["redacted"])
            self.assertNotIn("STARTMARKER", info["text"])
            self.assertNotIn(self.TOKEN, info["text"])
            self.assertIn("tail-line-29", info["text"])
            self.assertLessEqual(len(info["text"].encode("utf-8")), wlt_agent.MAX_LOG_RETURN_BYTES)
            self.assertLessEqual(info["lines"], 10)

    def test_read_logs_clamps_lines_and_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "agent.log").write_text("agent ok\n", encoding="utf-8")
            result = wlt_agent.read_logs({"source": "all", "lines": 99999}, tmp, [self.TOKEN])
            self.assertEqual(result["lines"], wlt_agent.MAX_LOG_LINES)
            self.assertEqual([item["source"] for item in result["logs"]], ["agent", "control", "user"])
            self.assertFalse(result["logs"][0]["missing"])
            self.assertTrue(result["logs"][1]["missing"])
            self.assertNotIn(self.TOKEN, json.dumps(result))
            with self.assertRaises(ValueError):
                wlt_agent.read_logs({"source": "lanes"}, tmp)
            with self.assertRaises(ValueError):
                wlt_agent.clamp_log_lines(0)


class RouteTests(unittest.TestCase):
    def test_route_map(self):
        handler = wlt_agent.Handler.__new__(wlt_agent.Handler)
        handler.token = "x" * 16
        handler.jobs = None
        handler.manager = None
        handler.conf = {}
        names = set(handler.routes())
        for path in ("/proc", "/kill", "/log", "/window", "/windows", "/run",
                     "/job/start", "/wait-window"):
            self.assertIn(path, names)
        self.assertNotIn("/lanes", names)
        self.assertNotIn("/close", names)
        self.assertNotIn("/ps", names)

    def test_log_endpoint_over_loopback(self):
        token = "loopback-secret-token-value"
        old_logs = wlt_agent.LOGS_DIR
        old_token = wlt_agent.Handler.token
        with tempfile.TemporaryDirectory() as tmp:
            wlt_agent.LOGS_DIR = Path(tmp)
            (Path(tmp) / "agent.log").write_text(
                "hello\nAuthorization: Bearer " + token + "\n", encoding="utf-8")
            wlt_agent.Handler.token = token
            wlt_agent.Handler.jobs = None
            wlt_agent.Handler.manager = None
            wlt_agent.Handler.conf = {}
            httpd = wlt_agent.ThreadingHTTPServer(("127.0.0.1", 0), wlt_agent.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = httpd.server_address
                self.assertEqual(host, "127.0.0.1")

                def call(path, bearer=None, body=None):
                    data = None if body is None else json.dumps(body).encode("utf-8")
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}{path}", data=data, method="POST" if body is not None else "GET")
                    if bearer:
                        req.add_header("Authorization", "Bearer " + bearer)
                    if data is not None:
                        req.add_header("Content-Type", "application/json")
                    try:
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            return resp.status, resp.read().decode("utf-8")
                    except urllib.error.HTTPError as exc:
                        return exc.code, exc.read().decode("utf-8")

                status, body = call("/log")
                self.assertEqual(status, 401)
                self.assertNotIn(token, body)
                status, body = call("/log", bearer=token, body={"source": "agent", "lines": 20})
                self.assertEqual(status, 200, body)
                self.assertIn("hello", body)
                self.assertNotIn(token, body)
                self.assertIn("***", body)
                status, body = call("/nope", bearer=token)
                self.assertEqual(status, 404)
                self.assertNotIn("lanes", body)
            finally:
                httpd.shutdown()
                httpd.server_close()
                wlt_agent.LOGS_DIR = old_logs
                wlt_agent.Handler.token = old_token


if __name__ == "__main__":
    unittest.main()
