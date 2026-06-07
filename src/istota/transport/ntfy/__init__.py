"""ntfy push surface.

ntfy is a one-way push channel (bot → device). Folding it into the transport
abstraction makes it a first-class delivery target so there is no parallel
delivery path left in the codebase — the httpx POST lives here and nowhere
else. ``notifications._send_ntfy`` is now a thin sync shim that builds
``DeliveryOptions`` and calls this transport via ``run_coro``.

ntfy is a per-user connected service: there is no global ``[ntfy]`` block. Each
user supplies their own server URL, topic, and (optional) auth via the
encrypted ``secrets`` table; ``ntfy_settings`` resolves them. A user with no
topic configured gets ``resolve_target() -> None`` and no delivery.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import sqlite3
from typing import TYPE_CHECKING

import httpx

from .._types import DeliveryOptions, IncomingMessage, TransportCapabilities

if TYPE_CHECKING:
    from ... import db
    from ...config import Config

logger = logging.getLogger("istota.transport.ntfy")

_NTFY_DEFAULT_PRIORITY = 3
_NTFY_DEFAULT_SERVER = "https://ntfy.sh"

__all__ = ["NtfyTransport", "ntfy_settings", "send_ntfy_async"]


def ntfy_settings(config: "Config", user_id: str) -> dict[str, str] | None:
    """Resolve the user's ntfy settings from the encrypted secrets table.

    Returns None if the user hasn't configured a topic, or if the secrets DB is
    unreachable (missing path, no secrets table). Notifications are best-effort:
    a misconfigured DB must not raise into a heartbeat or scheduler loop. One
    SELECT fetches all ntfy keys at once — heartbeat polling fires this on every
    check, and 5× separate connections per send adds real WAL contention.
    """
    from ... import secrets_store

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


def _post_ntfy_blocking(
    settings: dict[str, str], message: str, options: DeliveryOptions,
) -> bool:
    """The blocking httpx POST. Runs in a thread executor so it never blocks the
    persistent asyncio loop the transport is awaited on."""
    url = f"{settings['server_url'].rstrip('/')}/{settings['topic']}"
    headers: dict[str, str] = {}
    if settings["token"]:
        headers["Authorization"] = f"Bearer {settings['token']}"
    elif settings["username"]:
        credentials = base64.b64encode(
            f"{settings['username']}:{settings['password']}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {credentials}"
    if options.title:
        headers["Title"] = options.title.replace("\r", "").replace("\n", " ")
    headers["Priority"] = str(
        options.priority if options.priority is not None else _NTFY_DEFAULT_PRIORITY
    )
    if options.tags:
        headers["Tags"] = options.tags.replace("\r", "").replace("\n", " ")

    try:
        response = httpx.post(url, content=message, headers=headers, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error("Failed to send ntfy notification: %s", e)
        return False


async def send_ntfy_async(
    config: "Config", user_id: str, message: str,
    options: DeliveryOptions | None = None,
) -> bool:
    """Resolve the user's ntfy settings and POST (off-loop). The single ntfy
    delivery code path; both ``NtfyTransport.deliver`` and the
    ``notifications._send_ntfy`` shim funnel through here."""
    settings = ntfy_settings(config, user_id)
    if settings is None:
        logger.warning("ntfy not configured for user %s", user_id)
        return False
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _post_ntfy_blocking, settings, message, options or DeliveryOptions(),
    )


class NtfyTransport:
    """One-way push adapter over a user's ntfy server."""

    name = "ntfy"
    capabilities = TransportCapabilities(
        supports_edit=False,
        supports_threading=False,
        supports_progress_ack=False,
        supports_typing=False,
        max_message_length=None,
        surface_class="push",
    )

    def __init__(self, config: "Config"):
        self._config = config

    async def poll(self) -> list[IncomingMessage]:
        return []

    async def deliver(
        self, target: str, text: str, *,
        task: "db.Task | None" = None,
        reply_to: int | None = None,
        reference_id: str | None = None,
        threaded: bool = False,
        options: DeliveryOptions | None = None,
    ) -> int | None:
        """Push ``text`` to the task user's ntfy topic. The user id comes from
        the task (the topic + auth are resolved from that user's secrets);
        ``target`` is advisory. Returns None — ntfy has no message-id concept."""
        if task is None:
            return None
        await send_ntfy_async(self._config, task.user_id, text, options)
        return None

    async def edit(self, target: str, message_id: int, text: str) -> None:
        return None

    async def download_attachment(self, remote_ref: str, local_path: str) -> None:
        return None

    def resolve_target(self, task: "db.Task") -> str | None:
        """The user's ntfy topic, or None when unconfigured."""
        settings = ntfy_settings(self._config, task.user_id)
        return settings["topic"] if settings else None
