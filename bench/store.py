"""One SQLite file holds everything a run needs to resume and to be audited:
the model-call cache, per-day usage against each free tier, run metadata,
answers, and grades."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls(
  key TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, effort TEXT, purpose TEXT,
  response TEXT NOT NULL, duration REAL NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS usage(
  day TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
  exhausted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day, provider, model));
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, created REAL NOT NULL, meta TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS answers(
  run_id TEXT NOT NULL, system TEXT NOT NULL, qid TEXT NOT NULL, answer TEXT, error TEXT,
  latency REAL, calls INTEGER, cached INTEGER, detail TEXT, created REAL NOT NULL,
  PRIMARY KEY(run_id, system, qid));
CREATE TABLE IF NOT EXISTS grades(
  key TEXT PRIMARY KEY, grade TEXT NOT NULL, how TEXT NOT NULL, grader TEXT NOT NULL, raw TEXT,
  created REAL NOT NULL);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # -- the call cache ------------------------------------------------------------------
    def cached(self, key: str, since: float | None = None) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT response, duration FROM calls WHERE key=? AND created >= ?",
                          (key, since if since is not None else 0)).fetchone()
        return dict(r) if r else None

    def remember(self, key: str, provider: str, model: str, effort: str, purpose: str, response: str,
                 duration: float) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO calls VALUES(?,?,?,?,?,?,?,?)",
                      (key, provider, model, effort, purpose, response, duration, time.time()))

    def forget(self, key: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM calls WHERE key=?", (key,))

    # -- usage against free tiers ---------------------------------------------------------
    def used(self, day: str, provider: str, model: str) -> tuple[int, bool]:
        with self.conn() as c:
            r = c.execute("SELECT calls, exhausted FROM usage WHERE day=? AND provider=? AND model=?",
                          (day, provider, model)).fetchone()
        return (r["calls"], bool(r["exhausted"])) if r else (0, False)

    def count_call(self, day: str, provider: str, model: str, exhausted: bool = False) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO usage(day, provider, model, calls, exhausted) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(day, provider, model) DO UPDATE SET calls=calls+excluded.calls, "
                      "exhausted=MAX(exhausted, excluded.exhausted)",
                      (day, provider, model, 0 if exhausted else 1, int(exhausted)))

    # -- runs, answers, grades ------------------------------------------------------------
    def run_meta(self, run_id: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT meta FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(r["meta"]) if r else None

    def save_run(self, run_id: str, meta: dict) -> None:
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO runs VALUES(?,?,?)", (run_id, time.time(), json.dumps(meta)))

    def update_run(self, run_id: str, meta: dict) -> None:
        with self.conn() as c:
            c.execute("UPDATE runs SET meta=? WHERE run_id=?", (json.dumps(meta), run_id))

    def runs(self) -> list[dict]:
        with self.conn() as c:
            return [{"run_id": r["run_id"], "created": r["created"], **json.loads(r["meta"])}
                    for r in c.execute("SELECT * FROM runs ORDER BY created")]

    def answer(self, run_id: str, system: str, qid: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM answers WHERE run_id=? AND system=? AND qid=?",
                          (run_id, system, qid)).fetchone()
        return dict(r) if r else None

    def save_answer(self, run_id: str, system: str, qid: str, *, answer: str | None, error: str | None,
                    latency: float, calls: int, cached: int, detail: dict) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO answers VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (run_id, system, qid, answer, error, latency, calls, cached, json.dumps(detail),
                       time.time()))

    def answers(self, run_id: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM answers WHERE run_id=? ORDER BY system, qid", (run_id,))
            return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]

    def grade(self, key: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM grades WHERE key=?", (key,)).fetchone()
        return dict(r) if r else None

    def save_grade(self, key: str, grade: str, how: str, grader: str, raw: str | None) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO grades VALUES(?,?,?,?,?,?)", (key, grade, how, grader, raw, time.time()))
