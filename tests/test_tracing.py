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
