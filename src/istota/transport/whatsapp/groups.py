"""WhatsApp groups as rooms (multiplayer D6). Baileys only.

A group the bot's number is in becomes a room: its JID is a room binding
(`surface='whatsapp'`, `surface_ref=<group jid>`), its members are the room's
participants, and its turns go through `record_inbound` like a Talk room's.
The surface still owns no rooms in general — every 1:1 WhatsApp chat stays
outside the room model — so a group turn is passed in as a `room_container`
and the room is registered here, never by `record_inbound`.

- **The roster** comes from the sidecar's `groupMetadata` read, sent when a
  group is first seen and again on every `group-participants.update`
  (`apply_roster`). The first roster a room is registered with is its
  founders, recorded as the epoch baseline; a later join always splits (D3),
  since WhatsApp has no history acknowledgment.
- **The host** is the istota user whose bound number added the bot, else the
  first principal on the roster. A group with no istota user in it is not
  registered: there is nobody a guest's turn could run as.
- **D14.** When the host leaves the group the room loses its host, is
  archived, and the bot leaves the group: the result carries the JID for the
  bridge to act on after the commit.
- **Identity** is `identity.group_member_user`, the Baileys resolution
  read-only. A sender WhatsApp shows only by LID is a guest (D1).
- **Addressing** (D5): the bot's JID in `mentionedJid` (the sidecar's
  answer), a quoted reply to a message the bot sent, or the bot's name as the
  first word.
- **Media** (ISSUE-646): an image or voice note reaches the task only on a
  member's turn the speech gate would answer. `media_recipient` asks that
  read-only before `stage_inbound_media` copies anything, so an unaddressed
  photo, or any file a guest sends, is never placed in an inbox (a guest's
  turn runs as the host, and the copy would land in the host's). Every turn
  with a file it did not open, and every unsupported message, is still
  recorded under a stand-in naming what was sent.

A group never sends the fixed direct-chat replies (`HELP`, `STOP`, the
unsupported-type and media-failed notices): those answer one person, and
everyone in the group would read them.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import TYPE_CHECKING

from istota import confirmations, db
from istota.rooms import policy as room_policy
from istota.rooms import veto as room_veto
from .. import participants
from .._types import ParticipantRef
from ..ingest import record_inbound
from . import identity as identity_rules
from . import jid_fingerprint, message_fingerprint
from . import media as media_rules
from ._types import (
    InboundWhatsAppEvent,
    WhatsAppGroupMember,
    WhatsAppGroupRoster,
    WhatsAppMention,
)

if TYPE_CHECKING:
    from ...config import Config
    from istota.rooms.speech_gate import GateDecision
    from .webhook import WhatsAppEventResult

logger = logging.getLogger(__name__)

SURFACE = "whatsapp"

_TEXT_TYPES = frozenset({"text"})
#: The types whose words the speech gate reads: a caption is a message.
_WORDED_TYPES = _TEXT_TYPES | frozenset(media_rules.MEDIA_KINDS)
#: Every type a group records as a turn; anything else is claimed and dropped.
_TURN_TYPES = _WORDED_TYPES | frozenset({"unsupported"})

#: What a mention of somebody the room cannot name becomes: neither a number
#: nor a LID.
UNNAMED_MENTION = "@member"


def group_room_token(group_jid: str) -> str:
    """The legacy canonical token, retained for migration tooling.

    A digest, never the JID: the token reaches task rows, log lines and the
    prompt header, and a group JID of the older `<creator>-<timestamp>` form
    carries its creator's phone number.
    """
    digest = hashlib.sha256(f"istota-whatsapp-group-v1\0{group_jid}".encode())
    return "whatsapp-group-" + digest.hexdigest()[:24]


def group_destination(conn, room_token: str | None) -> str:
    """The group JID a room's answers are sent to, or ``""``.

    Only a registered, unarchived room with a WhatsApp binding naming a group:
    this is what stands between the send path and an arbitrary JID.
    """
    if not room_token:
        return ""
    room = db.get_room(conn, room_token)
    if room is None or room.archived:
        return ""
    binding = db.get_room_binding(conn, room_token, SURFACE)
    if binding is None:
        return ""
    return identity_rules.normalize_group_jid(binding.surface_ref)


def addressed_to_bot(
    conn, config: "Config", text: str, *, mentions_bot: bool,
    reply_to_message_id: str | None,
) -> bool:
    """Whether a group turn speaks to the bot (D5)."""
    if mentions_bot:
        return True
    if quotes_bot(conn, reply_to_message_id):
        return True
    name = (getattr(config, "bot_name", "") or "").strip().casefold()
    if not name:
        return False
    spoken = text.lstrip().lstrip("@").casefold()
    if not spoken.startswith(name):
        return False
    rest = spoken[len(name):]
    return not rest or not rest[0].isalnum()


def quotes_bot(conn, reply_to_message_id: str | None) -> bool:
    """Whether a group turn quotes one of the bot's own sends."""
    return bool(reply_to_message_id) and conn.execute(
        "SELECT 1 FROM sent_whatsapp WHERE meta_message_id = ? LIMIT 1",
        (reply_to_message_id,),
    ).fetchone() is not None


def _mention_name(
    conn, config: "Config", room_token: str, mention: WhatsAppMention,
) -> str:
    """The name a mention renders as, read from this room's own roster only.

    Never from `identity.group_member_user`: a mention list is whatever the
    sender's client put in it, and resolving an arbitrary number through the
    bindings would let anyone in the group learn whether it belongs to a user
    of this installation, and that user's name.
    """
    if mention.bot:
        return (getattr(config, "bot_name", "") or "").strip()
    refs = [
        ref for ref in (
            identity_rules.normalize_jid(mention.jid),
            identity_rules.normalize_lid(mention.lid),
        ) if ref
    ]
    if not refs:
        return ""
    row = conn.execute(
        "SELECT user_id, display_name FROM room_participants "
        "WHERE room_token = ? AND surface = ? AND surface_ref IN "
        f"({', '.join('?' for _ in refs)}) "
        "ORDER BY (left_at IS NULL) DESC, id DESC LIMIT 1",
        (room_token, SURFACE, *refs),
    ).fetchone()
    if row is None:
        return ""
    user_id, display_name = row
    if user_id:
        user = config.users.get(user_id)
        return getattr(user, "display_name", "") or user_id
    return display_name or ""


def render_mentions(
    conn, config: "Config", room_token: str, text: str,
    mentions: tuple[WhatsAppMention, ...],
) -> str:
    """``text`` with each mention's ``@<id>`` token as a name (ISSUE-601).

    WhatsApp writes the mentioned account's LID or number into the body, so
    without this the room's transcript and the prompt read ``@<LID>``
    where the phone showed a name, and a phone-addressed mention puts a
    member's number in front of everyone in the room. The bot is named by
    ``bot_name``, an istota user in this room by their display name, anyone
    else in it by the name they last posted under, and everyone else,
    including a number nobody in the room holds, by `UNNAMED_MENTION`. A name is
    text its owner chose, so it is flattened like a guest label; the whole
    turn stays in the user half, fenced as any participant's words are.
    """
    for mention in mentions:
        name = participants.flatten_label(
            _mention_name(conn, config, room_token, mention),
        ).lstrip("@").strip()
        replacement = f"@{name}" if name else UNNAMED_MENTION
        # A token is never followed by a digit, so `@123` leaves `@1234` alone.
        text = re.sub(
            re.escape(mention.token) + r"(?![0-9])",
            lambda _match, value=replacement: value, text,
        )
    return text


def _member_refs(member: WhatsAppGroupMember) -> tuple[str, str]:
    """``(ref, lid)``: the phone JID when WhatsApp shows one, else the LID."""
    jid = identity_rules.normalize_jid(member.jid)
    lid = identity_rules.normalize_lid(member.lid)
    return (jid or lid), lid


def _person(user_id: str | None, ref: str) -> str:
    """How `db.audience_persons` spells this participant."""
    return f"u:{user_id}" if user_id else f"{SURFACE}:{ref}"


def _relabel(conn, room_token: str, lid: str, ref: str, user_id: str | None) -> None:
    """A participant first seen by LID whose number is now known (D1).

    The present row moves to the phone JID, and so does any epoch it started,
    since `front_stage_cutoff` matches an epoch by how its joiner is spelled
    now: left on the LID spelling, a joiner whose number appeared would stop
    narrowing the front stage.
    """
    if not lid or lid == ref:
        return
    moved = conn.execute(
        "UPDATE room_participants SET surface_ref = ?, "
        "  user_id = COALESCE(?, user_id) "
        "WHERE room_token = ? AND surface = ? AND surface_ref = ? "
        "AND left_at IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM room_participants WHERE room_token = ? AND surface = ? "
        "  AND surface_ref = ? AND left_at IS NULL)",
        (ref, user_id, room_token, SURFACE, lid, room_token, SURFACE, ref),
    ).rowcount
    if moved:
        conn.execute(
            "UPDATE room_epochs SET person = ? WHERE room_token = ? AND person = ?",
            (_person(user_id, ref), room_token, _person(None, lid)),
        )


def _sync(
    conn, config: "Config", room_token: str,
    people: list[tuple[str, str, str | None, str | None]],
    *, acknowledged: bool,
) -> None:
    """Record ``people`` as the group's present WhatsApp participants.

    Everyone else on the surface is marked as having left, which is what keeps
    `room_is_shared` about who is in the group now. A join that grows the
    audience starts an epoch unless ``acknowledged`` (the founders).
    """
    present: list[str] = []
    for ref, lid, user_id, name in people:
        _relabel(conn, room_token, lid, ref, user_id)
        author = ParticipantRef(
            surface=SURFACE, surface_ref=ref, user_id=user_id, display_name=name,
        )
        db.upsert_room_participant(
            conn, room_token=room_token, surface=SURFACE, surface_ref=ref,
            kind=participants.classify(conn, config, room_token, author),
            user_id=user_id, display_name=name, acknowledged=acknowledged,
        )
        present.append(ref)
    db.sync_room_roster(conn, room_token=room_token, surface=SURFACE, present=present)


def _register(
    conn, config: "Config", group_jid: str, roster: WhatsAppGroupRoster,
    people: list[tuple[str, str, str | None, str | None]],
) -> "WhatsAppEventResult":
    from .webhook import WhatsAppEventResult

    users: list[str] = []
    for _, _, user_id, _ in people:
        if user_id and user_id not in users:
            users.append(user_id)
    adder = identity_rules.group_member_user(conn, roster.added_by)
    host = adder if adder in users else (users[0] if users else None)
    if host is None:
        logger.info(
            "whatsapp.group.not_registered group=%s: no istota user is in it",
            jid_fingerprint(group_jid),
        )
        return WhatsAppEventResult("group_no_principal")
    room = db.register_bound_room(
        conn, host, origin=SURFACE, name=roster.subject,
        surface=SURFACE, surface_ref=group_jid,
    )
    if room is None:
        return WhatsAppEventResult("group_bind_refused")
    token = room.token
    db.observe_external_room_name(conn, token, SURFACE, roster.subject)
    # Founders, not joiners: the group existed before the bot saw it, and
    # everyone in it now is who its history was written for.
    for user_id in users:
        if user_id != host:
            db.add_room_member(conn, token, user_id, acknowledged=True)
    _sync(conn, config, token, people, acknowledged=True)
    db.mark_audience_baseline(conn, token, SURFACE)
    logger.info(
        "whatsapp.group.registered group=%s room=%s",
        jid_fingerprint(group_jid), token,
    )
    return WhatsAppEventResult("group_registered", user_id=host)


def apply_roster(conn, config: "Config", roster: WhatsAppGroupRoster) -> "WhatsAppEventResult":
    """Apply one roster the sidecar read, inside the batch transaction."""
    from .webhook import WhatsAppEventResult

    group_jid = identity_rules.normalize_group_jid(roster.group_jid)
    if not group_jid:
        return WhatsAppEventResult("invalid_group")
    token = db.resolve_room_token(conn, SURFACE, group_jid)
    room = db.get_room(conn, token) if token else None
    if not roster.bot_present:
        # Removed from the group: nothing can be delivered there any more,
        # and removing the bot's number is the WhatsApp spelling of the veto
        # (D8), so the room stays off after a re-add until it is switched on.
        if room is not None:
            db.set_room_archived(conn, room.token, True)
            room_veto.switch_off_by_removal(conn, room.token)
        return WhatsAppEventResult("group_left")
    people: list[tuple[str, str, str | None, str | None]] = []
    seen: set[str] = set()
    for member in roster.members:
        ref, lid = _member_refs(member)
        if not ref or ref in seen:
            continue
        seen.add(ref)
        people.append((
            ref, lid,
            identity_rules.group_member_user(conn, ref) if ref != lid else None,
            member.display_name,
        ))
    if room is None:
        return _register(conn, config, group_jid, roster, people)

    # Fixed before the roster moves anyone: made afterwards, a policy would
    # take its host from whoever is left, and the departure D14 is about
    # would read as a hand-off to the next member.
    policy = room_policy.ensure_policy(conn, room.token)
    host = policy.host_user_id if policy is not None else None
    if room.archived:
        if host is None:
            # Archived by D14 and the bot is still in the group: the leave
            # frame was lost, so ask again rather than reviving a room with
            # no host.
            return WhatsAppEventResult("host_left", leave_group_jid=group_jid)
        # Archived because the bot was removed, and it is back.
        db.set_room_archived(conn, room.token, False)
    db.observe_external_room_name(conn, room.token, SURFACE, roster.subject)
    before = _whatsapp_refs_by_user(conn, room.token)
    baseline = db.audience_baseline_pending(conn, room.token, SURFACE)
    _sync(conn, config, room.token, people, acknowledged=baseline)
    if baseline:
        db.mark_audience_baseline(conn, room.token, SURFACE)
    departed = _departed(before, people)
    if host in departed:
        # D14: the host left the group, so the bot leaves it too. The room
        # keeps its transcript, loses its host and is archived; the leave
        # itself is a frame the bridge writes after this commits.
        room_policy.lose_host(conn, room.token, host)
        db.set_room_archived(conn, room.token, True)
        logger.info(
            "whatsapp.group.host_left group=%s room=%s: leaving the group",
            jid_fingerprint(group_jid), room.token,
        )
        return WhatsAppEventResult("host_left", leave_group_jid=group_jid)
    for user_id in departed:
        # Membership came from the group, so it goes with the group: a member
        # who left keeps neither the room's transcript as context in their
        # private chat nor a way to post into it from there.
        db.remove_room_member(conn, room.token, user_id)
    return WhatsAppEventResult("roster_synced")


def _whatsapp_refs_by_user(conn, room_token: str) -> dict[str, set[str]]:
    """Each istota user's present WhatsApp refs in the room."""
    refs: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT user_id, surface_ref FROM room_participants WHERE room_token = ? "
        "AND surface = ? AND user_id IS NOT NULL AND left_at IS NULL",
        (room_token, SURFACE),
    ):
        refs.setdefault(row["user_id"], set()).add(row["surface_ref"])
    return refs


def _departed(
    before: dict[str, set[str]],
    people: list[tuple[str, str, str | None, str | None]],
) -> set[str]:
    """The users who were on the group's roster and are not on this one.

    Judged by the refs they were present under, not by whether this roster's
    entries still resolve to them: a number whose binding changed is still
    the same person in the group. And nobody is judged gone while the roster
    holds an entry WhatsApp shows only by LID, which could be any of them —
    a wrong answer here archives the room and leaves the group for good.
    """
    if any(lid and ref == lid for ref, lid, _, _ in people):
        return set()
    present_refs = {ref for ref, _, _, _ in people} | {
        lid for _, lid, _, _ in people if lid
    }
    present_users = {user_id for _, _, user_id, _ in people if user_id}
    return {
        user_id for user_id, refs in before.items()
        if user_id not in present_users and not (refs & present_refs)
    }


def classify_group_event(config: "Config", event) -> "GateDecision | None":
    """The speech gate's classifier answer for a group turn, asked ahead.

    Call before `handle_whatsapp_batch` opens `BEGIN IMMEDIATE`, for the reason
    `ingest.classify_ahead` gives: the model call must not hold the write lock.
    None for anything that is not a worded turn (text, or a caption) in a
    registered group, and for a group whose effective mode is not the
    classifier. Never raises.
    """
    group = getattr(event, "group", None)
    if group is None or getattr(event, "message_type", None) not in _WORDED_TYPES:
        return None
    text = (getattr(event, "text", None) or "").strip()
    if not text or text.startswith("!"):
        return None
    from istota.rooms import policy as room_policy
    from ..ingest import classify_ahead

    try:
        group_jid = identity_rules.normalize_group_jid(group.group_jid)
        if not group_jid:
            return None
        with db.get_db(config.db_path) as conn:
            # Not the deployment's mode alone: a group can opt in on its own
            # (ISSUE-640); `classify_ahead` reads the room's own mode.
            if not room_policy.classifier_in_use(conn, config.speech_gate.mode):
                return None
            token = db.resolve_room_token(conn, SURFACE, group_jid)
            room = db.get_room(conn, token) if token else None
            if room is None or room.archived:
                return None
            sender_jid = identity_rules.normalize_jid(event.from_user.jid)
            user_id = (
                identity_rules.group_member_user(conn, sender_jid) if sender_jid else None
            )
            text = render_mentions(conn, config, room.token, text, group.mentions)
            # A quote is addressed but still classified, for its kind
            # (ISSUE-653); a mention or the name first is not.
            addressed = addressed_to_bot(
                conn, config, text, mentions_bot=group.mentions_bot,
                reply_to_message_id=None,
            )
            quoted = not addressed and quotes_bot(conn, event.reply_to_message_id)
    except Exception as e:  # noqa: BLE001 — a failed lookup is a failed decision
        logger.warning("whatsapp.group.classify_failed: %s", type(e).__name__)
        return None
    return classify_ahead(
        config, surface=SURFACE, surface_ref=group_jid,
        user_id=user_id or room.user_id, text=text, is_group_chat=False,
        addressed_to_bot=addressed, source_type="whatsapp", room_container=True,
        author_label=None if user_id else (event.from_user.username or None),
        replied_to_bot=quoted,
    )


def media_recipient(
    conn_factory, config: "Config", event, *,
    classified: "GateDecision | None" = None,
) -> str | None:
    """Whose inbox a group turn's file goes to, or None for nobody's.

    Asked by `stage_inbound_media` before anything is copied (ISSUE-646), on
    the connection ``conn_factory`` returns, which this owns and closes on
    every path, as `media.precheck` does with its own. A user id only for a member's turn the speech gate
    would answer, read with `speech_gate.should_speak` itself so the two
    cannot disagree about a rung: a guest's file would otherwise land in the
    host's inbox, and an unaddressed photo in a shared group in the sender's
    for a turn that never runs. ``classified`` is the classifier's answer,
    asked ahead as the transaction's own gate is.

    **A pre-filter, not the gate.** Host loss is not read (its policy row is
    written on first read, and this connection writes nothing), and the
    answer can be stale; the transaction decides. What that costs is a
    member's own file in their own inbox for a turn that was then recorded
    only. Never raises.
    """
    import sqlite3

    from istota.rooms import speech_gate

    group = getattr(event, "group", None)
    if group is None:
        return None
    try:
        conn = conn_factory()
    except Exception:  # noqa: BLE001 — no read connection places nothing
        logger.warning("whatsapp.group.media_recipient_unavailable")
        return None
    try:
        # The binding and room lookups index rows by column name.
        conn.row_factory = sqlite3.Row
        group_jid = identity_rules.normalize_group_jid(group.group_jid)
        token = db.resolve_room_token(conn, SURFACE, group_jid) if group_jid else None
        room = db.get_room(conn, token) if token else None
        if room is None or room.archived or room_veto.is_vetoed(conn, room.token):
            return None
        sender_jid = identity_rules.normalize_jid(event.from_user.jid)
        user_id = (
            identity_rules.group_member_user(conn, sender_jid) if sender_jid else None
        )
        if not user_id:
            return None
        if conn.execute(
            "SELECT 1 FROM processed_whatsapp WHERE message_id = ?",
            (event.message_id,),
        ).fetchone() is not None:
            return None
        text = render_mentions(
            conn, config, room.token, (event.text or "").strip(), group.mentions,
        )
        if text.startswith("!"):
            # A command acts on the room; the file goes nowhere.
            return None
        decision = speech_gate.should_speak(
            is_multi_human=participants.is_multi_human(
                conn, surface=SURFACE, room_token=room.token,
                is_group_chat=False, room_container=True,
            ),
            addressed_to_bot=addressed_to_bot(
                conn, config, text, mentions_bot=group.mentions_bot,
                reply_to_message_id=event.reply_to_message_id,
            ),
            mode=room_policy.effective_speech_mode(
                conn, room.token, config.speech_gate.mode,
            ),
            classified=classified,
        )
        return user_id if decision.speak else None
    except Exception as e:  # noqa: BLE001 — a failed read places nothing
        logger.warning("whatsapp.group.media_recipient_failed: %s", type(e).__name__)
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — nothing useful to do with it
            pass


def _stand_in(event: InboundWhatsAppEvent, *, guest: bool) -> str:
    """The fixed line a turn's unopened file or unsupported message records.

    Fixed English and nothing off the wire: the label comes from
    `media.MEDIA_LABELS` or `media.UNSUPPORTED_LABELS` by an allowlisted key.
    """
    if event.message_type == "unsupported":
        label = media_rules.UNSUPPORTED_LABELS.get(event.unsupported_kind or "", "a message")
        return f"[Sent {label}, which this bot cannot open.]"
    label = media_rules.MEDIA_LABELS.get(event.message_type, "a file")
    if guest:
        return f"[Sent {label}. Files from guests are not opened.]"
    return f"[Sent {label}, which was not opened.]"


def handle_group_message(
    conn, config: "Config", event: InboundWhatsAppEvent,
    *, classified: "GateDecision | None" = None,
) -> "WhatsAppEventResult":
    """One message posted in a group, inside the batch transaction.

    ``classified`` is `classify_group_event`'s answer for this event, asked
    before the transaction opened; without it the classifier rung fails closed.
    """
    from .webhook import (
        WhatsAppEventResult,
        _claim,
        _media_for_user,
        _report_stranded_media,
        _set_disposition,
        media_turn_text,
    )

    assert event.group is not None
    group_jid = identity_rules.normalize_group_jid(event.group.group_jid)
    token = db.resolve_room_token(conn, SURFACE, group_jid) if group_jid else None
    room = db.get_room(conn, token) if token else None
    if room is None or room.archived:
        logger.info(
            "whatsapp.group.unregistered message=%s",
            message_fingerprint(event.message_id),
        )
        _report_stranded_media(event, "group_unregistered")
        return WhatsAppEventResult("group_unregistered")
    sender_jid = identity_rules.normalize_jid(event.from_user.jid)
    ref = sender_jid or identity_rules.normalize_lid(event.group.sender_lid)
    if not ref:
        _report_stranded_media(event, "unknown_sender")
        return WhatsAppEventResult("unknown_sender")
    user_id = identity_rules.group_member_user(conn, sender_jid) if sender_jid else None
    if not _claim(conn, event, user_id or "", f"group:{event.message_type}"):
        _report_stranded_media(event, "duplicate")
        return WhatsAppEventResult("duplicate", user_id=user_id)
    attached = False

    def done(result: "WhatsAppEventResult") -> "WhatsAppEventResult":
        # A file the staging step placed and this turn did not attach is a
        # stray copy in the member's inbox, logged as the direct chat does.
        if not attached:
            _report_stranded_media(event, result.disposition)
        _set_disposition(conn, event, result.disposition, result.task_id)
        return result

    text = render_mentions(
        conn, config, room.token, (event.text or "").strip(), event.group.mentions,
    )
    # `!<bot> off|on` is heard from anyone in the group, and its answer goes
    # into the group (multiplayer D8). A room switched off records nothing
    # else (D12): the message is claimed, so it is never seen twice, and that
    # is all.
    verb = (
        room_veto.parse_command(text, config.bot_name)
        if event.message_type in _TEXT_TYPES else None
    )
    if verb is not None:
        outcome = room_veto.apply(
            conn, config, room_token=room.token, verb=verb,
            author=ParticipantRef(
                surface=SURFACE, surface_ref=ref, user_id=user_id,
                display_name=event.from_user.username,
            ),
        )
        if outcome is not None:
            return done(WhatsAppEventResult(
                "room_veto", user_id=user_id, response_text=outcome.text,
                response_logical_key=outcome.reference,
                group_post_room=room.token, group_post_owner=room.user_id,
            ))
    if room_veto.is_vetoed(conn, room.token):
        return done(WhatsAppEventResult("group_vetoed", user_id=user_id))

    # The words decide addressing, commands and answers; the body is what the
    # room records and the task reads. They differ only for a file: an opened
    # one is attached, and anything not opened leaves a stand-in, so neither
    # the transcript nor the model loses that something was sent (ISSUE-646).
    body = text if event.message_type in _TURN_TYPES else ""
    attachments: list[str] = []
    unsupported = event.message_type == "unsupported"
    if unsupported and not text and event.unsupported_kind is None:
        # A frame with no kind and no words is whatever the sidecar could not
        # read at all (a wrapper, a reaction); a row for each would fill the
        # transcript with noise.
        body = ""
    elif unsupported or event.message_type in media_rules.MEDIA_KINDS:
        media = _media_for_user(event, user_id or "").media
        if (
            user_id and not unsupported
            and media is not None and media.error is None and media.staged_path
        ):
            attachments = [media.staged_path]
            attached = True
            body = media_turn_text(text, media, attachments)
        else:
            stand_in = _stand_in(event, guest=not user_id)
            body = f"{text}\n{stand_in}" if text else stand_in
    if not body:
        # Claimed and answered by nothing: the direct-chat notice would be
        # read by the whole group.
        return done(WhatsAppEventResult("group_unsupported", user_id=user_id))
    if unsupported:
        # Recorded only, caption included, as the direct chat answers the type
        # and reads no text: never a task, a command or a confirmation answer,
        # so a one-human group does not answer every sticker.
        outcome = record_inbound(
            conn, config,
            surface=SURFACE, surface_ref=group_jid, user_id=user_id or "",
            text=body, source_type="whatsapp", channel_name=None,
            external_id=event.message_id, addressed_to_bot=False,
            author=ParticipantRef(
                surface=SURFACE, surface_ref=ref, user_id=user_id,
                display_name=event.from_user.username,
            ),
            room_container=True, record_only=True,
        )
        return done(WhatsAppEventResult(
            f"group_{outcome.outcome}", user_id=user_id,
        ))

    is_command = text.startswith("!")
    if user_id and is_command:
        # A user's command acts on this room; its answer goes to their own
        # chat with the bot, never into the group.
        return done(WhatsAppEventResult(
            "command", user_id=user_id, command_text=text,
            conversation_token=room.token,
            response_logical_key=f"command:{event.message_id}",
        ))
    if user_id and not db.room_is_shared(conn, room.token):
        # Nobody else reads this group, so a bare answer to a question it
        # asked is that question's answer, as in a private Talk room.
        answer = confirmations.parse_answer(text)
        parked = db.get_pending_confirmation(conn, room.token, user_id=user_id)
        if answer is not None and parked is not None:
            response = confirmations.apply_answer(
                conn, parked, answer, config, by="whatsapp",
            )
            return done(WhatsAppEventResult(
                "confirmation_answer", user_id=user_id, task_id=parked.id,
                response_text=response,
                response_logical_key=f"confirmation-answer:{event.message_id}",
            ))
    if user_id:
        confirmations.cancel_for_conversation(conn, room.token, user_id, by="whatsapp")
    outcome = record_inbound(
        conn, config,
        surface=SURFACE, surface_ref=group_jid, user_id=user_id or "",
        text=body, source_type="whatsapp", channel_name=None,
        attachments=attachments or None,
        external_id=event.message_id,
        addressed_to_bot=addressed_to_bot(
            conn, config, text, mentions_bot=event.group.mentions_bot,
            reply_to_message_id=event.reply_to_message_id,
        ),
        author=ParticipantRef(
            surface=SURFACE, surface_ref=ref, user_id=user_id,
            display_name=event.from_user.username,
        ),
        is_command=is_command,
        room_container=True,
        classified=classified,
        can_react=True,
    )
    if outcome.held_for_reaction:
        return done(WhatsAppEventResult(
            "group_ack", user_id=user_id, task_id=outcome.task_id,
            react_jid=group_jid, react_message_id=event.message_id,
            react_turn_id=outcome.message_id,
        ))
    disposition = "task" if outcome.task_id is not None else f"group_{outcome.outcome}"
    return done(WhatsAppEventResult(
        disposition, user_id=user_id, task_id=outcome.task_id,
    ))


__all__ = [
    "addressed_to_bot",
    "apply_roster",
    "classify_group_event",
    "group_destination",
    "group_room_token",
    "handle_group_message",
    "render_mentions",
]
