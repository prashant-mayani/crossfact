"""The nightly answer-quality check, as metrics.

The chart's eval.nightly CronJob (python -m bench with bench/configs/nightly.yaml)
writes its results to <data_dir>/bench/latest.json. This collector reads that
file when Prometheus scrapes, and re-reads it only when it changes, so the
numbers reach the dashboards without the job and the app sharing anything but
the volume. No file, no series.

  crossfact_eval_answer_ratio{system, result}  share of graded questions: correct, incorrect, not_attempted
  crossfact_eval_graded_questions{system}      questions graded in the last run
  crossfact_eval_errors{system}                questions that failed on provider trouble (not scored)
  crossfact_eval_paused                        1 if the last run stopped on a daily budget
  crossfact_eval_last_run_timestamp_seconds    when the last run finished

Labels come from the job's own configuration (a handful of system names), never
from user input; at most MAX_SYSTEMS are exported.
"""
from __future__ import annotations

import json
import logging
import os
import threading

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import REGISTRY

from .config import cfg

log = logging.getLogger("crossfact.eval")
MAX_SYSTEMS = 20
RESULTS = (("correct", "correct_rate"), ("incorrect", "incorrect_rate"), ("not_attempted", "not_attempted_rate"))


def results_path() -> str:
    return os.path.join(cfg()["data_dir"], "bench", "latest.json")


def _families():
    return (GaugeMetricFamily("crossfact_eval_answer_ratio", "Nightly check: share of graded questions, by result",
                              labels=["system", "result"]),
            GaugeMetricFamily("crossfact_eval_graded_questions", "Nightly check: questions graded", labels=["system"]),
            GaugeMetricFamily("crossfact_eval_errors", "Nightly check: questions lost to provider trouble",
                              labels=["system"]))


class EvalCollector:
    def __init__(self, path_fn=results_path):
        self.path_fn = path_fn
        self._lock = threading.Lock()
        self._seen: tuple | None = None
        self._data: dict | None = None

    def _load(self) -> dict | None:
        path = self.path_fn()
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (path, st.st_mtime_ns, st.st_size)
        with self._lock:
            if key != self._seen:
                try:
                    with open(path) as f:
                        data = json.load(f)
                    self._data = data if isinstance(data, dict) and isinstance(data.get("systems"), dict) else None
                except (OSError, ValueError) as e:
                    log.warning("can't read %s: %s", path, e)
                    self._data = None
                self._seen = key
            return self._data

    def describe(self):
        yield from _families()
        yield GaugeMetricFamily("crossfact_eval_paused", "Nightly check: 1 if it stopped on a daily budget")
        yield GaugeMetricFamily("crossfact_eval_last_run_timestamp_seconds", "Nightly check: when it last finished")

    def collect(self):
        data = self._load()
        if not data:
            return
        ratio, graded, errors = _families()
        for name, s in list(data["systems"].items())[:MAX_SYSTEMS]:
            if not isinstance(s, dict):
                continue
            for result, key in RESULTS:
                if isinstance(s.get(key), (int, float)):
                    ratio.add_metric([str(name), result], float(s[key]))
            graded.add_metric([str(name)], float(s.get("n") or 0))
            errors.add_metric([str(name)], float(s.get("errors") or 0))
        yield from (ratio, graded, errors)
        yield GaugeMetricFamily("crossfact_eval_paused", "Nightly check: 1 if it stopped on a daily budget",
                                value=1.0 if data.get("paused") else 0.0)
        if isinstance(data.get("finished_ts"), (int, float)):
            yield GaugeMetricFamily("crossfact_eval_last_run_timestamp_seconds", "Nightly check: when it last finished",
                                    value=float(data["finished_ts"]))


_registered = False
_reg_lock = threading.Lock()


def register(registry=REGISTRY) -> None:
    global _registered
    with _reg_lock:
        if not _registered:
            registry.register(EvalCollector())
            _registered = True
