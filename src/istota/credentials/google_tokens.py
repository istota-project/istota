"""Google OAuth token access shared by skills and the web UI."""
import logging
from datetime import datetime, timezone

from istota import db

logger = logging.getLogger(__name__)


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


def access_token_for(conn, config, user_id: str) -> str | None:
    gw_config = getattr(config, "google_workspace", None)
    if not gw_config or not gw_config.enabled:
        return None
    token_data = db.get_google_token(conn, user_id)
    if not token_data:
        return None

    access_token = token_data["access_token"]

    if _is_expired(token_data["token_expiry"]):
        refreshed = _refresh_token(
            token_data["refresh_token"],
            gw_config.client_id,
            gw_config.client_secret,
        )
        if not refreshed or "access_token" not in refreshed:
            logger.warning("Could not refresh Google token for user %s", user_id)
            return None

        access_token = refreshed["access_token"]
        expires_in = refreshed.get("expires_in", 3600)
        new_expiry = datetime.now(timezone.utc).replace(microsecond=0)
        from datetime import timedelta
        new_expiry = (new_expiry + timedelta(seconds=expires_in)).isoformat()

        # Google may return a new refresh token; use it if present
        new_refresh = refreshed.get("refresh_token", token_data["refresh_token"])

        db.upsert_google_token(
            conn, user_id, access_token, new_refresh,
            new_expiry, token_data["scopes"],
        )
        logger.debug("Refreshed Google token for user %s", user_id)

    return access_token


def scope_selection(config, user_id: str) -> dict[str, str]:
    """The user's stored per-service selection ({} when they never chose).

    Fails soft: a DB that is unreachable here must not block a connect, and
    the empty selection resolves to the operator's whole ceiling, which is
    what the user got before the picker existed.
    """
    try:
        from istota import user_profiles
        from istota.credentials import google_scopes

        profile = user_profiles.get_profile(config.db_path, user_id)
        if profile is None:
            return {}
        return google_scopes.normalize_selection(profile.google_scopes)
    except Exception:
        logger.warning(
            "google_workspace: could not read the scope selection for %s; "
            "falling back to the instance ceiling", user_id, exc_info=True,
        )
        return {}


def requested_scopes(config, user_id: str) -> list[str]:
    """What a connect for this user asks Google for, clamped to the instance ceiling.

    The web connect route and the chat `connect-link` verb both read this, so
    the services a link names are the ones its consent screen asks for.
    """
    from istota.credentials import google_scopes

    gw_config = getattr(config, "google_workspace", None)
    if not gw_config:
        return []
    return google_scopes.resolve_selection(scope_selection(config, user_id), gw_config.scopes)
