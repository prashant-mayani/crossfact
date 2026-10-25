"""Fault injection for game days: break a provider on purpose, from the admin page.

A fault targets "provider", "provider/model" or "*", has a mode and a
probability, and always expires (no forgotten chaos). Faults live in SQLite so
every pod sees them, and they are cached for a few seconds per pod.

Modes:
  error       raise a server error (exercises retries and the breaker)
  timeout     hang until the call's timeout, then fail (exercises deadlines)
  rate_limit  raise a 429 with a 20 s retry hint (exercises hint handling)
  quota       raise daily-quota exhaustion (exercises fail-fast + fallbacks)
  auth        raise an auth error (exercises fail-fast, no retries)
  latency     add latency_s before the real call (exercises the latency SLO)
"""
from __future__ import annotations

import random
import sqlite3
import threading
import time

from . import db
from .resilience import ErrorKind, ProviderError

MODES = ("error", "timeout", "rate_limit", "quota", "auth", "latency")
_CACHE_S = 3.0
_cache: tuple[float, list[dict]] = (0.0, [])
_lock = threading.Lock()


def active(now: float | None = None) -> list[dict]:
    global _cache
    now = now or time.time()
    with _lock:
        if now - _cache[0] < _CACHE_S:
            return _cache[1]
    try:
        faults = db.active_faults(now)
    except sqlite3.Error:  # chaos tooling must never break a real call
        faults = []
    with _lock:
        _cache = (now, faults)
    return faults


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = (0.0, [])


def _matches(target: str, provider: str, model: str) -> bool:
    return target in ("*", provider, f"{provider}/{model}")


def apply(provider: str, model: str, timeout_s: float, *, sleep=None, rng=None) -> None:
    """Called right before a real model call. Raises or sleeps when a fault fires."""
    sleep, rng = sleep or time.sleep, rng or random.random
    for f in active():
        if not _matches(f["target"], provider, model) or rng() >= float(f["probability"]):
            continue
        mode = f["mode"]
        if mode == "latency":
            sleep(min(float(f["latency_s"]), timeout_s))
            continue
        if mode == "timeout":
            sleep(timeout_s)
            raise ProviderError(f"injected timeout after {timeout_s:.0f}s", ErrorKind.TIMEOUT, injected=True)
        if mode == "rate_limit":
            raise ProviderError("injected 429", ErrorKind.RATE_LIMITED, retry_after=20.0, status=429, injected=True)
        if mode == "quota":
            raise ProviderError("injected daily quota exhaustion", ErrorKind.QUOTA_EXHAUSTED, status=429,
                                injected=True)
        if mode == "auth":
            raise ProviderError("injected 401", ErrorKind.AUTH, status=401, injected=True)
        raise ProviderError("injected 503", ErrorKind.SERVER, status=503, injected=True)


# ---- operator CLI: game days from a terminal ------------------------------------------
#   kubectl -n crossfact exec deploy/crossfact -- python -m app.faults add gemini quota --minutes 15
#   kubectl -n crossfact exec deploy/crossfact -- python -m app.faults list
#   kubectl -n crossfact exec deploy/crossfact -- python -m app.faults clear --all
# Same rules as the admin page: known modes only, probability 0-1, expires in 1-240 minutes.

def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m app.faults", description="Inject provider faults (game days).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="inject a fault")
    add.add_argument("target", help='"provider", "provider/model" or "*"')
    add.add_argument("mode", choices=MODES)
    add.add_argument("--probability", type=float, default=1.0)
    add.add_argument("--latency", type=float, default=0.0, help="seconds, for mode latency")
    add.add_argument("--minutes", type=int, default=15)
    sub.add_parser("list", help="active faults")
    clear = sub.add_parser("clear", help="remove faults")
    clear.add_argument("id", nargs="?")
    clear.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)

    db.init_db()
    if args.cmd == "add":
        minutes = max(1, min(args.minutes, 240))
        probability = max(0.0, min(args.probability, 1.0))
        fid = db.add_fault(args.target, args.mode, probability, max(0.0, args.latency),
                           time.time() + minutes * 60, "cli")
        print(f"{fid}  {args.target} {args.mode} p={probability:g} for {minutes} min")
        return 0
    if args.cmd == "list":
        now = time.time()
        rows = db.active_faults(now)
        for f in rows:
            print(f"{f['id']}  {f['target']:28} {f['mode']:10} p={f['probability']:g} "
                  f"latency={f['latency_s']:g}s  {int((f['until'] - now) / 60)} min left  by {f['created_by']}")
        if not rows:
            print("no active faults")
        return 0
    if args.all:
        for f in db.active_faults():
            db.delete_fault(f["id"])
        print("cleared all faults")
        return 0
    if not args.id:
        ap.error("clear needs an id or --all")
    db.delete_fault(args.id)
    print(f"cleared {args.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
