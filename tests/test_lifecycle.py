"""Run ownership: migrations from v1, heartbeats, orphan sweeps with quota refunds."""
import json
import sqlite3
import time

from app import db


def _running(uid, age_s, owner="dead-pod"):
    tid = db.create_thread(uid, "t")
    db.add_message(tid, "user", "question?")
    mid = db.add_message(tid, "assistant", status="running", owner=owner,
                         pipeline={"stages": [], "mode": "consensus"})
    with db.conn() as c:
        c.execute("UPDATE messages SET heartbeat_at=?, created_at=? WHERE id=?",
                  (time.time() - age_s, time.time() - age_s, mid))
    return mid


def test_v1_database_upgrades_in_place(env):
    path = env / "data" / "crossfact.db"
    path.parent.mkdir(parents=True)
    c = sqlite3.connect(path)
    c.executescript(db.SCHEMA_V1)
    c.execute("INSERT INTO threads VALUES('t1','u1','old',0,0)")
    c.execute("INSERT INTO messages(id,thread_id,role,content,status,created_at) "
              "VALUES('m1','t1','assistant','','running',0)")
    c.commit()
    c.close()
    assert db.init_db() == db.SCHEMA_VERSION
    assert db.init_db() == db.SCHEMA_VERSION  # idempotent
    m = db.get_message("m1")
    assert "heartbeat_at" in m and m["owner"] is None
    assert db.stale_running(time.time())[0]["id"] == "m1"  # a v1 run left "running" is an orphan


def test_orphans_are_closed_and_quota_refunded(env):
    from app.lifecycle import LIFECYCLE

    db.init_db()
    uid = db.create_user("u", "x")
    db.bump_usage(uid)
    db.bump_usage(uid)
    dead = _running(uid, age_s=600)
    alive_elsewhere = _running(uid, age_s=5, owner="other-pod")
    assert LIFECYCLE.sweep_orphans() == 1
    m = db.get_message(dead)
    assert m["status"] == "error" and json.loads(m["pipeline_json"])["outcome"] == "abandoned"
    assert "Retry" in m["content"]
    assert db.usage_today(uid) == 1
    assert db.get_message(alive_elsewhere)["status"] == "running"
    assert LIFECYCLE.sweep_orphans() == 0  # nothing double-counted


def test_own_inflight_runs_are_never_swept(env):
    from app.lifecycle import LIFECYCLE

    db.init_db()
    uid = db.create_user("u", "x")
    mine = _running(uid, age_s=600, owner="me")
    LIFECYCLE.begin(mine)
    try:
        assert LIFECYCLE.sweep_orphans() == 0
        db.heartbeat([mine])
        assert db.get_message(mine)["heartbeat_at"] > time.time() - 5
    finally:
        LIFECYCLE.end(mine)


def test_mark_abandoned_is_first_writer_wins(env):
    db.init_db()
    uid = db.create_user("u", "x")
    mid = _running(uid, age_s=600)
    assert db.mark_abandoned(mid, "gone") is True
    assert db.mark_abandoned(mid, "gone") is False
