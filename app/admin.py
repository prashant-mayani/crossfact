"""Account recovery for the one Crossfact account, from a shell in the pod:

    kubectl -n crossfact exec -it deploy/crossfact -- python -m app.admin <command>

  status            the account: two-factor enrolled, lockouts, open sessions
  reset-2fa         forget the authenticator; the next sign-in shows a new QR code
                    (and signs out every session)
  set-password      prompt for a new password (or read one line from stdin);
                    --generate prints a random one, once. Signs out every session
  unlock            clear the login and two-factor lockouts
  rename <name>     change the username (2-32: letters, digits, dot, dash, underscore)
  purge-others      delete accounts left over from a multi-user version, with
                    their chats and files (asks first; --yes to skip)
  check-keys        send each provider with a key one tiny prompt and report
                    what came back (uses a sliver of each free quota; Claude's
                    counts against your plan's usage like any message)

Getting here already took the admin tunnel (Cloudflare Access) and the
cluster's client certificate, so this is the recovery path for a lost phone.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import sys
import time

from . import auth, db


class NoAccount(SystemExit):
    pass


def _owner() -> dict:
    db.init_db()
    u = auth.ensure_owner()
    if u is None:
        raise NoAccount("no account exists yet (admin_bootstrap_password isn't set): "
                        "run `python -m app.admin set-password` to create it with a password you choose")
    return u


def _drop_sessions(uid: str) -> int:
    with db.conn() as c:
        return c.execute("DELETE FROM sessions WHERE user_id=?", (uid,)).rowcount


def status(_args) -> int:
    u = _owner()
    with db.conn() as c:
        sessions = c.execute("SELECT COUNT(*) FROM sessions WHERE user_id=? AND expires_at>?",
                             (u["id"], time.time())).fetchone()[0]
        locks = [r["key"] for r in c.execute("SELECT key FROM login_attempts WHERE locked_until>?", (time.time(),))]
        others = c.execute("SELECT COUNT(*) FROM users WHERE id<>?", (u["id"],)).fetchone()[0]
    print(f"account:      {u['username']}")
    print(f"two-factor:   {'enrolled' if u['totp_secret_enc'] else 'not yet (QR code at next sign-in)'}")
    print(f"sessions:     {sessions} open")
    print(f"lockouts:     {', '.join(locks) if locks else 'none'}")
    if others:
        print(f"old accounts: {others} disabled (from a multi-user version; purge-others deletes them)")
    return 0


def reset_2fa(_args) -> int:
    u = _owner()
    db.update_user(u["id"], totp_secret_enc=None)
    n = _drop_sessions(u["id"])
    db.lockout_clear(f"totp:{u['id']}")
    print(f"two-factor reset for {u['username']}; {n} session(s) signed out. Sign in and scan the new QR code.")
    return 0


def set_password(args) -> int:
    db.init_db()
    u = auth.ensure_owner()  # None: no account yet; this command creates it
    if args.generate:
        pw = secrets.token_urlsafe(18)
    elif sys.stdin.isatty():
        pw = getpass.getpass("new password (10+ characters): ")
        if pw != getpass.getpass("again: "):
            print("the two entries differ; nothing changed", file=sys.stderr)
            return 1
    else:
        pw = sys.stdin.readline().rstrip("\n")
    if len(pw) < 10:
        print("use at least 10 characters; nothing changed", file=sys.stderr)
        return 1
    if u is None:
        u = auth.ensure_owner(password=pw)
        print(f"created the account {u['username']}" + (f" with the password (shown once): {pw}" if args.generate
                                                          else ""))
        return 0
    db.update_user(u["id"], pw_hash=auth.hash_pw(pw))
    n = _drop_sessions(u["id"])
    db.lockout_clear(f"u:{u['username']}")
    if args.generate:
        print(f"new password for {u['username']} (shown once): {pw}")
    print(f"password changed; {n} session(s) signed out")
    return 0


def unlock(_args) -> int:
    _owner()
    with db.conn() as c:
        n = c.execute("DELETE FROM login_attempts").rowcount
    print(f"cleared {n} lockout record(s)")
    return 0


def rename(args) -> int:
    u = _owner()
    name = args.name.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{2,32}", name):
        print("usernames are 2-32 characters: letters, digits, dot, dash, underscore", file=sys.stderr)
        return 1
    existing = db.get_user(username=name)
    if existing and existing["id"] != u["id"]:
        print(f"{name!r} is taken by a disabled old account; purge-others first", file=sys.stderr)
        return 1
    db.update_user(u["id"], username=name)
    print(f"renamed {u['username']} -> {name} (an authenticator entry keeps its old label; that is harmless)")
    return 0


def purge_others(args) -> int:
    u = _owner()
    others = [x for x in db.all_users() if x["id"] != u["id"]]
    if not others:
        print("no other accounts")
        return 0
    names = ", ".join(x["username"] for x in others)
    if not args.yes:
        if not sys.stdin.isatty():
            print("refusing without a terminal: pass --yes", file=sys.stderr)
            return 1
        if input(f"delete {names} and all their chats and files? Backups keep old copies until they "
                 "expire. [y/N] ").strip().lower() != "y":
            print("nothing deleted")
            return 1
    for x in others:
        with db.conn() as c:
            paths = [r["path"] for r in c.execute("SELECT path FROM files WHERE user_id=?", (x["id"],))]
            c.execute("DELETE FROM messages WHERE thread_id IN (SELECT id FROM threads WHERE user_id=?)", (x["id"],))
            for table in ("threads", "files", "usage", "sessions"):
                c.execute(f"DELETE FROM {table} WHERE user_id=?", (x["id"],))
            c.execute("DELETE FROM users WHERE id=?", (x["id"],))
        for path in paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
    print(f"deleted {names}")
    return 0


def check_keys(args) -> int:
    from . import providers
    from .config import registry
    from .resilience import ProviderError

    failed = 0
    for pid, p in providers.enabled_providers().items():
        models = p.get("models") or []
        if not models:
            continue
        if not providers.configured(pid):
            print(f"  -     {p.get('label', pid)}: no key set ({p.get('secret')}), skipped by every chain")
            continue
        model = args.model.get(pid) if args.model else None
        model = model or models[0]["id"]
        t0 = time.monotonic()
        try:
            text = providers.ADAPTERS[p["kind"]](p, model, "low", "Reply with the single word OK.", None, 60)
            print(f"  ok    {p.get('label', pid)} {model}: {text.strip()[:40]!r} in {time.monotonic() - t0:.1f}s")
        except ProviderError as e:
            failed += 1
            print(f"  FAIL  {p.get('label', pid)} {model}: {e.kind.value}: {e.short()}")
    judge = registry()["pipeline"].get("judge") or {}
    if judge and not providers.configured(judge.get("provider", "")):
        print(f"note: the default judge ({judge.get('provider')}) has no key; rulings come from a fallback")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.admin", description="Recovery for the one Crossfact account.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=status)
    sub.add_parser("reset-2fa").set_defaults(fn=reset_2fa)
    sp = sub.add_parser("set-password")
    sp.add_argument("--generate", action="store_true")
    sp.set_defaults(fn=set_password)
    sub.add_parser("unlock").set_defaults(fn=unlock)
    rn = sub.add_parser("rename")
    rn.add_argument("name")
    rn.set_defaults(fn=rename)
    po = sub.add_parser("purge-others")
    po.add_argument("--yes", action="store_true")
    po.set_defaults(fn=purge_others)
    ck = sub.add_parser("check-keys")
    ck.add_argument("--model", action="append", default=[], metavar="PROVIDER=MODEL",
                    help="which model to try for a provider (default: its first)")
    ck.set_defaults(fn=check_keys)
    args = ap.parse_args(argv)
    if args.cmd == "check-keys":
        args.model = dict(m.split("=", 1) for m in args.model if "=" in m)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
