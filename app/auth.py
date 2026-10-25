"""Authentication.

- Passwords: argon2id.
- 2FA: TOTP (RFC 6238) with QR enrollment for Google Authenticator and friends.
  Seeds are encrypted at rest with a key that lives only in Vault, so a copied
  database file can't mint codes.
- Sessions: a random server-side row, referenced by a signed cookie. Revoking a
  session means deleting the row. The CSRF token rides on the session.
- Lockout: 5 failures per username, per IP, or per TOTP user -> 15 minutes.
  Cloudflare's edge rate limit is the outer layer; this is the inner one.
- One account: Crossfact is single-user (each person deploys their own copy with
  their own keys). ensure_owner() keeps it that way; app/admin.py is the
  recovery tool (reset two-factor, set a password, clear a lockout).

The field-encryption key may be a ready-made Fernet key or any random secret
material (for example bytes that OCI Vault generated itself). Anything that is
not already a Fernet key is stretched into one with HKDF-SHA256, so the key can
be created server-side and never exist on a laptop or in Terraform state.
"""
from __future__ import annotations

import base64
import binascii
import io
import logging

import pyotp
import qrcode
import qrcode.image.svg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from itsdangerous import BadSignature, URLSafeSerializer

from . import db
from .config import cfg
from .secretstore import get_secret, try_secret

log = logging.getLogger("crossfact.auth")
OWNER_DEFAULT_USERNAME = "admin"

_ph = PasswordHasher()
COOKIE = "crossfact_session"


def hash_pw(pw: str) -> str:
    return _ph.hash(pw)


def verify_pw(hash_: str, pw: str) -> bool:
    try:
        return _ph.verify(hash_, pw)
    except (VerificationError, InvalidHashError):
        return False


def fernet_key_from_material(material: str) -> bytes:
    """Use a valid Fernet key as-is; otherwise derive one deterministically."""
    try:
        if len(base64.urlsafe_b64decode(material.encode())) == 32:
            return material.encode()
    except (binascii.Error, ValueError):
        pass
    try:
        raw = base64.b64decode(material, validate=True)
    except (binascii.Error, ValueError):
        raw = material.encode()
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"crossfact/field-encryption",
                   info=b"fernet-v1").derive(raw)
    return base64.urlsafe_b64encode(derived)


def _fernet() -> Fernet:
    return Fernet(fernet_key_from_material(get_secret("field_encryption_key")))


def _signer() -> URLSafeSerializer:
    return URLSafeSerializer(get_secret("session_secret"), salt="crossfact.sid")


def encrypt_totp(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_totp(enc: str) -> str:
    return _fernet().decrypt(enc.encode()).decode()


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(username: str, secret: str) -> str:
    issuer = (cfg().get("base_url") or "Crossfact").replace("https://", "").rstrip("/")
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def totp_qr_svg(uri: str) -> str:
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=12)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


def totp_verify(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def issue_cookie(uid: str) -> tuple[str, dict]:
    s = db.create_session(uid, cfg()["session_hours"])
    return _signer().dumps(s["sid"]), s


def read_session(cookie_val: str | None) -> dict | None:
    if not cookie_val:
        return None
    try:
        sid = _signer().loads(cookie_val)
    except BadSignature:
        return None
    return db.get_session(sid)


# ---- the one account ------------------------------------------------------------------

def ensure_owner() -> dict:
    """Exactly one account can sign in: the owner.

    An empty database gets one named "admin", with the Vault-held bootstrap
    password (rename it with `python -m app.admin rename`). A database from a
    multi-user version keeps its earliest admin as the owner and disables the
    others; their chats stay in the database (and backups) until
    `python -m app.admin purge-others` deletes them."""
    users = db.all_users()  # oldest first
    if not users:
        pw = try_secret("admin_bootstrap_password")
        if not pw:
            log.warning("admin_bootstrap_password is not set; using an insecure default. Set it in Vault.")
            pw = "change-me-now"
        db.create_user(OWNER_DEFAULT_USERNAME, hash_pw(pw), role="admin")
        return db.get_user(username=OWNER_DEFAULT_USERNAME)
    owner = next((u for u in users if u["role"] == "admin" and not u["disabled"]),
                 next((u for u in users if u["role"] == "admin"), users[0]))
    if owner["disabled"] or owner["role"] != "admin":
        db.update_user(owner["id"], disabled=0, role="admin")
    for u in users:
        if u["id"] != owner["id"] and not u["disabled"]:
            db.update_user(u["id"], disabled=1)
            log.warning("single-user: disabled account %r from an earlier multi-user version (its data is kept; "
                        "python -m app.admin purge-others deletes it)", u["username"])
    return db.get_user(uid=owner["id"])
