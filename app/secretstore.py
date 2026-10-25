"""Secret access behind one function, three backends.

  file  -> /var/run/secrets/crossfact/<name>, a Kubernetes Secret volume that
           External Secrets Operator fills from OCI Vault. Re-read every 60 s,
           so a rotated secret (say, next year's Claude token) is picked up
           without a restart.
  env   -> environment variables (laptop development, tests).
  vault -> OCI Vault through the VM's instance principal (non-Kubernetes VM).

Terraform creates every secret in Vault up front with a placeholder value, so
"missing" and "placeholder" mean the same thing here: not configured yet.
"""
from __future__ import annotations

import base64
import os
import threading
import time

from .config import cfg

PLACEHOLDERS = {"", "unset", "set-me", "placeholder", "changeme", "change-me"}
_TTL = {"file": 60.0, "env": float("inf"), "vault": 300.0}

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


def _from_vault(name: str) -> str:
    import oci  # lazy: only the VM backend needs the SDK at runtime

    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    client = oci.secrets.SecretsClient(config={}, signer=signer)
    if cfg()["oci"].get("region"):
        client.base_client.set_region(cfg()["oci"]["region"])
    try:
        bundle = client.get_secret_bundle_by_name(secret_name=name, vault_id=cfg()["oci"]["vault_id"]).data
    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            raise SecretNotFound(name) from e
        raise
    return base64.b64decode(bundle.secret_bundle_content.content).decode()


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
    elif mode == "vault":
        raw = _from_vault(name)
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
