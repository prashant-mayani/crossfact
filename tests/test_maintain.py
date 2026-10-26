"""Backups and restores against a fake Object Storage that fails the way the real one does.

The two data-loss paths these pin down:
  * restore must not treat "can't read the backup" as "there is no backup";
  * backup must not replace a backup full of conversations with an empty one.
"""
import email.utils
import importlib.util
import os
import time
from types import SimpleNamespace

import pytest
import yaml
from oci.exceptions import ServiceError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LATEST = "crossfact-latest.tar.gz"


def service_error(status, code):
    return ServiceError(status, code, {}, f"{code} (fake)")


class StreamedBody:
    """Like the SDK's response body: read it in chunks, never all at once."""

    def __init__(self, payload: bytes):
        self.raw = SimpleNamespace(stream=lambda size, decode_content=False: (
            payload[i:i + size] for i in range(0, len(payload), size)), close=lambda: None)

    @property
    def content(self):
        raise AssertionError("a backup must be streamed to disk, not loaded into memory")


class FakeObjectStorage:
    """Just enough of oci.object_storage.ObjectStorageClient. `fail` holds errors to raise
    on the next calls, in order; a missing object raises a real 404 ObjectNotFound."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.meta: dict[str, dict] = {}
        self.fail: list[Exception] = []
        self.calls: list[str] = []

    def _maybe_fail(self, op):
        self.calls.append(op)
        if self.fail:
            raise self.fail.pop(0)

    def put_object(self, ns, bucket, name, fh, opc_meta=None):
        self._maybe_fail("put")
        self.objects[name] = fh.read()
        self.meta[name] = {f"opc-meta-{k}": v for k, v in (opc_meta or {}).items()}

    def _headers(self, name, age_s):
        return {"last-modified": email.utils.formatdate(time.time() - age_s, usegmt=True), **self.meta.get(name, {})}

    def get_object(self, ns, bucket, name, range=None):  # "range" is the SDK's parameter name
        self._maybe_fail("get")
        if name not in self.objects:
            raise service_error(404, "ObjectNotFound")
        payload = self.objects[name]
        if range is not None:
            start, end = (int(x) for x in range.removeprefix("bytes=").split("-"))
            payload = payload[start:end + 1]
        return SimpleNamespace(data=StreamedBody(payload), headers=self._headers(name, 600))

    def head_object(self, ns, bucket, name):
        # A real HEAD error has no body, so the SDK's error carries no code: a missing
        # object can't be told apart from a missing bucket or a refused request.
        self._maybe_fail("head")
        if name not in self.objects:
            raise ServiceError(404, None, {}, None)
        return SimpleNamespace(headers=self._headers(name, 0))

    def get_bucket(self, ns, bucket, fields=None):
        size = getattr(self, "approximate_size", sum(len(b) for b in self.objects.values()))
        return SimpleNamespace(data=SimpleNamespace(approximate_size=size))


@pytest.fixture
def maintain(env, monkeypatch):
    spec = importlib.util.spec_from_file_location("maintain", os.path.join(ROOT, "scripts", "maintain.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    store = FakeObjectStorage()
    monkeypatch.setattr(mod, "object_storage", lambda: (store, "ns", "bucket"))
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    mod.store = store
    return mod


def seed(n_threads=1, messages_per_thread=1):
    from app import db

    db.init_db()
    uid = db.create_user("admin", "hash", role="admin")
    for i in range(n_threads):
        tid = db.create_thread(uid, f"t{i}")
        for _ in range(messages_per_thread):
            db.add_message(tid, "user", "hello")
    return uid


def wipe_volume(env):
    from app import db

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db.db_file() + suffix)
        except FileNotFoundError:
            pass


def test_the_first_backup_ever_goes_through(maintain, env):
    seed()
    maintain.backup()  # nothing in the bucket yet: must not be mistaken for an error
    assert LATEST in maintain.store.objects


def test_backup_restore_verify_round_trip(maintain, env, capsys):
    from app import db

    db.init_db()
    uid = db.create_user("admin", "hash", role="admin")
    tid = db.create_thread(uid, "kept")
    up = env / "data" / "uploads" / uid
    up.mkdir(parents=True)
    (up / "notes.md").write_text("hello")

    maintain.backup()
    names = set(maintain.store.objects)
    assert LATEST in names and any(n.startswith("crossfact-20") for n in names)
    assert maintain.store.meta[LATEST]["opc-meta-threads"] == "1"  # counts ride along for the guard

    wipe_volume(env)
    (up / "notes.md").unlink()
    maintain.restore()
    assert db.get_thread(tid, uid)["title"] == "kept"
    assert (up / "notes.md").read_text() == "hello"
    out = capsys.readouterr().out
    assert '"backup_age_minutes": 10' in out  # the drill reports this as the data lost
    assert '"threads": 1' in out
    maintain.verify()


def test_restore_is_a_no_op_on_a_live_volume_and_needs_no_credentials(maintain, env, capsys, monkeypatch):
    seed()
    monkeypatch.setattr(maintain, "object_storage", lambda: pytest.fail("restore contacted the bucket"))
    maintain.restore()
    assert "already present" in capsys.readouterr().out


def test_first_deploy_starts_fresh_only_when_the_object_is_missing(maintain, env, capsys):
    from app import db

    maintain.restore()  # empty bucket: a real 404 ObjectNotFound
    assert "first deploy" in capsys.readouterr().out
    assert not os.path.exists(db.db_file())


@pytest.mark.parametrize("status, code", [
    (404, "BucketNotFound"),            # wrong bucket name, or the bucket was deleted
    (404, "NotAuthorizedOrNotFound"),   # the policy doesn't cover this bucket
    (401, "NotAuthenticated"),          # wrong key or fingerprint
    (503, "ServiceUnavailable"),        # an outage that outlasts the retries
])
def test_restore_fails_closed_when_the_backup_cannot_be_read(maintain, env, capsys, status, code):
    from app import db

    maintain.store.objects[LATEST] = b"whatever"
    maintain.store.fail = [service_error(status, code)] * 100
    with pytest.raises(SystemExit, match="restore FAILED"):
        maintain.restore()
    assert not os.path.exists(db.db_file())  # nothing for the app to start on
    attempts = maintain.store.calls.count("get")
    assert 3 <= attempts < 100  # retried for a while, then gave up
    assert code in capsys.readouterr().out


def test_restore_rides_out_a_brief_outage(maintain, env):
    from app import db

    uid = seed()
    maintain.backup()
    wipe_volume(env)
    maintain.store.fail = [service_error(503, "ServiceUnavailable"), ConnectionError("reset by peer")]
    maintain.restore()
    assert db.user_threads(uid)


def test_restore_refuses_a_damaged_backup_without_touching_the_volume(maintain, env):
    import io
    import tarfile

    from app import db

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        junk = b"SQLite format 3\x00" + b"\x00" * 200
        info = tarfile.TarInfo("crossfact.db")
        info.size = len(junk)
        t.addfile(info, io.BytesIO(junk))
    maintain.store.objects[LATEST] = buf.getvalue()
    keep = env / "data" / "uploads" / "keep.txt"
    keep.parent.mkdir(parents=True)
    keep.write_text("still here")
    with pytest.raises(SystemExit, match="damaged"):
        maintain.restore()
    assert not os.path.exists(db.db_file())
    assert keep.read_text() == "still here"


def test_backup_refuses_to_replace_conversations_with_an_empty_database(maintain, env, capsys):
    seed(n_threads=3)
    maintain.backup()
    good = maintain.store.objects[LATEST]

    wipe_volume(env)  # the rebuild-gone-wrong case: the app started on a fresh, empty database
    seed(n_threads=0)
    puts_before = maintain.store.calls.count("put")
    with pytest.raises(SystemExit, match="REFUSED"):
        maintain.backup()
    assert maintain.store.calls.count("put") == puts_before  # nothing uploaded, not even the dated copy
    assert maintain.store.objects[LATEST] == good
    assert '"refused": true' in capsys.readouterr().out

    maintain.backup(force=True)  # an owner who really did delete everything
    assert maintain.store.objects[LATEST] != good


def test_backup_allows_ordinary_growth_and_pruning(maintain, env):
    from app import db

    uid = seed(n_threads=10)
    maintain.backup()
    for t in db.user_threads(uid)[:6]:
        db.delete_thread(t["id"], uid)
    maintain.backup()  # 10 -> 4 threads is someone tidying up, not data loss
    assert maintain.store.meta[LATEST]["opc-meta-threads"] == "4"


@pytest.mark.parametrize("previous, new, refused", [
    (None, {"threads": 0, "messages": 0}, False),                      # first backup ever
    ({"threads": 0, "messages": 0}, {"threads": 0, "messages": 0}, False),
    ({"threads": 3, "messages": 9}, {"threads": 0, "messages": 0}, True),
    ({"threads": 3, "messages": 9}, {"threads": 2, "messages": 5}, False),
    ({"threads": 60, "messages": 600}, {"threads": 5, "messages": 40}, True),   # a tenth or less
    ({"threads": 60, "messages": 600}, {"threads": 20, "messages": 200}, False),
    ({"threads": 40, "messages": 45}, {"threads": 3, "messages": 4}, False),    # small: only "to zero" counts
    ({"threads": 55, "messages": 58}, {"threads": 3, "messages": 4}, True),
])
def test_data_loss_rule(maintain, previous, new, refused):
    assert maintain.looks_like_data_loss(previous, new) is refused


def test_old_backups_without_counts_do_not_block(maintain, env):
    seed()
    maintain.store.objects[LATEST] = b"from v2, no metadata"
    maintain.store.meta[LATEST] = {}
    maintain.backup()
    assert maintain.store.meta[LATEST]["opc-meta-threads"] == "1"


def test_verify_fails_on_a_corrupt_backup(maintain, env):
    maintain.store.objects[LATEST] = b"not a tarball"
    with pytest.raises(Exception):  # noqa: B017 - tarfile raises its own ReadError
        maintain.verify()


def test_cleanup_enforces_upload_retention(maintain, env):
    from app import db

    db.init_db()
    uid = db.create_user("u", "hash")
    path = env / "old.txt"
    path.write_text("old")
    fid = db.add_file(uid, "old.txt", str(path), 3)
    with db.conn() as c:
        c.execute("UPDATE files SET created_at=? WHERE id=?", (time.time() - 40 * 86400, fid))
    maintain.cleanup()
    assert not path.exists() and db.get_file(fid, uid) is None


def test_verify_fails_when_the_bucket_outgrows_the_free_allowance(maintain, env):
    seed()
    maintain.backup()
    maintain.store.approximate_size = 9 * 1024**3  # under 10 GB, but too close to a paid account's allowance
    with pytest.raises(SystemExit, match="over the 8 GB budget"):
        maintain.verify()


# ---- the backup credential ------------------------------------------------------------------

@pytest.fixture
def configured(env, monkeypatch):
    """Write a config.yaml the way the chart renders it and point the app at it."""
    import app.config as config

    def write(auth: dict, **backup):
        path = env / "config.yaml"
        path.write_text(yaml.safe_dump({"backup": {"bucket": "crossfact-backups", "namespace": "ns",
                                                   "region": "eu-zurich-1", "auth": auth, **backup}}))
        monkeypatch.setattr(config, "CONFIG_PATH", str(path))
        config.reload()

    return write


def load_maintain():
    spec = importlib.util.spec_from_file_location("maintain_real", os.path.join(ROOT, "scripts", "maintain.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_key(path, fmt="PKCS8"):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, getattr(serialization.PrivateFormat, fmt),
                                       serialization.NoEncryption()))
    return key


AUTH = {"mode": "api_key", "tenancy": "ocid1.tenancy.oc1..aaaa", "user": "ocid1.user.oc1..bbbb"}


def test_api_key_credential_builds_a_real_client(configured, env):
    key_file = env / "backup_api_private_key"
    write_key(key_file)
    configured({**AUTH, "key_file": str(key_file)})
    client, ns, bucket = load_maintain().object_storage()  # no network until a request is made
    assert (ns, bucket) == ("ns", "crossfact-backups")
    assert "eu-zurich-1" in client.base_client.endpoint


@pytest.mark.parametrize("fmt", ["PKCS8", "TraditionalOpenSSL"])  # openssl genrsa writes either, by version
def test_fingerprint_is_what_openssl_and_the_console_show(env, fmt):
    import hashlib

    from cryptography.hazmat.primitives import serialization

    key = write_key(env / "k.pem", fmt)
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    expected = ":".join(f"{b:02x}" for b in hashlib.md5(der, usedforsecurity=False).digest())
    assert load_maintain().key_fingerprint((env / "k.pem").read_bytes()) == expected


def test_api_key_credential_reports_what_is_missing(configured, env):
    configured({**AUTH, "user": ""})
    with pytest.raises(SystemExit, match="missing: user"):
        load_maintain().object_storage()


def test_api_key_credential_needs_the_synced_key_file(configured, env):
    configured({**AUTH, "key_file": str(env / "absent")})
    with pytest.raises(SystemExit, match="crossfact-backup-key secret synced"):
        load_maintain().object_storage()


def test_an_unset_key_slot_is_named_as_the_cause(configured, env):
    key_file = env / "backup_api_private_key"
    key_file.write_text("UNSET")  # what Vault holds until the key is stored
    configured({**AUTH, "key_file": str(key_file)})
    with pytest.raises(SystemExit, match="still UNSET"):
        load_maintain().object_storage()


def test_a_key_that_is_not_a_pem_private_key_is_named_as_the_cause(configured, env):
    key_file = env / "backup_api_private_key"
    key_file.write_text("-----BEGIN PUBLIC KEY-----\nnope\n-----END PUBLIC KEY-----\n")
    configured({**AUTH, "key_file": str(key_file)})
    with pytest.raises(SystemExit, match="not an unencrypted PEM private key"):
        load_maintain().object_storage()


def test_backups_never_use_the_vm_identity(configured):
    configured({"mode": "instance_principal"})
    with pytest.raises(SystemExit, match="never use the VM's identity"):
        load_maintain().object_storage()


def test_forced_restore_replaces_a_live_database_and_drops_its_journal(maintain, env):
    from app import db

    uid = seed(n_threads=2)
    maintain.backup()
    for t in db.user_threads(uid):
        db.delete_thread(t["id"], uid)
    wal = db.db_file() + "-wal"
    with open(wal, "wb") as fh:
        fh.write(b"journal of the database being replaced")
    maintain.restore(force=True)
    assert not os.path.exists(wal)
    assert len(db.user_threads(uid)) == 2


@pytest.mark.parametrize("killed_at", ["uploads", "database"])
def test_a_restore_killed_part_way_runs_again_on_the_next_start(maintain, env, monkeypatch, killed_at):
    """The database's presence marks a finished restore, so it goes in last: a restore
    killed at any step before that leaves no database, and the next start restores again."""
    from app import db

    uid = seed()
    up = env / "data" / "uploads" / uid
    up.mkdir(parents=True)
    (up / "notes.md").write_text("hello")
    maintain.backup()
    wipe_volume(env)
    (up / "notes.md").unlink()

    real = os.replace

    step = "uploads" if killed_at == "uploads" else os.path.basename(db.db_file())

    def killed(src, dst):
        if os.path.basename(dst) == step:
            raise KeyboardInterrupt("the pod was killed")
        return real(src, dst)

    monkeypatch.setattr(maintain.os, "replace", killed)
    with pytest.raises(KeyboardInterrupt):
        maintain.restore()
    assert not os.path.exists(db.db_file())  # so the next start doesn't skip the restore
    monkeypatch.setattr(maintain.os, "replace", real)
    maintain.restore()
    assert db.user_threads(uid) and (up / "notes.md").read_text() == "hello"


def test_cleanup_removes_staging_left_by_a_killed_job(maintain, env):
    seed()
    data = env / "data"
    old, fresh = data / ".staging-old", data / ".staging-running"
    for d in (old, fresh):
        d.mkdir()
        (d / "backup.tar.gz").write_bytes(b"partial")
    two_days_ago = time.time() - 2 * 86400
    os.utime(old, (two_days_ago, two_days_ago))
    maintain.cleanup()
    assert not old.exists() and fresh.exists()


def test_staging_happens_on_the_data_volume(maintain, env, monkeypatch):
    import tempfile

    seen = []
    real = tempfile.TemporaryDirectory

    def spy(*a, **k):
        seen.append(k.get("dir"))
        return real(*a, **k)

    monkeypatch.setattr(maintain.tempfile, "TemporaryDirectory", spy)
    seed()
    maintain.backup()
    wipe_volume(env)
    maintain.restore()
    maintain.verify()
    assert seen and all(d == str(env / "data") for d in seen)
