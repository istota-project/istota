"""Private replies: what a shared room has for one member, in that member's own room (ISSUE-608).

A shared room is read by several people, so a confirmation, a whisper, a held
guest proposal or a private answer meant for one of them goes somewhere only
they read. That place is a room the member already has: their private room on
the shared room's own surface where there is one, else their private room on
any other surface. Nothing here creates a room. Where the member has none, the
notification bell is the last resort.

Three pieces, each a plain function:

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
`linked_context`): a reply to or quote of a tagged row links the new turn to
the shared room the row is about, through `tasks.about_room_token`.
`transport.ingest.record_inbound` applies the one rule for every surface; a
WhatsApp quote is resolved back to its row through the ``private-reply:``
ledger key first.

`side_rooms` still holds the side-room machinery these replace; the shared
helpers (`room_label`, `HEADER_PREFIX`, `_send_private_mail`,
`transcript_context`) are imported from there until the rename completes.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass

from istota import db
from istota.rooms.scopes import canonical_token, is_current_member

logger = logging.getLogger(__name__)

KINDS = ("whisper", "confirmation", "proposal", "answer_notice")

#: What the shared room is told when a member's note could not stay out of
#: sight any other way. Names nobody and carries nothing of the note.
SHARED_ROOM_NOTICE = (
    "I've sent a private note to the person who asked. If you don't have a "
    "private chat with me yet, message me directly."
)

PRIVATE_NOTE_ALERT = "private_note"
WHATSAPP_KEY_PREFIX = "private-reply:"

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
    if room.side_of:
        # Until side rooms are removed: a pinned default can still name one.
        return None
    talk = db.get_room_binding(conn, room.token, "talk")
    return PrivateDestination(room_token=room.token, surface=surface,
                              talk_ref=talk.surface_ref if talk else None,
                              whatsapp=surface == "whatsapp")


def _talk_candidates(conn, user_id: str) -> list[str]:
    """The configured default room, then the rooms that could be the default.

    `side_rooms.talk_view`'s registry half. Its last resort, the poller's
    in-memory 1:1 cache, is not carried over: a row is written into the room
    a note goes to, so it has to be a registry room, and a 1:1 the poller has
    seen already is one.
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
    """
    if not quoted_id or not room_token:
        return None
    row = conn.execute(
        "SELECT logical_key FROM sent_whatsapp WHERE meta_message_id = ? AND user_id = ? "
        "AND logical_key LIKE ? ORDER BY id DESC LIMIT 1",
        (quoted_id, user_id, WHATSAPP_KEY_PREFIX + "%"),
    ).fetchone()
    if row is None:
        return None
    raw = row["logical_key"][len(WHATSAPP_KEY_PREFIX):]
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
    from istota.rooms.side_rooms import transcript_context

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
    from istota.rooms.side_rooms import room_label

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
    from istota.rooms.side_rooms import room_label

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
    from istota.rooms import side_rooms

    if not getattr(config.email, "enabled", False):
        return False
    user = config.users.get(delivery.user_id)
    address = user.email_addresses[0] if user and user.email_addresses else None
    if not address:
        return False
    try:
        await asyncio.to_thread(
            side_rooms._send_private_mail, config, to=address,
            subject=side_rooms.HEADER_PREFIX + label,
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
    from istota.rooms.side_rooms import _LABEL_MAX, HEADER_PREFIX

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
