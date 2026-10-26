"""The systems under test. All answer closed-book (no search) and get the same
instruction: give only the answer, or say "I don't know" when not confident.

  single     one model, one call
  selfcheck  the same model, then a second call asking it to check its own
             answer and give a final one. As many calls as a second opinion,
             from the same model: does Crossfact's gain come from a second model,
             or just from a second look? (Without outside feedback, models'
             self-correction lowered accuracy on reasoning benchmarks: Huang et
             al., ICLR 2024.)
  vote       several models answer; an answer that more than half of them gave
             (after normalizing) wins, otherwise "I don't know". Agree-or-abstain.
             With samples: n and one model, it is self-consistency: the same
             model asked n times, a baseline multi-agent debate often fails
             to beat (Zhang et al., 2025)
  moa        Mixture-of-Agents, one layer: several models answer, an aggregator
             reads them all and writes the final answer (Wang et al., 2024)
  crossfact  the app's own pipeline (consensus mode), through the same call path.
             A run in which a provider call failed (a timeout, a server error)
             is not scored: like a baseline's failed call, it is recorded as an
             error and tried again on a later run, with the calls that succeeded
             served from the cache. What the method itself gets wrong (a judge
             reply that isn't JSON) is scored.

Model specs are "provider/model" (the model may itself contain slashes, as in
groq/openai/gpt-oss-120b), optionally "@effort".
"""
from __future__ import annotations

import re
import time
from collections import Counter

from app.resilience import ErrorKind

from .grader import answer_key, normalize

DEFAULT_INSTRUCTION = ("Reply with only the answer, with no explanation. If you are not confident that your answer "
                       "is correct, reply with exactly: I don't know")
PROMPT = "{question}\n\n{instruction}"
SELFCHECK = """Question: {question}

Your earlier answer: {answer}

Check that answer for errors: is it actually right, and does it answer exactly what was asked?
Then give your final answer.
{instruction}"""
AGGREGATE = """Several AI models answered the same question independently. Their answers may be wrong,
and they may disagree; do not assume the majority is right.

Question: {question}

{answers}

Using these answers and your own knowledge, give the single best final answer to the question.
{instruction}"""


def model_spec(spec: str | dict, effort: str = "medium") -> tuple[str, str, str]:
    if isinstance(spec, dict):
        return spec["provider"], spec["model"], spec.get("effort", effort)
    if "@" in spec.rsplit("/", 1)[-1]:
        spec, effort = spec.rsplit("@", 1)
    provider, _, model = spec.partition("/")
    if not model:
        raise ValueError(f"model spec {spec!r} should be provider/model")
    return provider, model, effort


def _short(text: str) -> str:
    """The answer line of a reply: models sometimes add a sentence anyway."""
    t = (text or "").strip()
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S).strip()
    return t


class System:
    kind = ""

    def __init__(self, name: str, spec: dict, calls, instruction: str, effort: str):
        self.name, self.spec, self.calls = name, spec, calls
        self.instruction, self.effort = instruction, spec.get("effort", effort)

    def ask(self, model: str | dict, prompt: str, purpose: str = "answer", sample: int = 0) -> str:
        provider, m, effort = model_spec(model, self.effort)
        return _short(self.calls.ask(provider, m, effort, prompt, purpose=purpose, sample=sample)["text"])

    def first(self, model, question: str, sample: int = 0) -> str:
        return self.ask(model, PROMPT.format(question=question, instruction=self.instruction), sample=sample)

    def run(self, question: str) -> tuple[str, dict]:
        raise NotImplementedError


class Single(System):
    kind = "single"

    def run(self, question):
        return self.first(self.spec["model"], question), {"participants": {self.spec["model"]: None}}


class SelfCheck(System):
    kind = "selfcheck"

    def run(self, question):
        m = self.spec["model"]
        first = self.first(m, question)
        final = self.ask(m, SELFCHECK.format(question=question, answer=first, instruction=self.instruction),
                         purpose="selfcheck")
        return final, {"participants": {"first": first}, "changed": normalize(first) != normalize(final)}


class Vote(System):
    kind = "vote"

    def run(self, question):
        draws = int(self.spec.get("samples", 1))  # draw 0 is the one every other system shares
        answers = {(m if i == 0 else f"{m}#{i + 1}"): self.first(m, question, sample=i)
                   for m in self.spec["models"] for i in range(draws)}
        counted = Counter(answer_key(a) for a in answers.values() if a.strip() and not _declines(a))
        need = (len(answers) // 2) + 1
        winner = next((k for k, n in counted.most_common() if n >= need), None)
        final = next(a for a in answers.values() if answer_key(a) == winner) if winner else "I don't know"
        return final, {"participants": answers, "agreeing": counted.most_common(1)[0][1] if counted else 0}


class MixtureOfAgents(System):
    kind = "moa"

    def run(self, question):
        answers = {m: self.first(m, question) for m in self.spec["models"]}
        listed = "\n".join(f"Answer {i}: {a}" for i, a in enumerate(answers.values(), 1))
        final = self.ask(self.spec["aggregator"],
                         AGGREGATE.format(question=question, answers=listed, instruction=self.instruction),
                         purpose="aggregate")
        return final, {"participants": answers}


class ProviderTrouble(RuntimeError):
    """Crossfact's run was degraded by provider trouble: not an answer, try again later."""


class Misconfigured(RuntimeError):
    """Every run would fail the same way (a pipeline model has no key): stop."""


INFRA = {k.value for k in ErrorKind}  # what a provider call can fail with


class Crossfact(System):
    """The app's pipeline, unchanged, with its model calls going through the
    harness (cache, pacing and budgets). The question carries the same instruction.

    The pipeline looks its call function up as a module global, so swapping
    orchestrator.ask reaches every stage, including the two answers it asks for
    in parallel threads."""

    kind = "crossfact"

    def run(self, question):
        from app import orchestrator

        a = model_spec(self.spec["side_a"], self.effort)
        b = model_spec(self.spec["side_b"], self.effort)
        side_a = {"provider": a[0], "model": a[1], "effort": a[2]}
        side_b = {"provider": b[0], "model": b[1], "effort": b[2]}
        real = orchestrator.ask
        orchestrator.ask = self.calls.ask
        try:
            out = orchestrator.run_pipeline(PROMPT.format(question=question, instruction=self.instruction), "",
                                            side_a, side_b, self.spec.get("mode", "consensus"))
        finally:
            orchestrator.ask = real
        errors = [s for s in out["stages"] if s["stage"] == "error"]
        missing = next((s for s in errors if s.get("kind") == "not_configured"), None)
        if missing:
            raise Misconfigured(f"Crossfact's {missing.get('where')} model has no key ({missing.get('detail', '')})")
        internal = next((s for s in errors if s.get("kind") == "internal"), None)
        if internal:
            raise RuntimeError(f"the pipeline failed: {internal.get('detail', '')}")
        # Any provider failure changed this run's path (a side missing, a debate cut short, a fallback
        # answering), which a retry would not: like a baseline's failed call, it isn't scored.
        trouble = [s for s in errors if s.get("kind") in INFRA]
        if trouble or "deadline" in out["degraded"]:
            t = trouble[0] if trouble else {"where": "run", "detail": "it ran out of its time budget"}
            raise ProviderTrouble(f"{t.get('where')}: {t.get('detail') or t.get('kind')}"
                                  + (f" (+{len(trouble) - 1} more)" if len(trouble) > 1 else ""))
        answers = {f"{s['side']}:{s['provider']}/{s['model']}": _short(s.get("text", ""))
                   for s in out["stages"] if s["stage"] == "answer"}
        judge = next((s.get("rulings") for s in out["stages"] if s["stage"] == "judge"), None)
        rounds = sum(1 for s in out["stages"] if s["stage"] == "debate")
        return _short(out["final"]), {"outcome": out["outcome"], "degraded": out["degraded"], "participants": answers,
                                      "judge": judge, "debate_rounds": rounds,
                                      "method_errors": [s.get("kind") for s in errors] or None}


def _declines(answer: str) -> bool:
    from .grader import DECLINE

    return bool(DECLINE.match(normalize(answer)))


KINDS = {c.kind: c for c in (Single, SelfCheck, Vote, MixtureOfAgents, Crossfact)}


def build(spec: dict, calls, instruction: str, effort: str) -> System:
    kind = spec.get("kind")
    if kind not in KINDS:
        raise ValueError(f"system {spec.get('name')!r}: kind must be one of {sorted(KINDS)}")
    models = spec.get("models") or []
    if len({model_spec(m, effort) for m in models}) != len(models):
        # the cache returns one answer per model and prompt, so a repeated model adds a copy, not a sample
        raise ValueError(f"system {spec['name']!r}: list each model once (for several draws of one model, "
                         "use samples)")
    return KINDS[kind](spec["name"], spec, calls, instruction, effort)


def timed(system: System, question: str) -> dict:
    """Answer one question; latency excludes time spent waiting out rate limits."""
    c = system.calls
    hits0, real0, waited0 = c.hits, c.real, c.waited_s
    t0 = time.monotonic()
    answer, detail = system.run(question)
    return {"answer": answer, "detail": detail, "latency": max(0.0, time.monotonic() - t0 - (c.waited_s - waited0)),
            "calls": (c.hits - hits0) + (c.real - real0), "cached": c.hits - hits0}
