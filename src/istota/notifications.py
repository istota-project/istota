"""Centralized notification dispatcher for Talk, Email, and ntfy."""

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("istota.notifications")


def resolve_conversation_token(config: "Config", user_id: str) -> str | None:
    """Resolve Talk conversation token for a user.

    Priority: user invoicing_conversation_token > first briefing's token.
    """
    user_config = config.users.get(user_id)
    if not user_config:
        return None

    if user_config.invoicing_conversation_token:
        return user_config.invoicing_conversation_token

    for briefing in user_config.briefings:
        if briefing.conversation_token:
            return briefing.conversation_token

    return None


async def _send_talk(
    config: "Config", user_id: str, message: str,
    conversation_token: str | None = None,
) -> bool:
    """Send a notification via Talk. Returns True on success."""
    token = conversation_token or resolve_conversation_token(config, user_id)
    if not token:
        logger.warning("No conversation token for notification (user: %s)", user_id)
        return False

    if not config.nextcloud.url:
        logger.warning("Nextcloud not configured for notifications")
        return False

    try:
        from .talk import TalkClient
        client = TalkClient(config)
        await client.send_message(token, message)
        return True
    except Exception as e:
        logger.error("Failed to send Talk notification (user: %s): %s", user_id, e)
        return False


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


def _send_ntfy(
    config: "Config", user_id: str, message: str,
    title: str | None = None,
    priority: int | None = None,
    tags: str | None = None,
    ntfy_topic: str | None = None,
) -> bool:
    """Send a notification via ntfy. Returns True on success."""
    if not config.ntfy.enabled:
        logger.warning("ntfy not configured for notifications")
        return False

    # Resolve topic: explicit > per-user > global
    user_config = config.users.get(user_id)
    topic = ntfy_topic or (user_config.ntfy_topic if user_config else "") or config.ntfy.topic
    if not topic:
        logger.warning("No ntfy topic for notification (user: %s)", user_id)
        return False

    url = f"{config.ntfy.server_url.rstrip('/')}/{topic}"
    headers = {}
    if config.ntfy.token:
        headers["Authorization"] = f"Bearer {config.ntfy.token}"
    elif config.ntfy.username:
        import base64
        credentials = base64.b64encode(
            f"{config.ntfy.username}:{config.ntfy.password}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {credentials}"
    if title:
        headers["Title"] = title
    headers["Priority"] = str(priority if priority is not None else config.ntfy.priority)
    if tags:
        headers["Tags"] = tags

    try:
        response = httpx.post(url, content=message, headers=headers, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error("Failed to send ntfy notification (user: %s): %s", user_id, e)
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
    ntfy_topic: str | None = None,
) -> bool:
    """Send a notification via the specified surface.

    Args:
        surface: "talk", "email", "ntfy", "both" (talk+email), or "all" (talk+email+ntfy).
        conversation_token: Talk room override (falls back to user config resolution).
        ntfy_topic: ntfy topic override (falls back to user > global config).
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
        if _send_ntfy(config, user_id, message, title=title, priority=priority, tags=tags, ntfy_topic=ntfy_topic):
            sent = True

    if not sent:
        logger.warning(
            "Notification not delivered (user: %s, surface: %s)", user_id, surface,
        )

    return sent
