"""Google Workspace skill — setup_env hook and CLI passthrough."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

logger = logging.getLogger("istota.skills.google_workspace")


def _is_expired(token_expiry: str) -> bool:
    """Check if the token expiry (ISO 8601 UTC) is in the past."""
    try:
        expiry = datetime.fromisoformat(token_expiry).replace(tzinfo=timezone.utc)
        # Refresh 60s before actual expiry to avoid race conditions
        return datetime.now(timezone.utc) >= expiry
    except (ValueError, TypeError):
        return True


def _refresh_token(refresh_token: str, client_id: str, client_secret: str) -> dict | None:
    """Refresh a Google OAuth access token.

    Returns dict with access_token, expires_in on success, None on failure.
    """
    import httpx

    try:
        resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.warning("Google token refresh failed: %s %s", resp.status_code, resp.text)
            return None
        return resp.json()
    except Exception as e:
        logger.warning("Google token refresh error: %s", e)
        return None


def setup_env(ctx) -> dict[str, str]:
    """Inject GOOGLE_WORKSPACE_CLI_TOKEN from DB, refreshing if expired."""
    config = ctx.config
    gw_config = getattr(config, "google_workspace", None)
    if not gw_config or not gw_config.enabled:
        return {}

    user_id = ctx.task.user_id

    from istota import db as _db

    with _db.get_db(config.db_path) as conn:
        token_data = _db.get_google_token(conn, user_id)

    if not token_data:
        return {}

    access_token = token_data["access_token"]

    if _is_expired(token_data["token_expiry"]):
        refreshed = _refresh_token(
            token_data["refresh_token"],
            gw_config.client_id,
            gw_config.client_secret,
        )
        if not refreshed or "access_token" not in refreshed:
            logger.warning("Could not refresh Google token for user %s", user_id)
            return {}

        access_token = refreshed["access_token"]
        expires_in = refreshed.get("expires_in", 3600)
        new_expiry = datetime.now(timezone.utc).replace(microsecond=0)
        from datetime import timedelta
        new_expiry = (new_expiry + timedelta(seconds=expires_in)).isoformat()

        # Google may return a new refresh token; use it if present
        new_refresh = refreshed.get("refresh_token", token_data["refresh_token"])

        with _db.get_db(config.db_path) as conn:
            _db.upsert_google_token(
                conn, user_id, access_token, new_refresh,
                new_expiry, token_data["scopes"],
            )
        logger.debug("Refreshed Google token for user %s", user_id)

    env = {"GOOGLE_WORKSPACE_CLI_TOKEN": access_token}

    # Point gws config/cache to the writable temp dir (sandbox HOME is read-only)
    env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(ctx.user_temp_dir / "gws_cache")

    return env


def main() -> None:
    """Pass through to gws binary with all arguments."""
    os.execvp("gws", ["gws"] + sys.argv[1:])
