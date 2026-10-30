"""Deadlines, backoff, circuit breakers: pure logic with a fake clock."""
import pytest

from app.resilience import (
    BreakerPolicy,
    BreakerState,
    CircuitBreaker,
    Deadline,
    ErrorKind,
    ProviderError,
    RetryPolicy,
    call_with_resilience,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_deadline_caps_timeouts():
    clock = Clock()
    d = Deadline(100, clock=clock)
    assert d.cap(300) == 100
    clock.t += 70
    assert d.remaining() == pytest.approx(30)
    assert d.cap(300) == pytest.approx(30)
    clock.t += 40
    assert d.expired() and d.remaining() == 0


def test_full_jitter_backoff_stays_under_the_cap():
    p = RetryPolicy(max_attempts=5, base_delay_s=1, max_delay_s=8)
    assert p.backoff(1, rng=lambda: 1.0) == 1
    assert p.backoff(3, rng=lambda: 1.0) == 4
    assert p.backoff(10, rng=lambda: 1.0) == 8          # capped
    assert p.backoff(3, rng=lambda: 0.0) == 0           # jitter can go to zero


def test_retry_decisions_by_error_kind():
    p = RetryPolicy(max_attempts=3, base_delay_s=1, max_delay_s=30)
    one = lambda: 1.0  # noqa: E731
    assert p.delay_for(ProviderError("x", ErrorKind.AUTH), 1, rng=one) is None
    assert p.delay_for(ProviderError("x", ErrorKind.QUOTA_EXHAUSTED), 1, rng=one) is None
    assert p.delay_for(ProviderError("x", ErrorKind.BAD_REQUEST), 1, rng=one) is None
    assert p.delay_for(ProviderError("x", ErrorKind.SERVER), 2, rng=one) == 2
    # a 429 with a short hint: honour the hint (plus jitter)
    assert p.delay_for(ProviderError("x", ErrorKind.RATE_LIMITED, retry_after=10), 1, rng=lambda: 0) == 10
    # a 429 that says "come back much later" is not worth waiting for
    assert p.delay_for(ProviderError("x", ErrorKind.RATE_LIMITED, retry_after=3600), 1, rng=one) is None


def test_breaker_opens_after_threshold_then_half_opens_with_one_trial():
    clock = Clock()
    transitions = []
    b = CircuitBreaker("p/m", BreakerPolicy(failure_threshold=3, cooldown_s=60, max_cooldown_s=200), clock=clock,
                       on_transition=lambda n, s: transitions.append(s))
    for _ in range(3):
        assert b.allow()
        b.record_failure()
    assert b.state is BreakerState.OPEN and not b.allow()
    clock.t += 59
    assert not b.allow()
    clock.t += 1
    assert b.allow()                       # the single trial
    assert not b.allow()                   # concurrent callers keep waiting
    b.record_failure()                     # trial failed: reopen with doubled cooldown
    clock.t += 60
    assert not b.allow()
    clock.t += 60
    assert b.allow()
    b.record_success()
    assert b.state is BreakerState.CLOSED and b.allow()
    assert transitions == [BreakerState.OPEN, BreakerState.HALF_OPEN, BreakerState.OPEN,
                           BreakerState.HALF_OPEN, BreakerState.CLOSED]


def test_breaker_cooldown_is_capped():
    clock = Clock()
    b = CircuitBreaker("p/m", BreakerPolicy(failure_threshold=1, cooldown_s=100, max_cooldown_s=150), clock=clock)
    b.allow()
    b.record_failure()
    for _ in range(4):
        clock.t += 1000
        assert b.allow()
        b.record_failure()
    assert b.retry_in() == pytest.approx(150)


def _call(fn, *, attempts=3, deadline=600, breaker=None, clock=None, retries=None):
    clock = clock or Clock()
    return call_with_resilience(
        fn, timeout_s=60, deadline=Deadline(deadline, clock=clock),
        policy=RetryPolicy(max_attempts=attempts, base_delay_s=1, max_delay_s=4),
        breaker=breaker or CircuitBreaker("t", clock=clock), sleep=clock.sleep,
        on_retry=(lambda n, e, d: retries.append((n, e.kind, d))) if retries is not None else None)


def test_transient_failures_are_retried_until_success():
    seen, retries = [], []

    def flaky(timeout):
        seen.append(timeout)
        if len(seen) < 3:
            raise ProviderError("503", ErrorKind.SERVER)
        return "ok"

    assert _call(flaky, retries=retries) == ("ok", 3)
    assert [k for _, k, _ in retries] == [ErrorKind.SERVER, ErrorKind.SERVER]


@pytest.mark.parametrize("status", [503, 529])
def test_overloaded_answers_go_to_the_substitute_instead_of_retrying(status):
    calls, breaker = [], CircuitBreaker("p/m", clock=Clock())

    def busy(timeout):
        calls.append(1)
        raise ProviderError(f"gemini {status}: high demand", ErrorKind.SERVER, status=status)

    with pytest.raises(ProviderError):
        _call(busy, breaker=breaker)
    assert len(calls) == 1                       # no retry on the same busy model
    assert breaker.snapshot()["failures"] == 1   # but the breaker still counts it


def test_auth_errors_fail_fast():
    calls = []

    def bad_key(timeout):
        calls.append(1)
        raise ProviderError("401", ErrorKind.AUTH)

    with pytest.raises(ProviderError) as e:
        _call(bad_key)
    assert e.value.kind is ErrorKind.AUTH and len(calls) == 1 and e.value.attempts == 1


def test_open_circuit_refuses_without_calling():
    clock = Clock()
    b = CircuitBreaker("t", BreakerPolicy(failure_threshold=1), clock=clock)
    b.allow()
    b.record_failure()
    calls = []
    with pytest.raises(ProviderError) as e:
        _call(lambda t: calls.append(t), breaker=b, clock=clock)
    assert e.value.kind is ErrorKind.CIRCUIT_OPEN and not calls


def test_no_attempt_when_the_run_budget_is_nearly_spent():
    with pytest.raises(ProviderError) as e:
        _call(lambda t: "never", deadline=3)
    assert e.value.kind is ErrorKind.DEADLINE


def test_retry_skipped_when_backoff_would_blow_the_budget():
    clock = Clock()
    calls = []

    def slow_fail(timeout):
        calls.append(timeout)
        clock.t += timeout - 1  # the failing call ate almost its whole timeout
        raise ProviderError("timeout", ErrorKind.TIMEOUT)

    with pytest.raises(ProviderError):
        _call(slow_fail, deadline=70, clock=clock)
    assert len(calls) == 1  # 60s gone, 10s left: not enough for backoff + a useful call


def test_hinted_rate_limits_are_retried_without_tripping_the_breaker():
    """Game day "gemini-429": counting honoured 429s opened breakers and degraded runs."""
    clock = Clock()
    b = CircuitBreaker("t", BreakerPolicy(failure_threshold=2), clock=clock)
    seen = []

    def throttled(timeout):
        seen.append(timeout)
        if len(seen) < 3:
            raise ProviderError("429", ErrorKind.RATE_LIMITED, retry_after=2.0)
        return "ok"

    assert _call(throttled, breaker=b, clock=clock) == ("ok", 3)
    assert b.state is BreakerState.CLOSED


def test_unhinted_or_long_rate_limits_still_count():
    from app.resilience import trips_breaker

    policy = RetryPolicy(max_delay_s=30)
    assert trips_breaker(ProviderError("429", ErrorKind.RATE_LIMITED), policy)
    assert trips_breaker(ProviderError("429", ErrorKind.RATE_LIMITED, retry_after=3600), policy)
    assert not trips_breaker(ProviderError("429", ErrorKind.RATE_LIMITED, retry_after=20), policy)
    assert trips_breaker(ProviderError("timeout", ErrorKind.TIMEOUT), policy)


def test_fault_cli_adds_lists_and_clears(llm, capsys):
    from app import db, faults

    assert faults.main(["add", "beta", "quota", "--minutes", "999", "--probability", "7"]) == 0
    (f,) = db.active_faults()
    assert f["target"] == "beta" and f["mode"] == "quota" and f["probability"] == 1.0
    assert f["until"] - f["created_at"] <= 240 * 60 + 1  # chaos always expires, at most 4 hours
    faults.main(["list"])
    assert "beta" in capsys.readouterr().out
    faults.main(["clear", "--all"])
    assert db.active_faults() == []
    with pytest.raises(SystemExit):
        faults.main(["add", "beta", "meltdown"])
