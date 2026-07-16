"""User-scoped Nextcloud OAuth token custody — web process only.

Retains the OAuth2 access/refresh pair minted at web login, encrypted at rest
with a Fernet key derived from ``$ISTOTA_WEB_TOKEN_KEY`` — a *separate* key
from ``secrets_store``'s ``$ISTOTA_SECRET_KEY``, delivered only to the web
unit. Nextcloud OAuth2 has no scopes (a token is full account access), so who
can decrypt must stay auditable by grep: distinct env var, distinct scrypt
salt, distinct table. The scheduler and webhook units never load this module's
decrypt path; the LLM sandbox sees only undecryptable ciphertext rows in the
read-only framework DB.

Consumers (all in the web process): post-as-user Talk mirroring at ingest,
read-marker sync, and the settings status/disconnect card.

Refresh contract (Nextcloud rotates refresh tokens): every refresh returns a
new access+refresh pair and invalidates the old refresh token, so persistence
is a single atomic UPDATE and refresh is serialized per user via a
process-local lock (the web app is a single process; nothing else refreshes).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import httpx

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("istota.web_tokens")

# Same length floor and scrypt parameters as secrets_store._derive_fernet_key
# (the reference implementation) — but a distinct salt, so the two keyspaces
# can never be conflated even if an operator reuses the same passphrase.
_KEY_ENV_VAR = "ISTOTA_WEB_TOKEN_KEY"
_MIN_KEY_LEN = 32
_SCRYPT_SALT = b"istota-web-tokens-v1"
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1

# Refresh when the access token is within this many seconds of expiry.
_REFRESH_MARGIN_SECONDS = 60
_REFRESH_TIMEOUT_SECONDS = 10.0

# Per-user refresh serialization. Process-local by design — the web app is a
# single process and the scheduler never refreshes. Guarded by _locks_guard so
# two requests can't race the dict itself.
_refresh_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class WebTokenKeyMissingError(RuntimeError):
    """Raised when an encrypt/decrypt operation runs without ISTOTA_WEB_TOKEN_KEY."""


class WebTokenKeyTooWeakError(RuntimeError):
    """Raised when ISTOTA_WEB_TOKEN_KEY is shorter than the minimum length."""


def token_key_available() -> bool:
    """True iff $ISTOTA_WEB_TOKEN_KEY is present and meets the length floor.

    Safe to call at import time — never raises.
    """
    secret = os.environ.get(_KEY_ENV_VAR, "").strip()
    return bool(secret) and len(secret) >= _MIN_KEY_LEN


def feature_enabled(config: "Config") -> bool:
    """The single opt-in gate: `[web] token_storage = "encrypted"` AND the
    web-only key present. Every consuming call site checks this and falls
    through to legacy behaviour when False."""
    return config.web.token_storage == "encrypted" and token_key_available()


def _validated_key() -> str:
    secret = os.environ.get(_KEY_ENV_VAR, "").strip()
    if not secret:
        raise WebTokenKeyMissingError(
            f"{_KEY_ENV_VAR} is not set; cannot encrypt or decrypt stored web tokens."
        )
    if len(secret) < _MIN_KEY_LEN:
        raise WebTokenKeyTooWeakError(
            f"{_KEY_ENV_VAR} must be at least {_MIN_KEY_LEN} characters "
            f"(got {len(secret)}). Generate one with "
            f'`python3 -c "import secrets; print(secrets.token_hex(32))"`.'
        )
    return secret


def _derive_fernet_key(secret: str) -> bytes:
    derived = hashlib.scrypt(
        secret.encode("utf-8"),
        salt=_SCRYPT_SALT,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return base64.urlsafe_b64encode(derived)


def _get_fernet():
    """Build a Fernet instance from $ISTOTA_WEB_TOKEN_KEY (lazy import)."""
    from cryptography.fernet import Fernet  # noqa: PLC0415

    return Fernet(_derive_fernet_key(_validated_key()))


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Short-lived connection matching secrets_store's conventions (WAL is
    persistent in the file header; never re-issued per open)."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create the table if a pre-migration DB is in play (a web unit deployed
    ahead of the scheduler's init_db run). Matches db._run_migrations."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS web_user_tokens (
            user_id       TEXT PRIMARY KEY,
            access_token  TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)


def _user_lock(user_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _refresh_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _refresh_locks[user_id] = lock
        return lock


def _expires_at(expires_in: int | float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))
    ).isoformat()


def store_tokens(
    db_path: Path,
    user_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: int | float,
) -> None:
    """Encrypt and upsert the user's OAuth pair. Called by the login callback
    (every successful login overwrites — a dead refresh token self-heals at
    next login) and by the refresh path with a rotated pair."""
    fernet = _get_fernet()
    access_ct = fernet.encrypt(access_token.encode("utf-8")).decode("ascii")
    refresh_ct = fernet.encrypt(refresh_token.encode("utf-8")).decode("ascii")
    with _connect(db_path) as conn:
        _ensure_table(conn)
        conn.execute(
            """
            INSERT INTO web_user_tokens
                (user_id, access_token, refresh_token, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                expires_at = excluded.expires_at,
                updated_at = datetime('now')
            """,
            (user_id, access_ct, refresh_ct, _expires_at(expires_in)),
        )


def delete_tokens(db_path: Path, user_id: str) -> bool:
    """Delete the user's stored pair (the settings Disconnect action, and the
    invalid_grant / undecryptable-row self-heal). Returns True if a row was
    removed."""
    try:
        with _connect(db_path) as conn:
            cur = conn.execute(
                "DELETE FROM web_user_tokens WHERE user_id = ?", (user_id,)
            )
            return cur.rowcount > 0
    except sqlite3.OperationalError:
        return False  # table absent — nothing to delete


def token_status(db_path: Path, user_id: str) -> dict | None:
    """`{connected: True, expires_at: str}` without decrypting (for the
    settings UI), or None when no row is stored."""
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT expires_at FROM web_user_tokens WHERE user_id = ?",
                (user_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return {"connected": True, "expires_at": row["expires_at"]}


def _token_endpoint(config: "Config") -> str:
    return (
        config.web.oauth2_token_endpoint
        or f"{config.web.oauth2_provider.rstrip('/')}/index.php/apps/oauth2/api/v1/token"
    )


def get_access_token(
    db_path: Path,
    config: "Config",
    user_id: str,
    *,
    force_refresh: bool = False,
) -> str | None:
    """The workhorse: return a live access token for `user_id`, refreshing the
    pair first when within `_REFRESH_MARGIN_SECONDS` of expiry (or when
    `force_refresh` — the caller saw a 401 on a supposedly-live token).

    Returns None (never raises to callers) when: the key is absent, no row is
    stored, the row won't decrypt (deleted + WARNING — key rotation), or the
    refresh fails. A definitive refresh failure (400/401, e.g. invalid_grant —
    revoked in NC security settings) deletes the row so the next web login
    re-mints; a transient one (network, 5xx) keeps the row for a later retry.
    """
    if not token_key_available():
        return None
    fernet = _get_fernet()

    with _user_lock(user_id):
        # Re-read under the lock: a concurrent request may have just rotated
        # the pair; using the stale refresh token would kill the row.
        try:
            with _connect(db_path) as conn:
                row = conn.execute(
                    "SELECT access_token, refresh_token, expires_at "
                    "FROM web_user_tokens WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
        except sqlite3.OperationalError:
            return None  # table absent (pre-migration DB)
        if row is None:
            return None

        try:
            access_token = fernet.decrypt(row["access_token"].encode("ascii")).decode("utf-8")
            refresh_token = fernet.decrypt(row["refresh_token"].encode("ascii")).decode("utf-8")
        except Exception:
            logger.warning(
                "web token decrypt failed user=%s — deleting row "
                "(rotated ISTOTA_WEB_TOKEN_KEY?)",
                user_id,
            )
            delete_tokens(db_path, user_id)
            return None

        needs_refresh = force_refresh
        if not needs_refresh:
            try:
                expires = datetime.fromisoformat(row["expires_at"])
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                remaining = (expires - datetime.now(timezone.utc)).total_seconds()
                needs_refresh = remaining < _REFRESH_MARGIN_SECONDS
            except (ValueError, TypeError):
                needs_refresh = True  # unparseable expiry — treat as expired

        if not needs_refresh:
            return access_token

        return _refresh(db_path, config, user_id, refresh_token)


def _refresh(
    db_path: Path, config: "Config", user_id: str, refresh_token: str,
) -> str | None:
    """One refresh attempt against the NC token endpoint. Caller holds the
    per-user lock. Persists the rotated pair on success."""
    endpoint = _token_endpoint(config)
    try:
        resp = httpx.post(
            endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": config.web.oauth2_client_id,
                "client_secret": config.web.oauth2_client_secret,
            },
            timeout=_REFRESH_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        logger.warning("web token refresh failed (transient) user=%s: %s", user_id, e)
        return None

    if resp.status_code in (400, 401):
        # Definitive: revoked/expired refresh token (invalid_grant). Delete so
        # the settings card shows "not connected"; next login re-mints.
        logger.warning(
            "web token refresh rejected (%d) user=%s — deleting stored pair",
            resp.status_code, user_id,
        )
        delete_tokens(db_path, user_id)
        return None
    if resp.status_code != 200:
        logger.warning(
            "web token refresh failed (transient %d) user=%s",
            resp.status_code, user_id,
        )
        return None

    try:
        body = resp.json()
        new_access = body["access_token"]
        new_refresh = body.get("refresh_token") or refresh_token
        expires_in = body.get("expires_in", 3600)
    except (ValueError, KeyError, TypeError) as e:
        logger.warning("web token refresh: malformed response user=%s: %s", user_id, e)
        return None

    store_tokens(db_path, user_id, new_access, new_refresh, expires_in)
    logger.debug("web token refreshed user=%s", user_id)
    return new_access
