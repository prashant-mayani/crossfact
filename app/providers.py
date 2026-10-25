"""Model adapters and the single call surface:

    ask(provider_id, model, effort, prompt, files=[...], deadline=..., purpose="answer") -> dict

Every call goes through: fault injection (game days) -> per-provider timeout ->
retries with backoff and jitter -> per provider/model circuit breaker ->
metrics + an OpenTelemetry span. Adapters only translate one request and
classify failures into resilience.ErrorKind.

Kinds:
  claude_cli  Claude Code in print mode, authenticated with your Pro/Max token
              (CLAUDE_CODE_OAUTH_TOKEN). Runs with --restricted, dontAsk
              permissions, reads confined to a fresh per-run directory holding
              only that run's files, and an environment that contains its own
              token and nothing else.
  gemini_api  generativelanguage.googleapis.com generateContent; key in a header.
  gemini_cli  Gemini CLI with the one-time OAuth login (optional second pool).
  openai      any OpenAI-compatible endpoint (Groq, OpenRouter, Cerebras, ...).

Effort is one dial (off/low/medium/high/max) translated per backend: Claude's
--effort levels, Gemini 3.x thinkingLevel, Gemini 2.5 thinkingBudget, and
OpenAI-style reasoning_effort. Current Claude models can't turn thinking fully
off, so "off" means the lowest effort there.
"""
from __future__ import annotations

import base64
import contextlib
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

import httpx

from . import faults
from . import telemetry as tm
from .config import pipeline_setting, registry
from .resilience import (
    BREAKERS,
    RETRYABLE,
    STATE_VALUE,
    BreakerPolicy,
    BreakerState,
    Deadline,
    ErrorKind,
    ProviderError,
    RetryPolicy,
    call_with_resilience,
)
from .secretstore import try_secret

EFFORTS = ["off", "low", "medium", "high", "max"]
CLAUDE_EFFORT = {"off": "low", "low": "low", "medium": "medium", "high": "high", "max": "max"}
GEMINI_LEVELS = ["minimal", "low", "medium", "high"]
GEMINI_LEVEL = {"off": "minimal", "low": "low", "medium": "medium", "high": "high", "max": "high"}
GEMINI_25_BUDGET = {"off": 0, "low": 2048, "medium": 8192, "high": 16384, "max": -1}
OPENAI_EFFORT = {"off": None, "low": "low", "medium": "medium", "high": "high", "max": "high"}

TEXT_EXT = (".txt", ".md", ".csv", ".json", ".log", ".py", ".yaml", ".yml")
MAX_INLINE_TEXT = 120_000
MAX_INLINE_BINARY = 14 * 1024 * 1024  # stays under Gemini's ~20 MB request cap after base64

DEFAULT_TIMEOUT_S = {"claude_cli": 300, "gemini_cli": 300, "gemini_api": 180, "openai": 120}
# Claude Code already retries transient API errors internally, so one outer retry is plenty.
DEFAULT_RETRY = {
    "claude_cli": {"max_attempts": 2, "base_delay_s": 3.0, "max_delay_s": 30.0},
    "gemini_cli": {"max_attempts": 2, "base_delay_s": 3.0, "max_delay_s": 30.0},
    "gemini_api": {"max_attempts": 3, "base_delay_s": 1.5, "max_delay_s": 30.0},
    "openai": {"max_attempts": 3, "base_delay_s": 1.0, "max_delay_s": 20.0},
}

# Claude Code reads are fenced to the run directory twice over: the setting
# below refuses reads outside the working directory in every permission mode,
# and the deny rules name the places a secret or another user's data could be.
CLAUDE_DENY = [f"Read(//{p}/**)" for p in ("proc", "sys", "run", "var", "data", "etc", "home", "root", "app",
                                           "opt", "usr")]
CLAUDE_SETTINGS = {"permissions": {"blockReadsOutsideWorkingDirectories": True, "deny": CLAUDE_DENY}}
PASS_THROUGH_ENV = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
                    "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "TZ")


def _on_breaker_transition(name, state):
    tm.CIRCUIT_STATE.labels(name).set(STATE_VALUE[state])
    tm.CIRCUIT_TRANSITIONS.labels(name, state.value).inc()


BREAKERS.on_transition = _on_breaker_transition


# ---- registry helpers --------------------------------------------------------------

def provider_cfg(pid: str) -> dict:
    p = registry()["providers"].get(pid)
    if not p:
        raise ProviderError(f"unknown provider {pid!r}", ErrorKind.BAD_REQUEST)
    if not p.get("enabled"):
        raise ProviderError(f"provider {pid!r} is disabled in providers.yaml", ErrorKind.BAD_REQUEST)
    return p


def enabled_providers() -> dict:
    return {k: v for k, v in registry()["providers"].items() if v.get("enabled")}


def model_cfg(p: dict, model: str) -> dict:
    for m in p.get("models", []):
        if m.get("id") == model:
            return m
    return {}


def runs_dir() -> str:
    d = os.environ.get("CROSSFACT_RUNS_DIR", os.path.join(tempfile.gettempdir(), "crossfact-runs"))
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def cli_home() -> str:
    d = os.environ.get("CROSSFACT_CLI_HOME", os.path.join(tempfile.gettempdir(), "crossfact-cli-home"))
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


# ---- error classification -------------------------------------------------------------

# Order matters: the first match wins. "Per day" style limits are quota
# exhaustion (wait hours); "per minute" limits are ordinary rate limiting.
_PATTERNS = [
    (ErrorKind.QUOTA_EXHAUSTED, r"usage limit|per ?day|\brpd\b|daily (limit|quota)|insufficient_quota|"
                                r"exceeded your current quota|credit balance"),
    (ErrorKind.RATE_LIMITED, r"rate.?limit|\b429\b|too many requests|resource.?exhausted"),
    (ErrorKind.AUTH, r"authenticat|unauthori[sz]ed|\b401\b|\b403\b|invalid (api )?key|api key not valid|"
                     r"oauth|token (has )?expired|forbidden|account_on_hold|billing|permission.?denied|/login"),
    (ErrorKind.SERVER, r"overloaded|\b(500|502|503|504|529)\b|internal (server )?error|unavailable|server_error"),
    (ErrorKind.BAD_REQUEST, r"model_not_found|not.?found|invalid model|unknown model|invalid_request|"
                            r"invalid argument|unsupported"),
    (ErrorKind.NETWORK, r"econn\w+|enotfound|etimedout|network|connection (error|reset|refused)|socket|"
                        r"\bdns\b|\btls\b|\bssl\b"),
]


def classify_text(text: str) -> ErrorKind:
    t = (text or "").lower()
    for kind, pattern in _PATTERNS:
        if re.search(pattern, t):
            return kind
    return ErrorKind.UNKNOWN


def classify_status(status: int) -> ErrorKind:
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if status in (401, 403):
        return ErrorKind.AUTH
    if status in (400, 404, 409, 413, 422):
        return ErrorKind.BAD_REQUEST
    if status == 408:
        return ErrorKind.TIMEOUT
    if status >= 500:
        return ErrorKind.SERVER
    return ErrorKind.UNKNOWN


def _retry_after_header(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    try:
        return float(value) if value else None
    except ValueError:
        return None


# ---- subprocess plumbing (Claude / Gemini CLIs) ----------------------------------------

def _kill_group(proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run_cli(cmd: list[str], *, stdin_text: str | None, env: dict, cwd: str, timeout: float) -> tuple[int, str, str]:
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, cwd=cwd, start_new_session=True)
    except FileNotFoundError:
        raise ProviderError(f"{cmd[0]} is not installed in this image", ErrorKind.BAD_REQUEST) from None
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        with contextlib.suppress(Exception):  # just reaping pipes of a killed process
            proc.communicate(timeout=5)
        raise ProviderError(f"{os.path.basename(cmd[0])} timed out after {timeout:.0f}s", ErrorKind.TIMEOUT) from None
    return proc.returncode, out or "", err or ""


def _run_dir_with(files: list[str] | None) -> str:
    """A fresh directory holding copies of only this run's files."""
    d = tempfile.mkdtemp(prefix="run-", dir=runs_dir())
    for f in files or []:
        shutil.copyfile(f, os.path.join(d, os.path.basename(f)))
    return d


def _cli_env(**extra: str) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": cli_home(), "LANG": "C.UTF-8"}
    for k in PASS_THROUGH_ENV:
        if os.environ.get(k):
            env[k] = os.environ[k]
    env.update(extra)
    return env


# ---- claude_cli ----------------------------------------------------------------------------

def claude_command(p: dict, model: str, effort: str | None, has_files: bool) -> list[str]:
    cmd = [p.get("bin", "claude"), "-p", "--restricted", "--output-format", "json", "--model", model,
           "--permission-mode", "dontAsk", "--no-session-persistence",
           "--tools", "Read" if has_files else "",
           "--disallowedTools", "mcp__*",
           "--settings", json.dumps(CLAUDE_SETTINGS),
           "--max-turns", "8" if has_files else "2"]
    if effort:
        cmd += ["--effort", CLAUDE_EFFORT.get(effort, "medium")]
    return cmd


def _ask_claude_cli(p, model, effort, prompt, files, timeout):
    token = try_secret(p.get("secret", "claude_oauth_token"))
    if not token:
        raise ProviderError("Claude token is not configured (claude_oauth_token)", ErrorKind.AUTH)
    env = _cli_env(
        CLAUDE_CODE_OAUTH_TOKEN=token,
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_AUTOUPDATER="1",
        DISABLE_UPDATES="1",
        CLAUDE_CODE_DISABLE_CLAUDE_MDS="1",
        CLAUDE_CODE_DISABLE_TERMINAL_TITLE="1",
    )
    workdir = _run_dir_with(files)
    if files:
        names = ", ".join(os.path.basename(f) for f in files)
        prompt = f"Files for this request are in the current directory: {names}. Read them as needed.\n\n{prompt}"
    try:
        started = time.monotonic()
        code, out, err = run_cli(claude_command(p, model, effort, bool(files)), stdin_text=prompt, env=env,
                                 cwd=workdir, timeout=timeout)
        if code != 0 and "effort" in (out + err).lower() and effort:
            # This model doesn't take that effort level: ask once more at the model's default.
            remaining = max(5.0, timeout - (time.monotonic() - started))
            code, out, err = run_cli(claude_command(p, model, None, bool(files)), stdin_text=prompt, env=env,
                                     cwd=workdir, timeout=remaining)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    payload = None
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        pass
    if isinstance(payload, dict) and not payload.get("is_error") and code == 0:
        return str(payload.get("result") or "")
    detail = (payload.get("result") if isinstance(payload, dict) else None) or err or out or f"exit {code}"
    raise ProviderError(f"claude: {str(detail)[:400]}", classify_text(str(detail)))


# ---- gemini_api --------------------------------------------------------------------------------

def gemini_thinking(model: str, effort: str, mcfg: dict) -> dict:
    if model.startswith("gemini-2.5"):
        budget = GEMINI_25_BUDGET.get(effort, -1)
        if budget == 0 and "pro" in model:
            budget = 128  # 2.5 Pro can't fully disable thinking
        return {"thinkingBudget": budget}
    supported = [lvl for lvl in GEMINI_LEVELS if lvl in (mcfg.get("thinking_levels") or ["low", "medium", "high"])]
    want = GEMINI_LEVELS.index(GEMINI_LEVEL.get(effort, "medium"))
    best = min(supported, key=lambda lvl: (abs(GEMINI_LEVELS.index(lvl) - want), -GEMINI_LEVELS.index(lvl)))
    return {"thinkingLevel": best}


def _gemini_parts(prompt, files):
    parts = []
    for f in files or []:
        name = os.path.basename(f)
        if f.lower().endswith(TEXT_EXT):
            with open(f, encoding="utf-8", errors="replace") as fh:
                parts.append({"text": f"[file {name}]\n{fh.read()[:MAX_INLINE_TEXT]}"})
        elif os.path.getsize(f) <= MAX_INLINE_BINARY:
            mime = mimetypes.guess_type(f)[0] or "application/octet-stream"
            with open(f, "rb") as fh:
                parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(fh.read()).decode()}})
        else:
            parts.append({"text": f"[file {name} was too large to attach]"})
    parts.append({"text": prompt})
    return parts


def _gemini_error(resp: httpx.Response) -> ProviderError:
    status = resp.status_code
    kind, retry_after, message = classify_status(status), _retry_after_header(resp), resp.text[:400]
    try:
        err = resp.json().get("error", {})
        message = err.get("message", message)[:400]
        for d in err.get("details", []) or []:
            t = d.get("@type", "")
            if t.endswith("RetryInfo") and d.get("retryDelay"):
                retry_after = float(str(d["retryDelay"]).rstrip("s") or 0)
            if t.endswith("QuotaFailure"):
                for v in d.get("violations", []) or []:
                    if "perday" in str(v.get("quotaId", "")).lower():
                        kind = ErrorKind.QUOTA_EXHAUSTED
    except (ValueError, AttributeError):
        pass
    return ProviderError(f"gemini {status}: {message}", kind, retry_after=retry_after, status=status)


def _ask_gemini_api(p, model, effort, prompt, files, timeout):
    key = try_secret(p.get("secret", "gemini_api_key"))
    if not key:
        raise ProviderError("Gemini API key is not configured (gemini_api_key)", ErrorKind.AUTH)
    base = p.get("base_url", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    body = {"contents": [{"role": "user", "parts": _gemini_parts(prompt, files)}],
            "generationConfig": {"thinkingConfig": gemini_thinking(model, effort, model_cfg(p, model))}}
    url = f"{base}/models/{model}:generateContent"
    headers = {"x-goog-api-key": key}
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=timeout)
        if resp.status_code == 400 and "thinking" in resp.text.lower():
            # API shape drift: let the model use its default thinking rather than fail the call.
            body["generationConfig"].pop("thinkingConfig", None)
            resp = httpx.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.TimeoutException:
        raise ProviderError(f"gemini timed out after {timeout:.0f}s", ErrorKind.TIMEOUT) from None
    except httpx.TransportError as e:
        raise ProviderError(f"gemini network error: {type(e).__name__}", ErrorKind.NETWORK) from None
    if resp.status_code != 200:
        raise _gemini_error(resp)
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise ProviderError(f"gemini returned no answer ({reason})", ErrorKind.BAD_REQUEST)
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "\n".join(pt.get("text", "") for pt in parts if "text" in pt and not pt.get("thought"))
    if not text.strip():
        raise ProviderError(f"gemini returned an empty answer ({candidates[0].get('finishReason')})",
                            ErrorKind.SERVER)
    return text


# ---- gemini_cli ----------------------------------------------------------------------------------

def _ask_gemini_cli(p, model, effort, prompt, files, timeout):
    home = p.get("home") or os.path.join(os.environ.get("CROSSFACT_DATA_DIR", "/data"), "gemini-home")
    workdir = _run_dir_with(files)
    if files:
        prompt = " ".join(f"@{os.path.basename(f)}" for f in files) + "\n" + prompt
    try:
        code, out, err = run_cli([p.get("bin", "gemini"), "-m", model, "-p", prompt], stdin_text=None,
                                 env=_cli_env(HOME=home), cwd=workdir, timeout=timeout)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if code != 0:
        detail = (err or out)[:400]
        raise ProviderError(f"gemini-cli: {detail}", classify_text(detail))
    return out


# ---- openai-compatible ------------------------------------------------------------------------------

def _ask_openai(p, model, effort, prompt, files, timeout):
    key = try_secret(p["secret"]) if p.get("secret") else ""
    if p.get("secret") and not key:
        raise ProviderError(f"{p.get('label', 'provider')} key is not configured ({p['secret']})", ErrorKind.AUTH)
    for f in files or []:
        if f.lower().endswith(TEXT_EXT):
            with open(f, encoding="utf-8", errors="replace") as fh:
                prompt = f"[file {os.path.basename(f)}]\n{fh.read()[:MAX_INLINE_TEXT]}\n\n" + prompt
    body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    level = OPENAI_EFFORT.get(effort)
    if level and p.get("supports_reasoning_effort"):
        body["reasoning_effort"] = level
    try:
        resp = httpx.post(f"{p['base_url'].rstrip('/')}/chat/completions",
                          headers={"Authorization": f"Bearer {key}"} if key else {}, json=body, timeout=timeout)
    except httpx.TimeoutException:
        raise ProviderError(f"{p.get('label')} timed out after {timeout:.0f}s", ErrorKind.TIMEOUT) from None
    except httpx.TransportError as e:
        raise ProviderError(f"{p.get('label')} network error: {type(e).__name__}", ErrorKind.NETWORK) from None
    if resp.status_code != 200:
        kind = classify_status(resp.status_code)
        if kind is ErrorKind.RATE_LIMITED and classify_text(resp.text) is ErrorKind.QUOTA_EXHAUSTED:
            kind = ErrorKind.QUOTA_EXHAUSTED
        raise ProviderError(f"{p.get('label')} {resp.status_code}: {resp.text[:300]}", kind,
                            retry_after=_retry_after_header(resp), status=resp.status_code)
    try:
        return resp.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, ValueError):
        raise ProviderError(f"{p.get('label')}: unexpected response shape", ErrorKind.SERVER) from None


ADAPTERS = {"claude_cli": _ask_claude_cli, "gemini_api": _ask_gemini_api, "gemini_cli": _ask_gemini_cli,
            "openai": _ask_openai}


# ---- the call surface ------------------------------------------------------------------------------------

def breaker_for(pid: str, model: str, p: dict | None = None):
    p = p if p is not None else registry()["providers"].get(pid, {})
    return BREAKERS.get(f"{pid}/{model}", BreakerPolicy(**(p.get("circuit") or {})))


def ask(pid: str, model: str, effort: str, prompt: str, files: list[str] | None = None, *,
        deadline: Deadline | None = None, purpose: str = "answer") -> dict:
    p = provider_cfg(pid)
    kind = p["kind"]
    if kind not in ADAPTERS:
        raise ProviderError(f"unknown provider kind {kind!r}", ErrorKind.BAD_REQUEST)
    timeout_s = float(p.get("timeout_s", DEFAULT_TIMEOUT_S.get(kind, 120)))
    policy = RetryPolicy(**{**DEFAULT_RETRY.get(kind, {}), **(p.get("retry") or {})})
    deadline = deadline or Deadline(float(pipeline_setting("run_deadline_s", 600)))
    breaker = breaker_for(pid, model, p)
    started = time.monotonic()
    labels = {"provider": pid, "model": model, "purpose": purpose}

    with tm.span("provider.call", **{"crossfact.provider": pid, "crossfact.model": model,
                                     "crossfact.effort": effort, "crossfact.purpose": purpose}) as sp:
        def attempt(call_timeout: float) -> str:
            t0 = time.monotonic()
            faults.apply(pid, model, call_timeout)
            # Injected latency is part of the call: it spends the same timeout a slow
            # provider would (game day "gemini-slow" overran the run budget without this).
            left = call_timeout - (time.monotonic() - t0)
            if left <= 0:
                raise ProviderError(f"injected latency used the whole {call_timeout:.0f}s timeout",
                                    ErrorKind.TIMEOUT, injected=True)
            try:
                return ADAPTERS[kind](p, model, effort, prompt, files, left)
            except ProviderError:
                raise
            except Exception as e:  # library surprise: classify instead of crashing the run
                raise ProviderError(f"{type(e).__name__}: {e}", ErrorKind.UNKNOWN) from e

        def on_retry(n: int, err: ProviderError, delay: float) -> None:
            tm.PROVIDER_RETRIES.labels(pid, err.kind.value).inc()
            sp.add_event("retry", {"attempt": n, "kind": err.kind.value, "delay_s": round(delay, 2)})

        try:
            text, attempts = call_with_resilience(attempt, timeout_s=timeout_s, deadline=deadline, policy=policy,
                                                  breaker=breaker, min_call_s=float(p.get("min_call_s", 5)),
                                                  on_retry=on_retry)
        except ProviderError as e:
            tm.PROVIDER_CALLS.labels(result=e.kind.value, **labels).inc()
            tm.observe(tm.PROVIDER_DURATION, time.monotonic() - started, **labels)
            sp.set_attribute("crossfact.result", e.kind.value)
            sp.set_attribute("crossfact.attempts", e.attempts)
            tm.mark_error(sp, e.short())
            raise
        duration = time.monotonic() - started
        tm.PROVIDER_CALLS.labels(result="ok", **labels).inc()
        tm.observe(tm.PROVIDER_DURATION, duration, **labels)
        sp.set_attribute("crossfact.result", "ok")
        sp.set_attribute("crossfact.attempts", attempts)
    return {"text": (text or "").strip(), "provider": pid, "model": model, "effort": effort,
            "attempts": attempts, "duration_s": round(duration, 2)}


def prime_metrics() -> None:
    """Every enabled model's call, retry, straggler and breaker series at zero (see
    orchestrator.prime_metrics for why). Bounded by the registry: models x purposes x
    outcomes, a few hundred series."""
    reg = registry()
    pipe = reg["pipeline"]
    enabled = enabled_providers()
    combos = {(pid, m["id"], purpose) for pid, p in enabled.items() for m in p.get("models", [])
              for purpose in ("answer", "debate")}
    for key, purposes in (("utility", ("compare", "merge")), ("utility_fallbacks", ("compare", "merge")),
                          ("judge", ("judge",)), ("judge_fallbacks", ("judge",))):
        entries = pipe.get(key) or []
        for e in [entries] if isinstance(entries, dict) else entries:
            if e.get("provider") in enabled:
                combos |= {(e["provider"], e["model"], purpose) for purpose in purposes}
    results = ["ok", *(k.value for k in ErrorKind)]
    for pid, model, purpose in combos:
        for result in results:
            tm.PROVIDER_CALLS.labels(pid, model, purpose, result)
        tm.PROVIDER_DURATION.labels(pid, model, purpose)
        if purpose in ("answer", "debate"):
            tm.STRAGGLERS.labels(pid, model, purpose)
    for pid in enabled:
        for kind in RETRYABLE:
            tm.PROVIDER_RETRIES.labels(pid, kind.value)
    for pid, model in {(c[0], c[1]) for c in combos}:
        name = f"{pid}/{model}"
        for state in BreakerState:
            tm.CIRCUIT_TRANSITIONS.labels(name, state.value)
        if name not in BREAKERS.all():
            tm.CIRCUIT_STATE.labels(name).set(0)


def health(pid: str) -> dict:
    """What the UI shows next to a provider: configured? which breakers are open?"""
    p = registry()["providers"].get(pid, {})
    if p.get("kind") == "gemini_cli":
        home = p.get("home") or os.path.join(os.environ.get("CROSSFACT_DATA_DIR", "/data"), "gemini-home")
        configured = os.path.exists(os.path.join(home, ".gemini"))
    else:
        configured = not p.get("secret") or try_secret(p["secret"]) is not None
    circuits = {}
    for m in p.get("models", []):
        b = BREAKERS.all().get(f"{pid}/{m['id']}")
        if b is not None:
            circuits[m["id"]] = b.snapshot()
    return {"configured": configured, "circuits": circuits}
