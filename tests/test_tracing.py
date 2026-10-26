"""Traces: one consensus.run span with provider.call children, across worker threads,
and exemplars on the latency histograms so Grafana can jump from a slow point to its trace."""
import pytest

pytest.importorskip("opentelemetry.sdk")


@pytest.fixture
def spans(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", lambda *a, **k: provider.get_tracer("test"))
    return exporter


def test_provider_calls_nest_under_the_run_span(llm, spans):
    from app.orchestrator import run_pipeline

    run_pipeline("q", "", {"provider": "alpha", "model": "a1", "effort": "low"},
                 {"provider": "beta", "model": "b1", "effort": "low"}, "consensus")
    finished = spans.get_finished_spans()
    root = [s for s in finished if s.name == "consensus.run"][0]
    calls = [s for s in finished if s.name == "provider.call"]
    assert root.attributes["crossfact.outcome"] == "agreed"
    assert {c.attributes["crossfact.purpose"] for c in calls} == {"answer", "compare", "merge"}
    assert all(c.parent is not None and c.context.trace_id == root.context.trace_id for c in calls)
    answer_spans = [c for c in calls if c.attributes["crossfact.purpose"] == "answer"]
    assert {c.attributes["crossfact.provider"] for c in answer_spans} == {"alpha", "beta"}  # both threads traced


def test_failed_calls_are_marked_on_the_span(llm, spans):
    from opentelemetry.trace import StatusCode

    from app.orchestrator import run_pipeline
    from app.resilience import ErrorKind, ProviderError

    llm.on(provider="beta", error=ProviderError("401", ErrorKind.AUTH))
    run_pipeline("q", "", {"provider": "alpha", "model": "a1", "effort": "low"},
                 {"provider": "beta", "model": "b1", "effort": "low"}, "consensus")
    failed = [s for s in spans.get_finished_spans()
              if s.name == "provider.call" and s.attributes["crossfact.provider"] == "beta"]
    assert failed and all(s.status.status_code is StatusCode.ERROR for s in failed)
    assert {s.attributes["crossfact.result"] for s in failed} == {"auth"}


def test_latency_histograms_carry_trace_exemplars(llm, spans):
    from prometheus_client import REGISTRY
    from prometheus_client.openmetrics.exposition import generate_latest

    from app.orchestrator import run_pipeline

    run_pipeline("q", "", {"provider": "alpha", "model": "a1", "effort": "low"},
                 {"provider": "beta", "model": "b1", "effort": "low"}, "single")
    text = generate_latest(REGISTRY).decode()
    lines = [ln for ln in text.splitlines() if ln.startswith("crossfact_run_duration_seconds_bucket") and "# {" in ln]
    assert lines and 'trace_id="' in lines[0]


def test_bounded_series_exist_at_zero_before_the_first_event(llm, monkeypatch):
    """increase() loses a counter's first event unless the series already exists at 0:
    the first degraded run, auth failure or fallback would never reach an SLO or alert."""
    from prometheus_client import CollectorRegistry, Histogram

    from app import orchestrator, providers
    from app import telemetry as tm

    fresh = CollectorRegistry()  # the process-wide one has series from other tests' runs
    for name in ("RUNS", "RUN_DURATION", "DEGRADED", "SUBSTITUTIONS", "PROVIDER_CALLS", "PROVIDER_DURATION",
                 "PROVIDER_RETRIES", "STRAGGLERS", "CIRCUIT_TRANSITIONS"):
        m = getattr(tm, name)
        extra = {"buckets": m._upper_bounds} if isinstance(m, Histogram) else {}
        clone = type(m)(m._name, m._documentation, m._labelnames, registry=fresh, **extra)
        monkeypatch.setattr(tm, name, clone)
    orchestrator.prime_metrics()
    providers.prime_metrics()

    def value(name, labels):
        v = fresh.get_sample_value(name, labels)
        assert v in (None, 0.0), f"{name}{labels} = {v}: priming must not count anything"
        return v
    assert value("crossfact_runs_total", {"mode": "consensus", "outcome": "degraded"}) is not None
    assert value("crossfact_runs_total", {"mode": "single", "outcome": "abandoned"}) is not None
    assert value("crossfact_degraded_runs_total", {"reason": "judge_failed"}) is not None
    assert value("crossfact_run_duration_seconds_bucket",
                 {"mode": "consensus", "result": "success", "le": "240.0"}) is not None
    assert value("crossfact_provider_calls_total",
                 {"provider": "alpha", "model": "a1", "purpose": "answer", "result": "auth"}) is not None
    assert value("crossfact_provider_calls_total",
                 {"provider": "judge", "model": "j1", "purpose": "judge", "result": "quota_exhausted"}) is not None
    assert value("crossfact_provider_calls_total",
                 {"provider": "util", "model": "u2", "purpose": "merge", "result": "ok"}) is not None
    assert value("crossfact_provider_retries_total", {"provider": "beta", "reason": "rate_limited"}) is not None
    assert value("crossfact_substitutions_total", {"side": "B", "from_model": "b1", "to_model": "b2"}) is not None
    assert value("crossfact_circuit_transitions_total", {"breaker": "beta/b2", "to": "open"}) is not None
    # disabled providers stay out: no series for what can't be called
    assert value("crossfact_provider_calls_total",
                 {"provider": "off", "model": "o1", "purpose": "answer", "result": "ok"}) is None


def test_buffered_spans_are_sent_at_shutdown(monkeypatch):
    """Spans go out in batches; the app flushes them on its way out, so the runs a
    drain waited for still reach Tempo."""
    from app import telemetry as tm

    calls = []

    class Provider:
        def force_flush(self, ms):
            calls.append(("flush", ms))

        def shutdown(self):
            calls.append(("shutdown",))

    monkeypatch.setattr(tm, "_tracing_ready", True)
    monkeypatch.setattr(tm.trace, "get_tracer_provider", lambda: Provider())
    tm.shutdown_tracing()
    assert calls == [("flush", 10000), ("shutdown",)] and tm._tracing_ready is False
    tm.shutdown_tracing()  # a second call is harmless
    assert len(calls) == 2
