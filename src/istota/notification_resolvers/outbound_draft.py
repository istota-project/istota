"""An outbound email held by the approval gate — the inbox's second source.

Two producers write this row, and they are deliberately *not* the same as
``outbound_drafts.hold``'s two callers being wrapped: the raise happens at the
call sites, because one of those callers is the email skill CLI, a short-lived
host-side process the skill proxy spawns. Raising inside ``hold`` would put
``send_notification``'s Talk and ntfy fan-out in that subprocess. The daemon
call site writes and delivers; the skill CLI writes only.

Closing is simpler, because it needs no delivery: ``outbound_drafts.discard``
and ``release``'s finalize step close the row on the connection they already
hold, which covers the web endpoints, ``!drafts`` and any future caller at once.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from ..config import Config
    from ..notification_sources import NotificationRow, NotificationView
    from ..notification_store import RaiseResult

logger = logging.getLogger(__name__)

SOURCE = "outbound_draft"
OBJECT_TYPE = "draft"

SEVERITY = "warning"

STATUS_PENDING = "pending"
STATUS_SENDING = "sending"

_SENDING_NOTE = "This message is being sent right now and can no longer be changed."

_UNREADABLE_NOTE = (
    "This draft's stored recipients could not be read, so it cannot be sent — "
    "it can still be discarded from the drafts list."
)

# How much of a subject survives into the notification body.
_SUBJECT_CHARS = 120


def dedup_key(draft_id: int | str) -> str:
    """``draft:{id}``, verbatim — see the note on the confirmation source."""
    return f"{OBJECT_TYPE}:{draft_id}"


def title_for(to_addr: str) -> str:
    """The one-line label, from the recipient alone.

    One spelling, used by both producers and by the resolver, so the stored
    fallback text and the rendered title cannot drift apart.
    """
    from ..confirmations import flatten

    who = flatten(to_addr or "") or "an unknown address"
    return f"Email reply to {who} is waiting for your approval"


def body_for(subject: str | None) -> str:
    from ..confirmations import flatten

    flat = flatten(subject or "")[:_SUBJECT_CHARS]
    if not flat:
        return "Nothing was sent. Approve it to send, or discard it."
    return f"Subject: {flat}. Nothing was sent."


def delivery_body_for(subject: str | None, draft_id: int) -> str:
    """The stored body, which is also what the push says.

    Carries the `!drafts` verbs because a push lands on a surface with no
    buttons — Talk, ntfy, an alerts room — and the notice it replaces
    (ISSUE-246) named them. The panel never shows this: the resolver rebuilds
    title and body from the live draft, and the stored text is the fallback for
    a row whose object can no longer be read.
    """
    return (
        f"{body_for(subject)} Review it with `!drafts`, then "
        f"`!drafts send {draft_id}` or `!drafts discard {draft_id}`."
    )


def write(
    conn: "sqlite3.Connection",
    user_id: str,
    *,
    draft_id: int,
    title: str,
    body: str = "",
    room_token: str | None = None,
) -> "RaiseResult | None":
    """Write the row on the caller's connection, inside its transaction."""
    from ..notification_store import write_notification

    return write_notification(
        conn, user_id,
        source=SOURCE,
        dedup_key=dedup_key(draft_id),
        title=title,
        body=body,
        severity=SEVERITY,
        actionable=True,
        object_type=OBJECT_TYPE,
        object_id=str(draft_id),
        room_token=room_token,
        purpose="alert",
    )


def resolve_for_draft(
    conn: "sqlite3.Connection", user_id: str, draft_id: int, *, by: str,
) -> int:
    """Close the row for a draft that has just been sent or discarded."""
    from ..notification_store import resolve_by_object

    return resolve_by_object(
        conn, user_id, SOURCE, OBJECT_TYPE, str(draft_id), by=by,
    )


def _draft_id(row: "NotificationRow") -> int | None:
    """The row's ``object_id`` as an integer, or None. See the confirmation source."""
    try:
        return int(str(row.object_id).strip())
    except (TypeError, ValueError):
        logger.warning(
            "notification %s names a non-numeric draft id %r", row.id, row.object_id,
        )
        return None


class OutboundDraftResolver:
    source = SOURCE
    auto_resolve_on_seen = False

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        from .. import outbound_drafts as drafts
        from ..notification_sources import NotificationAction, NotificationView

        draft_id = _draft_id(row)
        if draft_id is None:
            return None

        current = drafts.identity(conn, draft_id)
        if current is None:
            return None
        owner, status = current
        if owner != row.user_id:
            logger.error(
                "notification %s belongs to %r but names %r's draft %s",
                row.id, row.user_id, owner, draft_id,
            )
            return None
        if status not in (STATUS_PENDING, STATUS_SENDING):
            # sent or discarded: the decision was made somewhere else.
            return None

        # Through `identity` above and `get` only for the display text, in that
        # order: a row whose stored JSON is malformed can still be *discarded*,
        # and refusing to render it would leave the user an item with no action
        # that works. Same reasoning `outbound_drafts.discard` documents.
        title = row.title
        body = row.body
        try:
            draft = drafts.get(conn, draft_id)
        except Exception:
            logger.warning(
                "notification %s could not read draft %s for display",
                row.id, draft_id, exc_info=True,
            )
            draft = None
        if draft is not None:
            title = title_for(draft.to_addrs[0] if draft.to_addrs else "")
            body = body_for(draft.subject)

        if status == STATUS_SENDING:
            return NotificationView(
                title=title, body=body, severity=row.severity,
                status_note=_SENDING_NOTE,
            )

        actions = [
            NotificationAction(
                id="approve", label="Send", kind="primary", method="POST",
                endpoint=f"/chat/drafts/{draft_id}/approve",
            ),
            NotificationAction(
                id="discard", label="Discard", kind="danger", method="POST",
                endpoint=f"/chat/drafts/{draft_id}/discard",
            ),
        ]
        note = None
        if draft is None:
            actions = [actions[1]]
            note = _UNREADABLE_NOTE
        return NotificationView(
            title=title, body=body, severity=row.severity,
            actions=tuple(actions), status_note=note,
        )


RESOLVER = OutboundDraftResolver()
