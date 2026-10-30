"""The consensus pipeline end to end, through the real call path with a fake model."""
import json

import pytest

from app.resilience import ErrorKind, ProviderError
from tests.conftest import COMPARE_AGREE, COMPARE_CONFLICT

A = {"provider": "alpha", "model": "a1", "effort": "medium"}
B = {"provider": "beta", "model": "b1", "effort": "medium"}


def run(mode="consensus", **kw):
    from app.orchestrator import run_pipeline

    return run_pipeline("What is x?", "", A, B, mode, None, **kw)


def stages(out, kind):
    return [s for s in out["stages"] if s["stage"] == kind]


def test_agreement_merges_the_union(llm):
    out = run()
    assert out["outcome"] == "agreed" and out["final"] == "merged answer" and out["degraded"] == []
    assert [s["side"] for s in stages(out, "answer")] == ["A", "B"]
    assert llm.count(purpose="debate") == 0 and llm.count(purpose="judge") == 0


def test_conflict_resolved_by_debate(llm):
    llm.on(purpose="compare", reply=COMPARE_CONFLICT, times=1)
    out = run()
    assert out["outcome"] == "debated"
    assert len(stages(out, "debate")) == 1 and llm.count(purpose="debate") == 2


def test_persistent_conflict_goes_to_the_judge_and_rulings_are_unflipped(llm, monkeypatch):
    llm.on(purpose="compare", reply=COMPARE_CONFLICT)
    monkeypatch.setattr("app.orchestrator.random.random", lambda: 0.0)  # force the anonymizing flip
    out = run()
    assert out["outcome"] == "judged"
    assert len(stages(out, "debate")) == 3
    judge = stages(out, "judge")[0]
    assert judge["rulings"][0]["winner"] == "B"  # judge said "A" about the flipped order -> real side B


CAPITAL = ('{"verdict":"partial","agreements":[],"unique_a":[],"unique_b":[],'
           '"conflicts":[{"topic":"capital","a_position":"Canberra","b_position":"Sydney"}]}')


def content_judge(prompt: str) -> str:
    """A judge that reads: it picks the answer saying Canberra, and refuses to rule when
    the contested points contradict the full answers it was shown."""
    import re

    says = dict(re.findall(r"Answer ([AB]) says: (\w+)", prompt))
    full = dict(re.findall(r"ANSWER ([AB]) IN FULL:\n(.*?)(?:\n\n|$)", prompt, re.S))
    consistent = all(says[k] in full[k] for k in "AB")
    winner = next(k for k, v in says.items() if v == "Canberra") if consistent else "unverified"
    return json.dumps({"rulings": [{"point": 1, "winner": winner, "reason": "r"}], "analysis": "ok"})


@pytest.mark.parametrize("draw", [0.0, 0.9], ids=["shown-swapped", "shown-in-order"])
def test_judge_sees_points_labelled_like_the_answers_in_either_order(llm, monkeypatch, draw):
    llm.on(purpose="compare", reply=CAPITAL)
    llm.on(provider="alpha", purpose="answer", reply="The capital is Canberra.")
    llm.on(provider="beta", purpose="answer", reply="The capital is Sydney.")
    llm.on(provider="alpha", purpose="debate", reply="Still Canberra.")
    llm.on(provider="beta", purpose="debate", reply="Still Sydney.")
    llm.on(purpose="judge", reply=content_judge)
    monkeypatch.setattr("app.orchestrator.random.random", lambda: draw)
    out = run()
    judge = stages(out, "judge")[0]
    assert judge["shown_swapped"] is (draw < 0.5)
    assert judge["rulings"][0]["winner"] == "A"  # alpha said Canberra, however the judge saw it
    # the merge gets the winning position itself, not a side letter it can't look up
    merge_text = [p for p in llm.prompts if "Combine the material" in p][-1]
    assert "capital: Canberra (ruled correct" in merge_text and "side A" not in merge_text


def test_judge_rulings_are_matched_to_points_not_trusted_by_order(llm, monkeypatch):
    two = ('{"verdict":"partial","agreements":[],"unique_a":[],"unique_b":[],"conflicts":['
           '{"topic":"capital","a_position":"Canberra","b_position":"Sydney"},'
           '{"topic":"population","a_position":"27 million","b_position":"30 million"}]}')
    llm.on(purpose="compare", reply=two)
    # out of order, one point missing, one with an invented winner
    llm.on(purpose="judge", reply='{"rulings":[{"point":2,"winner":"B","reason":"census"},'
                                  '{"point":7,"winner":"maybe"}],"analysis":"x"}')
    monkeypatch.setattr("app.orchestrator.random.random", lambda: 0.9)
    rulings = stages(run(), "judge")[0]["rulings"]
    assert [(r["topic"], r["winner"]) for r in rulings] == [("capital", "unverified"), ("population", "B")]


def test_union_mode_never_debates(llm):
    llm.on(purpose="compare", reply=COMPARE_CONFLICT)
    out = run("union")
    assert out["outcome"] == "union" and llm.count(purpose="debate") == 0 and llm.count(purpose="judge") == 0


def test_single_mode_uses_side_a_only(llm):
    out = run("single")
    assert out["outcome"] == "single" and out["final"] == "answer from alpha/a1"
    assert llm.count(provider="beta") == 0


def test_one_side_failing_is_labelled_degraded_not_consensus(llm):
    llm.on(provider="beta", error=ProviderError("401", ErrorKind.AUTH))
    try:
        out = run()
    finally:
        _join_stragglers()
    assert out["outcome"] == "degraded" and out["degraded"] == ["side_b_failed"]
    assert out["final"] == "answer from alpha/a1"
    err = [s for s in stages(out, "error") if s["where"] == "answer_B"]
    assert err and err[0]["kind"] == "auth"


def test_substitute_model_stands_in_for_a_failed_side(llm):
    llm.on(provider="beta", model="b1", error=ProviderError("quota", ErrorKind.QUOTA_EXHAUSTED))
    out = run()
    assert out["outcome"] == "agreed" and out["degraded"] == []
    b = [s for s in stages(out, "answer") if s["side"] == "B"][0]
    assert b["model"] == "b2" and b["substituted_from"] == "beta/b1"


def test_both_sides_failing_is_an_error(llm):
    llm.on(purpose="answer", error=ProviderError("503", ErrorKind.SERVER))
    out = run()
    assert out["outcome"] == "error"


def test_transient_errors_are_retried_inside_a_call(llm):
    llm.on(provider="alpha", purpose="answer", error=ProviderError("503", ErrorKind.SERVER), times=1)
    out = run()
    assert out["outcome"] == "agreed"
    a = [s for s in stages(out, "answer") if s["side"] == "A"][0]
    assert a["attempts"] == 2


def test_comparator_falls_back_to_the_next_utility_model(llm):
    llm.on(provider="util", model="u1", error=ProviderError("503", ErrorKind.SERVER))
    out = run()
    assert out["outcome"] == "agreed" and llm.count(provider="util", model="u2", purpose="compare") == 1


def test_comparator_failure_shows_both_answers(llm):
    llm.on(purpose="compare", reply="this is not json")
    out = run()
    assert out["outcome"] == "degraded" and out["degraded"] == ["comparator_failed"]
    assert "answer from alpha/a1" in out["final"] and "answer from beta/b1" in out["final"]


def test_judge_failure_is_degraded_with_unresolved_positions(llm):
    llm.on(purpose="compare", reply=COMPARE_CONFLICT)
    llm.on(purpose="judge", error=ProviderError("503", ErrorKind.SERVER))
    out = run()
    assert out["outcome"] == "degraded" and "judge_failed" in out["degraded"]


def test_merge_failure_falls_back_to_side_by_side(llm):
    llm.on(purpose="merge", error=ProviderError("503", ErrorKind.SERVER))
    out = run()
    assert out["outcome"] == "degraded" and out["degraded"] == ["merge_failed"]


def test_debate_is_cut_short_to_protect_the_time_budget(llm, monkeypatch):
    llm.on(purpose="compare", reply=COMPARE_CONFLICT)
    import app.orchestrator as orch

    real = orch.pipeline_setting
    monkeypatch.setattr(orch, "pipeline_setting",
                        lambda k, d: 400 if k == "reserve_judge_s" else real(k, d))
    out = run(deadline_s=420)
    assert stages(out, "debate") == [] and stages(out, "note")
    assert out["outcome"] == "judged"  # the judge still settles it inside the budget


def test_breaker_opens_and_later_runs_skip_the_broken_model(llm):
    from app.resilience import BREAKERS, BreakerState

    llm.on(provider="beta", model="b1", error=ProviderError("503", ErrorKind.SERVER))
    run()      # 2 attempts on b1, then substitute b2
    run()      # 1 more failure on b1 -> threshold 3 -> open
    assert BREAKERS.get("beta/b1").state is BreakerState.OPEN
    before = llm.count(provider="beta", model="b1")
    out = run()
    assert llm.count(provider="beta", model="b1") == before  # never called while open
    assert [s for s in stages(out, "answer") if s["side"] == "B"][0]["model"] == "b2"


def test_an_overloaded_side_is_answered_by_its_substitute_at_once(llm):
    llm.on(provider="beta", model="b1", error=ProviderError("gemini 503: high demand", ErrorKind.SERVER, status=503))
    out = run()
    assert llm.count(provider="beta", model="b1") == 1   # asked once, not retried
    b = [s for s in stages(out, "answer") if s["side"] == "B"][0]
    assert b["model"] == "b2" and b["substituted_from"] == "beta/b1"
    assert out["degraded"] == []


def test_disabled_provider_is_reported_not_crashing(llm):
    from app.orchestrator import run_pipeline

    out = run_pipeline("q", "", A, {"provider": "off", "model": "o1", "effort": "low"}, "consensus")
    assert out["outcome"] == "degraded" and out["degraded"] == ["side_b_failed"]


@pytest.mark.parametrize("compare_reply", ["```json\n" + COMPARE_AGREE + "\n```", "noise " + COMPARE_AGREE])
def test_comparator_json_is_parsed_robustly(llm, compare_reply):
    llm.on(purpose="compare", reply=compare_reply)
    assert run()["outcome"] == "agreed"


def _join_stragglers():
    """A cut-off call keeps running in its worker thread; don't let it leak into the next test."""
    import threading

    for t in threading.enumerate():
        if t.name.startswith("side") and t is not threading.current_thread():
            t.join(5)


def test_a_hung_side_is_cut_off_once_the_other_answers(llm, monkeypatch):
    """Game day "claude-hangs": without a cutoff, users waited 300-600 s for the
    answer the other model gave in 25 s."""
    import threading
    import time

    import app.orchestrator as orch

    release = threading.Event()
    real_setting = orch.pipeline_setting
    monkeypatch.setattr(orch, "pipeline_setting",
                        lambda key, default: 0.3 if key == "straggler_grace_s" else real_setting(key, default))
    monkeypatch.setattr(orch, "POLL_S", 0.01)
    llm.on(provider="alpha", purpose="answer", reply=lambda prompt: (release.wait(10), "late")[1])
    t0 = time.monotonic()
    try:
        out = run()
    finally:
        release.set()
        _join_stragglers()
    assert time.monotonic() - t0 < 5
    assert out["outcome"] == "degraded" and out["degraded"] == ["side_a_failed"]
    err = stages(out, "error")[0]
    assert err["side"] == "A" and err["kind"] == "timeout" and err["straggler"] is True
    assert stages(out, "answer")[0]["side"] == "B"
    from prometheus_client import REGISTRY

    assert REGISTRY.get_sample_value("crossfact_straggler_cutoffs_total",
                                     {"provider": "alpha", "model": "a1", "stage": "answer"}) >= 1


def test_no_cutoff_while_the_only_remaining_side_is_still_working(llm, monkeypatch):
    """If one side failed, the other is all we have: wait for it (within the run budget)."""
    import app.orchestrator as orch

    real_setting = orch.pipeline_setting
    monkeypatch.setattr(orch, "pipeline_setting",
                        lambda key, default: 0.0 if key == "straggler_grace_s" else real_setting(key, default))
    llm.on(provider="beta", error=ProviderError("401", ErrorKind.AUTH))
    try:
        out = run()
    finally:
        _join_stragglers()
    assert out["outcome"] == "degraded" and out["degraded"] == ["side_b_failed"]
    assert stages(out, "answer")[0]["side"] == "A"


def test_a_step_refused_for_time_is_reported_as_deadline(llm):
    """Game day "gemini-slow": a merge refused for lack of budget said merge_failed."""
    llm.on(purpose="merge", error=ProviderError("only 3s left in the run budget", ErrorKind.DEADLINE))
    out = run()
    assert out["outcome"] == "degraded" and out["degraded"] == ["deadline"]
    assert "time budget" in out["final"]


def test_a_merge_that_breaks_is_still_merge_failed(llm):
    llm.on(purpose="merge", error=ProviderError("503", ErrorKind.SERVER))
    out = run()
    assert out["outcome"] == "degraded" and out["degraded"] == ["merge_failed"]


def test_a_judge_without_its_key_is_skipped_not_called(llm, monkeypatch):
    """An optional provider you haven't signed up for must not cost a failed call (or
    fire the auth alert): the chain goes straight to the next configured judge."""
    from app.config import registry

    reg = registry()
    monkeypatch.setitem(reg["providers"]["judge"], "secret", "judge_api_key")  # never set in the test env
    monkeypatch.setitem(reg["pipeline"], "judge_fallbacks", [{"provider": "alpha", "model": "a1", "effort": "high"}])
    llm.on(purpose="compare", reply=COMPARE_CONFLICT)
    out = run()
    assert llm.count(provider="judge") == 0
    assert stages(out, "judge")[0]["judge"] == "alpha/a1"


def test_no_configured_judge_degrades_with_a_reason(llm, monkeypatch):
    from app.config import registry

    reg = registry()
    monkeypatch.setitem(reg["providers"]["judge"], "secret", "judge_api_key")
    monkeypatch.setitem(reg["pipeline"], "judge_fallbacks", [])
    llm.on(purpose="compare", reply=COMPARE_CONFLICT)
    out = run()
    assert out["outcome"] == "degraded" and "judge_failed" in out["degraded"]
    assert any(e.get("kind") == "not_configured" for e in stages(out, "error"))


def test_auto_lets_side_a_answer_a_calculation_alone(llm):
    from prometheus_client import REGISTRY

    from app.orchestrator import ROUTE_NOTE

    seen = []
    llm.on(provider="alpha", purpose="answer", reply=lambda p: (seen.append(p), "[direct]\n2 + 3 = 5")[1])
    before = REGISTRY.get_sample_value("crossfact_runs_total", {"mode": "single", "outcome": "single"}) or 0
    out = run(mode="auto")
    assert out["outcome"] == "single" and out["route"] == "direct" and out["final"] == "2 + 3 = 5"
    assert ROUTE_NOTE.strip() in seen[0]                      # side A was asked to route
    assert llm.count(purpose="compare") == 0 and llm.count(purpose="debate") == 0
    assert [s["side"] for s in stages(out, "answer")] == ["A"] and stages(out, "note")
    after = REGISTRY.get_sample_value("crossfact_runs_total", {"mode": "single", "outcome": "single"})
    assert after == before + 1                                 # counted as a single-model run


@pytest.mark.parametrize("reply", ["[direct]\n5", "[direct] 5", "**[direct]**\n5", "  [DIRECT]\n\n5"])
def test_the_mark_is_found_however_side_a_writes_it(llm, reply):
    llm.on(provider="alpha", purpose="answer", reply=reply)
    out = run(mode="auto")
    assert out["route"] == "direct" and out["final"] == "5"


def test_the_mark_only_counts_at_the_start(llm):
    llm.on(provider="alpha", purpose="answer", reply="It depends. Some would answer [direct] here.")
    assert run(mode="auto")["route"] == "consensus"

def test_auto_runs_the_full_consensus_when_side_a_does_not_answer_alone(llm):
    from app.orchestrator import ROUTE_NOTE

    out = run(mode="auto")
    assert out["outcome"] == "agreed" and out["route"] == "consensus" and llm.count(purpose="compare") == 1
    assert sum(ROUTE_NOTE.strip() in p for p in llm.prompts) == 1   # side A only, never side B


def test_consensus_mode_never_takes_the_fast_path(llm):
    from app.orchestrator import ROUTE_NOTE

    llm.on(provider="alpha", purpose="answer", reply="[direct]\n5")
    out = run()
    assert out["route"] is None and out["outcome"] == "agreed"
    assert not any(ROUTE_NOTE.strip() in p for p in llm.prompts)
