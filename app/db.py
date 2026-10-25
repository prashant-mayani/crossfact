"""SQLite persistence: one file at {data_dir}/crossfact.db in WAL mode.

During a rolling update two pods share this file on the same node (the PVC is
ReadWriteOnce, which is per node, not per pod). SQLite's file locking makes
that safe; the rules that keep it safe are:
  * schema changes are additive only (new tables, new nullable columns);
  * migrations run inside BEGIN IMMEDIATE, so two starting pods serialize;
  * a running message is owned by the pod that heartbeats it, and only a stale
    heartbeat makes it an orphan (see lifecycle.py).

App-level privacy: every user-facing query is scoped by user_id. Root on the
node can read the file; that boundary is documented in docs/DESIGN.md.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

from .config import cfg

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS users(
  id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  totp_secret_enc TEXT, role TEXT NOT NULL DEFAULT 'user',
  disabled INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  sid TEXT PRIMARY KEY, user_id TEXT NOT NULL, csrf TEXT NOT NULL,
  created_at REAL NOT NULL, expires_at REAL NOT NULL, totp_ok INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS login_attempts(
  key TEXT PRIMARY KEY, fails INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS threads(
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '', pipeline_json TEXT,
  status TEXT NOT NULL DEFAULT 'done',
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_at);
CREATE TABLE IF NOT EXISTS files(
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, orig_name TEXT NOT NULL,
  path TEXT NOT NULL, size INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS usage(
  user_id TEXT NOT NULL, day TEXT NOT NULL, runs INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user_id, day));
"""

# (version, statements). Additive only: a pod running the previous release must
# keep working against the new schema while it drains.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (2, [
        "ALTER TABLE messages ADD COLUMN owner TEXT",
        "ALTER TABLE messages ADD COLUMN heartbeat_at REAL",
        "ALTER TABLE messages ADD COLUMN started_at REAL",
        "ALTER TABLE messages ADD COLUMN finished_at REAL",
        "CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status)",
        """CREATE TABLE IF NOT EXISTS faults(
             id TEXT PRIMARY KEY, target TEXT NOT NULL, mode TEXT NOT NULL,
             probability REAL NOT NULL DEFAULT 1.0, latency_s REAL NOT NULL DEFAULT 0,
             until REAL NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL)""",
    ]),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def db_file() -> str:
    d = cfg()["data_dir"]
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "crossfact.db")


def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(db_file(), timeout=30, isolation_level=None)  # autocommit; explicit txns below
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA foreign_keys=ON")
    return c


@contextmanager
def conn():
    c = _connect()
    try:
        # IMMEDIATE: take the write lock up front. Deferred transactions that
        # upgrade from read to write can fail instantly with "database is locked"
        # when a second pod writes at the same moment; IMMEDIATE waits instead.
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def init_db() -> int:
    """Create/upgrade the schema. Safe to call from two pods at once."""
    c = _connect()
    try:
        c.execute("BEGIN IMMEDIATE")  # take the write lock before reading the version
        for stmt in [s for s in SCHEMA_V1.split(";") if s.strip()]:
            c.execute(stmt)
        version = c.execute("PRAGMA user_version").fetchone()[0]
        for target, statements in MIGRATIONS:
            if version >= target:
                continue
            for stmt in statements:
                try:
                    c.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):
                        raise
            c.execute(f"PRAGMA user_version={target}")
            version = target
        c.execute("COMMIT")
        return version
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def new_id() -> str:
    return uuid.uuid4().hex


def today() -> str:
    return time.strftime("%Y-%m-%d")


# ---- users ------------------------------------------------------------------

def create_user(username, pw_hash, role="user", totp_secret_enc=None) -> str:
    uid = new_id()
    with conn() as c:
        c.execute("INSERT INTO users(id,username,pw_hash,totp_secret_enc,role,created_at) VALUES(?,?,?,?,?,?)",
                  (uid, username, pw_hash, totp_secret_enc, role, time.time()))
    return uid


def get_user(username=None, uid=None):
    q, arg = ("username=?", username) if username else ("id=?", uid)
    with conn() as c:
        r = c.execute(f"SELECT * FROM users WHERE {q}", (arg,)).fetchone()
    return dict(r) if r else None


def all_users():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM users ORDER BY created_at")]


def update_user(uid, **fields):
    sets = ",".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), uid))


# ---- sessions / lockout -------------------------------------------------------

def create_session(uid, hours) -> dict:
    now = time.time()
    s = {"sid": new_id(), "user_id": uid, "csrf": new_id(), "created_at": now, "expires_at": now + hours * 3600}
    with conn() as c:
        c.execute("INSERT INTO sessions(sid,user_id,csrf,created_at,expires_at) VALUES(?,?,?,?,?)", tuple(s.values()))
    return s


def get_session(sid):
    with conn() as c:
        r = c.execute("SELECT * FROM sessions WHERE sid=? AND expires_at>?", (sid, time.time())).fetchone()
    return dict(r) if r else None


def mark_totp_ok(sid):
    with conn() as c:
        c.execute("UPDATE sessions SET totp_ok=1 WHERE sid=?", (sid,))


def drop_session(sid):
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE sid=?", (sid,))


def purge_sessions() -> int:
    with conn() as c:
        return c.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),)).rowcount


def lockout_check(key) -> float:
    with conn() as c:
        r = c.execute("SELECT locked_until FROM login_attempts WHERE key=?", (key,)).fetchone()
    return max(0.0, r["locked_until"] - time.time()) if r else 0.0


def lockout_fail(key, max_fails=5, lock_s=900):
    with conn() as c:
        c.execute("INSERT INTO login_attempts(key,fails) VALUES(?,1) "
                  "ON CONFLICT(key) DO UPDATE SET fails=fails+1", (key,))
        r = c.execute("SELECT fails FROM login_attempts WHERE key=?", (key,)).fetchone()
        if r["fails"] >= max_fails:
            c.execute("UPDATE login_attempts SET locked_until=?, fails=0 WHERE key=?", (time.time() + lock_s, key))


def lockout_clear(key):
    with conn() as c:
        c.execute("DELETE FROM login_attempts WHERE key=?", (key,))


# ---- threads / messages ---------------------------------------------------------

def create_thread(uid, title) -> str:
    tid, now = new_id(), time.time()
    with conn() as c:
        c.execute("INSERT INTO threads VALUES(?,?,?,?,?)", (tid, uid, title[:120], now, now))
    return tid


def user_threads(uid):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM threads WHERE user_id=? ORDER BY updated_at DESC", (uid,))]


def get_thread(tid, uid):
    with conn() as c:
        r = c.execute("SELECT * FROM threads WHERE id=? AND user_id=?", (tid, uid)).fetchone()
    return dict(r) if r else None


def delete_thread(tid, uid):
    with conn() as c:
        c.execute("DELETE FROM messages WHERE thread_id IN "
                  "(SELECT id FROM threads WHERE id=? AND user_id=?)", (tid, uid))
        c.execute("DELETE FROM threads WHERE id=? AND user_id=?", (tid, uid))


def add_message(tid, role, content="", status="done", pipeline=None, owner=None) -> str:
    mid, now = new_id(), time.time()
    running = status == "running"
    with conn() as c:
        c.execute("INSERT INTO messages(id,thread_id,role,content,pipeline_json,status,created_at,"
                  "owner,heartbeat_at,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (mid, tid, role, content, json.dumps(pipeline) if pipeline else None, status, now,
                   owner if running else None, now if running else None, now if running else None))
        c.execute("UPDATE threads SET updated_at=? WHERE id=?", (now, tid))
    return mid


def update_message(mid, **fields):
    if "pipeline" in fields:
        fields["pipeline_json"] = json.dumps(fields.pop("pipeline"))
    if fields.get("status") in ("done", "error"):
        fields.setdefault("finished_at", time.time())
    sets = ",".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE messages SET {sets} WHERE id=?", (*fields.values(), mid))


def thread_messages(tid):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY created_at", (tid,))]


def get_message(mid):
    with conn() as c:
        r = c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    return dict(r) if r else None


def message_owner_user(mid) -> str | None:
    with conn() as c:
        r = c.execute("SELECT t.user_id FROM messages m JOIN threads t ON t.id=m.thread_id WHERE m.id=?",
                      (mid,)).fetchone()
    return r["user_id"] if r else None


def heartbeat(mids: list[str], ts: float | None = None) -> None:
    if not mids:
        return
    ts = ts or time.time()
    with conn() as c:
        c.executemany("UPDATE messages SET heartbeat_at=? WHERE id=? AND status='running'", [(ts, m) for m in mids])


def stale_running(cutoff: float) -> list[dict]:
    """Running messages whose owner stopped heartbeating before `cutoff`."""
    with conn() as c:
        rows = c.execute(
            "SELECT m.*, t.user_id AS user_id FROM messages m JOIN threads t ON t.id=m.thread_id "
            "WHERE m.status='running' AND COALESCE(m.heartbeat_at, m.created_at) < ?", (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def mark_abandoned(mid: str, note: str) -> bool:
    """Atomically flip a still-running message to error. False if someone else got there first."""
    with conn() as c:
        row = c.execute("SELECT pipeline_json FROM messages WHERE id=? AND status='running'", (mid,)).fetchone()
        if not row:
            return False
        pl = json.loads(row["pipeline_json"]) if row["pipeline_json"] else {"stages": []}
        pl["outcome"] = "abandoned"
        pl.setdefault("stages", []).append({"stage": "error", "where": "lifecycle", "detail": note})
        c.execute("UPDATE messages SET status='error', content=?, pipeline_json=?, finished_at=? "
                  "WHERE id=? AND status='running'", (note, json.dumps(pl), time.time(), mid))
    return True


# ---- files ------------------------------------------------------------------------

def add_file(uid, orig_name, path, size) -> str:
    fid = new_id()
    with conn() as c:
        c.execute("INSERT INTO files VALUES(?,?,?,?,?,?)", (fid, uid, orig_name, path, size, time.time()))
    return fid


def user_files(uid):
    with conn() as c:
        rows = c.execute("SELECT * FROM files WHERE user_id=? ORDER BY created_at DESC", (uid,))
        return [dict(r) for r in rows]


def get_file(fid, uid):
    with conn() as c:
        r = c.execute("SELECT * FROM files WHERE id=? AND user_id=?", (fid, uid)).fetchone()
    return dict(r) if r else None


def delete_file_row(fid, uid):
    with conn() as c:
        c.execute("DELETE FROM files WHERE id=? AND user_id=?", (fid, uid))


def expired_files(cutoff: float) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT id, path FROM files WHERE created_at < ?", (cutoff,))]


def delete_file_by_id(fid):
    with conn() as c:
        c.execute("DELETE FROM files WHERE id=?", (fid,))


# ---- usage quota ----------------------------------------------------------------------

def bump_usage(uid) -> int:
    day = today()
    with conn() as c:
        c.execute("INSERT INTO usage(user_id,day,runs) VALUES(?,?,1) "
                  "ON CONFLICT(user_id,day) DO UPDATE SET runs=runs+1", (uid, day))
        return c.execute("SELECT runs FROM usage WHERE user_id=? AND day=?", (uid, day)).fetchone()["runs"]


def refund_usage(uid, day: str | None = None) -> None:
    with conn() as c:
        c.execute("UPDATE usage SET runs=MAX(runs-1,0) WHERE user_id=? AND day=?", (uid, day or today()))


def usage_today(uid) -> int:
    with conn() as c:
        r = c.execute("SELECT runs FROM usage WHERE user_id=? AND day=?", (uid, today())).fetchone()
    return r["runs"] if r else 0


# ---- fault injection (game days) ----------------------------------------------------------

def add_fault(target, mode, probability, latency_s, until, created_by) -> str:
    fid = new_id()
    with conn() as c:
        c.execute("INSERT INTO faults VALUES(?,?,?,?,?,?,?,?)",
                  (fid, target, mode, probability, latency_s, until, created_by, time.time()))
    return fid


def active_faults(now: float | None = None) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM faults WHERE until > ? ORDER BY created_at",
                                           (now or time.time(),))]


def delete_fault(fid):
    with conn() as c:
        c.execute("DELETE FROM faults WHERE id=?", (fid,))
