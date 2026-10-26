"""The measurement harness, end to end with fake models: every system answers
through the real call path (Crossfact through the real pipeline), answers are
graded, runs resume from the cache and pause on daily budgets, and the
statistics are what they say they are."""
import json
import re
from collections import Counter

import pytest
import yaml

QUESTIONS = [
    {"id": "1", "question": "What is the capital of France?", "answer": "Paris", "topic": "geo"},
    {"id": "2", "question": "What is the capital of Australia?", "answer": "Canberra", "topic": "geo"},
    {"id": "3", "question": "Who wrote Hamlet?", "answer": "William Shakespeare", "topic": "lit"},
    {"id": "4", "question": "What is 487 multiplied by 36?", "answer": "17532", "topic": "math"},
]
TRUTH = {"France": "Paris", "Australia": "Canberra", "Hamlet": "Shakespeare", "487": "17532"}
# What each fake model "knows": alpha is wrong about Australia (and fixes it when asked to
# check), beta is wrong about Hamlet and abstains on arithmetic, util knows everything.
KNOWS = {
    "alpha": {"France": "Paris", "Australia": "Sydney", "Hamlet": "Shakespeare", "487": "17,532"},
    "beta": {"France": "Paris", "Australia": "Canberra", "Hamlet": "Marlowe", "487": "I don't know"},
    "util": dict(TRUTH),
}
ON_SECOND_LOOK = {"alpha": {"Australia": "Canberra"}}
TEMPLATE = '''GRADER_TEMPLATE = """
Grade it.
Here is a new example.
Question: {question}
Gold target: {target}
Predicted answer: {predicted_answer}
Reply A, B or C.
""".strip()
'''


def _topic(text):
    return next((k for k in TRUTH if k in text), None)


def _same(x, y):
    from bench.grader import answer_key

    return answer_key(x) == answer_key(y)


def _right(text, question_text):
    return TRUTH[_topic(question_text)].lower() in text.lower().replace(",", "")


def model_reply(pid):
    """A fake model that plays every role the harness and the pipeline ask for."""

    def reply(prompt):
        if "Gold target:" in prompt:  # the grader
            gold = re.search(r"Gold target: (.*)", prompt).group(1).strip().lower()
            pred = re.search(r"Predicted answer: (.*)", prompt).group(1).strip().lower()
            return "A" if gold.split()[-1] in pred else ("C" if "know" in pred else "B")
        if "strict comparator" in prompt:
            a, b = (x.strip() for x in re.search(r"ANSWER A:\n(.*?)\n\nANSWER B:\n(.*)$", prompt, re.S).groups())
            if _same(a, b):
                return json.dumps({"verdict": "agree", "agreements": [a], "unique_a": [], "unique_b": [],
                                   "conflicts": []})
            return json.dumps({"verdict": "disagree", "agreements": [], "unique_a": [], "unique_b": [],
                               "conflicts": [{"topic": "the answer", "a_position": a, "b_position": b}]})
        if "Reconsider ONLY" in prompt:  # hold a position; trade "I don't know" for theirs
            own = re.search(r"YOUR PREVIOUS ANSWER:\n(.*)$", prompt, re.S).group(1).strip()
            return re.search(r"theirs: (.*)", prompt).group(1).strip() if "know" in own else own
        if "neutral arbiter" in prompt:  # rules for the right position, in the order shown
            q = re.search(r"QUESTION:\n(.*?)\n", prompt).group(1)
            shown_first = re.search(r"Answer A says: (.*)", prompt).group(1)
            winner = "A" if _right(shown_first, q) else "B"
            return json.dumps({"rulings": [{"point": 1, "winner": winner, "reason": "r"}], "analysis": "a"})
        if "Combine the material" in prompt:
            ruled = re.search(r"- the answer: (.*?) \(ruled correct", prompt)
            if ruled:
                return ruled.group(1)
            agreed = re.search(r"AGREED BY BOTH:\n- (.*)", prompt).group(1)
            return "I don't know" if agreed == "(none)" else agreed
        if "Your earlier answer" in prompt:
            topic = _topic(prompt.split("Your earlier answer")[0])
            return ON_SECOND_LOOK.get(pid, {}).get(topic) or KNOWS[pid][topic]
        topic = _topic(prompt)
        return KNOWS[pid][topic] if topic else "I don't know"

    return reply


SYSTEMS = [
    {"name": "single-alpha", "kind": "single", "model": "alpha/a1"},
    {"name": "single-beta", "kind": "single", "model": "beta/b1"},
    {"name": "selfcheck-alpha", "kind": "selfcheck", "model": "alpha/a1"},
    {"name": "vote", "kind": "vote", "models": ["alpha/a1", "beta/b1", "util/u1"]},
    {"name": "moa", "kind": "moa", "models": ["alpha/a1", "beta/b1"], "aggregator": "util/u1"},
    {"name": "crossfact", "kind": "crossfact", "side_a": "alpha/a1", "side_b": "beta/b1"},
]


def error(provider, exc, purpose=None, times=None):
    return {"provider": provider, "model": None, "purpose": purpose, "reply": None, "error": exc, "times": times}


@pytest.fixture
def bench_env(llm, env, tmp_path):
    for pid in ("alpha", "beta", "util"):
        llm.on(provider=pid, reply=model_reply(pid))
    llm.on(provider="judge", reply=model_reply("judge"))
    (tmp_path / "qs.yaml").write_text(yaml.safe_dump({"questions": QUESTIONS}))
    (tmp_path / "simpleqa_eval.py").write_text(TEMPLATE)
    cfg = {"name": "t", "dataset": str(tmp_path / "qs.yaml"), "seed": 1, "effort": "medium",
           "grader": {"model": "judge/j1", "effort": "off"}, "systems": SYSTEMS}

    def write(**over):
        path = tmp_path / "cfg.yaml"
        path.write_text(yaml.safe_dump({**cfg, **over}))
        return str(path)

    return {"write": write, "db": str(tmp_path / "bench.db"), "cache": str(tmp_path / "cache"),
            "template": str(tmp_path / "simpleqa_eval.py"), "llm": llm, "providers": tmp_path / "providers.yaml"}


def go(b, path, **kw):
    from bench import runner

    return runner.run(path, db=b["db"], cache_dir=b["cache"], template_file=b["template"], log=lambda *a: None, **kw)


def results(b, run_id):
    from bench import runner
    from bench.store import Store

    return runner.results(Store(b["db"]), run_id, b["cache"], b["template"])


def counts(res):
    return {k: (v["correct"], v["incorrect"], v["not_attempted"]) for k, v in res["systems"].items()}


def answers_of(b, run_id, system):
    from bench.store import Store

    return {a["qid"]: a for a in Store(b["db"]).answers(run_id) if a["system"] == system}


def asked(llm, provider, start):
    return [p for c, p in zip(llm.calls, llm.prompts, strict=True) if c["provider"] == provider and p.startswith(start)]


# -- end to end ------------------------------------------------------------------------------

def test_every_system_answers_and_is_graded(bench_env):
    run_id, info = go(bench_env, bench_env["write"]())
    assert info["paused"] is None
    res = results(bench_env, run_id)
    assert list(res["systems"]) == [s["name"] for s in SYSTEMS]
    assert all(v["n"] == 4 and v["errors"] == 0 and v["ungraded"] == 0 for v in res["systems"].values())
    assert counts(res) == {
        "single-alpha": (3, 1, 0),     # Sydney
        "single-beta": (2, 1, 1),      # Marlowe; abstains on 487
        "selfcheck-alpha": (4, 0, 0),  # a second look fixes Sydney
        "vote": (4, 0, 0),             # "17,532" and "17532" are one answer
        "moa": (4, 0, 0),
        "crossfact": (4, 0, 0),
    }
    assert res["reference"] == "single-alpha"  # the best single model by F1
    s = res["systems"]
    assert s["selfcheck-alpha"]["gain_rate"] == 0.25 and s["selfcheck-alpha"]["oracle_gap"] == 0
    assert "oracle_gap" not in s["single-alpha"]


def test_the_report_says_what_happened_when_one_side_abstained(bench_env):
    from bench import report

    run_id, _ = go(bench_env, bench_env["write"]())
    res = results(bench_env, run_id)
    # 487 x 36: beta said "I don't know", alpha answered (rightly); Crossfact committed to alpha's answer
    assert res["systems"]["crossfact"]["split"] == {"n": 1, "correct": 1, "incorrect": 0, "abstained": 0,
                                                  "a_participant_correct": 1}
    assert "split" not in res["systems"]["selfcheck-alpha"]  # alpha never abstained
    md = report.markdown(res)
    assert "| crossfact | 1 | 1 | 0 | 0 | 1 |" in md and "commits to the other side's answer" in md


def test_crossfact_runs_the_real_pipeline(bench_env):
    run_id, _ = go(bench_env, bench_env["write"]())
    got = answers_of(bench_env, run_id, "crossfact")
    assert {q: a["detail"]["outcome"] for q, a in got.items()} == {
        "1": "agreed", "2": "judged", "3": "judged", "4": "debated"}
    assert got["2"]["detail"]["debate_rounds"] == 3 and got["4"]["detail"]["debate_rounds"] == 1
    assert got["2"]["answer"] == "Canberra" and got["3"]["answer"] == "Shakespeare"
    assert got["2"]["detail"]["participants"] == {"A:alpha/a1": "Sydney", "B:beta/b1": "Canberra"}
    import app.orchestrator as orchestrator
    from app.providers import ask

    assert orchestrator.ask is ask  # the harness put the app's call function back


def test_a_second_run_costs_no_calls_and_gives_the_same_numbers(bench_env):
    path = bench_env["write"]()
    run_id, _ = go(bench_env, path)
    before = results(bench_env, run_id)
    n_calls = len(bench_env["llm"].calls)
    run_id2, second = go(bench_env, path)
    assert run_id2 == run_id and second["new_calls"] == 0
    assert len(bench_env["llm"].calls) == n_calls
    assert results(bench_env, run_id)["systems"] == before["systems"]


def test_each_distinct_call_is_made_once(bench_env):
    """The baselines and Crossfact ask alpha the same question: one call serves all of them."""
    go(bench_env, bench_env["write"]())
    llm = bench_env["llm"]
    sent = Counter((c["provider"], c["model"], p) for c, p in zip(llm.calls, llm.prompts, strict=True))
    assert max(sent.values()) == 1
    assert len(asked(llm, "alpha", "What is the capital of France?")) == 1  # single, self-check, vote, MoA, Crossfact


def test_the_cli_runs_reports_and_feeds_the_metrics(bench_env, tmp_path, capsys, monkeypatch):
    from prometheus_client import CollectorRegistry

    from app.evalmetrics import EvalCollector
    from bench.__main__ import main

    monkeypatch.setenv("CROSSFACT_FAULTS", "off")  # what the CLI sets for itself, undone after the test
    out = tmp_path / "latest.json"
    args = ["--dir", str(tmp_path / "cli"), "--template-file", bench_env["template"]]
    assert main([*args, "run", "--config", bench_env["write"](), "--json", str(out)]) == 0
    res = json.loads(out.read_text())
    assert res["systems"]["crossfact"]["correct"] == 4 and res["paused"] is None
    assert main([*args, "report", "--run", res["run_id"]]) == 0
    assert "| crossfact | 4 |" in capsys.readouterr().out
    assert main([*args, "report", "--run", "nope"]) == 1

    reg = CollectorRegistry()
    reg.register(EvalCollector(lambda: str(out)))
    value = reg.get_sample_value
    assert value("crossfact_eval_answer_ratio", {"system": "crossfact", "result": "correct"}) == 1.0
    assert value("crossfact_eval_answer_ratio", {"system": "single-beta", "result": "not_attempted"}) == 0.25
    assert value("crossfact_eval_graded_questions", {"system": "vote"}) == 4
    assert value("crossfact_eval_paused") == 0 and value("crossfact_eval_last_run_timestamp_seconds") > 0


def test_grade_check_measures_agreement_between_two_graders(bench_env, tmp_path, capsys, monkeypatch):
    from bench.__main__ import main

    monkeypatch.setenv("CROSSFACT_FAULTS", "off")
    args = ["--dir", str(tmp_path / "cli"), "--template-file", bench_env["template"]]
    assert main([*args, "run", "--config", bench_env["write"]()]) == 0
    run_id = re.search(r"### `([^`]+)`", capsys.readouterr().out).group(1)
    util = model_reply("util")
    bench_env["llm"].rules.insert(0, {"provider": "util", "model": None, "purpose": None, "error": None,
                                      "times": None, "reply": lambda p: "B" if "Gold target:" in p else util(p)})
    assert main([*args, "grade-check", "--run", run_id, "--grader", "util/u1"]) == 0
    # three distinct answers needed the model (Sydney, Shakespeare, Marlowe); the second grader
    # calls them all wrong: it agrees on two, which is what chance alone predicts
    assert "3 model-graded answers" in (out := capsys.readouterr().out)
    assert "agreement 66.7%, Cohen's kappa 0.00" in out


def test_a_paused_scheduled_run_is_not_a_failure(bench_env, tmp_path, monkeypatch):
    from bench.__main__ import main

    monkeypatch.setenv("CROSSFACT_FAULTS", "off")
    path = bench_env["write"](budgets={"beta/b1": 1})
    args = ["--dir", str(tmp_path / "cli"), "--template-file", bench_env["template"], "run", "--config", path]
    assert main(args) == 3
    assert main([*args, "--pause-ok"]) == 0


def test_the_metrics_collector_tolerates_no_file_and_bad_files(tmp_path):
    from prometheus_client import CollectorRegistry

    from app.evalmetrics import EvalCollector

    f = tmp_path / "latest.json"
    reg = CollectorRegistry()
    reg.register(EvalCollector(lambda: str(f)))
    assert reg.get_sample_value("crossfact_eval_paused") is None  # no file, no series
    f.write_text("{not json")
    assert reg.get_sample_value("crossfact_eval_paused") is None
    f.write_text(json.dumps({"systems": {"s": {"correct_rate": 0.5, "n": 2}}, "paused": "budget"}))
    assert reg.get_sample_value("crossfact_eval_paused") == 1
    assert reg.get_sample_value("crossfact_eval_answer_ratio", {"system": "s", "result": "correct"}) == 0.5


def test_game_day_faults_dont_reach_the_harness(bench_env, monkeypatch):
    import time

    from app import db, faults

    db.add_fault("alpha", "error", 1.0, 0.0, time.time() + 3600, "test")
    faults.invalidate()
    assert faults.active()  # the app would fail every alpha call now
    monkeypatch.setenv("CROSSFACT_FAULTS", "off")
    run_id, _ = go(bench_env, bench_env["write"](systems=[SYSTEMS[0]]))
    assert results(bench_env, run_id)["systems"]["single-alpha"]["errors"] == 0


def test_report_puts_correct_wrong_and_not_sure_side_by_side(bench_env):
    from bench import report

    run_id, _ = go(bench_env, bench_env["write"]())
    md = report.markdown(results(bench_env, run_id))
    assert "| system | graded | correct | wrong | not sure |" in md
    assert "Against **single-alpha**" in md and "McNemar" in md and "| lost | found |" in md
    assert "mixed versions" not in md
    assert "not the pinned simple-evals file" in md  # the tests grade with a made-up template


# -- budgets, pacing and failures ---------------------------------------------------------------

def test_budgets_pause_the_run_and_later_days_finish_it(bench_env, monkeypatch):
    from bench import calls

    day = {"n": 1}
    monkeypatch.setattr(calls, "utc_day", lambda: f"2099-01-{day['n']:02d}")
    llm = bench_env["llm"]
    tight = bench_env["write"](budgets={"beta/b1": 2})
    run_id, info = go(bench_env, tight)
    assert info["paused"] and "beta/b1" in info["paused"]
    assert results(bench_env, run_id)["systems"]["single-beta"]["n"] < 4

    beta = llm.count(provider="beta")
    _, again = go(bench_env, tight)  # the same day: still paused, beta not asked
    assert again["paused"] and llm.count(provider="beta") == beta

    while info["paused"]:
        day["n"] += 1
        assert day["n"] < 20, "the run never finished"
        run_id2, info = go(bench_env, bench_env["write"](budgets={"beta/b1": 2 + day["n"] % 2}))  # budgets can change
        assert run_id2 == run_id
    assert counts(results(bench_env, run_id))["crossfact"] == (4, 0, 0)


def test_token_budgets_use_the_counts_the_provider_reports(bench_env):
    """Groq's free tier caps tokens per day (reasoning included), which a reasoning model
    reaches before its request cap: the run stops on our own token budget first."""
    from app.providers import _reply

    alpha = model_reply("alpha")
    bench_env["llm"].rules.insert(0, {"provider": "alpha", "model": None, "purpose": None, "error": None,
                                      "times": None, "reply": lambda p: _reply(alpha(p), {"total": 100})})
    run_id, info = go(bench_env, bench_env["write"](systems=[SYSTEMS[0]], token_budgets={"alpha/a1": 250}))
    assert info["paused"] and "300 tokens today" in info["paused"]
    assert len(answers_of(bench_env, run_id, "single-alpha")) == 3


def test_leaving_quota_for_the_app_scales_every_budget(bench_env):
    from bench import runner

    path = bench_env["write"](systems=[SYSTEMS[1]], budgets={"beta/b1": 4})
    run_id, info = go(bench_env, path, leave_for_app=0.5)
    assert info["paused"] and "budget is 2 calls" in info["paused"]
    assert len(answers_of(bench_env, run_id, "single-beta")) == 2
    with pytest.raises(runner.RunError, match="fraction"):
        go(bench_env, path, leave_for_app=1.0)


def test_quota_exhaustion_inside_crossfact_is_a_pause_not_an_answer(bench_env):
    from app.resilience import ErrorKind, ProviderError
    from bench.store import Store

    llm = bench_env["llm"]
    llm.rules.insert(0, error("beta", ProviderError("tokens per day limit reached", ErrorKind.QUOTA_EXHAUSTED)))
    path = bench_env["write"](systems=[SYSTEMS[-1], SYSTEMS[0]])
    run_id, info = go(bench_env, path)
    assert info["paused"] and "quota" in info["paused"]
    assert Store(bench_env["db"]).answers(run_id) == []  # nothing scored, nothing recorded as failed
    beta = llm.count(provider="beta")
    _, info = go(bench_env, path)
    assert info["paused"] and llm.count(provider="beta") == beta  # not asked again today


def test_provider_trouble_in_crossfact_is_retried_not_scored(bench_env):
    from app.resilience import ErrorKind, ProviderError

    bench_env["llm"].rules.insert(0, error("beta", ProviderError("upstream 503", ErrorKind.SERVER), "debate", 2))
    path = bench_env["write"](systems=[SYSTEMS[-1]])
    run_id, _ = go(bench_env, path)
    got = answers_of(bench_env, run_id, "crossfact")
    assert [q for q, a in got.items() if a["error"]] == ["2"]  # its debate failed; the run went on without it
    assert got["2"]["error"].startswith("ProviderTrouble") and got["2"]["answer"] is None
    assert results(bench_env, run_id)["systems"]["crossfact"]["errors"] == 1
    go(bench_env, path)  # transient: a later run answers it, reusing the calls that worked
    got = answers_of(bench_env, run_id, "crossfact")
    assert all(a["error"] is None for a in got.values()) and got["2"]["detail"]["attempts"] == 2
    assert got["2"]["detail"]["outcome"] == "judged"


def test_a_system_that_keeps_failing_is_counted_as_an_error(bench_env):
    from app.resilience import ErrorKind, ProviderError
    from bench import runner

    bench_env["llm"].rules.insert(0, error("beta", ProviderError("bad gateway", ErrorKind.SERVER)))
    path = bench_env["write"](systems=[SYSTEMS[1]])
    for _ in range(runner.MAX_ATTEMPTS + 1):
        run_id, _ = go(bench_env, path)
    s = results(bench_env, run_id)["systems"]["single-beta"]
    assert s["errors"] == 4 and s["n"] == 0
    bench_env["llm"].rules.pop(0)
    go(bench_env, path)
    assert results(bench_env, run_id)["systems"]["single-beta"]["errors"] == 4  # final after three tries


def test_rate_limits_are_waited_out(bench_env, monkeypatch):
    from app.resilience import ErrorKind, ProviderError

    slept = []
    monkeypatch.setattr("time.sleep", slept.append)
    bench_env["llm"].rules.insert(0, error("alpha", ProviderError("429", ErrorKind.RATE_LIMITED, retry_after=7),
                                           times=2))
    run_id, _ = go(bench_env, bench_env["write"](systems=[SYSTEMS[0]], pace={"alpha/a1": 30}))
    res = results(bench_env, run_id)["systems"]["single-alpha"]
    assert res["errors"] == 0 and res["n"] == 4
    assert slept.count(7) == 2  # the provider's hint, once the app's own retries gave up
    assert sum(1 for s in slept if 1.5 < s <= 2.0) >= 2  # pace: 30 a minute is one every 2 s
    assert res["latency_p95"] < 1  # waiting isn't latency


def test_an_uncached_run_is_fresh_each_day_but_shares_calls_within_the_day(bench_env, monkeypatch):
    from bench import calls

    day = {"d": "2099-02-01"}
    monkeypatch.setattr(calls, "utc_day", lambda: day["d"])
    llm = bench_env["llm"]
    path = bench_env["write"](cache=False)
    run1, _ = go(bench_env, path)
    assert run1.endswith("-20990201")
    first = len(llm.calls)
    day["d"] = "2099-02-02"
    run2, info = go(bench_env, path)
    assert run2 != run1 and info["new_calls"] > 0
    llm.calls, llm.prompts = llm.calls[first:], llm.prompts[first:]
    assert len(asked(llm, "alpha", "What is the capital of France?")) == 1


def test_an_unparseable_grade_is_retried_then_left_for_the_next_run(bench_env):
    bench_env["llm"].on(provider="judge", reply="hmm", times=2)
    bench_env["llm"].rules.insert(0, bench_env["llm"].rules.pop())
    path = bench_env["write"](systems=[SYSTEMS[0]])
    run_id, _ = go(bench_env, path)
    assert results(bench_env, run_id)["systems"]["single-alpha"]["ungraded"] == 1  # Sydney
    go(bench_env, path)  # the useless replies weren't cached
    assert results(bench_env, run_id)["systems"]["single-alpha"]["ungraded"] == 0


# -- what a run is ------------------------------------------------------------------------------

def test_a_run_wont_mix_code_versions(bench_env, monkeypatch):
    from bench import report, runner

    path = bench_env["write"]()
    run_id, _ = go(bench_env, path)
    monkeypatch.setattr(runner, "code_version", lambda: "something-else")
    with pytest.raises(runner.RunError, match="started on code"):
        go(bench_env, path)
    go(bench_env, path, allow_version_change=True)
    go(bench_env, path)  # allowed once, that version is part of the run now
    assert "mixed versions" in report.markdown(results(bench_env, run_id))


def test_a_changed_registry_counts_as_a_version_change(bench_env):
    from bench import runner

    go(bench_env, bench_env["write"]())
    reg = yaml.safe_load(bench_env["providers"].read_text())
    reg["providers"]["alpha"]["timeout_s"] = 61
    bench_env["providers"].write_text(yaml.safe_dump(reg))
    with pytest.raises(runner.RunError, match="started on registry"):
        go(bench_env, bench_env["write"]())


def test_missing_keys_stop_the_run_before_it_starts(bench_env):
    from bench import runner

    with pytest.raises(runner.RunError, match="off"):
        go(bench_env, bench_env["write"](systems=[{"name": "s", "kind": "single", "model": "off/o1"}]))
    with pytest.raises(runner.RunError, match="utility"):
        go(bench_env, bench_env["write"](systems=[SYSTEMS[-1]], pipeline={
            "utility": {"provider": "off", "model": "o1"}, "utility_fallbacks": []}))
    assert bench_env["llm"].calls == []


def test_self_consistency_asks_one_model_several_times_and_shares_the_first_draw(bench_env):
    llm = bench_env["llm"]
    sc = {"name": "sc-alpha", "kind": "vote", "models": ["alpha/a1"], "samples": 3}
    run_id, _ = go(bench_env, bench_env["write"](systems=[SYSTEMS[0], sc]))
    assert len(asked(llm, "alpha", "What is the capital of France?")) == 3  # single's answer is draw 1
    got = answers_of(bench_env, run_id, "sc-alpha")
    assert set(got["2"]["detail"]["participants"]) == {"alpha/a1", "alpha/a1#2", "alpha/a1#3"}
    assert got["2"]["answer"] == "Sydney"  # the same mistake three times is still the majority


def test_vote_and_moa_list_each_model_once():
    from bench import systems

    with pytest.raises(ValueError, match="once"):
        systems.build({"name": "v", "kind": "vote", "models": ["alpha/a1", "alpha/a1@medium"]}, None, "", "medium")


# -- the statistics -----------------------------------------------------------------------------

def test_mcnemar_and_f1():
    from bench.metrics import _rates, mcnemar_exact

    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(10, 0) == pytest.approx(2 / 1024)
    assert mcnemar_exact(5, 5) == 1.0
    assert mcnemar_exact(1, 8) == pytest.approx(2 * (1 + 9) / 512)
    r = _rates(["CORRECT"] * 6 + ["INCORRECT"] * 2 + ["NOT_ATTEMPTED"] * 2)
    assert r["correct_rate"] == 0.6 and r["attempted_accuracy"] == 0.75
    assert r["f1"] == pytest.approx(2 * 0.6 * 0.75 / 1.35)


def test_holm_adjustment():
    from bench.metrics import holm

    assert holm([0.01, 0.04, 0.03, 0.2]) == pytest.approx([0.04, 0.09, 0.09, 0.2])
    assert holm([0.5]) == [0.5] and holm([]) == []
    assert holm([0.6, 0.7]) == pytest.approx([1.0, 1.0])


def test_the_f1_difference_test():
    from bench.metrics import f1_difference

    same = ["CORRECT", "INCORRECT", "NOT_ATTEMPTED"] * 10
    r = f1_difference(same, same, boot=300)
    assert r["delta"] == 0 and r["p"] == 1.0
    better = ["CORRECT"] * 25 + ["INCORRECT"] * 5
    worse = ["INCORRECT"] * 25 + ["CORRECT"] * 5
    r = f1_difference(better, worse, boot=300)
    assert r["delta"] > 0.5 and r["p"] < 0.01 and r["ci"][0] > 0


def test_the_headline_is_fixed_in_advance_and_the_rest_is_adjusted(bench_env):
    from bench import report

    run_id, _ = go(bench_env, bench_env["write"]())
    res = results(bench_env, run_id)
    h = res["primary"]
    assert (h["system"], h["reference"], h["n"]) == ("crossfact", "single-alpha", 4)
    assert h["incorrect_delta"] == pytest.approx(-0.25) and h["f1_delta"] == pytest.approx(0.25)
    assert h["incorrect_p_holm"] >= h["incorrect_p"] and h["f1_p_holm"] >= h["f1_p"]
    p = res["paired"]
    assert "incorrect_p_holm" not in p["crossfact"] and "correct_p_holm" in p["crossfact"]
    assert all("incorrect_p_holm" in v and "correct_p_holm" in v for k, v in p.items() if k != "crossfact")
    md = report.markdown(res)
    assert md.index("Headline, chosen before the run: crossfact against single-alpha") < md.index("| system | graded")
    assert "(headline)" in md


def test_abstaining_more_lowers_wrong_and_correct_together():
    """The trap: a system that says "I don't know" more looks more accurate when it
    answers. The paired numbers show what it gave up for that."""
    from bench.metrics import summarize

    rows = []
    for q in range(20):
        rows.append({"system": "bold", "kind": "single", "qid": str(q), "grade": "CORRECT" if q < 14 else "INCORRECT"})
        rows.append({"system": "timid", "kind": "single", "qid": str(q),
                     "grade": "CORRECT" if q < 10 else "NOT_ATTEMPTED"})
    s = summarize(rows, reference="bold", boot=200)
    timid, bold = s["systems"]["timid"], s["systems"]["bold"]
    assert timid["attempted_accuracy"] == 1.0 > bold["attempted_accuracy"] == 0.7
    p = s["paired"]["timid"]
    assert p["correct_delta"] == pytest.approx(-0.2) and p["incorrect_delta"] == pytest.approx(-0.3)
    assert p["correct_discordant"] == [4, 0] and p["correct_p"] == pytest.approx(2 / 16)
    assert timid["f1"] < bold["f1"]
    lo, hi = timid["correct_rate_ci"]
    assert lo <= 0.5 <= hi


def test_stratified_sample_is_deterministic_keeps_the_mix_and_nests():
    from bench.datasets import Question, sample

    qs = [Question(str(i), "q", "a", topic="a" if i < 75 else "b") for i in range(100)]
    s1, s2 = sample(qs, 20, seed=3), sample(qs, 20, seed=3)
    assert [q.qid for q in s1] == [q.qid for q in s2]
    assert sum(q.topic == "a" for q in s1) == 15 and sum(q.topic == "b" for q in s1) == 5
    assert [q.qid for q in sample(qs, 20, seed=4)] != [q.qid for q in s1]
    assert {q.qid for q in sample(qs, 8, seed=3)} <= {q.qid for q in s1}  # a quick look is part of the full run
    assert {q.qid for q in sample(qs, 8, 3, None)} <= {q.qid for q in sample(qs, 30, 3, None)}


# -- datasets and grading -----------------------------------------------------------------------

def test_a_changed_dataset_is_refused(tmp_path, monkeypatch):
    from bench import datasets

    monkeypatch.setattr(datasets, "PACKAGED_LOCK", str(tmp_path / "packaged.json"))
    csv = "original_index,problem,answer,topic\n1,Q?,A,t\n"
    monkeypatch.setattr(datasets, "_fetch", lambda url: csv.encode())
    lock = tmp_path / "lock.json"
    qs, sha = datasets.simpleqa_verified(str(tmp_path / "c1"), lock_path=str(lock))
    assert qs[0].qid == "1" and json.loads(lock.read_text())["simpleqa_verified"]["sha256"] == sha
    monkeypatch.setattr(datasets, "_fetch", lambda url: (csv + "2,Q2?,B,t\n").encode())
    datasets.simpleqa_verified(str(tmp_path / "c1"), lock_path=str(lock))  # the checked copy is reused
    with pytest.raises(datasets.DatasetError, match="changed"):
        datasets.simpleqa_verified(str(tmp_path / "c2"), lock_path=str(lock))
    qs, _ = datasets.simpleqa_verified(str(tmp_path / "c3"), lock_path=str(lock), repin=True)
    assert len(qs) == 2


def test_pins_shipped_with_the_code_are_checked_when_the_lock_is_elsewhere(tmp_path, monkeypatch):
    """The nightly writes its lock on its volume; pins committed with the code still apply."""
    from bench import datasets

    packaged = tmp_path / "packaged.json"
    packaged.write_text(json.dumps({"simpleqa_verified": {"url": "u", "sha256": "0" * 64}}))
    monkeypatch.setattr(datasets, "PACKAGED_LOCK", str(packaged))
    monkeypatch.setattr(datasets, "_fetch", lambda url: b"original_index,problem,answer\n1,Q?,A\n")
    with pytest.raises(datasets.DatasetError, match="changed"):
        datasets.simpleqa_verified(str(tmp_path / "c"), lock_path=str(tmp_path / "volume" / "lock.json"))


def test_the_grader_template_is_read_as_data_and_gets_the_verified_rules(tmp_path):
    from bench import grader

    marker = tmp_path / "ran"
    f = tmp_path / "simpleqa_eval.py"
    f.write_text(TEMPLATE + f"\nopen({str(marker)!r}, 'w').close()\n")
    t, sha = grader.load_template(str(tmp_path), str(f))
    assert t.index("Four more rules") < t.index("Here is a new example.")
    assert "{question}" in t and len(sha) == 64
    assert not marker.exists()  # parsed, never run


def test_a_downloaded_template_must_match_its_pinned_hash(tmp_path):
    from bench import grader

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"simpleqa_eval-{grader.SIMPLE_EVALS_COMMIT[:12]}.py").write_text(TEMPLATE)
    with pytest.raises(grader.GraderError, match="pinned sha256"):
        grader.load_template(str(cache))


@pytest.mark.parametrize("answer,gold,grade", [
    ("I don't know", "Paris", "NOT_ATTEMPTED"), ("I\u2019m not sure.", "Paris", "NOT_ATTEMPTED"),
    ("", "Paris", "NOT_ATTEMPTED"), ("paris.", "Paris", "CORRECT"), ("17,532", "17532", "CORRECT"),
    ("Sydney", "Canberra", None), ("1.73", "1.73 m", None), ("I don't know Paris", "Paris", None),
])
def test_only_unambiguous_cases_skip_the_grader(answer, gold, grade):
    from bench.grader import auto_grade

    got = auto_grade(answer, gold)
    assert (got[0] if got else None) == grade


@pytest.mark.parametrize("reply,grade", [
    ("A", "CORRECT"), ("B.", "INCORRECT"), ("(C)", "NOT_ATTEMPTED"), ("**A**", "CORRECT"), ("b\n", "INCORRECT"),
    # an explanation is read by its grade words, never by a leading word "A"
    ("A predicted answer of 1923 contradicts the gold target, so it is INCORRECT", "INCORRECT"),
    ("The predicted answer is not correct.", "INCORRECT"), ("C) NOT ATTEMPTED", "NOT_ATTEMPTED"),
    ("A: CORRECT", "CORRECT"), ("A\nThe predicted answer matches the gold target.", "CORRECT"),
    ("Grade: C (NOT_ATTEMPTED)", "NOT_ATTEMPTED"), ("NOT_ATTEMPTED", "NOT_ATTEMPTED"),
    ("The grade is A", "CORRECT"), ("Answer: B.", "INCORRECT"),
    # ambiguous or no grade at all: ask again
    ("A: INCORRECT", None), ("A\nThis is INCORRECT because", None), ("CORRECT, not INCORRECT", None),
    ("I would say A", None), ("A cat sat on the mat", None), ("maybe", None), ("", None),
])
def test_grader_replies_are_parsed(reply, grade):
    from bench.grader import parse

    assert parse(reply) == grade


def test_a_parser_change_regrades_from_cached_replies(bench_env, monkeypatch):
    """Grades are cached per parser version: a fixed parser re-reads the stored replies
    (no new grader calls) instead of keeping grades the old parser got wrong."""
    from bench import grader

    path = bench_env["write"](systems=[SYSTEMS[0], SYSTEMS[1]])
    run_id, _ = go(bench_env, path)
    before = counts(results(bench_env, run_id))
    monkeypatch.setattr(grader, "PARSER_VERSION", grader.PARSER_VERSION + 1)
    assert results(bench_env, run_id)["systems"]["single-alpha"]["ungraded"] == 4  # every grade is per parser
    _, info = go(bench_env, path)
    assert info["new_calls"] == 0 and counts(results(bench_env, run_id)) == before


def test_vote_keys():
    from bench.grader import answer_key

    assert answer_key("17,532") == answer_key("17532.0") == answer_key(" 17532 ")
    assert answer_key("The Beatles") == answer_key("beatles.")
    assert answer_key("Shakespeare") != answer_key("William Shakespeare")
