"""The recovery CLI for the one account (python -m app.admin)."""
import io

import pyotp
import pytest

from tests.conftest import enroll


@pytest.fixture
def cli(env, monkeypatch):
    from app import admin, auth, db

    db.init_db()
    auth.ensure_owner()

    def run(*argv, stdin=None):
        if stdin is not None:
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        return admin.main(list(argv))

    return run


def test_status(cli, capsys):
    assert cli("status") == 0
    out = capsys.readouterr().out
    assert "account:      admin" in out and "not yet" in out


def test_reset_2fa_signs_out_and_re_enrolls(client, admin, cli):
    from app import db

    assert db.get_user(username="admin")["totp_secret_enc"]
    assert cli("reset-2fa") == 0
    assert db.get_user(username="admin")["totp_secret_enc"] is None
    assert client.get("/chat", follow_redirects=False).status_code == 303  # the old session is gone
    enroll(client)  # a new QR code, and the app works again


def test_set_password_from_stdin_and_too_short(cli):
    from app import auth, db

    assert cli("set-password", stdin="short\n") == 1
    assert cli("set-password", stdin="a-much-longer-pass\n") == 0
    assert auth.verify_pw(db.get_user(username="admin")["pw_hash"], "a-much-longer-pass")


def test_unlock_clears_a_lockout(client, cli):
    for _ in range(5):
        client.post("/login", data={"username": "admin", "password": "wrong"})
    assert client.post("/login", data={"username": "admin", "password": "test-password-123"}).status_code == 429
    assert cli("unlock") == 0
    assert client.post("/login", data={"username": "admin", "password": "test-password-123"},
                       follow_redirects=False).status_code == 303


def test_rename(cli):
    from app import db

    assert cli("rename", "Not Valid!") == 1
    assert cli("rename", "prashant") == 0
    assert db.get_user(username="prashant") and db.get_user(username="admin") is None


def test_purge_others_deletes_old_accounts_and_their_data(env, cli, monkeypatch):
    from app import auth, db

    guest = db.create_user("guest", auth.hash_pw("guest-password-1"))
    tid = db.create_thread(guest, "theirs")
    db.add_message(tid, "user", "hello")
    f = env / "guest.txt"
    f.write_text("x")
    db.add_file(guest, "guest.txt", str(f), 1)
    assert cli("purge-others") == 1  # no terminal, no --yes: refuses
    assert cli("purge-others", "--yes") == 0
    assert db.get_user(username="guest") is None and not f.exists()
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert db.get_user(username="admin")  # the owner stays


def test_totp_codes_still_verify_after_rename(client, admin, cli):
    """Renaming doesn't touch the seed: the existing authenticator entry keeps working."""
    import re

    from app import auth, db

    assert cli("rename", "owner") == 0
    secret = auth.decrypt_totp(db.get_user(username="owner")["totp_secret_enc"])
    client.post("/logout")
    r = client.post("/login", data={"username": "owner", "password": "test-password-123"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/totp", data={"code": pyotp.TOTP(secret).now()}, follow_redirects=False)
    assert r.status_code == 303 and re.search("/", r.headers["location"])


def test_check_keys_reports_each_provider(cli, llm, capsys, monkeypatch):
    from app.config import registry
    from app.resilience import ErrorKind, ProviderError

    monkeypatch.setitem(registry()["providers"]["judge"], "secret", "judge_api_key")  # unset in tests
    llm.on(provider="beta", error=ProviderError("API key not valid", ErrorKind.AUTH))
    assert cli("check-keys") == 1
    out = capsys.readouterr().out
    assert "ok    Alpha a1" in out and "FAIL  Beta b1: auth" in out
    assert "Judge: no key set" in out and "the default judge (judge) has no key" in out
