"""Email threads as rooms (multiplayer D6, D10).

A thread with two or more humans besides the bot is a room, and so is a
stranger's first mail at a user's plus-address. Mail between the user and the
bot alone is the user's private email room. Every other mail stays as it was:
a task on a thread hash, mirrored into a room only by the existing routing.

- **The key** is the thread's root `Message-ID`: the first id in References,
  else In-Reply-To, else the message's own. The room binding is
  ``surface='email'``, ``surface_ref=<root>``. A later message finds the room
  through any id in its chain: the root through the binding, any other id
  through the room's stored mail (`processed_emails`, and the bot's own
  replies in `sent_emails`), so a client that keeps only In-Reply-To or trims
  References from the front still lands in it rather than founding a second
  room for the same thread.
- **Minting** happens only on evidence that the thread is the host's: their
  own address is on it, it threads onto a mail the bot sent for them, or it
  reached the bot at their plus-address. A thread the bot starts is minted at
  the send (`register_sent_thread`), with the sent mail as its first row, so
  the first reply finds it like any other. A reply with two or more people on
  it, on a thread sent before that existed, mints at the reply. A stranger's
  mail to ``bot+<user>@`` mints on receipt with one person on it, once the
  untrusted-sender gate admits it (a trusted sender, or the user approving the
  held mail, which mints at the approval: `confirmations.approve`). A held
  mail mints nothing. Rooms are otherwise user-created; this is the second
  system-minted kind after WhatsApp groups, and for the same reason: the
  container already exists on the surface, and the room is its transcript
  rather than a new conversation.
- **Mail between the user and the bot alone** is not a thread: it is a turn
  in the user's private email room (`private_room`, `is_private_mail`), which
  no message threads into (`thread_binding`).
- **Participants** are the union of From/To/Cc over the thread, bot addresses
  removed. Nobody leaves by being dropped from a Cc: the union is who has read
  the thread. The first message's people are the epoch baseline; anyone new on
  a later message always splits (D3), since email has no history
  acknowledgment.
- **Membership** is the host alone, for good (ISSUE-606). The thread is the
  host's correspondence: another istota user on it is a correspondent like
  anyone else, and the web refuses to add them as a member. Every admitted
  turn runs as the host at the host's full reach, with no guest mode
  (`rooms.scopes.is_email_thread_room`); the admit gate and the outbound gate
  are what bound it.
- **Addressed** is decided in code from the message's own headers
  (`intake_facts`, `thread_addressed`). The host's mail: the bot in To, or
  named in the new text (ISSUE-607). Anyone else's: named, or the host not on
  the message, which also marks the task `host_absent`. A correspondent's
  reply-all the host is on and that does not name the bot is recorded only.
- **The reply** is a reply-all to the latest message's participants, not the
  union, so somebody removed from Cc is respected (`reply_all`). The host's
  own addressed question, from a mail that authenticated, is answered with no
  outbound hold while those people are the ones it was asked in front of
  (`host_asked`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from email.utils import getaddresses, parseaddr
from typing import TYPE_CHECKING

from istota import db
from istota.rooms import policy as room_policy
from istota.mail.ownership import (
    bot_addressed_in_to, is_bot_address, match_thread, parse_message_ids,
)
from .. import participants
from .._types import ParticipantRef
from .private_room import is_private_email_ref

if TYPE_CHECKING:
    from ...config import Config

logger = logging.getLogger(__name__)

SURFACE = "email"
#: Humans besides the bot a received thread needs before it is a room. A
#: thread the bot started (`register_sent_thread`, or a reply on one sent
#: before rooms were minted at send) or one written to the user's
#: plus-address needs one besides the user.
MIN_HUMANS = 2

_NAME_MAX = 80
#: Display cap on a recorded subject, as `outbound._MAIL_SUBJECT_MAX_CHARS`.
_MAIL_SUBJECT_MAX = 200
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
    """The legacy canonical token, retained for migration tooling.

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


def thread_binding(conn, token: str | None):
    """The room's email binding when the room is a thread, else None.

    The user's private email room is bound to email too, under its creator's
    own token (`private_room`), and no message threads into it as a thread:
    mail there is the user's alone, and a Message-ID is a bearer token anyone
    on a forward holds.
    """
    room = db.get_room(conn, token) if token else None
    binding = db.get_room_binding(conn, token, SURFACE) if room else None
    if binding is None or is_private_email_ref(binding.surface_ref, room.user_id):
        return None
    return binding


def find_thread_room(conn, config: "Config", email) -> ThreadRoom | None:
    """The room this message's thread already is, or None. Reads only."""
    for mid in thread_message_ids(email):
        token = db.resolve_room_token(conn, SURFACE, mid) or _token_by_stored_mail(conn, mid)
        token = db._canonical_room_token(conn, token, cross_surface=False) if token else None
        binding = thread_binding(conn, token)
        if binding is None:
            continue
        return ThreadRoom(token=token, ref=binding.surface_ref,
                          host=find_host(conn, token))
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
            if thread_binding(conn, token) is not None:
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


def is_private_mail(config: "Config", email, user_id: str) -> bool:
    """Whether every human on this message is ``user_id`` themselves.

    From, To and Cc, folded, the bot's addresses dropped. Such a mail is a turn
    in the user's private email room rather than a thread (section 7).
    """
    owner = config.users.get(user_id)
    owned = {fold(a) for a in (owner.email_addresses if owner else [])}
    people = thread_people(config, email)
    return bool(people) and all(address in owned for address, _ in people)


def resolve_thread(
    conn, config: "Config", email, *,
    owner_user_id: str, existing: ThreadRoom | None, ours: bool,
    plus_address: bool = False,
) -> ThreadRoom | None:
    """Record this message's people in its thread's room, minting one if due.

    Only for a mail the untrusted-sender gate let through: the caller keeps a
    held mail out of the room entirely, so neither its sender nor anyone it
    copies becomes one of the thread's people on its strength. ``existing``
    is `find_thread_room`'s answer, asked before the caller's transaction
    wrote anything; ``ours`` is that the mail threads onto one the bot sent
    for ``owner_user_id``. ``plus_address`` is that the mail was routed to
    ``owner_user_id`` by their plus-address. Either is evidence enough the
    thread is theirs, and then one person on it is enough: a correspondent's
    reply to a mail the bot sent, or stranger first contact (the ``office``
    case).
    """
    people = thread_people(config, email)
    if existing is not None:
        _sync(conn, config, existing.token, people, acknowledged=False)
        return existing
    ids = thread_message_ids(email)
    if not ids or len(people) < (1 if (ours or plus_address) else MIN_HUMANS):
        return None
    owner = config.users.get(owner_user_id)
    owned = {fold(a) for a in (owner.email_addresses if owner else [])}
    if not (ours or plus_address or any(address in owned for address, _ in people)):
        return None
    if find_thread_room(conn, config, email) is not None:
        # The thread's room exists but the caller could not use it (its host is
        # no longer configured): never re-found it with this message's people.
        return None
    return _mint(conn, config, owner_user_id=owner_user_id, root=ids[0],
                 subject=getattr(email, "subject", None), people=people)


def _mint(
    conn, config: "Config", *, owner_user_id: str, root: str,
    subject: str | None, people,
) -> ThreadRoom | None:
    """Mint the thread's room, bound to its root id, with ``people`` as the
    baseline. The one copy, for a mail received and a mail sent alike."""
    if root.startswith("email-"):
        # The shape of a private email room's ref (`private_room`). A sender
        # chooses the Message-ID, and a thread bound under a user's private
        # token would take their own mail as a thread with a stranger on it.
        logger.warning("Refusing a thread root shaped like a private email room ref")
        return None
    room = db.register_bound_room(
        conn, owner_user_id, origin=SURFACE, name=_room_name(subject),
        surface=SURFACE, surface_ref=root,
    )
    if room is None:
        return None
    token = room.token
    # The people on the first message are who the thread was written for.
    _sync(conn, config, token, people, acknowledged=True)
    db.mark_audience_baseline(conn, token, SURFACE)
    return ThreadRoom(token=token, ref=root, host=owner_user_id)


def _addresses(values) -> list[tuple[str, str]]:
    """``(folded address, display name)`` for each address in ``values``,
    in order, once each."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, address in getaddresses([str(v) for v in (values or ()) if v]):
        address = fold(address)
        if "@" not in address or address in seen:
            continue
        seen.add(address)
        out.append((address, " ".join((name or "").split())))
    return out


def record_sent_mail(
    conn, room_token: str, *, to, cc=(), subject: str | None, body: str | None,
) -> int:
    """The bot's mail as a row of its thread's room, with its outgoing-mail
    card (ISSUE-612's shape), so the transcript starts with what was sent.

    Not tied to the task that sent it: that task ran in another room, and
    tying the row to it would pair its turn into this room's history and show
    its trace here (the rule `private_replies._post_answers_task` states).
    """
    message_id = db.add_message(
        conn, room_token, role="assistant", body=body or "", origin_surface=SURFACE,
    )
    db.set_outgoing_mail(conn, message_id, {
        "to": [address for address, _ in _addresses(to)],
        "cc": [address for address, _ in _addresses(cc)],
        "subject": (subject or "")[:_MAIL_SUBJECT_MAX],
        "state": "sent",
    })
    return message_id


def register_sent_thread(
    conn, config: "Config", *, user_id: str, message_id: str | None,
    in_reply_to: str | None = None, references: str | None = None,
    to=(), cc=(), subject: str | None = None, body: str | None = None,
    task_id: int | None = None,
) -> ThreadRoom | None:
    """Put a mail the bot sent for ``user_id`` into its thread's room.

    Called after each send is recorded in `sent_emails`, so a reply finds its
    room from the first one on. The root is worked out as for inbound mail
    (`thread_message_ids`). A thread that already has a room records the
    recipients as its people, only when ``user_id`` hosts it, since the ids
    can come from a deferred file the model writes. A thread with none is
    minted when anyone besides the user and the bot is on it, with the sent
    mail as its first row; mail to the user's own addresses mints nothing and
    lands in their private email room, when they have one.
    Bcc is never passed in and never becomes a person.

    Best-effort and never raises: its writes go in a savepoint, so a failure
    rolls back only this and leaves the send recorded. A reply then mints at
    the reply, through `ownership.match_thread`.
    """
    savepoint = f"sent_thread_{uuid.uuid4().hex[:12]}"
    try:
        conn.execute(f"SAVEPOINT {savepoint}")
    except Exception as e:  # noqa: BLE001 — see the docstring
        logger.warning("Could not register the thread of sent mail %s (task %s): %s",
                       message_id, task_id, e)
        return None
    try:
        room = _register_sent_thread(
            conn, config, user_id=user_id,
            ids=SimpleNamespace(message_id=message_id, in_reply_to=in_reply_to,
                                references=references),
            to=to, cc=cc, subject=subject, body=body, task_id=task_id,
        )
        conn.execute(f"RELEASE {savepoint}")
        return room
    except Exception as e:  # noqa: BLE001 — see the docstring
        try:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        except Exception:  # noqa: BLE001 — the warning below is the report
            pass
        logger.warning("Could not register the thread of sent mail %s (task %s): %s",
                       message_id, task_id, e)
        return None


def _bound_room(conn, ids: list[str]) -> ThreadRoom | None:
    """The room bound to one of ``ids``, through the binding alone.

    Not `find_thread_room`: its stored-mail arm reads `sent_emails`, whose row
    for this very send names the sending task's room, so a new mail from a
    task in a thread room would fold into that thread.
    """
    for mid in ids:
        token = db.resolve_room_token(conn, SURFACE, mid)
        token = db._canonical_room_token(conn, token, cross_surface=False) if token else None
        binding = thread_binding(conn, token)
        if binding is not None:
            return ThreadRoom(token=token, ref=binding.surface_ref,
                              host=find_host(conn, token))
    return None


def _stored_for_someone_else(conn, ids: list[str], user_id: str) -> bool:
    """Whether any of ``ids`` is a mail stored for another user."""
    marks = ",".join("?" * len(ids))
    for table in ("processed_emails", "sent_emails"):
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE message_id IN ({marks}) "
            "AND user_id IS NOT NULL AND user_id != ? LIMIT 1",
            (*ids, user_id),
        ).fetchone()
        if row is not None:
            return True
    return False


def _record_in_private_room(
    conn, *, user_id: str, task_id: int | None, to, cc,
    subject: str | None, body: str | None,
) -> None:
    """Mail the bot sent to the user alone, as a row of their private email
    room once it exists, like a push into a phone room. Never mints. Not when
    the sending task's answer is a row there already: the scheduler stores a
    room turn's answer before it is mailed."""
    from ..routing import private_surface_room

    room = private_surface_room(conn, SURFACE, user_id)
    if room is None:
        return
    if task_id is not None and db.get_turn_message_id(conn, room, task_id) is not None:
        return
    record_sent_mail(conn, room, to=to, cc=cc, subject=subject, body=body)


def _register_sent_thread(
    conn, config: "Config", *, user_id: str, ids, to, cc,
    subject: str | None, body: str | None, task_id: int | None = None,
) -> ThreadRoom | None:
    root_ids = thread_message_ids(ids)
    owner = config.users.get(user_id)
    if not root_ids or owner is None:
        return None
    people = [(address, name) for address, name in _addresses([*to, *cc])
              if not is_bot_address(config, address)]
    existing = _bound_room(conn, root_ids)
    if existing is not None:
        if existing.host != user_id:
            logger.warning("Sent mail for %s threads onto a room they do not host; "
                           "not recorded there", user_id)
            return None
        _sync(conn, config, existing.token, people, acknowledged=False)
        return existing
    if _stored_for_someone_else(conn, root_ids, user_id):
        # The ids can come from a deferred file the task wrote: a thread
        # another user's mail is on is never bound under this one.
        logger.warning("Sent mail for %s names another user's thread; no room minted",
                       user_id)
        return None
    owned = {fold(a) for a in owner.email_addresses}
    if not any(address not in owned for address, _ in people):
        if people:
            _record_in_private_room(conn, user_id=user_id, task_id=task_id,
                                    to=to, cc=cc, subject=subject, body=body)
        return None
    room = _mint(conn, config, owner_user_id=user_id, root=root_ids[0],
                 subject=subject, people=people)
    if room is not None:
        record_sent_mail(conn, room.token, to=to, cc=cc, subject=subject, body=body)
    return room


def recipients_json(email) -> str:
    """To and Cc as `processed_emails.recipients` stores them."""
    return json.dumps(
        list(getattr(email, "to", ()) or ()) + list(getattr(email, "cc", ()) or ()),
    )


# An attribution line, in the forms the common clients write: English
# ("On … wrote:"), German ("Am … schrieb …:") and French ("Le … a écrit :").
_ATTRIBUTION = re.compile(
    r"^\s*(On\b.*\bwrote:|Am\b.*\bschrieb\b.*:|Le\b.*\ba\s+écrit\s*:)\s*$",
    re.IGNORECASE,
)
# The tail of one a client wrapped, whose opening is up to two lines above.
_ATTRIBUTION_TAIL = re.compile(r"(\bwrote:|\bschrieb\b.*:|\ba\s+écrit\s*:)\s*$",
                               re.IGNORECASE)
_ATTRIBUTION_HEAD = re.compile(r"^\s*(On|Am|Le)\b", re.IGNORECASE)
_FORWARD = re.compile(
    r"^\s*(-{2,}\s*(Original Message|Forwarded message)\s*-{2,}"
    r"|Begin forwarded message:|_{8,}\s*$)",
    re.IGNORECASE,
)
# Outlook's quoted header block: a From: line followed by Sent: or Date:.
_HEADER_FROM = re.compile(r"^\s*From:\s", re.IGNORECASE)
_HEADER_NEXT = re.compile(r"^\s*(Sent|Date):\s", re.IGNORECASE)


def new_text(body: str | None) -> str:
    """The part of a mail its sender wrote now, above the quoted history.

    Cut at an attribution line (wrapped over up to three lines included), a
    forwarded or original-message marker, Outlook's underscore rule or its
    ``From:``/``Sent:`` header block, with every ``>``-quoted line dropped.
    Otherwise every later reply quoting a question to the bot would ask it
    again. A client quoting in some other form is read as new text.
    """
    lines = (body or "").splitlines()
    kept: list[str] = []
    for index, line in enumerate(lines):
        if _ATTRIBUTION.match(line) or _FORWARD.match(line):
            break
        if _HEADER_FROM.match(line) and any(
            _HEADER_NEXT.match(following) for following in lines[index + 1:index + 4]
        ):
            break
        if _ATTRIBUTION_TAIL.search(line):
            head = next(
                (back for back in (1, 2) if index - back >= 0
                 and _ATTRIBUTION_HEAD.match(lines[index - back])
                 and len(kept) >= back and kept[-back] == lines[index - back]),
                None,
            )
            if head is not None:
                del kept[-head:]
                break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


def addressed_in_new_text(config: "Config", body: str | None) -> bool:
    """Whether the mail's new text names the bot, by the web rule.

    ``@<name>`` anywhere, or the name as the first word of any line, since a
    mail opens with a greeting. "Ask zorg about it" is about the bot, not to it.
    """
    from ..web import addressed_to_bot_in_text

    return addressed_to_bot_in_text(
        new_text(body), (getattr(config, "bot_name", "") or "",), any_line=True,
    )


def asked_by_name(config: "Config", body: str | None) -> bool:
    """Whether the new text puts a question to the bot by name, not merely
    mentions it: ``@<name>`` anywhere, or the name followed by a comma or a
    colon at the start of a line ("Zorg, when …"). Stricter than
    `addressed_in_new_text`, which also takes "Zorg booked the table": this one
    decides whether the host's question goes out without an outbound hold.
    """
    name = (getattr(config, "bot_name", "") or "").strip()
    if not name:
        return False
    escaped = re.escape(name)
    pattern = rf"(?<![\w@])@{escaped}(?![\w-])|^\s*{escaped}\s*[,:]"
    return re.search(pattern, new_text(body), re.IGNORECASE | re.MULTILINE) is not None


@dataclass(frozen=True)
class IntakeFacts:
    """What one message's own headers say about its host and the bot."""

    #: The sender claims to be the host. A claim, as `sender_claims_to_be_user`
    #: says; authentication is `host_asked`'s question, not this one.
    author_is_host: bool
    #: One of the host's own addresses is in this message's To or Cc.
    host_on_message: bool
    #: The new text names the bot (`addressed_in_new_text`).
    named: bool

    @property
    def host_absent(self) -> bool:
        """Somebody else wrote it, and the host is not on it."""
        return not self.author_is_host and not self.host_on_message


def intake_facts(config: "Config", email, host_user_id: str) -> IntakeFacts:
    """The three facts the intake decision on a thread room is made from.

    Read from this message's headers alone. Missing To and Cc mean the host is
    not on the message, which fails toward telling them.
    """
    from istota.mail.support import own_addresses, sender_claims_to_be_user

    own = {fold(a) for a in own_addresses(config, host_user_id)}
    listed = list(getattr(email, "to", ()) or ()) + list(getattr(email, "cc", ()) or ())
    return IntakeFacts(
        author_is_host=sender_claims_to_be_user(
            config, host_user_id, getattr(email, "sender", None)),
        host_on_message=any(
            fold(address) in own for _, address in getaddresses([str(v) for v in listed if v])
        ),
        named=addressed_in_new_text(config, getattr(email, "body", None)),
    )


def thread_addressed(config: "Config", email, facts: IntakeFacts) -> bool:
    """Whether a thread room's admitted mail asks the bot, by the intake table.

    The host's own mail keeps ISSUE-607's rule: the bot in To, or named. For
    anyone else the bot in To says nothing, since every reply-all on a thread
    the bot started has it there: a mail is asked when it names the bot, or
    when the host is not on it, since then nobody else will tell them.
    """
    if facts.author_is_host:
        return bot_addressed_in_to(config, email) or facts.named
    return facts.named or not facts.host_on_message


#: Routes whose held mail is admitted to a room when the user approves it.
_ADMITTED_ROUTES = ("plus_address", "sender_match", "thread_room")


def admit_approved_mail(conn, config: "Config", task) -> str | None:
    """Put a held mail the user just approved into its room, and point its
    task at that room. Returns the room token, or None to leave the task as it
    was held.

    The untrusted-sender gate keeps a held mail out of every room; approving
    is the admission (section 7). The user's own mail lands in their private
    email room. Anyone else's lands in its thread's room: an existing one
    records the sender as one of the thread's people, and a stranger's first
    mail at the user's plus-address mints one, as an admitted mail would have
    on receipt. The task then runs as that room's turn: its answer is a
    reply-all through the outbound gate, or a reply to the user.

    Rebuilt from the `processed_emails` row, which holds the headers the
    decision needs. Runs in a savepoint; a failure rolls this back, logs, and
    leaves the approval standing.
    """
    if config is None or getattr(task, "source_type", None) != SURFACE:
        return None
    savepoint = f"admit_mail_{uuid.uuid4().hex[:12]}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        token = _admit_approved_mail(conn, config, task)
        conn.execute(f"RELEASE {savepoint}")
        return token
    except Exception as e:  # noqa: BLE001 — see the docstring
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        logger.warning("Could not admit approved mail of task %s to its room: %s",
                       task.id, e)
        return None


def _admit_approved_mail(conn, config: "Config", task) -> str | None:
    from ..ingest import record_inbound, record_phone_turn
    from .private_room import email_conversation_token

    row = conn.execute(
        'SELECT sender_email, recipients, message_id, "references", subject, '
        "routing_method FROM processed_emails WHERE task_id = ? ORDER BY id LIMIT 1",
        (task.id,),
    ).fetchone()
    if row is None or row["routing_method"] not in _ADMITTED_ROUTES:
        return None
    try:
        listed = json.loads(row["recipients"] or "[]")
    except ValueError:
        listed = []
    email = SimpleNamespace(
        sender=row["sender_email"], to=tuple(str(a) for a in listed if a), cc=(),
        message_id=row["message_id"], references=row["references"], in_reply_to=None,
        subject=row["subject"], body="",
    )
    user_id = task.user_id
    if is_private_mail(config, email, user_id):
        result = record_phone_turn(
            conn, config, surface=SURFACE, surface_ref=email_conversation_token(user_id),
            user_id=user_id, text=task.prompt, channel_name="Email", record_only=True,
            sender_address=row["sender_email"],
        )
        if result.message_id is None:
            return None
        _point_task_at(conn, task, result.room_token, result.message_id,
                       host_absent=False)
        return result.room_token
    existing = find_thread_room(conn, config, email)
    if existing is not None and existing.host != user_id:
        return None
    sent = match_thread(conn, email) if existing is None else None
    if sent is not None and sent.user_id != user_id:
        sent = None
    room = resolve_thread(
        conn, config, email, owner_user_id=user_id, existing=existing,
        ours=sent is not None, plus_address=row["routing_method"] == "plus_address",
    )
    if room is None:
        return None
    if existing is None and sent is not None:
        # A thread sent before rooms were minted at send, as the poller's
        # early mint does: the bot's mail first, with no body to show.
        record_sent_mail(conn, room.token, to=[sent.to_addr], subject=sent.subject,
                         body="")
    author = author_ref(conn, config, room.token, row["sender_email"])
    result = record_inbound(
        conn, config, surface=SURFACE, surface_ref=room.ref,
        user_id=author.user_id or room.host, text=task.prompt, source_type=SURFACE,
        output_target=SURFACE, sender_address=row["sender_email"], author=author,
        room_container=True, record_only=True, addressed_to_bot=True,
    )
    if result.message_id is None:
        return None
    # `reply_all` reads the thread's latest stored mail by room token.
    conn.execute("UPDATE processed_emails SET thread_id = ? WHERE task_id = ?",
                 (room.token, task.id))
    _point_task_at(conn, task, room.token, result.message_id,
                   host_absent=intake_facts(config, email, room.host).host_absent)
    return room.token


def _point_task_at(conn, task, room_token: str, message_id: int, *, host_absent: bool) -> None:
    """The held task, as the turn the poller would have created in the room."""
    multi = participants.is_multi_human(
        conn, surface=SURFACE, room_token=room_token, is_group_chat=False,
        room_container=True,
    )
    conn.execute("UPDATE messages SET task_id = ? WHERE id = ?", (task.id, message_id))
    conn.execute(
        "UPDATE tasks SET conversation_token = ?, output_target = ?, "
        "talk_delivery_token = NULL, host_absent = ?, "
        "is_group_chat = ?, audience = ? WHERE id = ?",
        (room_token, SURFACE, int(host_absent), int(multi),
         room_policy.audience_class(conn, room_token, is_group_chat=multi), task.id),
    )


def thread_room_for_task(conn, task) -> str | None:
    """The thread room an email task belongs to, or None."""
    if getattr(task, "source_type", None) != SURFACE or not task.conversation_token:
        return None
    if thread_binding(conn, task.conversation_token) is None:
        return None
    return task.conversation_token


def _reply_recipients(config: "Config", row) -> tuple[str, list[str]] | None:
    """A stored message's reply-all: its sender in To, its other recipients in
    Cc, the bot's addresses dropped. None without a usable sender."""
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
    return to, cc


def host_asked(conn, config: "Config", room_token: str | None, task, plan) -> bool:
    """Whether ``task`` answers the host's own authenticated, addressed
    question on this thread, to the people it was asked in front of.

    The release from the outbound hold (ISSUE-607) needs all of it: the task
    was created by a mail the poller marked `host_asked` (the host's own
    address, a DMARC pass, the bot addressed), it is no guest's turn and runs
    as the room's host, and ``plan``'s recipients are exactly that mail's
    reply-all. Anything else (a forged or unauthenticated mail, a scheduled
    job, a subtask, a thread that gained a recipient since) is gated as usual.
    """
    if not room_token or task is None or getattr(task, "guest_participant_id", None):
        return False
    host = find_host(conn, room_token)
    if host is None or host != getattr(task, "user_id", None):
        return False
    row = conn.execute(
        'SELECT sender_email, recipients FROM processed_emails '
        "WHERE task_id = ? AND thread_id = ? AND host_asked = 1 "
        "ORDER BY id LIMIT 1",
        (task.id, room_token),
    ).fetchone()
    if row is None:
        return False
    people = _reply_recipients(config, row)
    return people is not None and people == (plan.to, list(plan.cc))


def find_host(conn, room_token: str) -> str | None:
    """The room's host: the policy's, else its creator, as `find_thread_room`."""
    room = db.get_room(conn, room_token)
    if room is None:
        return None
    policy = room_policy.get_policy(conn, room_token)
    return policy.host_user_id if policy and policy.host_user_id else room.user_id


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
    people = _reply_recipients(config, row)
    if people is None:
        return None
    to, cc = people
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
    "IntakeFacts",
    "admit_approved_mail",
    "MIN_HUMANS",
    "ReplyAll",
    "SURFACE",
    "ThreadRoom",
    "addressed_in_new_text",
    "asked_by_name",
    "author_ref",
    "find_host",
    "find_thread_room",
    "fold",
    "host_asked",
    "intake_facts",
    "is_present",
    "is_private_mail",
    "new_text",
    "recipients_json",
    "record_sent_mail",
    "register_sent_thread",
    "reply_all",
    "resolve_thread",
    "speaking_user",
    "thread_message_ids",
    "thread_addressed",
    "thread_binding",
    "thread_people",
    "thread_room_for_task",
    "thread_room_token",
]
