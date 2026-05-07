"""Centralized notification dispatcher for Talk, Email, and ntfy."""

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("istota.notifications")


def resolve_conversation_token(config: "Config", user_id: str) -> str | None:
    """Resolve Talk conversation token for a user.

    Priority: alerts_channel > briefing token > auto-detected 1:1 DM.
    """
    user_config = config.users.get(user_id)
    if not user_config:
        return None

    if user_config.alerts_channel:
        return user_config.alerts_channel

    for briefing in user_config.briefings:
        if briefing.conversation_token:
            return briefing.conversation_token

    # Fall back to auto-detected 1:1 DM from talk poller
    try:
        from .talk_poller import get_dm_token
        dm_token = get_dm_token(user_id)
        if dm_token:
            return dm_token
    except ImportError:
        pass

    return None


async def _send_talk(
    config: "Config", user_id: str, message: str,
    conversation_token: str | None = None,
) -> int | None:
    """Send a notification via Talk. Returns message_id on success, None on failure."""
    token = conversation_token or resolve_conversation_token(config, user_id)
    if not token:
        logger.warning("No conversation token for notification (user: %s)", user_id)
        return None

    if not config.nextcloud.url:
        logger.warning("Nextcloud not configured for notifications")
        return None

    try:
        from .talk import TalkClient
        client = TalkClient(config)
        response = await client.send_message(token, message)
        return response.get("ocs", {}).get("data", {}).get("id")
    except Exception as e:
        logger.error("Failed to send Talk notification (user: %s): %s", user_id, e)
        return None


def send_talk_confirmation(
    config: "Config",
    user_id: str,
    message: str,
    conversation_token: str | None = None,
) -> int | None:
    """Send a confirmation prompt via Talk (sync). Returns message_id or None."""
    import asyncio
    return asyncio.run(_send_talk(config, user_id, message, conversation_token))


def _send_email(
    config: "Config", user_id: str, subject: str, body: str,
) -> bool:
    """Send a notification via email. Returns True on success."""
    user_config = config.users.get(user_id)
    if not user_config or not user_config.email_addresses:
        logger.warning("No email address for notification (user: %s)", user_id)
        return False

    if not config.email.enabled:
        logger.warning("Email not configured for notifications")
        return False

    try:
        from .email_poller import get_email_config
        from .skills.email import send_email
        email_config = get_email_config(config)
        send_email(
            to=user_config.email_addresses[0],
            subject=subject,
            body=body,
            config=email_config,
            from_addr=config.email.bot_email,
            content_type="plain",
        )
        return True
    except Exception as e:
        logger.error("Failed to send email notification (user: %s): %s", user_id, e)
        return False


_NTFY_DEFAULT_PRIORITY = 3
_NTFY_DEFAULT_SERVER = "https://ntfy.sh"


def _ntfy_settings(config: "Config", user_id: str) -> dict[str, str] | None:
    """Resolve the user's ntfy settings from the encrypted secrets table.

    Returns None if the user hasn't configured a topic, or if the secrets
    DB is unreachable (missing path, no secrets table). Notifications are
    best-effort: a misconfigured DB must not raise into a heartbeat or
    scheduler loop.

    Uses a single SELECT to fetch all ntfy keys at once — heartbeat polling
    fires this on every check, and 5× separate connections per send adds
    real WAL contention.
    """
    import sqlite3

    from . import secrets_store

    db_path = config.db_path
    if not db_path:
        return None

    try:
        rows = secrets_store.get_service_secrets(db_path, user_id, "ntfy")
    except (sqlite3.Error, OSError, TypeError) as e:
        logger.warning("ntfy settings lookup failed for user %s: %s", user_id, e)
        return None

    topic = rows.get("topic")
    if not topic:
        return None
    return {
        "topic": topic,
        "server_url": rows.get("server_url") or _NTFY_DEFAULT_SERVER,
        "token": rows.get("token", ""),
        "username": rows.get("username", ""),
        "password": rows.get("password", ""),
    }


def _send_ntfy(
    config: "Config", user_id: str, message: str,
    title: str | None = None,
    priority: int | None = None,
    tags: str | None = None,
) -> bool:
    """Send a notification via the user's own ntfy server. Returns True on success."""
    settings = _ntfy_settings(config, user_id)
    if settings is None:
        logger.warning("ntfy not configured for user %s", user_id)
        return False

    url = f"{settings['server_url'].rstrip('/')}/{settings['topic']}"
    headers = {}
    if settings["token"]:
        headers["Authorization"] = f"Bearer {settings['token']}"
    elif settings["username"]:
        import base64
        credentials = base64.b64encode(
            f"{settings['username']}:{settings['password']}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {credentials}"
    if title:
        headers["Title"] = title.replace("\r", "").replace("\n", " ")
    headers["Priority"] = str(priority if priority is not None else _NTFY_DEFAULT_PRIORITY)
    if tags:
        headers["Tags"] = tags.replace("\r", "").replace("\n", " ")

    try:
        response = httpx.post(url, content=message, headers=headers, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error("Failed to send ntfy notification (user: %s): %s", user_id, e)
        return False


def is_channel_configured(
    config: "Config",
    user_id: str,
    surface: str,
    *,
    conversation_token: str | None = None,
) -> bool:
    """Probe: does the user have this notification channel set up?

    Distinguishes "user hasn't configured this channel" from "tried to send
    and the network/server failed". Heartbeat uses this to avoid bumping
    ``consecutive_errors`` when a check is misconfigured (e.g. ``channel =
    "ntfy"`` but the user never set their ntfy topic).

    Compound surfaces (``both``, ``all``) are configured if **any** of
    their leaf channels are.
    """
    user_config = config.users.get(user_id)

    def _talk_ok() -> bool:
        if not config.nextcloud.url:
            return False
        if conversation_token:
            return True
        return resolve_conversation_token(config, user_id) is not None

    def _email_ok() -> bool:
        return bool(
            config.email.enabled
            and user_config
            and user_config.email_addresses
        )

    def _ntfy_ok() -> bool:
        return _ntfy_settings(config, user_id) is not None

    if surface == "talk":
        return _talk_ok()
    if surface == "email":
        return _email_ok()
    if surface == "ntfy":
        return _ntfy_ok()
    if surface == "both":
        return _talk_ok() or _email_ok()
    if surface == "all":
        return _talk_ok() or _email_ok() or _ntfy_ok()
    return False


def send_notification(
    config: "Config",
    user_id: str,
    message: str,
    *,
    surface: str = "talk",
    conversation_token: str | None = None,
    title: str | None = None,
    priority: int | None = None,
    tags: str | None = None,
) -> bool:
    """Send a notification via the specified surface.

    Args:
        surface: "talk", "email", "ntfy", "both" (talk+email), or "all" (talk+email+ntfy).
        conversation_token: Talk room override (falls back to user config resolution).
    """
    import asyncio

    sent = False

    if surface in ("talk", "both", "all"):
        if asyncio.run(_send_talk(config, user_id, message, conversation_token)):
            sent = True

    if surface in ("email", "both", "all"):
        if _send_email(config, user_id, title or "Notification", message):
            sent = True

    if surface in ("ntfy", "all"):
        if _send_ntfy(config, user_id, message, title=title, priority=priority, tags=tags):
            sent = True

    if not sent:
        logger.warning(
            "Notification not delivered (user: %s, surface: %s)", user_id, surface,
        )

    return sent
