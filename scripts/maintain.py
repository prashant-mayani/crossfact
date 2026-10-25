#!/usr/bin/env python3
"""crossfact-maintain: retention cleanup, backups, restore, restore-verification.

Runs as Kubernetes Jobs from the same image as the app (never inside the app
pod: only these pods may reach the instance-metadata endpoint, and so only they
can act as the VM's identity to reach Object Storage).

  cleanup   delete uploads past retention, purge expired sessions
  backup    consistent SQLite snapshot + uploads -> tar.gz -> bucket
            (crossfact-YYYYMMDD-HHMM.tar.gz and crossfact-latest.tar.gz)
  daily     cleanup, then backup                      (the nightly CronJob)
  restore   pull crossfact-latest.tar.gz if no database exists (--force to overwrite)
  verify    download latest backup to a temp dir and prove it opens: SQLite
            integrity_check plus row counts; and check the bucket still fits
            the free 20 GB of Object Storage          (the weekly CronJob)

The bucket keeps prior versions and a lifecycle rule ages them out; the VM's
policy can create and overwrite objects but cannot delete them.
"""
from __future__ import annotations

import email.utils
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import db  # noqa: E402
from app.config import cfg  # noqa: E402

LATEST = "crossfact-latest.tar.gz"
# Always Free Object Storage is 20 GB in total (current objects and kept versions).
# Fail the weekly verify job above this, so an alert fires while there is room left.
BUCKET_BUDGET_GB = float(os.environ.get("CROSSFACT_BUCKET_BUDGET_GB", "15"))


def log(event: str, **fields) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}),
          flush=True)


def object_storage():
    import oci

    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    client = oci.object_storage.ObjectStorageClient(config={}, signer=signer)
    b = cfg()["backup"]
    region = b.get("region") or cfg()["oci"].get("region")
    if region:
        client.base_client.set_region(region)
    if not b.get("bucket") or not b.get("namespace"):
        raise SystemExit("backup.bucket and backup.namespace must be set in config.yaml")
    return client, b["namespace"], b["bucket"]


def cleanup() -> None:
    db.init_db()
    days = int(cfg()["uploads"]["retention_days"])
    removed = 0
    for row in db.expired_files(time.time() - days * 86400):
        try:
            os.remove(row["path"])
        except FileNotFoundError:
            pass
        db.delete_file_by_id(row["id"])
        removed += 1
    log("cleanup", expired_uploads=removed, retention_days=days, expired_sessions=db.purge_sessions())


def _snapshot_tarball(workdir: str) -> str:
    snap = os.path.join(workdir, "crossfact.db")
    src, dst = sqlite3.connect(db.db_file()), sqlite3.connect(snap)
    try:
        src.backup(dst)  # consistent online copy, even while the app writes
    finally:
        src.close()
        dst.close()
    tar_path = os.path.join(workdir, "backup.tar.gz")
    with tarfile.open(tar_path, "w:gz") as t:
        t.add(snap, arcname="crossfact.db")
        uploads = os.path.join(cfg()["data_dir"], "uploads")
        if os.path.isdir(uploads):
            t.add(uploads, arcname="uploads")
    return tar_path


def backup() -> None:
    db.init_db()
    with tempfile.TemporaryDirectory() as td:
        tar_path = _snapshot_tarball(td)
        size = os.path.getsize(tar_path)
        client, ns, bucket = object_storage()
        name = time.strftime("crossfact-%Y%m%d-%H%M.tar.gz", time.gmtime())
        for obj in (name, LATEST):
            with open(tar_path, "rb") as fh:
                client.put_object(ns, bucket, obj, fh)
    log("backup", object=name, bytes=size)


def _age_hours(headers) -> float:
    """Age of an object from its Last-Modified header (an RFC 1123 date in GMT)."""
    return (time.time() - email.utils.parsedate_to_datetime(headers["last-modified"]).timestamp()) / 3600


def restore(force: bool = False) -> None:
    if os.path.exists(db.db_file()) and not force:
        log("restore", skipped="database already present")
        return
    try:
        client, ns, bucket = object_storage()
        obj = client.get_object(ns, bucket, LATEST)
    except Exception as e:  # first boot ever: nothing to restore
        log("restore", skipped=f"no backup available ({type(e).__name__})")
        return
    data_dir = cfg()["data_dir"]
    os.makedirs(data_dir, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(obj.data.content), mode="r:gz") as t:
        t.extractall(data_dir, filter="data")
    # The backup's age is the data lost in a rebuild (the recovery point); the DR drill reports it.
    headers = getattr(obj, "headers", None) or {}
    age_min = round(_age_hours(headers) * 60) if "last-modified" in headers else None
    log("restore", restored_into=data_dir, version=db.init_db(), backup_age_minutes=age_min)


def verify() -> None:
    client, ns, bucket = object_storage()
    head = client.head_object(ns, bucket, LATEST)
    age_h = _age_hours(head.headers)
    obj = client.get_object(ns, bucket, LATEST)
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(fileobj=io.BytesIO(obj.data.content), mode="r:gz") as t:
            t.extractall(td, filter="data")
        c = sqlite3.connect(os.path.join(td, "crossfact.db"))
        try:
            integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
            counts = {tbl: c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                      for tbl in ("users", "threads", "messages", "files")}
        finally:
            c.close()
    size_gb = _bucket_gb(client, ns, bucket)
    log("verify", integrity=integrity, age_hours=round(age_h, 1), bucket_gb=size_gb, **counts)
    if integrity != "ok" or counts["users"] < 1:
        raise SystemExit("backup verification FAILED")
    if age_h > 36:
        raise SystemExit(f"latest backup is {age_h:.0f}h old")
    if size_gb is not None and size_gb > BUCKET_BUDGET_GB:
        raise SystemExit(f"backup bucket holds {size_gb:.1f} GB, over the {BUCKET_BUDGET_GB:.0f} GB budget "
                         "(Always Free Object Storage is 20 GB): shorten backup retention (terraform "
                         "backup_retention_days) or upload retention (chart config.uploads.retention_days)")


def _bucket_gb(client, ns: str, bucket: str) -> float | None:
    """Approximate bucket size as Object Storage reports it (may lag by hours)."""
    try:
        size = client.get_bucket(ns, bucket, fields=["approximateSize"]).data.approximate_size
    except Exception as e:  # sizing is advisory; never fail verification over it
        log("verify", bucket_size_unavailable=type(e).__name__)
        return None
    return None if size is None else round(size / 1024**3, 2)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if action == "cleanup":
        cleanup()
    elif action == "backup":
        backup()
    elif action == "daily":
        cleanup()
        backup()
    elif action == "restore":
        restore("--force" in sys.argv)
    elif action == "verify":
        verify()
    else:
        raise SystemExit(f"unknown action {action!r}")
