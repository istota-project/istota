"""Garmin Connect integration for the health module.

Two surfaces:

* **Auth lifecycle** — :func:`connect`, :func:`complete_mfa`, :func:`disconnect`,
  :func:`get_status`. Tokens live in ``health_settings`` under the key
  ``garmin_tokens`` (JSON blob). The blob is never returned by any API
  endpoint — only ``garmin_connected`` / ``garmin_email`` / ``last_sync`` /
  ``error`` are exposed.
* **Adapter abstraction** — :class:`GarminAdapter` Protocol with a
  production implementation that wraps :mod:`garminconnect`. Tests inject
  a fake adapter via :data:`_adapter_factory`.

The sync engine (next stage) builds on these primitives.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from istota.health import db as health_db


logger = logging.getLogger(__name__)


SETTINGS_KEY_TOKENS = "garmin_tokens"
SETTINGS_KEY_ERROR = "garmin_error"

# How long a partially-authenticated Garmin client stays in the
# pending-auth cache between ``connect`` (MFA-required) and
# ``complete_mfa``. Matches the spec.
PENDING_AUTH_TTL_SEC = 300


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GarminAuthError(Exception):
    """Raised for any user-correctable auth failure (bad creds, MFA wrong,
    pending-auth expired, token refresh failed)."""


class GarminNotInstalled(RuntimeError):
    """Raised when the ``garminconnect`` extra isn't installed."""


# ---------------------------------------------------------------------------
# Adapter protocol — keeps the rest of the module independent of the
# garminconnect SDK shape. Tests inject a fake adapter via
# ``set_adapter_factory``.
# ---------------------------------------------------------------------------


@dataclass
class ConnectResult:
    """Outcome of an auth attempt.

    ``status``:

    * ``"ok"`` — fully authenticated; ``tokens`` is populated.
    * ``"mfa_required"`` — partially authenticated, awaiting TOTP; the
      adapter is held in the pending-auth cache.
    """
    status: str
    tokens: dict[str, Any] | None = None
    email: str | None = None
    prompt: str | None = None


class GarminAdapter(Protocol):
    """Thin façade over :mod:`garminconnect`.

    Implementations encapsulate the SDK's session/serialisation API. The
    auth functions in this module talk to the adapter, not the SDK.
    """

    def login(self, email: str, password: str) -> ConnectResult: ...

    def resume_mfa(self, code: str) -> ConnectResult: ...

    def serialize_tokens(self) -> dict[str, Any]:
        """Return a JSON-safe dict suitable for storage in ``health_settings``."""

    def load_tokens(self, tokens: dict[str, Any]) -> None:
        """Restore session state from a previously serialised blob."""

    # Sync engine uses these — implemented in stage 2. Stubbed here so the
    # Protocol is complete.
    def get_user_profile(self) -> dict[str, Any] | None: ...


# ---------------------------------------------------------------------------
# Adapter factory — overridable for tests
# ---------------------------------------------------------------------------


_AdapterFactory = Callable[[], GarminAdapter]
_adapter_factory: _AdapterFactory | None = None


def set_adapter_factory(factory: _AdapterFactory | None) -> None:
    """Install (or reset) the global adapter factory.

    Tests call this with a factory that returns a fake adapter. Production
    code leaves it as ``None`` so :func:`_build_adapter` lazily wires up
    the real ``garminconnect``-backed implementation.
    """
    global _adapter_factory
    _adapter_factory = factory


def _build_adapter() -> GarminAdapter:
    if _adapter_factory is not None:
        return _adapter_factory()
    return _RealGarminAdapter()


# ---------------------------------------------------------------------------
# Production adapter (lazy-imports garminconnect)
# ---------------------------------------------------------------------------


class _RealGarminAdapter:
    """Real adapter that wraps ``garminconnect.Garmin``.

    The garminconnect library serialises its OAuth state to a dict via its
    own ``garth``-derived session helpers; the exact shape is opaque to
    this module — we round-trip it as a JSON blob and let the SDK
    deserialise on the other side.
    """

    def __init__(self) -> None:
        try:
            import garminconnect  # noqa: PLC0415
        except ImportError as exc:
            raise GarminNotInstalled(
                "garminconnect not installed; run `uv sync --extra garmin`"
            ) from exc
        self._sdk = garminconnect
        self._client: Any | None = None

    def login(self, email: str, password: str) -> ConnectResult:
        try:
            client = self._sdk.Garmin(email=email, password=password)
            result = client.login()
        except Exception as exc:  # noqa: BLE001 — surface to caller verbatim
            msg = str(exc)
            if _looks_like_mfa(msg):
                # Some library versions raise from login() when MFA is
                # required; the partial session is still on ``client``.
                self._client = client
                return ConnectResult(status="mfa_required", prompt="Enter Garmin MFA code")
            raise GarminAuthError(f"Garmin login failed: {msg}") from exc

        self._client = client
        # Some library versions return a tuple ``(client_state, needs_mfa)``
        # from login(); others a plain success. Handle both.
        if isinstance(result, tuple) and len(result) >= 2 and result[1]:
            return ConnectResult(status="mfa_required", prompt="Enter Garmin MFA code")
        return ConnectResult(
            status="ok",
            tokens=self.serialize_tokens(),
            email=email,
        )

    def resume_mfa(self, code: str) -> ConnectResult:
        if self._client is None:
            raise GarminAuthError("no pending Garmin auth — restart connect")
        try:
            self._client.resume_login(code)
        except Exception as exc:  # noqa: BLE001
            raise GarminAuthError(f"MFA verification failed: {exc}") from exc
        return ConnectResult(status="ok", tokens=self.serialize_tokens())

    def serialize_tokens(self) -> dict[str, Any]:
        if self._client is None:
            raise GarminAuthError("not connected")
        # The garminconnect Garmin object exposes its garth session, which
        # has a dump() helper that returns the OAuth1+OAuth2 state. Newer
        # versions expose ``garth_client.dumps()``. Try both.
        client = self._client
        garth = getattr(client, "garth", None)
        if garth is not None and hasattr(garth, "dumps"):
            return {"garth": garth.dumps()}
        # Fallback shape: pull the inner attributes directly. Tests cover
        # both shapes via the fake adapter.
        return {
            "oauth1_token": getattr(client, "oauth1_token", None),
            "oauth2_token": getattr(client, "oauth2_token", None),
        }

    def load_tokens(self, tokens: dict[str, Any]) -> None:
        client = self._sdk.Garmin()
        garth = getattr(client, "garth", None)
        if garth is not None and "garth" in tokens and hasattr(garth, "loads"):
            garth.loads(tokens["garth"])
        else:
            for k, v in tokens.items():
                if hasattr(client, k):
                    setattr(client, k, v)
        self._client = client

    def get_user_profile(self) -> dict[str, Any] | None:
        if self._client is None:
            return None
        try:
            return self._client.get_user_profile()
        except Exception as exc:  # noqa: BLE001
            logger.warning("garmin get_user_profile failed: %s", exc)
            return None


def _looks_like_mfa(msg: str) -> bool:
    lo = msg.lower()
    return "mfa" in lo or "two-factor" in lo or "totp" in lo


# ---------------------------------------------------------------------------
# Pending-auth cache (in-memory; cleared on process restart)
# ---------------------------------------------------------------------------


@dataclass
class _PendingAuth:
    adapter: GarminAdapter
    email: str
    created_at: float


_PENDING: dict[str, _PendingAuth] = {}
_PENDING_LOCK = threading.Lock()


def _gc_pending(now: float) -> None:
    """Drop expired pending-auth entries. Caller holds ``_PENDING_LOCK``."""
    expired = [
        uid for uid, p in _PENDING.items()
        if now - p.created_at > PENDING_AUTH_TTL_SEC
    ]
    for uid in expired:
        _PENDING.pop(uid, None)


def _stash_pending(user_id: str, adapter: GarminAdapter, email: str) -> None:
    now = time.monotonic()
    with _PENDING_LOCK:
        _gc_pending(now)
        _PENDING[user_id] = _PendingAuth(adapter=adapter, email=email, created_at=now)


def _take_pending(user_id: str) -> _PendingAuth | None:
    now = time.monotonic()
    with _PENDING_LOCK:
        _gc_pending(now)
        return _PENDING.pop(user_id, None)


def clear_pending(user_id: str | None = None) -> None:
    """Test helper / explicit reset. ``None`` clears every pending entry."""
    with _PENDING_LOCK:
        if user_id is None:
            _PENDING.clear()
        else:
            _PENDING.pop(user_id, None)


# ---------------------------------------------------------------------------
# Token persistence helpers (DB I/O)
# ---------------------------------------------------------------------------


def store_tokens(
    conn: sqlite3.Connection, tokens: dict[str, Any], *, email: str | None = None,
) -> None:
    """Upsert the Garmin token blob and clear any prior error."""
    blob = dict(tokens)
    if email is not None:
        blob["email"] = email
    blob["last_sync"] = blob.get("last_sync")  # preserved if caller set it
    health_db.set_setting(conn, SETTINGS_KEY_TOKENS, blob)
    health_db.delete_setting(conn, SETTINGS_KEY_ERROR)


def load_tokens(conn: sqlite3.Connection) -> dict[str, Any] | None:
    settings = health_db.get_settings(conn)
    blob = settings.get(SETTINGS_KEY_TOKENS)
    return blob if isinstance(blob, dict) else None


def clear_tokens(conn: sqlite3.Connection) -> None:
    health_db.delete_setting(conn, SETTINGS_KEY_TOKENS)
    health_db.delete_setting(conn, SETTINGS_KEY_ERROR)


def mark_token_error(conn: sqlite3.Connection, reason: str) -> None:
    """Record a non-retryable token error (token_expired, revoked, …).

    Surfaced via :func:`get_status` so the UI can prompt re-auth.
    """
    health_db.set_setting(conn, SETTINGS_KEY_ERROR, reason)


def update_last_sync(conn: sqlite3.Connection, *, when: datetime | None = None) -> None:
    blob = load_tokens(conn)
    if not blob:
        return
    blob["last_sync"] = (when or datetime.now(timezone.utc)).isoformat()
    health_db.set_setting(conn, SETTINGS_KEY_TOKENS, blob)


# ---------------------------------------------------------------------------
# Public API used by the web routes and CLI
# ---------------------------------------------------------------------------


def get_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the public-facing connection status.

    The token blob is **never** included. Only the fields that the
    settings UI and dashboard render: connected flag, email, last sync,
    error string.
    """
    blob = load_tokens(conn)
    error = health_db.get_settings(conn).get(SETTINGS_KEY_ERROR)
    if not blob:
        return {
            "connected": False,
            "email": None,
            "last_sync": None,
            "error": error if isinstance(error, str) else None,
        }
    return {
        "connected": True,
        "email": blob.get("email"),
        "last_sync": blob.get("last_sync"),
        "error": error if isinstance(error, str) else None,
    }


def connect(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    email: str,
    password: str,
) -> dict[str, Any]:
    """Start a Garmin OAuth flow.

    Returns a status dict:

    * ``{"status": "ok"}`` on success — tokens are persisted.
    * ``{"status": "mfa_required", "prompt": "Enter TOTP code"}`` — the
      caller must follow up with :func:`complete_mfa` within
      ``PENDING_AUTH_TTL_SEC`` seconds.

    Credentials are not stored — only the resulting OAuth tokens are.
    """
    if not email or not password:
        raise GarminAuthError("email and password are required")

    adapter = _build_adapter()
    result = adapter.login(email, password)

    if result.status == "mfa_required":
        _stash_pending(user_id, adapter, email)
        return {
            "status": "mfa_required",
            "prompt": result.prompt or "Enter Garmin MFA code",
        }

    if result.status != "ok" or not result.tokens:
        raise GarminAuthError("unexpected Garmin login result")

    store_tokens(conn, result.tokens, email=result.email or email)
    conn.commit()
    clear_pending(user_id)
    return {"status": "ok"}


def complete_mfa(
    conn: sqlite3.Connection, *, user_id: str, code: str,
) -> dict[str, Any]:
    """Complete the MFA step started by :func:`connect`."""
    pending = _take_pending(user_id)
    if pending is None:
        raise GarminAuthError(
            "no pending Garmin auth — restart from /garmin/connect"
        )
    result = pending.adapter.resume_mfa(code)
    if result.status != "ok" or not result.tokens:
        raise GarminAuthError("MFA verification did not return tokens")
    store_tokens(conn, result.tokens, email=pending.email)
    conn.commit()
    return {"status": "ok"}


def disconnect(conn: sqlite3.Connection, *, user_id: str) -> dict[str, Any]:
    """Remove stored tokens and any pending auth for this user."""
    clear_tokens(conn)
    conn.commit()
    clear_pending(user_id)
    return {"status": "ok"}
