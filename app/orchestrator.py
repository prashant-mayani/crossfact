"""The consensus pipeline (docs/DESIGN.md explains every step).

  answers    both sides answer independently and in parallel; a side whose
             model fails may be answered by a configured substitute
  comparator one structured call returns the set decomposition:
             agreements (A and B), unique_a, unique_b, conflicts
  debate     up to N rounds over the conflicts only; settled points are locked
             so the conflict set can only shrink
  judge      anonymized ruling per remaining conflict; may say "unverified"
  merge      union = agreements + unique_a + unique_b (+ rulings)

Everything runs inside one time budget (pipeline.run_deadline_s). When a step
fails or the budget runs short, the run still finishes, but it is labelled
"degraded" with the reason, never passed off as a consensus.

Outcomes: agreed | debated | judged | union | single | degraded | error
(plus "abandoned", which lifecycle.py assigns to runs a dead pod left behind).
"""
from __future__ import annotations

import concurrent.futures as cf
import contextvars
import json
import random
import re
import time

from . import telemetry as tm
from .config import pipeline_setting, registry
from .providers import ask
from .resilience import Deadline, ErrorKind, ProviderError

SUCCESS = {"agreed", "debated", "judged", "union", "single"}

COMPARATOR_PROMPT = """You are a strict comparator. Two AI answers to the same question follow.
Decompose them. Respond with ONLY a JSON object, no markdown fences, no prose:
{{
 "verdict": "agree" | "partial" | "disagree",
 "agreements": ["claim both make, in one sentence", ...],
 "unique_a": ["substantive point only Answer A makes", ...],
 "unique_b": ["substantive point only Answer B makes", ...],
 "conflicts": [{{"topic": "...", "a_position": "...", "b_position": "..."}}, ...]
}}
Rules: a conflict is a genuine contradiction, not a difference in emphasis or
coverage. Style differences are never conflicts. If the question is a matter of
taste or open-ended creativity, prefer empty conflicts.

QUESTION:
{question}

ANSWER A:
{a}

ANSWER B:
{b}"""

MERGE_PROMPT = """Combine the material below into one final answer to the question.
This is a UNION: include every agreement and every unique point from both sides,
deduplicated and organized; drop nothing substantive. Write the answer directly,
with no meta-commentary about "Answer A" or "Answer B".

QUESTION:
{question}

AGREED BY BOTH:
{agreements}

ONLY IN ANSWER A:
{unique_a}

ONLY IN ANSWER B:
{unique_b}

{ruling_block}"""

DEBATE_PROMPT = """You previously answered a question. Another independent AI disagreed
with you on specific points. Reconsider ONLY the contested points below: defend
your position with reasoning or evidence, or revise it if the other position is
stronger. Then output your full updated answer to the original question.
Do not revisit the settled points; keep them as they are.

ORIGINAL QUESTION:
{question}

SETTLED (do not change):
{agreements}

CONTESTED POINTS (your position vs theirs):
{conflicts}

YOUR PREVIOUS ANSWER:
{own}"""

JUDGE_PROMPT = """You are a neutral arbiter. Two anonymous AI assistants gave final,
still-conflicting positions on specific points after debate. For each contested
point, rule which position is better supported, or rule "unverified" if neither
can be trusted without external evidence. You do not know which system produced
which answer; judge only the content. Respond with ONLY JSON:
{{
 "rulings": [{{"topic": "...", "winner": "A" | "B" | "unverified", "reason": "one or two sentences"}}],
 "analysis": "3-6 sentence overall analysis"
}}

QUESTION:
{question}

CONTESTED POINTS:
{conflicts}

POSITION A:
{a}

POSITION B:
{b}"""


# ---- helpers --------------------------------------------------------------------

def _fmt_list(items):
    return "\n".join(f"- {i}" for i in items) if items else "- (none)"


def _fmt_conflicts(conflicts, mine="a_position", theirs="b_position"):
    return "\n".join(f"- {c.get('topic', '?')}\n    yours: {c.get(mine, '')}\n    theirs: {c.get(theirs, '')}"
                     for c in conflicts) or "- (none)"


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def label(side: dict) -> str:
    return f"{side['provider']}/{side['model']}"


def _public(r: dict) -> dict:
    return {k: r[k] for k in ("text", "provider", "model", "effort", "attempts", "duration_s") if k in r}


def _bound(fn, *args, **kwargs):
    """Run in a worker thread but inside the caller's context (keeps trace parentage)."""
    ctx = contextvars.copy_context()
    return lambda: ctx.run(fn, *args, **kwargs)


def _future(f: cf.Future):
    try:
        return f.result(), None
    except ProviderError as e:
        return None, e


POLL_S = 0.25


def _gather(futures: dict[str, cf.Future], grace_s: float) -> dict[str, tuple]:
    """Wait for both sides, but once one has answered, give the other at most
    grace_s more. A side cut off this way comes back as a TIMEOUT marked
    straggler; its call keeps running in the background until its own timeout
    (bounded by the run deadline) and its result is dropped.

    Without this, one hung provider makes every user wait out its full timeout
    (game day "claude-hangs": 300-600 s for an answer the other model gave in 25 s).
    Polls with time.sleep rather than cf.wait so a swapped clock (tests, the
    game-day simulator) governs the grace period too.
    """
    pending = dict(futures)
    out: dict[str, tuple] = {}
    first_ok_at = None
    while pending:
        for side, f in list(pending.items()):
            if f.done():
                out[side] = _future(f)
                del pending[side]
                if out[side][0] is not None and first_ok_at is None:
                    first_ok_at = time.monotonic()
        if not pending:
            break
        if first_ok_at is not None and time.monotonic() - first_ok_at >= grace_s:
            for side in pending:
                err = ProviderError(f"gave up waiting: still running {grace_s:.0f}s after the other side answered",
                                    ErrorKind.TIMEOUT)
                err.straggler = True
                out[side] = (None, err)
            break
        time.sleep(POLL_S)
    return out


def _both(fn_a, fn_b, grace_s: float) -> dict[str, tuple]:
    """Run two calls in parallel; return {"A": (result, error), "B": (...)}.
    The executor is not joined: a straggler must not hold the run open."""
    ex = cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="side")
    try:
        return _gather({"A": ex.submit(fn_a), "B": ex.submit(fn_b)}, grace_s)
    finally:
        ex.shutdown(wait=False)


def _chain(primary_key: str, fallback_key: str) -> list[dict]:
    pipe = registry()["pipeline"]
    chain = [pipe.get(primary_key)] if pipe.get(primary_key) else []
    return chain + list(pipe.get(fallback_key) or [])


def _fallbacks_for(side: dict) -> list[dict]:
    table = registry()["pipeline"].get("fallbacks") or {}
    return [{**side, **f} for f in table.get(label(side), [])]


class Run:
    def __init__(self, progress, deadline: Deadline):
        self.stages: list[dict] = []
        self.reasons: list[str] = []
        self.progress = progress
        self.deadline = deadline
        self.out_of_time = False  # a step was refused for lack of run budget

    def failed(self, step_reason: str) -> str:
        """Degrade reason for a failed step: the step's own name, or "deadline" when
        the real cause was the run budget (game day "gemini-slow" showed merges
        refused for time reported as merge_failed, pointing at the wrong thing)."""
        return "deadline" if self.out_of_time else step_reason

    def record(self, stage_name: str, /, **data) -> dict:
        entry = {"stage": stage_name, "t": round(self.deadline.elapsed(), 1), **data}
        self.stages.append(entry)
        self.progress(entry)
        return entry

    def degrade(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
            tm.DEGRADED.labels(reason).inc()


# ---- model-call wrappers --------------------------------------------------------

def _answer(side: dict, prompt: str, files, deadline: Deadline, purpose: str):
    """Primary model, then configured substitutes. Returns (result, effective_side, substituted_from)."""
    last = None
    for i, cfg in enumerate([side] + _fallbacks_for(side)):
        try:
            r = ask(cfg["provider"], cfg["model"], cfg.get("effort", "medium"), prompt, files,
                    deadline=deadline, purpose=purpose)
            return r, cfg, (label(side) if i else None)
        except ProviderError as e:
            last = e
            if e.kind is ErrorKind.DEADLINE:
                break
    raise last


def _utility_json(run: Run, purpose: str, prompt: str, deadline: Deadline) -> dict | None:
    for cfg in _chain("utility", "utility_fallbacks"):
        try:
            raw = ask(cfg["provider"], cfg["model"], cfg.get("effort", "off"), prompt, deadline=deadline,
                      purpose=purpose)["text"]
            return parse_json(raw)
        except ProviderError as e:
            run.record("error", where=purpose, model=label(cfg), kind=e.kind.value, detail=e.short())
            if e.kind is ErrorKind.DEADLINE:
                run.out_of_time = True
                return None
        except (json.JSONDecodeError, ValueError) as e:
            run.record("error", where=purpose, model=label(cfg), kind="bad_json", detail=str(e)[:200])
    return None


def _compare(run: Run, question: str, a: str, b: str, deadline: Deadline) -> dict | None:
    d = _utility_json(run, "compare", COMPARATOR_PROMPT.format(question=question, a=a, b=b), deadline)
    if d is None:
        return None
    for k in ("agreements", "unique_a", "unique_b", "conflicts"):
        d.setdefault(k, [])
    d["conflicts"] = [c for c in d["conflicts"] if isinstance(c, dict)]
    d.setdefault("verdict", "disagree" if d["conflicts"] else "agree")
    return d


def _merge(run: Run, question: str, settled, comp, ruling_block: str, deadline: Deadline) -> str | None:
    prompt = MERGE_PROMPT.format(question=question, agreements=_fmt_list(settled),
                                 unique_a=_fmt_list(comp.get("unique_a", [])),
                                 unique_b=_fmt_list(comp.get("unique_b", [])), ruling_block=ruling_block)
    for cfg in _chain("utility", "utility_fallbacks"):
        try:
            return ask(cfg["provider"], cfg["model"], cfg.get("effort", "off"), prompt, deadline=deadline,
                       purpose="merge")["text"]
        except ProviderError as e:
            run.record("error", where="merge", model=label(cfg), kind=e.kind.value, detail=e.short())
            if e.kind is ErrorKind.DEADLINE:
                run.out_of_time = True
                break
    return None


def _judge(run: Run, question: str, conflicts, a: str, b: str, deadline: Deadline) -> str | None:
    flip = random.random() < 0.5  # anonymize: the judge can't learn which side is which
    first, second = (b, a) if flip else (a, b)
    prompt = JUDGE_PROMPT.format(question=question, conflicts=_fmt_conflicts(conflicts), a=first, b=second)
    for cfg in _chain("judge", "judge_fallbacks"):
        try:
            raw = ask(cfg["provider"], cfg["model"], cfg.get("effort", "high"), prompt, deadline=deadline,
                      purpose="judge")["text"]
            j = parse_json(raw)
        except ProviderError as e:
            run.record("error", where="judge", model=label(cfg), kind=e.kind.value, detail=e.short())
            if e.kind is ErrorKind.DEADLINE:
                run.out_of_time = True
                return None
            continue
        except (json.JSONDecodeError, ValueError) as e:
            run.record("error", where="judge", model=label(cfg), kind="bad_json", detail=str(e)[:200])
            continue
        rulings = [r for r in j.get("rulings", []) if isinstance(r, dict)]
        if flip:
            for r in rulings:
                r["winner"] = {"A": "B", "B": "A"}.get(r.get("winner"), r.get("winner"))
        run.record("judge", judge=label(cfg), rulings=rulings, analysis=j.get("analysis", ""))
        lines = [f"- {r.get('topic', '?')}: side {r.get('winner', '?')}, because {r.get('reason', '')}"
                 for r in rulings]
        return ("JUDGE RULINGS ON REMAINING CONFLICTS (follow these; present 'unverified' items as uncertain):\n"
                + "\n".join(lines) + f"\n\nJUDGE ANALYSIS:\n{j.get('analysis', '')}")
    return None


def _side_by_side(a_side, a_text, b_side, b_text, note: str) -> str:
    return (f"{note}\n\n**{label(a_side)}**\n\n{a_text}\n\n---\n\n**{label(b_side)}**\n\n{b_text}")


def _unresolved(conflicts) -> str:
    return ("UNRESOLVED CONFLICTS (present both positions side by side and say they are unresolved):\n"
            + _fmt_conflicts(conflicts, "a_position", "b_position"))


# ---- the pipeline ---------------------------------------------------------------------

def run_pipeline(question: str, history: str, side_a: dict, side_b: dict, mode: str = "consensus",
                 files: list[str] | None = None, progress=lambda stage: None, *,
                 deadline_s: float | None = None) -> dict:
    deadline = Deadline(deadline_s or float(pipeline_setting("run_deadline_s", 600)))
    run = Run(progress, deadline)
    with tm.span("consensus.run", **{"crossfact.mode": mode, "crossfact.side_a": label(side_a),
                                     "crossfact.side_b": label(side_b) if mode != "single" else None}) as sp:
        try:
            result = _execute(run, question, history, side_a, side_b, mode, files, deadline)
        except Exception as e:  # a bug, not a provider failure: fail this run, keep the server
            run.record("error", where="pipeline", kind="internal", detail=f"{type(e).__name__}: {e}"[:300])
            result = {"final": "The run failed because of an internal error. It has been logged.",
                      "outcome": "error"}
        outcome = result["outcome"]
        duration = deadline.elapsed()
        sp.set_attribute("crossfact.outcome", outcome)
        if run.reasons:
            sp.set_attribute("crossfact.degraded", ",".join(run.reasons))
        if outcome == "error":
            tm.mark_error(sp, "run failed")
        tm.RUNS.labels(mode, outcome).inc()
        result_class = "success" if outcome in SUCCESS else outcome
        tm.observe(tm.RUN_DURATION, duration, mode=mode, result=result_class)
        run.record("final", outcome=outcome, degraded=run.reasons, duration_s=round(duration, 1))
    return {"final": result["final"], "stages": run.stages, "outcome": outcome, "degraded": run.reasons,
            "duration_s": round(duration, 1)}


def _execute(run: Run, question, history, side_a, side_b, mode, files, deadline: Deadline) -> dict:
    q_full = f"Conversation so far:\n{history}\n\nCurrent question:\n{question}" if history else question

    if mode == "single":
        try:
            r, _, sub = _answer(side_a, q_full, files, deadline, "answer")
        except ProviderError as e:
            run.record("error", where="answer_A", side="A", kind=e.kind.value, detail=e.short())
            return {"final": f"{label(side_a)} could not answer ({e.kind.value}). Try again or pick another model.",
                    "outcome": "error"}
        run.record("answer", side="A", substituted_from=sub, **_public(r))
        if sub:
            tm.SUBSTITUTIONS.labels("A", side_a["model"], r["model"]).inc()
        return {"final": r["text"], "outcome": "single"}

    # -- independent answers, in parallel --------------------------------------------
    grace = float(pipeline_setting("straggler_grace_s", 150))
    res = _both(_bound(_answer, side_a, q_full, files, deadline, "answer"),
                _bound(_answer, side_b, q_full, files, deadline, "answer"), grace)
    sides = {"A": side_a, "B": side_b}
    texts, effective = {}, {}
    for s in ("A", "B"):
        ok, err = res[s]
        if ok:
            r, eff, sub = ok
            texts[s], effective[s] = r["text"], eff
            run.record("answer", side=s, substituted_from=sub, **_public(r))
            if sub:
                tm.SUBSTITUTIONS.labels(s, sides[s]["model"], r["model"]).inc()
        else:
            straggler = getattr(err, "straggler", False)
            run.record("error", where=f"answer_{s}", side=s, kind=err.kind.value, detail=err.short(),
                       straggler=straggler)
            if straggler:
                tm.STRAGGLERS.labels(sides[s]["provider"], sides[s]["model"], "answer").inc()
    if not texts:
        return {"final": "Neither model could answer this time (see the pipeline transcript for why). "
                         "Nothing was counted against your daily quota.", "outcome": "error"}
    if len(texts) == 1:
        alive = next(iter(texts))
        run.degrade(f"side_{'b' if alive == 'A' else 'a'}_failed")
        return {"final": texts[alive], "outcome": "degraded"}

    a_text, b_text = texts["A"], texts["B"]
    a_side, b_side = effective["A"], effective["B"]

    # -- compare -------------------------------------------------------------------------
    comp = _compare(run, question, a_text, b_text, deadline)
    if comp is None:
        run.degrade(run.failed("comparator_failed"))
        return {"final": _side_by_side(a_side, a_text, b_side, b_text,
                                       "Both models answered, but comparing them failed, so here are both "
                                       "answers in full rather than a merged one."),
                "outcome": "degraded"}
    run.record("comparator", **comp)
    settled = list(comp["agreements"])

    if mode == "union":
        block = _unresolved(comp["conflicts"]) if comp["conflicts"] else ""
        final = _merge(run, question, settled, comp, block, deadline)
        if final is None:
            run.degrade(run.failed("merge_failed"))
            return {"final": _side_by_side(a_side, a_text, b_side, b_text, "Merging failed; both answers follow."),
                    "outcome": "degraded"}
        return {"final": final, "outcome": "union"}

    # -- debate over conflicts only, inside the time budget -------------------------------
    reserve = float(pipeline_setting("reserve_judge_s", 90)) + float(pipeline_setting("reserve_merge_s", 60))
    round_cost = max(30.0, deadline.elapsed())  # the answers round is our best estimate
    rounds, max_rounds = 0, int(pipeline_setting("max_debate_rounds", 3))
    while comp["conflicts"] and rounds < max_rounds:
        if deadline.remaining() < round_cost + reserve:
            run.record("note", detail=f"Stopped debating to stay inside the {deadline.total:.0f}s budget.")
            break
        rounds += 1
        t0 = time.monotonic()
        prompt_a = DEBATE_PROMPT.format(question=question, agreements=_fmt_list(settled),
                                        conflicts=_fmt_conflicts(comp["conflicts"]), own=a_text)
        prompt_b = DEBATE_PROMPT.format(question=question, agreements=_fmt_list(settled),
                                        conflicts=_fmt_conflicts(comp["conflicts"], "b_position", "a_position"),
                                        own=b_text)
        both = _both(_bound(ask, a_side["provider"], a_side["model"], a_side.get("effort", "medium"),
                            prompt_a, deadline=deadline, purpose="debate"),
                     _bound(ask, b_side["provider"], b_side["model"], b_side.get("effort", "medium"),
                            prompt_b, deadline=deadline, purpose="debate"), grace)
        (ra, ea), (rb, eb) = both["A"], both["B"]
        if ea or eb:
            for s, e, sd in (("A", ea, a_side), ("B", eb, b_side)):
                if e:
                    straggler = getattr(e, "straggler", False)
                    run.record("error", where="debate", side=s, round=rounds, kind=e.kind.value, detail=e.short(),
                               straggler=straggler)
                    if straggler:
                        tm.STRAGGLERS.labels(sd["provider"], sd["model"], "debate").inc()
            break  # keep the last complete positions; the judge settles what's left
        a_text, b_text = ra["text"], rb["text"]
        run.record("debate", round=rounds, a=a_text, b=b_text)
        round_cost = max(round_cost, time.monotonic() - t0)
        new = _compare(run, question, a_text, b_text, deadline)
        if new is None:
            break
        comp = new
        settled += [ag for ag in comp["agreements"] if ag not in settled]
        run.record("comparator", round=rounds, **comp)

    # -- judge whatever is still contested --------------------------------------------------
    outcome, block = ("debated" if rounds else "agreed"), ""
    if comp["conflicts"]:
        if deadline.remaining() < float(pipeline_setting("reserve_merge_s", 60)) + 20:
            run.degrade("deadline")
            outcome, block = "degraded", _unresolved(comp["conflicts"])
        else:
            block = _judge(run, question, comp["conflicts"], a_text, b_text, deadline)
            if block is None:
                run.degrade(run.failed("judge_failed"))
                outcome, block = "degraded", _unresolved(comp["conflicts"])
            else:
                outcome = "judged"

    # -- union merge ---------------------------------------------------------------------------
    final = _merge(run, question, settled, comp, block, deadline)
    if final is None:
        run.degrade(run.failed("merge_failed"))
        note = ("The run used its whole time budget before the final merge; both answers follow."
                if run.out_of_time else "Merging failed; both answers follow.")
        return {"final": _side_by_side(a_side, a_text, b_side, b_text, note), "outcome": "degraded"}
    return {"final": final, "outcome": outcome}
