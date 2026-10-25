"""Graceful drain against a real server process and a real SIGTERM.

The fake `claude` takes three seconds to answer. SIGTERM arrives mid-run; the
server must refuse new runs, keep serving polls, finish the run, then exit.
"""
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLOW_CLAUDE = """#!/usr/bin/env python3
import json, sys, time
sys.stdin.read()
time.sleep(3)
print(json.dumps({"type": "result", "is_error": False, "result": "slow answer"}))
"""
PROVIDERS = """
pipeline: {run_deadline_s: 120}
defaults: {side_a: {provider: claude, model: sonnet, effort: low}}
providers:
  claude: {label: Claude, kind: claude_cli, secret: claude_oauth_token, enabled: true, timeout_s: 60,
           models: [{id: sonnet, label: Sonnet}]}
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_drains_inflight_runs(env, tmp_path):
    from app import auth, db

    (tmp_path / "bin").mkdir()
    fake = tmp_path / "bin" / "claude"
    fake.write_text(SLOW_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    providers = tmp_path / "drain-providers.yaml"
    providers.write_text(PROVIDERS)

    db.init_db()
    uid = db.create_user("admin", auth.hash_pw("pw-1234567890"), role="admin")
    cookie, session = auth.issue_cookie(uid)
    db.mark_totp_ok(session["sid"])

    port = free_port()
    child_env = {**os.environ, "CROSSFACT_PROVIDERS": str(providers), "CROSSFACT_PORT": str(port),
                 "CLAUDE_OAUTH_TOKEN": "sk-ant-oat-test", "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}
    proc = subprocess.Popen([sys.executable, "-m", "app.server"], cwd=ROOT, env=child_env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    headers = {"Cookie": f"crossfact_session={cookie}"}
    form = {"question": "slow?", "mode": "single", "a_provider": "claude", "a_model": "sonnet",
            "a_effort": "low", "csrf": session["csrf"]}
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        r = httpx.post(f"{base}/ask", data=form, headers=headers, timeout=5)
        assert r.status_code == 200, r.text
        mid = r.json()["message_id"]
        time.sleep(0.5)

        proc.send_signal(signal.SIGTERM)
        sent = time.monotonic()
        time.sleep(0.3)
        refused = httpx.post(f"{base}/ask", data=form, headers=headers, timeout=5)
        assert refused.status_code == 503 and "restarting" in refused.json()["detail"]
        assert httpx.get(f"{base}/readyz", timeout=5).status_code == 503
        poll = httpx.get(f"{base}/api/messages/{mid}", headers=headers, timeout=5)
        assert poll.status_code == 200 and poll.json()["status"] == "running"

        proc.wait(timeout=30)
        waited = time.monotonic() - sent
    finally:
        if proc.poll() is None:
            proc.kill()
        output = proc.stdout.read() if proc.stdout else ""

    m = db.get_message(mid)
    assert m["status"] == "done" and m["content"] == "slow answer", output[-2000:]
    assert json.loads(m["pipeline_json"])["outcome"] == "single"
    assert waited >= 1.5, "server exited before the in-flight run finished"
    assert "draining" in output
