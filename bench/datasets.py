"""Question sets.

  simpleqa_verified  SimpleQA Verified (Haas et al., Google DeepMind and Google
                     Research, 2025): 1,000 short factual questions answered from
                     a model's own knowledge, each with one verified answer; a
                     cleaned-up, rebalanced, harder subset of OpenAI's SimpleQA.
                     Downloaded from Google's Hugging Face copy. The first
                     download pins its sha256 in bench/datasets.lock.json (commit
                     that file); every later download must match, so a silently
                     changed dataset can't change the numbers.
  <path>.yaml/.csv   your own: rows with id, question, answer, and optionally topic.
                     bench/sets/exact.yaml is one: questions with exact answers.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import random
from dataclasses import dataclass, field

import yaml

SIMPLEQA_VERIFIED_URL = ("https://huggingface.co/datasets/google/simpleqa-verified/resolve/main/"
                         "simpleqa_verified.csv")
HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGED_LOCK = os.path.join(HERE, "datasets.lock.json")
# Where pins are written. In the cluster the image is read-only, so the nightly job
# points this at its volume; pins committed with the code are read from PACKAGED_LOCK.
LOCK = os.environ.get("CROSSFACT_BENCH_LOCK", PACKAGED_LOCK)


@dataclass
class Question:
    qid: str
    question: str
    answer: str
    topic: str = ""
    meta: dict = field(default_factory=dict)


class DatasetError(RuntimeError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch(url: str) -> bytes:
    import httpx

    r = httpx.get(url, timeout=120, follow_redirects=True)
    r.raise_for_status()
    return r.content


def _pinned(name: str, data: bytes, url: str, lock_path: str, repin: bool) -> str:
    sha = _sha(data)
    lock = {}
    for path in (lock_path, PACKAGED_LOCK):  # the writable lock, else the pins shipped with the code
        if os.path.exists(path):
            with open(path) as f:
                lock = json.load(f)
            break
    want = (lock.get(name) or {}).get("sha256")
    if want and want != sha and not repin:
        raise DatasetError(f"{name} changed: {url} now has sha256 {sha}, the lock file pins {want}. "
                           "Find out why before re-pinning with --repin (results across versions don't compare).")
    if want != sha:
        lock[name] = {"url": url, "sha256": sha}
        try:
            with open(lock_path, "w") as f:
                json.dump(lock, f, indent=2, sort_keys=True)
                f.write("\n")
        except OSError:  # a read-only image: the run's metadata still records the hash
            pass
    return sha


def simpleqa_verified(cache_dir: str, lock_path: str = LOCK, repin: bool = False,
                      url: str = SIMPLEQA_VERIFIED_URL) -> tuple[list[Question], str]:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "simpleqa_verified.csv")
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
    else:
        data = _fetch(url)
    sha = _pinned("simpleqa_verified", data, url, lock_path, repin)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
    missing = {"original_index", "problem", "answer"} - set(rows[0] if rows else {})
    if missing:
        raise DatasetError(f"simpleqa_verified.csv lacks columns {sorted(missing)}")
    qs = [Question(qid=str(r["original_index"]), question=r["problem"], answer=r["answer"],
                   topic=r.get("topic", ""),
                   meta={k: r[k] for k in ("answer_type", "multi_step", "requires_reasoning") if k in r})
          for r in rows]
    return qs, sha


def custom(path: str) -> tuple[list[Question], str]:
    with open(path, "rb") as f:
        data = f.read()
    if path.endswith((".yaml", ".yml")):
        rows = yaml.safe_load(data)
        rows = rows.get("questions", rows) if isinstance(rows, dict) else rows
    else:
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
    qs = []
    for i, r in enumerate(rows):
        if not r.get("question") or r.get("answer") in (None, ""):
            raise DatasetError(f"{path}: row {i + 1} needs a question and an answer")
        qs.append(Question(qid=str(r.get("id", i + 1)), question=str(r["question"]), answer=str(r["answer"]),
                           topic=str(r.get("topic", ""))))
    if len({q.qid for q in qs}) != len(qs):
        raise DatasetError(f"{path}: ids must be unique")
    return qs, _sha(data)


def load(name: str, cache_dir: str, repin: bool = False) -> tuple[list[Question], str]:
    if name == "simpleqa_verified":
        return simpleqa_verified(cache_dir, repin=repin)
    path = name if os.path.isabs(name) or os.path.exists(name) else os.path.join(HERE, "sets", name)
    if not os.path.exists(path):
        raise DatasetError(f"no dataset {name!r} (simpleqa_verified, or a .yaml/.csv path)")
    return custom(path)


def sample(questions: list[Question], limit: int | None, seed: int = 1,
           stratify: str | None = "topic") -> list[Question]:
    """A deterministic subset of `limit` questions, in proportion to each stratum's
    size (largest remainder), so a 200-question run has the full set's topic mix.
    Each stratum is shuffled once per seed and cut, so a smaller sample is (almost
    always) part of a larger one: a quick 50-question look is reused by the 200 run."""
    if not limit or limit >= len(questions):
        return list(questions)
    rng = random.Random(seed)
    if not stratify:
        order = questions[:]
        rng.shuffle(order)
        picked = order[:limit]
    else:
        groups: dict[str, list[Question]] = {}
        for q in questions:
            groups.setdefault(getattr(q, stratify, "") or "", []).append(q)
        quotas = {k: limit * len(v) / len(questions) for k, v in groups.items()}
        take = {k: int(x) for k, x in quotas.items()}
        for k in sorted(quotas, key=lambda k: (quotas[k] - take[k], k), reverse=True)[:limit - sum(take.values())]:
            take[k] += 1
        picked = []
        for k in sorted(groups):
            g = groups[k][:]
            rng.shuffle(g)
            picked += g[:take[k]]
    return sorted(picked, key=lambda q: (len(q.qid), q.qid))
