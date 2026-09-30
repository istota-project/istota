"""Native web credentials, independent of HTTP, mail and configuration.

Reads distinguish a missing row from a failed database read: callers must deny
access on errors, particularly where absence permits Nextcloud-only sessions.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
from pathlib import Path
import re
import secrets
import sqlite3
import traceback

from .db import get_db

logger = logging.getLogger(__name__)
_N, _R, _P = 32768, 8, 1
_MAX_PASSWORD_BYTES = 1024
_PURPOSES = {"enrol", "reset", "login"}


@dataclass(frozen=True)
class Identity:
    user_id: str
    email: str
    password_hash: str
    credential_epoch: int
    disabled: bool
    created_at: str
    last_login_at: str | None


@dataclass(frozen=True)
class TokenRecord:
    user_id: str
    email: str
    purpose: str
    expires_at: str


@dataclass(frozen=True)
class Policy:
    min_password_length: int
    throttle_window_seconds: int
    throttle_max_email: int
    throttle_max_ip: int
    enrol_ttl_seconds: int
    reset_ttl_seconds: int
    login_link_ttl_seconds: int
    mail_link_max_email: int


def policy_from_config(config) -> Policy:
    web = config.web
    return Policy(
        min_password_length=web.auth_min_password_length,
        throttle_window_seconds=web.auth_throttle_window_seconds,
        throttle_max_email=web.auth_throttle_max_email,
        throttle_max_ip=web.auth_throttle_max_ip,
        enrol_ttl_seconds=web.auth_enrol_ttl_hours * 3600,
        reset_ttl_seconds=web.auth_reset_ttl_hours * 3600,
        login_link_ttl_seconds=web.auth_login_link_ttl_minutes * 60,
        mail_link_max_email=web.auth_mail_link_max_email,
    )


def _timestamp(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S.%f")


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if len(raw) > _MAX_PASSWORD_BYTES:
        raise ValueError("Password must be at most 1024 bytes")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(raw, salt=salt, n=_N, r=_R, p=_P, dklen=32, maxmem=128 * 1024 * 1024)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> tuple[bool, bool]:
    try:
        algorithm, n_raw, r_raw, p_raw, salt_raw, digest_raw = encoded.split("$")
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        # The stored parameters are untrusted too. Bound both memory and work.
        if algorithm != "scrypt" or n < 2 or n > 2**18 or n & (n - 1):
            return False, False
        if not 1 <= r <= 8 or not 1 <= p <= 4 or n * r * p > _N * _R * 4:
            return False, False
        salt = base64.b64decode(salt_raw, validate=True)
        expected = base64.b64decode(digest_raw, validate=True)
        raw = password.encode("utf-8")
        if len(salt) != 16 or len(expected) != 32 or len(raw) > _MAX_PASSWORD_BYTES:
            return False, False
        actual = hashlib.scrypt(raw, salt=salt, n=n, r=r, p=p, dklen=32, maxmem=128 * 1024 * 1024)
        ok = hmac.compare_digest(actual, expected)
        return ok, ok and (n, r, p) != (_N, _R, _P)
    except (ValueError, TypeError, UnicodeError, OverflowError):
        return False, False


DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def normalize_email(raw: str) -> str:
    return raw.strip().casefold()


def valid_new_user_id(raw: str) -> bool:
    return re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", raw) is not None


def password_policy_error(password: str, policy: Policy, *, email: str, user_id: str) -> str | None:
    if policy.min_password_length > 128:
        raise ValueError("Minimum password length must not exceed 128")
    if len(password.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        return "Password must be at most 1024 bytes"
    if len(password) < max(8, policy.min_password_length):
        return f"Password must be at least {max(8, policy.min_password_length)} characters"
    if password.casefold() in (normalize_email(email), user_id.casefold()):
        return "Password must differ from your email address and user ID"
    return None


def _identity(row: sqlite3.Row | None) -> Identity | None:
    if row is None:
        return None
    return Identity(row["user_id"], row["email"], row["password_hash"], row["credential_epoch"],
                    bool(row["disabled"]), row["created_at"], row["last_login_at"])


def _get_identity(conn: sqlite3.Connection, user_id: str) -> Identity | None:
    return _identity(conn.execute("SELECT * FROM web_auth_identities WHERE user_id = ?", (user_id,)).fetchone())


def _require_identity(conn: sqlite3.Connection, user_id: str) -> Identity:
    identity = _get_identity(conn, user_id)
    if identity is None:
        raise ValueError("No email identity for this user")
    return identity


def _has_profile(conn: sqlite3.Connection, user_id: str) -> bool:
    return conn.execute("SELECT 1 FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone() is not None


def get_identity(db_path: Path, user_id: str) -> Identity | None:
    with get_db(db_path) as conn:
        return _get_identity(conn, user_id)


def get_identity_by_email(db_path: Path, email: str) -> Identity | None:
    with get_db(db_path) as conn:
        return _identity(conn.execute("SELECT * FROM web_auth_identities WHERE email = ?", (normalize_email(email),)).fetchone())


def list_identities(db_path: Path) -> list[Identity]:
    with get_db(db_path) as conn:
        return [_identity(row) for row in conn.execute("SELECT * FROM web_auth_identities ORDER BY user_id")]


def _invalidate_tokens(conn: sqlite3.Connection, user_id: str, purpose: str | None = None) -> None:
    sql = "UPDATE web_auth_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL"
    args = [_timestamp(), user_id]
    if purpose is not None:
        sql += " AND purpose = ?"
        args.append(purpose)
    conn.execute(sql, args)


def upsert_identity(
    db_path: Path, user_id: str, email: str, *, create_profile: bool = False,
    display_name: str = "", reject_case_collision: bool = False,
) -> Identity:
    email = normalize_email(email)
    if not email:
        raise ValueError("Email must not be empty")
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if reject_case_collision:
            for row in conn.execute("SELECT user_id FROM user_profiles UNION SELECT user_id FROM web_auth_identities"):
                if row["user_id"] != user_id and row["user_id"].casefold() == user_id.casefold():
                    raise ValueError(f"User ID differs only in case from {row['user_id']}")
        conflict = conn.execute("SELECT user_id FROM web_auth_identities WHERE email = ? AND user_id != ?",
                                (email, user_id)).fetchone()
        if conflict and reject_case_collision:
            raise ValueError(f"That address is already a login for {conflict['user_id']}")
        if not _has_profile(conn, user_id):
            if not create_profile:
                raise ValueError("No user profile for this identity")
            if not valid_new_user_id(user_id):
                raise ValueError("New user IDs must be 1–32 lowercase letters, digits, dots, underscores or hyphens, starting with a letter or digit; they become directory names.")
            from .user_profiles import UserProfile, insert_profile
            insert_profile(conn, UserProfile(user_id=user_id, display_name=display_name or user_id))
        identity = _get_identity(conn, user_id)
        if identity is None:
            # A fixed epoch would revive old cookies after remove/re-add.
            # Leave half the integer range available for monotonic increments.
            epoch = secrets.randbelow(2**62 - 1) + 1
            conn.execute("INSERT INTO web_auth_identities (user_id, email, credential_epoch) VALUES (?, ?, ?)",
                         (user_id, email, epoch))
        elif identity.email != email:
            conn.execute("UPDATE web_auth_identities SET email = ?, credential_epoch = credential_epoch + 1, updated_at = ? WHERE user_id = ?",
                         (email, _timestamp(), user_id))
            _invalidate_tokens(conn, user_id)
        return _require_identity(conn, user_id)


def _change_credential(db_path: Path, user_id: str, *, password_hash: str | None = None, disabled: bool | None = None, protected_admins: set[str] | None = None) -> int:
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        identity = _require_identity(conn, user_id)
        if disabled:
            _protect_last_admin(conn, identity, protected_admins)
        epoch = identity.credential_epoch + 1
        conn.execute("UPDATE web_auth_identities SET password_hash = ?, disabled = ?, credential_epoch = ?, updated_at = ? WHERE user_id = ?",
                     (identity.password_hash if password_hash is None else password_hash,
                      identity.disabled if disabled is None else disabled, epoch, _timestamp(), user_id))
        if password_hash is not None:
            _invalidate_tokens(conn, user_id)
        return epoch


def set_password(db_path: Path, user_id: str, password: str) -> int:
    return _change_credential(db_path, user_id, password_hash=hash_password(password))


def change_password(
    db_path: Path, policy: Policy, identity: Identity,
    current_password: str, new_password: str, *, ip: str | None,
) -> bool:
    """Replace a verified credential only while its session snapshot is live."""
    error = password_policy_error(new_password, policy, email=identity.email, user_id=identity.user_id)
    if error:
        raise ValueError(error)
    if not check_and_record(db_path, policy, email=identity.email, ip=ip):
        return False
    ok, _ = verify_password(current_password, identity.password_hash or DUMMY_HASH)
    if not ok or not identity.password_hash or identity.disabled:
        return False
    encoded = hash_password(new_password)
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _get_identity(conn, identity.user_id)
        if (current is None or current.disabled or current.email != identity.email
                or current.credential_epoch != identity.credential_epoch
                or current.password_hash != identity.password_hash
                or not _has_profile(conn, identity.user_id)):
            return False
        conn.execute("UPDATE web_auth_identities SET password_hash = ?, credential_epoch = credential_epoch + 1, updated_at = ? WHERE user_id = ?",
                     (encoded, _timestamp(), identity.user_id))
        _invalidate_tokens(conn, identity.user_id)
        return True


def clear_password(db_path: Path, user_id: str) -> int:
    return _change_credential(db_path, user_id, password_hash="")


def set_disabled(db_path: Path, user_id: str, disabled: bool, *, protected_admins: set[str] | None = None) -> int:
    return _change_credential(db_path, user_id, disabled=disabled, protected_admins=protected_admins)


def bump_epoch(db_path: Path, user_id: str) -> int:
    return _change_credential(db_path, user_id)


def _protect_last_admin(conn: sqlite3.Connection, identity: Identity, admins: set[str] | None) -> None:
    if not admins or identity.disabled or identity.user_id not in admins:
        return
    enabled = conn.execute("""SELECT i.user_id FROM web_auth_identities i
        JOIN user_profiles p ON p.user_id = i.user_id WHERE i.disabled = 0""")
    if not any(row["user_id"] in admins and row["user_id"] != identity.user_id for row in enabled):
        raise ValueError("Cannot remove or disable the last enabled admin identity")


def delete_identity(db_path: Path, user_id: str, *, protected_admins: set[str] | None = None) -> bool:
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if protected_admins is not None:
            _protect_last_admin(conn, _require_identity(conn, user_id), protected_admins)
        _invalidate_tokens(conn, user_id)
        return conn.execute("DELETE FROM web_auth_identities WHERE user_id = ?", (user_id,)).rowcount > 0


def touch_login(db_path: Path, user_id: str) -> None:
    with get_db(db_path) as conn:
        conn.execute("UPDATE web_auth_identities SET last_login_at = ? WHERE user_id = ?", (_timestamp(), user_id))


def _issue_token(conn: sqlite3.Connection, identity: Identity, purpose: str, ttl_seconds: int) -> str:
    if purpose not in _PURPOSES or ttl_seconds <= 0:
        raise ValueError("Invalid token purpose or lifetime")
    if identity.disabled or not _has_profile(conn, identity.user_id):
        raise ValueError("Identity is disabled or has no profile")
    token = secrets.token_urlsafe(32)
    # High-entropy tokens need a lookup digest, not an expensive password KDF.
    digest = hashlib.sha256(token.encode()).hexdigest()
    _invalidate_tokens(conn, identity.user_id, purpose)
    conn.execute("INSERT INTO web_auth_tokens (token_hash, user_id, email, purpose, expires_at) VALUES (?, ?, ?, ?, ?)",
                 (digest, identity.user_id, identity.email, purpose, _timestamp(ttl_seconds)))
    return token


def issue_token(db_path: Path, user_id: str, purpose: str, ttl_seconds: int, *, expected_identity: Identity | None = None) -> str:
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        identity = _require_identity(conn, user_id)
        if expected_identity is not None and (identity.email != expected_identity.email
                or identity.credential_epoch != expected_identity.credential_epoch):
            raise ValueError("Identity changed before the link was sent. Try again.")
        return _issue_token(conn, identity, purpose, ttl_seconds)


def _live_token(conn: sqlite3.Connection, token: str, purpose: str | None) -> sqlite3.Row | None:
    if not isinstance(token, str) or not token or len(token) > 256 or not token.isascii():
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    return conn.execute("""
        SELECT t.*, i.password_hash, i.credential_epoch
        FROM web_auth_tokens t JOIN web_auth_identities i ON i.user_id = t.user_id
        JOIN user_profiles p ON p.user_id = i.user_id
        WHERE t.token_hash = ? AND t.used_at IS NULL AND t.expires_at > ?
          AND i.disabled = 0 AND i.email = t.email
          AND (? IS NULL OR t.purpose = ?)
    """, (digest, _timestamp(), purpose, purpose)).fetchone()


def peek_token(db_path: Path, token: str, purpose: str | None = None) -> TokenRecord | None:
    with get_db(db_path) as conn:
        row = _live_token(conn, token, purpose)
        if row is None:
            return None
        return TokenRecord(row["user_id"], row["email"], row["purpose"], row["expires_at"])


def consume_and_set_password(db_path: Path, token: str, purpose: str, password: str, policy: Policy) -> tuple[str, str, int] | None:
    if purpose not in {"enrol", "reset"}:
        return None
    with get_db(db_path) as conn:
        before = _live_token(conn, token, purpose)
    if before is None:
        return None
    error = password_policy_error(password, policy, email=before["email"], user_id=before["user_id"])
    if error:
        raise ValueError(error)
    encoded = hash_password(password)
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _live_token(conn, token, purpose)
        if row is None or row["credential_epoch"] != before["credential_epoch"]:
            return None
        epoch = row["credential_epoch"] + 1
        used = conn.execute("UPDATE web_auth_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL", (_timestamp(), row["id"]))
        if used.rowcount != 1:
            return None
        conn.execute("UPDATE web_auth_identities SET password_hash = ?, credential_epoch = ?, updated_at = ? WHERE user_id = ?",
                     (encoded, epoch, _timestamp(), row["user_id"]))
        _invalidate_tokens(conn, row["user_id"])
        return row["user_id"], row["email"], epoch


def consume_login_token(db_path: Path, token: str) -> tuple[str, str, int] | None:
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _live_token(conn, token, "login")
        if row is None:
            return None
        used = conn.execute("UPDATE web_auth_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL", (_timestamp(), row["id"]))
        if used.rowcount != 1:
            return None
        _invalidate_tokens(conn, row["user_id"], "login")
        conn.execute("UPDATE web_auth_identities SET last_login_at = ? WHERE user_id = ?", (_timestamp(), row["user_id"]))
        return row["user_id"], row["email"], row["credential_epoch"]


def _attempts(conn: sqlite3.Connection, kind: str, key: str, window_seconds: int) -> int:
    return conn.execute("SELECT count(*) FROM web_auth_attempts WHERE kind = ? AND key = ? AND at > ?",
                        (kind, key, _timestamp(-window_seconds))).fetchone()[0]


def attempts_in_window(db_path: Path, kind: str, key: str, window_seconds: int) -> int:
    if kind in {"email", "mail_link"}:
        key = normalize_email(key)
    with get_db(db_path) as conn:
        return _attempts(conn, kind, key, window_seconds)


def prune_attempts(db_path: Path, older_than_seconds: int) -> int:
    if older_than_seconds <= 0:
        raise ValueError("Retention must be positive")
    with get_db(db_path) as conn:
        return conn.execute("DELETE FROM web_auth_attempts WHERE at <= ?", (_timestamp(-older_than_seconds),)).rowcount


def check_and_record(db_path: Path, policy: Policy, *, email: str, ip: str | None) -> bool:
    email = normalize_email(email)
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if _attempts(conn, "email", email, policy.throttle_window_seconds) >= policy.throttle_max_email:
            return False
        if ip is not None and _attempts(conn, "ip", ip, policy.throttle_window_seconds) >= policy.throttle_max_ip:
            return False
        conn.execute("DELETE FROM web_auth_attempts WHERE kind IN ('email', 'ip') AND at <= ?", (_timestamp(-policy.throttle_window_seconds),))
        now = _timestamp()
        conn.execute("INSERT INTO web_auth_attempts (kind, key, at) VALUES ('email', ?, ?)", (email, now))
        if ip is not None:
            conn.execute("INSERT INTO web_auth_attempts (kind, key, at) VALUES ('ip', ?, ?)", (ip, now))
        return True


def issue_mail_link_if_allowed(db_path: Path, policy: Policy, email: str, purpose: str) -> tuple[str, Identity] | None:
    if purpose not in {"login", "reset"}:
        raise ValueError("Mail link purpose must be login or reset")
    email = normalize_email(email)
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        identity = _identity(conn.execute("SELECT * FROM web_auth_identities WHERE email = ?", (email,)).fetchone())
        if identity is None or identity.disabled or not _has_profile(conn, identity.user_id):
            return None
        if _attempts(conn, "mail_link", email, 3600) >= policy.mail_link_max_email:
            return None
        ttl = policy.login_link_ttl_seconds if purpose == "login" else policy.reset_ttl_seconds
        token = _issue_token(conn, identity, purpose, ttl)
        conn.execute("DELETE FROM web_auth_attempts WHERE kind = 'mail_link' AND at <= ?", (_timestamp(-3600),))
        conn.execute("INSERT INTO web_auth_attempts (kind, key, at) VALUES ('mail_link', ?, ?)", (email, _timestamp()))
        return token, identity


def authenticate(db_path: Path, policy: Policy, email: str, password: str, *, ip: str | None) -> tuple[str, Identity | None]:
    try:
        email = normalize_email(email)
        if not check_and_record(db_path, policy, email=email, ip=ip):
            return "throttled", None
        identity = get_identity_by_email(db_path, email)
        encoded = identity.password_hash if identity and identity.password_hash else DUMMY_HASH
        ok, rehash = verify_password(password, encoded)
        if not ok or identity is None or not identity.password_hash or identity.disabled:
            return "bad", None
        upgraded = hash_password(password) if rehash else identity.password_hash
        # KDF work runs outside the write lock. Do not mint a session from a
        # password or epoch that changed while it ran, or overwrite a reset.
        with get_db(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = _get_identity(conn, identity.user_id)
            if (current is None or current.disabled or current.email != identity.email
                    or current.credential_epoch != identity.credential_epoch
                    or current.password_hash != identity.password_hash
                    or not _has_profile(conn, identity.user_id)):
                return "bad", None
            conn.execute("UPDATE web_auth_identities SET password_hash = ?, last_login_at = ? WHERE user_id = ?",
                         (upgraded, _timestamp(), identity.user_id))
            return "ok", _require_identity(conn, identity.user_id)
    except Exception as exc:
        # Keep frames for diagnosis, but never exception text: backend errors
        # can include SQL parameters or credentials supplied by the caller.
        logger.error("Web authentication failed (%s)\n%s", type(exc).__name__, "".join(traceback.format_tb(exc.__traceback__)))
        return "bad", None
