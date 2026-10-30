"""The consensus pipeline (docs/DESIGN.md explains every step).

  answers    both sides answer independently and in parallel; a side whose
             model fails may be answered by a configured substitute
  comparator one structured call returns the set decomposition:
             agreements (A and B), unique_a, unique_b, conflicts
  debate     up to N rounds over the conflicts only; settled points are locked
             so the conflict set can only shrink
  judge      anonymized ruling per remaining conflict; may say "unverified"
  merge      union = agreements + unique_a + unique_b (+ rulings)

Mode "auto" (the default) sends each question down the route that suits it:

  exact       arithmetic, unit conversions and date arithmetic are computed in
              code (app/exact.py) and cached: no model call, the same answer at
              once, every time
  short fact  both sides answer with the same routing note; when both mark the
              question as one short answer that doesn't change over time, two
              answers that agree are the answer. A side that says "?" (not sure)
              or fails brings in a third model, which answers on its own; its
              agreement counts only beside a "?" or a failure, never against a
              different answer. Anything else is "not sure", with what each model
              said. No comparison call, no debate.
  consensus   everything else: the full pipeline above, as in mode "consensus"

Everything runs inside one time budget (pipeline.run_deadline_s). When a step
fails or the budget runs short, the run still finishes, but it is labelled
"degraded" with the reason, never passed off as a consensus.

Outcomes: agreed | debated | judged | union | single | exact | majority | unsure |
degraded | error (plus "abandoned", which lifecycle.py assigns to runs a dead pod
left behind).
"""
from __future__ import annotations

import concurrent.futures as cf
import contextvars
import json
import random
import re
import time
import unicodedata
from fractions import Fraction

from . import exact
from . import telemetry as tm
from .config import pipeline_setting, registry
from .providers import ask, configured
from .resilience import Deadline, ErrorKind, ProviderError

SUCCESS = {"agreed", "debated", "judged", "union", "single", "exact", "majority", "unsure"}
# Every outcome a run can end with, by what ran: a mode, or the route mode "auto" chose
# ("abandoned": its pod died; lifecycle.py counts an "auto" run it closes as consensus).
OUTCOMES_BY_MODE = {
    "single": ("single", "error", "abandoned"),
    "union": ("union", "degraded", "error", "abandoned"),
    "consensus": ("agreed", "debated", "judged", "degraded", "error", "abandoned"),
    "exact": ("exact",),
    "fact": ("agreed", "majority", "unsure", "degraded"),
}
# Mode "auto": both sides get this note, and mark a short, settled fact (module docstring).
FACT_MARK = "[fact]"
FACT_NOTE = f"""

(Routing note, not part of the question. If the question has one short correct
answer, such as a name, a date, a place, a number or the result of a calculation,
and that answer does not change over time, reply in exactly two lines: the first
is {FACT_MARK}, the second only the answer, in a few words, with no explanation.
If you are not sure of the answer, put ? on the second line instead of guessing.
For anything else, including facts that can change (who holds a role now, prices,
the latest version of something, records, populations), answer normally, without
the {FACT_MARK} line.)"""

VOTE_PROMPT = """{question}

Reply with only the answer, in a few words, with no explanation. If you are not
sure of the answer, reply with exactly: ?"""

SAME_PROMPT = """Two short answers to the same question follow. Do they give the same answer:
the same person, place, thing, date or quantity, perhaps worded differently or one
more precisely than the other, without contradicting each other? Respond with ONLY
a JSON object, {{"same": true}} or {{"same": false}}.

QUESTION:
{question}

ANSWER 1: {x}
ANSWER 2: {y}"""

DEGRADE_REASONS = ("side_a_failed", "side_b_failed", "comparator_failed", "merge_failed", "judge_failed",
                   "deadline")

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
still-conflicting positions on specific points after debate. For each numbered
contested point, rule which position is better supported, or rule "unverified"
if neither can be trusted without external evidence. You do not know which
system produced which answer; judge only the content. Respond with ONLY JSON,
one ruling per point:
{{
 "rulings": [{{"point": 1, "winner": "A" | "B" | "unverified", "reason": "one or two sentences"}}],
 "analysis": "3-6 sentence overall analysis"
}}

QUESTION:
{question}

CONTESTED POINTS (what each answer says):
{conflicts}

ANSWER A IN FULL:
{a}

ANSWER B IN FULL:
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


def _gather(futures: dict[str, cf.Future], grace_s: float, grace_for=None) -> dict[str, tuple]:
    """Wait for both sides, but once one has answered, give the other at most
    grace_s more. A side cut off this way comes back as a TIMEOUT marked
    straggler; its call keeps running in the background until its own timeout
    (bounded by the run deadline) and its result is dropped.

    grace_for(side, (result, error)) -> seconds sets the grace from the first
    answer instead (mode "auto": a short fact is worth seconds of waiting, not minutes).

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
                    if grace_for:
                        grace_s = grace_for(side, out[side])
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


def _both(fn_a, fn_b, grace_s: float, grace_for=None) -> dict[str, tuple]:
    """Run two calls in parallel; return {"A": (result, error), "B": (...)}.
    The executor is not joined: a straggler must not hold the run open."""
    ex = cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="side")
    try:
        return _gather({"A": ex.submit(fn_a), "B": ex.submit(fn_b)}, grace_s, grace_for)
    finally:
        ex.shutdown(wait=False)


# ---- short facts (mode "auto") ---------------------------------------------------

# The mark at the very start, alone on its line or not, bare or in markdown (**[fact]**).
_FACT_RE = re.compile(r"\A\s*[*_`]*" + re.escape(FACT_MARK) + r"[*_`]*[ \t]*", re.IGNORECASE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_UNSURE_RE = re.compile(r"^(?:i (?:do not|don't|dont) know|not sure|unsure|unknown|no idea|"
                        r"i'?m not (?:sure|certain)|i am not (?:sure|certain)|(?:it )?cannot be determined)$")
_NUMBER_RE = re.compile(r"^[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")


def _first_line(text: str) -> str:
    rest = _THINK_RE.sub("", text or "")
    line = next((ln for ln in rest.splitlines() if ln.strip()), "")
    return line.strip().strip("*_`").strip()


def _fact_answer(text: str) -> str | None:
    """The short answer under the mark ("?" when the model wasn't sure), or None when
    the reply isn't marked (the model judged it not a short, settled fact)."""
    m = _FACT_RE.match(_THINK_RE.sub("", text or ""))
    if not m:
        return None
    return _first_line(_THINK_RE.sub("", text or "")[m.end():]) or "?"


def _key(text: str) -> str:
    """An answer reduced for comparison: case, accents, punctuation, articles and
    thousands separators don't count."""
    t = "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).lower()
    t = re.sub(r"(?<=\d),(?=\d{3}\b)|['’]", "", t)  # 1,000 is 1000; don't is dont
    t = re.sub(r"(?<!\d)\.|\.(?!\d)|[^\w\s.]", " ", t)
    t = re.sub(r"\b(?:the|a|an)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _unsure(answer: str) -> bool:
    raw = (answer or "").strip().strip("*_`").strip()
    return not raw.strip("?.! ") or bool(_UNSURE_RE.match(_key(raw)))


def _as_number(answer: str) -> Fraction | None:
    t = (answer or "").strip().rstrip(".")
    return Fraction(t.replace(",", "")) if _NUMBER_RE.match(t) else None


def _same(run: Run, question: str, x: str, y: str, deadline: Deadline) -> bool | None:
    """Whether two short answers say the same thing: by their text, as numbers, or
    else by one utility call. None when that call failed."""
    if _key(x) == _key(y):
        return True
    nx, ny = _as_number(x), _as_number(y)
    if nx is not None and ny is not None:
        return nx == ny
    d = _utility_json(run, "same", SAME_PROMPT.format(question=question, x=x, y=y), deadline)
    if not isinstance(d, dict) or not isinstance(d.get("same"), bool):
        if d is not None:
            run.record("error", where="same", kind="bad_json", detail='expected {"same": true or false}')
        return None
    return d["same"]


def _vote(run: Run, asked: str, deadline: Deadline) -> tuple[str, str] | None:
    """A third model's own short answer (it sees neither of the others), from the
    judge's chain: a family that didn't answer. (answer, model) or None."""
    chain = _chain("judge", "judge_fallbacks")
    if not chain:
        run.record("error", where="vote", kind="not_configured", detail="no judge model has its key set")
    for cfg in chain:
        try:
            r = ask(cfg["provider"], cfg["model"], cfg.get("effort", "high"), VOTE_PROMPT.format(question=asked),
                    deadline=deadline, purpose="vote")
        except ProviderError as e:
            run.record("error", where="vote", model=label(cfg), kind=e.kind.value, detail=e.short())
            if e.kind is ErrorKind.DEADLINE:
                run.out_of_time = True
                return None
            continue
        text = _fact_answer(r["text"]) or _first_line(r["text"]) or "?"
        run.record("answer", side="C", **_public({**r, "text": text}))
        return text, label(cfg)
    return None


def _shorter(x: str, y: str) -> str:
    """Of two answers that agree, the one that claims less ("1945" over "September 2, 1945")."""
    return y if len(_key(y)) < len(_key(x)) else x


NOT_SURE = "Not sure."  # the answer when the models don't agree; the note says what each said


def _short_fact(run: Run, question: str, asked: str, facts: dict, who: dict, sides: dict,
                deadline: Deadline) -> dict:
    """Mode "auto"'s short-fact route. facts: side -> short answer ("?" when unsure),
    for every side that answered; who: side -> the model that answered; asked: the
    question as the sides saw it (with the conversation), for the third model.

    The answer is one that two models gave on their own. A third model's agreement
    counts only beside a "?" or a failed side, never against a different answer
    (the plan's rule: a 2-of-3 vote counts only when the dissenter said "?").
    Everything else is "not sure", with what each model said in the route note."""
    sure = {s: a for s, a in facts.items() if not _unsure(a)}
    missing = next((s for s in ("A", "B") if s not in facts), None)

    def done(outcome: str, final: str, note: str, said: list) -> dict:
        run.record("fact", verdict=outcome, answers=[{"model": m, "answer": a} for m, a in said])
        return {"final": final, "outcome": outcome, "route": "fact", "route_note": note}

    said = [(who[s], facts[s]) for s in ("A", "B") if s in facts]
    if len(sure) == 2:
        same = _same(run, question, sure["A"], sure["B"], deadline)
        if same:
            return done("agreed", _shorter(sure["A"], sure["B"]),
                        f"Short fact: {who['A']} and {who['B']} each gave this answer on their own.", said)
        if same is None:
            run.degrade(run.failed("comparator_failed"))
            return done("degraded", f"{who['A']}: {sure['A']}\n{who['B']}: {sure['B']}",
                        "The two models' answers are worded differently, and checking whether they agree "
                        "failed, so both are shown.", said)
        return done("unsure", NOT_SURE,
                    f"{who['A']}: {sure['A']} · {who['B']}: {sure['B']}. No source settled it.", said)
    if not sure:
        if missing:
            run.record("note", detail=f"Side {missing} failed, and the side that answered wasn't sure.")
        return done("unsure", NOT_SURE,
                    " · ".join(f"{m}: not sure" for m, _ in said) + ".", said)
    # One sure answer; the other side said "?" or failed. A third model answers on its own.
    (s, x), = sure.items()
    vote = _vote(run, asked, deadline)
    if vote is None:
        if missing:
            run.degrade(run.failed(f"side_{missing.lower()}_failed"))
            return done("degraded", x, f"Only {who[s]} answered, and no third model could check it.", said)
        run.degrade(run.failed("judge_failed"))
        return done("degraded", NOT_SURE,
                    f"{who[s]}: {x} · {who[_other(s)]}: not sure; the third model failed.", said)
    z, zwho = vote
    said.append((zwho, z))
    if not _unsure(z):
        same = _same(run, question, x, z, deadline)
        if same is None:
            run.degrade(run.failed("comparator_failed"))
            return done("degraded", NOT_SURE, f"{who[s]}: {x} · {zwho}: {z}; checking whether they agree failed.",
                        said)
        if same:
            if missing:
                run.record("note", detail=f"Side {missing} failed, so a third model checked side {s}'s answer.")
                return done("agreed", _shorter(x, z),
                            f"Short fact: {who[s]} and {zwho} each gave this answer on their own "
                            f"({label(sides[missing])} failed).", said)
            return done("majority", _shorter(x, z),
                        f"Short fact: {who[s]} and {zwho} each gave this answer on their own; "
                        f"{who[_other(s)]} wasn't sure.", said)
    others = " · ".join(f"{m}: {'not sure' if _unsure(a) else a}" for m, a in said)
    return done("unsure", NOT_SURE, f"{others}.", said)


def _other(side: str) -> str:
    return "B" if side == "A" else "A"


def _fact_grace(fact_s: float, default_s: float):
    """grace_for in mode "auto": once a side has answered with the short-fact mark,
    wait fact_s (not minutes) for the other; the third model covers for a slow side."""
    def grace_for(side: str, outcome: tuple) -> float:
        result, _ = outcome
        return fact_s if result and _fact_answer(result[0]["text"]) is not None else default_s
    return grace_for


def _chain(primary_key: str, fallback_key: str) -> list[dict]:
    """The configured models for a pipeline role, in order (unconfigured ones skipped)."""
    pipe = registry()["pipeline"]
    chain = ([pipe.get(primary_key)] if pipe.get(primary_key) else []) + list(pipe.get(fallback_key) or [])
    return [c for c in chain if configured(c.get("provider", ""))]


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
    chain = _chain("utility", "utility_fallbacks")
    if not chain:
        run.record("error", where=purpose, kind="not_configured", detail="no utility model has its key set")
    for cfg in chain:
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
    chain = _chain("utility", "utility_fallbacks")
    if not chain:
        run.record("error", where="merge", kind="not_configured", detail="no utility model has its key set")
    for cfg in chain:
        try:
            return ask(cfg["provider"], cfg["model"], cfg.get("effort", "off"), prompt, deadline=deadline,
                       purpose="merge")["text"]
        except ProviderError as e:
            run.record("error", where="merge", model=label(cfg), kind=e.kind.value, detail=e.short())
            if e.kind is ErrorKind.DEADLINE:
                run.out_of_time = True
                break
    return None


def _fmt_judge_points(conflicts, flip: bool) -> str:
    """Numbered points with each answer's position, labelled the way the judge sees the
    answers: when they are swapped, so are the positions (or the points would credit A
    with B's claim)."""
    first, second = ("b_position", "a_position") if flip else ("a_position", "b_position")
    return "\n".join(f"{i}. {c.get('topic', '?')}\n   Answer A says: {c.get(first, '')}\n"
                     f"   Answer B says: {c.get(second, '')}" for i, c in enumerate(conflicts, 1)) or "(none)"


def _match_rulings(raw_rulings, conflicts) -> list[dict]:
    """One ruling per conflict, in conflict order. Matched by point number, then by
    topic, then by position; a point the judge skipped is "unverified"."""
    by_point, by_topic, loose = {}, {}, []
    for r in raw_rulings:
        try:
            n = int(r.get("point"))
        except (TypeError, ValueError):
            n = None
        if n is not None and 1 <= n <= len(conflicts) and n not in by_point:
            by_point[n] = r
        elif str(r.get("topic", "")).strip().lower():
            by_topic.setdefault(str(r.get("topic")).strip().lower(), r)
        else:
            loose.append(r)
    out = []
    for i, c in enumerate(conflicts, 1):
        r = by_point.get(i) or by_topic.get(str(c.get("topic", "")).strip().lower())
        if r is None and loose:
            r = loose.pop(0)
        r = dict(r or {"winner": "unverified", "reason": "the judge gave no ruling on this point"})
        if r.get("winner") not in ("A", "B", "unverified"):
            r["winner"] = "unverified"
        r["topic"] = c.get("topic", "?")
        r.pop("point", None)
        out.append(r)
    return out


def _ruling_block(conflicts, rulings, analysis: str) -> str:
    """What the merge needs to apply each ruling: the winning position itself, not
    just a side letter it has no way to look up."""
    lines = []
    for c, r in zip(conflicts, rulings, strict=True):
        if r["winner"] == "A":
            lines.append(f"- {c.get('topic', '?')}: {c.get('a_position', '')} (ruled correct: {r.get('reason', '')})")
        elif r["winner"] == "B":
            lines.append(f"- {c.get('topic', '?')}: {c.get('b_position', '')} (ruled correct: {r.get('reason', '')})")
        else:
            lines.append(f"- {c.get('topic', '?')}: UNVERIFIED. One answer says: {c.get('a_position', '')}; "
                         f"the other says: {c.get('b_position', '')} ({r.get('reason', '')})")
    return ("JUDGE RULINGS ON REMAINING CONFLICTS (state the ruled-correct position; present UNVERIFIED items "
            "as uncertain, with both positions):\n" + "\n".join(lines) + f"\n\nJUDGE ANALYSIS:\n{analysis}")


def _judge(run: Run, question: str, conflicts, a: str, b: str, deadline: Deadline) -> str | None:
    flip = random.random() < 0.5  # anonymize: the judge can't learn which side is which
    first, second = (b, a) if flip else (a, b)
    prompt = JUDGE_PROMPT.format(question=question, conflicts=_fmt_judge_points(conflicts, flip),
                                 a=first, b=second)
    chain = _chain("judge", "judge_fallbacks")
    if not chain:
        run.record("error", where="judge", kind="not_configured", detail="no judge model has its key set")
    for cfg in chain:
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
        if not isinstance(j, dict):
            run.record("error", where="judge", model=label(cfg), kind="bad_json", detail="not a JSON object")
            continue
        rulings = _match_rulings([r for r in j.get("rulings", []) if isinstance(r, dict)], conflicts)
        if flip:  # the judge's "A" was the answer shown first, which was B's
            for r in rulings:
                r["winner"] = {"A": "B", "B": "A"}.get(r["winner"], r["winner"])
        run.record("judge", judge=label(cfg), rulings=rulings, analysis=j.get("analysis", ""), shown_swapped=flip)
        return _ruling_block(conflicts, rulings, j.get("analysis", ""))
    return None


def _side_by_side(a_side, a_text, b_side, b_text, note: str) -> str:
    return (f"{note}\n\n**{label(a_side)}**\n\n{a_text}\n\n---\n\n**{label(b_side)}**\n\n{b_text}")


def _unresolved(conflicts) -> str:
    return ("UNRESOLVED CONFLICTS (present both positions side by side and say they are unresolved):\n"
            + _fmt_conflicts(conflicts, "a_position", "b_position"))


# ---- metrics -----------------------------------------------------------------------------

def prime_metrics() -> None:
    """Create this module's label combinations at zero, before anything happens.

    Prometheus's increase() and rate() measure change between samples, so a series
    whose first sample is already 1 loses that first event: the first degraded run
    would never reach the SLO, the first fallback never reach a dashboard."""
    for mode, outcomes in OUTCOMES_BY_MODE.items():
        for outcome in outcomes:
            tm.RUNS.labels(mode, outcome)
        for result in {"success" if o in SUCCESS else o for o in outcomes}:
            tm.RUN_DURATION.labels(mode, result)
    for reason in DEGRADE_REASONS:
        tm.DEGRADED.labels(reason)
    for key, subs in (registry()["pipeline"].get("fallbacks") or {}).items():
        model = key.split("/", 1)[-1]
        for sub in subs:
            for side in ("A", "B"):
                tm.SUBSTITUTIONS.labels(side, model, sub.get("model", model))


# ---- the pipeline ---------------------------------------------------------------------

def run_pipeline(question: str, history: str, side_a: dict, side_b: dict, mode: str = "consensus",
                 files: list[str] | None = None, progress=lambda stage: None, *,
                 deadline_s: float | None = None, instruction: str = "") -> dict:
    """instruction: text for the models after the question ("reply with only the
    answer", from the benchmark); routing reads the question without it."""
    deadline = Deadline(deadline_s or float(pipeline_setting("run_deadline_s", 600)))
    run = Run(progress, deadline)
    with tm.span("consensus.run", **{"crossfact.mode": mode, "crossfact.side_a": label(side_a),
                                     "crossfact.side_b": label(side_b) if mode != "single" else None}) as sp:
        try:
            result = _execute(run, question, instruction, history, side_a, side_b, mode, files, deadline)
        except Exception as e:  # a bug, not a provider failure: fail this run, keep the server
            run.record("error", where="pipeline", kind="internal", detail=f"{type(e).__name__}: {e}"[:300])
            result = {"final": "The run failed because of an internal error. It has been logged.",
                      "outcome": "error"}
        outcome = result["outcome"]
        route = result.get("route") or ("consensus" if mode == "auto" else None)
        duration = deadline.elapsed()
        sp.set_attribute("crossfact.outcome", outcome)
        if route:
            sp.set_attribute("crossfact.route", route)
        if run.reasons:
            sp.set_attribute("crossfact.degraded", ",".join(run.reasons))
        if outcome == "error":
            tm.mark_error(sp, "run failed")
        # Metrics count what actually ran: mode "auto" counts as the route it took,
        # so the consensus SLO still measures consensus runs only.
        metric_mode = route or mode
        tm.RUNS.labels(metric_mode, outcome).inc()
        result_class = "success" if outcome in SUCCESS else outcome
        tm.observe(tm.RUN_DURATION, duration, mode=metric_mode, result=result_class)
        run.record("final", outcome=outcome, degraded=run.reasons, duration_s=round(duration, 3 if duration < 1 else 1))
    return {"final": result["final"], "stages": run.stages, "outcome": outcome, "degraded": run.reasons,
            "duration_s": round(duration, 3 if duration < 1 else 1), "route": route,
            "route_note": result.get("route_note")}


def _execute(run: Run, question, instruction, history, side_a, side_b, mode, files, deadline: Deadline) -> dict:
    if mode == "auto":
        hit = exact.solve(question)  # the question alone: a computation doesn't depend on the conversation
        if hit:
            run.record("exact", kind=hit.kind, working=hit.working)
            return {"final": hit.answer, "outcome": "exact", "route": "exact",
                    "route_note": f"Computed in code, not by a model: {hit.working}"}
    if instruction:
        question = f"{question}\n\n{instruction}"
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
    auto = mode == "auto"
    note = FACT_NOTE if auto else ""
    res = _both(_bound(_answer, side_a, q_full + note, files, deadline, "answer"),
                _bound(_answer, side_b, q_full + note, files, deadline, "answer"), grace,
                _fact_grace(float(pipeline_setting("fact_grace_s", 30)), grace) if auto else None)
    sides = {"A": side_a, "B": side_b}
    texts, effective, facts = {}, {}, {}
    for s in ("A", "B"):
        ok, err = res[s]
        if ok:
            r, eff, sub = ok
            fact = _fact_answer(r["text"]) if auto else None
            if fact is not None:  # a short answer under the mark: keep the answer, not the mark
                facts[s] = fact
            texts[s] = r["text"] if fact is None else ("I don't know." if _unsure(fact) else fact)
            effective[s] = eff
            run.record("answer", side=s, substituted_from=sub, **_public({**r, "text": fact or r["text"]}))
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
    if facts and len(facts) == len(texts):  # every side that answered marked it a short fact
        return _short_fact(run, question, q_full, facts, {s: label(effective[s]) for s in facts}, sides,
                           deadline)
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
