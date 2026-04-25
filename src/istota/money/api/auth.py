"""API key authentication with per-user scoping and rate limiting.

Supports two key types:
- Master key: full access, can impersonate any user via X-User header
- Derived key: HMAC-SHA256(master_key, username) — scoped to a specific user

Callers should prefer derived keys. The istota money skill derives keys
automatically from the master key + MONEY_USER.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections import defaultdict

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Simple in-memory rate limiter for failed auth attempts.
_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_MAX_FAILURES = 10
_failure_log: dict[str, list[float]] = defaultdict(list)


def derive_user_key(master_key: str, username: str) -> str:
    """Derive a per-user API key from the master key.

    Returns hex-encoded HMAC-SHA256(master_key, username).
    """
    return hmac.new(
        master_key.encode(), username.encode(), hashlib.sha256,
    ).hexdigest()


def _check_rate_limit(client_ip: str) -> None:
    """Raise 429 if too many recent auth failures from this IP."""
    now = time.monotonic()
    timestamps = _failure_log[client_ip]
    cutoff = now - _RATE_LIMIT_WINDOW
    _failure_log[client_ip] = [t for t in timestamps if t > cutoff]
    if len(_failure_log[client_ip]) >= _RATE_LIMIT_MAX_FAILURES:
        raise HTTPException(status_code=429, detail="Too many failed attempts")


def _record_failure(client_ip: str) -> None:
    _failure_log[client_ip].append(time.monotonic())


async def verify_api_key(
    request: Request,
    key: str | None = Security(api_key_header),
):
    expected = request.app.state.ctx.api_key
    if not expected:
        logger.warning("No API key configured — all endpoints are unauthenticated")
        return

    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    if not key:
        _record_failure(client_ip)
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Check master key first
    if hmac.compare_digest(key.encode(), expected.encode()):
        request.state.auth_user = None  # admin — X-User honored in deps
        return

    # Check derived per-user key: HMAC(master, username) where username = X-User
    x_user = request.headers.get("X-User")
    if x_user:
        derived = derive_user_key(expected, x_user)
        if hmac.compare_digest(key.encode(), derived.encode()):
            request.state.auth_user = x_user  # scoped to this user
            return

    _record_failure(client_ip)
    raise HTTPException(status_code=401, detail="Invalid API key")
