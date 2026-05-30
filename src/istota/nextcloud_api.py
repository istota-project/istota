"""Nextcloud API integration for user metadata hydration."""

import logging

from .config import Config
from .nextcloud_client import ocs_get

logger = logging.getLogger("istota.nextcloud_api")


def fetch_user_info(config: Config, user_id: str) -> dict | None:
    """
    Fetch user info from Nextcloud OCS API.

    Returns dict with 'displayname' and 'email' keys, or None on error.
    """
    data = ocs_get(config, f"/cloud/users/{user_id}")
    if data is None:
        return None
    return {
        "displayname": data.get("displayname", ""),
        "email": data.get("email", ""),
    }


def fetch_user_timezone(config: Config, user_id: str) -> str | None:
    """
    Fetch user timezone from Nextcloud preferences API.

    Returns timezone string (e.g. "America/New_York"), or None on error.
    """
    data = ocs_get(
        config,
        f"/apps/provisioning_api/api/v1/config/users/{user_id}/core/timezone",
    )
    if data is None:
        return None
    tz = data.get("data", "")
    return tz if tz else None


def hydrate_user_configs(config: Config) -> None:
    """
    Merge Nextcloud API metadata into config.users in place.

    Override logic:
    - display_name: always uses Nextcloud's value (canonical source)
    - email: API email appended to email_addresses if not already present (case-insensitive)
    - timezone: follows Nextcloud only when the user's profile has
      ``timezone_follow_nextcloud`` set (the default). When the user pins their
      timezone in the Istota web UI (toggle off), Nextcloud is ignored and the
      stored value persists across restarts (ISSUE-102).

    Graceful degradation: API failures are silently logged and skipped.
    """
    if not config.nextcloud.url:
        logger.info("Skipping user hydration: Nextcloud not configured")
        return

    for user_id, user_config in config.users.items():
        # Fetch basic user info (display name, email)
        info = fetch_user_info(config, user_id)
        if info:
            # display_name: always use Nextcloud's value if available
            api_name = info.get("displayname", "")
            if api_name:
                user_config.display_name = api_name
                logger.debug("Set display_name for %s: %s", user_id, api_name)

            # email: append if not already present
            api_email = info.get("email", "")
            if api_email:
                existing_lower = [e.lower() for e in user_config.email_addresses]
                if api_email.lower() not in existing_lower:
                    user_config.email_addresses.append(api_email)
                    logger.info("Hydrated email for %s: %s", user_id, api_email)

        # Timezone: only follow Nextcloud when the user hasn't pinned it in the
        # Istota web UI (ISSUE-102). The toggle is mirrored onto the in-memory
        # UserConfig by _apply_user_profiles; default True keeps the legacy
        # "Nextcloud is canonical" behavior.
        if getattr(user_config, "timezone_follow_nextcloud", True):
            tz = fetch_user_timezone(config, user_id)
            if tz:
                user_config.timezone = tz
                logger.debug("Set timezone for %s: %s", user_id, tz)
                # Persist so the value survives the post-hydrate DB overlay
                # (_apply_user_profiles re-reads the row) and future restarts.
                _persist_followed_timezone(config, user_id, tz)
        else:
            logger.debug(
                "timezone for %s pinned in Istota UI; skipping Nextcloud sync",
                user_id,
            )


def _persist_followed_timezone(config: Config, user_id: str, tz: str) -> None:
    """Write a Nextcloud-followed timezone back to the user_profiles row.

    Best-effort. Skips when the DB or the row is absent (a brand-new user's row
    is seeded later by ``user_profiles.import_from_user_configs`` with the value
    just placed on the in-memory config). Only writes when the stored value
    actually differs, so steady-state restarts don't churn ``updated_at``.
    """
    db_path = getattr(config, "db_path", None)
    if not db_path:
        return
    # Don't materialise an empty DB file just to discover there's no row
    # (mirrors _apply_user_profiles' existence guard).
    from pathlib import Path as _Path  # noqa: PLC0415

    if not _Path(db_path).exists():
        return
    try:
        from . import user_profiles as _up  # noqa: PLC0415

        profile = _up.get_profile(db_path, user_id)
        if profile is None or profile.timezone == tz:
            return
        _up.update_profile(db_path, user_id, timezone=tz)
        logger.info("Synced timezone for %s from Nextcloud: %s", user_id, tz)
    except Exception as e:  # noqa: BLE001 - best-effort
        logger.debug("Could not persist followed timezone for %s: %s", user_id, e)


def sync_user_timezone(config: Config, user_id: str) -> str | None:
    """Fetch the live Nextcloud timezone for one user and persist it.

    Returns the Nextcloud timezone (also written back to the
    ``user_profiles`` row when it differs), or None when Nextcloud is
    unconfigured/unreachable or reports no timezone. Used by the web
    settings GET so a "follow Nextcloud" user sees the value actually in
    effect rather than the DB cache that only refreshes on scheduler
    restart. Best-effort — never raises.
    """
    if not config.nextcloud.url:
        return None
    try:
        tz = fetch_user_timezone(config, user_id)
    except Exception as e:  # noqa: BLE001 - best-effort
        logger.debug("Live Nextcloud timezone fetch failed for %s: %s", user_id, e)
        return None
    if tz:
        _persist_followed_timezone(config, user_id, tz)
    return tz
