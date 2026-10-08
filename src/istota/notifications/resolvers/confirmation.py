"""A task parked in ``pending_confirmation`` — the inbox's first source.

Two producers write this row: the inbound email gate
(``transport/email/inbound.py``) and the scheduler's own confirmation park. Four
paths close it, all of them through ``confirmations.approve`` / ``decline``,
plus ``expire_stale_confirmations`` when the question times out.

The resolver is the backstop for all five. It returns ``None`` the moment the
task stops being ``pending_confirmation``, so a confirmation answered over Talk
can never render as still-waiting in the panel even if the close path was missed.

**The title never comes from ``tasks.prompt``.** For a gated email that column
*is* the untrusted message the gate is withholding. The bell renders the
row's stored title, which each producer builds from fixed text: the gate from
``confirmations.describe_email`` (the sender and subject, flattened), the
scheduler's park from :func:`park_title`. Rendering the stored title rather
than recomputing one from the task is what keeps the bell and the push on one
title (#663): the two producers cannot be told apart from the task, and an
email-origin park recomputed as the gate's held-mail label. A relay hold is
the one exception, rendered by ``describe_title`` for the web view only.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import _common

if TYPE_CHECKING:
    import sqlite3

    from istota.config import Config
    from istota.notifications.sources import NotificationRow, NotificationView
    from istota.notifications.store import RaiseResult

logger = logging.getLogger(__name__)

RECOVERY_FILL_TITLE = "Recovery code waiting for approval"

SOURCE = "confirmation"
OBJECT_TYPE = "task"

# Held mail carries a decision the user has not made yet, so the row is
# actionable and warns rather than informs.
SEVERITY = "warning"

# The task status this source watches. Spelled once: `resolve` returning None
# means "the object is gone", and `list_open` feeds those ids straight to
# `mark_stale` — so a literal that drifted from what the tasks table actually
# stores would close every open row of this source with nothing logged anywhere.
HELD_STATUS = "pending_confirmation"

# How much of a bot-composed question survives into the notification body. The
# title already carries the one-line label; this is the rest of what was asked.
_BODY_CHARS = 400

# The stored title and body of a held room post (a `room post` or a guest
# proposal), which is what a push carries: fixed words, never the preview.
PURCHASE_TITLE = "Purchase waiting for approval"
ROOM_POST_TITLE = "Room post awaiting approval"
ROOM_POST_BODY = "Open your private chat with the bot to review and approve it."
# The same, for an owner with no private chat: the bell is the preview (#633).
ROOM_POST_BELL_BODY = "Open this notification to review and approve it."

# The stored body of a scheduler park, which is what its push carries (#638):
# a push can land in a room, and a room gets a notice about the question, never
# the question. The bell renders the question from the live task.
PARK_BODY = (
    "The question is in the conversation it was asked in. "
    "Answer it there, or open this notification."
)
# The same, for a park whose question is in no room: the bell is the only
# place it is.
PARK_BELL_BODY = "Open this notification to read the question and answer it."
# What the owed re-push says once the question's own delivery failed: the place
# it was asked is where it did not arrive.
PARK_UNDELIVERED_BODY = (
    "The question could not be delivered where it was asked. "
    "Open this notification to read it and answer."
)


def owed_push(result: "RaiseResult | None") -> "RaiseResult | None":
    """A park's held result, re-pushed because the question's delivery failed."""
    return _common.pushing_only(result, PARK_UNDELIVERED_BODY)


def park_title(task_id: int) -> str:
    """The stored title of a scheduler park."""
    return f"Task #{int(task_id)} is waiting for your approval"


def dedup_key(task_id: int | str) -> str:
    """``task:{id}``.

    The prefix is load-bearing and stays here: idempotency comes from
    ``UNIQUE (user_id, source, dedup_key)``, and one character of drift between a
    producer and the backfill means every held item shows twice, permanently,
    with only one of the two closable.
    """
    return _common.object_dedup_key(OBJECT_TYPE, task_id)


def body_for(confirmation_prompt: str | None) -> str:
    """The notification body for a held task: its own question, flattened.

    One rule for **both** producers, and that is the point. This source has two
    — the inbound email gate and the scheduler's mid-run park — and the row they
    write is indistinguishable afterwards: same `source`, same `object_type`,
    same `object_id`, and `source_type` is `email` on both, because an
    email-origin task whose answer asks a question parks exactly like any other
    (`.claude/rules/transport.md` records that as a deliberate decision). A
    resolver branching on `source_type` therefore renders the *gate's* wording
    over the *scheduler's* question — "nothing has been run, and the message
    body is not shown", on a task that ran to completion.

    `tasks.confirmation_prompt` is the one thing that is right in both cases: it
    is the gate's own composed message for a hold, and the model's question for
    a park. Neither is the withheld body — the gate's message carries the sender
    and subject only, which is exactly what `describe` is allowed to show — and
    flattening covers the fact that both spellings embed attacker-supplied text.
    """
    from istota.confirmations import flatten

    return flatten(confirmation_prompt or "")[:_BODY_CHARS]


def write(
    conn: "sqlite3.Connection",
    user_id: str,
    *,
    task_id: int,
    title: str,
    body: str = "",
    room_token: str | None = None,
    in_room: bool = True,
) -> "RaiseResult | None":
    """Write the row on the producer's own connection, inside its transaction.

    Returns the :class:`RaiseResult` for the producer to buffer and hand to
    ``deliver_pending`` after its ``with`` block closes — see the store's module
    docstring for why the two are separate calls.

    ``in_room`` says the question is already shown in a room (the private
    room, the web room it was asked in, the Talk room), and makes the result
    `room_free`, so its push goes to ntfy and email only (#625). A producer
    whose question is in no room passes False and the push takes the user's
    whole alert routing: a private park with no private room, and the email
    gate, whose row is pushed only when its own prompt reached nobody.
    """
    from dataclasses import replace

    from istota.notifications.store import write_notification

    result = write_notification(
        conn, user_id,
        **_common.row_kwargs(
            source=SOURCE,
            dedup_key=dedup_key(task_id),
            title=title,
            body=body,
            severity=SEVERITY,
            actionable=True,
            object_type=OBJECT_TYPE,
            object_id=str(task_id),
            room_token=room_token,
        ),
    )
    return replace(result, room_free=in_room) if result is not None else None


def resolve_for_task(
    conn: "sqlite3.Connection", user_id: str, task_id: int, *, by: str,
) -> int:
    """Close the row for a task whose question has just been answered."""
    return _common.resolve_for(
        conn, user_id, SOURCE, OBJECT_TYPE, task_id, by=by,
    )


def _task_id(row: "NotificationRow") -> int | None:
    """The row's ``object_id`` as an integer, or None if it is not one."""
    return _common.coerce_object_id(row, noun="task", logger=logger)


def _title(conn, row: "NotificationRow", task) -> str:
    """The title the row was stored with, so the bell names what the push did."""
    from istota.confirmations import describe

    return row.title or describe(conn, task)


# Lines of a relay hold's preview the bell shows when a private room shows the
# whole of it: there the bell is a pointer to the preview, not the place to
# approve.
_PREVIEW_LINES = 2


def deep_link(room_token: str, task_id: int) -> str | None:
    """The web path that opens ``room_token`` at ``task_id``'s turn, or None
    when the token would not pass the notification URL allowlist."""
    from istota.notifications.sources import SAFE_PATH_RE

    href = f"/chat/r/{room_token}/t/{int(task_id)}"
    return href if SAFE_PATH_RE.match(href) else None


def bell_confirm_endpoint(task_id: int, digest: str) -> str:
    """The confirm route the bell posts to: the task and the preview it showed."""
    return f"/chat/tasks/{int(task_id)}/confirm/{digest}"


def _relay_held_view(conn, row: "NotificationRow", task) -> "NotificationView":
    """A relay question, room post or guest proposal waiting on its owner (#624).

    Where a private room shows the preview, no Confirm: the bell offers the
    link to that room, and the confirm route refuses an approval made anywhere
    else. Where none does (#633), the bell is the only place the preview can be
    read, so it shows the whole of it and offers a Confirm in the detail view
    only, whose path carries the digest of the preview shown; the route refuses
    it once the preview has changed or a private room has appeared. The preview
    is fenced, as `relay_question` fences a question: this view reaches only
    the authenticated web session, while the stored title and body a push
    carries stay the producer's fixed text.
    """
    from istota import confirmations
    from istota.lib.untrusted import frame_untrusted
    from istota.notifications.sources import NotificationAction, NotificationView
    from istota.relay.requests import text_hash
    from istota.rooms.private_replies import preview_rooms

    request = confirmations.held_request(conn, task)
    label = confirmations.flatten(
        str(confirmations.held_destination(request).get("label") or ""),
    ) or "a room"
    if request is not None and request["kind"] == "purchase":
        label = "this purchase"
    if request is not None and request["kind"] == "recovery_fill":
        label = "this recovery code"
    preview = task.confirmation_prompt or ""
    lines = [line for line in preview.splitlines() if line.strip()]
    actions = []
    rooms = preview_rooms(conn, task)
    if rooms:
        body = f"For {label}. Open your private chat to review and approve it."
        if lines:
            body += "\n\n" + frame_untrusted("\n".join(lines[:_PREVIEW_LINES]), "APPROVAL PREVIEW")
        href = deep_link(rooms[0], task.id)
        if href is not None:
            actions.append(NotificationAction(
                id="open", label="Open", kind="primary", method="LINK", href=href,
            ))
    else:
        body = f"For {label}. Confirm approves exactly the preview below."
        if lines:
            body += "\n\n" + frame_untrusted(preview, "APPROVAL PREVIEW")
            actions.append(NotificationAction(
                id="confirm", label="Confirm", kind="primary", method="POST",
                endpoint=bell_confirm_endpoint(task.id, text_hash(preview)),
                detail_only=True,
            ))
    actions.append(NotificationAction(
        id="discard", label="Discard", kind="danger", method="POST",
        endpoint=f"/chat/tasks/{task.id}/cancel",
    ))
    return NotificationView(
        title=confirmations.describe_title(conn, task), body=body,
        severity=row.severity, actions=tuple(actions),
    )


class ConfirmationResolver:
    source = SOURCE
    auto_resolve_on_seen = False

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        from istota import db
        from istota.notifications.sources import NotificationAction, NotificationView

        task_id = _task_id(row)
        if task_id is None:
            return None

        task = db.get_task(conn, task_id)
        if task is None:
            return None
        if task.user_id != row.user_id:
            # The row is already scoped to one user by the query that produced
            # it, so this can only be a producer that wrote somebody else's id.
            # Refuse rather than render: an action built from it would POST at
            # an endpoint that then correctly refuses, and the user would be
            # left pressing a button that never works.
            logger.error(
                "notification %s belongs to %r but names %r's task %s",
                row.id, row.user_id, task.user_id, task_id,
            )
            return None
        if task.status != HELD_STATUS:
            return None

        # A phone task's question was asked by text and is answered by text;
        # both endpoints refuse it from web (room-surface-model Stage 24), so
        # buttons here would never work.
        from istota.transport.routing import phone_transcript_surface

        phone = (
            phone_transcript_surface(conn, task.conversation_token)
            if task.conversation_token else None
        )
        if phone is not None:
            label = "SMS" if phone == "sms" else "WhatsApp"
            # The question too: when its text did not arrive, the bell is
            # where the owed push sends the user to read it (#638).
            question = body_for(task.confirmation_prompt)
            reply = f"Reply by {label} to answer this question."
            return NotificationView(
                title=_title(conn, row, task),
                body=f"{question}\n\n{reply}" if question else reply,
                severity=row.severity,
                actions=(),
            )

        if task.whatsapp_confirmation_request_id:
            return _relay_held_view(conn, row, task)

        return NotificationView(
            title=_title(conn, row, task),
            body=body_for(task.confirmation_prompt),
            severity=row.severity,
            actions=(
                NotificationAction(
                    id="confirm", label="Confirm", kind="primary", method="POST",
                    endpoint=f"/chat/tasks/{task_id}/confirm",
                ),
                NotificationAction(
                    id="discard", label="Discard", kind="danger", method="POST",
                    endpoint=f"/chat/tasks/{task_id}/cancel",
                ),
            ),
        )


RESOLVER = ConfirmationResolver()
