"""Grading: correct, incorrect, or not attempted.

The prompt is OpenAI's SimpleQA grader template (simple-evals), fetched at a
pinned commit and checked against a pinned sha256, then read out of the Python
source as data (parsed, never executed). Added before its final example, in our
own words, are the grading changes SimpleQA Verified made (Haas et al., 2025):
a numeric gold target states its acceptable range; only the direct answer is
graded; a response that lists candidates without committing to one is not
attempted; and declining in any phrasing is not attempted.

SimpleQA Verified's own autorater is a paid model (gpt-4.1-2025-04-14). These
numbers come from a free grader from a model family that answers nothing in the
comparison, so they compare systems with each other, graded identically; they
are not leaderboard numbers. `python -m bench grade-check` measures how often
two graders agree.

Two cases skip the model: a normalized answer equal to the normalized gold
target is correct, and a bare refusal ("I don't know") is not attempted.
Grades are cached per (grader, question, normalized answer).
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import unicodedata

SIMPLE_EVALS_COMMIT = "652c89d0ca9df547706735883097e9537d40dc47"
SIMPLE_EVALS_FILE_SHA256 = "d61a8f97132b29f6fe5d264dd9d619407ea14421525827fabd4cc21162e35c29"
TEMPLATE_URL = f"https://raw.githubusercontent.com/openai/simple-evals/{SIMPLE_EVALS_COMMIT}/simpleqa_eval.py"
MARKER = "Here is a new example."

VERIFIED_RULES = """Four more rules, which take precedence over the examples and notes above:
- Numbers. When the gold target states an acceptable range (for example, 8282 with an acceptable
  range of 8199 to 8365), a predicted number inside the range is CORRECT and one outside it is
  INCORRECT; the note about the last significant figure does not apply. Without a stated range,
  that note applies.
- Grade only the part of the response that directly answers the question. Ignore any additional
  information the response gives.
- A response that names several candidate answers must commit to one of them as the most likely.
  If it does, grade that one. If it does not, the response is NOT_ATTEMPTED (this replaces the
  example above that calls such a response INCORRECT).
- A response that declines to answer, in any phrasing ("I don't know", "I can't verify that",
  "I'm not certain, so I won't guess"), without committing to an answer is NOT_ATTEMPTED.


"""

GRADES = {"A": "CORRECT", "B": "INCORRECT", "C": "NOT_ATTEMPTED"}
DECLINE = re.compile(
    r"^(i (do not|don't|dont) know|i'?m not (sure|certain)|i am not (sure|certain)|not sure|unknown|no idea|"
    r"i (cannot|can't|cant) (answer|say|verify|determine|be sure)|(this )?cannot be determined|"
    r"i (do not|don't) have (enough )?information)[.!]?$")


class GraderError(RuntimeError):
    pass


def extract_template(source: str) -> str:
    """GRADER_TEMPLATE from simpleqa_eval.py, read as data (it is `\"\"\"...\"\"\".strip()`)."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "GRADER_TEMPLATE" for t in node.targets):
            v = node.value
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "strip" \
                    and isinstance(v.func.value, ast.Constant) and isinstance(v.func.value.value, str):
                return v.func.value.value.strip()
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                return v.value
    raise GraderError("GRADER_TEMPLATE not found in simpleqa_eval.py")


def load_template(cache_dir: str, local_file: str | None = None) -> tuple[str, str]:
    """(template with the Verified rules, sha256 of the template text)."""
    if local_file:
        with open(local_file, "rb") as f:
            data = f.read()
    else:
        path = os.path.join(cache_dir, f"simpleqa_eval-{SIMPLE_EVALS_COMMIT[:12]}.py")
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
        else:
            import httpx

            r = httpx.get(TEMPLATE_URL, timeout=60, follow_redirects=True)
            r.raise_for_status()
            data = r.content
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        if hashlib.sha256(data).hexdigest() != SIMPLE_EVALS_FILE_SHA256:
            raise GraderError(f"simpleqa_eval.py at {SIMPLE_EVALS_COMMIT[:12]} doesn't match its pinned sha256")
    template = extract_template(data.decode("utf-8"))
    for placeholder in ("{question}", "{target}", "{predicted_answer}", MARKER):
        if placeholder not in template:
            raise GraderError(f"the grader template has no {placeholder!r}: the format changed")
    template = template.replace(MARKER, VERIFIED_RULES + MARKER, 1)
    return template, hashlib.sha256(template.encode()).hexdigest()


QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


def normalize(text: str | None) -> str:
    t = unicodedata.normalize("NFKC", (text or "")).translate(QUOTES).strip().lower()
    t = t.strip("\"'`*_ ").rstrip(".!")
    t = re.sub(r"\s+", " ", t)
    return t


NUMBER = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")


def _number(text: str) -> float | None:
    t = text.replace(" ", "")
    return float(t.replace(",", "")) if NUMBER.match(t) else None


def answer_key(text: str | None) -> str:
    """What two answers must share to count as the same answer in a vote: the
    normalized text without a leading article, or, for a number, its value.
    Deliberately literal: "Shakespeare" and "William Shakespeare" are different
    answers here, which undercounts agreement (docs/EVAL.md)."""
    t = re.sub(r"^(the|a|an) ", "", normalize(text))
    n = _number(t)
    return repr(n) if n is not None else t


def auto_grade(answer: str | None, gold: str) -> tuple[str, str] | None:
    a, g = normalize(answer), normalize(gold)
    if not a or DECLINE.match(a):
        return "NOT_ATTEMPTED", "auto_decline"
    if a == g:
        return "CORRECT", "auto_exact"
    na, ng = _number(a), _number(g)
    if na is not None and ng is not None and na == ng:  # "17,532" and "17532"; anything else goes to the grader
        return "CORRECT", "auto_exact"
    return None


def parse(text: str) -> str | None:
    t = (text or "").strip().upper()
    m = re.match(r"^\W*([ABC])\b", t) or re.search(r"\b([ABC])\W*$", t)
    if m:
        return GRADES[m.group(1)]
    for word, grade in (("NOT_ATTEMPTED", "NOT_ATTEMPTED"), ("NOT ATTEMPTED", "NOT_ATTEMPTED"),
                        ("INCORRECT", "INCORRECT"), ("CORRECT", "CORRECT")):
        if word in t:
            return grade
    return None


class Grader:
    def __init__(self, calls, spec: dict, template: str, template_sha: str):
        from .systems import model_spec

        self.calls = calls
        self.provider, self.model, self.effort = model_spec(spec["model"], spec.get("effort", "off"))
        self.template, self.template_sha = template, template_sha
        self.id = f"{self.provider}/{self.model}@{self.effort}#{template_sha[:12]}"

    def key(self, qid: str, answer: str | None) -> str:
        return hashlib.sha256(f"{self.id}|{qid}|{normalize(answer)}".encode()).hexdigest()

    def prompt(self, question: str, gold: str, answer: str) -> str:
        return (self.template.replace("{question}", question).replace("{target}", gold)
                .replace("{predicted_answer}", answer))

    def grade(self, store, qid: str, question: str, gold: str, answer: str | None) -> dict:
        key = self.key(qid, answer)
        hit = store.grade(key)
        if hit:
            return hit
        auto = auto_grade(answer, gold)
        if auto:
            grade, how, raw = auto[0], auto[1], None
        else:
            prompt = self.prompt(question, gold, answer or "")
            raw = self.calls.ask(self.provider, self.model, self.effort, prompt, purpose="grade")["text"]
            grade, how = parse(raw), "model"
            if grade is None:  # one retry with the instruction repeated last
                again = prompt + "\n\nReply with one letter: A, B or C."
                raw = self.calls.ask(self.provider, self.model, self.effort, again, purpose="grade")["text"]
                grade = parse(raw)
            if grade is None:  # not cached as an answer: the next run asks afresh
                for p in (prompt, again):
                    self.calls.forget(self.provider, self.model, self.effort, p)
                raise GraderError(f"the grader's reply isn't A, B or C: {raw[:80]!r}")
        store.save_grade(key, grade, how, self.id, raw)
        return {"grade": grade, "how": how, "grader": self.id, "raw": raw}
