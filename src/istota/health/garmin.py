"""Garmin Connect integration for the health module.

Two surfaces:

* **Auth lifecycle** — :func:`connect`, :func:`complete_mfa`, :func:`disconnect`,
  :func:`get_status`. Tokens live in the encrypted ``secrets`` table under
  ``service="garmin"`` (Fernet via ``ISTOTA_SECRET_KEY``). The token blob
  is never returned by any API endpoint — only ``connected`` /
  ``email`` / ``last_sync`` / ``error`` are exposed.
* **Adapter abstraction** — :class:`GarminAdapter` Protocol with a
  production implementation that wraps :mod:`garminconnect`. Tests inject
  a fake adapter via :func:`set_adapter_factory`.

The sync engine builds on these primitives.

**Single-worker requirement (C2).** The pending-auth cache (used to
hold the partially-authenticated SDK client between ``connect`` and
``complete_mfa``) is process-local. The production deploy runs
``istota-web`` as a single uvicorn process and the only path that calls
``connect`` / ``complete_mfa`` is the web service, so today this works.
Multi-worker fan-out (``uvicorn --workers N``) would break MFA: the
follow-up call may land on a different worker that doesn't hold the
pending state. If we ever need workers, move ``_PENDING`` into a DB
table (Garmin's ``garth`` serialises partial-auth state, so the SDK
side is portable).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from istota import secrets_store


logger = logging.getLogger(__name__)


# Encrypted-secrets service + key names used by ``secrets_store``.
SECRET_SERVICE = "garmin"
SECRET_KEY_BLOB = "oauth_state"        # JSON blob: {"sdk": {...}, "ours": {...}}
SECRET_KEY_EMAIL = "email"             # display-only; never sent back to SDK
SECRET_KEY_LAST_SYNC = "last_sync"     # ISO 8601 UTC
SECRET_KEY_ERROR = "error"             # short error code (e.g. "token_expired")

# How long a partially-authenticated Garmin client stays in the
# pending-auth cache between ``connect`` (MFA-required) and
# ``complete_mfa``. Process-local; see module docstring.
PENDING_AUTH_TTL_SEC = 300

# Cap entries so a runaway client (or admin probing other users)
# can't grow the cache unboundedly.
PENDING_MAX_ENTRIES = 64


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GarminAuthError(Exception):
    """Raised for any user-correctable auth failure (bad creds, MFA wrong,
    pending-auth expired, token refresh failed)."""


class GarminNotInstalled(RuntimeError):
    """Raised when the ``garminconnect`` extra isn't installed."""


class GarminRateLimited(Exception):
    """Raised when Garmin returns HTTP 429. Carries an optional
    ``retry_after`` (seconds) parsed from the response header so the
    sync engine can back off compliantly."""

    def __init__(self, retry_after: int | None = None) -> None:
        super().__init__(f"Garmin rate-limited (retry_after={retry_after})")
        self.retry_after = retry_after


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

    def get_user_profile(self) -> dict[str, Any] | None: ...

    # Daily-summary fetchers used by the sync engine. Each takes an ISO
    # date (YYYY-MM-DD) and returns a JSON-shaped dict, or None when the
    # API has no data for that day (device wasn't worn, etc.). Adapters
    # should swallow per-endpoint exceptions and let the engine decide
    # how to attribute the failure; auth errors must propagate.
    def get_sleep_data(self, date: str) -> dict[str, Any] | None: ...

    def get_stress_data(self, date: str) -> dict[str, Any] | None: ...

    def get_body_battery(self, date: str) -> list[dict[str, Any]] | dict[str, Any] | None: ...

    def get_steps_data(self, date: str) -> dict[str, Any] | None: ...

    def get_spo2_data(self, date: str) -> dict[str, Any] | None: ...

    def get_hrv_data(self, date: str) -> dict[str, Any] | None: ...

    def get_vo2_max(self, date: str) -> dict[str, Any] | float | None: ...

    def get_respiration_data(self, date: str) -> dict[str, Any] | None: ...

    def get_resting_heart_rate(self, date: str) -> dict[str, Any] | None: ...

    def get_user_summary(self, date: str) -> dict[str, Any] | None: ...

    def get_body_composition(self, date: str) -> dict[str, Any] | None: ...


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
    """Real adapter wrapping ``garminconnect.Garmin``.

    The ``garminconnect`` library exposes session persistence on the
    inner ``client`` object: ``client.dumps() -> str`` returns the full
    token store (DI access token + refresh token + JWT + cookies as a
    base64-wrapped JSON blob); ``Garmin().login(tokenstore=<str>)``
    rehydrates a fresh client from that blob. The MFA flow uses
    ``return_on_mfa=True`` so ``login()`` returns ``(mfa_status, _)``
    rather than prompting via stdin; ``resume_login(mfa_status, code)``
    completes the handshake.
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
        # MFA state captured at login() time; consumed by resume_mfa().
        self._mfa_state: Any = None

    def login(self, email: str, password: str) -> ConnectResult:
        try:
            client = self._sdk.Garmin(email=email, password=password)
            client.return_on_mfa = True
            mfa_status, _ = client.login()
        except Exception as exc:  # noqa: BLE001
            raise GarminAuthError(f"Garmin login failed: {exc}") from exc

        self._client = client
        if mfa_status:
            # MFA required. ``mfa_status`` is the opaque client_state
            # the SDK threads into resume_login.
            self._mfa_state = mfa_status
            return ConnectResult(
                status="mfa_required",
                prompt="Enter Garmin MFA code",
            )

        # Fully authenticated. Issue a trivial API call to surface any
        # latent "Not authenticated" failure mode immediately — the
        # rate-limited mobile-login fallback returns a 200 from the
        # SSO step but leaves the session unusable; catching it here
        # means the operator never sees a "connected" UI for an
        # unusable session.
        try:
            client.client.connectapi("/userprofile-service/socialProfile")
        except Exception as exc:  # noqa: BLE001
            raise GarminAuthError(
                f"Garmin login completed but session is unusable: {exc}",
            ) from exc

        return ConnectResult(
            status="ok",
            tokens=self.serialize_tokens(),
            email=email,
        )

    def resume_mfa(self, code: str) -> ConnectResult:
        if self._client is None or self._mfa_state is None:
            raise GarminAuthError("no pending Garmin auth — restart connect")
        try:
            self._client.resume_login(self._mfa_state, code)
        except Exception as exc:  # noqa: BLE001
            raise GarminAuthError(f"MFA verification failed: {exc}") from exc
        self._mfa_state = None
        return ConnectResult(status="ok", tokens=self.serialize_tokens())

    def serialize_tokens(self) -> dict[str, Any]:
        if self._client is None:
            raise GarminAuthError("not connected")
        return {"tokenstore": self._client.client.dumps()}

    def load_tokens(self, tokens: dict[str, Any]) -> None:
        tokenstore = tokens.get("tokenstore") if isinstance(tokens, dict) else None
        if not isinstance(tokenstore, str) or not tokenstore:
            raise GarminAuthError(
                "stored Garmin tokens are missing the 'tokenstore' string — "
                "the stored blob predates the current adapter and must be "
                "re-created via /garmin/connect",
            )
        client = self._sdk.Garmin()
        try:
            # Passing the tokenstore string forwards to the SDK's
            # rehydrate path. Anything > 512 chars is treated as the
            # blob itself; shorter values are interpreted as a path on
            # disk, so the length floor matters.
            client.login(tokenstore=tokenstore)
        except Exception as exc:  # noqa: BLE001
            raise GarminAuthError(
                f"Garmin token rehydrate failed: {exc}",
            ) from exc
        self._client = client

    def get_user_profile(self) -> dict[str, Any] | None:
        return self._call_optional("get_user_profile")

    # -- Daily summaries -----------------------------------------------------
    #
    # Each wrapper is one-line: pass the date through to the SDK method,
    # return whatever it gives back. Per-endpoint exceptions short-circuit
    # to None so a missing-data day doesn't kill the whole sync run; auth
    # errors are detected by the caller (sync engine) based on status
    # patterns rather than re-raised here.

    def get_sleep_data(self, date: str) -> dict[str, Any] | None:
        return self._call_optional("get_sleep_data", date)

    def get_stress_data(self, date: str) -> dict[str, Any] | None:
        return self._call_optional("get_stress_data", date)

    def get_body_battery(self, date: str):
        return self._call_optional("get_body_battery", date)

    def get_steps_data(self, date: str) -> dict[str, Any] | None:
        # Some SDK versions name this ``get_steps_data``, others ``get_daily_steps``.
        for name in ("get_steps_data", "get_daily_steps"):
            if hasattr(self._client, name):
                return self._call_optional(name, date)
        return None

    def get_spo2_data(self, date: str) -> dict[str, Any] | None:
        return self._call_optional("get_spo2_data", date)

    def get_hrv_data(self, date: str) -> dict[str, Any] | None:
        return self._call_optional("get_hrv_data", date)

    def get_vo2_max(self, date: str):
        for name in ("get_max_metrics", "get_vo2_max"):
            if hasattr(self._client, name):
                return self._call_optional(name, date)
        return None

    def get_respiration_data(self, date: str) -> dict[str, Any] | None:
        return self._call_optional("get_respiration_data", date)

    def get_resting_heart_rate(self, date: str) -> dict[str, Any] | None:
        for name in ("get_rhr_day", "get_resting_heart_rate"):
            if hasattr(self._client, name):
                return self._call_optional(name, date)
        return None

    def get_user_summary(self, date: str) -> dict[str, Any] | None:
        for name in ("get_user_summary", "get_stats"):
            if hasattr(self._client, name):
                return self._call_optional(name, date)
        return None

    def get_body_composition(self, date: str) -> dict[str, Any] | None:
        return self._call_optional("get_body_composition", date)

    def _call_optional(self, method: str, *args: Any) -> Any:
        """Call ``method`` on the SDK client with retry-once on transient
        failures and Retry-After honoring on 429.

        Auth errors propagate. Rate-limit 429s raise
        :class:`GarminRateLimited` so the sync engine can back off
        cleanly. Other persistent failures log a warning and return None.
        """
        if self._client is None:
            return None
        fn = getattr(self._client, method, None)
        if fn is None:
            return None
        last_exc: BaseException | None = None
        for attempt in (1, 2):
            try:
                return fn(*args)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if _looks_like_auth_error(exc):
                    raise GarminAuthError(
                        f"Garmin auth error during {method}: {exc}"
                    ) from exc
                rate_limited, retry_after = _looks_like_rate_limited(exc)
                if rate_limited:
                    raise GarminRateLimited(retry_after) from exc
                if attempt == 1 and _looks_like_transient_network_error(exc):
                    # Single retry with a short fixed backoff. The spec
                    # asked for "exponential" but with only two attempts
                    # the curve is moot.
                    time.sleep(2)
                    continue
                logger.warning("garmin %s failed: %s", method, exc)
                return None
        # Two attempts exhausted on transient — log and give up.
        logger.warning("garmin %s failed after retry: %s", method, last_exc)
        return None


def _looks_like_auth_error(exc: BaseException) -> bool:
    """Heuristic check for SDK auth-failure exceptions.

    We can't import garminconnect's exception classes here without
    forcing the optional install, so we match by class name first (the
    SDK ships ``GarminConnectAuthenticationError`` / ``LoginError``) and
    only fall back to the HTTP-status text for a *narrow* 401 signal.
    The old heuristic matched ``"403"`` / ``"forbidden"`` anywhere in
    the message; that triggered on WAF / geo-block / Cloudflare 403s
    that have no relation to token validity (H4) and on some
    rate-limit responses whose body mentions "403 Forbidden" as part of
    a longer string. 401 is the correct signal for "token bad"; 403 is
    almost always an authorisation or anti-bot decision that re-auth
    won't fix.
    """
    cls = type(exc).__name__.lower()
    if "auth" in cls or "login" in cls or "unauthorized" in cls:
        return True
    msg = str(exc).lower()
    # Require a 401 paired with an auth-shaped keyword to avoid false
    # positives like "401 retries exhausted".
    if "401" in msg and ("unauthor" in msg or "token" in msg or "expir" in msg):
        return True
    return False


def _looks_like_rate_limited(exc: BaseException) -> tuple[bool, int | None]:
    """Detect HTTP 429 responses.

    Returns ``(is_rate_limited, retry_after_seconds | None)``. The
    Retry-After is parsed out of the exception message when present.
    Used by the sync engine for compliant backoff behaviour.
    """
    msg = str(exc)
    if "429" not in msg and "rate" not in msg.lower() and "too many" not in msg.lower():
        return False, None
    retry_after: int | None = None
    # Match either "retry-after: 60" / "retry_after=60" / "Retry-After 60".
    import re
    m = re.search(r"retry[-_ ]after[:= ]\s*(\d+)", msg, re.IGNORECASE)
    if m:
        try:
            retry_after = int(m.group(1))
        except ValueError:
            pass
    return True, retry_after


def _looks_like_transient_network_error(exc: BaseException) -> bool:
    """Detect retry-once-eligible transient failures (5xx, connection
    reset, timeout). 4xx errors are intentionally excluded — those are
    client-side and won't change on retry."""
    cls = type(exc).__name__.lower()
    if "timeout" in cls or "connectionerror" in cls or "ssl" in cls:
        return True
    msg = str(exc).lower()
    for needle in ("500", "502", "503", "504", "timeout", "connection reset"):
        if needle in msg:
            return True
    return False


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
        # Hard cap (L2) — drop the oldest entries first so a runaway
        # caller can't blow the cache up unboundedly.
        if len(_PENDING) >= PENDING_MAX_ENTRIES:
            for old_uid in sorted(_PENDING, key=lambda u: _PENDING[u].created_at)[:max(1, len(_PENDING) - PENDING_MAX_ENTRIES + 1)]:
                _PENDING.pop(old_uid, None)
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
# Encrypted-storage helpers (Fernet via secrets_store)
# ---------------------------------------------------------------------------
#
# The token blob is JSON-serialised and round-tripped through one secret
# entry (service="garmin", key="oauth_state"). It carries the SDK's
# session state inside a ``sdk`` sub-key so we never collide with our
# own presentation fields — fixes the previous flat-merge that risked
# overwriting an SDK-internal ``email`` key (M3).
#
# Display-only fields (``email``, ``last_sync``, ``error``) live in
# their own secret keys so they can be written / cleared independently
# of the OAuth blob — fixes the read-modify-write race in
# ``update_last_sync`` (H2).


def _validate_db_path(db_path: Path | None) -> Path:
    if db_path is None:
        raise GarminAuthError(
            "framework db_path unavailable; cannot read/write Garmin tokens"
        )
    return Path(db_path)


def store_tokens(
    db_path: Path, user_id: str, tokens: dict[str, Any],
    *, email: str | None = None,
) -> None:
    """Encrypt and persist the Garmin OAuth blob.

    ``tokens`` is the adapter's serialised session — opaque to this
    module. ``email`` is stored separately for display.
    """
    db_path = _validate_db_path(db_path)
    blob = {"sdk": dict(tokens)}
    secrets_store.set_secret(
        db_path, user_id, SECRET_SERVICE, SECRET_KEY_BLOB,
        json.dumps(blob),
    )
    if email is not None:
        secrets_store.set_secret(
            db_path, user_id, SECRET_SERVICE, SECRET_KEY_EMAIL, email,
        )
    # Any prior error becomes stale on a successful (re)connect.
    secrets_store.delete_secret(db_path, user_id, SECRET_SERVICE, SECRET_KEY_ERROR)


def load_tokens(db_path: Path, user_id: str) -> dict[str, Any] | None:
    """Decrypt and return the raw SDK token state, or None if absent.

    Returns the inner ``sdk`` blob ready to hand to ``adapter.load_tokens``.
    Display-only keys are not mixed in.
    """
    db_path = _validate_db_path(db_path)
    raw = secrets_store.get_secret(db_path, user_id, SECRET_SERVICE, SECRET_KEY_BLOB)
    if not raw:
        return None
    try:
        wrapper = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(wrapper, dict):
        return None
    sdk = wrapper.get("sdk")
    return sdk if isinstance(sdk, dict) else None


def clear_tokens(db_path: Path, user_id: str) -> None:
    """Wipe the token blob, last_sync, and any error flag. Email is
    preserved as a courtesy so the UI can pre-fill the form."""
    db_path = _validate_db_path(db_path)
    for key in (SECRET_KEY_BLOB, SECRET_KEY_LAST_SYNC, SECRET_KEY_ERROR):
        secrets_store.delete_secret(db_path, user_id, SECRET_SERVICE, key)


def mark_token_error(db_path: Path, user_id: str, reason: str) -> None:
    """Record a non-retryable token error.

    Also wipes the OAuth blob — the previous behaviour left a stale blob
    on disk while reporting connected=true + error, which the UI rendered
    as "Connected" with a red banner forever (H6). Clearing the blob
    flips ``connected`` to false and forces the user to re-auth.
    """
    db_path = _validate_db_path(db_path)
    secrets_store.set_secret(
        db_path, user_id, SECRET_SERVICE, SECRET_KEY_ERROR, reason,
    )
    secrets_store.delete_secret(db_path, user_id, SECRET_SERVICE, SECRET_KEY_BLOB)


def update_last_sync(
    db_path: Path, user_id: str, *, when: datetime | None = None,
) -> None:
    db_path = _validate_db_path(db_path)
    iso = (when or datetime.now(timezone.utc)).isoformat()
    secrets_store.set_secret(
        db_path, user_id, SECRET_SERVICE, SECRET_KEY_LAST_SYNC, iso,
    )


# ---------------------------------------------------------------------------
# Public API used by the web routes and CLI
# ---------------------------------------------------------------------------


def get_status(db_path: Path, user_id: str) -> dict[str, Any]:
    """Return the public-facing connection status.

    The token blob is **never** included. Only the fields the settings
    UI and dashboard render: connected flag, email, last sync, error
    string. ``connected`` is False when the OAuth blob has been cleared
    (no tokens, expired, or revoked).
    """
    db_path = _validate_db_path(db_path)
    svc = secrets_store.get_service_secrets(db_path, user_id, SECRET_SERVICE)
    has_blob = bool(svc.get(SECRET_KEY_BLOB))
    return {
        "connected": has_blob,
        "email": svc.get(SECRET_KEY_EMAIL) or None,
        "last_sync": svc.get(SECRET_KEY_LAST_SYNC) or None,
        "error": svc.get(SECRET_KEY_ERROR) or None,
    }


def connect(
    db_path: Path, *, user_id: str, email: str, password: str,
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
    db_path = _validate_db_path(db_path)

    adapter = _build_adapter()
    try:
        result = adapter.login(email, password)
    except GarminAuthError:
        # try/finally would have been simpler than mirroring the cleanup
        # in every branch (L1) — drop any stale pending entry on a fresh
        # auth attempt that failed outright.
        clear_pending(user_id)
        raise

    if result.status == "mfa_required":
        _stash_pending(user_id, adapter, email)
        return {
            "status": "mfa_required",
            "prompt": result.prompt or "Enter Garmin MFA code",
        }

    if result.status != "ok" or not result.tokens:
        clear_pending(user_id)
        raise GarminAuthError("unexpected Garmin login result")

    try:
        store_tokens(db_path, user_id, result.tokens, email=result.email or email)
    finally:
        clear_pending(user_id)
    return {"status": "ok"}


def complete_mfa(
    db_path: Path, *, user_id: str, code: str,
) -> dict[str, Any]:
    """Complete the MFA step started by :func:`connect`."""
    db_path = _validate_db_path(db_path)
    pending = _take_pending(user_id)
    if pending is None:
        raise GarminAuthError(
            "no pending Garmin auth — restart from /garmin/connect"
        )
    result = pending.adapter.resume_mfa(code)
    if result.status != "ok" or not result.tokens:
        raise GarminAuthError("MFA verification did not return tokens")
    store_tokens(db_path, user_id, result.tokens, email=pending.email)
    return {"status": "ok"}


def disconnect(db_path: Path, *, user_id: str) -> dict[str, Any]:
    """Remove stored tokens and any pending auth for this user."""
    clear_tokens(_validate_db_path(db_path), user_id)
    clear_pending(user_id)
    return {"status": "ok"}
