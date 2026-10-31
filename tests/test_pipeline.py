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


# ---- mode "auto": the routes -------------------------------------------------------------

def auto(question="What is the capital of Australia?", **kw):
    from app.orchestrator import run_pipeline

    return run_pipeline(question, "", A, B, "auto", None, **kw)


def runs(mode, outcome):
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value("crossfact_runs_total", {"mode": mode, "outcome": outcome}) or 0


def test_auto_computes_exact_questions_in_code_without_any_model(llm):
    before = runs("exact", "exact")
    out = auto("What is 2+3?")
    assert out["outcome"] == "exact" and out["route"] == "exact" and out["final"] == "5"
    assert llm.calls == []                                      # no model was asked anything
    assert stages(out, "exact")[0]["working"] == "2 + 3 = 5"
    assert out["route_note"] == "Computed in code, not by a model: 2 + 3 = 5"
    assert runs("exact", "exact") == before + 1                 # counted as its own route, not as consensus


def test_the_exact_route_reads_the_question_not_the_instruction(llm):
    """The benchmark adds "reply with only the answer" after every question."""
    out = auto("What is 487 multiplied by 36?", instruction="Reply with only the answer.")
    assert out["route"] == "exact" and out["final"] == "17532" and llm.calls == []


def test_other_modes_never_compute_in_code(llm):
    from app.orchestrator import run_pipeline

    out = run_pipeline("What is 2+3?", "", A, B, "consensus", None)
    assert out["outcome"] == "agreed" and out["route"] is None and llm.count(purpose="answer") == 2


def test_a_short_fact_both_models_give_is_the_answer_with_no_comparison_or_debate(llm):
    from app.orchestrator import FACT_NOTE

    llm.on(provider="alpha", purpose="answer", reply="[fact]\nCanberra")
    llm.on(provider="beta", purpose="answer", reply="[fact]\nCanberra.")
    before = runs("fact", "agreed")
    out = auto()
    assert out["outcome"] == "agreed" and out["route"] == "fact" and out["final"] == "Canberra"
    assert sum(FACT_NOTE.strip() in p for p in llm.prompts) == 2     # both sides got the routing note
    assert [c["purpose"] for c in llm.calls] == ["answer", "answer"]  # nothing else: no compare, debate, merge
    assert [s["text"] for s in stages(out, "answer")] == ["Canberra", "Canberra."]  # the mark isn't shown
    assert out["route_note"] == "Short fact: alpha/a1 and beta/b1 each gave this answer on their own."
    assert stages(out, "fact")[0]["answers"] == [{"model": "alpha/a1", "answer": "Canberra"},
                                                 {"model": "beta/b1", "answer": "Canberra."}]
    assert runs("fact", "agreed") == before + 1


@pytest.mark.parametrize("reply", ["[fact]\nCanberra", "[fact] Canberra", "**[fact]**\n\nCanberra",
                                   "  [FACT]\nCanberra\nIt became the capital in 1913.",
                                   "<think>easy</think>[fact]\nCanberra"])
def test_the_mark_is_found_however_a_model_writes_it(llm, reply):
    llm.on(purpose="answer", reply=reply)
    out = auto()
    assert out["route"] == "fact" and out["final"] == "Canberra"


def test_the_mark_only_counts_at_the_start(llm):
    llm.on(purpose="answer", reply="It depends. Some would write [fact] here.")
    assert auto()["route"] == "consensus"


def test_answers_worded_differently_get_one_check_and_the_answer_claims_no_more_than_both(llm):
    llm.on(provider="alpha", purpose="answer", reply="[fact]\nCanberra, Australia")
    llm.on(provider="beta", purpose="answer", reply="[fact]\nCanberra")
    llm.on(purpose="same", reply='{"same": true}')
    out = auto()
    assert out["outcome"] == "agreed" and out["final"] == "Canberra" and llm.count(purpose="same") == 1


@pytest.mark.parametrize(("a", "b", "outcome"), [("1,048,576", "1048576", "agreed"), ("2.50", "2.5", "agreed"),
                                                 ("42", "41", "unsure")])
def test_numbers_are_compared_as_numbers_without_a_model(llm, a, b, outcome):
    llm.on(provider="alpha", purpose="answer", reply=f"[fact]\n{a}")
    llm.on(provider="beta", purpose="answer", reply=f"[fact]\n{b}")
    out = auto("How many?")
    assert out["outcome"] == outcome and llm.count(purpose="same") == 0 and llm.count(purpose="vote") == 0


def test_two_different_answers_are_not_sure_and_say_what_each_model_said(llm):
    llm.on(provider="alpha", purpose="answer", reply="[fact]\nSydney")
    llm.on(provider="beta", purpose="answer", reply="[fact]\nCanberra")
    llm.on(purpose="same", reply='{"same": false}')
    before = runs("fact", "unsure")
    out = auto()
    assert out["outcome"] == "unsure" and out["final"] == "Not sure." and out["degraded"] == []
    assert out["route_note"] == "alpha/a1: Sydney · beta/b1: Canberra. No source settled it."
    # by the 2-of-3 rule a third model couldn't settle two different answers, so none is asked
    assert llm.count(purpose="vote") == 0 and llm.count(purpose="judge") == 0
    assert runs("fact", "unsure") == before + 1


def test_a_model_that_is_not_sure_brings_in_a_third_whose_agreement_counts(llm):
    llm.on(provider="alpha", purpose="answer", reply="[fact]\nCanberra")
    llm.on(provider="beta", purpose="answer", reply="[fact]\n?")
    llm.on(provider="judge", purpose="vote", reply="Canberra")
    out = auto()
    assert out["outcome"] == "majority" and out["final"] == "Canberra"
    third = [s for s in stages(out, "answer") if s["side"] == "C"][0]
    assert (third["provider"], third["text"]) == ("judge", "Canberra")
    vote_prompt = next(p for p in llm.prompts if "reply with exactly: ?" in p)
    assert "Canberra" not in vote_prompt and "[fact]" not in vote_prompt  # it answers on its own
    assert out["route_note"] == ("Short fact: alpha/a1 and judge/j1 each gave this answer on their own; "
                                 "beta/b1 wasn't sure.")


@pytest.mark.parametrize("third", ["Sydney", "?", "I don't know"])
def test_a_third_model_that_does_not_agree_leaves_it_not_sure(llm, third):
    llm.on(provider="alpha", purpose="answer", reply="[fact]\nCanberra")
    llm.on(provider="beta", purpose="answer", reply="[fact]\nI don't know")
    llm.on(provider="judge", purpose="vote", reply=third)
    llm.on(purpose="same", reply='{"same": false}')
    out = auto()
    assert out["outcome"] == "unsure" and out["final"] == "Not sure."


def test_when_no_model_is_sure_nothing_more_is_asked(llm):
    llm.on(purpose="answer", reply="[fact]\n?")
    out = auto()
    assert out["outcome"] == "unsure" and out["final"] == "Not sure."
    assert out["route_note"] == "alpha/a1: not sure · beta/b1: not sure."
    assert [c["purpose"] for c in llm.calls] == ["answer", "answer"]


def test_a_failed_side_is_covered_by_the_third_model(llm):
    llm.on(provider="beta", error=ProviderError("401", ErrorKind.AUTH))
    llm.on(provider="alpha", purpose="answer", reply="[fact]\nCanberra")
    llm.on(provider="judge", purpose="vote", reply="Canberra")
    out = auto()
    assert out["outcome"] == "agreed" and out["final"] == "Canberra" and out["degraded"] == []
    assert stages(out, "error")[0]["side"] == "B"
    assert "(beta/b1 failed)" in out["route_note"]


def test_a_failed_side_with_no_third_model_is_degraded_not_passed_off_as_checked(llm):
    llm.on(provider="beta", error=ProviderError("401", ErrorKind.AUTH))
    llm.on(provider="judge", error=ProviderError("401", ErrorKind.AUTH))
    llm.on(provider="alpha", purpose="answer", reply="[fact]\nCanberra")
    out = auto()
    assert out["outcome"] == "degraded" and out["degraded"] == ["side_b_failed"] and out["final"] == "Canberra"


def test_a_failed_same_answer_check_shows_both_answers(llm):
    llm.on(provider="alpha", purpose="answer", reply="[fact]\nCanberra, ACT")
    llm.on(provider="beta", purpose="answer", reply="[fact]\nCanberra")
    llm.on(purpose="same", error=ProviderError("boom", ErrorKind.SERVER))
    out = auto()
    assert out["outcome"] == "degraded" and out["degraded"] == ["comparator_failed"]
    assert out["final"] == "alpha/a1: Canberra, ACT\nbeta/b1: Canberra"


def test_one_short_answer_and_one_full_answer_run_the_consensus(llm):
    llm.on(provider="alpha", purpose="answer", reply="[fact]\nCanberra")
    llm.on(provider="beta", purpose="answer", reply="The capital of Australia is Canberra, since 1913.")
    out = auto()
    assert out["route"] == "consensus" and out["outcome"] == "agreed" and llm.count(purpose="compare") == 1
    compare = next(p for p in llm.prompts if "strict comparator" in p)
    assert "ANSWER A:\nCanberra\n" in compare and "[fact]" not in compare


def test_unmarked_answers_run_the_full_consensus(llm):
    out = auto("Should I learn Rust or Go first?")
    assert out["route"] == "consensus" and out["outcome"] == "agreed" and llm.count(purpose="compare") == 1


def test_a_slow_side_gets_seconds_not_minutes_once_the_other_gave_a_short_fact(llm, monkeypatch):
    import threading
    import time

    import app.orchestrator as orch

    release = threading.Event()
    real_setting = orch.pipeline_setting
    monkeypatch.setattr(orch, "pipeline_setting",
                        lambda key, default: 0.3 if key == "fact_grace_s" else real_setting(key, default))
    monkeypatch.setattr(orch, "POLL_S", 0.01)
    llm.on(provider="alpha", purpose="answer", reply="[fact]\nCanberra")
    llm.on(provider="beta", purpose="answer", reply=lambda prompt: (release.wait(10), "[fact]\nCanberra")[1])
    llm.on(provider="judge", purpose="vote", reply="Canberra")
    t0 = time.monotonic()
    try:
        out = auto()  # straggler_grace_s is 150 s; the short fact waits 0.3 s
    finally:
        release.set()
        _join_stragglers()
    assert time.monotonic() - t0 < 5
    assert out["outcome"] == "agreed" and stages(out, "error")[0]["straggler"] is True


def test_short_fact_helpers():
    from app.orchestrator import _fact_answer, _key, _unsure

    assert _fact_answer("[fact]\n") == "?" and _fact_answer("Canberra") is None
    assert _key("The Beatles.") == _key("beatles") and _key("Zürich") == _key("Zurich")
    assert _key("北京") != _key("上海")  # accents go, other scripts stay
    assert all(_unsure(x) for x in ("?", " ?? ", "I don't know.", "Not sure", "unknown", ""))
    assert not any(_unsure(x) for x in ("Canberra", "0", "No"))


def test_a_failed_check_of_the_third_models_answer_is_degraded_not_a_verdict(llm):
    llm.on(provider="alpha", purpose="answer", reply="[fact]\nCanberra")
    llm.on(provider="beta", purpose="answer", reply="[fact]\n?")
    llm.on(provider="judge", purpose="vote", reply="Canberra, ACT")
    llm.on(purpose="same", error=ProviderError("boom", ErrorKind.SERVER))
    out = auto()
    assert out["outcome"] == "degraded" and out["degraded"] == ["comparator_failed"] and out["final"] == "Not sure."


# ---- the asker's date, and the judge's labels ------------------------------------------

def test_with_the_askers_date_days_until_christmas_is_exact(llm):
    from datetime import date

    out = auto("How many days until Christmas?", today=date(2026, 10, 30), tz="America/New_York")
    assert out["route"] == "exact" and out["final"] == "56 days" and llm.calls == []
    assert "(America/New_York)" in out["route_note"]


def test_every_model_is_told_the_askers_date_and_the_benchmark_never_is(llm):
    from datetime import date

    auto("Who is the current UN Secretary-General?", today=date(2026, 10, 30), tz="America/New_York")
    dated = "(Today is Friday, October 30, 2026, time zone America/New_York, where the person asking is.)"
    assert len(llm.prompts) >= 2 and all(dated in p for p in llm.prompts)
    llm.prompts.clear()
    auto("Who is the current UN Secretary-General?")
    assert not any("Today is" in p for p in llm.prompts)


def test_the_judges_explanation_names_the_real_sides(llm, monkeypatch):
    llm.on(purpose="compare", reply=COMPARE_CONFLICT)
    llm.on(purpose="judge", reply=json.dumps({
        "rulings": [{"point": 1, "winner": "A", "reason": "Answer A cites a source; answer B does not."}],
        "analysis": "Answer A is better supported than Answer B."}))
    monkeypatch.setattr("app.orchestrator.random.random", lambda: 0.0)  # shown swapped: its "A" is side B
    judge = stages(run(), "judge")[0]
    assert judge["rulings"][0]["winner"] == "B"
    assert judge["rulings"][0]["reason"] == "Side B cites a source; side A does not."
    assert judge["analysis"] == "Side B is better supported than Side A."
