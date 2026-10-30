"""Prefer a newer model for new questions only while it proves healthy.

A side in providers.yaml `defaults` may name a model it would rather use:

  side_b:
    provider: gemini
    model: gemini-3.6-flash          # the dependable default
    effort: medium
    prefer: {model: gemini-3.8-flash, check_every_s: 1800, passes: 2, timeout_s: 60}

While the side is on its dependable model, a background thread asks the
preferred one a single tiny question every check_every_s. After `passes` passes
in a row, new questions default to the preferred model. The first sign of
trouble hands the default back: a failed check, or a real question in which the
preferred model failed (its breaker records the failure; that question's own
fallbacks still answer it). While the preferred model is the default, the
checks stop: real questions are the test, and a free tier as small as 20
requests a day is not spent on probes.

Questions never wait for a check. A check calls the model once, with no retries
and around its circuit breaker, so it measures the model rather than our
backoff. The state lives in this process: after a restart a side starts on its
dependable model and earns the preferred one again. An explicit choice in the
UI is never overridden; this only decides what a new question starts with.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from . import providers
from . import telemetry as tm
from .config import registry
from .resilience import BreakerState, ProviderError

log = logging.getLogger(__name__)

CHECK_PROMPT = "Reply with the single word OK."
PREFER_DEFAULTS = {"check_every_s": 1800.0, "passes": 2, "timeout_s": 60.0}


def preferences() -> dict[str, dict]:
    """side -> its default plus a complete `prefer` block, for every side that names
    a preferred model its provider actually offers."""
    reg = registry()
    out = {}
    for side, d in (reg.get("defaults") or {}).items():
        pref = (d or {}).get("prefer")
        if not (isinstance(pref, dict) and pref.get("model") and d.get("provider") and d.get("model")):
            continue
        offered = {m.get("id") for m in (reg["providers"].get(d["provider"], {}).get("models") or [])}
        if pref["model"] not in offered:
            log.warning("defaults.%s.prefer: %s/%s is not in the registry; ignored", side, d["provider"],
                        pref["model"])
            continue
        out[side] = {**d, "prefer": {**PREFER_DEFAULTS, **pref}}
    return out


class Promotions:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- state (callers hold the lock) ------------------------------------------
    def _st(self, side: str) -> dict:
        return self._state.setdefault(side, {"streak": 0, "promoted": False, "last_check": None,
                                             "last": "not checked yet"})

    def _demote(self, side: str, st: dict, why: str) -> None:
        st.update(promoted=False, streak=0, last_check=self.clock(), last=why)
        tm.PREFERRED_ACTIVE.labels(side).set(0)
        log.warning("new questions on %s go back to the dependable model: %s", side, why)

    # -- what new questions start with --------------------------------------------
    def effective_defaults(self) -> dict:
        """registry defaults, with each side's preferred model swapped in while it is
        promoted and has not failed since. The `prefer` blocks are not passed on."""
        prefs = preferences()
        out = {}
        for side, d in (registry().get("defaults") or {}).items():
            d = {k: v for k, v in (d or {}).items() if k != "prefer"}
            if side in prefs and self._use_preferred(side, prefs[side]):
                d["model"] = prefs[side]["prefer"]["model"]
            out[side] = d
        return out

    def _use_preferred(self, side: str, d: dict) -> bool:
        with self._lock:
            st = self._st(side)
            if not st["promoted"]:
                return False
            b = providers.breaker_for(d["provider"], d["prefer"]["model"])
            if b.state is BreakerState.CLOSED and b.snapshot()["failures"] == 0:
                return True
            self._demote(side, st, "it failed in a real question")
            return False

    # -- the checks ------------------------------------------------------------------
    def check(self, side: str, d: dict) -> bool:
        pid, pref = d["provider"], d["prefer"]
        model = pref["model"]
        why = ""
        if not providers.configured(pid):
            ok, why = False, f"{pid} has no key"
        else:
            try:
                p = providers.provider_cfg(pid)
                providers.ADAPTERS[p["kind"]](p, model, "low", CHECK_PROMPT, None, float(pref["timeout_s"]))
                ok = True
            except ProviderError as e:
                ok, why = False, f"{e.kind.value}: {e.short()}"
            except Exception as e:  # a library surprise must not stop the checks
                ok, why = False, type(e).__name__
        tm.PREFERRED_CHECKS.labels(side, "pass" if ok else "fail").inc()
        with self._lock:
            st = self._st(side)
            st["last_check"] = self.clock()
            if not ok:
                st["streak"] = 0
                st["last"] = f"check failed: {why}"
                if st["promoted"]:
                    self._demote(side, st, f"check failed: {why}")
                return False
            st["streak"] += 1
            st["last"] = f"check passed ({st['streak']} in a row)"
            if not st["promoted"] and st["streak"] >= int(pref["passes"]):
                st["promoted"] = True
                # Earlier failures may have left its breaker open; the checks just showed it is back.
                providers.breaker_for(pid, model).record_success()
                tm.PREFERRED_ACTIVE.labels(side).set(1)
                log.info("new questions on %s now start with %s/%s (%d checks passed in a row)",
                         side, pid, model, st["streak"])
            return True

    def due(self) -> list[tuple[str, dict]]:
        now = self.clock()
        out = []
        with self._lock:
            for side, d in preferences().items():
                st = self._st(side)
                if st["promoted"]:
                    continue  # in use: real questions are the test
                if st["last_check"] is None or now - st["last_check"] >= float(d["prefer"]["check_every_s"]):
                    out.append((side, d))
        return out

    def tick(self) -> None:
        for side, d in self.due():
            self.check(side, d)

    def snapshot(self) -> dict:
        with self._lock:
            return {side: dict(st) for side, st in self._state.items()}

    # -- the background thread -----------------------------------------------------
    def start(self, every_s: float = 30.0, first_after_s: float = 60.0) -> None:
        for side in preferences():
            tm.PREFERRED_ACTIVE.labels(side).set(0)
            for result in ("pass", "fail"):
                tm.PREFERRED_CHECKS.labels(side, result)
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            if self._stop.wait(first_after_s):
                return
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:  # keep checking; one bad tick must not end the thread
                    log.exception("preferred-model check failed")
                self._stop.wait(every_s)

        self._thread = threading.Thread(target=loop, name="preferred-model-checks", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


PROMOTIONS = Promotions()
