"""Crossfact configuration.

/etc/crossfact/config.yaml    instance settings (paths, secrets mode, retention, ...)
/etc/crossfact/providers.yaml model/provider registry (see config/providers.example.yaml)

In Kubernetes both files come from the chart's ConfigMap. CROSSFACT_* environment
variables override the few values that differ between laptop, CI and cluster.
"""
from __future__ import annotations

import functools
import os

import yaml

CONFIG_PATH = os.environ.get("CROSSFACT_CONFIG", "/etc/crossfact/config.yaml")
PROVIDERS_PATH = os.environ.get("CROSSFACT_PROVIDERS", "/etc/crossfact/providers.yaml")
VERSION = os.environ.get("CROSSFACT_VERSION", "dev")

_DEFAULTS: dict = {
    "data_dir": "/data",
    # file  -> one file per secret under secrets_dir (Kubernetes Secret volume, fed by External Secrets)
    # env   -> environment variables named like the secret, upper-cased (laptop, tests)
    "secrets_mode": "file",
    "secrets_dir": "/var/run/secrets/crossfact",
    # Object Storage for backups, signed in as the backup user (scripts/maintain.py)
    "backup": {"bucket": "", "namespace": "", "region": "", "auth": {}},
    "uploads": {
        "retention_days": 30,
        "max_mb": 25,
        "allowed_ext": [".txt", ".md", ".csv", ".json", ".pdf", ".png", ".jpg", ".jpeg",
                        ".webp", ".log", ".py", ".yaml", ".yml"],
    },
    # One account; the cap keeps a runaway script or a stolen session from burning the free tiers.
    "quotas": {"daily_runs": 200},
    # Values substituted into providers.yaml base URLs, e.g. {cloudflare_account_id} (not secrets).
    "provider_vars": {},
    "session_hours": 72,
    "base_url": "",
    "server": {
        "host": "0.0.0.0",
        "port": 8080,
        "metrics_port": 9095,        # separate listener: never routed through the tunnel
        "drain_timeout_s": 660,      # must exceed pipeline.run_deadline_s
    },
    "tracing": {"enabled": False, "endpoint": "", "sample_ratio": 1.0},
    # first_sweep_max_s: a new pod closes out orphans once Prometheus has read its
    # counters at zero, or after this long without a scrape (app/lifecycle.py).
    "lifecycle": {"heartbeat_s": 15, "orphan_after_s": 90, "sweep_every_s": 60, "first_sweep_max_s": 600},
}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _deep_merge(base[k], v)
        else:
            out[k] = v
    return out


@functools.lru_cache(maxsize=1)
def cfg() -> dict:
    loaded: dict = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            loaded = yaml.safe_load(f) or {}
    c = _deep_merge(_DEFAULTS, loaded)
    env = os.environ
    if env.get("CROSSFACT_DATA_DIR"):
        c["data_dir"] = env["CROSSFACT_DATA_DIR"]
    if env.get("CROSSFACT_SECRETS_MODE"):
        c["secrets_mode"] = env["CROSSFACT_SECRETS_MODE"]
    if env.get("CROSSFACT_SECRETS_DIR"):
        c["secrets_dir"] = env["CROSSFACT_SECRETS_DIR"]
    if env.get("CROSSFACT_METRICS_PORT"):
        c["server"]["metrics_port"] = int(env["CROSSFACT_METRICS_PORT"])
    if env.get("CROSSFACT_PORT"):
        c["server"]["port"] = int(env["CROSSFACT_PORT"])
    if env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        c["tracing"] = {**c["tracing"], "enabled": True, "endpoint": env["OTEL_EXPORTER_OTLP_ENDPOINT"]}
    return c


@functools.lru_cache(maxsize=1)
def registry() -> dict:
    path = PROVIDERS_PATH
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(__file__), "..", "config", "providers.example.yaml")
    with open(path) as f:
        reg = yaml.safe_load(f) or {}
    reg.setdefault("pipeline", {})
    reg.setdefault("defaults", {})
    reg.setdefault("providers", {})
    return reg


def pipeline_setting(key: str, default):
    """config.yaml `pipeline:` overrides providers.yaml `pipeline:` (the chart sets the
    run deadline there so it always matches the pod's termination grace period)."""
    override = cfg().get("pipeline") or {}
    if key in override:
        return override[key]
    return registry()["pipeline"].get(key, default)


def reload() -> None:
    """Drop cached config (tests, and SIGHUP-style reloads)."""
    cfg.cache_clear()
    registry.cache_clear()
