"""Model calls for the harness: through the app's own call path (timeouts,
retries, breakers), cached, paced, and budgeted against each free tier.

The cache makes a run resumable and repeatable: the same provider, model,
effort and prompt never costs a second call, so a baseline and Crossfact that ask
a model the same question share one answer, and re-running a report costs
nothing. Only successful calls are cached. A run that must measure today's
models (the nightly) passes fresh_after: it reuses only calls made since it
started, so within the run a shared answer is still asked once.

Budgets: free tiers cap requests per day per model. A budget here is our own,
lower cap per (provider, model) per UTC day; a provider saying its daily quota
is gone counts as the cap being reached. Either way the run stops with
BudgetExhausted and picks up where it left off on the next run.

Per-minute limits are pacing, not failures: an optional requests-per-minute
pace per model spaces calls out, and a call refused for rate (after the app's
own retries) or by an open breaker waits and tries again. Time spent waiting
is not counted as latency.
"""
from __future__ import annotations

import hashlib
import json
import time

from app.resilience import Deadline, ErrorKind, ProviderError

from .store import Store

WAIT_KINDS = {ErrorKind.RATE_LIMITED, ErrorKind.CIRCUIT_OPEN}
MAX_WAITS = 6          # per call, before the error is the answer's error
MAX_WAIT_S = 120.0     # longest single wait


class BudgetExhausted(BaseException):  # noqa: N818 - BaseException on purpose: see below
    """A model's daily allowance is used up: stop now, resume on a later day.

    Derived from BaseException so the app's pipeline, which turns any Exception
    into a failed run, lets it through: running out of free quota is not an
    answer, and must not be scored as one."""


def utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def call_key(provider: str, model: str, effort: str, prompt: str, sample: int = 0) -> str:
    """Sample n > 0 is a separate draw of the same prompt (self-consistency); 0 is the shared one."""
    return hashlib.sha256(json.dumps([provider, model, effort, prompt] + ([sample] if sample else [])).encode()
                          ).hexdigest()


class Calls:
    def __init__(self, store: Store, budgets: dict[str, int] | None = None, default_budget: int | None = None,
                 timeout_s: float = 300, fresh_after: float | None = None, pace: dict[str, float] | None = None):
        self.store = store
        self.budgets = budgets or {}
        self.default_budget = default_budget
        self.timeout_s = timeout_s
        self.fresh_after = fresh_after  # None: any cached call; a time: only calls made since then
        self.pace = pace or {}          # "provider/model" (or provider) -> requests per minute
        self._last: dict[tuple[str, str], float] = {}
        self.real = 0
        self.hits = 0
        self.waited_s = 0.0

    def _limit(self, table: dict, provider: str, model: str, default=None):
        return table.get(f"{provider}/{model}", table.get(provider, default))

    def budget_for(self, provider: str, model: str) -> int | None:
        return self._limit(self.budgets, provider, model, self.default_budget)

    def _wait(self, seconds: float) -> None:
        seconds = max(0.0, min(MAX_WAIT_S, seconds))
        if seconds:
            time.sleep(seconds)
            self.waited_s += seconds

    def _paced(self, provider: str, model: str) -> None:
        rpm = self._limit(self.pace, provider, model)
        if rpm:
            last = self._last.get((provider, model))
            if last is not None:
                self._wait(60.0 / float(rpm) - (time.monotonic() - last))
        self._last[(provider, model)] = time.monotonic()

    def forget(self, provider: str, model: str, effort: str, prompt: str) -> None:
        """Drop a cached reply that turned out unusable, so the next run asks again."""
        self.store.forget(call_key(provider, model, effort, prompt))

    def ask(self, provider: str, model: str, effort: str, prompt: str, files=None, *, deadline=None,
            purpose: str = "answer", sample: int = 0) -> dict:
        """Same signature and result as app.providers.ask, so the app's pipeline can use it."""
        from app import providers  # imported late: the harness sets up the app's config first

        key = call_key(provider, model, effort, prompt, sample)
        hit = self.store.cached(key, since=self.fresh_after)
        if hit:
            self.hits += 1
            return {"text": hit["response"], "provider": provider, "model": model, "effort": effort,
                    "attempts": 0, "duration_s": hit["duration"], "cached": True}
        for waits in range(MAX_WAITS + 1):
            day = utc_day()
            used, exhausted = self.store.used(day, provider, model)
            budget = self.budget_for(provider, model)
            if exhausted or (budget is not None and used >= budget):
                raise BudgetExhausted(f"{provider}/{model}: {used} calls today"
                                      + (" and the provider says its daily quota is gone" if exhausted
                                         else f", our budget is {budget}"))
            self._paced(provider, model)
            try:
                r = providers.ask(provider, model, effort, prompt, files,
                                  deadline=deadline or Deadline(self.timeout_s), purpose=purpose)
                break
            except ProviderError as e:
                if e.kind is ErrorKind.QUOTA_EXHAUSTED:
                    self.store.count_call(day, provider, model, exhausted=True)
                    raise BudgetExhausted(f"{provider}/{model}: daily quota exhausted ({e.short()})") from e
                self.store.count_call(day, provider, model)
                if e.kind not in WAIT_KINDS or waits == MAX_WAITS or (deadline and deadline.remaining() < 60):
                    raise
                self._wait(e.retry_after or 30.0)  # an open breaker in its probe says 0: wait anyway
        self.store.count_call(day, provider, model)
        self.store.remember(key, provider, model, effort, purpose, r["text"], r.get("duration_s", 0.0))
        self.real += 1
        return {**r, "cached": False}
