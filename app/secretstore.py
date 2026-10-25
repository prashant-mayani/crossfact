"""Secret access behind one function, two backends.

  file  -> /var/run/secrets/crossfact/<name>, a Kubernetes Secret volume that
           External Secrets Operator fills from OCI Vault. Re-read every 60 s,
           so a rotated secret (say, next year's Claude token) is picked up
           without a restart.
  env   -> environment variables (laptop development, tests).

The app never talks to Vault itself: that would mean acting as the VM, and
nothing in the app's namespace may (the deploy repo's network and admission
policies enforce it).

Terraform creates every secret in Vault up front with a placeholder value, so
"missing" and "placeholder" mean the same thing here: not configured yet.
"""
from __future__ import annotations

import os
import threading
import time

from .config import cfg

PLACEHOLDERS = {"", "unset", "set-me", "placeholder", "changeme", "change-me"}
_TTL = {"file": 60.0, "env": float("inf")}

_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


class SecretNotFound(RuntimeError):
    pass


def _clean(name: str, value: str | None) -> str:
    if value is None:
        raise SecretNotFound(name)
    value = value.strip()
    if value.lower() in PLACEHOLDERS or value.upper().startswith("UNSET"):
        raise SecretNotFound(f"{name} is still a placeholder")
    return value


def _from_file(name: str) -> str:
    path = os.path.join(cfg()["secrets_dir"], name)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        raise SecretNotFound(name) from None


def _from_env(name: str) -> str | None:
    return os.environ.get(name.upper()) or os.environ.get(name)


def get_secret(name: str) -> str:
    mode = cfg()["secrets_mode"]
    now = time.monotonic()
    with _lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < _TTL.get(mode, 60.0):
            return hit[1]
    if mode == "file":
        raw = _from_file(name)
    elif mode == "env":
        raw = _from_env(name)
    else:
        raise RuntimeError(f"unknown secrets_mode {mode!r}")
    value = _clean(name, raw)
    with _lock:
        _cache[name] = (now, value)
    return value


def try_secret(name: str) -> str | None:
    try:
        return get_secret(name)
    except SecretNotFound:
        return None


def clear_cache() -> None:
    with _lock:
        _cache.clear()
