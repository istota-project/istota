"""Side rooms: one member's private companion of a shared room (multiplayer D4).

A shared room is read by several people, so anything meant for one of them
alone needs somewhere else to go. A side room is that place. It is an ordinary
private web room with one member, linked to its parent by `rooms.side_of` and
`rooms.side_for_user`, and created by the system the first time the parent has
something for that member alone. `db.ensure_side_room` records why the system
may create this one room when rooms are otherwise user-created.

What this module holds, and what it deliberately does not:

- **Context.** A task in a side room reads the parent's transcript as fenced,
  read-only material in the user half of its prompt (`parent_context`), and
  only while its member is still in the parent.
- **Pinned delivery.** Nothing a side-room task outputs reaches the parent:
  `pin_plan` drops every delivery destination that lands there.
- **Two verbs**, on the relay request table rather than a second hold table
  (D16). `room whisper` (`enqueue_whisper`) is a task in a shared room writing
  to its principal's side room, queued at once because it reaches only them.
  `room post` (`hold_room_post`) is a side-room task's post into the parent,
  held for the member's approval of the exact text through the relay's
  preview-digest machinery, and released without approval only when the text
  is the member's own words on a clean turn (ISSUE-565's rule).
- **Confirmations.** A confirmation a task in a shared room raises goes to the
  principal's side room, never into the room (`confirmation_route`).
- **External views.** On web the side room is itself. On Talk it is the
  member's own private conversation with the bot, each message headed
  ``re: <room>`` (`push_to_talk_view`), and only when the parent is on Talk.
  On WhatsApp it is the member's own chat with the bot's number, headed the
  same way (`push_to_whatsapp_view`), when the parent is a WhatsApp group. On
  email it is a private mail to the member's own address, never a reply on the
  thread (`push_to_email_view`), when the parent is an email thread room.
  Nothing binds the side room to either: it is its own room.

- **Side-room answers** (D4 item 1). A task in a shared room that needs a scope
  the room withholds asks for the answer privately (`queue_side_answer`): the
  principal's own question is put in their side room as a turn of theirs, and
  runs there as an ordinary private task. The shared-room task never gains the
  scope, and the side-room task's answer reaches the room only through a held
  `room post`.

Not here: the nested and ephemeral web rendering (Stage 17).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from . import db
from .untrusted import frame_untrusted
from .whatsapp_requests import (
    CLAIM_RECOVERY_SECONDS,
    ROOM_KINDS,
    RequestError,
    _clean_turn,
    _queue_question,
    _question_response,
    _store_request,
    _validate_input,
    text_hash,
    write_transaction,
)

logger = logging.getLogger(__name__)

HEADER_PREFIX = "re: "
UNTRUSTED_LABEL = "PARENT ROOM TRANSCRIPT"
PARENT_CONTEXT_MESSAGES = 40
PARENT_CONTEXT_CHARS = 12000
_LABEL_MAX = 80


def canonical_token(conn, token: str | None) -> str | None:
    """The registry token a conversation token names, or None for no room."""
    if not token:
        return None
    if db.get_room(conn, token) is not None:
        return token
    return db.find_room_token_by_ref(conn, token)


def is_shared_room(conn, room_token: str, *, is_group_chat: bool = False) -> bool:
    """The signal `room_scopes` restricts on: the surface's roster, or the room's."""
    return bool(is_group_chat) or db.room_is_shared(conn, room_token)


def room_label(room: db.Room | None) -> str:
    """A room's name for a header line: flattened, capped, never empty."""
    from .confirmations import flatten

    name = flatten(db.room_display_name(room, None) or "") if room else ""
    return name[:_LABEL_MAX] or "a shared room"


def task_side_room(conn, task) -> db.Room | None:
    """The side room a task runs in, while its member still reads the parent."""
    token = canonical_token(conn, task.conversation_token)
    side = db.side_room_parent(conn, token) if token else None
    if side is None or side.side_for_user != task.user_id:
        return None
    return side


def _speaker(config, row) -> str:
    if row["author_label"]:
        return row["author_label"]
    if row["author_user_id"]:
        return row["author_user_id"]
    if row["role"] in ("assistant", "system"):
        return getattr(config, "bot_name", "") or "assistant"
    return "member"


def parent_context(conn, config, task) -> str:
    """The parent room's recent transcript for a side-room task, or "".

    User-half material, fenced as untrusted: every line in it was written by
    somebody else in a room the member does not control. All of it, not only
    what the member saw, because the side room is where the member reads the
    room back (Stage 14 limits front-stage context by epoch; a side room is
    the unrestricted side). Newest kept when the cap bites.
    """
    side = task_side_room(conn, task)
    if side is None:
        return ""
    rows = conn.execute(
        "SELECT role, body, author_user_id, author_label, created_at FROM messages "
        "WHERE room_token = ? ORDER BY id DESC LIMIT ?",
        (side.side_of, PARENT_CONTEXT_MESSAGES),
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
    return (
        "## Parent room (read-only)\n\n"
        "This side room belongs to a shared room. Its recent messages, oldest "
        "first. Nothing written in this side room reaches it.\n\n"
        f"{transcript}"
    )


# ---------------------------------------------------------------------------
# Pinned delivery
# ---------------------------------------------------------------------------


def pin_plan(config, task, plan: list) -> list:
    """Drop every destination of a side-room task that lands in its parent.

    Keyed on the destination's channel resolved to a room token, so a parent
    named by its canonical token, by its web token or by its Talk ref is caught
    alike. A read that fails keeps the plan: this runs for every task, and the
    database being unreadable is not a side-room question.
    """
    if not plan or not getattr(task, "conversation_token", None) or not config.db_path:
        return plan
    # Opening a path that does not exist would create it, and a database that
    # does not exist holds no side room.
    if not Path(config.db_path).exists():
        return plan
    try:
        with db.get_db(config.db_path) as conn:
            token = canonical_token(conn, task.conversation_token)
            room = db.get_room(conn, token) if token else None
            if room is None or not room.side_of:
                return plan
            parent = room.side_of
            parent_refs = {parent} | {b.surface_ref for b in db.list_room_bindings(conn, parent)}
            kept = []
            dropped = False
            for dest in plan:
                channel = getattr(dest, "channel", None)
                if channel and (channel in parent_refs or canonical_token(conn, channel) == parent):
                    logger.info("task %s: dropped a delivery into its side room's parent",
                                getattr(task, "id", "?"))
                    dropped = True
                    continue
                kept.append(dest)
            if dropped and not kept:
                # What would have gone to the parent goes to the side room.
                from .transport.routing import Destination
                kept.append(Destination("web", room.token, "push"))
            return kept
    except Exception as exc:
        logger.warning("side room pin check failed for task %s: %s", getattr(task, "id", "?"), exc)
        return plan


# ---------------------------------------------------------------------------
# The Talk view
# ---------------------------------------------------------------------------


def _private_talk_ref(conn, room_token: str, user_id: str) -> str | None:
    room = db.get_room(conn, room_token)
    if room is None or room.archived or room.side_of:
        return None
    if db.list_room_members(conn, room_token) != [user_id]:
        return None
    binding = db.get_room_binding(conn, room_token, "talk")
    return binding.surface_ref if binding else None


def talk_view(conn, config, user_id: str) -> str | None:
    """The Talk conversation a user's side rooms are shown in, or None.

    Their own private conversation with the bot: the configured default room
    when it is a private Talk room, else the oldest private Talk room that
    could be their default, else the 1:1 the poller detected. A live
    participant check runs before anything is posted to it.
    """
    configured = db.configured_default_room(conn, user_id)
    if configured:
        ref = _private_talk_ref(conn, configured, user_id)
        if ref:
            return ref
    for room in db._default_room_candidates(conn, user_id):
        ref = _private_talk_ref(conn, room.token, user_id)
        if ref:
            return ref
    try:
        from .transport.talk import get_dm_token
    except ImportError:
        return None
    return get_dm_token(user_id)


async def push_to_talk_view(
    config, *, user_id: str, parent_token: str, body: str, reference_id: str,
) -> int | None:
    """Post ``body`` to the user's Talk view, headed ``re: <room>``.

    Only for a parent on Talk: a member reading the room on web reads its side
    room on web. Returns the Talk message id, or None when nothing was posted.
    """
    def _resolve():
        with db.get_db(config.db_path) as conn:
            parent = db.get_room(conn, parent_token)
            if parent is None or db.get_room_binding(conn, parent_token, "talk") is None:
                return None, None
            return talk_view(conn, config, user_id), HEADER_PREFIX + room_label(parent)

    ref, header = await asyncio.to_thread(_resolve)
    if not ref:
        return None
    from . import message_relays

    try:
        await message_relays.verify_private_audience(
            config, actor_user_id=user_id, origin={"talk_ref": ref})
    except RequestError:
        logger.warning("side room Talk view for %s is not private; not posted", user_id)
        return None
    from .transport.talk import TalkTransport

    try:
        return await TalkTransport(config).deliver(ref, f"{header}\n\n{body}", reference_id=reference_id)
    except Exception as exc:
        logger.warning("side room Talk view post failed for %s: %s", user_id, exc)
        return None


async def push_to_whatsapp_view(
    config, *, user_id: str, parent_token: str, body: str, reference_id: str,
) -> bool:
    """Send ``body`` to the user's own WhatsApp chat with the bot, headed ``re: <room>``.

    The side room's external view on WhatsApp (D4), and only for a parent
    bound to a WhatsApp group: a member reading the room elsewhere reads its
    side room there. The chat is the user's own binding, so it is private by
    construction. True when the message reached WhatsApp.
    """
    def _resolve():
        with db.get_db(config.db_path) as conn:
            parent = db.get_room(conn, parent_token)
            if parent is None or db.get_room_binding(conn, parent_token, "whatsapp") is None:
                return None
            return HEADER_PREFIX + room_label(parent)

    header = await asyncio.to_thread(_resolve)
    if header is None:
        return False
    from .transport.whatsapp import REACHED_META
    from .transport.whatsapp.outbound import current_destination, deliver_whatsapp

    if not await asyncio.to_thread(current_destination, config, user_id):
        return False
    try:
        record = await deliver_whatsapp(
            config, logical_key=f"side-view:{reference_id}", user_id=user_id,
            text=f"{header}\n\n{body}",
        )
    except Exception as exc:
        logger.warning("side room WhatsApp view send failed for %s: %s", user_id, exc)
        return False
    return record.status in REACHED_META


def _send_private_mail(config, *, to: str, subject: str, body: str) -> None:
    from .email_support import get_email_config
    from .skills.email import send_email

    send_email(to=to, subject=subject, body=body, config=get_email_config(config),
               from_addr=config.email.bot_email)


async def push_to_email_view(
    config, *, user_id: str, parent_token: str, body: str, reference_id: str,
) -> bool:
    """Mail ``body`` to the user's own address, headed ``re: <room>`` (D4).

    The side room's external view on email, and only for a parent bound to an
    email thread: a fresh mail with no threading headers, so it can never
    land on the thread itself. The address is the user's own configured one,
    which no outbound policy holds. True when the mail was handed to SMTP.
    """
    def _resolve():
        with db.get_db(config.db_path) as conn:
            parent = db.get_room(conn, parent_token)
            if parent is None or db.get_room_binding(conn, parent_token, "email") is None:
                return None
            return HEADER_PREFIX + room_label(parent)

    if not getattr(config.email, "enabled", False):
        return False
    user = config.users.get(user_id)
    address = user.email_addresses[0] if user and user.email_addresses else None
    subject = await asyncio.to_thread(_resolve)
    if subject is None or not address:
        return False
    try:
        await asyncio.to_thread(
            _send_private_mail, config, to=address, subject=subject, body=body,
        )
    except Exception as exc:
        logger.warning("side room email view send failed for %s (%s): %s",
                       user_id, reference_id, exc)
        return False
    return True


def email_confirmation_body(prompt: str, task_id: int) -> str:
    """A side-routed question as its email view carries it: a reply to that
    private mail is a new message, not an answer, so it names the command."""
    return (f"{prompt}\n\nTask #{task_id}. Answer in the side room, or send "
            f"!confirm {task_id} yes or !confirm {task_id} no on any surface.")


def whatsapp_confirmation_body(prompt: str, task_id: int) -> str:
    """A side-routed question as its WhatsApp view carries it.

    A bare YES in that chat answers only a question parked there, and this one
    is parked in the group's room, so the message names the command that
    answers it by id. The question is trimmed rather than the instruction,
    leaving room for the ``re: <room>`` header the view puts in front.
    """
    from .transport.whatsapp.outbound import WHATSAPP_TEXT_LIMIT, render_whatsapp

    suffix = (f"\n\nTask #{task_id}. Reply `!confirm {task_id} yes` "
              f"or `!confirm {task_id} no`.")
    budget = WHATSAPP_TEXT_LIMIT - len(suffix) - len(HEADER_PREFIX) - _LABEL_MAX - 2
    return render_whatsapp(prompt, limit=budget) + suffix


# ---------------------------------------------------------------------------
# Confirmations raised in a shared room
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfirmationRoute:
    """Where a shared-room task's confirmation goes instead of the room."""

    parent_token: str
    side_token: str | None
    talk_bound: bool
    # The parent is a WhatsApp group: the question also goes to the
    # principal's own WhatsApp chat (`push_to_whatsapp_view`).
    whatsapp_bound: bool = False
    # The parent is an email thread: the question also goes to the
    # principal's own address (`push_to_email_view`).
    email_bound: bool = False

    @property
    def externally_viewed(self) -> bool:
        return self.talk_bound or self.whatsapp_bound or self.email_bound


def confirmation_route(conn, task) -> ConfirmationRoute | None:
    """None for a task not in a shared room; the side room to use otherwise.

    ``side_token`` is None when no side room could be made (the principal is
    not a member). The question still stays out of the room; the confirmation
    notification is then the only place it is pushed.
    """
    parent_token = canonical_token(conn, task.conversation_token)
    parent = db.get_room(conn, parent_token) if parent_token else None
    if parent is None or parent.side_of:
        return None
    # A guest's turn asks its host privately even in a room no second member
    # reads: the guest is the audience it must not reach (multiplayer D2).
    if (getattr(task, "guest_participant_id", None) is None
            and not is_shared_room(conn, parent_token, is_group_chat=task.is_group_chat)):
        return None
    talk_bound = db.get_room_binding(conn, parent_token, "talk") is not None
    whatsapp_bound = db.get_room_binding(conn, parent_token, "whatsapp") is not None
    email_bound = db.get_room_binding(conn, parent_token, "email") is not None
    try:
        side = db.ensure_side_room(conn, parent_token, task.user_id)
    except ValueError:
        return ConfirmationRoute(parent_token=parent_token, side_token=None, talk_bound=False)
    return ConfirmationRoute(parent_token=parent_token, side_token=side.token,
                             talk_bound=talk_bound, whatsapp_bound=whatsapp_bound,
                             email_bound=email_bound)


def write_confirmation(conn, route: ConfirmationRoute, task, prompt: str) -> None:
    """The question as a row in the side room, keyed so a re-park adds a row."""
    if route.side_token is None:
        return
    db.add_message(
        conn, route.side_token, role="system", body=prompt, origin_surface="web",
        delivery_reference=f"side-confirmation:{task.id}:{text_hash(prompt)[:16]}",
    )


# ---------------------------------------------------------------------------
# Guest proposals and backstage memory (multiplayer Stage 11)
# ---------------------------------------------------------------------------


def _host_of(conn, room_token: str) -> str | None:
    """The room's host while present, read without recording a loss."""
    from . import room_policy

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
    from . import room_policy

    token = canonical_token(conn, task.conversation_token)
    if not token or _host_of(conn, token) != task.user_id:
        return None
    policy = room_policy.get_policy(conn, token)
    return "direct" if policy.guest_reply == room_policy.DIRECT else "held"


@dataclass(frozen=True)
class GuestProposal:
    route: ConfirmationRoute
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
    digest, and the preview written to the host's side room by the caller's
    ordinary side-routed confirmation path. None when it cannot be proposed —
    no side room, a parent the host no longer reads, or an answer the post
    path would refuse — and the caller then cancels rather than posting.
    """
    from .confirmations import flatten
    from .whatsapp_requests import associate_confirmation

    parent = canonical_token(conn, task.conversation_token)
    if not parent:
        return None
    try:
        side = db.ensure_side_room(conn, parent, task.user_id)
        destination = _post_destination(conn, parent, task.user_id)
        if destination["talk_ref"]:
            from .transport.talk import TalkTransport
            if len(reply) > TalkTransport.capabilities.max_message_length:
                raise RequestError("invalid_rendering")
        label, words = _guest_words(conn, task)
        if len(words) > _GUEST_QUOTE_CHARS:
            words = words[:_GUEST_QUOTE_CHARS].rstrip() + "…"
        bot = flatten(getattr(config, "bot_name", "") or "") or "the assistant"
        preview = (
            f"{label} asked in {destination['label']}:\n{words}\n\n"
            f"Post this answer there as {bot}? Reply yes to post it exactly as "
            "written, or no to drop it. Only the message below is posted.\n\n"
            f"Message:\n{reply}"
        )
        with write_transaction(conn):
            row = _store_request(
                conn, actor_user_id=task.user_id, task_id=task.id,
                request_key=f"guest-reply-{task.id}", kind="room_post",
                recipient_user_id=task.user_id, text=reply, service_body=reply,
                template_body=None, provider="room",
                binding_fingerprint=destination["fingerprint"], preview=preview,
                origin={"surface": "web", "room_token": side.token, "channel": side.token},
                destination={key: destination[key]
                             for key in ("kind", "room_token", "talk_ref", "label")},
            )
            db.set_task_confirmation(conn, task.id, row["preview"])
            associate_confirmation(conn, actor_user_id=task.user_id, task_id=task.id,
                                   request_id=row["id"], preview_digest=row["preview_digest"])
    except (ValueError, RequestError) as exc:
        logger.info("task %s: guest reply not proposed: %s", task.id, exc)
        return None
    talk_bound = db.get_room_binding(conn, parent, "talk") is not None
    whatsapp_bound = db.get_room_binding(conn, parent, "whatsapp") is not None
    email_bound = db.get_room_binding(conn, parent, "email") is not None
    return GuestProposal(
        route=ConfirmationRoute(parent_token=parent, side_token=side.token,
                                talk_bound=talk_bound, whatsapp_bound=whatsapp_bound,
                                email_bound=email_bound),
        preview=row["preview"],
    )


_SPEAKER_SURFACES = ("talk", "web")


def backstage_room(conn, task) -> db.Room | None:
    """The side room whose notes a task in a shared room may read (D4 item 4).

    The task's principal's side room, and only when that principal is the
    speaker (their own turn in the room) or the host a guest's turn runs as.
    A cron job, a subtask or a retry carrying the room's token has neither,
    so it reads nothing backstage, and nor does a task in a private room.
    """
    token = canonical_token(conn, task.conversation_token)
    room = db.get_room(conn, token) if token else None
    if room is None or room.side_of:
        return None
    if getattr(task, "guest_participant_id", None) is not None:
        if _host_of(conn, token) != task.user_id:
            return None
    else:
        if (task.source_type not in _SPEAKER_SURFACES or task.parent_task_id
                or task.command or task.skill or task.scheduled_job_id):
            return None
        if not is_shared_room(conn, token, is_group_chat=task.is_group_chat):
            return None
    side = db.get_side_room(conn, token, task.user_id)
    if side is None or db.side_room_parent(conn, side.token) is None:
        return None
    return side


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


def enqueue_whisper(conn, config, *, actor_user_id: str, task_id: int,
                    request_key: str, text: str) -> dict:
    """A shared-room task writes to its principal's side room.

    Queued at once: the text reaches the principal alone, which is the audience
    of the task's own private reach, so there is nothing to approve. Refused
    from anywhere but a shared room the principal is in, since elsewhere the
    task's own answer already reaches only them.
    """
    _validate_input(request_key, text)
    _owned_running_task(conn, actor_user_id=actor_user_id, task_id=task_id)
    replay = _replay(conn, actor_user_id=actor_user_id, task_id=task_id,
                     request_key=request_key, kind="side_whisper", text=text)
    if replay is not None:
        return replay
    task = db.get_task(conn, task_id)
    with write_transaction(conn):
        parent = canonical_token(conn, task.conversation_token)
        room = db.get_room(conn, parent) if parent else None
        if (room is None or room.side_of or room.archived
                or not db.is_room_member(conn, parent, actor_user_id)
                or not is_shared_room(conn, parent, is_group_chat=task.is_group_chat)):
            raise RequestError("not_a_shared_room")
        try:
            side = db.ensure_side_room(conn, parent, actor_user_id)
        except ValueError:
            raise RequestError("side_room_unavailable") from None
        row = _store_request(
            conn, actor_user_id=actor_user_id, task_id=task_id, request_key=request_key,
            kind="side_whisper", recipient_user_id=actor_user_id, text=text,
            service_body=text, template_body=None, provider="room",
            binding_fingerprint=_fingerprint(side.token, parent),
            destination={"kind": "side_room", "room_token": side.token, "parent": parent},
        )
        return _question_response(conn, row)


SIDE_ANSWER_REFERENCE = "side-answer:"


def queue_side_answer(conn, config, *, actor_user_id: str, task_id: int) -> dict:
    """Ask a shared-room task's question again, privately, in the side room.

    For a question the room withholds the scope to answer (multiplayer D4 item
    1). What goes to the side room is the principal's own turn, as stored with
    the task, never text the model wrote: the side-room task runs at the
    principal's full reach, so a question composed by a model that has been
    reading guests' words would be an injection route to that reach. It is
    recorded as the principal's turn in their side room and runs there as an
    ordinary private task (source ``web``), which is what the principal would
    have got by asking there, and its answer is pinned to the side room like
    every side-room task's.

    Not the deferred-subtask op: a subtask is pinned to its parent's
    conversation, so it would run in the same shared room at the same
    restricted reach, and it is admin-only and rate-limited besides.

    Refused for a guest's turn (the question is not the host's), for anything
    but the principal's own turn in a shared room they are in, and from a side
    room. Idempotent per task: a second call returns the first side-room task.
    """
    task = _owned_running_task(conn, actor_user_id=actor_user_id, task_id=task_id)
    if getattr(task, "guest_participant_id", None) is not None:
        raise RequestError("guest_turn")
    if (task.source_type not in _SPEAKER_SURFACES or task.parent_task_id
            or task.command or task.skill or task.scheduled_job_id):
        raise RequestError("unsupported_origin")
    reference = f"{SIDE_ANSWER_REFERENCE}{task.id}"
    with write_transaction(conn):
        existing = conn.execute(
            "SELECT task_id FROM messages WHERE delivery_reference = ?", (reference,),
        ).fetchone()
        if existing is not None:
            if existing["task_id"] is None:
                raise RequestError("request_conflict")
            return {"status": "queued", "task_id": int(existing["task_id"])}
        parent = canonical_token(conn, task.conversation_token)
        room = db.get_room(conn, parent) if parent else None
        if (room is None or room.side_of or room.archived
                or not db.is_room_member(conn, parent, actor_user_id)
                or not is_shared_room(conn, parent, is_group_chat=task.is_group_chat)):
            raise RequestError("not_a_shared_room")
        try:
            side = db.ensure_side_room(conn, parent, actor_user_id)
        except ValueError:
            raise RequestError("side_room_unavailable") from None
        new_id = db.create_task(
            conn, prompt=task.prompt, user_id=actor_user_id, source_type="web",
            conversation_token=side.token, attachments=task.attachments or None,
            model=task.model, effort=task.effort, brain=task.brain,
            model_namespace=task.model_namespace,
        )
        db.add_message(
            conn, side.token, role="user", body=task.prompt, origin_surface="web",
            task_id=new_id, author_user_id=actor_user_id, delivery_reference=reference,
        )
    return {"status": "queued", "task_id": new_id}


def side_answer_parent(conn, task) -> str | None:
    """The parent room of a side-room answer task, or None for any other task.

    Its answer goes to the side room like any side-room task's; the parent is
    what `push_to_talk_view` needs to show it in the principal's Talk view as
    well, since the question was asked where they read on Talk.
    """
    row = conn.execute(
        "SELECT 1 FROM messages WHERE task_id = ? AND delivery_reference LIKE ?",
        (task.id, SIDE_ANSWER_REFERENCE + "%"),
    ).fetchone()
    if row is None:
        return None
    side = task_side_room(conn, task)
    return side.side_of if side is not None else None


def _post_destination(conn, parent_token: str, user_id: str) -> dict:
    """The parent a post goes to, re-resolved the same way at hold and delivery."""
    from .relay_destinations import destination_fingerprint

    room = db.get_room(conn, parent_token)
    if (room is None or room.archived or room.side_of
            or not db.is_room_member(conn, parent_token, user_id)):
        raise RequestError("parent_unavailable")
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


def hold_room_post(conn, config, *, actor_user_id: str, task_id: int,
                   request_key: str, text: str) -> dict:
    """A side-room task asks to post ``text`` into the parent, as the bot.

    Held for the member's approval of the exact preview, the relay's rule: the
    preview is stored with its digest, the task parks on it, and only an
    approval of that digest releases the post. The one exception is ISSUE-565's
    clean turn, with the recipient test swapped for "the text is the member's
    own words, verbatim in their prompt".
    """
    from . import message_relays

    _validate_input(request_key, text)
    task = _owned_running_task(conn, actor_user_id=actor_user_id, task_id=task_id)
    replay = _replay(conn, actor_user_id=actor_user_id, task_id=task_id,
                     request_key=request_key, kind="room_post", text=text)
    if replay is not None:
        return replay
    if task.parent_task_id or task.command or task.skill or task.scheduled_job_id:
        raise RequestError("unsupported_origin")
    side = task_side_room(conn, task)
    if side is None:
        raise RequestError("not_a_side_room")
    origin = message_relays.private_origin(conn, config, actor_user_id=actor_user_id,
                                           surface=task.source_type,
                                           conversation_token=task.conversation_token)
    with write_transaction(conn):
        message_relays.validate_origin(conn, config, actor_user_id=actor_user_id, origin=origin)
        destination = _post_destination(conn, side.side_of, actor_user_id)
        if destination["talk_ref"]:
            from .transport.talk import TalkTransport
            if len(text) > TalkTransport.capabilities.max_message_length:
                raise RequestError("invalid_rendering")
        from .confirmations import flatten
        bot = flatten(getattr(config, "bot_name", "") or "") or "the assistant"
        preview = (f"Post this in {destination['label']} as {bot}?\n"
                   "Only the message below is posted, exactly as written. "
                   "Sending must start within 10 minutes of approval.\n\n"
                   f"Message:\n{text}")
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
            if row["queue_deadline"] is None or row["queue_deadline"] <= db.sql_datetime_now():
                raise RequestError("queue_expired")
            user = row["requester_user_id"]
            body = row["service_body"]
            if user not in config.users or row["recipient_user_id"] != user:
                raise RequestError("recipient_unavailable")
            if not body or text_hash(body) != row["service_hash"]:
                raise RequestError("invalid_rendering")
            destination = json.loads(row["destination"] or "{}")
            talk_ref = None
            if row["kind"] == "room_post":
                if (not row["approved_at"] or row["approved_digest"] != row["preview_digest"]
                        or text_hash(row["preview"] or "") != row["approved_digest"]):
                    raise RequestError("request_unavailable")
                current = _post_destination(conn, destination.get("room_token") or "", user)
                if current["fingerprint"] != row["binding_fingerprint"]:
                    raise RequestError("destination_changed")
                target, talk_ref, parent = current["room_token"], current["talk_ref"], current["room_token"]
                whatsapp_group = bool(current.get("whatsapp_ref"))
                email_thread = bool(current.get("email_ref"))
                reference = "room-post:" + request_id
            else:
                parent = destination.get("parent") or ""
                side = db.get_side_room(conn, parent, user)
                if (side is None or side.token != destination.get("room_token")
                        or _fingerprint(side.token, parent) != row["binding_fingerprint"]
                        or db.list_room_members(conn, side.token) != [user]):
                    raise RequestError("destination_changed")
                if not db.is_room_member(conn, parent, user):
                    raise RequestError("parent_unavailable")
                target = side.token
                reference = "room-whisper:" + request_id
            message_id = db.add_message(conn, target, role="system", body=body,
                                        origin_surface="web", delivery_reference=reference)
            conn.execute("UPDATE whatsapp_skill_requests SET state='sending',updated_at=datetime('now') "
                         "WHERE id=?", (request_id,))
            return {"request_id": request_id, "kind": row["kind"], "user_id": user, "body": body,
                    "message_id": message_id, "talk_ref": talk_ref, "parent": parent,
                    "reference": reference, "whatsapp_group": whatsapp_group,
                    "email_thread": email_thread, "task_id": row["origin_task_id"]}


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
    from .notification_resolvers import task_alert
    from .notification_store import deliver_pending

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
                what = "Room post" if row["kind"] == "room_post" else "Side-room note"
                notice = task_alert.write(
                    conn, row["requester_user_id"], dedup_key=f"side-room-request:{request_id}",
                    title=f"{what} not delivered",
                    body=f"{what} {request_id} was not delivered ({reason}).",
                    params={"request_id": request_id, "status": state},
                )
    if notice is not None:
        deliver_pending(config, [notice])


async def deliver_request(config, row) -> None:
    """Deliver one queued `side_whisper` or `room_post`, or recover a stale claim."""
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
            from .transport.talk import TalkTransport
            try:
                talk_id = await TalkTransport(config).deliver(
                    claim["talk_ref"], claim["body"], reference_id=claim["reference"])
            except Exception as exc:
                logger.warning("room post %s: Talk post failed: %s", claim["request_id"], exc)
        if claim["whatsapp_group"]:
            # The group itself (multiplayer D6). Like the Talk half, a failed
            # send does not fail the post: the canonical row is the post, and
            # the ledger row and its alert say what happened to the group.
            from .transport.whatsapp.outbound import deliver_whatsapp
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
            from .transport.email.outbound import deliver_thread_post
            try:
                await deliver_thread_post(
                    config, task_id=int(claim["task_id"]), room_token=claim["parent"],
                    body=claim["body"])
            except Exception as exc:
                logger.warning("room post %s: email reply-all failed: %s", claim["request_id"], exc)
    else:
        talk_id = await push_to_talk_view(
            config, user_id=claim["user_id"], parent_token=claim["parent"],
            body=claim["body"], reference_id=claim["reference"])
        await push_to_whatsapp_view(
            config, user_id=claim["user_id"], parent_token=claim["parent"],
            body=claim["body"], reference_id=claim["reference"])
        await push_to_email_view(
            config, user_id=claim["user_id"], parent_token=claim["parent"],
            body=claim["body"], reference_id=claim["reference"])
    await asyncio.to_thread(_settle, config, claim, talk_id)
