#!/usr/bin/env python3
"""Does the Claude Code CLI still accept how Crossfact calls it?

Runs the exact command the adapter builds (app.providers.claude_command, both
variants) with the adapter's environment, a fake OAuth token, and a stand-in
HTTPS proxy on localhost that records where the CLI tries to connect and then
refuses. Nothing reaches the internet.

  passes  the CLI parsed the flags, read the token, and tried to reach the API
  fails   an unknown flag; "Not logged in" (the token was ignored, which is what
          --bare does, and what Claude Code's docs say -p will default to in a
          future release); or it exited without trying the API

CI runs it on every change against the pinned CLI (image/package-lock.json) and
inside the built image with no network. A failure means: keep the previous
Claude Code version until the adapter is updated.

usage: python scripts/check_claude_cli.py [--bin PATH] [--timeout SECONDS]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.providers import BARE_MODE_HINT, claude_command, claude_env  # noqa: E402

FAKE_TOKEN = "sk-ant-oat01-" + "crossfact-cli-contract-check-" * 3  # never valid; never sent beyond localhost


class RecordingProxy:
    """Accepts proxy connections, remembers the first request line, refuses the rest."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.seen: list[str] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                try:
                    line = conn.recv(4096).split(b"\r\n", 1)[0].decode("latin-1")
                    self.seen.append(line)
                    conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass

    def close(self):
        self.sock.close()


def check(bin_path: str, has_files: bool, timeout: float) -> tuple[bool, str]:
    proxy = RecordingProxy()
    url = f"http://127.0.0.1:{proxy.port}"
    home, work = tempfile.mkdtemp(), tempfile.mkdtemp()
    env = claude_env(FAKE_TOKEN, HOME=home, HTTPS_PROXY=url, HTTP_PROXY=url, https_proxy=url, http_proxy=url,
                     NO_PROXY="", no_proxy="")
    cmd = claude_command({"bin": bin_path}, "sonnet", "medium", has_files)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env, cwd=work, start_new_session=True)
    proc.stdin.write("Reply with OK.")
    proc.stdin.close()
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if any("anthropic.com" in line for line in proxy.seen):
                return True, f"tried the API ({proxy.seen[0]})"
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        else:
            return False, f"neither tried the API nor exited within {timeout:.0f}s"
        out = proc.stdout.read()
        try:  # --output-format json: the message is in "result"
            out = str(json.loads(out).get("result") or out)
        except (ValueError, AttributeError):
            pass
        lowered = out.lower()
        if "not logged in" in lowered or "/login" in lowered:
            return False, f"ignored the token: {out.strip()[:300]}\n{BARE_MODE_HINT}"
        if "unknown option" in lowered or "error:" in lowered[:200]:
            return False, f"rejected the command line: {out.strip()[:300]}"
        return False, f"exited ({proc.returncode}) without trying the API: {out.strip()[:300]}"
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, 9)
            proc.wait(10)
        proxy.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--bin", default="claude", help="the claude binary (default: from PATH)")
    ap.add_argument("--timeout", type=float, default=60)
    args = ap.parse_args()
    version = subprocess.run([args.bin, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    print(f"Claude Code {version or '(version unknown)'}")
    ok_all = True
    for has_files in (False, True):
        ok, detail = check(args.bin, has_files, args.timeout)
        ok_all &= ok
        print(f"  {'ok  ' if ok else 'FAIL'} {'with files' if has_files else 'prompt only'}: {detail}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
