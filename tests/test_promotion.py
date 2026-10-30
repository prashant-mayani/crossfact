"""Preferred models: start on the dependable one, move to the preferred one after
background checks pass, hand back at the first failure (app/promotion.py)."""
import json
import re

import pytest

from tests.conftest import TEST_PROVIDERS, enroll

PREFER = "{model: b1, check_every_s: 100, passes: 2, timeout_s: 5}"
WITH_PREFER = TEST_PROVIDERS.replace(
    "side_b: {provider: beta, model: b1, effort: medium}",
    f"side_b: {{provider: beta, model: b2, effort: medium, prefer: {PREFER}}}")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def preferring(env, llm):
    """The test registry, with side B on b2 and b1 preferred."""
    from app import config
    from app.promotion import PROMOTIONS

    assert WITH_PREFER != TEST_PROVIDERS
    (env / "providers.yaml").write_text(WITH_PREFER)
    config.reload()
    PROMOTIONS.reset()
    yield llm
    PROMOTIONS.reset()


@pytest.fixture
def promo(preferring):
    from app.promotion import Promotions

    clock = Clock()
    return Promotions(clock=clock), clock


def side_b(p):
    return p.effective_defaults()["side_b"]


def test_starts_on_the_dependable_model_and_hides_prefer(promo):
    p, _ = promo
    assert side_b(p) == {"provider": "beta", "model": "b2", "effort": "medium"}


def test_promotes_only_after_passes_in_a_row_spaced_by_the_interval(promo, preferring):
    from app.providers import breaker_for

    p, clock = promo
    b = breaker_for("beta", "b1")
    for _ in range(3):
        b.record_failure()  # earlier real failures left it open
    p.tick()                                    # first check: due at once
    assert preferring.count(model="b1") == 1 and side_b(p)["model"] == "b2"
    clock.now += 50
    p.tick()                                    # not due yet
    assert preferring.count(model="b1") == 1
    clock.now += 50
    p.tick()                                    # second pass: promoted
    assert preferring.count(model="b1") == 2
    assert side_b(p)["model"] == "b1"
    assert b.snapshot()["state"] == "closed"    # the open breaker was cleared on promotion


def test_a_failed_check_restarts_the_streak(promo, preferring):
    from app.resilience import ErrorKind, ProviderError

    p, clock = promo
    p.tick()
    preferring.on(model="b1", error=ProviderError("gemini 503: high demand", ErrorKind.SERVER), times=1)
    clock.now += 100
    p.tick()
    assert p.snapshot()["side_b"]["streak"] == 0 and "503" in p.snapshot()["side_b"]["last"]
    clock.now += 100
    p.tick()
    assert side_b(p)["model"] == "b2"           # one pass since the failure: not yet
    clock.now += 100
    p.tick()
    assert side_b(p)["model"] == "b1"


def test_no_checks_while_promoted_and_a_real_failure_hands_back(promo, preferring):
    from app.providers import breaker_for

    p, clock = promo
    p.tick()
    clock.now += 100
    p.tick()
    assert side_b(p)["model"] == "b1"
    calls = preferring.count(model="b1")
    clock.now += 1000
    p.tick()
    assert preferring.count(model="b1") == calls  # in use: real questions are the test
    breaker_for("beta", "b1").record_failure()   # one failed attempt in a real question
    assert side_b(p)["model"] == "b2"
    assert p.snapshot()["side_b"]["promoted"] is False
    p.tick()
    assert preferring.count(model="b1") == calls  # the next check waits a full interval
    clock.now += 100
    p.tick()
    assert preferring.count(model="b1") == calls + 1


def test_a_preferred_model_the_provider_does_not_offer_is_ignored(env, llm):
    from app import config
    from app.promotion import Promotions, preferences

    (env / "providers.yaml").write_text(WITH_PREFER.replace("{model: b1,", "{model: nope,"))
    config.reload()
    assert preferences() == {}
    p = Promotions()
    p.tick()
    assert llm.calls == []
    assert Promotions().effective_defaults()["side_b"]["model"] == "b2"


def test_the_chat_page_starts_new_questions_on_the_effective_default(preferring, client):
    from app.promotion import PROMOTIONS, preferences

    def boot():
        page = client.get("/chat").text
        return json.loads(re.search(r'<script type="application/json" id="boot">(.*?)</script>', page, re.S)
                          .group(1))["defaults"]["side_b"]

    enroll(client)
    assert boot() == {"provider": "beta", "model": "b2", "effort": "medium"}
    d = preferences()["side_b"]
    PROMOTIONS.check("side_b", d)
    PROMOTIONS.check("side_b", d)
    assert boot()["model"] == "b1"
