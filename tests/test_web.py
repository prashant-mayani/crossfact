"""HTTP behaviour: auth, the one account, headers, the ask->poll loop, drain, quotas, faults, metrics."""
import re
import time

from prometheus_client import generate_latest


def wait_done(client, mid, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/messages/{mid}").json()
        if d["status"] != "running":
            return d
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def ask(client, csrf, question="What is x?", mode="consensus", thread_id="", **sides):
    data = {"question": question, "thread_id": thread_id, "mode": mode, "csrf": csrf,
            "a_provider": "alpha", "a_model": "a1", "a_effort": "medium",
            "b_provider": "beta", "b_model": "b1", "b_effort": "medium", **sides}
    return client.post("/ask", data=data)


def test_security_headers_and_probes(client):
    r = client.get("/login")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/readyz").json() == {"ready": True}


def test_login_lockout_after_repeated_failures(client):
    for _ in range(5):
        r = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert r.status_code == 401
    r = client.post("/login", data={"username": "admin", "password": "test-password-123"})
    assert r.status_code == 429


def test_enrollment_qr_works_under_the_csp(client):
    client.post("/login", data={"username": "admin", "password": "test-password-123"}, follow_redirects=False)
    page = client.get("/totp").text
    svg = page[page.index("<svg"):page.index("</svg>")]
    assert "style=" not in svg and "<script" not in svg


def test_full_ask_poll_render_loop(client, admin, llm):
    r = ask(client, admin)
    assert r.status_code == 200, r.text
    mid, tid = r.json()["message_id"], r.json()["thread_id"]
    d = wait_done(client, mid)
    assert d["status"] == "done" and d["pipeline"]["outcome"] == "agreed" and d["content"] == "merged answer"
    page = client.get(f"/chat/{tid}").text
    assert 'class="chip agreed"' in page and "Pipeline transcript" in page
    assert "<script>" not in page  # boot data is a JSON block, not inline script


def test_auto_is_the_default_mode_and_says_when_one_model_answered(client, admin, llm):
    page = client.get("/chat").text
    assert page.index('value="auto"') < page.index('value="consensus"')
    llm.on(provider="alpha", purpose="answer", reply="[direct]\n5")
    r = ask(client, admin, mode="auto")
    d = wait_done(client, r.json()["message_id"])
    assert d["pipeline"]["route"] == "direct" and d["content"] == "5"
    assert "Answered by side A alone" in client.get(f"/chat/{r.json()['thread_id']}").text


def test_degraded_run_is_labelled_and_offers_retry(client, admin, llm):
    from app.resilience import ErrorKind, ProviderError

    llm.on(provider="beta", error=ProviderError("401", ErrorKind.AUTH))
    r = ask(client, admin)
    d = wait_done(client, r.json()["message_id"])
    assert d["pipeline"]["outcome"] == "degraded" and d["pipeline"]["degraded"] == ["side_b_failed"]
    page = client.get(f"/chat/{r.json()['thread_id']}").text
    assert "not a consensus" in page and 'class="retry"' in page


def test_failed_runs_do_not_cost_quota(client, admin, llm):
    from app import db
    from app.resilience import ErrorKind, ProviderError

    llm.on(purpose="answer", error=ProviderError("503", ErrorKind.SERVER))
    r = ask(client, admin)
    d = wait_done(client, r.json()["message_id"])
    assert d["status"] == "error"
    assert db.usage_today(db.get_user(username="admin")["id"]) == 0


def test_single_user_there_is_no_way_to_add_accounts(client, admin):
    r = client.post("/admin/users", data={"username": "guest", "password": "another-pass-1", "csrf": admin})
    assert r.status_code in (404, 405)
    page = client.get("/admin").text
    assert "Create user" not in page and "Fault injection" in page and "app.admin reset-2fa" in page


def test_accounts_from_a_multi_user_version_are_disabled_at_startup(env):
    from fastapi.testclient import TestClient

    from app import auth, db
    from app.main import app

    db.init_db()
    owner = db.create_user("prashant", auth.hash_pw("owner-password-1"), role="admin")
    db.create_user("guest", auth.hash_pw("guest-password-1"))
    with TestClient(app, base_url="https://testserver") as c:  # startup runs ensure_owner
        assert db.get_user(username="guest")["disabled"] == 1
        assert db.get_user(uid=owner)["disabled"] == 0
        assert c.post("/login", data={"username": "guest", "password": "guest-password-1"}).status_code == 401
        assert c.post("/login", data={"username": "prashant", "password": "owner-password-1"},
                      follow_redirects=False).status_code == 303
    assert db.get_user(username="admin") is None  # the existing owner is kept, not replaced


def test_csrf_and_input_validation(client, admin, llm):
    assert ask(client, "bad-token").status_code == 403
    assert ask(client, admin, mode="nonsense").status_code == 400
    assert ask(client, admin, a_effort="extreme").status_code == 400
    assert ask(client, admin, question="   ").status_code == 400


def test_draining_refuses_new_runs_but_keeps_serving(client, admin, llm):
    from app.lifecycle import LIFECYCLE

    r = ask(client, admin)
    mid = r.json()["message_id"]
    wait_done(client, mid)
    LIFECYCLE.draining.set()
    try:
        r = ask(client, admin)
        assert r.status_code == 503 and "restarting" in r.json()["detail"]
        assert client.get(f"/api/messages/{mid}").status_code == 200  # polls still work
        assert client.get("/readyz").status_code == 503
    finally:
        LIFECYCLE.draining.clear()


def test_daily_quota(client, admin, llm, monkeypatch):
    from app import config

    cfg = config.cfg()
    monkeypatch.setitem(cfg["quotas"], "daily_runs", 1)
    wait_done(client, ask(client, admin).json()["message_id"])
    r = ask(client, admin)
    assert r.status_code == 429 and "quota" in r.json()["detail"].lower()


def test_fault_injection_from_the_admin_page(client, admin, llm):
    r = client.post("/admin/faults", data={"target": "beta/b1", "mode": "error", "probability": "1",
                                           "latency_s": "0", "minutes": "5", "csrf": admin}, follow_redirects=False)
    assert r.status_code == 303
    assert "Fault injection is on" in client.get("/chat").text
    d = wait_done(client, ask(client, admin).json()["message_id"])
    b = [s for s in d["pipeline"]["stages"] if s["stage"] == "answer" and s["side"] == "B"][0]
    assert b["model"] == "b2" and b["substituted_from"] == "beta/b1"  # the substitute stepped in
    fid = re.search(r'action="/admin/faults/([0-9a-f]{32})/delete"', client.get("/admin").text).group(1)
    client.post(f"/admin/faults/{fid}/delete", data={"csrf": admin})
    assert "Fault injection is on" not in client.get("/chat").text


def test_metrics_describe_runs_and_calls(client, admin, llm):
    wait_done(client, ask(client, admin).json()["message_id"])
    text = generate_latest().decode()
    assert re.search(r'crossfact_runs_total\{mode="consensus",outcome="agreed"\} [1-9]', text)
    assert re.search(r'crossfact_provider_calls_total\{model="b1",provider="beta",purpose="answer",result="ok"\}',
                     text)
    assert 'crossfact_run_duration_seconds_bucket{le="240.0",mode="consensus",result="success"}' in text
    assert re.search(r'crossfact_http_requests_total\{method="POST",route="/ask",status="200"\}', text)


def test_a_run_is_in_flight_before_its_thread_starts(client, admin, llm, monkeypatch):
    """The drain race: SIGTERM between /ask returning and the run's thread starting used
    to find nothing in flight, exit, and leave the run for the next pod to call abandoned."""
    import app.main as main
    from app.lifecycle import LIFECYCLE

    real, pending = main._start_job, []
    monkeypatch.setattr(main, "_start_job", lambda *a: pending.append(a))  # the run's thread hasn't started yet
    r = ask(client, admin)
    assert r.status_code == 200
    mid = r.json()["message_id"]
    assert mid in LIFECYCLE.inflight()  # so a drain that starts now waits for it
    real(*pending[0])  # the thread gets its turn
    assert wait_done(client, mid)["status"] == "done" and mid not in LIFECYCLE.inflight()


def test_a_drain_that_starts_mid_request_refuses_the_run_uncharged(client, admin, llm, monkeypatch):
    from app import db
    from app.lifecycle import LIFECYCLE

    monkeypatch.setattr(LIFECYCLE, "begin", lambda mid: False)  # draining began after the first check
    r = ask(client, admin)
    assert r.status_code == 503 and "restarting" in r.text
    assert db.usage_today(db.get_user(username="admin")["id"]) == 0


def test_begin_and_drain_share_one_lock(env):
    import threading

    from app import db
    from app.lifecycle import LIFECYCLE

    db.init_db()  # drain() keeps the runs it waits for heartbeating
    assert LIFECYCLE.begin("m1")
    t = threading.Thread(target=LIFECYCLE.drain, args=(0.3,))
    t.start()
    for _ in range(100):
        if LIFECYCLE.draining.is_set():
            break
        time.sleep(0.01)
    assert LIFECYCLE.begin("m2") is False and LIFECYCLE.inflight() == ["m1"]
    LIFECYCLE.end("m1")
    t.join()


def test_without_a_bootstrap_password_sign_in_says_what_to_set(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app import secretstore
    from app.main import app

    monkeypatch.delenv("ADMIN_BOOTSTRAP_PASSWORD")
    secretstore.clear_cache()
    with TestClient(app, base_url="https://testserver") as c:
        assert "admin_bootstrap_password" in c.get("/login").text
        r = c.post("/login", data={"username": "admin", "password": "change-me-now"}, follow_redirects=False)
        assert r.status_code == 503 and "admin_bootstrap_password" in r.text  # no default password exists
        monkeypatch.setenv("ADMIN_BOOTSTRAP_PASSWORD", "set-in-vault-later-1")
        secretstore.clear_cache()  # the secret store re-reads Vault's copy within a minute
        r = c.post("/login", data={"username": "admin", "password": "set-in-vault-later-1"}, follow_redirects=False)
        assert r.status_code == 303  # the account is created at this first sign-in


def test_chats_can_be_deleted_from_the_sidebar(client, admin, llm):
    from app import db

    tids = []
    for q in ("first <b>chat</b>", "second"):
        r = ask(client, admin, question=q)
        wait_done(client, r.json()["message_id"])
        tids.append(r.json()["thread_id"])
    t1, t2 = tids
    page = client.get(f"/chat/{t2}").text
    assert f'action="/threads/{t1}/delete"' in page and f'action="/threads/{t2}/delete"' in page
    assert "data-confirm=" in page and "<b>chat</b>" not in page   # the title is escaped in the prompt
    assert client.post(f"/threads/{t1}/delete", data={"csrf": "wrong"}).status_code == 403
    # deleting another chat keeps you on the one you were reading
    r = client.post(f"/threads/{t1}/delete", data={"csrf": admin, "back": t2}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/chat/{t2}"
    assert client.get(f"/chat/{t1}").status_code == 404
    # deleting the open chat takes you to a new one, and its messages go with it
    r = client.post(f"/threads/{t2}/delete", data={"csrf": admin, "back": t2}, follow_redirects=False)
    assert r.headers["location"] == "/chat"
    assert db.thread_messages(t2) == [] and db.thread_messages(t1) == []
    assert 'action="/threads/' not in client.get("/chat").text


def test_each_build_gets_fresh_css_and_js_urls(client):
    from app.config import VERSION

    page = client.get("/login").text
    v = VERSION[:12]
    assert f'href="/static/app.css?v={v}"' in page and f'src="/static/app.js?v={v}"' in page
    r = client.get(f"/static/app.css?v={v}")
    assert r.status_code == 200 and ".delthread" in r.text
