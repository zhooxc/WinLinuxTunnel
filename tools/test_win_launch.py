#!/usr/bin/env python3
"""Launch and packaging checks that run without a Windows build.

Covers the unified entry, the single-instance lock, GUI-style agent spawn,
payload zip safety, and the source archive's refusal to ship secrets or the
legacy embeddable runtime.

Usage: python tools/test_win_launch.py
"""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIN = ROOT / "win"
sys.path.insert(0, str(WIN))
sys.path.insert(0, str(ROOT / "tools"))

import wlt_launch
import wlt_payload
import wlt_setup


def check(cond, message):
    if not cond:
        raise SystemExit("FAIL: " + message)
    print("ok  " + message)


def test_agent_command_dev():
    cmd = wlt_launch.agent_command()
    check(cmd[0] == sys.executable, "dev agent uses this interpreter")
    check(cmd[1].endswith("wlt_main.py"), "dev agent entry is wlt_main.py")
    check(cmd[2:] == ["--agent"], "dev agent argv is --agent")


def test_agent_command_frozen(monkey_exe):
    old = getattr(sys, "frozen", None)
    sys.frozen = True
    try:
        cmd = wlt_launch.agent_command()
    finally:
        if old is None:
            delattr(sys, "frozen")
        else:
            sys.frozen = old
    check(cmd == [str(Path(sys.executable).resolve()), "--agent"],
          "frozen agent is the same exe --agent")


def test_spawn_strips_token():
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        captured["flags"] = kwargs.get("creationflags", 0)

        class Proc:
            pid = 0
        return Proc()

    old = wlt_launch.subprocess.Popen
    wlt_launch.subprocess.Popen = fake_popen
    os.environ["WLT_AGENT_TOKEN"] = "stale-token-should-not-pass"
    try:
        wlt_launch.spawn_agent()
    finally:
        wlt_launch.subprocess.Popen = old
        os.environ.pop("WLT_AGENT_TOKEN", None)
    check("WLT_AGENT_TOKEN" not in captured["env"], "spawn drops stale WLT_AGENT_TOKEN")
    check(captured["cmd"][-1] == "--agent", "spawn argv ends with --agent")
    if os.name == "nt":
        check(captured["flags"] & 0x08000000, "Windows spawn sets CREATE_NO_WINDOW")


def test_zip_slip_and_symlink():
    tmp = Path(tempfile.mkdtemp(prefix="wlt-zip-"))
    try:
        good = tmp / "good.zip"
        with zipfile.ZipFile(good, "w") as zf:
            zf.writestr("WinLinuxTunnel.exe", b"exe")
            zf.writestr("linux/wlt", b"cli")
        dest = tmp / "out"
        wlt_setup.extract_zip(good, dest)
        check((dest / "WinLinuxTunnel.exe").read_bytes() == b"exe", "extract keeps files")
        check((dest / "linux" / "wlt").read_bytes() == b"cli", "extract keeps nested files")

        bad = tmp / "slip.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../evil.txt", b"nope")
        try:
            wlt_setup.extract_zip(bad, tmp / "slip")
            raise SystemExit("FAIL: zip slip was accepted")
        except ValueError:
            check(True, "zip slip is rejected")

        link = tmp / "link.zip"
        info = zipfile.ZipInfo("evil-link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK << 16) | 0o777
        with zipfile.ZipFile(link, "w") as zf:
            zf.writestr(info, "/tmp/secret")
        try:
            wlt_setup.extract_zip(link, tmp / "link")
            raise SystemExit("FAIL: symlink member was accepted")
        except ValueError:
            check(True, "symlink member is rejected")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_payload_allowlist():
    tmp = Path(tempfile.mkdtemp(prefix="wlt-payload-"))
    try:
        onedir = tmp / "onedir"
        (onedir / "_internal").mkdir(parents=True)
        (onedir / "WinLinuxTunnel.exe").write_bytes(b"MZ")
        (onedir / "_internal" / "python.dll").write_bytes(b"dll")
        (onedir / "agent.token").write_text("secret", encoding="ascii")
        root = tmp / "repo"
        (root / "win").mkdir(parents=True)
        (root / "linux").mkdir()
        (root / "docs").mkdir()
        (root / "win" / "runtime").mkdir()
        (root / "win" / "install-win.ps1").write_text("# installer\n", encoding="ascii")
        (root / "win" / "wlt.conf.example").write_text("# example\n", encoding="ascii")
        (root / "win" / "runtime" / "python.exe").write_bytes(b"nope")
        (root / "linux" / "wlt").write_text("#!/usr/bin/env python3\n", encoding="ascii")
        (root / "docs" / "USAGE.md").write_text("usage\n", encoding="ascii")
        (root / "keys").mkdir()
        (root / "keys" / "id.pem").write_text("PRIVATE", encoding="ascii")
        (root / "VERSION").write_text("9.9.9\n", encoding="ascii")
        (root / "README.md").write_text("readme\n", encoding="ascii")
        (root / "agent.token").write_text("nope", encoding="ascii")
        zpath = tmp / "payload.zip"
        wlt_payload.build_payload_zip(onedir, zpath, root)
        names = wlt_payload.verify_payload_zip(zpath)
        blob = "\n".join(sorted(names))
        check("WinLinuxTunnel.exe" in names, "payload has the exe")
        check("install-win.ps1" in names, "payload has install-win.ps1")
        check("linux/wlt" in names, "payload has the Linux CLI")
        check("agent.token" not in blob, "payload has no token")
        check("runtime" not in blob, "payload has no embeddable runtime")
        check(".pem" not in blob, "payload has no private key")
        check(not any(n.startswith("win/") for n in names), "payload does not ship the source tree")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_source_archive_excludes():
    files = list(wlt_payload.iter_source_files(ROOT))
    rels = {p.relative_to(ROOT).as_posix() for p in files}
    check("win/wlt_main.py" in rels, "source zip includes the unified entry")
    check("tools/build-windows.ps1" in rels, "source zip includes the Windows build script")
    check("linux/wlt" in rels, "source zip includes the Linux CLI")
    check(not any("runtime/" in r or r.endswith("agent.token") for r in rels),
          "source zip skips runtime and tokens")


def test_help():
    result = subprocess.run(
        [sys.executable, str(WIN / "wlt_main.py"), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    check(result.returncode == 0, "--help exits 0")
    check("WinLinuxTunnel.exe --agent" in result.stdout, "--help documents --agent")


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _ping(port, token):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/ping" % port,
        headers={"Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(req, timeout=2) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_ping(port, token_path, timeout=15):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        token = ""
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
        if token:
            try:
                return _ping(port, token), token
            except (urllib.error.URLError, TimeoutError, OSError, ConnectionError) as exc:
                last = str(exc)
        time.sleep(0.15)
    raise SystemExit("FAIL: agent did not answer /ping (%s)" % last)


def test_agent_lifecycle():
    tmp = Path(tempfile.mkdtemp(prefix="wlt-agent-"))
    port = _free_port()
    conf = "\n".join([
        "WLT_LINUX_HOST=",
        "WLT_LINUX_USER=",
        "WLT_SSH_PORT=22",
        "WLT_AGENT_PORT=%d" % port,
        "WLT_TUNNEL_PORT=18081",
        "",
    ])
    (tmp / "wlt.conf").write_text(conf, encoding="utf-8")
    (tmp / "agent.token").write_text("file-token-ok", encoding="ascii")
    env = os.environ.copy()
    env["WLT_APP_DIR"] = str(tmp)
    # Direct --agent honors WLT_AGENT_TOKEN when it is set. The GUI spawn
    # path strips it (see test_spawn_agent_real); this launch uses the file.
    env.pop("WLT_AGENT_TOKEN", None)
    env.pop("WLT_AGENT_PORT", None)
    log_path = tmp / "child-out.txt"
    log = open(log_path, "w", encoding="utf-8")
    cmd = [sys.executable, str(WIN / "wlt_main.py"), "--agent"]
    proc = subprocess.Popen(cmd, cwd=str(WIN), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        body, token = _wait_ping(port, tmp / "agent.token")
        check(token == "file-token-ok", "agent uses the token file, not a stale env value")
        check(body.get("ok") is True, "/ping ok")
        check(body.get("version") == "0.3.0", "agent version is 0.3.0")
        check((tmp / "agent.pid").read_text(encoding="ascii").strip() == str(proc.pid),
              "agent.pid records the process")
        dup = subprocess.run(cmd, cwd=str(WIN), env=env, capture_output=True, text=True, timeout=20)
        check(dup.returncode == 0, "second --agent exits 0 (single-instance lock)")
        check(proc.poll() is None, "first agent still running after duplicate start")
        body2, _token2 = _wait_ping(port, tmp / "agent.token", timeout=5)
        check(body2.get("ok") is True, "agent still answers after duplicate start")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/shutdown" % port, data=b"{}", method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            shut = json.loads(resp.read().decode("utf-8"))
        check(shut.get("shutting_down") is True, "POST /shutdown accepted")
        proc.wait(timeout=10)
        check(proc.returncode == 0, "agent exits after /shutdown")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()
        if os.environ.get("WLT_TEST_KEEP") != "1":
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print("kept " + str(tmp))


def test_spawn_agent_real():
    """The same path the GUI uses: spawn_agent(), then /ping."""
    tmp = Path(tempfile.mkdtemp(prefix="wlt-spawn-"))
    port = _free_port()
    (tmp / "wlt.conf").write_text(
        "WLT_AGENT_PORT=%d\nWLT_TUNNEL_PORT=18081\nWLT_LINUX_HOST=\nWLT_SSH_PORT=22\n" % port,
        encoding="utf-8",
    )
    old_app = os.environ.get("WLT_APP_DIR")
    old_token = os.environ.get("WLT_AGENT_TOKEN")
    os.environ["WLT_APP_DIR"] = str(tmp)
    os.environ["WLT_AGENT_TOKEN"] = "should-not-reach-child"
    proc = None
    try:
        proc = wlt_launch.spawn_agent()
        body, token = _wait_ping(port, tmp / "agent.token")
        check(body.get("ok") is True, "spawn_agent reaches /ping")
        check(token != "should-not-reach-child", "spawned agent did not keep the parent token")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/shutdown" % port, data=b"{}", method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5).read()
        proc.wait(timeout=10)
        check(proc.returncode == 0, "spawned agent exits after /shutdown")
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if old_app is None:
            os.environ.pop("WLT_APP_DIR", None)
        else:
            os.environ["WLT_APP_DIR"] = old_app
        if old_token is None:
            os.environ.pop("WLT_AGENT_TOKEN", None)
        else:
            os.environ["WLT_AGENT_TOKEN"] = old_token
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_agent_command_dev()
    test_agent_command_frozen(None)
    test_spawn_strips_token()
    test_zip_slip_and_symlink()
    test_payload_allowlist()
    test_source_archive_excludes()
    test_help()
    test_agent_lifecycle()
    test_spawn_agent_real()
    print("all launch tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
