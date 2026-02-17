"""Nextcloud API integration for user metadata hydration."""

import logging

import httpx

from .config import Config

logger = logging.getLogger("istota.nextcloud_api")


def fetch_user_info(config: Config, user_id: str) -> dict | None:
    """
    Fetch user info from Nextcloud OCS API.

    Returns dict with 'displayname' and 'email' keys, or None on error.
    """
    if not config.nextcloud.url or not config.nextcloud.username:
        return None

    base_url = config.nextcloud.url.rstrip("/")
    url = f"{base_url}/ocs/v2.php/cloud/users/{user_id}"
    auth = (config.nextcloud.username, config.nextcloud.app_password)
    headers = {"OCS-APIRequest": "true", "Accept": "application/json"}

    try:
        resp = httpx.get(url, auth=auth, headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        user_data = data.get("ocs", {}).get("data", {})
        return {
            "displayname": user_data.get("displayname", ""),
            "email": user_data.get("email", ""),
        }
    except Exception as e:
        logger.debug("Failed to fetch user info for %s: %s", user_id, e)
        return None


def fetch_user_timezone(config: Config, user_id: str) -> str | None:
    """
    Fetch user timezone from Nextcloud preferences API.

    Returns timezone string (e.g. "America/New_York"), or None on error.
    """
    if not config.nextcloud.url or not config.nextcloud.username:
        return None

    base_url = config.nextcloud.url.rstrip("/")
    url = (
        f"{base_url}/ocs/v2.php/apps/provisioning_api/api/v1"
        f"/config/users/{user_id}/core/timezone"
    )
    auth = (config.nextcloud.username, config.nextcloud.app_password)
    headers = {"OCS-APIRequest": "true", "Accept": "application/json"}

    try:
        resp = httpx.get(url, auth=auth, headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        tz = data.get("ocs", {}).get("data", {}).get("data", "")
        return tz if tz else None
    except Exception as e:
        logger.debug("Failed to fetch timezone for %s: %s", user_id, e)
        return None


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
