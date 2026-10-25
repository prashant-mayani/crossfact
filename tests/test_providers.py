"""Adapters: Claude CLI isolation, Gemini thinking/error mapping, classification."""
import json
import os
import stat
import time

import httpx
import pytest

from app import providers
from app.resilience import ErrorKind, ProviderError

FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, os, sys, time
prompt = sys.stdin.read()
mode = os.environ.get("FAKE_MODE", "")
if "SLEEP" in prompt:
    time.sleep(30)
if "FAIL_USAGE" in prompt:
    print(json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1760000000"}))
    sys.exit(1)
if "FAIL_AUTH" in prompt:
    print(json.dumps({"type": "result", "is_error": True, "result": "OAuth token has expired. Please run /login"}))
    sys.exit(1)
if "FAIL_EFFORT" in prompt and "--effort" in sys.argv:
    print("error: effort level max is not supported for this model", file=sys.stderr)
    sys.exit(2)
report = {"argv": sys.argv[1:], "cwd": os.getcwd(), "cwd_files": sorted(os.listdir(".")),
          "env_keys": sorted(os.environ), "prompt": prompt}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(report)}))
"""


@pytest.fixture
def fake_claude(env, tmp_path, monkeypatch):
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(FAKE_CLAUDE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLAUDE_OAUTH_TOKEN", "sk-ant-oat-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret-must-not-leak")
    return {"bin": str(path), "kind": "claude_cli", "secret": "claude_oauth_token"}


def call_claude(p, prompt="hello", files=None, effort="medium", timeout=20):
    return providers._ask_claude_cli(p, "sonnet", effort, prompt, files, timeout)


def test_claude_runs_restricted_with_reads_fenced(fake_claude):
    report = json.loads(call_claude(fake_claude))
    argv = report["argv"]
    for flag in ("--restricted", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--tools") + 1] == ""            # no files: no tools at all
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["permissions"]["blockReadsOutsideWorkingDirectories"] is True
    assert "Read(//proc/**)" in settings["permissions"]["deny"]
    assert argv[argv.index("--effort") + 1] == "medium"


def test_claude_environment_holds_only_its_own_token(fake_claude):
    keys = json.loads(call_claude(fake_claude))["env_keys"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" in keys
    for leaked in ("GEMINI_API_KEY", "SESSION_SECRET", "FIELD_ENCRYPTION_KEY", "ADMIN_BOOTSTRAP_PASSWORD",
                   "CLAUDE_OAUTH_TOKEN"):
        assert leaked not in keys
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" in keys


def test_claude_sees_only_this_runs_files_and_cleans_up(fake_claude, tmp_path):
    mine = tmp_path / "uploads" / "u1" / "notes.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("mine")
    (tmp_path / "uploads" / "u1" / "other.md").write_text("not attached")
    report = json.loads(call_claude(fake_claude, files=[str(mine)]))
    argv = report["argv"]
    assert argv[argv.index("--tools") + 1] == "Read"
    assert report["cwd_files"] == ["notes.md"]
    assert "notes.md" in report["prompt"]
    assert not os.path.exists(report["cwd"])                 # per-run dir removed afterwards


@pytest.mark.parametrize("marker,kind", [("FAIL_USAGE", ErrorKind.QUOTA_EXHAUSTED), ("FAIL_AUTH", ErrorKind.AUTH)])
def test_claude_failures_are_classified(fake_claude, marker, kind):
    with pytest.raises(ProviderError) as e:
        call_claude(fake_claude, prompt=marker)
    assert e.value.kind is kind


def test_claude_retries_once_without_an_unsupported_effort(fake_claude):
    report = json.loads(call_claude(fake_claude, prompt="FAIL_EFFORT", effort="max"))
    assert "--effort" not in report["argv"]


def test_claude_timeout_kills_the_process_group(fake_claude):
    started = time.monotonic()
    with pytest.raises(ProviderError) as e:
        call_claude(fake_claude, prompt="SLEEP", timeout=1)
    assert e.value.kind is ErrorKind.TIMEOUT
    assert time.monotonic() - started < 10


def test_missing_token_is_an_auth_error(env):
    with pytest.raises(ProviderError) as e:
        providers._ask_claude_cli({"bin": "claude", "secret": "claude_oauth_token"}, "sonnet", "low", "q", None, 5)
    assert e.value.kind is ErrorKind.AUTH


@pytest.mark.parametrize("model,effort,levels,expected", [
    ("gemini-2.5-flash", "off", None, {"thinkingBudget": 0}),
    ("gemini-2.5-pro", "off", None, {"thinkingBudget": 128}),
    ("gemini-2.5-pro", "max", None, {"thinkingBudget": -1}),
    ("gemini-3.8-flash", "off", ["low", "medium", "high"], {"thinkingLevel": "low"}),
    ("gemini-3.8-flash", "max", ["low", "medium", "high"], {"thinkingLevel": "high"}),
    ("gemini-3.5-flash-lite", "off", ["minimal", "low", "medium", "high"], {"thinkingLevel": "minimal"}),
    ("gemini-3.1-pro-preview", "medium", ["low", "high"], {"thinkingLevel": "high"}),
])
def test_gemini_effort_maps_to_supported_thinking(model, effort, levels, expected):
    assert providers.gemini_thinking(model, effort, {"thinking_levels": levels} if levels else {}) == expected


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, headers=None, json=None, timeout=None):  # noqa: A002 - mirrors httpx.post
        self.requests.append({"url": url, "headers": headers, "json": json})
        status, body = self.responses.pop(0)
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


GEMINI = {"kind": "gemini_api", "secret": "gemini_api_key", "models": [{"id": "gemini-3.8-flash",
                                                                        "thinking_levels": ["low", "medium", "high"]}]}


def ok_body(text="hi"):
    return {"candidates": [{"content": {"parts": [{"text": "pondering", "thought": True}, {"text": text}]}}]}


def test_gemini_key_goes_in_a_header_and_thoughts_are_dropped(env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k-123")
    fake = FakeHTTP((200, ok_body("answer")))
    monkeypatch.setattr(providers.httpx, "post", fake)
    assert providers._ask_gemini_api(GEMINI, "gemini-3.8-flash", "medium", "q", None, 30) == "answer"
    req = fake.requests[0]
    assert req["headers"] == {"x-goog-api-key": "k-123"} and "key=" not in req["url"]
    assert req["json"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "medium"}


def test_gemini_drops_thinking_config_if_the_api_rejects_it(env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    fake = FakeHTTP((400, {"error": {"message": "Unknown name thinkingLevel"}}), (200, ok_body()))
    monkeypatch.setattr(providers.httpx, "post", fake)
    assert providers._ask_gemini_api(GEMINI, "gemini-3.8-flash", "low", "q", None, 30) == "hi"
    assert "thinkingConfig" not in fake.requests[1]["json"]["generationConfig"]


def test_gemini_429_with_retry_hint_vs_daily_quota(env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    per_minute = {"error": {"message": "slow down", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}]}}
    per_day = {"error": {"message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
    monkeypatch.setattr(providers.httpx, "post", FakeHTTP((429, per_minute), (429, per_day)))
    with pytest.raises(ProviderError) as e1:
        providers._ask_gemini_api(GEMINI, "gemini-3.8-flash", "low", "q", None, 30)
    assert e1.value.kind is ErrorKind.RATE_LIMITED and e1.value.retry_after == 17
    with pytest.raises(ProviderError) as e2:
        providers._ask_gemini_api(GEMINI, "gemini-3.8-flash", "low", "q", None, 30)
    assert e2.value.kind is ErrorKind.QUOTA_EXHAUSTED


@pytest.mark.parametrize("text,kind", [
    ("Rate limit reached for model on tokens per minute (TPM)", ErrorKind.RATE_LIMITED),
    ("Rate limit reached ... requests per day (RPD)", ErrorKind.QUOTA_EXHAUSTED),
    ("API Error: 529 overloaded_error", ErrorKind.SERVER),
    ("Invalid API key · Please run /login", ErrorKind.AUTH),
    ("model_not_found: claude-x", ErrorKind.BAD_REQUEST),
    ("API Error: Connection error.", ErrorKind.NETWORK),
    ("something odd", ErrorKind.UNKNOWN),
])
def test_text_classification(text, kind):
    assert providers.classify_text(text) is kind



def test_injected_latency_spends_the_calls_own_timeout(llm, monkeypatch):
    """Game day "gemini-slow": latency added on top of the timeout let runs overrun their budget."""
    import time

    from app import db, faults, providers
    from app.resilience import Deadline

    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    db.add_fault("alpha", "latency", 1.0, 30.0, time.time() + 600, "test")
    faults.invalidate()
    seen = []
    real_adapter = providers.ADAPTERS["fake"]
    monkeypatch.setitem(providers.ADAPTERS, "fake",
                        lambda p, model, effort, prompt, files, timeout: (seen.append(timeout),
                                                                          real_adapter(p, model, effort, prompt,
                                                                                       files, timeout))[1])
    providers.ask("alpha", "a1", "low", "hi", deadline=Deadline(600))
    assert seen == [pytest.approx(60 - 30)]  # alpha's timeout_s is 60; the fault spent 30 of it


def test_injected_latency_longer_than_the_timeout_is_a_timeout(llm, monkeypatch):
    import time

    from app import db, faults, providers
    from app.resilience import Deadline, ErrorKind, ProviderError

    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    db.add_fault("alpha", "latency", 1.0, 90.0, time.time() + 600, "test")
    faults.invalidate()
    with pytest.raises(ProviderError) as e:
        providers.ask("alpha", "a1", "low", "hi", deadline=Deadline(600))
    assert e.value.kind is ErrorKind.TIMEOUT and llm.count(provider="alpha") == 0
