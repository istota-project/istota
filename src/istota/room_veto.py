"""The participant veto and the bot's announcement (multiplayer Stage 20).

D8: in a room more than one human reads, anyone, guest included, switches the
bot off with ``!<bot name> off`` — the bot's own name, so a guest who has never
heard of istota can guess it — or, on WhatsApp, by removing the bot's number.
D12: while a room is off nothing is recorded. Every surface asks
`is_vetoed` before it writes, and `transport.ingest.record_inbound` and
`classify_ahead` refuse as the backstop, so there is no transcript row, no
classifier call and no task. The room comes back on only when a member sends
``!<bot name> on`` **and** everyone who switched it off has agreed with their
own ``!<bot name> on`` or has left the room. Agreement is that act and nothing
else: a vetoer who goes quiet keeps the room off.

The vetoers are a set (`room_vetoes`), one row per person as
`db.audience_persons` spells them, so two people switching the bot off are two
objections and one agreeing does not overrule the other. Removal from a
WhatsApp group switches the room off with no vetoer row, since the roster frame
does not say who removed the bot; a member's ``on`` then suffices.

A member's ``on`` counts as a member's only when the surface authenticated who
sent it (`apply`'s ``authenticated``). A mail's From proves nothing, so on
email a member's ``on`` is their agreement as a vetoer and no more; the member
switches the room back on from the web view.

The announcement: the first time a guest is in a room with a host, the bot
says once what it is, who it works for and how to switch it off. Talk, web and
WhatsApp get it from `drain_announcements`; an email thread room cannot be
posted into except by a mail the outbound gate governs, so there it rides the
bot's first reply-all on the thread (`with_email_notice`).
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from . import db, room_policy
from .transport import participants
from .transport._types import ParticipantRef

logger = logging.getLogger(__name__)

OFF, ON = "off", "on"

ANNOUNCE_REFERENCE = "room-announce:"
NOTICE_REFERENCE = "room-veto:"


@dataclass(frozen=True)
class VetoOutcome:
    """What one ``!<bot> off|on`` did.

    ``state``: ``off`` (switched off now), ``already_off``, ``on`` (switched
    back on), ``waiting`` (an ``on`` recorded, the room still off) or
    ``not_off``. ``text`` is the reply the surface posts into the room;
    ``reference`` keys that post (a Talk reference id, a WhatsApp ledger key).
    """

    state: str
    text: str
    reference: str


def parse_command(text: object, bot_name: object) -> str | None:
    """``off`` or ``on`` for ``!<bot name> off|on``, else None.

    The whole message, case-insensitive, with a trailing full stop or
    exclamation mark allowed; a name with spaces may be typed with or
    without them. Anything more is not the veto, so a sentence that happens to
    contain it is an ordinary turn.
    """
    if not isinstance(text, str) or not isinstance(bot_name, str):
        return None
    words = bot_name.split()
    if not words:
        return None
    name = r"\s*".join(re.escape(word) for word in words)
    match = re.fullmatch(
        rf"\s*!\s*{name}\s+(off|on)\s*[.!]?\s*", text, flags=re.IGNORECASE,
    )
    return match.group(1).lower() if match else None


def command_word(config) -> str:
    """The name as typed after ``!``: the bot's name, lowercased, no spaces."""
    return "".join((getattr(config, "bot_name", "") or "").split()).lower() or "bot"


def _bot(config) -> str:
    from .confirmations import flatten

    return flatten(getattr(config, "bot_name", "") or "") or "The assistant"


def _person(ref: ParticipantRef) -> str:
    return f"u:{ref.user_id}" if ref.user_id else f"{ref.surface}:{ref.surface_ref}"


def is_vetoed(conn, room_token: str | None) -> bool:
    """Whether the room is switched off. ``room_token`` is the canonical token."""
    if not room_token:
        return False
    row = conn.execute(
        "SELECT vetoed_at FROM room_policy WHERE room_token = ?", (room_token,),
    ).fetchone()
    return row is not None and row["vetoed_at"] is not None


def is_vetoed_ref(conn, surface: str, surface_ref: str) -> bool:
    """`is_vetoed` for a surface's own ref (a Talk token, a group JID)."""
    token = db.resolve_room_token(conn, surface, surface_ref) or surface_ref
    return is_vetoed(conn, token)


def task_room_vetoed(conn, task) -> bool:
    """Whether the room a task's answer would land in has been switched off.

    A side room is the member's own and is never off; its parent being off
    does not silence it.
    """
    token = getattr(task, "conversation_token", None)
    if not token:
        return False
    if db.get_room(conn, token) is None:
        token = db.find_room_token_by_ref(conn, token)
    return is_vetoed(conn, token)


def _write_notice(conn, room_token: str, text: str, reference: str) -> None:
    db.add_message(conn, room_token, role="system", body=text,
                   origin_surface="web", delivery_reference=reference)


def _cancel_queued(conn, room_token: str) -> None:
    """Work queued in the room before the veto would answer into it after."""
    refs = db.room_ref_tokens(conn, room_token)
    placeholders = ", ".join("?" for _ in refs)
    for row in conn.execute(
        f"SELECT id FROM tasks WHERE status = 'pending' "
        f"AND conversation_token IN ({placeholders})",
        refs,
    ).fetchall():
        db.cancel_task(conn, row["id"])
        db.log_task(conn, row["id"], "info", "Cancelled: the room was switched off")


def _way_back(config) -> str:
    word = command_word(config)
    return (
        f"To switch it back on, a member of the room sends `!{word} on`, and "
        f"everyone who switched it off agrees by sending `!{word} on` too."
    )


def apply(
    conn, config, *, room_token: str, author: ParticipantRef, verb: str,
    authenticated: bool = True,
) -> VetoOutcome | None:
    """Apply ``!<bot> off|on`` from ``author``, on the caller's connection.

    None when it is not a veto here: no such room, a side room, a bot author,
    or a room only one human reads (D8 is about the people who did not choose
    the bot, and a private room has none). The caller then treats the text as
    it would any other. Writes the author's participant row, as recording
    their turn would have, so the vetoer is on record as present.
    """
    room = db.get_room(conn, room_token)
    if room is None or room.side_of or verb not in (OFF, ON):
        return None
    kind = participants.classify(conn, config, room_token, author)
    if kind == participants.AGENT:
        return None
    # A room that is off stays answerable after its vetoers leave, which is
    # exactly when it may have become private.
    if (kind == participants.PRINCIPAL and not is_vetoed(conn, room_token)
            and room_policy.audience_class(conn, room_token) == room_policy.PRIVATE):
        return None
    participant_id = None
    if author.surface_ref:
        participant_id = db.upsert_room_participant(
            conn, room_token=room_token, surface=author.surface,
            surface_ref=author.surface_ref, kind=kind, user_id=author.user_id,
            display_name=author.display_name,
        )
    policy = room_policy.ensure_policy(conn, room_token)
    if policy is None:
        return None
    reference = f"{NOTICE_REFERENCE}{uuid.uuid4().hex}"
    if verb == OFF:
        return _switch_off(conn, config, policy, _person(author), participant_id,
                           reference)
    return _switch_on(conn, config, policy, _person(author),
                      member=kind == participants.PRINCIPAL and authenticated,
                      user_id=author.user_id, reference=reference)


def _switch_off(conn, config, policy, person: str, participant_id, reference: str):
    token = policy.room_token
    conn.execute(
        "INSERT INTO room_vetoes (room_token, person, participant_id) VALUES (?, ?, ?) "
        "ON CONFLICT (room_token, person) DO UPDATE SET agreed_at = NULL, "
        "participant_id = excluded.participant_id",
        (token, person, participant_id),
    )
    conn.execute(
        "UPDATE room_policy SET vetoed_at = COALESCE(vetoed_at, datetime('now')), "
        "vetoed_by = COALESCE(vetoed_by, ?), veto_on_by = NULL WHERE room_token = ?",
        (participant_id, token),
    )
    _cancel_queued(conn, token)
    if policy.vetoed_at is not None:
        return VetoOutcome(
            "already_off",
            f"{_bot(config)} is already switched off in this room. {_way_back(config)}",
            reference,
        )
    text = (f"{_bot(config)} is now switched off in this room and records "
            f"nothing here. {_way_back(config)}")
    _write_notice(conn, token, text, reference)
    logger.info("room %s switched off", token)
    return VetoOutcome(OFF, text, reference)


def _switch_on(conn, config, policy, person: str, *, member: bool,
               user_id: str | None, reference: str):
    token = policy.room_token
    bot = _bot(config)
    if policy.vetoed_at is None:
        return VetoOutcome("not_off", f"{bot} is not switched off in this room.",
                           reference)
    conn.execute(
        "UPDATE room_vetoes SET agreed_at = datetime('now') "
        "WHERE room_token = ? AND person = ?",
        (token, person),
    )
    if member:
        conn.execute("UPDATE room_policy SET veto_on_by = ? WHERE room_token = ?",
                     (user_id, token))
    asked_by = room_policy.get_policy(conn, token).veto_on_by
    present = db.audience_persons(conn, token)
    outstanding = [
        row["person"] for row in conn.execute(
            "SELECT person FROM room_vetoes WHERE room_token = ? AND agreed_at IS NULL",
            (token,),
        ).fetchall()
        if row["person"] in present
    ]
    if asked_by and not outstanding:
        conn.execute("DELETE FROM room_vetoes WHERE room_token = ?", (token,))
        conn.execute(
            "UPDATE room_policy SET vetoed_at = NULL, vetoed_by = NULL, "
            "veto_on_by = NULL WHERE room_token = ?",
            (token,),
        )
        text = f"{bot} is back on in this room."
        _write_notice(conn, token, text, reference)
        logger.info("room %s switched back on", token)
        return VetoOutcome(ON, text, reference)
    word = command_word(config)
    missing = []
    if not asked_by:
        missing.append(f"a member of the room sends `!{word} on`")
    if outstanding:
        missing.append(f"everyone who switched it off agrees by sending `!{word} on`")
    return VetoOutcome(
        "waiting", f"Noted. {bot} stays off until {' and '.join(missing)}.", reference,
    )


def switch_off_by_removal(conn, room_token: str) -> None:
    """The bot was removed from the room's WhatsApp group (D8).

    The roster frame does not name who removed it, so there is no vetoer row
    and a member's ``on`` brings the room back once the bot is re-added.
    """
    if room_policy.ensure_policy(conn, room_token) is None:
        return
    conn.execute(
        "UPDATE room_policy SET vetoed_at = COALESCE(vetoed_at, datetime('now')), "
        "veto_on_by = NULL WHERE room_token = ?",
        (room_token,),
    )
    _cancel_queued(conn, room_token)


# ---------------------------------------------------------------------------
# The announcement
# ---------------------------------------------------------------------------


def _host(conn, room_token: str) -> str | None:
    return room_policy.room_readers(conn, room_token).host


def needs_announcement(conn, room_token: str) -> bool:
    """A guest is in the room, it has a host, and nobody has been told yet."""
    room = db.get_room(conn, room_token)
    if room is None or room.archived or room.side_of:
        return False
    policy = room_policy.get_policy(conn, room_token)
    if policy is not None and (policy.announced_at or policy.vetoed_at):
        return False
    guest = conn.execute(
        "SELECT 1 FROM room_participants WHERE room_token = ? AND kind = 'guest' "
        "AND left_at IS NULL LIMIT 1",
        (room_token,),
    ).fetchone()
    return guest is not None and _host(conn, room_token) is not None


def announcement_text(conn, config, room_token: str) -> str:
    """The fixed announcement, from the room's tables and the deployment's names."""
    host = _host(conn, room_token)
    user = getattr(config, "users", {}).get(host) if host else None
    from .confirmations import flatten

    who = flatten(getattr(user, "display_name", None) or host or "") or "this room's host"
    word = command_word(config)
    how = f"sending `!{word} off`"
    if db.get_room_binding(conn, room_token, "whatsapp") is not None:
        how += " or removing my number from the group"
    elif db.get_room_binding(conn, room_token, "email") is not None:
        how = f"replying with `!{word} off` as the first line"
    return (
        f"Hello, I'm {_bot(config)}, an AI assistant working for {who}. I read this "
        "conversation so I can help when I'm asked. Anyone here can switch me off "
        f"for this room by {how}. I then record nothing here until a member "
        f"switches me back on with `!{word} on` and everyone who switched me off "
        "agrees."
    )


def _claim_announcements(config, limit: int) -> list[dict]:
    from .whatsapp_requests import write_transaction

    claims: list[dict] = []
    with db.get_db(config.db_path) as conn:
        due = [row[0] for row in conn.execute(
            "SELECT DISTINCT p.room_token FROM room_participants p "
            "JOIN rooms r ON r.token = p.room_token "
            "LEFT JOIN room_policy rp ON rp.room_token = p.room_token "
            "WHERE p.kind = 'guest' AND p.left_at IS NULL AND NOT r.archived "
            "AND r.side_of IS NULL AND (rp.room_token IS NULL OR "
            "(rp.announced_at IS NULL AND rp.vetoed_at IS NULL)) "
            "AND NOT EXISTS (SELECT 1 FROM room_bindings b WHERE "
            "b.room_token = p.room_token AND b.surface = 'email') LIMIT ?",
            (limit,),
        ).fetchall()]
        for token in due:
            with write_transaction(conn):
                if not needs_announcement(conn, token):
                    continue
                room_policy.ensure_policy(conn, token)
                claimed = conn.execute(
                    "UPDATE room_policy SET announced_at = datetime('now') "
                    "WHERE room_token = ? AND announced_at IS NULL",
                    (token,),
                ).rowcount
                if not claimed:
                    continue
                text = announcement_text(conn, config, token)
                reference = ANNOUNCE_REFERENCE + token
                _write_notice(conn, token, text, reference)
                talk = db.get_room_binding(conn, token, "talk")
                whatsapp = db.get_room_binding(conn, token, "whatsapp")
                claims.append({
                    "token": token, "text": text, "reference": reference,
                    "talk_ref": talk.surface_ref if talk else None,
                    "whatsapp": whatsapp is not None,
                    "owner": _host(conn, token) or db.get_room(conn, token).user_id,
                })
    return claims


async def push_to_room(config, *, room_token: str, text: str, reference: str,
                       talk_ref: str | None, whatsapp: bool, owner: str) -> None:
    """Post a notice into the room's Talk conversation and WhatsApp group.

    The canonical row is the caller's, already written. Each leg is
    best-effort: a failed post is logged, and the row stands for the room.
    """
    if talk_ref:
        from .transport.talk import TalkTransport
        try:
            await TalkTransport(config).deliver(talk_ref, text, reference_id=reference)
        except Exception as exc:  # noqa: BLE001 — the row is the notice
            logger.warning("room %s: Talk notice failed: %s", room_token, exc)
    if whatsapp:
        from .transport.whatsapp.outbound import deliver_whatsapp
        try:
            await deliver_whatsapp(config, logical_key=reference, user_id=owner,
                                   text=text, group_room=room_token)
        except Exception as exc:  # noqa: BLE001 — the row is the notice
            logger.warning("room %s: WhatsApp notice failed: %s", room_token, exc)


async def push_outcome(config, room_token: str, outcome: VetoOutcome, *,
                       talk: bool = True) -> None:
    """After the commit: put a veto reply into the room's external surfaces."""
    import asyncio

    def _targets():
        with db.get_db(config.db_path) as conn:
            binding = db.get_room_binding(conn, room_token, "talk")
            group = db.get_room_binding(conn, room_token, "whatsapp")
            room = db.get_room(conn, room_token)
            return (binding.surface_ref if binding and talk else None,
                    group is not None, room.user_id if room else "")

    talk_ref, whatsapp, owner = await asyncio.to_thread(_targets)
    await push_to_room(config, room_token=room_token, text=outcome.text,
                       reference=outcome.reference, talk_ref=talk_ref,
                       whatsapp=whatsapp, owner=owner)


async def drain_announcements(config, *, limit: int = 20) -> int:
    """Announce the bot in every room that is owed it; how many were."""
    import asyncio

    claims = await asyncio.to_thread(_claim_announcements, config, limit)
    for claim in claims:
        await push_to_room(config, room_token=claim["token"], text=claim["text"],
                           reference=claim["reference"], talk_ref=claim["talk_ref"],
                           whatsapp=claim["whatsapp"], owner=claim["owner"])
    return len(claims)


def _email_notice(conn, config, room_token: str) -> str | None:
    if not room_token or not needs_announcement(conn, room_token):
        return None
    return announcement_text(conn, config, room_token)


def with_email_notice(conn, config, room_token: str | None, body: str) -> str:
    """``body`` with the announcement after it, while the thread is owed it."""
    notice = _email_notice(conn, config, room_token) if room_token else None
    if not notice or notice in body:
        return body
    return f"{body}\n\n--\n{notice}"


def note_email_notice_sent(conn, config, room_token: str | None, body: str) -> None:
    """A mail carrying the announcement went out: the thread has been told."""
    if not room_token:
        return
    notice = _email_notice(conn, config, room_token)
    if notice and notice in body:
        room_policy.ensure_policy(conn, room_token)
        conn.execute(
            "UPDATE room_policy SET announced_at = datetime('now') "
            "WHERE room_token = ? AND announced_at IS NULL",
            (room_token,),
        )


__all__ = [
    "VetoOutcome",
    "announcement_text",
    "apply",
    "command_word",
    "drain_announcements",
    "is_vetoed",
    "is_vetoed_ref",
    "needs_announcement",
    "note_email_notice_sent",
    "parse_command",
    "push_outcome",
    "push_to_room",
    "switch_off_by_removal",
    "task_room_vetoed",
    "with_email_notice",
]
