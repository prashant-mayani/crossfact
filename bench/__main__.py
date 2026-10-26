"""python -m bench <command>

  run --config bench/configs/free.yaml [--limit N]   answer, grade, resume
  report --run RUN_ID [--md FILE] [--json FILE]      the tables for docs/EVAL.md
  runs                                               runs in the database
  grade-check --run RUN_ID --grader provider/model   how often a second grader agrees

Models are reached the way the app reaches them: keys from the environment on a
laptop (CROSSFACT_SECRETS_MODE=env; GROQ_API_KEY, GEMINI_API_KEY, ...), from the
mounted secrets in the cluster. Exit status 3 means "paused on a daily budget":
run the same command again later (--pause-ok makes that 0, for schedules).
Faults injected for a game day are ignored (CROSSFACT_FAULTS=off).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time

from app.resilience import ProviderError

from . import grader, report, runner
from .calls import BudgetExhausted, Calls
from .store import Store

DEFAULT_DIR = os.environ.get("CROSSFACT_BENCH_DIR", os.path.join(runner.ROOT, "bench", "runs"))


def _paths(args) -> tuple[str, str]:
    base = args.dir or DEFAULT_DIR
    os.makedirs(base, exist_ok=True)
    return args.db or os.path.join(base, "bench.db"), os.path.join(base, "cache")


def _write_json(path: str, data: dict) -> None:
    """Atomically: the app's metrics collector may be reading the old file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def cmd_run(args) -> int:
    db, cache = _paths(args)
    run_id, info = runner.run(args.config, db=db, cache_dir=cache, limit=args.limit,
                              template_file=args.template_file, allow_version_change=args.allow_version_change,
                              repin=args.repin)
    res = runner.results(Store(db), run_id, cache, args.template_file)
    print(report.markdown(res))
    if args.json:
        _write_json(args.json, {**res, **info, "finished_ts": time.time()})
    return 3 if info["paused"] and not args.pause_ok else 0


def cmd_report(args) -> int:
    db, cache = _paths(args)
    res = runner.results(Store(db), args.run, cache, args.template_file)
    md = report.markdown(res)
    print(md)
    if args.md:
        with open(args.md, "w") as f:
            f.write(md)
    if args.json:
        _write_json(args.json, res)
    return 0


def cmd_runs(args) -> int:
    db, _ = _paths(args)
    for r in Store(db).runs():
        print(f"{r['run_id']:40} {r['started']}  {r['questions']:>5} questions  code {r['code_version']}")
    return 0


def cmd_grade_check(args) -> int:
    """Re-grade a sample of model-graded answers with a second grader: agreement and Cohen's kappa."""
    db, cache = _paths(args)
    store = Store(db)
    meta = store.run_meta(args.run)
    if not meta:
        print(f"no run {args.run}", file=sys.stderr)
        return 1
    runner.setup_app(meta["config"], tempfile.mkdtemp(prefix="bench-"))
    template, sha = grader.load_template(cache, args.template_file)
    calls = Calls(store, meta["config"].get("budgets"), meta["config"].get("default_budget"))
    first = grader.Grader(calls, meta["config"]["grader"], template, sha)
    second = grader.Grader(calls, {"model": args.grader, "effort": args.effort}, template, sha)
    from .datasets import load

    qs = {q.qid: q for q in load(meta["dataset"], cache)[0]}
    seen, pairs = set(), []
    for a in store.answers(args.run):  # each distinct graded answer once (systems share answers)
        key = first.key(a["qid"], a["answer"]) if a["error"] is None else None
        g = store.grade(key) if key else None
        if g and g["how"] == "model" and key not in seen:
            seen.add(key)
            pairs.append((a, g["grade"]))
    random.Random(1).shuffle(pairs)
    pairs = pairs[:args.sample]
    agree, labels, failed = 0, [], 0
    try:
        for a, g1 in pairs:
            q = qs[a["qid"]]
            try:
                g2 = second.grade(store, q.qid, q.question, q.answer, a["answer"])["grade"]
            except (ProviderError, grader.GraderError):
                failed += 1
                continue
            labels.append((g1, g2))
            agree += g1 == g2
    except BudgetExhausted as e:
        print(f"paused: {e}")
    if failed:
        print(f"{failed} could not be re-graded (provider errors or unreadable replies)")
    n = len(labels)
    if not n:
        print("nothing to compare yet")
        return 0
    cats = ("CORRECT", "INCORRECT", "NOT_ATTEMPTED")
    pe = sum((sum(x == k for x, _ in labels) / n) * (sum(y == k for _, y in labels) / n) for k in cats)
    po = agree / n
    kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
    print(f"{n} model-graded answers re-graded by {second.id}: agreement {100 * po:.1f}%, Cohen's kappa {kappa:.2f}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", help=f"where the database and caches live (default {DEFAULT_DIR})")
    ap.add_argument("--db", help="the SQLite file (default <dir>/bench.db)")
    ap.add_argument("--template-file", help="a local copy of simple-evals' simpleqa_eval.py (offline use)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--limit", type=int)
    r.add_argument("--json", help="also write the results here")
    r.add_argument("--allow-version-change", action="store_true")
    r.add_argument("--repin", action="store_true", help="accept a changed dataset and re-pin its hash")
    r.add_argument("--pause-ok", action="store_true", help="exit 0 when paused on a budget (scheduled runs)")
    r.set_defaults(fn=cmd_run)
    p = sub.add_parser("report")
    p.add_argument("--run", required=True)
    p.add_argument("--md")
    p.add_argument("--json")
    p.set_defaults(fn=cmd_report)
    sub.add_parser("runs").set_defaults(fn=cmd_runs)
    g = sub.add_parser("grade-check")
    g.add_argument("--run", required=True)
    g.add_argument("--grader", required=True, help="provider/model")
    g.add_argument("--effort", default="off")
    g.add_argument("--sample", type=int, default=100)
    g.set_defaults(fn=cmd_grade_check)
    args = ap.parse_args(argv)
    os.environ.setdefault("CROSSFACT_FAULTS", "off")  # a game day's injected faults aren't the models' answers
    try:
        return args.fn(args)
    except runner.RunError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
