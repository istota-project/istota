"""Shared inbound path — turn a normalized inbound message into a task.

`record_inbound` is the single inbound choke point every surface routes
through: resolve the canonical room token, lazily auto-register an unknown
room surface, echo-check, store the user message into the canonical `messages`
store, and create the task. `ingest_message` is a thin adapter over it for the
`IncomingMessage`-shaped callers (Talk, email); the web POST path calls
`record_inbound` directly (it never built an `IncomingMessage`).

Surface-specific filtering / short-circuiting (Talk's mention + command +
confirmation handling, email's untrusted-sender gate) stays inside each
transport's `poll()`; this just performs the resolve + store + create step.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .. import db
from ._types import IncomingMessage

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)

# Surfaces that participate in the unified room model (registry + canonical
# message store). Email/REPL are not rooms — they fall through to a plain
# create_task with no room registration or message storage (unchanged).
ROOM_SURFACES = frozenset({"talk", "web"})


def record_inbound(
    conn,
    config: "Config",
    *,
    surface: str,
    surface_ref: str,
    user_id: str,
    text: str,
    source_type: str | None = None,
    channel_name: str | None = None,
    is_group_chat: bool = False,
    attachments: list[str] | None = None,
    platform_message_id: int | None = None,
    reply_to_message_id: int | None = None,
    reply_to_content: str | None = None,
    delivery_token: str | None = None,
    output_target: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    priority: int = 5,
    external_id: str | None = None,
) -> tuple[str, int | None]:
    """Resolve → echo-check → store user message → create task.

    Returns `(room_token, task_id)`. `task_id` is `None` only when the message
    is dropped as a known echo (forward-looking; structurally impossible for the
    v1 Talk+web pair, where Talk self-filters bot posts by author and web is
    never polled inbound). `room_token` is the canonical conversation token the
    task was created under.
    """
    source_type = source_type or surface

    # 1. Resolve canonical room token. With no binding, the surface_ref *is* the
    #    canonical token (origin-surface case).
    room_token = db.resolve_room_token(conn, surface, surface_ref) or surface_ref
    room_surface = surface in ROOM_SURFACES and bool(room_token)

    if room_surface:
        # Lazy room registration on first sight (a Talk room the bot joined, a
        # web room created elsewhere). First writer wins on origin + name.
        existing = db.get_room(conn, room_token)
        if existing is None:
            db.register_room(
                conn, room_token, user_id, origin=surface, name=channel_name,
            )
        elif (
            surface == "talk"
            and existing.origin == "talk"
            and channel_name
            and channel_name != existing.name
        ):
            # Talk-side rename flows back to the registry on the next poll. Only
            # for Talk-origin rooms — a web-origin room's user-set name wins.
            db.rename_room(conn, room_token, channel_name)
        db.add_room_binding(conn, room_token, surface, surface_ref)

        # 2. Echo check (loop-prevention ledger). Dormant for v1 Talk+web.
        if external_id is not None and db.message_has_external_id(
            conn, room_token, surface, str(external_id),
        ):
            logger.info(
                "Dropping echo of a mirrored message on %s (room=%s ext=%s)",
                surface, room_token, external_id,
            )
            return room_token, None

    # 3. Create the task.
    task_id = db.create_task(
        conn,
        prompt=text,
        user_id=user_id,
        source_type=source_type,
        conversation_token=room_token,
        is_group_chat=is_group_chat,
        attachments=attachments or None,
        talk_message_id=platform_message_id,
        reply_to_talk_id=reply_to_message_id,
        reply_to_content=reply_to_content,
        output_target=output_target,
        talk_delivery_token=delivery_token,
        model=model,
        effort=effort,
        priority=priority,
    )

    # 4. Store the user message into the canonical store (room surfaces only),
    #    idempotently — Talk dedups a duplicate poll to the same task id, so we
    #    must not store a second user row for it.
    if room_surface and task_id is not None:
        already = conn.execute(
            "SELECT 1 FROM messages WHERE room_token = ? AND task_id = ? "
            "AND role = 'user' LIMIT 1",
            (room_token, task_id),
        ).fetchone()
        if not already:
            db.add_message(
                conn, room_token, role="user", body=text,
                origin_surface=surface, task_id=task_id,
            )

    return room_token, task_id


def ingest_message(conn, config: "Config", msg: IncomingMessage) -> int | None:
    """Create a task from a normalized inbound message via `record_inbound`.

    Returns the task id, or `None` if the message was dropped as a known echo
    (only reachable for a room surface — email never echo-drops). On a duplicate
    Talk message (same `platform_message_id` + `channel_token`) `db.create_task`
    returns the id of the already-existing task rather than inserting twice.
    """
    _room_token, task_id = record_inbound(
        conn,
        config,
        surface=msg.surface,
        surface_ref=msg.channel_token,
        user_id=msg.user_id,
        text=msg.text,
        source_type=msg.source_type,
        channel_name=msg.channel_name,
        is_group_chat=msg.is_group_chat,
        attachments=msg.attachments or None,
        platform_message_id=msg.platform_message_id,
        reply_to_message_id=msg.reply_to_message_id,
        reply_to_content=msg.reply_to_content,
        delivery_token=msg.delivery_token,
        output_target=msg.output_target,
        model=msg.model,
        effort=msg.effort,
        external_id=str(msg.platform_message_id)
        if msg.platform_message_id is not None
        else None,
    )
    return task_id
