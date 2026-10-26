"""The numbers: correct, incorrect and not attempted, always side by side.

Per system, over the questions it answered (errors are counted, not scored):
  correct, incorrect, not_attempted   rates over all graded questions
  attempted_accuracy                  correct / (correct + incorrect)
  f1                                  harmonic mean of correct rate and attempted accuracy
                                      (SimpleQA's F-score: rewards answering, punishes wrong answers)
  95% intervals                       bootstrap over questions (paired: same resamples for every system)
  latency                             median and p95, only over answers that made no cached calls
  oracle_gap ("lost")                 systems with participants (the models a vote, MoA or Crossfact
                                      asked; self-check's first answer): questions where a participant
                                      was correct and the final answer was not
  gain_rate ("found")                 the reverse: the final answer was correct and no participant was

Against a reference (the best single model by F1, unless the config names one):
  the change in correct and incorrect rates on the questions both answered, each with
  an exact McNemar test on the paired outcomes. A system that only abstains more
  lowers its incorrect rate and its correct rate together; both are shown.
"""
from __future__ import annotations

import math
import random
from statistics import median


def _rates(grades: list[str]) -> dict:
    n = len(grades)
    c, i, a = (sum(g == k for g in grades) for k in ("CORRECT", "INCORRECT", "NOT_ATTEMPTED"))
    cr = c / n if n else 0.0
    aga = c / (c + i) if (c + i) else 0.0
    f1 = 2 * cr * aga / (cr + aga) if (cr + aga) else 0.0
    return {"n": n, "correct": c, "incorrect": i, "not_attempted": a, "correct_rate": cr,
            "incorrect_rate": i / n if n else 0.0, "not_attempted_rate": a / n if n else 0.0,
            "attempted_accuracy": aga, "f1": f1}


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, max(0, math.ceil(q * len(v)) - 1))]


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts b and c."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, j) for j in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def summarize(rows: list[dict], reference: str | None = None, boot: int = 2000, seed: int = 0) -> dict:
    """rows: {system, kind, qid, grade (or None), error, latency, cached, participant_grades: list}."""
    systems: dict[str, list[dict]] = {}
    for r in rows:
        systems.setdefault(r["system"], []).append(r)
    out: dict[str, dict] = {}
    for name, rs in systems.items():
        graded = [r for r in rs if r.get("grade")]
        s = _rates([r["grade"] for r in graded])
        s["kind"] = rs[0].get("kind", "")
        s["errors"] = sum(1 for r in rs if r.get("error"))
        s["ungraded"] = sum(1 for r in rs if not r.get("error") and not r.get("grade"))
        lat = [r["latency"] for r in graded if r.get("cached", 0) == 0 and r.get("latency") is not None]
        s["latency_n"], s["latency_p50"], s["latency_p95"] = len(lat), (median(lat) if lat else None), pct(lat, 0.95)
        s["calls_per_question"] = (sum(r.get("calls", 0) for r in graded) / len(graded)) if graded else 0.0
        with_parts = [r for r in graded if r.get("participant_grades")]
        if with_parts:
            missed = sum(1 for r in with_parts if r["grade"] != "CORRECT" and "CORRECT" in r["participant_grades"])
            s["oracle_gap"] = missed / len(with_parts)
            s["gain_rate"] = sum(1 for r in with_parts if r["grade"] == "CORRECT"
                                 and "CORRECT" not in r["participant_grades"]) / len(with_parts)
            s["oracle_correct_rate"] = sum("CORRECT" in r["participant_grades"] for r in with_parts) / len(with_parts)
        out[name] = s

    # paired bootstrap: the same question resamples for every system
    qids = sorted({r["qid"] for r in rows if r.get("grade")})
    by = {name: {r["qid"]: r["grade"] for r in rs if r.get("grade")} for name, rs in systems.items()}
    rng = random.Random(seed)
    samples = {name: {"correct_rate": [], "incorrect_rate": [], "f1": []} for name in systems}
    for _ in range(boot if qids else 0):
        pick = [qids[rng.randrange(len(qids))] for _ in qids]
        for name in systems:
            gs = [by[name][q] for q in pick if q in by[name]]
            if gs:
                r = _rates(gs)
                for k in samples[name]:
                    samples[name][k].append(r[k])
    for name, ks in samples.items():
        for k, vals in ks.items():
            out[name][f"{k}_ci"] = [pct(vals, 0.025), pct(vals, 0.975)] if vals else None

    singles = [n for n, s in out.items() if s["kind"] == "single" and s["n"]]
    ref = reference if reference in out else (max(singles, key=lambda n: out[n]["f1"]) if singles else None)
    paired = {}
    if ref:
        for name in out:
            if name == ref:
                continue
            common = sorted(set(by[name]) & set(by[ref]))
            if not common:
                continue
            res = {"n": len(common), "reference": ref}
            for grade, label in (("CORRECT", "correct"), ("INCORRECT", "incorrect")):
                b = sum(1 for q in common if by[ref][q] == grade and by[name][q] != grade)
                c = sum(1 for q in common if by[name][q] == grade and by[ref][q] != grade)
                res[f"{label}_delta"] = (c - b) / len(common)
                res[f"{label}_p"] = mcnemar_exact(b, c)
                res[f"{label}_discordant"] = [b, c]
            paired[name] = res
    return {"systems": out, "reference": ref, "paired": paired, "questions": len(qids)}
