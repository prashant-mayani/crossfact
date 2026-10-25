#!/usr/bin/env python3
"""crossfact-maintain: retention cleanup, backups, restore, restore-verification.

Runs in the app's image: restore as the app pod's init container, the rest as
CronJobs. Object Storage is reached with a dedicated backup credential, an OCI
API key whose user may only read and write the backup bucket. Never with the
VM's own identity: the chart is public code, and nothing it runs may act as the VM.

  cleanup   delete uploads past retention, purge expired sessions
  backup    consistent SQLite snapshot + uploads -> tar.gz -> bucket
            (crossfact-YYYYMMDD-HHMM.tar.gz and crossfact-latest.tar.gz)
  daily     cleanup, then backup                      (the nightly CronJob)
  restore   pull crossfact-latest.tar.gz if no database exists (--force to overwrite)
  verify    download the latest backup and prove it opens: SQLite integrity_check
            and row counts; and check the bucket still fits the free 20 GB of
            Object Storage                            (the weekly CronJob)

Two failure modes this guards against, both of which would silently lose data:
  * restore treating "can't read the backup" as "there is no backup", so the
    app starts on an empty database. Only a missing object means "first
    deploy"; anything else fails the restore, and the app does not start.
  * a later backup replacing the real backup with that empty database. backup
    refuses to replace a backup holding conversations with one holding none
    (or under a tenth as many, from 50 up), unless --force.

The bucket keeps prior versions and a lifecycle rule ages them out; the backup
credential can create and overwrite objects but cannot delete them.
"""
from __future__ import annotations

import email.utils
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import db  # noqa: E402
from app.config import cfg  # noqa: E402

LATEST = "crossfact-latest.tar.gz"
COUNTED = ("users", "threads", "messages", "files")
DEFAULT_KEY_FILE = "/var/run/secrets/crossfact-backup/backup_api_private_key"
# Archives are staged on the data volume, not in /tmp: uploads can outgrow a
# small emptyDir, and restore must not hold a whole backup in memory.
STAGING_PREFIX = ".staging-"
# Always Free Object Storage is 20 GB in total (current objects and kept versions).
# Fail the weekly verify job above this, so an alert fires while there is room left.
BUCKET_BUDGET_GB = float(os.environ.get("CROSSFACT_BUCKET_BUDGET_GB", "15"))
# How long restore keeps retrying errors other than "no backup yet" before failing
# (a new API key or policy can take a minute or two to start working).
RESTORE_RETRY_S = float(os.environ.get("CROSSFACT_RESTORE_RETRY_S", "300"))


def log(event: str, **fields) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}),
          flush=True)


# ---- Object Storage ------------------------------------------------------------------------

def key_fingerprint(pem: bytes) -> str:
    """OCI's API-key fingerprint: MD5 of the public key in DER form, colon-separated
    (what `openssl rsa -pubout -outform DER | openssl md5 -c` prints). Computing it
    from the key means rotating the key changes nothing in the chart."""
    import hashlib

    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(pem, password=None)
    der = key.public_key().public_bytes(serialization.Encoding.DER,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    digest = hashlib.md5(der, usedforsecurity=False).hexdigest()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def storage_config() -> dict:
    """OCI SDK config for the backup credential (chart values backup.auth.*)."""
    b = cfg()["backup"]
    auth = b.get("auth") or {}
    mode = auth.get("mode", "api_key")
    if mode != "api_key":
        raise SystemExit(f"backup.auth.mode must be api_key, not {mode!r}: backups never use the VM's identity")
    conf = {"tenancy": auth.get("tenancy"), "user": auth.get("user"),
            "key_file": auth.get("key_file") or DEFAULT_KEY_FILE, "region": b.get("region")}
    missing = [k for k, v in conf.items() if not v]
    if missing:
        raise SystemExit(f"backup.auth is missing: {', '.join(missing)} (chart values backup.auth.*, backup.region)")
    if not os.path.exists(conf["key_file"]):
        raise SystemExit(f"backup key {conf['key_file']} not found: is the crossfact-backup-key secret synced?")
    with open(conf["key_file"], "rb") as fh:
        pem = fh.read()
    if pem.startswith(b"UNSET"):
        raise SystemExit("the Vault secret backup_api_private_key is still UNSET: store the backup user's "
                         "private key (the deploy repo's scripts/backup-key.sh)")
    try:
        conf["fingerprint"] = auth.get("fingerprint") or key_fingerprint(pem)
    except (ValueError, TypeError) as e:
        raise SystemExit(f"the backup key in {conf['key_file']} is not an unencrypted PEM private key ({e})") from e
    return conf


def object_storage():
    import oci

    b = cfg()["backup"]
    if not b.get("bucket") or not b.get("namespace"):
        raise SystemExit("backup.bucket and backup.namespace must be set in config.yaml")
    conf = storage_config()
    oci.config.validate_config(conf)
    return oci.object_storage.ObjectStorageClient(conf), b["namespace"], b["bucket"]


def _is_missing_object(e: Exception) -> bool:
    """True only for "the object doesn't exist". OCI also answers 404 when the
    bucket is missing or the credential lacks permission (BucketNotFound,
    NotAuthorizedOrNotFound): those are failures, not an empty bucket."""
    return getattr(e, "status", None) == 404 and getattr(e, "code", None) == "ObjectNotFound"


def _download(obj, path: str) -> None:
    raw = getattr(obj.data, "raw", None)
    with open(path, "wb") as fh:
        if raw is not None and hasattr(raw, "stream"):
            for chunk in raw.stream(1 << 20, decode_content=False):
                fh.write(chunk)
        else:
            fh.write(obj.data.content)


def _staging() -> tempfile.TemporaryDirectory:
    data_dir = cfg()["data_dir"]
    os.makedirs(data_dir, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=data_dir, prefix=STAGING_PREFIX)


# ---- cleanup -----------------------------------------------------------------------------------

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
    stale = 0  # staging dirs left behind by a job that was killed mid-way
    data_dir = cfg()["data_dir"]
    for name in os.listdir(data_dir):
        path = os.path.join(data_dir, name)
        if name.startswith(STAGING_PREFIX) and time.time() - os.path.getmtime(path) > 86400:
            shutil.rmtree(path, ignore_errors=True)
            stale += 1
    log("cleanup", expired_uploads=removed, retention_days=days, expired_sessions=db.purge_sessions(),
        stale_staging_dirs=stale)


# ---- backup ------------------------------------------------------------------------------------

def _counts(path: str) -> dict:
    c = sqlite3.connect(path)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNTED}
    finally:
        c.close()


def _snapshot_tarball(workdir: str) -> tuple[str, dict]:
    snap = os.path.join(workdir, "crossfact.db")
    src, dst = sqlite3.connect(db.db_file()), sqlite3.connect(snap)
    try:
        src.backup(dst)  # consistent online copy, even while the app writes
    finally:
        src.close()
        dst.close()
    counts = _counts(snap)
    tar_path = os.path.join(workdir, "backup.tar.gz")
    with tarfile.open(tar_path, "w:gz") as t:
        t.add(snap, arcname="crossfact.db")
        uploads = os.path.join(cfg()["data_dir"], "uploads")
        if os.path.isdir(uploads):
            t.add(uploads, arcname="uploads")
    return tar_path, counts


def _latest_counts(client, ns: str, bucket: str) -> dict | None:
    """Row counts recorded on the current latest backup, or None if there is none
    (or it was written before backups recorded them).

    A one-byte GET rather than a HEAD: a HEAD error has no body, so a missing
    object, a missing bucket and a refused request all look the same."""
    try:
        resp = client.get_object(ns, bucket, LATEST, range="bytes=0-0")
    except Exception as e:
        if _is_missing_object(e):
            return None
        raise
    raw = getattr(resp.data, "raw", None)
    if raw is not None and hasattr(raw, "close"):
        raw.close()
    headers = {k.lower(): v for k, v in (getattr(resp, "headers", None) or {}).items()}
    if "opc-meta-threads" not in headers:
        return None
    return {k: int(headers.get(f"opc-meta-{k}", 0)) for k in COUNTED}


def looks_like_data_loss(previous: dict | None, new: dict) -> bool:
    """A backup that would replace conversations with (almost) none."""
    if not previous:
        return False
    for key in ("threads", "messages"):
        before, after = previous.get(key, 0), new.get(key, 0)
        if before > 0 and after == 0:
            return True
        if before >= 50 and after < before * 0.1:
            return True
    return False


def backup(force: bool = False) -> None:
    db.init_db()
    with _staging() as work:
        tar_path, counts = _snapshot_tarball(work)
        size = os.path.getsize(tar_path)
        client, ns, bucket = object_storage()
        previous = _latest_counts(client, ns, bucket)
        if not force and looks_like_data_loss(previous, counts):
            log("backup", refused=True, latest=previous, new=counts)
            raise SystemExit(
                f"backup REFUSED: the current backup holds {previous['threads']} threads and "
                f"{previous['messages']} messages, this database {counts['threads']} and {counts['messages']}. "
                "That looks like the app started on an empty database. Nothing was uploaded. If the data really "
                "is gone on purpose, run the backup with --force (docs/OPERATIONS.md, 'Backups').")
        meta = {k: str(v) for k, v in counts.items()}
        name = time.strftime("crossfact-%Y%m%d-%H%M.tar.gz", time.gmtime())
        for obj in (name, LATEST):
            with open(tar_path, "rb") as fh:
                client.put_object(ns, bucket, obj, fh, opc_meta=meta)
    log("backup", object=name, bytes=size, forced=force, **counts)


# ---- restore -----------------------------------------------------------------------------------

def _age_hours(headers) -> float:
    """Age of an object from its Last-Modified header (an RFC 1123 date in GMT)."""
    return (time.time() - email.utils.parsedate_to_datetime(headers["last-modified"]).timestamp()) / 3600


def _get_latest(client, ns: str, bucket: str):
    """The latest backup, or None when there has never been one. Any other error is
    retried for about RESTORE_RETRY_S seconds, then fails the restore."""
    waited, delay, attempt = 0.0, 5.0, 0
    while True:
        attempt += 1
        try:
            return client.get_object(ns, bucket, LATEST)
        except Exception as e:
            if _is_missing_object(e):
                return None
            detail = f"{type(e).__name__} {getattr(e, 'status', '')} {getattr(e, 'code', '')}: {str(e)[:200]}"
            if waited + delay > RESTORE_RETRY_S:
                raise SystemExit(
                    f"restore FAILED after {attempt} attempts: {detail}. The volume has no database and the "
                    "backup could not be read, so the app will not start on an empty one. Fix the backup "
                    "credential or bucket; the restore retries by itself (docs/DR.md).") from e
            log("restore", attempt=attempt, retrying_in_s=delay, error=detail)
            time.sleep(delay)
            waited += delay
            delay = min(delay * 2, 60.0)


def _quick_check(path: str) -> str:
    try:
        c = sqlite3.connect(path)
        try:
            return c.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            c.close()
    except sqlite3.DatabaseError as e:
        return str(e)


def restore(force: bool = False) -> None:
    if os.path.exists(db.db_file()) and not force:
        log("restore", skipped="database already present")
        return
    client, ns, bucket = object_storage()  # configuration errors stop here, before anything starts
    obj = _get_latest(client, ns, bucket)
    if obj is None:
        log("restore", skipped="no backup exists yet: first deploy, starting fresh")
        return
    data_dir = cfg()["data_dir"]
    with _staging() as staging:
        archive = os.path.join(staging, "backup.tar.gz")
        _download(obj, archive)
        out = os.path.join(staging, "out")
        with tarfile.open(archive, mode="r:gz") as t:
            t.extractall(out, filter="data")  # refuses absolute paths, "..", and links out of the tree
        restored = os.path.join(out, "crossfact.db")
        check = _quick_check(restored) if os.path.exists(restored) else "no crossfact.db in the archive"
        if check != "ok":
            raise SystemExit(f"restore FAILED: the latest backup's database is damaged ({check}). Nothing on "
                             "the volume was changed. Restore an older backup with scripts/restore.sh in the "
                             "deploy repository.")
        counts = _counts(restored)
        # Only now touch the volume. Stale journal files would be replayed over the restored copy.
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(db.db_file() + suffix)
            except FileNotFoundError:
                pass
        os.replace(restored, db.db_file())
        uploads = os.path.join(out, "uploads")
        if os.path.isdir(uploads):
            target = os.path.join(data_dir, "uploads")
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            os.replace(uploads, target)
    # The backup's age is the data lost in a rebuild (the recovery point); the DR drill reports it.
    headers = getattr(obj, "headers", None) or {}
    age_min = round(_age_hours(headers) * 60) if "last-modified" in headers else None
    log("restore", restored_into=data_dir, version=db.init_db(), backup_age_minutes=age_min, **counts)


# ---- verify ------------------------------------------------------------------------------------

def verify() -> None:
    client, ns, bucket = object_storage()
    obj = client.get_object(ns, bucket, LATEST)
    age_h = _age_hours(obj.headers)
    with _staging() as staging:
        archive = os.path.join(staging, "backup.tar.gz")
        _download(obj, archive)
        with tarfile.open(archive, mode="r:gz") as t:
            uploads = sum(1 for m in t.getmembers() if m.isfile() and m.name.startswith("uploads/"))
            t.extract("crossfact.db", staging, filter="data")
        path = os.path.join(staging, "crossfact.db")
        c = sqlite3.connect(path)
        try:
            integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            c.close()
        counts = _counts(path)
    size_gb = _bucket_gb(client, ns, bucket)
    log("verify", integrity=integrity, age_hours=round(age_h, 1), bucket_gb=size_gb, upload_files=uploads, **counts)
    if integrity != "ok" or counts["users"] < 1:
        raise SystemExit("backup verification FAILED")
    if age_h > 36:
        raise SystemExit(f"latest backup is {age_h:.0f}h old (is the nightly backup failing, or refusing? "
                         "kubectl -n crossfact logs job/<latest crossfact-backup job>)")
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
    force = "--force" in sys.argv
    if action == "cleanup":
        cleanup()
    elif action == "backup":
        backup(force)
    elif action == "daily":
        cleanup()
        backup(force)
    elif action == "restore":
        restore(force)
    elif action == "verify":
        verify()
    else:
        raise SystemExit(f"unknown action {action!r}")
