"""Email threads as rooms (multiplayer D6, D10).

A thread with two or more humans besides the bot is a room. Every other mail
stays exactly as it was: a task on a thread hash, mirrored into a room only by
the existing routing.

- **The key** is the thread's root `Message-ID`: the first id in References,
  else In-Reply-To, else the message's own. The room binding is
  ``surface='email'``, ``surface_ref=<root>``. A later message finds the room
  through any id in its chain: the root through the binding, any other id
  through the room's stored mail (`processed_emails`, and the bot's own
  replies in `sent_emails`), so a client that keeps only In-Reply-To or trims
  References from the front still lands in it rather than founding a second
  room for the same thread. `compute_thread_id` cannot be the key: it hashes the subject and
  the sender, so every correspondent's reply on one thread hashes differently.
- **Minting** happens only on evidence that the thread is the host's: their
  own address is on it, or it threads onto a mail the bot sent for them. A
  stranger copying people on a mail to ``bot+<user>@`` mints nothing, which
  keeps "existence, never creation" for unsolicited mail. A mail held by the
  untrusted-sender gate mints nothing either. Rooms are otherwise
  user-created; this is the second system-minted kind after WhatsApp groups,
  and for the same reason: the container already exists on the surface, and
  the room is its transcript rather than a new conversation.
- **Participants** are the union of From/To/Cc over the thread, bot addresses
  removed. Nobody leaves by being dropped from a Cc: the union is who has read
  the thread. The first message's people are the epoch baseline; anyone new on
  a later message always splits (D3), since email has no history
  acknowledgment.
- **Membership** is the host alone at minting. An address matching another
  istota user is a participant with that user id and a guest until the host
  adds them on web: an email From is a claim, not an identity, so mail cannot
  make anyone a member of somebody else's room.
- **The reply** is a reply-all to the latest message's participants, not the
  union, so somebody removed from Cc is respected (`reply_all`).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from email.utils import getaddresses, parseaddr
from typing import TYPE_CHECKING

from ... import db, room_policy
from ...email_ownership import is_bot_address, parse_message_ids
from .. import participants
from .._types import ParticipantRef

if TYPE_CHECKING:
    from ...config import Config

SURFACE = "email"
#: Humans besides the bot a thread needs before it is a room.
MIN_HUMANS = 2

_NAME_MAX = 80
_REPLY_PREFIX = re.compile(r"^\s*((re|fwd?)\s*:\s*)+", re.IGNORECASE)
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


@dataclass(frozen=True)
class ThreadRoom:
    token: str
    #: The root id the room is bound under.
    ref: str
    #: The istota user the thread's mail belongs to.
    host: str


@dataclass(frozen=True)
class ReplyAll:
    to: str
    cc: list[str]
    in_reply_to: str | None
    references: str | None
    subject: str


def fold(address: str | None) -> str:
    """An addr-spec as this module compares it: stripped, ASCII-lowercased."""
    return (address or "").strip().translate(_ASCII_LOWER)


def thread_message_ids(email) -> list[str]:
    """Every id the message names, root first."""
    ids: list[str] = []
    for value in (
        getattr(email, "references", None),
        getattr(email, "in_reply_to", None),
        getattr(email, "message_id", None),
    ):
        for mid in parse_message_ids(value):
            if mid not in ids:
                ids.append(mid)
    return ids


def thread_room_token(root: str) -> str:
    """The canonical token a thread's room is registered under.

    A digest, never the id: the token reaches task rows, logs and the prompt
    header, and a Message-ID can carry a hostname or a local part.
    """
    digest = hashlib.sha256(f"istota-email-thread-v1\0{root}".encode()).hexdigest()
    return "email-thread-" + digest[:24]


def thread_people(config: "Config", email) -> list[tuple[str, str]]:
    """``(address, display name)`` for each human on this message, From first."""
    raw = [getattr(email, "sender", "") or ""]
    raw += list(getattr(email, "to", ()) or ())
    raw += list(getattr(email, "cc", ()) or ())
    people: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, address in getaddresses(raw):
        address = fold(address)
        if "@" not in address or address in seen or is_bot_address(config, address):
            continue
        seen.add(address)
        people.append((address, " ".join((name or "").split())))
    return people


def find_thread_room(conn, config: "Config", email) -> ThreadRoom | None:
    """The room this message's thread already is, or None. Reads only."""
    for mid in thread_message_ids(email):
        token = db.resolve_room_token(conn, SURFACE, mid) or _token_by_stored_mail(conn, mid)
        token = db._canonical_room_token(conn, token, cross_surface=False) if token else None
        room = db.get_room(conn, token) if token else None
        binding = db.get_room_binding(conn, token, SURFACE) if room else None
        if binding is None:
            continue
        policy = room_policy.get_policy(conn, token)
        host = policy.host_user_id if policy and policy.host_user_id else room.user_id
        return ThreadRoom(token=token, ref=binding.surface_ref, host=host)
    return None


def _token_by_stored_mail(conn, message_id: str) -> str | None:
    """The thread room a message id was stored under, by us or by the bot."""
    for table, column in (("processed_emails", "thread_id"),
                          ("sent_emails", "conversation_token")):
        for row in conn.execute(
            f"SELECT {column} FROM {table} WHERE message_id = ? ORDER BY id DESC",
            (message_id,),
        ):
            if not row[0]:
                continue
            token = db._canonical_room_token(conn, row[0], cross_surface=False)
            if db.get_room_binding(conn, token, SURFACE) is not None:
                return token
    return None


def is_present(conn, room_token: str, sender: str | None) -> bool:
    """Whether the envelope sender is already one of the thread's people."""
    address = fold(parseaddr(sender or "")[1])
    return bool(address) and conn.execute(
        "SELECT 1 FROM room_participants WHERE room_token = ? AND surface = ? "
        "AND surface_ref = ? AND left_at IS NULL LIMIT 1",
        (room_token, SURFACE, address),
    ).fetchone() is not None


def speaking_user(conn, config: "Config", room_token: str, sender: str | None) -> str | None:
    """The istota user a turn speaks as: the sender's user, only if a member."""
    user = config.find_user_by_email(fold(parseaddr(sender or "")[1]))
    if user and db.is_room_member(conn, room_token, user):
        return user
    return None


def author_ref(conn, config: "Config", room_token: str, sender: str | None) -> ParticipantRef:
    name, address = parseaddr(sender or "")
    return ParticipantRef(
        surface=SURFACE, surface_ref=fold(address),
        user_id=speaking_user(conn, config, room_token, sender),
        display_name=" ".join((name or "").split()) or None,
    )


def _sync(conn, config: "Config", room_token: str, people, *, acknowledged: bool) -> None:
    for address, name in people:
        user = config.find_user_by_email(address)
        ref = ParticipantRef(
            surface=SURFACE, surface_ref=address, user_id=user, display_name=name or None,
        )
        db.upsert_room_participant(
            conn, room_token=room_token, surface=SURFACE, surface_ref=address,
            kind=participants.classify(conn, config, room_token, ref),
            user_id=user, display_name=name or None, acknowledged=acknowledged,
        )


def _room_name(subject: str | None) -> str | None:
    name = " ".join(_REPLY_PREFIX.sub("", subject or "").split())
    return name[:_NAME_MAX] or None


def resolve_thread(
    conn, config: "Config", email, *,
    owner_user_id: str, existing: ThreadRoom | None, ours: bool,
) -> ThreadRoom | None:
    """Record this message's people in its thread's room, minting one if due.

    Only for a mail the untrusted-sender gate let through: the caller keeps a
    held mail out of the room entirely, so neither its sender nor anyone it
    copies becomes one of the thread's people on its strength. ``existing``
    is `find_thread_room`'s answer, asked before the caller's transaction
    wrote anything; ``ours`` is that the mail threads onto one the bot sent
    for ``owner_user_id``.
    """
    people = thread_people(config, email)
    if existing is not None:
        _sync(conn, config, existing.token, people, acknowledged=False)
        return existing
    ids = thread_message_ids(email)
    if not ids or len(people) < MIN_HUMANS:
        return None
    owner = config.users.get(owner_user_id)
    owned = {fold(a) for a in (owner.email_addresses if owner else [])}
    if not (ours or any(address in owned for address, _ in people)):
        return None
    root = ids[0]
    token = thread_room_token(root)
    if db.get_room(conn, token) is not None:
        # The thread's room exists but the caller could not use it (its host is
        # no longer configured): never re-found it with this message's people.
        return None
    db.register_room(
        conn, token, owner_user_id, origin=SURFACE,
        name=_room_name(getattr(email, "subject", None)),
    )
    db.add_room_binding(conn, token, SURFACE, root)
    # The people on the first message are who the thread was written for.
    _sync(conn, config, token, people, acknowledged=True)
    db.mark_audience_baseline(conn, token, SURFACE)
    return ThreadRoom(token=token, ref=root, host=owner_user_id)


def recipients_json(email) -> str:
    """To and Cc as `processed_emails.recipients` stores them."""
    return json.dumps(
        list(getattr(email, "to", ()) or ()) + list(getattr(email, "cc", ()) or ()),
    )


def thread_room_for_task(conn, task) -> str | None:
    """The thread room an email task belongs to, or None."""
    if getattr(task, "source_type", None) != SURFACE or not task.conversation_token:
        return None
    if db.get_room_binding(conn, task.conversation_token, SURFACE) is None:
        return None
    return task.conversation_token


def reply_all(
    conn, config: "Config", room_token: str, *, task_id: int | None = None,
) -> ReplyAll | None:
    """Reply-all on the thread, or None with no message stored for it.

    The recipients are the latest message's: To is its sender, Cc its other
    recipients, the bot dropped. The latest rather than the union, so a
    person removed from Cc is not written to again. The threading headers
    answer the message that triggered ``task_id`` when it is on this thread
    (D5: a reply is threaded to what it answers), else the latest. Only mail
    admitted to the room is stored under its token, so a held message never
    decides either.
    """
    row = conn.execute(
        'SELECT sender_email, recipients, message_id, "references", subject '
        "FROM processed_emails WHERE thread_id = ? AND recipients IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (room_token,),
    ).fetchone()
    if row is None:
        return None
    to = fold(parseaddr(row["sender_email"] or "")[1])
    if "@" not in to:
        return None
    try:
        listed = json.loads(row["recipients"] or "[]")
    except ValueError:
        listed = []
    cc: list[str] = []
    for _, address in getaddresses([str(a) for a in listed if a]):
        address = fold(address)
        if ("@" not in address or address == to or address in cc
                or is_bot_address(config, address)):
            continue
        cc.append(address)
    message_id = row["message_id"]
    references = row["references"]
    subject = row["subject"] or ""
    if task_id is not None:
        trigger = conn.execute(
            'SELECT message_id, "references", subject FROM processed_emails '
            "WHERE task_id = ? AND thread_id = ? ORDER BY id DESC LIMIT 1",
            (task_id, room_token),
        ).fetchone()
        if trigger is not None and trigger["message_id"]:
            message_id = trigger["message_id"]
            references = trigger["references"]
            subject = trigger["subject"] or subject
    if references and message_id:
        references = f"{references} {message_id}"
    elif message_id:
        references = message_id
    return ReplyAll(
        to=to, cc=cc, in_reply_to=message_id, references=references,
        subject=subject,
    )


__all__ = [
    "MIN_HUMANS",
    "ReplyAll",
    "SURFACE",
    "ThreadRoom",
    "author_ref",
    "find_thread_room",
    "fold",
    "is_present",
    "recipients_json",
    "reply_all",
    "resolve_thread",
    "speaking_user",
    "thread_message_ids",
    "thread_people",
    "thread_room_for_task",
    "thread_room_token",
]
