"""Shared inbound path — turn a normalized ``IncomingMessage`` into a task.

This is the only inbound code shared across surfaces. Surface-specific
filtering / short-circuiting (Talk's mention + command + confirmation handling,
email's untrusted-sender gate) stays inside each transport's ``poll()``; this
just performs the ``create_task`` step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import db
from ._types import IncomingMessage

if TYPE_CHECKING:
    from ..config import Config


def ingest_message(conn, config: "Config", msg: IncomingMessage) -> int:
    """Create a task from a normalized inbound message.

    Returns the task id. On a duplicate Talk message (same
    ``platform_message_id`` + ``channel_token``) ``db.create_task`` returns the
    id of the already-existing task rather than inserting a second one.
    """
    return db.create_task(
        conn,
        prompt=msg.text,
        user_id=msg.user_id,
        source_type=msg.source_type,
        conversation_token=msg.channel_token,
        is_group_chat=msg.is_group_chat,
        attachments=msg.attachments or None,
        talk_message_id=msg.platform_message_id,
        reply_to_talk_id=msg.reply_to_message_id,
        reply_to_content=msg.reply_to_content,
        output_target=msg.output_target,
        talk_delivery_token=msg.delivery_token,
        model=msg.model,
        effort=msg.effort,
    )
