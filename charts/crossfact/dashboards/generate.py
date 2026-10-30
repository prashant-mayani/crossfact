#!/usr/bin/env python3
"""Generate the Grafana dashboards shipped with the chart.

Dashboards are code here (diffable, reviewable), and this script writes the
JSON that the chart's ConfigMap embeds. Run it after editing:

    python charts/crossfact/dashboards/generate.py
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DS = {"type": "prometheus", "uid": "${datasource}"}
NS = 'namespace="$namespace"'


class Board:
    def __init__(self, uid: str, title: str, description: str):
        self.uid, self.title, self.description = uid, title, description
        self.panels: list[dict] = []
        self.y = 0
        self.next_id = 1

    def row(self, title: str) -> None:
        self.panels.append({"type": "row", "title": title, "id": self._id(), "collapsed": False,
                            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}, "panels": []})
        self.y += 1

    def add(self, panels: list[dict], h: int = 8) -> None:
        width = 24 // len(panels)
        for i, p in enumerate(panels):
            p["id"] = self._id()
            p["gridPos"] = {"h": h, "w": width, "x": i * width, "y": self.y}
            p.setdefault("datasource", DS)
        self.panels += panels
        self.y += h

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id - 1

    def json(self) -> dict:
        return {
            "uid": self.uid, "title": self.title, "description": self.description,
            "tags": ["crossfact"], "timezone": "browser", "schemaVersion": 39, "version": 1,
            "editable": True, "graphTooltip": 1, "refresh": "1m",
            "time": {"from": "now-24h", "to": "now"},
            "templating": {"list": [
                {"name": "datasource", "type": "datasource", "query": "prometheus", "label": "Data source",
                 "current": {"text": "Prometheus", "value": "prometheus"}},
                {"name": "namespace", "type": "custom", "query": "crossfact", "label": "Namespace",
                 "current": {"text": "crossfact", "value": "crossfact"}, "hide": 2},
            ]},
            "panels": self.panels,
        }


def target(expr: str, legend: str = "", exemplar: bool = False, instant: bool = False) -> dict:
    t = {"expr": expr, "legendFormat": legend or "__auto", "refId": "ABCDEFGH"[0], "exemplar": exemplar}
    if instant:
        t["instant"], t["range"] = True, False
    return t


def targets(*ts: dict) -> list[dict]:
    for i, t in enumerate(ts):
        t["refId"] = "ABCDEFGH"[i]
    return list(ts)


def timeseries(title: str, *ts: dict, unit: str = "short", desc: str = "", stack: bool = False) -> dict:
    return {"type": "timeseries", "title": title, "description": desc, "targets": targets(*ts),
            "fieldConfig": {"defaults": {"unit": unit, "custom": {
                "drawStyle": "line", "lineWidth": 2, "fillOpacity": 10, "showPoints": "never",
                "stacking": {"mode": "normal" if stack else "none"}}}, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def stat(title: str, expr: str, unit: str = "short", desc: str = "", thresholds=None, decimals=None) -> dict:
    steps = thresholds or [{"color": "green", "value": None}]
    defaults = {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {"type": "stat", "title": title, "description": desc, "targets": targets(target(expr, instant=True)),
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none",
                        "textMode": "value"}}


def table(title: str, expr: str, desc: str = "") -> dict:
    t = target(expr, instant=True)
    t["format"] = "table"
    return {"type": "table", "title": title, "description": desc, "targets": targets(t),
            "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True}}}],
            "fieldConfig": {"defaults": {}, "overrides": []}, "options": {"showHeader": True}}


def budget_thresholds():
    return [{"color": "red", "value": None}, {"color": "orange", "value": 0.25}, {"color": "green", "value": 0.5}]


def overview() -> dict:
    b = Board("crossfact-overview", "Crossfact overview",
              "Runs, providers, and failure handling. Exemplar dots link latency points to Tempo traces.")
    b.row("Runs")
    b.add([
        stat("Runs (24h)", f"sum(increase(crossfact_runs_total{{{NS}}}[24h]))", decimals=0),
        # Runs that asked two models and got both (computed answers asked none: left out)
        stat("Full two-model results (24h)",
             f'sum(increase(crossfact_runs_total{{{NS},outcome=~"agreed|debated|judged|union|majority|unsure"}}'
             f'[24h])) / sum(increase(crossfact_runs_total{{{NS},mode!~"single|exact"}}[24h]))',
             unit="percentunit", decimals=1),
        stat("Degraded (24h)", f"sum(increase(crossfact_degraded_runs_total{{{NS}}}[24h]))", decimals=0,
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]),
        stat("In flight now", f"sum(crossfact_runs_in_flight{{{NS}}})", decimals=0),
    ], h=4)
    b.add([
        timeseries("Runs by outcome", target(f"sum by (outcome) (increase(crossfact_runs_total{{{NS}}}[1h]))",
                                             "{{outcome}}"), stack=True,
                   desc="degraded = finished without a full two-model result; abandoned = a pod died mid-run."),
        timeseries("Run duration (consensus)",
                   target(f'histogram_quantile(0.5, sum by (le) (rate(crossfact_run_duration_seconds_bucket'
                          f'{{{NS},mode="consensus"}}[30m])))', "p50", exemplar=True),
                   target(f'histogram_quantile(0.95, sum by (le) (rate(crossfact_run_duration_seconds_bucket'
                          f'{{{NS},mode="consensus"}}[30m])))', "p95", exemplar=True),
                   unit="s", desc="Click an exemplar dot to open the trace of that run."),
    ])
    b.row("Providers: who is slow, who is failing")
    b.add([
        timeseries("p95 model-call latency by model",
                   target('histogram_quantile(0.95, sum by (le, provider, model) '
                          f'(rate(crossfact_provider_call_duration_seconds_bucket{{{NS}}}[30m])))',
                          "{{provider}}/{{model}}", exemplar=True), unit="s",
                   desc="Includes retries. The slowest line is the provider holding runs back."),
        timeseries("Call outcomes",
                   target(f'sum by (provider, result) (increase(crossfact_provider_calls_total{{{NS}}}[1h]))',
                          "{{provider}} {{result}}"), stack=True),
    ])
    b.add([
        timeseries("Retries by cause", target(f"sum by (provider, reason) "
                                              f"(increase(crossfact_provider_retries_total{{{NS}}}[1h]))",
                                              "{{provider}} {{reason}}"),
                   target(f"sum by (provider, model) "
                          f"(increase(crossfact_straggler_cutoffs_total{{{NS}}}[1h]))",
                          "cut off: {{provider}}/{{model}}"),
                   desc="Retries, and sides left behind because they were still running long after the "
                        "other model answered (pipeline.straggler_grace_s)."),
        timeseries("Circuit breakers (0 closed · 1 half-open · 2 open)",
                   target(f"max by (breaker) (crossfact_circuit_state{{{NS}}})", "{{breaker}}")),
        table("Substitutions (7d)", f"sum by (side, from_model, to_model) "
                                    f"(increase(crossfact_substitutions_total{{{NS}}}[7d])) > 0",
              desc="A fallback model stood in for a side whose model failed."),
    ])
    b.row("Service")
    b.add([
        timeseries("HTTP requests by route",
                   target(f'sum by (route) (rate(crossfact_http_requests_total{{{NS},route!="/static"}}[5m]))',
                          "{{route}}"), unit="reqps"),
        timeseries("HTTP p95 latency",
                   target(f'histogram_quantile(0.95, sum by (le, route) (rate(crossfact_http_request_duration_seconds'
                          f'_bucket{{{NS},route!="/static"}}[5m])))', "{{route}}"), unit="s"),
        timeseries("Draining, orphans, faults",
                   target(f"max(crossfact_draining{{{NS}}})", "draining"),
                   target(f"sum(increase(crossfact_orphaned_runs_total{{{NS}}}[1h]))", "orphaned runs"),
                   target(f"sum(crossfact_fault_injections_active{{{NS}}})", "active faults")),
    ])
    b.row("Answer quality (nightly check)")
    nightly = ("The same ten SimpleQA Verified questions every night (docs/EVAL.md). One question is ten "
               "points, so read the trend over weeks, not a single night. Empty until eval.nightly.enabled.")
    b.add([
        timeseries("Correct, by system",
                   target(f'max by (system) (crossfact_eval_answer_ratio{{{NS},result="correct"}})', "{{system}}"),
                   unit="percentunit", desc=nightly),
        timeseries("Wrong, by system",
                   target(f'max by (system) (crossfact_eval_answer_ratio{{{NS},result="incorrect"}})', "{{system}}"),
                   unit="percentunit",
                   desc="Wrong answers, not abstentions: a system that says \"I don't know\" more shows up as "
                        "fewer wrong and fewer correct together."),
        stat("Since the last check", f"time() - max(crossfact_eval_last_run_timestamp_seconds{{{NS}}})", unit="s",
             desc="A run stopped by a daily budget still counts; its numbers cover the questions it reached.",
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 2 * 86400}]),
    ])
    return b.json()


def slo() -> dict:
    b = Board("crossfact-slo", "Crossfact SLOs",
              "Three SLOs over 28 days. Budget remaining below 0 means the objective is missed.")
    specs = [
        ("Availability (synthetic probes, gaps count as down)", "availability", "0.995"),
        ("Consensus success (full two-model results)", "consensus_success", "0.95"),
        ("Consensus latency (successful runs under threshold)", "consensus_latency", "0.95"),
    ]
    for title, key, _ in specs:
        b.row(title)
        panels = [
            stat("SLI, 28 days", f"crossfact:{key}:ratio_window", unit="percentunit", decimals=3),
            stat("Error budget remaining", f"crossfact:{key}:error_budget_remaining", unit="percentunit",
                 decimals=1, thresholds=budget_thresholds()),
        ]
        if key == "availability":
            panels.append(timeseries("Error ratio by window",
                                     target("crossfact:availability:error_ratio_rate5m", "5m"),
                                     target("crossfact:availability:error_ratio_rate1h", "1h"),
                                     target("crossfact:availability:error_ratio_rate6h", "6h"),
                                     unit="percentunit"))
        else:
            panels.append(timeseries("Error ratio by window, with traffic",
                                     target(f"crossfact:{key}:error_ratio_rate1h", "1h error ratio"),
                                     target(f"crossfact:{key}:error_ratio_rate6h", "6h error ratio"),
                                     target("crossfact:consensus:runs_increase6h", "runs in 6h"),
                                     unit="short",
                                     desc="Burn alerts need enough runs in the window; see docs/SLO.md."))
        b.add(panels, h=6)
    return b.json()


if __name__ == "__main__":
    for name, board in (("crossfact-overview.json", overview()), ("crossfact-slo.json", slo())):
        with open(os.path.join(HERE, name), "w") as f:
            json.dump(board, f, indent=1)
            f.write("\n")
        print("wrote", name, len(board["panels"]), "panels")
