"""Run a configuration: answer every question with every system, grade, and
resume where the last run stopped (a model's daily budget, an interruption).

A run is identified by its configuration, its question set (and, for an
uncached run, the day): the same config on the same questions resumes the same
run. Pacing settings (budgets, pace) are not part of that identity, so they can
change mid-run. What decides answers is pinned when the run starts: the code
and the effective model registry. If either changed, the run refuses to
continue (pass allow_version_change to override, and the report says the run
mixed versions): numbers from two versions of the pipeline aren't one result.
"""
from __future__ import annotations

import copy
import glob
import hashlib
import json
import os
import tempfile
import time

import yaml

from . import calls as callmod
from . import datasets, grader, metrics, systems
from .calls import BudgetExhausted, Calls
from .store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_ATTEMPTS = 3  # a question a system keeps failing on is recorded as an error after this many runs
PACING_KEYS = ("budgets", "default_budget", "pace", "token_budgets")
# What decides an answer or a grade. Not here: the report, the statistics (computed at report
# time from stored grades, so fixing them never needs a re-run), the store and the CLI.
VERSIONED = ("app/*.py", "bench/calls.py", "bench/datasets.py", "bench/grader.py", "bench/systems.py")


class RunError(RuntimeError):
    pass


def code_version() -> str:
    h = hashlib.sha256()
    for f in sorted(p for pattern in VERSIONED for p in glob.glob(os.path.join(ROOT, pattern))):
        h.update(os.path.relpath(f, ROOT).encode())
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:16]


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    for key in ("name", "dataset", "systems", "grader"):
        if key not in cfg:
            raise RunError(f"{path}: missing {key!r}")
    names = [s["name"] for s in cfg["systems"]]
    if len(set(names)) != len(names):
        raise RunError(f"{path}: system names must be unique")
    head = cfg.get("primary") or {}
    unknown = [a for a in [head.get("system"), *(head.get("against") or [])]
               if a and a not in names and a not in ("best_single", "reference")]
    if unknown:
        raise RunError(f"{path}: the headline names systems this config doesn't run: {', '.join(unknown)}")
    return cfg


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


_MERGED: dict[str, str] = {}  # registry files written here -> the registry each was merged over


def setup_app(cfg: dict, workdir: str) -> str:
    """Point the app at this config's registry: the deployed (or built-in) one, with
    the config's providers and pipeline settings merged over it. Returns its sha256."""
    import app.config as config

    config.PROVIDERS_PATH = _MERGED.get(config.PROVIDERS_PATH, config.PROVIDERS_PATH)  # never merge over a merge
    config.reload()
    # Closed-book unless the config asks for search: the baselines don't search, and a
    # comparison in which only Crossfact could look things up would measure search.
    reg = _merge(config.registry(), {"providers": cfg.get("providers") or {}, "pipeline": cfg.get("pipeline") or {},
                                     "search": cfg.get("search") or {"enabled": False}})
    text = yaml.safe_dump(reg, sort_keys=True)
    path = os.path.join(workdir, "bench-providers.yaml")
    with open(path, "w") as f:
        f.write(text)
    _MERGED[path] = config.PROVIDERS_PATH
    config.PROVIDERS_PATH = path
    config.reload()
    # The instance's config.yaml can override pipeline settings (the chart sets the run deadline
    # there); for this process, the run's own pipeline settings win.
    instance = config.cfg().get("pipeline") or {}
    for key in cfg.get("pipeline") or {}:
        instance.pop(key, None)
    return hashlib.sha256(text.encode()).hexdigest()


def _pinned_template(template_file: str | None) -> bool:
    """Whether the grader prompt came from the pinned simple-evals file (a local
    --template-file may be anything)."""
    if not template_file:
        return True  # downloaded or cached copies are checked against the pin
    with open(template_file, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest() == grader.SIMPLE_EVALS_FILE_SHA256


def preflight(cfg: dict) -> None:
    """Every model the run needs must be reachable here: enabled, with its key set.
    Otherwise every question would fail the same way, three times over."""
    from app.config import registry
    from app.providers import configured

    needed = {systems.model_spec(cfg["grader"]["model"])[0]}
    for s in cfg["systems"]:
        for key in ("model", "aggregator", "side_a", "side_b"):
            if s.get(key):
                needed.add(systems.model_spec(s[key])[0])
        needed |= {systems.model_spec(m)[0] for m in s.get("models") or []}
        if s.get("kind") == "crossfact":
            pipe = registry()["pipeline"]
            for role in ("utility", "judge"):
                if not any(configured(c.get("provider", "")) for c in [pipe.get(role) or {}] +
                           list(pipe.get(f"{role}_fallbacks") or [])):
                    raise RunError(f"Crossfact needs a {role} model and none is set up here (pipeline.{role})")
    missing = sorted(p for p in needed if not configured(p))
    if missing:
        raise RunError(f"these providers aren't set up here (disabled, unknown, or no key): {', '.join(missing)}. "
                       "Set their keys (docs/EVAL.md) or leave out the systems that use them.")


def run(config_path: str, *, db: str, cache_dir: str, limit: int | None = None, template_file: str | None = None,
        allow_version_change: bool = False, repin: bool = False, leave_for_app: float = 0.0,
        log=print) -> tuple[str, dict]:
    """leave_for_app: the fraction of every daily budget to leave unused, for a deployed
    app on the same accounts (Groq's limits are per organization: a second key shares them)."""
    if not 0 <= leave_for_app < 1:
        raise RunError("--leave-for-app is a fraction between 0 and 1 (0.5 leaves half of each budget)")
    cfg = load_config(config_path)
    store = Store(db)
    registry_sha = setup_app(cfg, tempfile.mkdtemp(prefix="bench-"))
    preflight(cfg)
    questions, dataset_sha = datasets.load(cfg["dataset"], cache_dir, repin=repin)
    questions = datasets.sample(questions, limit or cfg.get("limit"), int(cfg.get("seed", 1)),
                                cfg.get("stratify", "topic"))
    template, template_sha = grader.load_template(cache_dir, template_file)
    cached = bool(cfg.get("cache", True))

    identity = {k: v for k, v in cfg.items() if k not in PACING_KEYS}
    config_sha = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    qset_sha = hashlib.sha256("|".join(q.qid for q in questions).encode()).hexdigest()
    run_id = f"{cfg['name']}-{config_sha[:8]}-{qset_sha[:8]}"
    if not cached:  # an uncached run measures one day: a new run each day
        run_id += "-" + callmod.utc_day().replace("-", "")
    version = {"code": code_version(), "registry": registry_sha[:16]}
    old = store.run_meta(run_id)
    if old:
        if old["dataset_sha256"] != dataset_sha or old["grader_template_sha256"] != template_sha:
            raise RunError(f"run {run_id}: the dataset or grader template changed since it started")
        if version not in old["versions"]:
            if not allow_version_change:
                was = old["versions"][-1]
                what = " and ".join(k for k in ("code", "registry") if was[k] != version[k])
                raise RunError(f"run {run_id} started on {what} {', '.join(was[k] for k in ('code', 'registry'))}; "
                               f"this is {version['code']}, {version['registry']}. Finish it on the same version, "
                               "or start a new run (change the config's name; the cache keeps it cheap)")
            old["versions"].append(version)
            store.update_run(run_id, old)
        meta = old
        log(f"resuming {run_id}")
    else:
        meta = {"config": cfg, "config_sha": config_sha, "versions": [version], "code_version": version["code"],
                "dataset": cfg["dataset"], "dataset_sha256": dataset_sha, "questions": len(questions),
                "qset_sha256": qset_sha, "grader_template_sha256": template_sha,
                "grader_template_pinned": _pinned_template(template_file),
                "instruction": cfg.get("instruction") or systems.DEFAULT_INSTRUCTION,
                "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "started_ts": time.time()}

    calls = Calls(store, cfg.get("budgets"), cfg.get("default_budget"), float(cfg.get("timeout_s", 300)),
                  fresh_after=None if cached else meta["started_ts"], pace=cfg.get("pace"),
                  token_budgets=cfg.get("token_budgets"), share=1.0 - leave_for_app)
    if leave_for_app:
        log(f"leaving {leave_for_app:.0%} of each daily budget for your app")
    grade = grader.Grader(calls, cfg["grader"], template, template_sha)
    built = [systems.build(s, calls, meta["instruction"], cfg.get("effort", "medium")) for s in cfg["systems"]]
    if not old:
        meta["grader"] = grade.id
        store.save_run(run_id, meta)
        log(f"new run {run_id}: {len(questions)} questions x {len(built)} systems, grader {grade.id}")

    paused = None
    try:
        for i, q in enumerate(questions, 1):
            for s in built:
                prev = store.answer(run_id, s.name, q.qid)
                tried = json.loads(prev["detail"]).get("attempts", 1) if prev and prev["detail"] else 0
                if prev and (prev["error"] is None or tried >= MAX_ATTEMPTS):
                    continue
                try:
                    r = systems.timed(s, q.question)
                    store.save_answer(run_id, s.name, q.qid, answer=r["answer"], error=None,
                                      latency=r["latency"], calls=r["calls"], cached=r["cached"],
                                      detail={**r["detail"], "attempts": tried + 1})
                except systems.Misconfigured as e:
                    raise RunError(str(e)) from e
                except Exception as e:  # a failed answer is recorded, retried on later runs, never scored
                    store.save_answer(run_id, s.name, q.qid, answer=None, error=f"{type(e).__name__}: {e}"[:300],
                                      latency=None, calls=0, cached=0, detail={"attempts": tried + 1})
            _grade_question(store, run_id, q, grade, log)
            if i % 10 == 0 or i == len(questions):
                log(f"  {i}/{len(questions)} questions; calls: {calls.real} new, {calls.hits} cached")
    except BudgetExhausted as e:
        paused = str(e)
        log(f"paused: {paused}. Run the same command on a later day to continue.")
    return run_id, {"paused": paused, "new_calls": calls.real, "cached_calls": calls.hits}


def _grade_question(store: Store, run_id: str, q, grade: grader.Grader, log=print) -> None:
    """Grade the question's answers, and each multi-model system's participants (for
    what the system lost or found). A grade that fails waits for the next run."""
    with store.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM answers WHERE run_id=? AND qid=? AND error IS NULL",
                                           (run_id, q.qid))]
    todo = []
    for r in rows:
        todo.append(r["answer"])
        todo += [a for a in (json.loads(r["detail"] or "{}").get("participants") or {}).values() if a is not None]
    for answer in dict.fromkeys(todo):
        try:
            grade.grade(store, q.qid, q.question, q.answer, answer)
        except BudgetExhausted:
            raise
        except Exception as e:
            log(f"  could not grade an answer to {q.qid} yet: {type(e).__name__}: {e}"[:200])


def results(store: Store, run_id: str, cache_dir: str, template_file: str | None = None) -> dict:
    meta = store.run_meta(run_id)
    if not meta:
        raise RunError(f"no run {run_id} (python -m bench runs)")
    cfg = meta["config"]
    template, template_sha = grader.load_template(cache_dir, template_file)
    grade = grader.Grader(Calls(store), cfg["grader"], template, template_sha)
    kinds = {s["name"]: s["kind"] for s in cfg["systems"]}
    rows = []
    for a in store.answers(run_id):
        g = store.grade(grade.key(a["qid"], a["answer"])) if a["error"] is None else None
        parts = [(store.grade(grade.key(a["qid"], p)) or {}).get("grade")
                 for p in (a["detail"].get("participants") or {}).values() if p is not None]
        rows.append({"system": a["system"], "kind": kinds.get(a["system"], ""), "qid": a["qid"],
                     "grade": g["grade"] if g else None, "error": a["error"], "latency": a["latency"],
                     "calls": a["calls"], "cached": a["cached"],
                     "participant_grades": parts if kinds.get(a["system"]) != "single" and all(parts) else []})
    order = [s["name"] for s in cfg["systems"]]
    head = cfg.get("primary") or {}
    primary = head.get("system") or next((n for n, k in kinds.items() if k == "crossfact"), None)
    summary = metrics.summarize(rows, reference=cfg.get("reference"), seed=int(cfg.get("seed", 1)), primary=primary,
                                against=head.get("against"))
    summary["systems"] = {k: summary["systems"][k] for k in order if k in summary["systems"]}
    return {"run_id": run_id, "meta": meta, "grader": grade.id, **summary}
