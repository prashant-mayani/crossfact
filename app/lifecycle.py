"""Run ownership, graceful drain, and orphan recovery.

Every running message is owned by the pod executing it. That pod refreshes a
heartbeat on its messages every few seconds. Any pod (including a new one that
just started) periodically closes out runs whose heartbeat went stale, which
only happens when the owner died: OOM-kill, node reboot, crash. Those runs are
marked "abandoned", their daily-quota slot is refunded, and the UI offers Retry.

A new pod's first sweep waits until Prometheus has read its counters once, so
the runs it closes out are counted (see _loop).

A deploy is not a crash: on SIGTERM the server stops accepting new runs,
finishes the ones in flight (bounded by the run deadline), keeps answering the
UI's polls meanwhile, and only then exits. See server.py.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time

from . import db
from . import telemetry as tm
from .config import cfg

log = logging.getLogger("crossfact.lifecycle")

INSTANCE_ID = os.environ.get("POD_NAME") or socket.gethostname()
ABANDONED_NOTE = ("This run was interrupted because the server restarted before it finished. "
                  "Nothing was counted against your daily quota. Use Retry to ask again.")


class Lifecycle:
    def __init__(self):
        self._lock = threading.Lock()
        self._inflight: dict[str, float] = {}
        self.draining = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- run registry ------------------------------------------------------
    def begin(self, mid: str) -> bool:
        """Count a run as in flight, unless a drain has started (then False: refuse it).
        One lock with drain(): a run is either counted before the drain looks, or
        refused, never started unseen and killed when the process exits."""
        with self._lock:
            if self.draining.is_set():
                return False
            self._inflight[mid] = time.time()
            tm.RUNS_IN_FLIGHT.set(len(self._inflight))
            return True

    def end(self, mid: str) -> None:
        with self._lock:
            self._inflight.pop(mid, None)
            tm.RUNS_IN_FLIGHT.set(len(self._inflight))

    def inflight(self) -> list[str]:
        with self._lock:
            return list(self._inflight)

    # -- background loop ----------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            if not self._stop.is_set():
                return
            self._thread.join(timeout=2)  # a previous loop is still winding down
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="lifecycle", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def reset(self) -> None:
        """Forget in-flight runs and drain state (tests, and a fresh app start)."""
        with self._lock:
            self._inflight.clear()
        self.draining.clear()
        tm.DRAINING.set(0)
        tm.RUNS_IN_FLIGHT.set(0)

    def _loop(self) -> None:
        """Heartbeats, faults, and orphan sweeps. The first sweep waits until Prometheus
        has scraped this pod once (or first_sweep_max_s): runs a dead pod left behind
        are closed out here, and counted before the first scrape they would be lost to
        increase(), so CrossfactRunsAbandoned would never see a crash that took the pod
        down with runs in flight."""
        lc = cfg()["lifecycle"]
        hb, sweep_every = float(lc["heartbeat_s"]), float(lc["sweep_every_s"])
        first_max = float(lc.get("first_sweep_max_s", 600))
        started, last_sweep = time.monotonic(), None
        while not self._stop.wait(hb):
            try:
                db.heartbeat(self.inflight())
                self.publish_faults()
                now = time.monotonic()
                if last_sweep is None:
                    due = tm.SCRAPED.seen.is_set() or now - started >= first_max
                else:
                    due = now - last_sweep >= sweep_every
                if due:
                    self.sweep_orphans()
                    last_sweep = time.monotonic()
            except Exception:  # never let bookkeeping kill the pod
                log.exception("lifecycle tick failed")

    @staticmethod
    def publish_faults() -> None:
        """Mirror the shared fault table into this pod's gauge (expired faults drop out)."""
        tm.FAULTS_ACTIVE.clear()
        for f in db.active_faults():
            tm.FAULTS_ACTIVE.labels(f["target"], f["mode"]).inc()

    def sweep_orphans(self) -> int:
        cutoff = time.time() - float(cfg()["lifecycle"]["orphan_after_s"])
        mine = set(self.inflight())
        closed = 0
        for row in db.stale_running(cutoff):
            if row["id"] in mine:
                continue  # ours and alive; the heartbeat thread is just behind
            if not db.mark_abandoned(row["id"], ABANDONED_NOTE):
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(row.get("started_at") or row["created_at"]))
            db.refund_usage(row["user_id"], day)
            try:
                mode = json.loads(row["pipeline_json"] or "{}").get("mode") or "unknown"
            except ValueError:
                mode = "unknown"
            tm.ORPHANS.inc()
            tm.RUNS.labels(mode, "abandoned").inc()
            closed += 1
            log.warning("closed orphaned run %s (owner %s)", row["id"], row.get("owner"))
        return closed

    # -- drain -----------------------------------------------------------------
    def drain(self, timeout: float) -> bool:
        """Refuse new runs and wait for in-flight ones. True if all finished."""
        with self._lock:
            self.draining.set()
        tm.DRAINING.set(1)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = self.inflight()
            if not remaining:
                log.info("drain complete")
                return True
            db.heartbeat(remaining)  # keep them ours while we wait
            time.sleep(1)
        log.warning("drain timed out with %d run(s) in flight", len(self.inflight()))
        return False


LIFECYCLE = Lifecycle()
