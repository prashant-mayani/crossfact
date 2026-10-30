"""Failure handling for model calls: deadlines, retries, circuit breakers.

Everything here is deliberately free of I/O, metrics and globals other than the
breaker registry, so the behaviour can be unit-tested with a fake clock.

Error taxonomy (ProviderError.kind) and what each one does:

  kind             retry?  trips breaker?  typical cause
  timeout          yes     yes             model too slow for the per-call timeout
  rate_limited     yes*    yes**           per-minute 429; *only if the hint fits the budget;
                                           **not when it carries a retry hint we honour
  quota_exhausted  no      yes             daily free-tier quota gone; retrying is pointless
  server           yes*    yes             5xx; *not 503/529 "overloaded": go to the substitute
  network          yes     yes             DNS, TLS, connection reset
  auth             no      yes             bad/expired key or token; fail fast, stop hammering
  bad_request      no      no              our request was wrong (unknown model, blocked prompt)
  circuit_open     no      no              the breaker refused the call; nothing was sent
  deadline         no      no              not enough of the run's time budget left to try
  unknown          yes     yes             anything unclassified
"""
from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class ErrorKind(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    SERVER = "server"
    NETWORK = "network"
    AUTH = "auth"
    BAD_REQUEST = "bad_request"
    CIRCUIT_OPEN = "circuit_open"
    DEADLINE = "deadline"
    UNKNOWN = "unknown"


RETRYABLE = {ErrorKind.TIMEOUT, ErrorKind.RATE_LIMITED, ErrorKind.SERVER, ErrorKind.NETWORK, ErrorKind.UNKNOWN}
TRIPS_BREAKER = {ErrorKind.TIMEOUT, ErrorKind.RATE_LIMITED, ErrorKind.QUOTA_EXHAUSTED, ErrorKind.SERVER,
                 ErrorKind.NETWORK, ErrorKind.AUTH, ErrorKind.UNKNOWN}
# A model that answers "overloaded" (503; Anthropic's 529) is shedding load for
# minutes, and Gemini takes about a minute to say so. Retrying it spent a side's
# whole straggler grace before its substitute got a turn (October 2026: Gemini
# 3.8 Flash at "high demand" while 3.6 Flash answered in 6 s), so these go
# straight to the next model in the chain. The breaker still counts them.
OVERLOADED_STATUSES = {503, 529}


class ProviderError(RuntimeError):
    def __init__(self, message: str, kind: ErrorKind = ErrorKind.UNKNOWN, *,
                 retry_after: float | None = None, status: int | None = None, injected: bool = False):
        super().__init__(message)
        self.kind = ErrorKind(kind)
        self.retry_after = retry_after
        self.status = status
        self.injected = injected
        self.attempts = 0

    def short(self) -> str:
        return f"{self.kind.value}: {str(self)[:200]}"


def _monotonic() -> float:
    """time.monotonic looked up at call time, so a test or the game-day simulator
    can swap the clock (a default of time.monotonic itself would be frozen at import)."""
    return time.monotonic()


class Deadline:
    """A shared time budget for one consensus run."""

    def __init__(self, seconds: float, clock: Callable[[], float] | None = None):
        clock = clock or _monotonic
        self._clock = clock
        self.total = float(seconds)
        self.started = clock()

    def elapsed(self) -> float:
        return self._clock() - self.started

    def remaining(self) -> float:
        return max(0.0, self.total - self.elapsed())

    def expired(self) -> bool:
        return self.remaining() <= 0

    def cap(self, timeout: float) -> float:
        return min(float(timeout), self.remaining())


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 20.0

    def backoff(self, attempt: int, rng: Callable[[], float] = random.random) -> float:
        """Full jitter: uniform(0, min(cap, base * 2^(attempt-1)))."""
        ceiling = min(self.max_delay_s, self.base_delay_s * (2 ** max(0, attempt - 1)))
        return rng() * ceiling

    def delay_for(self, err: ProviderError, attempt: int, rng: Callable[[], float] = random.random) -> float | None:
        """Seconds to wait before the next attempt, or None if retrying is pointless."""
        if err.kind not in RETRYABLE:
            return None
        if err.kind is ErrorKind.SERVER and err.status in OVERLOADED_STATUSES:
            return None
        if err.kind is ErrorKind.RATE_LIMITED and err.retry_after is not None:
            if err.retry_after > self.max_delay_s:
                return None  # the provider says "much later": treat like quota exhaustion
            return err.retry_after + rng() * 0.5  # honour the hint, plus a little jitter
        return self.backoff(attempt, rng)


@dataclass
class BreakerPolicy:
    failure_threshold: int = 3   # consecutive failures before opening
    cooldown_s: float = 60.0     # first open period
    max_cooldown_s: float = 600.0  # doubles on each failed trial, up to this


class BreakerState(StrEnum):
    CLOSED = "closed"
    HALF_OPEN = "half_open"
    OPEN = "open"


STATE_VALUE = {BreakerState.CLOSED: 0, BreakerState.HALF_OPEN: 1, BreakerState.OPEN: 2}


@dataclass
class CircuitBreaker:
    name: str
    policy: BreakerPolicy = field(default_factory=BreakerPolicy)
    clock: Callable[[], float] = _monotonic
    on_transition: Callable[[str, BreakerState], None] | None = None

    def __post_init__(self):
        self._lock = threading.Lock()
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._cooldown = self.policy.cooldown_s
        self._opened_at = 0.0
        self._trial_in_flight = False

    # -- transitions -------------------------------------------------------
    def _set(self, state: BreakerState) -> None:
        if state is not self._state:
            self._state = state
            if self.on_transition:
                self.on_transition(self.name, state)

    def allow(self) -> bool:
        with self._lock:
            if self._state is BreakerState.CLOSED:
                return True
            if self._state is BreakerState.OPEN:
                if self.clock() - self._opened_at >= self._cooldown:
                    self._set(BreakerState.HALF_OPEN)
                    self._trial_in_flight = True
                    return True  # exactly one trial call
                return False
            # HALF_OPEN: one trial at a time
            if self._trial_in_flight:
                return False
            self._trial_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._trial_in_flight = False
            self._cooldown = self.policy.cooldown_s
            self._set(BreakerState.CLOSED)

    def record_failure(self) -> None:
        with self._lock:
            self._trial_in_flight = False
            if self._state is BreakerState.HALF_OPEN:
                self._cooldown = min(self._cooldown * 2, self.policy.max_cooldown_s)
                self._opened_at = self.clock()
                self._set(BreakerState.OPEN)
                return
            self._failures += 1
            if self._failures >= self.policy.failure_threshold:
                self._opened_at = self.clock()
                self._set(BreakerState.OPEN)

    def release(self) -> None:
        """A trial call ended without a verdict (e.g. bad_request): free the slot."""
        with self._lock:
            self._trial_in_flight = False

    # -- introspection --------------------------------------------------------
    @property
    def state(self) -> BreakerState:
        with self._lock:
            if self._state is BreakerState.OPEN and self.clock() - self._opened_at >= self._cooldown:
                return BreakerState.HALF_OPEN  # would admit a trial now
            return self._state

    def retry_in(self) -> float:
        with self._lock:
            if self._state is not BreakerState.OPEN:
                return 0.0
            return max(0.0, self._cooldown - (self.clock() - self._opened_at))

    def snapshot(self) -> dict:
        return {"name": self.name, "state": self.state.value, "failures": self._failures,
                "retry_in_s": round(self.retry_in(), 1)}


class BreakerRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._breakers: dict[str, CircuitBreaker] = {}
        self.on_transition: Callable[[str, BreakerState], None] | None = None

    def get(self, name: str, policy: BreakerPolicy | None = None) -> CircuitBreaker:
        with self._lock:
            b = self._breakers.get(name)
            if b is None:
                b = CircuitBreaker(name, policy or BreakerPolicy(), on_transition=self.on_transition)
                self._breakers[name] = b
            return b

    def all(self) -> dict[str, CircuitBreaker]:
        with self._lock:
            return dict(self._breakers)

    def reset(self) -> None:
        with self._lock:
            self._breakers.clear()


BREAKERS = BreakerRegistry()


def trips_breaker(err: ProviderError, policy: RetryPolicy) -> bool:
    """A 429 that says "retry in N s" (N within our backoff cap) is the provider
    pacing us, not failing: we wait and retry, and the breaker stays out of it.
    Counting those (game day "gemini-429") opened breakers 12 times in 10 runs and
    turned recoverable throttling into degraded answers."""
    if err.kind is ErrorKind.RATE_LIMITED and err.retry_after is not None \
            and err.retry_after <= policy.max_delay_s:
        return False
    return err.kind in TRIPS_BREAKER


def call_with_resilience(
    fn: Callable[[float], str],
    *,
    timeout_s: float,
    deadline: Deadline,
    policy: RetryPolicy,
    breaker: CircuitBreaker,
    min_call_s: float = 5.0,
    sleep: Callable[[float], None] | None = None,
    on_retry: Callable[[int, ProviderError, float], None] | None = None,
) -> tuple[str, int]:
    """Run fn(timeout) with retries, backoff and a circuit breaker.

    Returns (result, attempts). Raises ProviderError with .attempts set.
    """
    sleep = sleep or time.sleep  # resolved per call so tests can patch time.sleep
    attempt = 0
    while True:
        attempt += 1
        if not breaker.allow():
            err = ProviderError(f"circuit open for {breaker.name}; retry in {breaker.retry_in():.0f}s",
                                ErrorKind.CIRCUIT_OPEN, retry_after=breaker.retry_in())
            err.attempts = attempt - 1
            raise err
        call_timeout = deadline.cap(timeout_s)
        if call_timeout < min_call_s:
            breaker.release()
            err = ProviderError(f"only {deadline.remaining():.0f}s left in the run budget", ErrorKind.DEADLINE)
            err.attempts = attempt - 1
            raise err
        try:
            result = fn(call_timeout)
        except ProviderError as e:
            e.attempts = attempt
            if trips_breaker(e, policy):
                breaker.record_failure()
            else:
                breaker.release()
            delay = policy.delay_for(e, attempt) if attempt < policy.max_attempts else None
            if delay is None or delay + min_call_s > deadline.remaining():
                raise
            if e.kind is ErrorKind.TIMEOUT and deadline.remaining() - delay < call_timeout * 0.5:
                raise  # it just needed more than call_timeout; a much shorter retry can't succeed
            if on_retry:
                on_retry(attempt, e, delay)
            sleep(delay)
            continue
        except Exception:
            breaker.release()
            raise
        breaker.record_success()
        return result, attempt
