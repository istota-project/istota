"""Web chat delivery surface.

Web chat is a ``stream``-class surface for *interactive* task output: a
``source_type="web"`` task streams its result over SSE from the ``task_events``
log, exactly like the REPL, so ``resolve_delivery_plan`` resolves ``web`` to a
stream destination and nothing is pushed for those tasks.

Its ``deliver()``, unlike the REPL's no-op, is a real write — used by the
*routing* paths (alerts, the verbose execution log, any notification routed to
the ``web`` surface). A web room persists its messages, so an unsolicited
bot message is appended to ``web_chat_messages`` and rendered as a standalone
system message in the room transcript (and picked up by an open client the next
time it reads room history). This is what makes ``web`` a selectable destination
in the logs/alerts routing UI (``user_routable=True``).

``resolve_target`` returns the user's default room token, so a bare ``web``
route (no explicit ``:token``) lands in the user's ``general`` room.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .._types import DeliveryOptions, IncomingMessage, TransportCapabilities

if TYPE_CHECKING:
    from ... import db
    from ...config import Config

logger = logging.getLogger("istota.transport.web")

__all__ = ["WebTransport", "default_web_room_token"]


def default_web_room_token(config: "Config", user_id: str) -> str | None:
    """The token of the user's default (``general``) web chat room, provisioning
    it if the user has none yet. Returns None when the DB is unreachable —
    delivery is best-effort and must not raise into a heartbeat / scheduler loop.
    """
    from ... import db

    if not config.db_path:
        return None
    try:
        with db.get_db(config.db_path) as conn:
            return db.ensure_default_web_chat_room(conn, user_id).token
    except Exception as e:
        logger.warning("default web room lookup failed for user %s: %s", user_id, e)
        return None


def _append_blocking(
    config: "Config", token: str, text: str, title: str | None,
) -> int | None:
    """Insert one ``web_chat_messages`` row for ``token``, attributed to the
    room's owner. Returns the new id, or None when the room can't be resolved.

    Room tokens are minted at room creation, so a token with no room row is a
    *deleted* room — never a pending one. We drop (with a WARNING) rather than
    insert an orphan row that can never render: the room is gone, and under
    ``origin+thread`` the email mirror leg still delivers; under ``origin``-only
    the reply is a logged drop, the configured trade-off."""
    from ... import db

    if not config.db_path:
        return None
    try:
        with db.get_db(config.db_path) as conn:
            room = db.get_web_chat_room_by_token(conn, token)
            if room is None:
                logger.warning(
                    "Dropping web delivery: no room for token %r (deleted?)", token,
                )
                return None
            return db.add_web_chat_message(
                conn, room.user_id, token, text, title=title,
            )
    except Exception as e:
        logger.warning("web delivery failed for room %r: %s", token, e)
        return None


class WebTransport:
    """Append-a-room-message adapter for the web chat surface.

    Stream-class for interactive task output (the SSE tails ``task_events``), but
    ``deliver()`` is a real persistence write used by the notification / log
    routing paths. ``supports_edit=False`` so the log path delivers one final
    summary rather than a per-tool edit stream.
    """

    name = "web"
    capabilities = TransportCapabilities(
        supports_edit=False,
        supports_threading=False,
        supports_progress_ack=False,
        supports_typing=False,
        max_message_length=None,
        surface_class="stream",
        user_routable=True,
    )

    def __init__(self, config: "Config"):
        self._config = config

    async def poll(self) -> list[IncomingMessage]:
        # Inbound is the /chat HTTP endpoint (web_app.py), not a poller.
        return []

    async def deliver(
        self, target: str, text: str, *,
        task: "db.Task | None" = None,
        reply_to: int | None = None,
        reference_id: str | None = None,
        threaded: bool = False,
        options: DeliveryOptions | None = None,
    ) -> int | None:
        """Append ``text`` as a system message to the room identified by
        ``target`` (a room channel token). Returns the new message id, or None
        when the room can't be resolved. The DB write runs off the persistent
        asyncio loop the transport is awaited on."""
        token = target or (task.conversation_token if task else None)
        if not token:
            return None
        title = options.title if options else None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _append_blocking, self._config, token, text, title,
        )

    async def edit(self, target: str, message_id: int, text: str) -> None:
        # Non-edit surface: the log path delivers a single final summary.
        return None

    async def download_attachment(self, remote_ref: str, local_path: str) -> None:
        return None

    def resolve_target(self, task: "db.Task") -> str | None:
        """The user's default web room token (a bare ``web`` route lands here)."""
        return default_web_room_token(self._config, task.user_id)
