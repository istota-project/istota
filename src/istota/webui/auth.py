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

from istota.db import get_db

logger = logging.getLogger(__name__)
_N, _R, _P = 32768, 8, 1
_MAX_PASSWORD_BYTES = 1024
_PURPOSES = {"enrol", "reset"}
# A six-digit code is bound to one client's secret and dies after this many
# wrong guesses (ISSUE-574). Anyone can open a request for any address, so the
# per-address daily cap is what bounds a guesser: 20 a day, about 2e-5.
SIGN_IN_CODE_ATTEMPTS = 5
SIGN_IN_CODE_DAILY_FAILURES = 20


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
    sign_in_code_ttl_seconds: int
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
        sign_in_code_ttl_seconds=web.auth_sign_in_code_ttl_minutes * 60,
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


def _retired_epoch(conn: sqlite3.Connection, user_id: str) -> int:
    row = conn.execute("SELECT credential_epoch FROM web_auth_retired_epochs WHERE user_id = ?", (user_id,)).fetchone()
    return row["credential_epoch"] if row else 0


def get_retired_epoch(db_path: Path, user_id: str) -> int:
    """Return the generation retained after identity removal, or zero if never removed."""
    with get_db(db_path) as conn:
        return _retired_epoch(conn, user_id)


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
    reject_address_holders: bool = False, conn: sqlite3.Connection | None = None,
) -> Identity:
    """Attach or change ``user_id``'s login email.

    ``reject_address_holders`` also refuses an address another user holds in
    their ``email_addresses``: mail to it would route to that user while the
    login belongs to this one. Pass ``conn`` to run inside the caller's
    transaction, uncommitted; otherwise this opens its own `BEGIN IMMEDIATE`.
    """
    email = normalize_email(email)
    if not email:
        raise ValueError("Email must not be empty")
    kwargs = dict(create_profile=create_profile, display_name=display_name,
                  reject_case_collision=reject_case_collision,
                  reject_address_holders=reject_address_holders)
    if conn is not None:
        return _upsert_identity(conn, user_id, email, **kwargs)
    with get_db(db_path) as own:
        own.execute("BEGIN IMMEDIATE")
        return _upsert_identity(own, user_id, email, **kwargs)


def _upsert_identity(
    conn: sqlite3.Connection, user_id: str, email: str, *, create_profile: bool,
    display_name: str, reject_case_collision: bool, reject_address_holders: bool,
) -> Identity:
    if reject_case_collision:
        for row in conn.execute("SELECT user_id FROM user_profiles UNION SELECT user_id FROM web_auth_identities"):
            if row["user_id"] != user_id and row["user_id"].casefold() == user_id.casefold():
                raise ValueError(f"User ID differs only in case from {row['user_id']}")
    conflict = conn.execute("SELECT user_id FROM web_auth_identities WHERE email = ? AND user_id != ?",
                            (email, user_id)).fetchone()
    if conflict and reject_case_collision:
        raise ValueError(f"That address is already a login for {conflict['user_id']}")
    current = _get_identity(conn, user_id)
    # Only a *new* login email is judged: resubmitting the one already held
    # passes even beside a duplicate stored before the rule, the way
    # `find_identity_conflicts` passes an address already on the user's list.
    if reject_address_holders and (current is None or current.email != email):
        from istota.user_profiles import find_identity_conflicts
        holder = find_identity_conflicts(conn, user_id, email_addresses=[email]).get(email)
        if holder:
            raise ValueError(f"That address is already an email address of {holder}")
    if not _has_profile(conn, user_id):
        if not create_profile:
            raise ValueError("No user profile for this identity")
        if not valid_new_user_id(user_id):
            raise ValueError("New user IDs must be 1–32 lowercase letters, digits, dots, underscores or hyphens, starting with a letter or digit; they become directory names.")
        from istota.user_profiles import UserProfile, insert_profile
        insert_profile(conn, UserProfile(user_id=user_id, display_name=display_name or user_id))
    identity = _get_identity(conn, user_id)
    if identity is None:
        # A fixed epoch would revive old cookies after remove/re-add.
        # Leave half the integer range available for monotonic increments.
        epoch = max(secrets.randbelow(2**62 - 1) + 1, _retired_epoch(conn, user_id) + 1)
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
        identity = _get_identity(conn, user_id)
        if identity is None:
            return False
        epoch = max(identity.credential_epoch, _retired_epoch(conn, user_id)) + 1
        conn.execute("""INSERT INTO web_auth_retired_epochs (user_id, credential_epoch) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET credential_epoch = excluded.credential_epoch""", (user_id, epoch))
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


def _mailable_identity(conn: sqlite3.Connection, policy: Policy, email: str) -> Identity | None:
    """The identity a self-service mail may go to, spending one unit of its hourly budget.

    One budget across reset links and sign-in codes, so neither can flood an
    inbox the other is limited for.
    """
    identity = _identity(conn.execute("SELECT * FROM web_auth_identities WHERE email = ?", (email,)).fetchone())
    if identity is None or identity.disabled or not _has_profile(conn, identity.user_id):
        return None
    if _attempts(conn, "mail_link", email, 3600) >= policy.mail_link_max_email:
        return None
    conn.execute("DELETE FROM web_auth_attempts WHERE kind = 'mail_link' AND at <= ?", (_timestamp(-3600),))
    conn.execute("INSERT INTO web_auth_attempts (kind, key, at) VALUES ('mail_link', ?, ?)", (email, _timestamp()))
    return identity


def issue_mail_link_if_allowed(db_path: Path, policy: Policy, email: str, purpose: str) -> tuple[str, Identity] | None:
    if purpose != "reset":
        raise ValueError("Mail link purpose must be reset")
    email = normalize_email(email)
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        identity = _mailable_identity(conn, policy, email)
        if identity is None:
            return None
        return _issue_token(conn, identity, purpose, policy.reset_ttl_seconds), identity


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _code_digest(request_id: str, code: str) -> str:
    # Salted by the request so equal codes on two requests differ. Six digits
    # are trivially reversible offline; the database is the boundary.
    return _digest(f"{request_id}:{code}")


def start_sign_in(db_path: Path, policy: Policy, email: str, secret: str) -> str:
    """Open a pending email sign-in for whichever client holds ``secret``.

    Created for every address, known or not, so the row says nothing about who
    can sign in. A client asking again reuses its secret; its older requests
    stay live until a newer one is actually sent a code.
    """
    if not isinstance(secret, str) or len(secret) < 16 or not secret.isascii():
        raise ValueError("Sign-in secret is too short")
    if policy.sign_in_code_ttl_seconds <= 0:
        raise ValueError("Sign-in code lifetime must be positive")
    request_id = secrets.token_urlsafe(18)
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM web_auth_sign_ins WHERE expires_at <= ?", (_timestamp(),))
        conn.execute("""INSERT INTO web_auth_sign_ins (request_id, secret_hash, email, expires_at)
            VALUES (?, ?, ?, ?)""", (request_id, _digest(secret), normalize_email(email),
                                     _timestamp(policy.sign_in_code_ttl_seconds)))
    return request_id


_LIVE_SIGN_IN = "used_at IS NULL AND expires_at > ? AND attempts < ?"


def _live_sign_in(conn: sqlite3.Connection, request_id: object) -> sqlite3.Row | None:
    if not isinstance(request_id, str) or not request_id or len(request_id) > 64 or not request_id.isascii():
        return None
    return conn.execute(f"SELECT * FROM web_auth_sign_ins WHERE request_id = ? AND {_LIVE_SIGN_IN}",
                        (request_id, _timestamp(), SIGN_IN_CODE_ATTEMPTS)).fetchone()


def _set_code(conn: sqlite3.Connection, row: sqlite3.Row, identity: Identity) -> str:
    code = f"{secrets.randbelow(10**6):06d}"
    conn.execute("""UPDATE web_auth_sign_ins SET code_hash = ?, user_id = ?, credential_epoch = ?, attempts = 0
        WHERE id = ?""", (_code_digest(row["request_id"], code), identity.user_id, identity.credential_epoch, row["id"]))
    return code


def issue_sign_in_code_if_allowed(db_path: Path, policy: Policy, request_id: str) -> tuple[str, Identity] | None:
    """Mint the code to email for a pending request, once, within the mail budget.

    ``None`` for an unknown, disabled or over-budget address: that request then
    has no code. Only a mint retires the same client's older requests, so asking
    again past the budget leaves the code already sent working.
    """
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _live_sign_in(conn, request_id)
        if row is None or row["code_hash"] is not None:
            return None
        identity = _mailable_identity(conn, policy, row["email"])
        if identity is None:
            return None
        conn.execute("UPDATE web_auth_sign_ins SET used_at = ? WHERE secret_hash = ? AND id != ? AND used_at IS NULL",
                     (_timestamp(), row["secret_hash"], row["id"]))
        return _set_code(conn, row, identity), identity


@dataclass(frozen=True)
class MintedCode:
    code: str
    requested_at: str
    expires_at: str
    pending: int


def mint_sign_in_code(db_path: Path, user_id: str) -> MintedCode:
    """Operator recovery: a fresh code for the newest pending sign-in for the user's address.

    Replaces any emailed code on that request and clears the address's failure
    budget. Anyone can open a request for an address, so the operator checks
    ``requested_at`` and ``pending`` with the user before reading the code out.
    """
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        identity = _require_identity(conn, user_id)
        if identity.disabled or not _has_profile(conn, user_id):
            raise ValueError("Identity is disabled or has no profile")
        rows = conn.execute(f"SELECT * FROM web_auth_sign_ins WHERE email = ? AND {_LIVE_SIGN_IN} ORDER BY id DESC",
                            (identity.email, _timestamp(), SIGN_IN_CODE_ATTEMPTS)).fetchall()
        if not rows:
            raise ValueError("No pending sign-in for this user; ask them to request a code on the sign-in page first")
        conn.execute("DELETE FROM web_auth_attempts WHERE kind = 'sign_in_code' AND key = ?", (identity.email,))
        return MintedCode(_set_code(conn, rows[0], identity), rows[0]["created_at"], rows[0]["expires_at"], len(rows))


def redeem_sign_in_code(db_path: Path, secret: str, code: str) -> tuple[str, tuple[str, str, int] | None]:
    """``("ok", (user_id, email, epoch))``, ``("bad", None)`` or ``("dead", None)``.

    Checks the newest of this client's live requests that has a code. A wrong
    secret is ``dead`` and costs no attempt, so a stranger cannot burn somebody
    else's request. Wrong codes are also counted per address over a day: a
    stranger can open requests for any address from their own browser, and
    without that count the mail budget alone would bound their guesses.
    """
    if (not isinstance(secret, str) or not secret.isascii() or len(secret) > 256
            or not isinstance(code, str) or len(code) > 64):
        return "dead", None
    typed = re.sub(r"[\s-]", "", code)
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(f"SELECT * FROM web_auth_sign_ins WHERE secret_hash = ? AND {_LIVE_SIGN_IN} ORDER BY id DESC",
                            (_digest(secret), _timestamp(), SIGN_IN_CODE_ATTEMPTS)).fetchall()
        if not rows:
            return "dead", None
        row = next((r for r in rows if r["code_hash"] is not None), rows[0])
        now = _timestamp()
        if _attempts(conn, "sign_in_code", row["email"], 86400) >= SIGN_IN_CODE_DAILY_FAILURES:
            conn.execute("UPDATE web_auth_sign_ins SET used_at = ? WHERE id = ?", (now, row["id"]))
            return "dead", None
        matched = (row["code_hash"] is not None and typed.isascii() and typed.isdigit()
                   and hmac.compare_digest(row["code_hash"], _code_digest(row["request_id"], typed)))
        if not matched:
            attempts = row["attempts"] + 1
            spent = attempts >= SIGN_IN_CODE_ATTEMPTS
            conn.execute("UPDATE web_auth_sign_ins SET attempts = ?, used_at = ? WHERE id = ?",
                         (attempts, now if spent else None, row["id"]))
            conn.execute("DELETE FROM web_auth_attempts WHERE kind = 'sign_in_code' AND at <= ?", (_timestamp(-86400),))
            conn.execute("INSERT INTO web_auth_attempts (kind, key, at) VALUES ('sign_in_code', ?, ?)",
                         (row["email"], now))
            return ("dead" if spent else "bad"), None
        conn.execute("UPDATE web_auth_sign_ins SET used_at = ? WHERE id = ?", (now, row["id"]))
        identity = _get_identity(conn, row["user_id"])
        if (identity is None or identity.disabled or identity.email != row["email"]
                or identity.credential_epoch != row["credential_epoch"]
                or not _has_profile(conn, identity.user_id)):
            return "dead", None
        conn.execute("UPDATE web_auth_sign_ins SET used_at = ? WHERE (user_id = ? OR secret_hash = ?) AND used_at IS NULL",
                     (now, identity.user_id, row["secret_hash"]))
        conn.execute("UPDATE web_auth_identities SET last_login_at = ? WHERE user_id = ?", (now, identity.user_id))
        return "ok", (identity.user_id, identity.email, identity.credential_epoch)


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
