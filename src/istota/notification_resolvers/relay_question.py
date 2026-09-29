"""A relay question waiting on its recipient, in the recipient's own inbox.

The asker's side is ``message_relay``; this is the other end of the same row in
``message_relays``. Object-backed: the relay's own state closes it, so it opens
when the question reaches its destination (``waiting`` or ``uncertain``) and is
resolved by ``message_relays`` on every transition out of those, with the
resolver returning None as the backstop.

**The stored body never carries the question.** It is what a push delivers, and
a push follows the recipient's alert routing, which can reach a shared room or a
third-party push service. The question is shown only by the resolver, in the
authenticated bell, framed as untrusted content.

**Delivery is per destination**, which is why ``write`` returns the result
rather than sending: a room question pushes (``deliver_pending`` after the
producer's transaction), while a WhatsApp or SMS question is already a push to
the recipient's phone and a second one would alert twice.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from . import _common

if TYPE_CHECKING:
    import sqlite3

    from ..config import Config
    from ..notification_sources import NotificationRow, NotificationView
    from ..notification_store import RaiseResult

SOURCE = "relay_question"
OBJECT_TYPE = "message_relay"
SEVERITY = "info"
OPEN_STATES = ("waiting", "uncertain")

# Where the "answer" action points. The web chat's message deep link is
# `/chat?room=<token>&task=<id>`, and a query string is outside the
# notification URL allowlist (`notification_sources.SAFE_PATH_RE`, and its
# client copy `isSafeActionPath`), so neither a message anchor nor a room can be
# named here. The chat page is the most specific path the allowlist admits; the
# view's body names the room.
RELAY_QUESTION_HREF = "/chat"


def _destination(relay) -> dict:
    try:
        value = json.loads(relay["destination"] or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def title_for(relay) -> str:
    return f"{relay['asker_display'] or relay['asker_user_id']} asked you a question"


def instruction(relay) -> str:
    """How to answer, in fixed words. Never the question."""
    destination = _destination(relay)
    kind = destination.get("kind") or relay["surface"]
    if kind == "room":
        return f"Reply to the message in {destination.get('label') or 'your room'}."
    if kind == "sms":
        return f"Send !relay reply {relay['id']} <answer> by SMS."
    return "Quote it on WhatsApp."


def write(conn: "sqlite3.Connection", relay) -> "RaiseResult | None":
    """Open (or bump) the recipient's row, inside the producer's transaction."""
    from ..notification_store import write_notification

    room_token = _destination(relay).get("room_token") if relay["surface"] == "room" else None
    return write_notification(
        conn, relay["recipient_user_id"],
        **_common.row_kwargs(
            source=SOURCE,
            dedup_key=relay["id"],
            title=title_for(relay),
            body=instruction(relay),
            severity=SEVERITY,
            actionable=True,
            object_type=OBJECT_TYPE,
            object_id=relay["id"],
            room_token=room_token,
        ),
    )


def resolve_for_relay(conn: "sqlite3.Connection", user_id: str, relay_id: str, *, by: str) -> int:
    return _common.resolve_for(conn, user_id, SOURCE, OBJECT_TYPE, relay_id, by=by)


class RelayQuestionResolver:
    source = SOURCE
    auto_resolve_on_seen = False

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        from ..notification_sources import NotificationAction, NotificationView
        from ..untrusted import frame_untrusted

        relay = conn.execute(
            "SELECT * FROM message_relays WHERE id=? AND recipient_user_id=?",
            (row.object_id, row.user_id),
        ).fetchone()
        if relay is None or relay["state"] not in OPEN_STATES or relay["question"] is None:
            return None
        actions = ()
        if relay["surface"] == "room":
            actions = (NotificationAction(
                id="answer", label="Open chat", kind="primary", method="LINK",
                href=RELAY_QUESTION_HREF,
            ),)
        body = instruction(relay) + "\n\n" + frame_untrusted(relay["question"], "RELAY QUESTION")
        return NotificationView(title=title_for(relay), body=body, severity=row.severity,
                                actions=actions)


RESOLVER = RelayQuestionResolver()
