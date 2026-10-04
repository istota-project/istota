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
email a member's ``on`` is their agreement as a vetoer and no more, and the
inbound poller hears a mail-borne ``on`` at all only from a sender the
receiving MTA authenticated; the member switches the room back on from the web
view. An email veto is not answered on the thread, since any mail there is the
outbound gate's to hold; its notice is the room row the web view shows.

Pending work in the room is cancelled at the veto and a running task's answer
is dropped when it finishes (`task_room_vetoed`); progress it posted while
running stays.

Replies reach a room where the command was heard: Talk and WhatsApp post them
from the process that read the command. The web app cannot reach a WhatsApp
group (the bridge is the scheduler's), so it queues them (`queue_notice`).
The announcement: the first time a guest is in a room with a host, the bot
says once what it is, who it works for and how to switch it off.
`drain_room_notices`, a scheduler gate, posts both into Talk and WhatsApp.

An email thread room is never announced (ISSUE-605). Mail from the bot's
address is from the bot, and a thread gains people on later messages who would
never see a notice sent once. What it carries instead, only when the operator
turns `[email] thread_disclosure_footer` on, is a plain line on every mail the
bot sends into the thread (`with_email_footer`), so nothing tracks whether the
thread was told.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from dataclasses import dataclass

from istota import db
from istota.rooms import policy as room_policy
from istota.transport import participants
from istota.transport._types import ParticipantRef

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
    from istota.confirmations import flatten

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


def switched_off(conn, room_token: str | None) -> dict | None:
    """When a room was switched off and by whom, or None while it is on.

    ``vetoers`` are the rows of ``room_vetoes`` in the order they switched it
    off, each with its user id or the guest's display name and whether they
    have since agreed. Empty for a WhatsApp group the bot was removed from,
    since the roster frame does not say who removed it.
    """
    if not room_token:
        return None
    row = conn.execute(
        "SELECT vetoed_at FROM room_policy WHERE room_token = ?", (room_token,),
    ).fetchone()
    if row is None or row["vetoed_at"] is None:
        return None
    vetoers = [
        {
            "user_id": r["person"][2:] if r["person"].startswith("u:") else None,
            "display_name": r["display_name"],
            "agreed": r["agreed_at"] is not None,
        }
        for r in conn.execute(
            "SELECT v.person, v.agreed_at, p.display_name FROM room_vetoes v "
            "LEFT JOIN room_participants p ON p.id = v.participant_id "
            "WHERE v.room_token = ? ORDER BY v.vetoed_at, v.rowid",
            (room_token,),
        )
    ]
    return {"at": row["vetoed_at"], "vetoers": vetoers}


def is_vetoed_ref(conn, surface: str, surface_ref: str) -> bool:
    """`is_vetoed` for a surface's own ref (a Talk token, a group JID)."""
    token = db.resolve_room_token(conn, surface, surface_ref) or surface_ref
    return is_vetoed(conn, token)


def task_room_vetoed(conn, task) -> bool:
    """Whether the room a task's answer would land in has been switched off.

    A member's private room is never off; a shared room it is linked to being
    off does not silence it.
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


def way_back(config) -> str:
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

    None when it is not a veto here: no such room, a bot author,
    or a room only one human reads (D8 is about the people who did not choose
    the bot, and a private room has none). The caller then treats the text as
    it would any other. Writes the author's participant row, as recording
    their turn would have, so the vetoer is on record as present.
    """
    room = db.get_room(conn, room_token)
    if room is None or verb not in (OFF, ON):
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
            f"{_bot(config)} is already switched off in this room. {way_back(config)}",
            reference,
        )
    text = (f"{_bot(config)} is now switched off in this room and records "
            f"nothing here. {way_back(config)}")
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
    """A guest is in the room, it has a host, and nobody has been told yet.

    Never for an email thread room (ISSUE-605): its disclosure, when the
    operator wants one, is the footer on every mail.
    """
    room = db.get_room(conn, room_token)
    if room is None or room.archived:
        return False
    if db.get_room_binding(conn, room_token, "email") is not None:
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


ANNOUNCEMENT_FILE = "room-announcement.md"
_ANNOUNCEMENT_OPENING_MAX = 800

# In the default persona's voice (`config/persona.md`). An operator with a
# different persona overrides it with `config/room-announcement.md`.
DEFAULT_ANNOUNCEMENT_OPENING = (
    "Hello. I'm {BOT_NAME}, this room's resident octopus. I work for the members "
    "here: when one of you asks me something, I act for you, with your own access "
    "and no one else's. I read along so I can help when asked."
)


def _announcement_opening(config) -> str:
    """The operator's opening from `config/`, else the default.

    Read from the deployment's config directory and never from a user's
    workspace: a user's files are writable from their sandbox, and this is read
    by everyone in a room that user may not host.
    """
    skills_dir = getattr(config, "skills_dir", None)
    if skills_dir is not None:
        try:
            text = (Path(skills_dir).parent / ANNOUNCEMENT_FILE).read_text()
        except (OSError, UnicodeDecodeError, ValueError):
            text = ""
        if text.strip():
            return text
    return DEFAULT_ANNOUNCEMENT_OPENING


def _one_line(text: str, limit: int) -> str:
    """Whitespace collapsed, every line break Python knows included, then capped
    on a word boundary. Markup is kept: it is the operator's own text."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return f"{cut}…"


def _guest_sentence(conn, room_token: str, who: str) -> str:
    """What happens to a guest's message, from the room's `guest_reply`."""
    policy = room_policy.get_policy(conn, room_token)
    if policy is not None:
        mode = policy.guest_reply
    else:
        room = db.get_room(conn, room_token)
        mode = room_policy.default_guest_reply(room.origin if room else None)
    if mode == room_policy.OFF:
        return "Messages from anyone else I record but don't answer."
    if mode == room_policy.DIRECT:
        return (f"Anyone else I answer on {who}'s behalf, since it's their room, "
                "and for them I'll do nothing beyond a reply.")
    return (f"Anyone else I answer on {who}'s behalf, since it's their room: {who} "
            "sees each of those replies before I post it, and I'll do nothing "
            "beyond it.")


def _announcement_closing(conn, config, room_token: str) -> str:
    """The off switch and the way back. Fixed, so an opening cannot drop it."""
    word = command_word(config)
    how = f"send `!{word} off`"
    if db.get_room_binding(conn, room_token, "whatsapp") is not None:
        how += " (or remove my number from the group)"
    return (
        f"If you'd rather I didn't, {how} and I'll record nothing here until a "
        f"member sends `!{word} on` and everyone who switched me off agrees."
    )


def announcement_text(conn, config, room_token: str) -> str:
    """The announcement: an overridable opening, then two fixed sentences.

    The opening says who the bot is; the guest sentence and the closing are
    facts about the room (its `guest_reply`, its off switch) and always render.
    The whole is one line, so an opening cannot forge a closing of its own.
    """
    host = _host(conn, room_token)
    user = getattr(config, "users", {}).get(host) if host else None
    from istota.confirmations import flatten

    who = flatten(getattr(user, "display_name", None) or host or "") or "this room's host"
    opening = (
        _announcement_opening(config)
        .replace("{BOT_NAME}", _bot(config))
        .replace("{BOT_DIR}", getattr(config, "bot_dir_name", "") or "")
        .replace("{HOST}", who)
    )
    opening = _one_line(opening, _ANNOUNCEMENT_OPENING_MAX)
    guest = _guest_sentence(conn, room_token, who)
    return f"{opening} {guest} {_announcement_closing(conn, config, room_token)}"


def _claim_announcements(config, limit: int) -> list[dict]:
    from istota.relay.requests import write_transaction

    claims: list[dict] = []
    with db.get_db(config.db_path) as conn:
        due = [row[0] for row in conn.execute(
            "SELECT DISTINCT p.room_token FROM room_participants p "
            "JOIN rooms r ON r.token = p.room_token "
            "LEFT JOIN room_policy rp ON rp.room_token = p.room_token "
            "WHERE p.kind = 'guest' AND p.left_at IS NULL AND NOT r.archived "
            "AND (rp.room_token IS NULL OR "
            "(rp.announced_at IS NULL AND rp.vetoed_at IS NULL "
            " AND rp.host_user_id IS NOT NULL)) "
            "AND NOT EXISTS (SELECT 1 FROM room_bindings b WHERE "
            "b.room_token = p.room_token AND b.surface = 'email') "
            "ORDER BY p.room_token LIMIT ?",
            (limit,),
        ).fetchall()]
        # A room that cannot be announced yet (no host present) is skipped on
        # a read, so it costs no write lock each tick.
        for token in [t for t in due if needs_announcement(conn, t)]:
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
        from istota.transport.talk import TalkTransport
        try:
            await TalkTransport(config).deliver(talk_ref, text, reference_id=reference)
        except Exception as exc:  # noqa: BLE001 — the row is the notice
            logger.warning("room %s: Talk notice failed: %s", room_token, exc)
    if whatsapp:
        from istota.transport.whatsapp.outbound import deliver_whatsapp
        try:
            await deliver_whatsapp(config, logical_key=reference, user_id=owner,
                                   text=text, group_room=room_token)
        except Exception as exc:  # noqa: BLE001 — the row is the notice
            logger.warning("room %s: WhatsApp notice failed: %s", room_token, exc)


def queue_notice(conn, room_token: str, outcome: VetoOutcome) -> None:
    """Owe the room's Talk conversation and WhatsApp group a veto reply.

    For a process that cannot post there itself: the web app holds no
    WhatsApp bridge (it is the scheduler's), and a send from it would settle
    the ledger row `failed` for good. `drain_room_notices` posts it.
    """
    if (db.get_room_binding(conn, room_token, "talk") is None
            and db.get_room_binding(conn, room_token, "whatsapp") is None):
        return
    conn.execute(
        "INSERT OR IGNORE INTO room_notices (room_token, body, reference) VALUES (?, ?, ?)",
        (room_token, outcome.text, outcome.reference),
    )


def _claim_notices(config, limit: int) -> list[dict]:
    from istota.relay.requests import write_transaction

    claims: list[dict] = []
    with db.get_db(config.db_path) as conn:
        with write_transaction(conn):
            rows = conn.execute(
                "SELECT id, room_token, body, reference FROM room_notices "
                "WHERE posted_at IS NULL ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE room_notices SET posted_at = datetime('now') WHERE id = ?",
                    (row["id"],),
                )
                room = db.get_room(conn, row["room_token"])
                if room is None:
                    continue
                talk = db.get_room_binding(conn, room.token, "talk")
                claims.append({
                    "token": room.token, "text": row["body"],
                    "reference": row["reference"],
                    "talk_ref": talk.surface_ref if talk else None,
                    "whatsapp": db.get_room_binding(conn, room.token, "whatsapp")
                    is not None,
                    "owner": _host(conn, room.token) or room.user_id,
                })
    return claims


async def drain_room_notices(config, *, limit: int = 20) -> int:
    """Post queued veto replies, then announce the bot wherever it is owed.

    Both are claimed before they are posted, so each goes out at most once.
    Returns how many were posted.
    """
    import asyncio

    claims = await asyncio.to_thread(_claim_notices, config, limit)
    claims += await asyncio.to_thread(_claim_announcements, config, limit)
    for claim in claims:
        await push_to_room(config, room_token=claim["token"], text=claim["text"],
                           reference=claim["reference"], talk_ref=claim["talk_ref"],
                           whatsapp=claim["whatsapp"], owner=claim["owner"])
    return len(claims)


def _host_name(conn, config, room_token: str) -> str:
    from istota.confirmations import flatten

    host = _host(conn, room_token)
    user = getattr(config, "users", {}).get(host) if host else None
    return flatten(getattr(user, "display_name", None) or host or "") or "this thread's host"


def email_footer(conn, config, room_token: str | None) -> str | None:
    """The disclosure line for a mail into an email thread room, or None.

    None unless the operator turned `[email] thread_disclosure_footer` on and
    the room is bound to an email thread. No persona voice: a fact about who
    wrote the mail and how to stop it.
    """
    email_config = getattr(config, "email", None)
    if not room_token or not getattr(email_config, "thread_disclosure_footer", False):
        return None
    binding = db.get_room_binding(conn, room_token, "email")
    if binding is None:
        return None
    from istota.transport.email.private_room import is_private_email_ref

    room = db.get_room(conn, room_token)
    # The user's private email room: its mail goes to the user alone.
    if room is not None and is_private_email_ref(binding.surface_ref, room.user_id):
        return None
    return (
        f"Written by {_bot(config)}, an AI assistant, for "
        f"{_host_name(conn, config, room_token)}. {_footer_switch(config)}"
    )


def _footer_switch(config) -> str:
    return (f"To stop it replying on this thread, reply with "
            f"`!{command_word(config)} off` as the first line.")


def with_email_footer(conn, config, room_token: str | None, body: str) -> str:
    """``body`` with the disclosure footer after it, when one is due.

    Recognised by its fixed second sentence at the end of the body, so a held
    proposal whose preview already carries it is not footed twice when it is
    sent, even if the host's display name changed in between. The end, not
    anywhere: a sentence a guest steered into the answer must not stand in for
    the real footer.
    """
    footer = email_footer(conn, config, room_token)
    if not footer or body.rstrip().endswith(_footer_switch(config)):
        return body
    return f"{body}\n\n--\n{footer}"


__all__ = [
    "VetoOutcome",
    "announcement_text",
    "apply",
    "command_word",
    "drain_room_notices",
    "is_vetoed",
    "is_vetoed_ref",
    "email_footer",
    "needs_announcement",
    "parse_command",
    "push_to_room",
    "queue_notice",
    "switch_off_by_removal",
    "task_room_vetoed",
    "with_email_footer",
]
