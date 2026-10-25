"""Backups round-trip through a fake Object Storage; verification catches corruption."""
import email.utils
import importlib.util
import os
import time
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeObjectStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, ns, bucket, name, fh):
        self.objects[name] = fh.read()

    def get_object(self, ns, bucket, name):
        if name not in self.objects:
            raise KeyError(name)
        return SimpleNamespace(data=SimpleNamespace(content=self.objects[name]),
                               headers={"last-modified": email.utils.formatdate(time.time() - 600, usegmt=True)})

    def get_bucket(self, ns, bucket, fields=None):
        size = getattr(self, "approximate_size", sum(len(b) for b in self.objects.values()))
        return SimpleNamespace(data=SimpleNamespace(approximate_size=size))

    def head_object(self, ns, bucket, name):
        return SimpleNamespace(headers={"last-modified": email.utils.formatdate(time.time(), usegmt=True)})


@pytest.fixture
def maintain(env, monkeypatch):
    spec = importlib.util.spec_from_file_location("maintain", os.path.join(ROOT, "scripts", "maintain.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    store = FakeObjectStorage()
    monkeypatch.setattr(mod, "object_storage", lambda: (store, "ns", "bucket"))
    mod.store = store
    return mod


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
    assert "crossfact-latest.tar.gz" in names and any(n.startswith("crossfact-20") for n in names)

    os.remove(db.db_file())
    (up / "notes.md").unlink()
    maintain.restore()
    assert db.get_thread(tid, uid)["title"] == "kept"
    assert (up / "notes.md").read_text() == "hello"
    assert '"backup_age_minutes": 10' in capsys.readouterr().out  # the drill reports this as data lost
    maintain.verify()


def test_restore_never_overwrites_a_live_database(maintain, env, capsys):
    from app import db

    db.init_db()
    db.create_user("admin", "hash")
    maintain.restore()
    assert "already present" in capsys.readouterr().out


def test_verify_fails_on_a_corrupt_backup(maintain, env):
    maintain.store.objects["crossfact-latest.tar.gz"] = b"not a tarball"
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
    from app import db

    db.init_db()
    db.create_user("admin", "hash", role="admin")
    maintain.backup()
    maintain.store.approximate_size = 16 * 1024**3
    with pytest.raises(SystemExit, match="over the 15 GB budget"):
        maintain.verify()
