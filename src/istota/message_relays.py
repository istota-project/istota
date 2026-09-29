"""Directional recipient consent and durable one-question relay state."""

from __future__ import annotations

import json
import logging
import sqlite3

from . import db

from .whatsapp_requests import CONTENT_RETENTION_DAYS, RequestError, write_transaction

logger = logging.getLogger(__name__)

RELAY_LIFETIME_SECONDS = 24 * 60 * 60
MAX_OPEN_PER_ASKER = 10
MAX_OPEN_PER_RECIPIENT = 20
MAX_REPLY_CANDIDATES = 20
REPLY_CANDIDATE_SECONDS = 600
OPEN_STATES = ("held", "queued", "sending", "waiting", "uncertain")
_OPEN_SQL = "('held','queued','sending','waiting','uncertain')"


def _validate_block_target(actor_user_id: str, asker_user_id: str) -> None:
    if not actor_user_id or not asker_user_id or actor_user_id == asker_user_id or asker_user_id == "*":
        raise RequestError("invalid_user")


def is_blocked(conn: sqlite3.Connection, *, actor_user_id: str, asker_user_id: str) -> bool:
    """Whether `actor_user_id` has blocked relay questions from `asker_user_id`.

    Asking is allowed by default between users of one installation (ISSUE-566);
    a block is the recipient's only control and is never revealed to the asker.
    """
    return conn.execute(
        "SELECT 1 FROM relay_blocks WHERE recipient_user_id=? AND asker_user_id=?",
        (actor_user_id, asker_user_id),
    ).fetchone() is not None


def list_blocks(conn: sqlite3.Connection, *, actor_user_id: str) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT asker_user_id,blocked_at FROM relay_blocks WHERE recipient_user_id=? ORDER BY asker_user_id",
        (actor_user_id,),
    )]


def block(conn: sqlite3.Connection, *, actor_user_id: str, asker_user_id: str) -> int:
    """Block an asker and close their unanswered relays; returns how many closed."""
    _validate_block_target(actor_user_id, asker_user_id)
    with write_transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO relay_blocks (recipient_user_id,asker_user_id) VALUES (?,?)",
            (actor_user_id, asker_user_id),
        )
        rows = conn.execute(
            f"SELECT id FROM message_relays WHERE recipient_user_id=? AND asker_user_id=? AND state IN {_OPEN_SQL}",
            (actor_user_id, asker_user_id),
        ).fetchall()
        for row in rows:
            _close_relay(conn, row["id"], state="cancelled", reason="blocked")
        return len(rows)


def unblock(conn: sqlite3.Connection, *, actor_user_id: str, asker_user_id: str) -> bool:
    _validate_block_target(actor_user_id, asker_user_id)
    return bool(conn.execute(
        "DELETE FROM relay_blocks WHERE recipient_user_id=? AND asker_user_id=?",
        (actor_user_id, asker_user_id),
    ).rowcount)


def _check_reservation(conn: sqlite3.Connection, *, actor_user_id: str, recipient_user_id: str) -> None:
    if is_blocked(conn, actor_user_id=recipient_user_id, asker_user_id=actor_user_id):
        raise RequestError("recipient_unavailable")
    if conn.execute(
        f"SELECT 1 FROM message_relays WHERE asker_user_id=? AND recipient_user_id=? AND state IN {_OPEN_SQL}",
        (actor_user_id, recipient_user_id),
    ).fetchone():
        raise RequestError("relay_already_open")
    for column, user, cap in (("asker_user_id", actor_user_id, MAX_OPEN_PER_ASKER),
                              ("recipient_user_id", recipient_user_id, MAX_OPEN_PER_RECIPIENT)):
        count = conn.execute(
            f"SELECT count(*) FROM message_relays WHERE {column}=? AND state IN {_OPEN_SQL}", (user,),
        ).fetchone()[0]
        if count >= cap:
            raise RequestError("relay_limit")


def _insert_relay(
    conn: sqlite3.Connection, *, relay_id: str, request_id: str, actor_user_id: str,
    recipient_user_id: str, question: str, provider: str, binding_fingerprint: str, snapshot: dict,
) -> None:
    destination = snapshot.get("destination") or {"kind": "whatsapp"}
    conn.execute(
        """INSERT INTO message_relays
           (id,asker_user_id,recipient_user_id,surface,request_id,question,asker_display,origin,audience,
            provider,binding_fingerprint,state,return_reference,destination)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,'held',?,?)""",
        (relay_id, actor_user_id, recipient_user_id, destination["kind"], request_id, question,
         snapshot["asker_display"], json.dumps(snapshot["origin"], sort_keys=True),
         json.dumps(snapshot["audience"]), provider, binding_fingerprint, "relay-return:" + relay_id,
         json.dumps(destination, sort_keys=True)),
    )


_PUBLIC_COLUMNS = """id,asker_user_id,recipient_user_id,surface,question,asker_display,
    state,created_at,approved_at,expires_at,answered_at,closed_at,answer_text,
    return_state,content_expires_at,content_cleared_at,approval,
    json_extract(destination,'$.label') AS destination_label"""


def get_relay(conn: sqlite3.Connection, *, actor_user_id: str, relay_id: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_PUBLIC_COLUMNS} FROM message_relays WHERE id=? AND (asker_user_id=? OR recipient_user_id=?)",
        (relay_id, actor_user_id, actor_user_id),
    ).fetchone()
    return dict(row) if row is not None else None


def list_relays(conn: sqlite3.Connection, *, actor_user_id: str, limit: int = 50) -> list[dict]:
    return [dict(row) for row in conn.execute(
        f"SELECT {_PUBLIC_COLUMNS} FROM message_relays WHERE asker_user_id=? OR recipient_user_id=? "
        "ORDER BY created_at DESC,id DESC LIMIT ?", (actor_user_id, actor_user_id, max(0, min(limit, 100))),
    )]


def cancel_relay(conn: sqlite3.Connection, *, actor_user_id: str, relay_id: str) -> bool:
    with write_transaction(conn):
        relay = get_relay(conn, actor_user_id=actor_user_id, relay_id=relay_id)
        if relay is None:
            raise RequestError("relay_unavailable")
        return _close_relay(conn, relay_id, state="cancelled", reason="cancelled")


def _close_relay(conn: sqlite3.Connection, relay_id: str, *, state: str, reason: str) -> bool:
    if state not in ("failed", "cancelled", "expired"):
        raise ValueError("invalid_terminal_state")
    changed = conn.execute(
        f"""UPDATE message_relays SET state=?,closed_at=datetime('now'),content_expires_at=datetime('now',?)
            WHERE id=? AND state IN {_OPEN_SQL}""",
        (state, f"+{CONTENT_RETENTION_DAYS} days", relay_id),
    ).rowcount
    if changed:
        conn.execute(
            """UPDATE whatsapp_skill_requests SET state=?,error_code=?,closed_at=datetime('now'),
               updated_at=datetime('now') WHERE relay_id=?""", (state, reason, relay_id),
        )
        conn.execute(
            """UPDATE tasks SET whatsapp_confirmation_request_id=NULL,
               status=CASE WHEN status='pending_confirmation' THEN 'cancelled' ELSE status END
               WHERE whatsapp_confirmation_request_id IN
               (SELECT id FROM whatsapp_skill_requests WHERE relay_id=?)""", (relay_id,),
        )
    if changed:
        from .notification_resolvers.message_relay import write

        relay = conn.execute("SELECT * FROM message_relays WHERE id=?", (relay_id,)).fetchone()
        write(conn, relay)
        _resolve_recipient_notice(conn, relay)
    return bool(changed)


def _resolve_recipient_notice(conn, relay) -> None:
    from .notification_resolvers import relay_question

    relay_question.resolve_for_relay(conn, relay["recipient_user_id"], relay["id"], by="system")


def store_reply_candidate(
    conn: sqlite3.Connection, *, actor_user_id: str, provider: str,
    inbound_id: str, quoted_id: str, text: str,
) -> str:
    """Persist an authenticated early quote, leaving task creation to ingest.

    Overflow and no-inflight results tell ingest to create the ordinary task
    immediately. Expired rows still count until reconciliation creates their
    task; silently dropping them would lose the recipient's message.
    """
    with write_transaction(conn):
        existing = conn.execute(
            "SELECT recipient_user_id FROM relay_reply_candidates WHERE provider=? AND inbound_id=?",
            (provider, inbound_id),
        ).fetchone()
        if existing is not None:
            if existing["recipient_user_id"] != actor_user_id:
                raise RequestError("candidate_unavailable")
            return "pending"
        if not conn.execute(
            "SELECT 1 FROM message_relays WHERE recipient_user_id=? AND provider=? AND state='sending'",
            (actor_user_id, provider),
        ).fetchone():
            return "no_inflight"
        count = conn.execute(
            "SELECT count(*) FROM relay_reply_candidates WHERE recipient_user_id=?", (actor_user_id,),
        ).fetchone()[0]
        if count >= MAX_REPLY_CANDIDATES:
            return "overflow"
        conn.execute(
            """INSERT INTO relay_reply_candidates
               (provider,inbound_id,recipient_user_id,quoted_id,answer_text,expires_at)
               VALUES (?,?,?,?,?,datetime('now',?))""",
            (provider, inbound_id, actor_user_id, quoted_id, text, f"+{REPLY_CANDIDATE_SECONDS} seconds"),
        )
        return "pending"


def private_origin(conn, config, *, actor_user_id: str, surface: str,
                   conversation_token: str | None) -> dict:
    """Resolve one private audience, with no output-plan fallback or fanout.

    Talk participants require a fresh server check via verify_private_audience before
    displaying content. The local membership table alone omits unknown users.
    """
    from .transport.whatsapp import whatsapp_conversation_token
    from .transport.sms import sms_conversation_token
    from .whatsapp_requests import binding_fingerprint, text_hash

    if actor_user_id not in config.users:
        raise RequestError("unsupported_origin")
    if surface in ("web", "talk"):
        room = db.get_room(conn, conversation_token or "")
        if room is None and surface == "talk":
            binding = conn.execute(
                "SELECT room_token FROM room_bindings WHERE surface='talk' AND surface_ref=?",
                (conversation_token,),
            ).fetchone()
            room = db.get_room(conn, binding[0]) if binding else None
        if (room is None or room.archived
                or db.list_room_members(conn, room.token) != [actor_user_id]):
            raise RequestError("unsupported_origin")
        talk = db.get_room_binding(conn, room.token, "talk")
        if surface == "talk" and talk is None:
            raise RequestError("unsupported_origin")
        return {"surface": surface, "channel": talk.surface_ref if surface == "talk" else room.token,
                "room_token": room.token, "talk_ref": talk.surface_ref if talk else None}
    if surface == "whatsapp" and conversation_token == whatsapp_conversation_token(actor_user_id):
        binding = db.get_whatsapp_binding(conn, actor_user_id)
        if config.whatsapp.enabled and binding and binding.provider == config.whatsapp.provider:
            return {"surface": surface, "channel": conversation_token,
                    "binding": binding_fingerprint(config.whatsapp.provider, binding)}
    if surface == "sms" and conversation_token == sms_conversation_token(actor_user_id):
        number = config.sms_phone_number_for(actor_user_id)
        if config.sms.enabled and number:
            return {"surface": surface, "channel": conversation_token,
                    "binding": text_hash(number)}
    raise RequestError("unsupported_origin")


def validate_origin(conn, config, *, actor_user_id: str, origin: dict) -> None:
    current = private_origin(conn, config, actor_user_id=actor_user_id,
                             surface=origin["surface"], conversation_token=origin.get("room_token") or origin["channel"])
    if current != origin:
        raise RequestError("unsupported_origin")


class AudienceUnavailable(RequestError):
    """The participant list could not be fetched, which says nothing about who is in the room.

    Carries the same code as a wrong audience, so a caller that answers a
    person refuses exactly as before; the delivery drain catches it first and
    tries again on its next tick instead of closing the relay.
    """

    def __init__(self) -> None:
        super().__init__("unsupported_origin")


async def verify_private_audience(config, *, actor_user_id: str, origin: dict) -> None:
    """Fresh external audience check, called outside the claim transaction."""
    if not origin.get("talk_ref"):
        return
    from .talk import TalkClient

    client = TalkClient(config)
    try:
        participants = await client.get_participants(origin["talk_ref"])
    except Exception as exc:
        # The class only: a message can carry the request URL. Without this a
        # lost credential and an outage read the same (ISSUE-568).
        logger.warning("relay audience check could not fetch Talk participants: %s",
                       type(exc).__name__)
        raise AudienceUnavailable() from None
    finally:
        await client.aclose()
    actors = set()
    for participant in participants:
        if participant.get("actorType") != "users" or not participant.get("actorId"):
            raise RequestError("unsupported_origin")
        actors.add(participant["actorId"])
    from .transport.talk import _bot_actor_ids

    if actor_user_id not in actors or len(actors) != 2 or not (actors - {actor_user_id}) <= _bot_actor_ids(config):
        raise RequestError("unsupported_origin")


def close_task_questions(conn, task_id: int, *, reason: str = "cancelled") -> None:
    with write_transaction(conn):
        rows = conn.execute(
            "SELECT relay_id FROM whatsapp_skill_requests WHERE origin_task_id=? AND state='held'",
            (task_id,),
        ).fetchall()
        for row in rows:
            _close_relay(conn, row[0], state="expired" if reason == "confirmation_expired" else "cancelled", reason=reason)


# Replies never enter command or confirmation dispatch again, including when
# correlation fails. These fixed notices carry no other user's content.
_REPLY_NOTICES = {
    "accepted": "Your answer was saved for return to the asker.",
    "unavailable": "Answer not forwarded: relay unavailable.",
    "closed": "Answer not forwarded: this relay is closed.",
    "expired": "Answer not forwarded: this relay has expired.",
    "answered": "Answer not forwarded: this relay already has an answer.",
    "empty": "Answer not forwarded: send a non-empty text reply.",
    "oversized": "Answer not forwarded: send a shorter explicit text reply.",
    "media": "Answer not forwarded: send a separate text-only reply to share an answer.",
    "unmatched": "Answer not forwarded: the quoted question could not be matched. Use !relay reply RELAY_ID <answer> to try again.",
}


def answer_body(relay, text: str) -> str:
    """One stable attribution header outside the exact authorized answer."""
    from .confirmations import flatten

    return f"Answer from {flatten(relay['recipient_user_id'])}:\n\n{text}"


def _answer_fits(config, relay, text: str) -> bool:
    from .transport.whatsapp.outbound import WHATSAPP_TEXT_LIMIT
    from .transport.sms.outbound import _gsm7_units, _utf16_units, _segment_count

    body = answer_body(relay, text)
    if len(body) > WHATSAPP_TEXT_LIMIT:
        return False
    origin = json.loads(relay["origin"])
    if origin["surface"] == "talk":
        from .transport.talk import TalkTransport

        if len(body) > (TalkTransport.capabilities.max_message_length or 4000):
            return False
    if origin["surface"] == "sms":
        gsm = _gsm7_units(body)
        encoding = "gsm7" if gsm is not None else "ucs2"
        units = gsm if gsm is not None else _utf16_units(body)
        return _segment_count(units, encoding) <= config.sms.max_segments
    return True


def _quoted_relay(conn, *, actor_user_id: str, provider: str, quoted_id: str):
    # Recognize closed or stale-binding questions too, so YES can never fall
    # through to a different action. Acceptance checks the live binding below.
    return conn.execute(
        "SELECT r.* FROM message_relays r JOIN sent_whatsapp s "
        "ON s.logical_key='relay-question:' || r.id "
        "WHERE s.meta_message_id=? AND s.user_id=? AND r.recipient_user_id=? AND r.provider=?",
        (quoted_id, actor_user_id, actor_user_id, provider),
    ).fetchone()


# Which relay destination kind each answering surface belongs to. A room is
# one conversation shown on web and Talk, so either may answer its question.
_SURFACE_KIND = {"web": "room", "talk": "room", "whatsapp": "whatsapp", "sms": "sms"}


def _destination_current(conn, config, relay, *, actor_user_id: str, surface: str) -> bool:
    """Whether the answer arrived through the destination the question went to.

    A room answer checks membership only: the reply was authored inside the
    room, and the live Talk participant check runs at delivery.
    """
    from .whatsapp_requests import binding_fingerprint, text_hash

    kind = _SURFACE_KIND.get(surface)
    if kind is None or relay["surface"] != kind:
        return False
    if kind == "whatsapp":
        provider = config.whatsapp.provider
        binding = db.get_whatsapp_binding(conn, actor_user_id)
        return bool(config.whatsapp.enabled and relay["provider"] == provider and binding is not None
                    and binding.provider == provider
                    and binding_fingerprint(provider, binding) == relay["binding_fingerprint"])
    if kind == "sms":
        number = config.sms_phone_number_for(actor_user_id)
        return bool(config.sms.enabled and number and text_hash(number) == relay["binding_fingerprint"])
    token = json.loads(relay["destination"] or "{}").get("room_token") or ""
    room = db.get_room(conn, token)
    return bool(relay["provider"] == "room" and room is not None and not room.archived
                and db.list_room_members(conn, token) == [actor_user_id])


def accept_reply(conn, config, *, actor_user_id: str, relay_id: str,
                 surface: str, inbound_id: str | None, text: str) -> str:
    """Claim the first authorized answer; caller owns dedup and task creation.

    `inbound_id` is namespaced by surface (`web:<id>`, `talk:<id>`, a provider
    id) so it stays unique across surfaces.
    """
    relay = conn.execute("SELECT * FROM message_relays WHERE id=? AND recipient_user_id=?",
                         (relay_id, actor_user_id)).fetchone()
    if relay is None:
        return "unavailable"
    if relay["state"] == "answered":
        return "answered"
    if relay["state"] == "expired":
        return "expired"
    if relay["state"] not in ("sending", "waiting", "uncertain"):
        return "closed"
    if not relay["expires_at"] or conn.execute(
        "SELECT ? <= datetime('now')", (relay["expires_at"],),
    ).fetchone()[0]:
        _close_relay(conn, relay_id, state="expired", reason="expired")
        return "expired"
    if (not _destination_current(conn, config, relay, actor_user_id=actor_user_id, surface=surface)
            or actor_user_id not in config.users
            or is_blocked(conn, actor_user_id=actor_user_id, asker_user_id=relay["asker_user_id"])):
        return "unavailable"
    if not text.strip():
        return "empty"
    if not _answer_fits(config, relay, text):
        return "oversized"
    changed = conn.execute(
        """UPDATE message_relays SET state='answered',answer_text=?,inbound_answer_id=?,
           answered_at=datetime('now'),return_state='pending',
           content_expires_at=datetime('now',?)
           WHERE id=? AND recipient_user_id=? AND state IN ('sending','waiting','uncertain')
           AND expires_at>datetime('now')""",
        (text, inbound_id, f"+{CONTENT_RETENTION_DAYS} days", relay_id, actor_user_id),
    ).rowcount
    if changed:
        conn.execute("UPDATE whatsapp_skill_requests SET state='sent',error_code=NULL,closed_at=datetime('now'),updated_at=datetime('now') "
                     "WHERE relay_id=? AND state IN ('sending','uncertain')", (relay_id,))
        _resolve_recipient_notice(conn, relay)
    return "accepted" if changed else "expired"


def relay_for_room_reply(conn, *, actor_user_id: str, room_token: str,
                         message_id: int | None = None, talk_id: int | None = None):
    """The room relay question a web or Talk reply names, or None.

    Closed questions are recognized too, so a quoted YES to one can never fall
    through to a parked confirmation; acceptance decides what it is worth.
    """
    if message_id is not None:
        parent = conn.execute("SELECT room_token,delivery_reference FROM messages WHERE id=?",
                              (message_id,)).fetchone()
        reference = (parent["delivery_reference"] or "") if parent is not None else ""
        if parent is None or parent["room_token"] != room_token or not reference.startswith("relay-question:"):
            return None
        relay = conn.execute(
            "SELECT * FROM message_relays WHERE id=? AND recipient_user_id=? AND surface='room' "
            "AND question_message_id=?",
            (reference.removeprefix("relay-question:"), actor_user_id, message_id),
        ).fetchone()
    elif talk_id is not None:
        relay = conn.execute(
            "SELECT * FROM message_relays WHERE question_talk_id=? AND recipient_user_id=? AND surface='room'",
            (int(talk_id), actor_user_id),
        ).fetchone()
    else:
        return None
    if relay is None or json.loads(relay["destination"] or "{}").get("room_token") != room_token:
        return None
    return relay


def accept_room_reply(conn, config, *, actor_user_id: str, relay_id: str, surface: str,
                      inbound_id: str | None, text: str, task_text: str | None = None,
                      attachments: list[str] | None = None, **task_kwargs) -> tuple[str, int | None]:
    """Accept a web or Talk reply and create the recipient's task, in the caller's transaction.

    A rejected answer still creates the ordinary task, carrying the rejection
    notice as relay context. A web reply's own message id is not known until
    its task exists, so its `inbound_id` is stamped afterwards.
    """
    if surface not in ("web", "talk"):
        raise ValueError("room replies arrive on web or talk")
    relay = conn.execute("SELECT * FROM message_relays WHERE id=? AND recipient_user_id=?",
                         (relay_id, actor_user_id)).fetchone()
    if attachments:
        outcome = "media"
    else:
        outcome = accept_reply(conn, config, actor_user_id=actor_user_id, relay_id=relay_id,
                               surface=surface, inbound_id=inbound_id, text=text)
    task_id = create_recipient_task(
        conn, config, relay, surface=surface, actor_user_id=actor_user_id,
        text=task_text if task_text is not None else text, outcome=outcome,
        attachments=attachments, **task_kwargs,
    )
    if surface == "web" and outcome == "accepted" and inbound_id is None and task_id is not None:
        row = conn.execute("SELECT id FROM messages WHERE task_id=? AND role='user' ORDER BY id LIMIT 1",
                           (task_id,)).fetchone()
        if row is not None:
            conn.execute("UPDATE message_relays SET inbound_answer_id=? WHERE id=? AND inbound_answer_id IS NULL",
                         (f"web:{row[0]}", relay_id))
    return outcome, task_id


def _context(relay, outcome: str) -> str:
    from .untrusted import frame_untrusted

    content = _REPLY_NOTICES[outcome]
    if relay is not None and relay["question"] is not None:
        content += (f"\nQuestion from {relay['asker_display']} ({relay['asker_user_id']}):"
                    f"\n{relay['question']}")
    return frame_untrusted(content, "RELAY CONTEXT")


def recipient_context(conn, *, actor_user_id: str, task_id: int) -> str:
    """Read only the recipient's associated question, never the asker's task."""
    relay = conn.execute(
        "SELECT r.question,r.asker_display,r.asker_user_id FROM message_relays r "
        "JOIN tasks t ON t.id=r.recipient_task_id "
        "WHERE r.recipient_task_id=? AND r.recipient_user_id=? AND t.user_id=?",
        (task_id, actor_user_id, actor_user_id),
    ).fetchone()
    return _context(relay, "accepted") if relay else ""


def create_recipient_task(conn, config, relay, *, surface: str, actor_user_id: str, text: str,
                          outcome: str, channel: str | None = None, attachments=None,
                          attachment_names=None, platform_message_id=None, reply_to_id=None,
                          channel_name=None, client_msg_id=None, model=None, effort=None,
                          model_prefix_used: bool = False) -> int | None:
    """The recipient's ordinary task for a relay reply, on the surface it arrived.

    Only the accepted task owns the association, and `recipient_context` reads
    it from there. A rejected reply carries a bounded snapshot of its own
    question and outcome in the user half instead.
    """
    from .transport._types import IncomingMessage
    from .transport.ingest import ingest_message, record_inbound

    context = None if outcome == "accepted" else _context(relay, outcome)
    if surface == "web":
        _, task_id = record_inbound(
            conn, config, surface="web", surface_ref=channel, user_id=actor_user_id, text=text,
            source_type="web", output_target="room", priority=5, attachments=attachments or None,
            attachment_names=attachment_names or None, client_msg_id=client_msg_id,
            reply_to_canonical_id=reply_to_id, reply_to_content=context,
            model=model, effort=effort, apply_room_default=not model_prefix_used,
        )
    elif surface == "talk":
        task_id = ingest_message(conn, config, IncomingMessage(
            user_id=actor_user_id, text=text, source_type="talk", surface="talk",
            channel_token=channel, channel_name=channel_name, attachments=attachments or [],
            platform_message_id=platform_message_id, reply_to_message_id=reply_to_id,
            reply_to_content=context, model=model, effort=effort, model_prefix_used=model_prefix_used,
        ))
    elif surface == "whatsapp":
        from .transport.whatsapp import whatsapp_conversation_token

        task_id = ingest_message(conn, config, IncomingMessage(
            user_id=actor_user_id, text=text, source_type="whatsapp", surface="whatsapp",
            channel_token=whatsapp_conversation_token(actor_user_id), output_target="whatsapp",
            attachments=attachments or [], mirror_to_room=False, queue="foreground",
            reply_to_content=context,
        ))
    elif surface == "sms":
        from .transport.sms import sms_conversation_token

        task_id = ingest_message(conn, config, IncomingMessage(
            user_id=actor_user_id, text=text, source_type="sms", surface="sms",
            channel_token=sms_conversation_token(actor_user_id), output_target="sms",
            mirror_to_room=False, queue="foreground", reply_to_content=context,
        ))
    else:
        raise ValueError("unsupported relay reply surface")
    if task_id is None:
        if surface in ("web", "talk"):
            # A room surface drops a known echo of a mirrored turn; raising
            # here would roll back the whole Talk poll batch on every retry.
            return None
        raise RuntimeError("relay recipient task was not created")
    if outcome == "accepted":
        conn.execute("UPDATE message_relays SET recipient_task_id=? WHERE id=? AND recipient_user_id=?",
                     (task_id, relay["id"], actor_user_id))
    return task_id


def _reply_task(conn, config, *, actor_user_id: str, inbound_id: str, text: str,
                relay, outcome: str, attachments=None):
    from .transport.whatsapp.webhook import WhatsAppEventResult, MEDIA_ONLY_PROMPT

    task_id = create_recipient_task(
        conn, config, relay, surface="whatsapp", actor_user_id=actor_user_id,
        text=text if text else MEDIA_ONLY_PROMPT, outcome=outcome, attachments=attachments,
    )
    disposition = "relay_answer" if outcome == "accepted" else "relay_rejected"
    conn.execute("UPDATE processed_whatsapp SET task_id=?,disposition=? WHERE message_id=? AND user_id=?",
                 (task_id, disposition, inbound_id, actor_user_id))
    return WhatsAppEventResult(disposition, user_id=actor_user_id, task_id=task_id,
                              response_text=_REPLY_NOTICES[outcome],
                              response_logical_key=f"relay-reply:{inbound_id}")


def parse_reply_command(text: str) -> tuple[bool, str | None, str]:
    """``(is_command, relay_id, answer)`` for a `!relay reply ID answer` message.

    Exactly one separator after the id is consumed and the rest is the answer
    verbatim. A command with no usable id comes back with `relay_id` None.
    """
    import re

    if not re.match(r"^\s*!relay\s+reply(?:\s|$)", text, re.IGNORECASE):
        return False, None, text
    parsed = re.match(r"^\s*!relay\s+reply +([^\s]+)(?: (.*))?$", text, re.IGNORECASE | re.DOTALL)
    if not parsed:
        return True, None, text
    return True, parsed[1], parsed[2] or ""


def match_whatsapp_reply(conn, config, *, actor_user_id: str, event):
    """Handle only explicit relay replies, before bare YES/NO or commands."""
    from .transport.whatsapp.webhook import WhatsAppEventResult

    text = event.text or ""
    command, relay_id, answer = parse_reply_command(text)
    relay = None
    if command:
        if relay_id is not None:
            relay = conn.execute("SELECT * FROM message_relays WHERE id=? AND recipient_user_id=?",
                                 (relay_id, actor_user_id)).fetchone()
    elif event.reply_to_message_id:
        relay = _quoted_relay(conn, actor_user_id=actor_user_id, provider=config.whatsapp.provider,
                              quoted_id=event.reply_to_message_id)
    else:
        return None
    if event.media is not None or event.message_type != "text":
        # A caption is never consent, even when its words are a relay command.
        if relay is None and not command:
            return None
        paths = [event.media.staged_path] if event.media and event.media.staged_path and not event.media.error else []
        return _reply_task(conn, config, actor_user_id=actor_user_id, inbound_id=event.message_id,
                           text=text, relay=relay, outcome="media", attachments=paths)
    if relay is None and not command:
        stored = store_reply_candidate(conn, actor_user_id=actor_user_id, provider=config.whatsapp.provider,
                                       inbound_id=event.message_id, quoted_id=event.reply_to_message_id, text=text)
        if stored == "no_inflight":
            return None
        if stored == "pending":
            return WhatsAppEventResult("relay_candidate", user_id=actor_user_id)
        outcome = "unmatched"
    elif relay is None:
        outcome = "unavailable"
    else:
        outcome = accept_reply(conn, config, actor_user_id=actor_user_id, relay_id=relay["id"],
                               surface="whatsapp", inbound_id=event.message_id, text=answer)
    return _reply_task(conn, config, actor_user_id=actor_user_id, inbound_id=event.message_id,
                       text=answer, relay=relay, outcome=outcome)


def match_sms_reply(conn, config, *, actor_user_id: str, inbound_id: str, text: str):
    """``(outcome, task_id)`` for an SMS `!relay reply`, or None for any other text.

    SMS has no quote field, so the command is the only way to answer. An
    unknown and a foreign id read alike. The recipient's task is created
    whatever the outcome, carrying the notice as relay context when refused.
    """
    command, relay_id, answer = parse_reply_command(text)
    if not command:
        return None
    relay = None
    if relay_id is not None:
        relay = conn.execute("SELECT * FROM message_relays WHERE id=? AND recipient_user_id=?",
                             (relay_id, actor_user_id)).fetchone()
    if relay is None:
        outcome = "unavailable"
    else:
        outcome = accept_reply(conn, config, actor_user_id=actor_user_id, relay_id=relay["id"],
                               surface="sms", inbound_id=inbound_id, text=answer)
    task_id = create_recipient_task(
        conn, config, relay, surface="sms", actor_user_id=actor_user_id,
        text=answer if answer.strip() else text.strip(), outcome=outcome,
    )
    return outcome, task_id


def reconcile_reply_candidates(config, *, limit: int = 20) -> list:
    """Bounded restart-safe correlation; no confirmation/command replay."""
    results = []
    with db.get_db(config.db_path) as conn:
        with write_transaction(conn):
            candidates = conn.execute(
                "SELECT *,expires_at<=datetime('now') AS expired FROM relay_reply_candidates "
                "ORDER BY received_at,inbound_id LIMIT ?", (max(0, min(limit, 100)),),
            ).fetchall()
            for candidate in candidates:
                actor = candidate["recipient_user_id"]
                relay = _quoted_relay(conn, actor_user_id=actor, provider=candidate["provider"],
                                      quoted_id=candidate["quoted_id"])
                if relay is None and not candidate["expired"]:
                    continue
                dedup = conn.execute("SELECT task_id FROM processed_whatsapp WHERE message_id=? AND user_id=?",
                                     (candidate["inbound_id"], actor)).fetchone()
                if dedup is not None and dedup["task_id"] is None:
                    outcome = "unmatched" if candidate["expired"] else accept_reply(
                        conn, config, actor_user_id=actor, relay_id=relay["id"], surface="whatsapp",
                        inbound_id=candidate["inbound_id"], text=candidate["answer_text"],
                    )
                    results.append(_reply_task(conn, config, actor_user_id=actor, inbound_id=candidate["inbound_id"],
                                               text=candidate["answer_text"], relay=relay, outcome=outcome))
                conn.execute("DELETE FROM relay_reply_candidates WHERE provider=? AND inbound_id=?",
                             (candidate["provider"], candidate["inbound_id"]))
    return results


RETURN_RECOVERY_SECONDS = 120


def expire_relays(conn, *, limit: int = 20) -> int:
    """Expire a bounded batch under the same writer lock as answer acceptance."""
    with write_transaction(conn):
        rows = conn.execute(
            f"SELECT id FROM message_relays WHERE state IN {_OPEN_SQL} AND expires_at<=datetime('now') "
            "ORDER BY expires_at,id LIMIT ?", (max(0, min(limit, 100)),),
        ).fetchall()
        for row in rows:
            _close_relay(conn, row["id"], state="expired", reason="expired")
    return len(rows)


# Ledger statuses meaning the provider took a question, across the WhatsApp
# and SMS ledgers. SMS `delivery_unconfirmed` is a message that left.
_QUESTION_REACHED = ("accepted", "queued", "sent", "delivered", "read", "delivery_unconfirmed")


def reconcile_question_delivery(conn, *, logical_key: str, status: str) -> None:
    """Provider receipts cannot undo a recipient's already committed answer."""
    if not logical_key.startswith("relay-question:"):
        return
    relay_id = logical_key.removeprefix("relay-question:")
    if status == "failed":
        _close_relay(conn, relay_id, state="failed", reason="delivery_failed")
    elif status in _QUESTION_REACHED:
        conn.execute("UPDATE whatsapp_skill_requests SET state='sent',error_code=NULL,updated_at=datetime('now'),closed_at=datetime('now') "
                     "WHERE relay_id=? AND state IN ('sending','uncertain')", (relay_id,))
        if conn.execute("UPDATE message_relays SET state='waiting' WHERE id=? AND state IN ('sending','uncertain')",
                        (relay_id,)).rowcount:
            write_recipient_notice(conn, relay_id)


def reconcile_return_delivery(conn, *, logical_key: str, status: str, message_id=None) -> None:
    if not logical_key.startswith("relay-return:"):
        return
    relay_id = logical_key.removeprefix("relay-return:")
    if status == "failed":
        changed = conn.execute(
            "UPDATE message_relays SET return_state='blocked',return_error='delivery_failed' "
            "WHERE id=? AND state='answered' AND return_state IN ('sending','uncertain','delivered')", (relay_id,),
        ).rowcount
        if changed:
            from .notification_resolvers.message_relay import write

            write(conn, conn.execute("SELECT * FROM message_relays WHERE id=?", (relay_id,)).fetchone())
    elif status in ("accepted", "queued", "sent", "delivered", "read"):
        _settle_return(conn, relay_id, "delivered", message_id=message_id)


def reconcile_relays(conn, *, limit: int = 20) -> None:
    """Project settled question ledgers after a restart, without sending."""
    reached = ",".join("?" * len(_QUESTION_REACHED))
    with write_transaction(conn):
        for table in ("sent_whatsapp", "sent_sms"):
            rows = conn.execute(
                f"SELECT s.logical_key,s.status FROM message_relays r JOIN {table} s "
                "ON s.logical_key='relay-question:' || r.id "
                f"WHERE (r.state IN ('sending','uncertain') AND s.status IN ({reached})) "
                "OR (r.state IN ('queued','sending','waiting','uncertain') AND s.status='failed') "
                "ORDER BY r.created_at,r.id LIMIT ?", (*_QUESTION_REACHED, max(0, min(limit, 100))),
            ).fetchall()
            for row in rows:
                reconcile_question_delivery(conn, logical_key=row["logical_key"], status=row["status"])


def write_recipient_notice(conn, relay_id: str):
    """Open the recipient's inbox row as the question reaches them.

    Returns the write result for a caller that pushes it; one that does not
    (a phone destination, whose message is its own push) drops it.
    """
    from .notification_resolvers import relay_question

    relay = conn.execute("SELECT * FROM message_relays WHERE id=?", (relay_id,)).fetchone()
    if relay is None or relay["state"] not in relay_question.OPEN_STATES:
        return None
    return relay_question.write(conn, relay)


async def deliver_question(config, row) -> None:
    """Release one approved relay question to its frozen destination.

    Every refusal settles through `_finish_request`, which closes the relay
    with a fixed reason; nothing here retargets.
    """
    import asyncio
    from .whatsapp_requests import _finish_request, logical_key

    try:
        with db.get_db(config.db_path) as conn:
            relay = conn.execute("SELECT origin,surface FROM message_relays WHERE id=?", (row["relay_id"],)).fetchone()
        if relay is None:
            raise RequestError("request_unavailable")
        if row["state"] == "queued":
            await verify_private_audience(config, actor_user_id=row["requester_user_id"], origin=json.loads(relay["origin"]))
        if relay["surface"] == "room":
            await _deliver_room_question(config, row)
            return
        if relay["surface"] == "sms":
            from .transport.sms.outbound import deliver_sms
            from .transport.sms.providers.registry import make_provider_registry
            try:
                providers = await asyncio.to_thread(make_provider_registry, config)
            except (ImportError, ValueError):
                raise RequestError("sms_unavailable") from None
            record = await deliver_sms(
                config, providers, logical_key=logical_key(row), user_id=row["recipient_user_id"],
                text="", relay_question_id=row["relay_id"],
            )
        else:
            from .transport.whatsapp.outbound import deliver_whatsapp
            record = await deliver_whatsapp(
                config, logical_key=logical_key(row), user_id=row["recipient_user_id"],
                text="", task_id=None, request_id=row["id"],
            )
    except AudienceUnavailable:
        # Nothing was claimed, so the row is still `queued` and the next drain
        # retries it. The queue deadline bounds the retries as it bounds any
        # queued question.
        if row["queue_deadline"] is None or row["queue_deadline"] <= db.sql_datetime_now():
            await asyncio.to_thread(_finish_request, config, row["id"], reason="queue_expired")
        else:
            logger.warning("relay %s: Talk participants unavailable; retrying next tick", row["relay_id"])
    except RequestError as exc:
        await asyncio.to_thread(_finish_request, config, row["id"], reason=str(exc))
    else:
        await asyncio.to_thread(_finish_request, config, row["id"], record=record)


async def _deliver_room_question(config, row) -> None:
    """The canonical row first, in one transaction with the claim, then Talk.

    The web row is the question: once it lands the relay is answerable, so a
    Talk post that fails afterwards is logged and costs nothing else. The
    request stays `sending` across the Talk post, so a crash between the two
    is found by the stale-claim recovery, which only reads the room back and
    never posts a second time.
    """
    import asyncio
    from .notification_store import deliver_pending

    fresh = row["state"] == "queued"
    if fresh:
        with db.get_db(config.db_path) as conn:
            stored = conn.execute("SELECT destination FROM message_relays WHERE id=?", (row["relay_id"],)).fetchone()
        destination = json.loads(stored["destination"]) if stored and stored["destination"] else {}
        try:
            await verify_private_audience(config, actor_user_id=row["recipient_user_id"],
                                          origin={"talk_ref": destination.get("talk_ref")})
        except AudienceUnavailable:
            raise
        except RequestError:
            raise RequestError("destination_not_private") from None
    claim, notice = await asyncio.to_thread(_claim_room_question, config, row["id"], fresh)
    if notice is not None:
        await asyncio.to_thread(deliver_pending, config, [notice])
    if claim is None:
        return
    talk_id = await _post_room_question(config, claim, fresh=fresh)
    if talk_id is None:
        logger.warning("relay %s: question not posted to Talk; it is answerable on web", claim["relay_id"])
    await asyncio.to_thread(_settle_room_question, config, claim, talk_id)


def _claim_room_question(config, request_id: str, fresh: bool):
    from .whatsapp_requests import CLAIM_RECOVERY_SECONDS, admit_request

    with db.get_db(config.db_path) as conn:
        with write_transaction(conn):
            row = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id=?", (request_id,)).fetchone()
            if row is None or not row["relay_id"]:
                return None, None
            relay_id = row["relay_id"]
            reference = "relay-question:" + relay_id
            if fresh:
                admitted = admit_request(conn, config, request_id=request_id, user_id=row["recipient_user_id"],
                                         logical_key=reference, send_kind="service", status="pending",
                                         surface="room")
                message_id = db.add_message(conn, admitted["room_token"], role="system", body=admitted["service"],
                                            origin_surface="web", delivery_reference=reference)
                conn.execute("UPDATE message_relays SET state='waiting',question_message_id=? "
                             "WHERE id=? AND state='sending'", (message_id, relay_id))
                notice = write_recipient_notice(conn, relay_id)
                if not admitted["talk_ref"]:
                    _mark_question_sent(conn, request_id)
                    return None, notice
                return {"relay_id": relay_id, "request_id": request_id, "message_id": message_id,
                        "talk_ref": admitted["talk_ref"], "body": admitted["service"]}, notice
            # Recovery of a claim whose Talk half never settled: take it over,
            # then only read back. The room row already exists.
            relay = conn.execute("SELECT * FROM message_relays WHERE id=?", (relay_id,)).fetchone()
            if row["state"] != "sending" or relay is None or not conn.execute(
                "SELECT ? <= datetime('now', ?)", (row["updated_at"], f"-{CLAIM_RECOVERY_SECONDS} seconds"),
            ).fetchone()[0]:
                return None, None
            conn.execute("UPDATE whatsapp_skill_requests SET updated_at=datetime('now') WHERE id=?", (request_id,))
            destination = json.loads(relay["destination"] or "{}")
            if (relay["question_message_id"] is None or relay["question_talk_id"] is not None
                    or not destination.get("talk_ref")):
                _mark_question_sent(conn, request_id)
                return None, None
            return {"relay_id": relay_id, "request_id": request_id, "message_id": relay["question_message_id"],
                    "talk_ref": destination["talk_ref"], "body": None}, None


def _mark_question_sent(conn, request_id: str) -> None:
    conn.execute("UPDATE whatsapp_skill_requests SET state='sent',error_code=NULL,closed_at=datetime('now'),"
                 "updated_at=datetime('now') WHERE id=? AND state='sending'", (request_id,))


async def _post_room_question(config, claim, *, fresh: bool):
    """The Talk half: a post the first time, a readback on recovery."""
    from .transport.talk import TalkTransport, get_talk_client

    transport = TalkTransport(config)
    reference = "relay-question:" + claim["relay_id"]
    try:
        if fresh:
            return await transport.deliver(claim["talk_ref"], claim["body"], reference_id=reference)
        message_id, _ = await transport._readback(get_talk_client(config), claim["talk_ref"], reference, True, None)
        return message_id
    except Exception:
        return None


def _settle_room_question(config, claim, talk_id) -> None:
    with db.get_db(config.db_path) as conn:
        with write_transaction(conn):
            if talk_id is not None:
                # The Talk post and the web row are one question: a reply to
                # either resolves the same relay.
                conn.execute("UPDATE message_relays SET question_talk_id=? WHERE id=? AND question_talk_id IS NULL",
                             (int(talk_id), claim["relay_id"]))
                db.set_message_external_id(conn, claim["message_id"], "talk", str(talk_id))
            _mark_question_sent(conn, claim["request_id"])


def return_payload(conn, config, *, relay_id: str, actor_user_id: str, surface: str) -> tuple[dict, str]:
    """Internal delivery admission, including the frozen origin's live binding."""
    relay = conn.execute("SELECT * FROM message_relays WHERE id=? AND asker_user_id=?",
                         (relay_id, actor_user_id)).fetchone()
    if (relay is None or relay["state"] != "answered" or relay["return_state"] != "sending"
            or relay["answer_text"] is None or relay["content_expires_at"] <= db.sql_datetime_now()):
        raise RequestError("return_unavailable")
    origin = json.loads(relay["origin"])
    if origin["surface"] != surface:
        raise RequestError("unsupported_origin")
    validate_origin(conn, config, actor_user_id=actor_user_id, origin=origin)
    if not _answer_fits(config, relay, relay["answer_text"]):
        raise RequestError("return_oversized")
    return origin, answer_body(relay, relay["answer_text"])


def return_whatsapp_destination(config, *, relay_id: str, actor_user_id: str, caps) -> str:
    from .whatsapp_requests import binding_fingerprint
    from .transport.whatsapp.outbound import _destination

    with db.get_db(config.db_path) as conn:
        relay = conn.execute("SELECT origin FROM message_relays WHERE id=? AND asker_user_id=? AND return_state='sending'",
                             (relay_id, actor_user_id)).fetchone()
        binding = db.get_whatsapp_binding(conn, actor_user_id)
        if (relay is None or binding is None or binding.provider != config.whatsapp.provider
                or binding_fingerprint(config.whatsapp.provider, binding) != json.loads(relay["origin"])["binding"]):
            raise RequestError("unsupported_origin")
        return _destination(binding, caps)


def _settle_return(conn, relay_id: str, state: str, *, message_id=None, error=None) -> None:
    changed = conn.execute(
        "UPDATE message_relays SET return_state=?,return_message_id=COALESCE(?,return_message_id),return_error=? "
        "WHERE id=? AND state='answered' AND return_state IN ('pending','sending','uncertain') AND return_state<>?",
        (state, str(message_id) if message_id is not None else None, error, relay_id, state),
    ).rowcount
    if changed and state == "delivered":
        from .notification_store import resolve_by_object

        resolve_by_object(conn, user_id=conn.execute("SELECT asker_user_id FROM message_relays WHERE id=?", (relay_id,)).fetchone()[0],
                          source="message_relay", object_type="message_relay", object_id=relay_id, by="delivered")
    if changed and state in ("blocked", "uncertain"):
        from .notification_resolvers.message_relay import write

        write(conn, conn.execute("SELECT * FROM message_relays WHERE id=?", (relay_id,)).fetchone())


def _return_rows(config, limit):
    with db.get_db(config.db_path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM message_relays WHERE state='answered' AND answer_text IS NOT NULL "
            "AND content_expires_at>datetime('now') AND (return_state='pending' "
            "OR (return_state IN ('sending','uncertain') AND return_claimed_at<=datetime('now',?))) "
            "ORDER BY CASE WHEN return_state='pending' THEN 0 ELSE 1 END,return_claimed_at,id LIMIT ?",
            (f"-{RETURN_RECOVERY_SECONDS} seconds", max(0, min(limit, 100))),
        )]


def _claim_return(config, relay_id: str) -> tuple[dict | None, bool]:
    """A web return and its outbox settlement commit together."""
    with db.get_db(config.db_path) as conn:
        with write_transaction(conn):
            relay = conn.execute("SELECT * FROM message_relays WHERE id=?", (relay_id,)).fetchone()
            if (relay is None or relay["state"] != "answered" or relay["answer_text"] is None
                    or relay["content_expires_at"] <= db.sql_datetime_now()):
                return None, False
            fresh = relay["return_state"] == "pending"
            if not fresh and (relay["return_state"] not in ("sending", "uncertain") or not conn.execute(
                "SELECT ?<=datetime('now',?)", (relay["return_claimed_at"], f"-{RETURN_RECOVERY_SECONDS} seconds"),
            ).fetchone()[0]):
                return None, False
            origin = json.loads(relay["origin"])
            try:
                validate_origin(conn, config, actor_user_id=relay["asker_user_id"], origin=origin)
            except RequestError:
                _settle_return(conn, relay_id, "blocked", error="unsupported_origin")
                return None, False
            reference = "relay-return:" + relay_id
            if origin["surface"] == "web":
                message_id = db.add_message(conn, origin["room_token"], role="system",
                                            body=answer_body(relay, relay["answer_text"]), origin_surface="web",
                                            delivery_reference=reference)
                conn.execute("UPDATE message_relays SET return_reference=? WHERE id=?", (reference, relay_id))
                _settle_return(conn, relay_id, "delivered", message_id=message_id)
                return None, False
            conn.execute("UPDATE message_relays SET return_state=?,return_reference=?,return_claimed_at=datetime('now') WHERE id=?",
                         ("sending" if fresh else relay["return_state"], reference, relay_id))
            return dict(conn.execute("SELECT * FROM message_relays WHERE id=?", (relay_id,)).fetchone()), fresh


def _record_return(config, relay_id, state, *, message_id=None, error=None):
    with db.get_db(config.db_path) as conn:
        with write_transaction(conn):
            _settle_return(conn, relay_id, state, message_id=message_id, error=error)


def _delivery_outcome(status):
    if status in ("accepted", "queued", "sent", "delivered", "read", "delivery_unconfirmed"):
        return "delivered"
    if status in ("pending", "unknown"):
        return "uncertain"
    return "blocked"


async def _external_return(config, relay, *, fresh):
    """The first attempt may send. Recovery may only read ledger/readback."""
    import asyncio

    origin = json.loads(relay["origin"])
    surface = origin["surface"]
    reference = relay["return_reference"]
    if surface == "talk":
        from .transport.talk import TalkTransport, get_talk_client

        transport = TalkTransport(config)
        if fresh:
            message_id = await transport.deliver(origin["channel"], answer_body(relay, relay["answer_text"]),
                                                 reference_id=reference)
        else:
            message_id, _ = await transport._readback(get_talk_client(config), origin["channel"], reference, True, None)
        return ("delivered" if message_id is not None else "uncertain"), message_id
    if not fresh:
        with db.get_db(config.db_path) as conn:
            table = "sent_whatsapp" if surface == "whatsapp" else "sent_sms"
            column = "meta_message_id" if surface == "whatsapp" else "provider_message_id"
            record = conn.execute(f"SELECT status,{column} FROM {table} WHERE logical_key=?", (reference,)).fetchone()
        # No ledger proves no attempt, but cannot prove a pre-ledger claimant
        # isn't still alive. Recovery never starts a second external call.
        return (_delivery_outcome(record[0]), record[1]) if record else ("uncertain", None)
    if surface == "whatsapp":
        from .transport.whatsapp.outbound import deliver_whatsapp

        record = await deliver_whatsapp(config, logical_key=reference, user_id=relay["asker_user_id"],
                                        text="", relay_return_id=relay["id"])
        return _delivery_outcome(record.status), record.meta_message_id
    from .transport.sms.outbound import deliver_sms
    from .transport.sms.providers.registry import make_provider_registry

    providers = await asyncio.to_thread(make_provider_registry, config)
    record = await deliver_sms(config, providers, logical_key=reference, user_id=relay["asker_user_id"],
                               text="", relay_return_id=relay["id"])
    return _delivery_outcome(record.status), record.provider_message_id


async def deliver_returns(config, *, limit: int = 20) -> int:
    import asyncio

    rows = await asyncio.to_thread(_return_rows, config, limit)
    for row in rows:
        try:
            await verify_private_audience(config, actor_user_id=row["asker_user_id"], origin=json.loads(row["origin"]))
        except RequestError:
            await asyncio.to_thread(_record_return, config, row["id"], "blocked", error="unsupported_origin")
            continue
        relay, fresh = await asyncio.to_thread(_claim_return, config, row["id"])
        if relay is None:
            continue
        try:
            state, message_id = await _external_return(config, relay, fresh=fresh)
        except RequestError as exc:
            state, message_id = "blocked", None
            error = str(exc)
        except Exception:
            state, message_id, error = "uncertain", None, "delivery_unknown"
        else:
            error = None if state == "delivered" else "could_not_deliver"
        await asyncio.to_thread(_record_return, config, relay["id"], state, message_id=message_id, error=error)
    return len(rows)


def _sweep_relays(config, limit):
    from .whatsapp_requests import cleanup_content

    with db.get_db(config.db_path) as conn:
        expire_relays(conn, limit=limit)
        reconcile_relays(conn, limit=limit)
        with write_transaction(conn):
            for table, column in (("sent_whatsapp", "meta_message_id"), ("sent_sms", "provider_message_id")):
                rows = conn.execute(
                    f"SELECT s.logical_key,s.status,s.{column} AS message_id FROM message_relays r JOIN {table} s "
                    "ON s.logical_key=r.return_reference WHERE r.state='answered' "
                    "AND r.return_state IN ('sending','uncertain','delivered') AND s.status='failed' LIMIT ?", (limit,),
                ).fetchall()
                for row in rows:
                    reconcile_return_delivery(conn, logical_key=row["logical_key"], status=row["status"], message_id=row["message_id"])
        cleanup_content(conn, limit=limit)


async def poll_relays(config, *, limit: int = 20):
    import asyncio

    limit = max(0, min(limit, 100))
    await asyncio.to_thread(_sweep_relays, config, limit)
    await deliver_returns(config, limit=limit)
    await deliver_relay_notices(config, limit=limit)


async def deliver_relay_notices(config, *, limit: int = 20):
    """Retry only body-free notices; never reroute a private answer."""
    import asyncio
    from .notification_store import RaiseResult, deliver_pending, mark_delivered
    from .notifications import send_notification

    with db.get_db(config.db_path) as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT n.*,r.origin FROM notifications n JOIN message_relays r ON r.id=n.object_id "
            "AND r.asker_user_id=n.user_id WHERE n.source='message_relay' AND n.state='open' "
            "AND n.last_delivered_at IS NULL ORDER BY n.id LIMIT ?", (max(0, min(limit, 100)),),
        )]
    for row in rows:
        origin = json.loads(row["origin"]) if row["origin"] else None
        try:
            if origin is None:
                raise RequestError("unsupported_origin")
            await verify_private_audience(config, actor_user_id=row["user_id"], origin=origin)
            with db.get_db(config.db_path) as conn:
                validate_origin(conn, config, actor_user_id=row["user_id"], origin=origin)
        except RequestError:
            result = RaiseResult(row["id"], row["user_id"], True, row["body"], row["title"], "alert")
            await asyncio.to_thread(deliver_pending, config, [result])
            continue
        descriptor = origin["surface"]
        if descriptor in ("web", "talk"):
            descriptor += ":" + origin["channel"]
        sent = await asyncio.to_thread(send_notification, config, row["user_id"], row["body"],
                                       surface=descriptor, reference_id="relay-notice:" + str(row["id"]))
        if sent:
            with db.get_db(config.db_path) as conn:
                mark_delivered(conn, [row["id"]])
