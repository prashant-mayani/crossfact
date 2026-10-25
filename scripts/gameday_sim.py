#!/usr/bin/env python3
"""Game days on a laptop: break the model providers on purpose and watch what the
pipeline does, in minutes instead of hours.

What is real here: the orchestrator, timeouts, retries and backoff, circuit
breakers, fallbacks, run deadline, fault injection (the same faults table the
admin page writes), and the Prometheus metrics. What is simulated: the models.
Each call sleeps for a modelled latency and returns canned JSON, and the whole
process runs on a clock SCALE times faster than real time (default 100x, so a
600 s run budget takes 6 s).

The latency model is an assumption, not a measurement (see LATENCY below);
the point is the shape of the behaviour under failure, not exact numbers.
Measured results from the real cluster go in docs/RESILIENCE.md next to these.

    python scripts/gameday_sim.py                    # every scenario, markdown report
    python scripts/gameday_sim.py --scenario claude-hangs --runs 12
    python scripts/gameday_sim.py --list
"""
from __future__ import annotations

import argparse
import math
import os
import random
import statistics
import sys
import tempfile
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ---- a faster clock, installed before the app is imported -----------------------------------------

class ScaledClock:
    """monotonic/time/sleep all running `scale` times faster than wall time.
    Threads keep real concurrency: two sides sleeping 20 s each overlap, as they would."""

    def __init__(self, scale: float):
        self.scale = scale
        self._mono, self._sleep = time.monotonic, time.sleep
        self.m0, self.t0 = time.monotonic(), time.time()

    def monotonic(self) -> float:
        return self.m0 + (self._mono() - self.m0) * self.scale

    def time(self) -> float:
        return self.t0 + (self._mono() - self.m0) * self.scale

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(seconds / self.scale)

    def install(self) -> None:
        time.monotonic, time.time, time.sleep = self.monotonic, self.time, self.sleep


# ---- the simulated models ---------------------------------------------------------------------------

# Median latency in seconds per (provider, purpose), before model multipliers and
# log-normal noise (sigma 0.35). Assumptions, roughly in line with what a 2026
# Claude Sonnet and Gemini Flash take for a substantial question.
LATENCY = {
    ("claude", "answer"): 55, ("claude", "debate"): 35, ("claude", "compare"): 14,
    ("claude", "judge"): 40, ("claude", "merge"): 22,
    ("gemini", "answer"): 24, ("gemini", "debate"): 16, ("gemini", "compare"): 7,
    ("gemini", "judge"): 26, ("gemini", "merge"): 11,
    # GPT-OSS 120B: fast on Groq's hardware, slower on Workers AI
    ("groq", "answer"): 9, ("groq", "debate"): 7, ("groq", "compare"): 4, ("groq", "judge"): 8, ("groq", "merge"): 5,
    ("workers ai", "answer"): 30, ("workers ai", "debate"): 22, ("workers ai", "compare"): 12,
    ("workers ai", "judge"): 25, ("workers ai", "merge"): 15,
}
MODEL_FACTOR = {"haiku": 0.45, "opus": 1.6, "gemini-3.5-flash-lite": 0.6, "gemini-3.6-flash": 1.1}
P_AGREE = 0.55  # chance the comparator finds no conflicts

COMPARE_AGREE = ('{"verdict":"agree","agreements":["x"],"unique_a":["a detail"],"unique_b":["b detail"],'
                 '"conflicts":[]}')
COMPARE_CONFLICT = ('{"verdict":"partial","agreements":["x"],"unique_a":[],"unique_b":[],'
                    '"conflicts":[{"topic":"y","a_position":"1","b_position":"2"}]}')
JUDGE = '{"rulings":[{"point":1,"winner":"A","reason":"better sourced"}],"analysis":"A cites evidence."}'


def purpose_of(prompt: str) -> str:
    if "strict comparator" in prompt:
        return "compare"
    if "neutral arbiter" in prompt:
        return "judge"
    if "Reconsider ONLY" in prompt:
        return "debate"
    if "Combine the material" in prompt:
        return "merge"
    return "answer"


class SimModels:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.calls: Counter = Counter()
        self.p_agree = P_AGREE

    def __call__(self, p, model, effort, prompt, files, timeout):
        from app.resilience import ErrorKind, ProviderError

        pid = p["label"].lower()
        purpose = purpose_of(prompt)
        self.calls[(pid, model, purpose)] += 1
        median = LATENCY[(pid, purpose)] * MODEL_FACTOR.get(model, 1.0)
        latency = median * math.exp(self.rng.gauss(0, 0.35))
        if latency > timeout:
            time.sleep(timeout)
            raise ProviderError(f"{pid}/{model} timed out after {timeout:.0f}s", ErrorKind.TIMEOUT)
        time.sleep(latency)
        if purpose == "compare":
            return COMPARE_AGREE if self.rng.random() < self.p_agree else COMPARE_CONFLICT
        if purpose == "judge":
            return JUDGE
        return f"{purpose} from {pid}/{model}"


# ---- scenarios: written down before running them ------------------------------------------------------

SCENARIOS = [
    {"name": "baseline", "title": "No faults",
     "faults": [],
     "hypothesis": "Every run ends as a full two-model result (agreed, debated or judged); "
                   "no retries; p95 well under the 240 s latency threshold."},
    {"name": "always-contested", "title": "No faults, every question contested to the end",
     "faults": [], "p_agree": 0.0,
     "hypothesis": "Every run debates three rounds and is judged by GPT-OSS on Groq; runs take longer "
                   "but stay inside the 600 s budget; no degraded runs."},
    {"name": "gemini-flash-quota", "title": "Gemini 3.8 Flash out of daily quota",
     "faults": [("gemini/gemini-3.8-flash", "quota", 1.0, 0)],
     "hypothesis": "Side B fails fast (no retries on quota) and its configured substitute answers "
                   "(Gemini 3.6 Flash); the judge is unaffected (GPT-OSS on Groq). Runs stay two-model, each "
                   "recording the substitution; the success SLI is unaffected."},
    {"name": "groq-quota", "title": "Groq out of daily quota (the judge's first choice)",
     "faults": [("groq", "quota", 1.0, 0)], "p_agree": 0.0,  # every run needs a ruling
     "hypothesis": "The judge moves to the same model on Workers AI with no retries on Groq; runs that "
                   "need a ruling are still judged, a little slower; nothing degrades."},
    {"name": "gpt-oss-everywhere-down", "title": "GPT-OSS unavailable on both hosts",
     "faults": [("groq", "error", 1.0, 0), ("workers_ai", "error", 1.0, 0)], "p_agree": 0.0,
     "hypothesis": "After the retries, the judge falls back to Gemini (a side's own family: the transcript "
                   "says so); runs are still judged; both GPT-OSS breakers open after three failures."},
    {"name": "gemini-all-quota", "title": "All Gemini models out of daily quota",
     "faults": [("gemini", "quota", 1.0, 0)],
     "hypothesis": "Side B has no working substitute, so every run finishes with Claude's answer alone, "
                   "labelled degraded (side_b_failed), in about Claude's answer time. Zero retries."},
    {"name": "claude-hangs", "title": "Claude hangs on every call",
     "faults": [("claude", "timeout", 1.0, 0)],
     "hypothesis": "Runs still finish with Gemini's answer, labelled degraded (side_a_failed). The first "
                   "runs wait out Claude's 300 s timeout; after 3 failures the breaker opens and later runs "
                   "degrade in about Gemini's answer time, except one half-open trial per cooldown."},
    {"name": "claude-auth", "title": "Claude token expired (401)",
     "faults": [("claude", "auth", 1.0, 0)],
     "hypothesis": "Auth errors are not retried: runs degrade to Gemini alone within Gemini's answer time; "
                   "the AuthFailing alert's metric (calls with result=auth) rises with every run."},
    {"name": "gemini-429", "title": "Gemini rate-limited on half of calls (429, retry in 20 s)",
     "faults": [("gemini", "rate_limit", 0.5, 0)],
     "hypothesis": "Retries honour the 20 s hint; nearly every run stays two-model but gets slower; "
                   "retries by reason rate_limited show up; few or no degraded runs."},
    {"name": "gemini-flaky", "title": "Gemini returns 503 on 30% of calls",
     "faults": [("gemini", "error", 0.3, 0)],
     "hypothesis": "Jittered retries absorb almost all failures; outcomes match baseline; "
                   "the breaker may open briefly after three failures in a row, then recover."},
    {"name": "gemini-slow", "title": "Gemini 90 s slower on every call",
     "faults": [("gemini", "latency", 1.0, 90)],
     "hypothesis": "Runs complete but most exceed the 240 s threshold (latency SLO burn); longer debates "
                   "hit the 600 s run budget and end degraded with reason deadline; nothing hangs."},
]


# ---- harness ----------------------------------------------------------------------------------------

def setup(tmp: str, scale: float):
    import yaml

    clock = ScaledClock(scale)
    clock.install()
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config", "providers.example.yaml")))
    for pid, p in cfg["providers"].items():
        if pid in ("claude", "gemini", "groq", "workers_ai"):
            p.update(kind="sim", secret=None, enabled=True, base_url="")
        else:
            p["enabled"] = False
    providers_file = os.path.join(tmp, "providers.yaml")
    with open(providers_file, "w") as f:
        yaml.safe_dump(cfg, f)
    os.environ.update({
        "CROSSFACT_CONFIG": os.path.join(tmp, "missing-config.yaml"), "CROSSFACT_PROVIDERS": providers_file,
        "CROSSFACT_DATA_DIR": os.path.join(tmp, "data"), "CROSSFACT_SECRETS_MODE": "env",
        "CROSSFACT_METRICS_PORT": "0", "CROSSFACT_RUNS_DIR": os.path.join(tmp, "runs"),
        "CROSSFACT_CLI_HOME": os.path.join(tmp, "home"), "SESSION_SECRET": "sim", "FIELD_ENCRYPTION_KEY": "sim",
    })
    import app.config as config

    config.PROVIDERS_PATH, config.CONFIG_PATH = providers_file, os.environ["CROSSFACT_CONFIG"]
    config.reload()
    from app import db

    db.init_db()
    return cfg


def counters() -> dict:
    from prometheus_client import REGISTRY

    out = {}
    for metric in REGISTRY.collect():
        for s in metric.samples:
            if s.name.endswith("_total"):
                out[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return out


def delta(before: dict, after: dict, name: str, by: str) -> Counter:
    c: Counter = Counter()
    for (n, labels), v in after.items():
        if n == name:
            d = v - before.get((n, labels), 0.0)
            if d:
                c[dict(labels)[by] if by else ""] += d
    return c


def run_scenario(sc: dict, cfg: dict, models: SimModels, runs: int, gap_s: float) -> dict:
    from app import db, faults, orchestrator
    from app.resilience import BREAKERS

    BREAKERS.reset()
    for f in db.active_faults():
        db.delete_fault(f["id"])
    for target, mode, prob, latency in sc["faults"]:
        db.add_fault(target, mode, prob, latency, time.time() + 24 * 3600, "gameday-sim")
    faults.invalidate()
    before = counters()
    side_a, side_b = cfg["defaults"]["side_a"], cfg["defaults"]["side_b"]
    rows = []
    for i in range(runs):
        t0 = time.time()
        r = orchestrator.run_pipeline("What changed in the 2026 tax rules?", "", side_a, side_b, "consensus")
        answered = [(s.get("side"), s.get("model"), s.get("substituted_from")) for s in r["stages"]
                    if s["stage"] == "answer"]
        rows.append({"n": i + 1, "start": t0, "outcome": r["outcome"], "degraded": r["degraded"],
                     "duration": r["duration_s"], "answered": answered})
        time.sleep(gap_s)  # users don't ask back to back; lets breakers cool down
    after = counters()
    return {"rows": rows,
            "retries": delta(before, after, "crossfact_provider_retries_total", "reason"),
            "calls": delta(before, after, "crossfact_provider_calls_total", "result"),
            "opens": delta(before, after, "crossfact_circuit_transitions_total", "to").get("open", 0),
            "substitutions": delta(before, after, "crossfact_substitutions_total", "to_model")}


def pct(values, q):
    v = sorted(values)
    return v[min(len(v) - 1, max(0, math.ceil(q * len(v)) - 1))]


def report(sc: dict, res: dict) -> str:
    rows = res["rows"]
    durations = [r["duration"] for r in rows]
    full = sum(r["outcome"] in ("agreed", "debated", "judged", "union") for r in rows)
    outcomes = Counter(r["outcome"] for r in rows)
    reasons = Counter(x for r in rows for x in r["degraded"])
    slow = sum(d > 240 for d in durations)
    subs = ", ".join(f"to {k} {v:.0f}" for k, v in sorted(res["substitutions"].items())) or "none"
    lines = [f"### {sc['title']} (`{sc['name']}`)", "",
             f"*Hypothesis:* {sc['hypothesis']}", "",
             "| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |",
             "|---|---|---|---|---|---|---|---|",
             f"| {len(rows)} | {full} | {', '.join(f'{k} {v}' for k, v in sorted(outcomes.items()))} | "
             f"{', '.join(f'{k} {v}' for k, v in sorted(reasons.items())) or '-'} | "
             f"{statistics.median(durations):.0f} s | {pct(durations, 0.95):.0f} s | {max(durations):.0f} s | "
             f"{slow} |", "",
             f"Model calls by result: {', '.join(f'{k} {v:.0f}' for k, v in sorted(res['calls'].items()))}. "
             f"Retries: {', '.join(f'{k} {v:.0f}' for k, v in sorted(res['retries'].items())) or 'none'}. "
             f"Breaker openings: {res['opens']:.0f}. "
             f"Substitutions: {subs}.",
             ""]
    if sc["name"] in ("claude-hangs", "gemini-slow", "gemini-flash-quota", "groq-quota", "gpt-oss-everywhere-down"):
        lines += ["Run by run:", "", "| run | outcome | took | answered by |", "|---|---|---|---|"]
        for r in rows:
            who = "; ".join(f"{s} {m}" + (f" (for {sub})" if sub else "") for s, m, sub in r["answered"]) or "-"
            out = r["outcome"] + (f" ({', '.join(r['degraded'])})" if r["degraded"] else "")
            lines.append(f"| {r['n']} | {out} | {r['duration']:.0f} s | {who} |")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", action="append", help="run only these (repeatable)")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--gap", type=float, default=120, help="seconds between runs (simulated)")
    ap.add_argument("--scale", type=float, default=100)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        for sc in SCENARIOS:
            print(f"{sc['name']:22} {sc['title']}")
        return
    chosen = [s for s in SCENARIOS if not args.scenario or s["name"] in args.scenario]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = setup(tmp, args.scale)
        import app.providers as providers

        models = SimModels(args.seed)
        providers.ADAPTERS["sim"] = models
        print(f"<!-- gameday_sim.py: {args.runs} runs per scenario, {args.gap:.0f} s apart, clock x{args.scale:.0f}, "
              f"seed {args.seed} -->\n")
        for sc in chosen:
            models.rng.seed(args.seed)  # same luck in every scenario, so differences come from the faults
            models.p_agree = sc.get("p_agree", P_AGREE)
            print(report(sc, run_scenario(sc, cfg, models, args.runs, args.gap)), flush=True)


if __name__ == "__main__":
    main()
