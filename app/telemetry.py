"""Metrics (Prometheus) and traces (OpenTelemetry -> Tempo).

Metrics are served on their own port (server.metrics_port, default 9095), so
nothing here is ever reachable through the Cloudflare tunnel. Histograms carry
exemplars (the trace_id of a sampled run) so a slow dot on a Grafana panel
links straight to the trace that explains it.

Label sets are small and bounded: provider, model, purpose and result come from
the registry or fixed enums, never from user input.
"""
from __future__ import annotations

import logging
import os
import threading

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, Info, start_http_server

log = logging.getLogger("crossfact.telemetry")

RUN_BUCKETS = (15, 30, 60, 90, 120, 180, 240, 300, 420, 600, 900)
CALL_BUCKETS = (1, 2.5, 5, 10, 20, 30, 60, 90, 120, 180, 300, 420)
HTTP_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

BUILD = Info("crossfact_build", "Build information")
HTTP_REQUESTS = Counter("crossfact_http_requests", "HTTP requests", ["route", "method", "status"])
HTTP_DURATION = Histogram("crossfact_http_request_duration_seconds", "HTTP request latency",
                          ["route", "method"], buckets=HTTP_BUCKETS)
RUNS = Counter("crossfact_runs", "Finished pipeline runs", ["mode", "outcome"])
RUN_DURATION = Histogram("crossfact_run_duration_seconds", "End-to-end run time",
                         ["mode", "result"], buckets=RUN_BUCKETS)
RUNS_IN_FLIGHT = Gauge("crossfact_runs_in_flight", "Runs executing in this pod")
DEGRADED = Counter("crossfact_degraded_runs", "Runs that finished degraded", ["reason"])
SUBSTITUTIONS = Counter("crossfact_substitutions", "A fallback model answered for a side",
                        ["side", "from_model", "to_model"])
PROVIDER_CALLS = Counter("crossfact_provider_calls", "Model calls by outcome",
                         ["provider", "model", "purpose", "result"])
PROVIDER_DURATION = Histogram("crossfact_provider_call_duration_seconds", "Model call latency incl. retries",
                              ["provider", "model", "purpose"], buckets=CALL_BUCKETS)
PROVIDER_RETRIES = Counter("crossfact_provider_retries", "Retries by cause", ["provider", "reason"])
STRAGGLERS = Counter("crossfact_straggler_cutoffs", "A side still running long after the other answered, left behind",
                     ["provider", "model", "stage"])
CIRCUIT_STATE = Gauge("crossfact_circuit_state", "Breaker state: 0 closed, 1 half-open, 2 open", ["breaker"])
CIRCUIT_TRANSITIONS = Counter("crossfact_circuit_transitions", "Breaker state changes", ["breaker", "to"])
ORPHANS = Counter("crossfact_orphaned_runs", "Runs found abandoned by a dead pod and closed out")
DRAINING = Gauge("crossfact_draining", "1 while this pod is draining for shutdown")
FAULTS_ACTIVE = Gauge("crossfact_fault_injections_active", "Active injected faults", ["target", "mode"])
PREFERRED_ACTIVE = Gauge("crossfact_preferred_model_active",
                         "1 while new questions on a side start with its preferred model (app/promotion.py)", ["side"])
PREFERRED_CHECKS = Counter("crossfact_preferred_model_checks", "Background checks of a side's preferred model",
                           ["side", "result"])

class _ScrapeWatch:
    """Notes that something has read this pod's metrics: on the metrics port, only
    Prometheus can. Counting an event before the first scrape loses it (increase()
    starts from the first sample it sees), so startup bookkeeping waits for this."""

    def __init__(self):
        self.seen = threading.Event()

    def describe(self):
        return []

    def collect(self):
        self.seen.set()
        return []


SCRAPED = _ScrapeWatch()
REGISTRY.register(SCRAPED)

_metrics_started = False
_metrics_lock = threading.Lock()


def start_metrics_server(port: int) -> None:
    global _metrics_started
    with _metrics_lock:
        if _metrics_started or not port:
            return
        start_http_server(port, addr="0.0.0.0")
        _metrics_started = True
        log.info("metrics on :%s/metrics", port)


# ---- tracing ------------------------------------------------------------------

try:  # the SDK is a hard dependency in the image; keep tests importable without it
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
except ImportError:  # pragma: no cover
    trace = None
    Status = StatusCode = None

_tracing_ready = False


def init_tracing(tracing_cfg: dict, version: str) -> None:
    global _tracing_ready
    if _tracing_ready or trace is None or not tracing_cfg.get("enabled") or not tracing_cfg.get("endpoint"):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    resource = Resource.create({
        "service.name": "crossfact",
        "service.version": version,
        "k8s.pod.name": os.environ.get("POD_NAME", ""),
    })
    provider = TracerProvider(resource=resource,
                              sampler=ParentBased(TraceIdRatioBased(float(tracing_cfg.get("sample_ratio", 1.0)))))
    endpoint = tracing_cfg["endpoint"].rstrip("/")
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    _tracing_ready = True
    log.info("tracing -> %s", endpoint)


def shutdown_tracing(timeout_s: float = 10.0) -> None:
    """Flush and stop the span exporter. Spans are sent in batches, so without this
    the last few (the runs a drain waited for) die with the process."""
    global _tracing_ready
    if not _tracing_ready or trace is None:
        return
    provider = trace.get_tracer_provider()
    try:
        provider.force_flush(int(timeout_s * 1000))
        provider.shutdown()
    except Exception:  # an unreachable collector must not hang the exit
        log.warning("could not flush traces at shutdown", exc_info=True)
    _tracing_ready = False


def tracer():
    return trace.get_tracer("crossfact") if trace else None


class _NoSpan:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def set_attribute(self, *a, **k):
        pass

    def set_attributes(self, *a, **k):
        pass

    def add_event(self, *a, **k):
        pass

    def set_status(self, *a, **k):
        pass

    def record_exception(self, *a, **k):
        pass


def span(name: str, **attributes):
    t = tracer()
    if t is None:
        return _NoSpan()
    clean = {k: v for k, v in attributes.items() if v is not None}
    return t.start_as_current_span(name, attributes=clean)


def mark_error(sp, message: str) -> None:
    if Status is not None and hasattr(sp, "set_status"):
        sp.set_status(Status(StatusCode.ERROR, message[:200]))


def trace_id() -> str | None:
    if trace is None:
        return None
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid or not ctx.trace_flags.sampled:
        return None
    return format(ctx.trace_id, "032x")


def exemplar() -> dict | None:
    tid = trace_id()
    return {"trace_id": tid} if tid else None


def observe(histogram, value: float, **labels) -> None:
    h = histogram.labels(**labels)
    ex = exemplar()
    if ex:
        h.observe(value, exemplar=ex)
    else:
        h.observe(value)
