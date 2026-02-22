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
    - display_name: API used only if config value is empty or matches user_id
    - email: API email appended to email_addresses if not already present (case-insensitive)
    - timezone: API used only if config value is "UTC" (the default)

    Graceful degradation: API failures are silently logged and skipped.
    """
    if not config.nextcloud.url:
        logger.info("Skipping user hydration: Nextcloud not configured")
        return

    for user_id, user_config in config.users.items():
        # Fetch basic user info (display name, email)
        info = fetch_user_info(config, user_id)
        if info:
            # display_name: only override if empty or matches user_id
            api_name = info.get("displayname", "")
            if api_name and (not user_config.display_name or user_config.display_name == user_id):
                user_config.display_name = api_name
                logger.info("Hydrated display_name for %s: %s", user_id, api_name)

            # email: append if not already present
            api_email = info.get("email", "")
            if api_email:
                existing_lower = [e.lower() for e in user_config.email_addresses]
                if api_email.lower() not in existing_lower:
                    user_config.email_addresses.append(api_email)
                    logger.info("Hydrated email for %s: %s", user_id, api_email)

        # Fetch timezone from preferences
        if user_config.timezone == "UTC":
            tz = fetch_user_timezone(config, user_id)
            if tz:
                user_config.timezone = tz
                logger.info("Hydrated timezone for %s: %s", user_id, tz)
