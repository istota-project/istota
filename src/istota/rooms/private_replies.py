"""Private replies: what a shared room has for one member, in that member's own room (ISSUE-608).

A shared room is read by several people, so a confirmation, a whisper, a held
guest proposal or a private answer meant for one of them goes somewhere only
they read. That place is a room the member already has: their private room on
the shared room's own surface where there is one, else their private room on
any other surface. Nothing here creates a room. Where the member has none, the
notification bell is the last resort.

The delivery, each a plain function:

- `private_room_for`, the resolver. Composed from the existing "private"
  decisions (`db.default_web_room`, `db.configured_default_room`,
  `db._default_room_candidates`, `transport.routing.private_phone_room`), each
  candidate re-checked by `db.is_private_room_of`, the predicate the relay's
  default-room destination uses too. SMS is never a destination.
- `deliver_private`, the record phase, inside the caller's transaction: one
  `role='system'` row in the member's room, tagged with `about_room_token` and
  keyed `private-<kind>:<reference>`. The key is unique across the whole
  `messages` table, so a retry returns the first row, in the room it first
  landed in. With no private room it writes nothing into any room; a whisper
  then becomes a `task_alert` bell row.
- `send_private`, the send phase, after the caller's commit and with its own
  short connections: the Talk post (after the live audience check, stamping
  the Talk id onto the row so a Talk reply resolves to it), the WhatsApp send
  (keyed `private-reply:<row id>`, which is how a quote is resolved back to
  the row), the heads-up mail for an email-thread parent, and the bell row's
  delivery. Never raises. Nothing crosses the network under a writer lock.

Linking (`linked_about`, `quoted_private_reply`, `linked_room`,
`linked_context`, `pin_plan`): a reply to or quote of a tagged row links the
new turn to the shared room the row is about, through `tasks.about_room_token`.
`transport.ingest.record_inbound` applies the one rule for every surface; a
WhatsApp quote is resolved back to its row through the ``private-reply:``
ledger key first, or a linked turn's answer through its ``task-result:`` key
(the scheduler tags that answer with the same link). A linked turn reads the
room's recent transcript, fenced, and never delivers into it.

My notes (`my_notes_room`): which shared room's notes a task may read.

Confirmations (`park_about`, `parked_here`): which park is asked privately,
and which parked tasks a bare answer in a private room can reach.

The three verbs, the first and last on the relay request table rather than a
second hold table (multiplayer D16). `room whisper` (`enqueue_whisper`) is a
task in a shared room writing to its principal privately, queued at once
because it reaches only them. Its stored kind is `side_whisper`, a historical
name kept to avoid a CHECK rebuild. `room answer-privately`
(`queue_private_answer`) asks the principal's own question again in their
private room, as a turn of theirs linked to the shared room. `room post`
(`hold_room_post`) is a private-room task's post into a shared room, held for
the member's approval of the exact text through the relay's preview-digest
machinery, and released without approval only when the text is the member's
own words on a clean turn (ISSUE-565's rule). Guest proposals
(`propose_guest_reply`) are put to the host through the scheduler's privately
routed confirmation path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from istota import db
from istota.lib.untrusted import frame_untrusted
from istota.relay.requests import (
    CLAIM_RECOVERY_SECONDS,
    ROOM_KINDS,
    RequestError,
    _clean_turn,
    _queue_question,
    check_preview_fits,
    _question_response,
    _store_request,
    _validate_input,
    text_hash,
    write_transaction,
)
from istota.rooms.scopes import canonical_token, is_current_member  # noqa: F401 — canonical_token re-exported; one copy

logger = logging.getLogger(__name__)

HEADER_PREFIX = "re: "
UNTRUSTED_LABEL = "PARENT ROOM TRANSCRIPT"
PARENT_CONTEXT_MESSAGES = 40
PARENT_CONTEXT_CHARS = 12000
_LABEL_MAX = 80


def is_shared_room(conn, room_token: str, *, is_group_chat: bool = False) -> bool:
    """The signal `room_scopes` restricts on: the surface's roster, or the room's."""
    return bool(is_group_chat) or db.room_is_shared(conn, room_token)


def room_label(room: db.Room | None) -> str:
    """A room's name for a header line: flattened, capped, never empty."""
    from istota.confirmations import flatten

    name = flatten(db.room_display_name(room, None) or "") if room else ""
    return name[:_LABEL_MAX] or "a shared room"



KINDS = ("whisper", "confirmation", "proposal", "answer_notice")

#: What the shared room is told when a member's note could not stay out of
#: sight any other way. Names nobody and carries nothing of the note.
SHARED_ROOM_NOTICE = (
    "I've sent a private note to the person who asked. If you don't have a "
    "private chat with me yet, message me directly."
)

PRIVATE_NOTE_ALERT = "private_note"
WHATSAPP_KEY_PREFIX = "private-reply:"
#: A task's final answer on WhatsApp (`.claude/rules/sms.md`, the namespaces).
ANSWER_KEY_PREFIX = "task-result:"

_EMAIL_HINTS = {
    "confirmation": "Reply in your private chat with the bot, or approve it in notifications.",
    "proposal": "Reply in your private chat with the bot, or approve it in notifications.",
    "whisper": "Reply in your private chat with the bot.",
    "answer_notice": "Reply in your private chat with the bot.",
}


@dataclass(frozen=True)
class PrivateDestination:
    """A member's own private room, and how a note reaches it."""

    room_token: str
    #: ``web``, ``talk`` or ``whatsapp``: the surface the row is recorded as.
    surface: str
    #: The room's Talk binding, posted to after the live audience check.
    talk_ref: str | None = None
    #: The member's private WhatsApp room: sent to their own chat.
    whatsapp: bool = False


@dataclass(frozen=True)
class PrivateDelivery:
    """What the record phase wrote, carried to the send phase."""

    dest: PrivateDestination | None
    message_id: int | None
    user_id: str = ""
    about_token: str = ""
    kind: str = ""
    delivery_reference: str = ""
    #: The bell row a whisper with no private room became, for `deliver_pending`.
    notice: object = None


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def _checked(conn, token, user_id: str, *, surface: str, allow_phone: bool = False):
    if not token or not db.is_private_room_of(conn, token, user_id, allow_phone=allow_phone):
        return None
    room = db.get_room(conn, token)
    talk = db.get_room_binding(conn, room.token, "talk")
    return PrivateDestination(room_token=room.token, surface=surface,
                              talk_ref=talk.surface_ref if talk else None,
                              whatsapp=surface == "whatsapp")


def _talk_candidates(conn, user_id: str) -> list[str]:
    """The configured default room, then the rooms that could be the default.

    Registry rooms only, never the Talk poller's in-memory 1:1 cache: a row is
    written into the room a note goes to, so it has to be a registry room, and
    a 1:1 the poller has seen already is one.
    """
    configured = db.configured_default_room(conn, user_id)
    tokens = [configured] if configured else []
    for room in db._default_room_candidates(conn, user_id):
        if room.token not in tokens:
            tokens.append(room.token)
    return tokens


def _talk_room(conn, user_id: str) -> PrivateDestination | None:
    for token in _talk_candidates(conn, user_id):
        dest = _checked(conn, token, user_id, surface="talk")
        if dest is not None and dest.talk_ref:
            return dest
    return None


def _web_room(conn, user_id: str) -> PrivateDestination | None:
    handle = db.default_web_room(conn, user_id)
    return _checked(conn, handle.token, user_id, surface="web") if handle else None


def _whatsapp_room(conn, user_id: str) -> PrivateDestination | None:
    from istota.transport.routing import private_phone_room

    # `private_phone_room` does not look at `archived`; the predicate does.
    token = private_phone_room(conn, "whatsapp", user_id)
    return _checked(conn, token, user_id, surface="whatsapp", allow_phone=True)


_FALLBACK_ORDER = (_web_room, _talk_room, _whatsapp_room)


def private_room_for(conn, config, user_id: str, about_token: str) -> PrivateDestination | None:
    """The private room a note about ``about_token`` reaches ``user_id`` in, or None.

    First the private room on the shared room's own surface: the member's
    WhatsApp room for a WhatsApp group, their private Talk room for a Talk
    room, their default web room for a web-only room. An email thread has no
    such room (a mail is notification only), so it starts at the fallback.
    Then any other private room, in a fixed order: web, Talk, WhatsApp. None
    when there is none; the caller falls back to the bell.
    """
    from istota.transport.routing import phone_room

    parent = canonical_token(conn, about_token) if about_token else None
    first = _web_room
    phone = phone_room(conn, parent) if parent else None
    if phone is not None and phone.group and phone.surface == "whatsapp":
        first = _whatsapp_room
    elif parent and db.get_room_binding(conn, parent, "talk") is not None:
        first = _talk_room
    elif parent and db.get_room_binding(conn, parent, "email") is not None:
        first = None
    if first is not None:
        dest = first(conn, user_id)
        if dest is not None:
            return dest
    for step in _FALLBACK_ORDER:
        if step is first:
            continue
        dest = step(conn, user_id)
        if dest is not None:
            return dest
    return None


# ---------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------

#: The prompt snapshot of a quoted parent, as web and Talk cap theirs.
REPLY_SNAPSHOT_CHARS = 1000


def linked_about(conn, parent_message_id: int | None, room_token: str | None) -> str | None:
    """The shared room a reply to ``parent_message_id`` is linked to, or None.

    The one linking rule, applied by `transport.ingest.record_inbound` for
    every surface that supplies a parent: the parent row is in the same room
    as the new turn and is tagged with ``about_room_token``. Nothing else
    links a turn, and nothing carries a link forward to the next one.
    """
    if parent_message_id is None or not room_token:
        return None
    row = conn.execute(
        "SELECT room_token, about_room_token FROM messages WHERE id = ?",
        (int(parent_message_id),),
    ).fetchone()
    if row is None or row["room_token"] != room_token:
        return None
    return row["about_room_token"] or None


def quoted_private_reply(conn, *, user_id: str, quoted_id: str | None,
                         room_token: str | None) -> tuple[int, str] | None:
    """``(messages.id, body)`` of the tagged row a WhatsApp quote names, or None.

    `send_private` keys a WhatsApp send ``private-reply:<messages.id>``, so the
    provider id the quote carries leads back to the row through `sent_whatsapp`
    (the shape `relays._quoted_relay` uses for ``relay-question:``). The row is
    then checked rather than trusted: ``messages.id`` has no AUTOINCREMENT, so
    a rowid can be reused once the newest rows are gone. It must be in this
    user's private WhatsApp room, carry ``about_room_token``, and have been
    written by `deliver_private`. Anything else is an ordinary quote, ignored.

    A linked turn's own answer is the other tagged row a member quotes (the
    answer to `room answer-privately`, say). It goes out under the task's
    ``task-result:<task id>`` key, so that key leads to the task's stored
    answer in this room, which the scheduler tags with the same link.
    """
    if not quoted_id or not room_token:
        return None
    row = conn.execute(
        "SELECT logical_key FROM sent_whatsapp WHERE meta_message_id = ? AND user_id = ? "
        "AND (logical_key LIKE ? OR logical_key LIKE ?) ORDER BY id DESC LIMIT 1",
        (quoted_id, user_id, WHATSAPP_KEY_PREFIX + "%", ANSWER_KEY_PREFIX + "%"),
    ).fetchone()
    if row is None:
        return None
    key = row["logical_key"]
    if key.startswith(ANSWER_KEY_PREFIX):
        raw = key[len(ANSWER_KEY_PREFIX):]
        if not raw.isdecimal():
            return None
        answer = conn.execute(
            "SELECT id, body FROM messages WHERE task_id = ? AND room_token = ? "
            "AND role = 'assistant' AND about_room_token IS NOT NULL "
            "ORDER BY id LIMIT 1",
            (int(raw), room_token),
        ).fetchone()
        if answer is None:
            return None
        return int(answer["id"]), answer["body"][:REPLY_SNAPSHOT_CHARS]
    raw = key[len(WHATSAPP_KEY_PREFIX):]
    if not raw.isdecimal():
        return None
    message = conn.execute(
        "SELECT id, room_token, body, about_room_token, delivery_reference "
        "FROM messages WHERE id = ?",
        (int(raw),),
    ).fetchone()
    if (
        message is None
        or message["room_token"] != room_token
        or not message["about_room_token"]
        or not (message["delivery_reference"] or "").startswith("private-")
    ):
        return None
    return int(message["id"]), message["body"][:REPLY_SNAPSHOT_CHARS]


def linked_room(conn, task) -> str | None:
    """The shared room a linked task may read and post into, or None.

    A link is checked again at execution, because a room can go and a member
    can leave between the reply and the run: the room must still exist, not
    archived, and the task's user must still be a current member (a Talk
    departure keeps the member row, so `is_current_member`). The task's own room must still be
    private, since the linked prompt tells the model only that user reads it.
    """
    about = getattr(task, "about_room_token", None)
    if not about:
        return None
    parent = canonical_token(conn, about)
    room = db.get_room(conn, parent) if parent else None
    if room is None or room.archived:
        return None
    if not is_current_member(conn, parent, task.user_id):
        return None
    own = canonical_token(conn, task.conversation_token) if task.conversation_token else None
    if not own or own == parent or db.room_is_shared(conn, own):
        return None
    return parent


def linked_context(conn, config, task) -> tuple[str | None, str]:
    """``(parent token, user-half transcript block)`` for a linked task, else ``(None, "")``."""
    parent = linked_room(conn, task)
    if parent is None:
        return None, ""
    return parent, transcript_context(
        conn, config, parent,
        heading="## Linked room (read-only)",
        intro=("This turn replies to a message about a shared room. Its recent "
               "messages, oldest first. Nothing written in this conversation "
               "reaches it."),
    )


# ---------------------------------------------------------------------------
# My notes
# ---------------------------------------------------------------------------

#: The surfaces whose turn is the principal speaking in a shared room: a Talk
#: room, a web room, a WhatsApp group and an email thread room.
_SPEAKER_SURFACES = ("talk", "web", "whatsapp", "email")


def my_notes_room(conn, task) -> str | None:
    """The shared room whose notes ``task.user_id`` wrote may go into this task.

    The principal's own turn in a shared room (not a subtask, command, skill
    or scheduled job), or a guest's turn the host answers, unless the room
    answers guests ``direct``: an unreviewed reply could repeat the notes.
    None everywhere else, a private room included.
    """
    from istota.rooms import policy as room_policy

    token = canonical_token(conn, task.conversation_token) if task.conversation_token else None
    room = db.get_room(conn, token) if token else None
    if room is None:
        return None
    if getattr(task, "guest_participant_id", None) is not None:
        if _host_of(conn, token) != task.user_id:
            return None
        policy = room_policy.get_policy(conn, token)
        if policy is None or policy.guest_reply == room_policy.DIRECT:
            return None
        return token
    if (task.source_type not in _SPEAKER_SURFACES or task.parent_task_id
            or task.command or task.skill or task.scheduled_job_id):
        return None
    if not is_shared_room(conn, token, is_group_chat=task.is_group_chat):
        return None
    return token


# ---------------------------------------------------------------------------
# Confirmations
# ---------------------------------------------------------------------------

#: The `messages.delivery_reference` prefixes of a privately routed park's
#: question: a confirmation, or a guest proposal put to the host. The
#: scheduler's park keys each ``<prefix><task id>:<prompt hash>``.
PARK_PREFIXES = ("private-confirmation:", "private-proposal:")


def park_about(conn, task) -> str | None:
    """The shared room a task's confirmation must be asked about privately, or None.

    A task in a shared room asks its principal privately, never in front of
    the room (multiplayer D4). A guest's turn does too even in a room no
    second member reads: the guest is the audience it must not reach (D2).
    """
    parent = canonical_token(conn, task.conversation_token) if task.conversation_token else None
    room = db.get_room(conn, parent) if parent else None
    if room is None:
        return None
    if (getattr(task, "guest_participant_id", None) is None
            and not (task.is_group_chat or db.room_is_shared(conn, parent))):
        return None
    return parent


def parked_here(conn, room_token: str, user_id: str) -> list:
    """``user_id``'s parked tasks whose private question is in ``room_token``."""
    clauses = " OR ".join("m.delivery_reference LIKE ? || t.id || ':%'" for _ in PARK_PREFIXES)
    rows = conn.execute(
        "SELECT DISTINCT t.id FROM tasks t JOIN messages m ON m.room_token = ? "
        f"AND ({clauses}) WHERE t.user_id = ? AND t.status = 'pending_confirmation' "
        "ORDER BY t.id",
        (room_token, *PARK_PREFIXES, user_id),
    ).fetchall()
    return [task for task in (db.get_task(conn, row["id"]) for row in rows) if task is not None]


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


class _Taken(Exception):
    """The reference already names a row in a room that is not this user's."""


def _existing(conn, delivery_reference: str, user_id: str) -> PrivateDestination | None:
    """The destination a retry already wrote to, so it never moves rooms.

    The key is unique across every room, so a row under it in a room that is
    not this user's own is another note's, and must not be taken as this one.
    """
    row = conn.execute(
        "SELECT room_token, origin_surface FROM messages WHERE delivery_reference = ?",
        (delivery_reference,),
    ).fetchone()
    if row is None:
        return None
    if not db.is_private_room_of(conn, row["room_token"], user_id, allow_phone=True):
        raise _Taken()
    talk = db.get_room_binding(conn, row["room_token"], "talk")
    return PrivateDestination(room_token=row["room_token"], surface=row["origin_surface"],
                              talk_ref=talk.surface_ref if talk else None,
                              whatsapp=row["origin_surface"] == "whatsapp")


def _bell_note(conn, *, user_id: str, about: str, reference: str, body: str, task_id):
    from istota.notifications.resolvers import task_alert

    return task_alert.write(
        conn, user_id, dedup_key=task_alert.private_note_key(reference),
        title=f"Private note about {room_label(db.get_room(conn, about))}",
        body=body, severity="info",
        params={"alert_type": PRIVATE_NOTE_ALERT, "about_room": about, "task_id": task_id},
    )


def deliver_private(conn, config, *, user_id: str, about_token: str, kind: str,
                    reference: str, body: str, task_id: int | None = None) -> PrivateDelivery:
    """Record a note for ``user_id`` about ``about_token``, inside the caller's transaction.

    ``reference`` is unique per note for the caller (it carries the task or
    request id). A resolver read error is logged and treated as "no private
    room", which fails toward the bell. Call `send_private` after commit.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown private reply kind: {kind!r}")
    about = canonical_token(conn, about_token) or about_token
    delivery_reference = f"private-{kind}:{reference}"
    try:
        dest = _existing(conn, delivery_reference, user_id)
        if dest is None:
            dest = private_room_for(conn, config, user_id, about)
    except _Taken:
        logger.warning("private %s %s: reference already used in another room; using the bell",
                       kind, reference)
        dest = None
    except sqlite3.Error as exc:
        logger.warning("private %s %s: room lookup failed (%s); using the bell",
                       kind, reference, type(exc).__name__)
        dest = None
    common = dict(user_id=user_id, about_token=about, kind=kind,
                  delivery_reference=delivery_reference)
    if dest is None:
        notice = None
        if kind == "whisper":
            notice = _bell_note(conn, user_id=user_id, about=about, reference=reference,
                                body=body, task_id=task_id)
        return PrivateDelivery(None, None, notice=notice, **common)
    message_id = db.add_message(
        conn, dest.room_token, role="system", body=body, origin_surface=dest.surface,
        about_room_token=about, delivery_reference=delivery_reference,
    )
    return PrivateDelivery(dest, message_id, **common)


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


def _parent_facts(config, about: str) -> tuple[str, bool]:
    with db.get_db(config.db_path) as conn:
        room = db.get_room(conn, about) if about else None
        email = room is not None and db.get_room_binding(conn, room.token, "email") is not None
        return room_label(room), email


def _stamp_talk(config, message_id: int, talk_id: int) -> None:
    with db.get_db(config.db_path) as conn:
        db.set_message_external_id(conn, message_id, "talk", str(talk_id))


async def _post_talk(config, delivery: PrivateDelivery, text: str) -> bool:
    from istota.relay import relays as message_relays
    from istota.relay.requests import RequestError
    from istota.transport.talk import TalkTransport

    ref = delivery.dest.talk_ref
    try:
        await message_relays.verify_private_audience(
            config, actor_user_id=delivery.user_id, origin={"talk_ref": ref})
    except message_relays.AudienceUnavailable:
        # An outage, not a wrong audience (relay.md, "Delivery").
        logger.warning("private %s: Talk participants unavailable; not posted",
                       delivery.delivery_reference)
        return False
    except RequestError:
        logger.warning("private %s: Talk room for %s is not private; not posted",
                       delivery.delivery_reference, delivery.user_id)
        return False
    try:
        talk_id = await TalkTransport(config).deliver(
            ref, text, reference_id=delivery.delivery_reference)
    except Exception as exc:
        logger.warning("private %s: Talk post failed (%s)",
                       delivery.delivery_reference, type(exc).__name__)
        return False
    if talk_id is None:
        return False
    await asyncio.to_thread(_stamp_talk, config, delivery.message_id, talk_id)
    return True


async def _send_whatsapp(config, delivery: PrivateDelivery, text: str) -> bool:
    from istota.transport.whatsapp import REACHED_META
    from istota.transport.whatsapp.outbound import current_destination, deliver_whatsapp

    if not await asyncio.to_thread(current_destination, config, delivery.user_id):
        return False
    try:
        record = await deliver_whatsapp(
            config, logical_key=f"{WHATSAPP_KEY_PREFIX}{delivery.message_id}",
            user_id=delivery.user_id, text=text,
        )
    except Exception as exc:
        logger.warning("private %s: WhatsApp send failed (%s)",
                       delivery.delivery_reference, type(exc).__name__)
        return False
    return record.status in REACHED_META


async def _send_heads_up(config, delivery: PrivateDelivery, label: str, body: str) -> bool:
    """The heads-up mail for an email-thread parent: notification only.

    A fresh mail to the member's own address with no threading headers, so it
    can never land on the thread; a reply to it is a new message, not an answer.
    """
    if not getattr(config.email, "enabled", False):
        return False
    user = config.users.get(delivery.user_id)
    address = user.email_addresses[0] if user and user.email_addresses else None
    if not address:
        return False
    try:
        await asyncio.to_thread(
            _send_private_mail, config, to=address,
            subject=HEADER_PREFIX + label,
            body=f"{body}\n\n{_EMAIL_HINTS[delivery.kind]}",
        )
    except Exception as exc:
        logger.warning("private %s: heads-up mail failed (%s)",
                       delivery.delivery_reference, type(exc).__name__)
        return False
    return True


def _late_bell_note(config, delivery: PrivateDelivery, body: str):
    with db.get_db(config.db_path) as conn:
        return _bell_note(conn, user_id=delivery.user_id, about=delivery.about_token,
                          reference=delivery.delivery_reference.split(":", 1)[1],
                          body=body, task_id=None)


async def _send(config, delivery: PrivateDelivery, *, header_room_label, body: str) -> bool:
    from istota.confirmations import flatten
    from istota.notifications.store import deliver_pending

    if delivery.notice is not None:
        await asyncio.to_thread(deliver_pending, config, [delivery.notice])
    label, email_parent = await asyncio.to_thread(_parent_facts, config, delivery.about_token)
    if header_room_label:
        label = flatten(header_room_label)[:_LABEL_MAX] or label
    text = f"{HEADER_PREFIX}{label}\n\n{body}"
    delivered = False
    pushed = False
    dest = delivery.dest
    if dest is not None and delivery.message_id is not None:
        if dest.talk_ref:
            pushed = True
            delivered = await _post_talk(config, delivery, text) or delivered
        if dest.whatsapp:
            pushed = True
            delivered = await _send_whatsapp(config, delivery, text) or delivered
    if email_parent and await _send_heads_up(config, delivery, label, body):
        delivered = True
    if delivery.kind == "whisper" and pushed and not delivered:
        # A whisper has no park bell behind it: one routed to a surface that
        # reached nobody there would sit unannounced in a transcript.
        notice = await asyncio.to_thread(_late_bell_note, config, delivery, body)
        await asyncio.to_thread(deliver_pending, config, [notice])
    return delivered


async def send_private(config, delivery: PrivateDelivery, *, body: str,
                       header_room_label: str | None = None) -> bool:
    """Push a recorded note to the surface its room lives on. Never raises.

    True when it reached the member outside web: a Talk post, a WhatsApp send
    the provider accepted, or the heads-up mail handed to SMTP. A web room
    needs no push (the room-event stream reads the row), so it reports False,
    and so does every failure. For a confirmation or a proposal, False means
    the park's own bell row is still owed its delivery. A whisper owes the
    caller nothing: it has no park bell, so whenever it was not pushed to the
    member (no private room, or a push that reached nobody) this function
    writes and delivers its `private_note` bell row itself. ``body`` is what the surface carries, which may differ from the row (a
    WhatsApp confirmation names its `!confirm` command). Failures are logged
    with the kind and reference, never the body.
    """
    try:
        return await _send(config, delivery, header_room_label=header_room_label, body=body)
    except Exception as exc:
        logger.warning("private %s: send failed (%s)",
                       delivery.delivery_reference, type(exc).__name__)
        return False


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _speaker(config, row) -> str:
    if row["author_label"]:
        return row["author_label"]
    if row["author_user_id"]:
        return row["author_user_id"]
    if row["role"] in ("assistant", "system"):
        return getattr(config, "bot_name", "") or "assistant"
    return "member"


def transcript_context(conn, config, room_token: str, *, heading: str, intro: str) -> str:
    """A shared room's recent transcript as a user-half block, or "".

    User-half material, fenced as untrusted: every line in it was written by
    somebody else in a room the member does not control. Newest kept when the
    cap bites.
    """
    rows = conn.execute(
        "SELECT role, body, author_user_id, author_label, created_at FROM messages "
        "WHERE room_token = ? ORDER BY id DESC LIMIT ?",
        (room_token, PARENT_CONTEXT_MESSAGES),
    ).fetchall()
    lines: list[str] = []
    used = 0
    for row in rows:
        line = f"[{row['created_at']}] {_speaker(config, row)}: {row['body']}"
        if used + len(line) > PARENT_CONTEXT_CHARS and lines:
            break
        lines.append(line[:PARENT_CONTEXT_CHARS])
        used += len(line)
    if not lines:
        return ""
    transcript = frame_untrusted("\n".join(reversed(lines)), UNTRUSTED_LABEL)
    return f"{heading}\n\n{intro}\n\n{transcript}"


# ---------------------------------------------------------------------------
# Pinned delivery
# ---------------------------------------------------------------------------


def pin_plan(config, task, plan: list, *, fallback=None) -> list:
    """Drop every destination that lands in the shared room a linked task is about.

    A linked task (ISSUE-608, ``tasks.about_room_token``) runs in its
    member's private room and never delivers into the shared room it is
    linked to. Keyed on the destination's channel resolved to a room token,
    so a room named by its canonical token, by its web token or by its Talk
    ref is caught alike.

    When that empties the plan, the task falls back to ``fallback()``, its own
    room by its origin's default plan. A read that fails keeps the plan: this
    runs for every linked task, and the database being unreadable is not a
    question about either room.
    """
    about = getattr(task, "about_room_token", None)
    if not plan or not about or not config.db_path:
        return plan
    # Opening a path that does not exist would create it, and a database that
    # does not exist holds no room to pin against.
    if not Path(config.db_path).exists():
        return plan
    try:
        with db.get_db(config.db_path) as conn:
            parent = canonical_token(conn, about) or about
            parent_refs = {parent} | {b.surface_ref for b in db.list_room_bindings(conn, parent)}
            kept = []
            dropped = False
            for dest in plan:
                channel = getattr(dest, "channel", None)
                if channel and (channel in parent_refs or canonical_token(conn, channel) == parent):
                    logger.info("task %s: dropped a delivery into the room it must not post into",
                                getattr(task, "id", "?"))
                    dropped = True
                    continue
                kept.append(dest)
    except Exception as exc:
        logger.warning("room pin check failed for task %s: %s", getattr(task, "id", "?"), exc)
        return plan
    if dropped and not kept and fallback is not None:
        try:
            own = fallback()
        except Exception as exc:
            # The planner's last interactive rung still answers the origin.
            logger.warning("room pin fallback failed for task %s: %s",
                           getattr(task, "id", "?"), exc)
            own = []
        for dest in own:
            channel = getattr(dest, "channel", None)
            if not (channel and channel in parent_refs):
                kept.append(dest)
    return kept


def _send_private_mail(config, *, to: str, subject: str, body: str) -> None:
    from istota.mail.support import get_email_config
    from istota.skills.email import send_email

    send_email(to=to, subject=subject, body=body, config=get_email_config(config),
               from_addr=config.email.bot_email)


def whatsapp_confirmation_body(prompt: str, task_id: int) -> str:
    """A privately routed question as the member's WhatsApp chat carries it.

    The question is parked against the shared room's task, and a bare YES in
    that chat answers only a question parked there, so the message names the
    command that answers it by id. The question is trimmed rather than the
    instruction, leaving room for the ``re: <room>`` header
    `send_private` puts in front.
    """
    from istota.transport.whatsapp.outbound import WHATSAPP_TEXT_LIMIT, render_whatsapp

    suffix = (f"\n\nTask #{task_id}. Reply `!confirm {task_id} yes` "
              f"or `!confirm {task_id} no`.")
    budget = WHATSAPP_TEXT_LIMIT - len(suffix) - len(HEADER_PREFIX) - _LABEL_MAX - 2
    return render_whatsapp(prompt, limit=budget) + suffix


# ---------------------------------------------------------------------------
# Guest proposals (multiplayer Stage 11)
# ---------------------------------------------------------------------------


def _host_of(conn, room_token: str) -> str | None:
    """The room's host while present, read without recording a loss."""
    from istota.rooms import policy as room_policy

    policy = room_policy.get_policy(conn, room_token)
    host = policy.host_user_id if policy is not None else None
    if host and room_policy.host_present(conn, room_token, host):
        return host
    return None


def guest_reply_mode(conn, task) -> str | None:
    """``direct`` or ``held`` for a guest's task at completion, or None.

    Read when the answer exists rather than frozen at the turn, so a host who
    tightened the policy meanwhile is obeyed. Anything but ``direct`` is held,
    ``off`` included: the turn already ran, and the host is the one to decide
    about its answer. None when the task's principal is no longer the room's
    host, and the answer then goes to nobody.
    """
    from istota.rooms import policy as room_policy

    token = canonical_token(conn, task.conversation_token)
    if not token or _host_of(conn, token) != task.user_id:
        return None
    policy = room_policy.get_policy(conn, token)
    return "direct" if policy.guest_reply == room_policy.DIRECT else "held"


@dataclass(frozen=True)
class GuestProposal:
    #: The shared room the proposal is about, which the scheduler routes the
    #: park's question to the host privately for.
    parent_token: str
    preview: str


def _guest_words(conn, task) -> tuple[str, str]:
    """The guest's label and what they wrote, off the transcript row."""
    row = conn.execute(
        "SELECT body, author_label FROM messages WHERE task_id = ? "
        "AND role = 'user' AND author_participant_id = ? ORDER BY id LIMIT 1",
        (task.id, task.guest_participant_id),
    ).fetchone()
    if row is None:
        return "A guest", ""
    return row["author_label"] or "A guest", row["body"] or ""


_GUEST_QUOTE_CHARS = 500


def propose_guest_reply(conn, config, task, reply: str) -> GuestProposal | None:
    """Hold a guest-triggered answer as a `room post` for the host (D4 item 2).

    The relay hold machinery carries it (D16): a held `room_post` request whose
    preview is the exact approval document, the task parked on that preview's
    digest, and the preview delivered to the host privately by the caller's
    ordinary privately routed confirmation path (ISSUE-608): their own private
    room, or the bell when they have none. None when it cannot be proposed —
    a parent the host no longer reads, or an answer the post path would
    refuse — and the caller then cancels rather than posting.
    """
    from istota.confirmations import flatten
    from istota.relay.requests import associate_confirmation

    parent = canonical_token(conn, task.conversation_token)
    if not parent:
        return None
    try:
        destination = _post_destination(conn, parent, task.user_id)
        if destination["talk_ref"]:
            from istota.transport.talk import TalkTransport
            if len(reply) > TalkTransport.capabilities.max_message_length:
                raise RequestError("invalid_rendering")
        label, words = _guest_words(conn, task)
        if len(words) > _GUEST_QUOTE_CHARS:
            words = words[:_GUEST_QUOTE_CHARS].rstrip() + "…"
        bot = flatten(getattr(config, "bot_name", "") or "") or "the assistant"
        recipients = ""
        email_recipients = None
        if destination.get("email_ref"):
            # On an email thread the post is a mail to these exact people, and
            # the host approving this preview approves that mail (D20): the
            # outbound gate does not hold a send that matches it.
            from istota.rooms.veto import with_email_notice
            from istota.transport.email import threads as email_threads
            from istota.transport.email.outbound import recipients_of

            plan = email_threads.reply_all(conn, config, parent, task_id=task.id)
            if plan is None:
                raise RequestError("parent_unavailable")
            email_recipients = recipients_of(plan)
            reply = with_email_notice(conn, config, parent, reply)
            recipients = (
                f"To: {email_recipients['to']}\n"
                f"Cc: {', '.join(email_recipients['cc']) or '(nobody)'}\n\n"
            )
        preview = (
            f"{label} asked in {destination['label']}:\n{words}\n\n"
            f"Post this answer there as {bot}? Reply yes to post it exactly as "
            "written, or no to drop it. Only the message below is posted.\n\n"
            f"{recipients}Message:\n{reply}"
        )
        stored_destination = {key: destination[key]
                              for key in ("kind", "room_token", "talk_ref", "label")}
        if email_recipients is not None:
            stored_destination["email_recipients"] = email_recipients
        with write_transaction(conn):
            # The host's own room, where the preview lands; with none, the
            # bell carries it and the origin names no room.
            private = private_room_for(conn, config, task.user_id, parent)
            origin = ({"surface": private.surface, "room_token": private.room_token}
                      if private is not None else {"surface": "notifications"})
            row = _store_request(
                conn, actor_user_id=task.user_id, task_id=task.id,
                request_key=f"guest-reply-{task.id}", kind="room_post",
                recipient_user_id=task.user_id, text=reply, service_body=reply,
                template_body=None, provider="room",
                binding_fingerprint=destination["fingerprint"], preview=preview,
                origin=origin,
                destination=stored_destination,
            )
            db.set_task_confirmation(conn, task.id, row["preview"])
            associate_confirmation(conn, actor_user_id=task.user_id, task_id=task.id,
                                   request_id=row["id"], preview_digest=row["preview_digest"])
    except (ValueError, RequestError) as exc:
        logger.info("task %s: guest reply not proposed: %s", task.id, exc)
        return None
    return GuestProposal(parent_token=parent, preview=row["preview"])


# ---------------------------------------------------------------------------
# room whisper and room post
# ---------------------------------------------------------------------------


def _owned_running_task(conn, *, actor_user_id: str, task_id: int):
    task = db.get_task(conn, task_id)
    if task is None or task.user_id != actor_user_id or task.status != "running":
        raise RequestError("task_unavailable")
    return task


def _replay(conn, *, actor_user_id: str, task_id: int, request_key: str, kind: str, text: str):
    existing = conn.execute(
        "SELECT * FROM whatsapp_skill_requests WHERE requester_user_id=? AND origin_task_id=? "
        "AND request_key=?", (actor_user_id, task_id, request_key),
    ).fetchone()
    if existing is None:
        return None
    if existing["content_hash"] != text_hash(json.dumps([kind, actor_user_id, text], ensure_ascii=True)):
        raise RequestError("request_conflict")
    return _question_response(conn, existing)


def _fingerprint(*parts) -> str:
    return text_hash(json.dumps(list(parts), ensure_ascii=True, separators=(",", ":")))


def _live_shared_room(conn, task, actor_user_id: str) -> str:
    """The shared room a task runs in, which ``actor_user_id`` is in now.

    `not_a_shared_room` otherwise. Membership is current membership, since a
    Talk departure keeps the member row (`rooms.scopes.is_current_member`).
    """
    parent = canonical_token(conn, task.conversation_token)
    room = db.get_room(conn, parent) if parent else None
    if (room is None or room.archived
            or not is_current_member(conn, parent, actor_user_id)
            or not is_shared_room(conn, parent, is_group_chat=task.is_group_chat)):
        raise RequestError("not_a_shared_room")
    return parent


def enqueue_whisper(conn, config, *, actor_user_id: str, task_id: int,
                    request_key: str, text: str) -> dict:
    """A shared-room task writes to its principal privately.

    Queued at once: the text reaches the principal alone, which is the audience
    of the task's own private reach, so there is nothing to approve. Refused
    from anywhere but a shared room the principal is in, since elsewhere the
    task's own answer already reaches only them. Where the note lands is
    decided when it is delivered (`_claim`), so a private room the principal
    opens in between is used; the answer here only predicts it, and says when
    the bell is all there is, so the model can tell the room.
    """

    _validate_input(request_key, text)
    _owned_running_task(conn, actor_user_id=actor_user_id, task_id=task_id)
    replay = _replay(conn, actor_user_id=actor_user_id, task_id=task_id,
                     request_key=request_key, kind="side_whisper", text=text)
    if replay is not None:
        return replay
    task = db.get_task(conn, task_id)
    with write_transaction(conn):
        parent = _live_shared_room(conn, task, actor_user_id)
        # `side_whisper` is the kind's stored name, kept to avoid a CHECK
        # rebuild; the destination is the principal's own private room.
        row = _store_request(
            conn, actor_user_id=actor_user_id, task_id=task_id, request_key=request_key,
            kind="side_whisper", recipient_user_id=actor_user_id, text=text,
            service_body=text, template_body=None, provider="room",
            binding_fingerprint=_fingerprint(actor_user_id, parent),
            destination={"kind": "private_reply", "about": parent, "user": actor_user_id},
        )
        response = _question_response(conn, row)
        if private_room_for(conn, config, actor_user_id, parent) is None:
            response["delivered_to"] = "notifications"
            response["room_notice"] = SHARED_ROOM_NOTICE
        return response


PRIVATE_ANSWER_REFERENCE = "private-answer:"
#: Where a member's own turn can be asked again privately: the surfaces whose
#: `tasks.prompt` is the member's own words in the room.
_ANSWER_SURFACES = ("talk", "web", "whatsapp")


def queue_private_answer(conn, config, *, actor_user_id: str, task_id: int) -> dict:
    """Ask a shared-room task's question again, in its principal's private room.

    For a question whose answer should not be read in the room (multiplayer
    D4 item 1, ISSUE-608). What is asked again is the principal's own turn, as
    stored with the task, never text the model wrote: the new turn runs at the
    principal's full reach with their personal memory, so a question composed
    by a model that has been reading guests' words would be an injection route
    to that reach. It is recorded as the principal's turn in their private room
    through `transport.ingest.record_inbound`, on that room's surface, tagged
    with the shared room (``about_room_token``), and its answer is delivered
    there the ordinary way. The tag gives it the room's transcript as context
    and keeps its delivery out of the room (`pin_plan`).

    Not the deferred-subtask op: a subtask is pinned to its parent's
    conversation, so it would run in the same shared room, and it is
    admin-only and rate-limited besides.

    Refused for a guest's turn (the question is not the host's), for anything
    but the principal's own turn in a shared room they are in, and when the
    principal has no private room (`no_private_room`): nothing creates one.
    Idempotent per task: a second call returns the first re-asked task.
    """
    from istota.transport.ingest import record_inbound, record_phone_turn

    task = _owned_running_task(conn, actor_user_id=actor_user_id, task_id=task_id)
    if getattr(task, "guest_participant_id", None) is not None:
        raise RequestError("guest_turn")
    if (task.source_type not in _ANSWER_SURFACES or task.parent_task_id
            or task.command or task.skill or task.scheduled_job_id):
        raise RequestError("unsupported_origin")
    reference = f"{PRIVATE_ANSWER_REFERENCE}{task.id}"
    with write_transaction(conn):
        existing = conn.execute(
            "SELECT task_id FROM messages WHERE delivery_reference = ?", (reference,),
        ).fetchone()
        if existing is not None:
            if existing["task_id"] is None:
                raise RequestError("request_conflict")
            return {"status": "queued", "task_id": int(existing["task_id"])}
        parent = _live_shared_room(conn, task, actor_user_id)
        dest = private_room_for(conn, config, actor_user_id, parent)
        if dest is None:
            raise RequestError("no_private_room")
        binding = db.get_room_binding(conn, dest.room_token, dest.surface)
        surface_ref = binding.surface_ref if binding is not None else dest.room_token
        common = dict(
            surface=dest.surface, surface_ref=surface_ref, user_id=actor_user_id,
            text=task.prompt, attachments=task.attachments or None,
            about_room_token=parent, delivery_reference=reference,
        )
        if dest.whatsapp:
            # The phone room's own recording path, so nothing about a phone
            # turn is written twice (`.claude/rules/transport.md`, "Phone rooms").
            result = record_phone_turn(conn, config, channel_name=None, **common)
        else:
            # No model, effort or brain carried over: the private room's own
            # defaults apply, as on the phone path, so the model and the brain
            # that runs it are resolved against the same room (ISSUE-420).
            result = record_inbound(
                conn, config, source_type=dest.surface,
                output_target="room" if dest.surface == "web" else None, **common,
            )
    if result.task_id is None:
        raise RequestError("no_private_room")
    return {"status": "queued", "task_id": int(result.task_id)}


def _post_destination(conn, parent_token: str, user_id: str) -> dict:
    """The room a post goes to, re-resolved the same way at hold and delivery."""
    from istota.relay.destinations import destination_fingerprint

    room = db.get_room(conn, parent_token)
    if (room is None or room.archived
            or not is_current_member(conn, parent_token, user_id)):
        raise RequestError("parent_unavailable")
    from istota.rooms.veto import is_vetoed

    if is_vetoed(conn, parent_token):
        # Switched off (D12): an approved post is refused, and the request
        # closes rather than waiting for the room to come back on.
        raise RequestError("room_off")
    talk = db.get_room_binding(conn, parent_token, "talk")
    whatsapp = db.get_room_binding(conn, parent_token, "whatsapp")
    email = db.get_room_binding(conn, parent_token, "email")
    destination = {"kind": "room", "room_token": parent_token,
                   "talk_ref": talk.surface_ref if talk else None, "label": room_label(room)}
    if whatsapp is not None:
        destination["whatsapp_ref"] = whatsapp.surface_ref
    if email is not None:
        destination["email_ref"] = email.surface_ref
    destination["fingerprint"] = destination_fingerprint(destination)
    return destination


def _post_target(conn, task, user_id: str, room: str | None) -> str:
    """The shared room a `room post` goes to: ``--room``, else the turn's link."""

    if room:
        from istota.rooms.lookup import Found, resolve_room

        found = resolve_room(conn, user_id, room)
        if not isinstance(found, Found):
            raise RequestError("parent_unavailable")
        target = found.room.token
    else:
        about = getattr(task, "about_room_token", None)
        if not about:
            raise RequestError("no_target_room")
        target = linked_room(conn, task)
        if target is None:
            raise RequestError("parent_unavailable")
    if not db.room_is_shared(conn, target) or not is_current_member(conn, target, user_id):
        raise RequestError("parent_unavailable")
    return target


def hold_room_post(conn, config, *, actor_user_id: str, task_id: int,
                   request_key: str, text: str, room: str | None = None) -> dict:
    """A private-room task asks to post ``text`` into a shared room, as the bot.

    From the member's own private room only, never from a shared room
    (`not_a_private_room`): the room is ``room`` when given (a token or a
    name), else the shared room the turn is linked to (ISSUE-608). Held for
    the member's approval of the exact preview, the relay's rule: the preview
    is stored with its digest, the task parks on it, and only an approval of
    that digest releases the post. The one exception is ISSUE-565's clean
    turn, with the recipient test swapped for "the text is the member's own
    words, verbatim in their prompt".
    """
    from istota.relay import relays as message_relays

    _validate_input(request_key, text)
    task = _owned_running_task(conn, actor_user_id=actor_user_id, task_id=task_id)
    replay = _replay(conn, actor_user_id=actor_user_id, task_id=task_id,
                     request_key=request_key, kind="room_post", text=text)
    if replay is not None:
        return replay
    if task.parent_task_id or task.command or task.skill or task.scheduled_job_id:
        raise RequestError("unsupported_origin")
    own = canonical_token(conn, task.conversation_token) if task.conversation_token else None
    if (not own or getattr(task, "guest_participant_id", None) is not None
            or is_shared_room(conn, own, is_group_chat=task.is_group_chat)):
        raise RequestError("not_a_private_room")
    origin = message_relays.private_origin(conn, config, actor_user_id=actor_user_id,
                                           surface=task.source_type,
                                           conversation_token=task.conversation_token)
    with write_transaction(conn):
        message_relays.validate_origin(conn, config, actor_user_id=actor_user_id, origin=origin)
        target = _post_target(conn, task, actor_user_id, room)
        destination = _post_destination(conn, target, actor_user_id)
        if destination["talk_ref"]:
            from istota.transport.talk import TalkTransport
            if len(text) > TalkTransport.capabilities.max_message_length:
                raise RequestError("invalid_rendering")
        from istota.confirmations import flatten
        bot = flatten(getattr(config, "bot_name", "") or "") or "the assistant"
        preview = (f"Post this in {destination['label']} as {bot}?\n"
                   "Only the message below is posted, exactly as written. "
                   "Sending must start within 10 minutes of approval.\n\n"
                   f"Message:\n{text}")
        # From a phone room the preview goes out by text, and must arrive whole.
        check_preview_fits(config, origin, preview)
        row = _store_request(
            conn, actor_user_id=actor_user_id, task_id=task_id, request_key=request_key,
            kind="room_post", recipient_user_id=actor_user_id, text=text,
            service_body=text, template_body=None, provider="room",
            binding_fingerprint=destination["fingerprint"], preview=preview,
            origin=origin,
            destination={key: destination[key] for key in ("kind", "room_token", "talk_ref", "label")},
        )
        if row["state"] == "held" and _clean_turn(conn, config, task, actor_user_id, post_text=text):
            _queue_question(conn, request_id=row["id"], relay_id=None,
                            digest=row["preview_digest"], approval="clean_turn")
            row = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id=?", (row["id"],)).fetchone()
            return _question_response(conn, row, approval="clean_turn")
        return _question_response(conn, row)


# ---------------------------------------------------------------------------
# Delivery, from the request drain
# ---------------------------------------------------------------------------


def _claim(config, request_id: str, fresh: bool) -> dict | None:
    """Admit a queued request and write its canonical row, in one transaction.

    Raises `RequestError` with a fixed reason for a request that must close.
    A stale `sending` claim is recovered by marking it sent: its canonical row
    was written with the claim, and the Talk half is never posted twice.
    """

    with db.get_db(config.db_path) as conn:
        with write_transaction(conn):
            row = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id=?", (request_id,)).fetchone()
            if row is None or row["kind"] not in ROOM_KINDS:
                return None
            if not fresh:
                stale = conn.execute("SELECT ? <= datetime('now', ?)",
                                     (row["updated_at"], f"-{CLAIM_RECOVERY_SECONDS} seconds")).fetchone()[0]
                if row["state"] == "sending" and stale:
                    _mark_sent(conn, request_id)
                return None
            if row["state"] != "queued":
                return None
            whatsapp_group = False
            email_thread = False
            email_recipients = None
            if row["queue_deadline"] is None or row["queue_deadline"] <= db.sql_datetime_now():
                raise RequestError("queue_expired")
            user = row["requester_user_id"]
            body = row["service_body"]
            if user not in config.users or row["recipient_user_id"] != user:
                raise RequestError("recipient_unavailable")
            if not body or text_hash(body) != row["service_hash"]:
                raise RequestError("invalid_rendering")
            destination = json.loads(row["destination"] or "{}")
            claim = {"request_id": request_id, "kind": row["kind"], "user_id": user, "body": body,
                     "task_id": row["origin_task_id"]}
            if row["kind"] == "room_post":
                if (not row["approved_at"] or row["approved_digest"] != row["preview_digest"]
                        or text_hash(row["preview"] or "") != row["approved_digest"]):
                    raise RequestError("request_unavailable")
                current = _post_destination(conn, destination.get("room_token") or "", user)
                if current["fingerprint"] != row["binding_fingerprint"]:
                    raise RequestError("destination_changed")
                parent = current["room_token"]
                whatsapp_group = bool(current.get("whatsapp_ref"))
                email_thread = bool(current.get("email_ref"))
                # Only a preview that showed them carries them (a guest
                # proposal on an email thread), and the digest check above is
                # what makes them the ones approved.
                email_recipients = destination.get("email_recipients")
                reference = "room-post:" + request_id
                message_id = db.add_message(conn, parent, role="system", body=body,
                                            origin_surface="web", delivery_reference=reference)
                claim.update(message_id=message_id, talk_ref=current["talk_ref"], parent=parent,
                             reference=reference, whatsapp_group=whatsapp_group,
                             email_thread=email_thread, email_recipients=email_recipients)
            else:
                # A whisper (stored kind `side_whisper`): the principal's own
                # private room, resolved now rather than at enqueue.
                parent = destination.get("about") or ""
                if (destination.get("kind") != "private_reply" or destination.get("user") != user
                        or _fingerprint(user, parent) != row["binding_fingerprint"]):
                    raise RequestError("destination_changed")
                if not is_current_member(conn, parent, user):
                    raise RequestError("parent_unavailable")
                claim["delivery"] = deliver_private(
                    conn, config, user_id=user, about_token=parent, kind="whisper",
                    reference=f"room-whisper:{request_id}", body=body,
                    task_id=row["origin_task_id"],
                )
            conn.execute("UPDATE whatsapp_skill_requests SET state='sending',updated_at=datetime('now') "
                         "WHERE id=?", (request_id,))
            return claim


def _mark_sent(conn, request_id: str) -> None:
    conn.execute("UPDATE whatsapp_skill_requests SET state='sent',error_code=NULL,closed_at=datetime('now'),"
                 "updated_at=datetime('now') WHERE id=? AND state='sending'", (request_id,))


def _settle(config, claim: dict, talk_id: int | None) -> None:
    with db.get_db(config.db_path) as conn:
        with write_transaction(conn):
            if talk_id is not None and claim["kind"] == "room_post":
                db.set_message_external_id(conn, claim["message_id"], "talk", str(talk_id))
            _mark_sent(conn, claim["request_id"])


def _finish(config, request_id: str, reason: str) -> None:
    """Close a request that could not be delivered, and tell its requester."""
    from istota.notifications.resolvers import task_alert
    from istota.notifications.store import deliver_pending

    state = "expired" if reason == "queue_expired" else "failed"
    notice = None
    with db.get_db(config.db_path) as conn:
        with write_transaction(conn):
            row = conn.execute("SELECT kind, requester_user_id FROM whatsapp_skill_requests WHERE id=?",
                               (request_id,)).fetchone()
            changed = conn.execute(
                "UPDATE whatsapp_skill_requests SET state=?,error_code=?,closed_at=datetime('now'),"
                "updated_at=datetime('now') WHERE id=? AND state IN ('queued','sending')",
                (state, reason, request_id),
            ).rowcount
            if changed and row is not None:
                what = "Room post" if row["kind"] == "room_post" else "Private note"
                notice = task_alert.write(
                    conn, row["requester_user_id"], dedup_key=f"private-reply-request:{request_id}",
                    title=f"{what} not delivered",
                    body=f"{what} {request_id} was not delivered ({reason}).",
                    params={"request_id": request_id, "status": state},
                )
    if notice is not None:
        deliver_pending(config, [notice])


async def deliver_request(config, row) -> None:
    """Deliver one queued whisper or `room_post`, or recover a stale claim."""

    fresh = row["state"] == "queued"
    try:
        claim = await asyncio.to_thread(_claim, config, row["id"], fresh)
    except RequestError as exc:
        await asyncio.to_thread(_finish, config, row["id"], str(exc))
        return
    if claim is None:
        return
    talk_id = None
    if claim["kind"] == "room_post":
        if claim["talk_ref"]:
            from istota.transport.talk import TalkTransport
            try:
                talk_id = await TalkTransport(config).deliver(
                    claim["talk_ref"], claim["body"], reference_id=claim["reference"])
            except Exception as exc:
                logger.warning("room post %s: Talk post failed: %s", claim["request_id"], exc)
        if claim["whatsapp_group"]:
            # The group itself (multiplayer D6). Like the Talk half, a failed
            # send does not fail the post: the canonical row is the post, and
            # the ledger row and its alert say what happened to the group.
            from istota.transport.whatsapp.outbound import deliver_whatsapp
            try:
                await deliver_whatsapp(
                    config, logical_key=claim["reference"], user_id=claim["user_id"],
                    text=claim["body"], group_room=claim["parent"])
            except Exception as exc:
                logger.warning("room post %s: WhatsApp post failed: %s", claim["request_id"], exc)
        if claim["email_thread"] and claim["task_id"] is not None:
            # The thread itself, as a reply-all through the outbound gate. As
            # with the other halves, the canonical row is the post; a held or
            # failed mail is reported by the gate and the send log.
            from istota.transport.email.outbound import deliver_thread_post
            try:
                await deliver_thread_post(
                    config, task_id=int(claim["task_id"]), room_token=claim["parent"],
                    body=claim["body"], approved_recipients=claim["email_recipients"])
            except Exception as exc:
                logger.warning("room post %s: email reply-all failed: %s", claim["request_id"], exc)
    else:
        # Never raises; a whisper that reached nobody becomes a bell row there.
        await send_private(config, claim["delivery"], body=claim["body"])
    await asyncio.to_thread(_settle, config, claim, talk_id)
