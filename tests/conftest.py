"""Shared fixtures: an isolated data dir, env-mode secrets, a scriptable fake
model provider, and a logged-in HTTPS test client.

The fake provider plugs into providers.ADAPTERS, so tests exercise the real
call path: fault injection, timeouts, retries, circuit breakers, metrics, spans.
"""
from __future__ import annotations

import os
import re
import sys
import textwrap

import pyotp
import pytest
from cryptography.fernet import Fernet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TEST_PROVIDERS = textwrap.dedent("""
    pipeline:
      max_debate_rounds: 3
      run_deadline_s: 600
      reserve_judge_s: 1
      reserve_merge_s: 1
      utility: {provider: util, model: u1, effort: "off"}
      utility_fallbacks: [{provider: util, model: u2, effort: "off"}]
      judge: {provider: judge, model: j1, effort: high}
      fallbacks:
        beta/b1: [{model: b2}]
    defaults:
      side_a: {provider: alpha, model: a1, effort: medium}
      side_b: {provider: beta, model: b1, effort: medium}
    providers:
      alpha: &fake
        label: Alpha
        kind: fake
        enabled: true
        timeout_s: 60
        retry: {max_attempts: 2, base_delay_s: 0, max_delay_s: 0}
        circuit: {failure_threshold: 3, cooldown_s: 60}
        models: [{id: a1, label: A1}]
      beta: {<<: *fake, label: Beta, models: [{id: b1, label: B1}, {id: b2, label: B2}]}
      util: {<<: *fake, label: Util, models: [{id: u1, label: U1}, {id: u2, label: U2}]}
      judge: {<<: *fake, label: Judge, models: [{id: j1, label: J1}]}
      off: {<<: *fake, label: Off, enabled: false, models: [{id: o1, label: O1}]}
""")

COMPARE_AGREE = '{"verdict":"agree","agreements":["x is true"],"unique_a":["a detail"],"unique_b":["b detail"],' \
                '"conflicts":[]}'
COMPARE_CONFLICT = '{"verdict":"partial","agreements":["x is true"],"unique_a":[],"unique_b":[],' \
                   '"conflicts":[{"topic":"y","a_position":"1","b_position":"2"}]}'
JUDGE_A = '{"rulings":[{"topic":"y","winner":"A","reason":"better sourced"}],"analysis":"A cites evidence."}'


def purpose_of(prompt: str) -> str:
    if "strict comparator" in prompt:
        return "compare"
    if "Two short answers to the same question" in prompt:
        return "same"
    if "reply with exactly: ?" in prompt:
        return "vote"
    if "neutral arbiter" in prompt:
        return "judge"
    if "Reconsider ONLY" in prompt:
        return "debate"
    if "Combine the material" in prompt:
        return "merge"
    return "answer"


class FakeLLM:
    """Scripted model. Rules match on provider/model/purpose; first match wins."""

    def __init__(self):
        self.rules: list[dict] = []
        self.calls: list[dict] = []
        self.prompts: list[str] = []

    def on(self, *, provider=None, model=None, purpose=None, reply=None, error=None, times=None):
        self.rules.append({"provider": provider, "model": model, "purpose": purpose, "reply": reply,
                           "error": error, "times": times})
        return self

    def __call__(self, p, model, effort, prompt, files, timeout):
        pid = p["label"].lower()
        purpose = purpose_of(prompt)
        self.prompts.append(prompt)
        self.calls.append({"provider": pid, "model": model, "purpose": purpose, "effort": effort,
                           "timeout": timeout, "files": files})
        for r in self.rules:
            if r["provider"] not in (None, pid) or r["model"] not in (None, model) \
                    or r["purpose"] not in (None, purpose):
                continue
            if r["times"] is not None:
                if r["times"] <= 0:
                    continue
                r["times"] -= 1
            if r["error"] is not None:
                raise r["error"]() if callable(r["error"]) else r["error"]
            return r["reply"](prompt) if callable(r["reply"]) else r["reply"]
        return {"compare": COMPARE_AGREE, "judge": JUDGE_A, "merge": "merged answer", "same": '{"same": false}',
                "debate": f"revised by {pid}"}.get(purpose, f"answer from {pid}/{model}")

    def count(self, **match) -> int:
        return sum(all(c.get(k) == v for k, v in match.items()) for c in self.calls)


@pytest.fixture
def env(tmp_path, monkeypatch):
    providers_file = tmp_path / "providers.yaml"
    providers_file.write_text(TEST_PROVIDERS)
    monkeypatch.setenv("CROSSFACT_CONFIG", str(tmp_path / "missing-config.yaml"))
    monkeypatch.setenv("CROSSFACT_PROVIDERS", str(providers_file))
    monkeypatch.setenv("CROSSFACT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CROSSFACT_SECRETS_MODE", "env")
    monkeypatch.setenv("CROSSFACT_METRICS_PORT", "0")
    monkeypatch.setenv("CROSSFACT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("CROSSFACT_CLI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ADMIN_BOOTSTRAP_PASSWORD", "test-password-123")
    import app.config as config
    import app.faults as faults
    import app.secretstore as secretstore
    from app.resilience import BREAKERS

    for mod_reset in (config.reload, secretstore.clear_cache, BREAKERS.reset, faults.invalidate):
        mod_reset()
    # these modules read paths through config at call time; reload makes them see the new env
    import app.config as cfgmod
    monkeypatch.setattr(cfgmod, "PROVIDERS_PATH", str(providers_file))
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(tmp_path / "missing-config.yaml"))
    config.reload()
    from app.lifecycle import LIFECYCLE

    LIFECYCLE.reset()
    yield tmp_path
    LIFECYCLE.stop()
    LIFECYCLE.reset()
    config.reload()
    secretstore.clear_cache()
    BREAKERS.reset()
    faults.invalidate()


@pytest.fixture
def llm(env, monkeypatch):
    import app.providers as providers
    from app import db

    db.init_db()
    fake = FakeLLM()
    monkeypatch.setitem(providers.ADAPTERS, "fake", fake)
    monkeypatch.setattr("time.sleep", lambda s: None)  # retries and latency faults are instant
    return fake


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, base_url="https://testserver") as c:
        yield c


def enroll(client, username="admin", password="test-password-123") -> str:
    """Sign in, finish TOTP enrollment, return the CSRF token."""
    r = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    page = client.get("/totp").text
    secret = re.search(r"<code>([A-Z2-7]+)</code>", page).group(1)
    r = client.post("/totp", data={"code": pyotp.TOTP(secret).now()}, follow_redirects=False)
    assert r.status_code == 303
    chat = client.get("/chat").text
    return re.search(r'"csrf": ?"([0-9a-f]{32})"', chat).group(1)


@pytest.fixture
def admin(client):
    return enroll(client)
