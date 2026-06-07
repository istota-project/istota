"""Nextcloud Talk surface.

This package is the home for everything Talk-specific that sits above the
low-level HTTP/OCS client (``istota.talk.TalkClient``):

- ``TalkTransport`` (here) — the bidirectional seam: outbound ``deliver`` /
  ``edit`` / ``resolve_target`` (the one place outside the CLI that constructs
  ``TalkClient``) plus the ``poll`` entry point.
- ``inbound`` — the inbound body (``poll_talk_conversations`` + the
  Talk-specific filtering / `!command` dispatch / confirmation handling and the
  module-global conversation/participant/DM caches).

``deliver`` replicates the previous ``post_result_to_talk`` body (split +
sequential post + group-chat reply-threading / @mention); ``edit`` replicates
``edit_talk_message``. The scheduler's ``post_result_to_talk`` /
``edit_talk_message`` are thin shims over these methods.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...async_runtime import get_talk_client
from ...talk import split_message
from .._types import IncomingMessage, TransportCapabilities
from .inbound import get_dm_token, poll_talk_conversations

if TYPE_CHECKING:
    from ... import db
    from ...config import Config
    from .._types import DeliveryOptions

logger = logging.getLogger("istota.transport.talk")

__all__ = ["TalkTransport", "poll_talk_conversations", "get_dm_token"]


class TalkTransport:
    """Bidirectional adapter over Nextcloud Talk."""

    name = "talk"
    capabilities = TransportCapabilities(
        supports_edit=True,
        supports_threading=True,
        supports_progress_ack=True,
        supports_typing=True,
        max_message_length=32000,
    )

    def __init__(self, config: "Config"):
        self._config = config

    async def poll(self) -> list[IncomingMessage]:
        """Poll Talk and create tasks.

        Like email, Talk self-creates its tasks inside ``poll_talk_conversations``
        rather than handing un-ingested ``IncomingMessage``s back to a driver:
        the create must share the ``db.get_db`` transaction with the poll-state
        advance / command dispatch / confirmation handling, or a create failure
        would advance the poll cursor past messages whose tasks were never made
        (silent message loss). So this returns an empty ``IncomingMessage`` list
        — there is nothing left for a driver to ingest. The inbound body owns
        the module-global conversation/participant/DM caches and the
        Talk-specific filtering.
        """
        await poll_talk_conversations(self._config)
        return []

    async def deliver(
        self, target: str, text: str, *,
        task: "db.Task | None" = None,
        reply_to: int | None = None,
        reference_id: str | None = None,
        threaded: bool = False,
        options: "DeliveryOptions | None" = None,
    ) -> int | None:
        """Send a message to a Talk room. Splits long messages and posts the
        parts sequentially; in group chats with ``threaded=True`` the first part
        replies to ``task.talk_message_id`` and @mentions the user. Returns the
        last posted message id, or None on failure / no target."""
        token = target or (task.conversation_token if task is not None else None)
        if not self._config.nextcloud.url or not token:
            return None

        try:
            client = get_talk_client(self._config)
            parts = split_message(text)
            msg_id = None
            for i, part in enumerate(parts):
                # In group chats, reply to the original message and @mention the
                # user for the first part only so they get a notification. Only
                # applied for final results (threaded=True), not intermediate
                # progress updates which would be too noisy.
                part_reply_to = None
                if threaded and i == 0 and task is not None and task.is_group_chat:
                    part_reply_to = task.talk_message_id
                    part = f"@{task.user_id} {part}"
                elif reply_to is not None and i == 0:
                    part_reply_to = reply_to
                response = await client.send_message(
                    token, part, reply_to=part_reply_to, reference_id=reference_id,
                )
                msg_id = response.get("ocs", {}).get("data", {}).get("id")
            return msg_id
        except Exception as e:
            task_id = task.id if task is not None else "?"
            logger.error(
                "Failed to post to Talk (task %s): %s: %r",
                task_id, type(e).__name__, e,
            )
            return None

    async def edit(self, target: str, message_id: int, text: str) -> None:
        """Edit a previously posted Talk message in place. Raises on API error
        (the scheduler ``edit_talk_message`` shim catches and returns False)."""
        if not self._config.nextcloud.url or not target:
            return None
        client = get_talk_client(self._config)
        await client.edit_message(target, message_id, text)
        return None

    async def resolve_channel_name(self, token: str) -> str:
        """Resolve a Talk room token to its display name, falling back to the
        token on any OCS error / missing config. Houses the last log-path OCS
        read behind the transport seam (was a direct ``get_conversation_info``
        in ``scheduler._resolve_channel_name``)."""
        if not self._config.nextcloud.url or not token:
            return token
        try:
            client = get_talk_client(self._config)
            info = await client.get_conversation_info(token)
            return info.get("displayName") or token
        except Exception:
            logger.debug(
                "Failed to resolve Talk channel name for %s", token, exc_info=True,
            )
            return token

    async def download_attachment(self, remote_ref: str, local_path: str) -> None:
        client = get_talk_client(self._config)
        await client.download_attachment(remote_ref, local_path)

    def resolve_target(self, task: "db.Task") -> str | None:
        from ...scheduler import _talk_target_for_delivery
        return _talk_target_for_delivery(self._config, task)
