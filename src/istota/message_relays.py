"""Directional recipient consent and durable one-question relay state."""

from __future__ import annotations

import json
import sqlite3

from . import db

from .whatsapp_requests import CONTENT_RETENTION_DAYS, RequestError, write_transaction

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
    conn.execute(
        """INSERT INTO message_relays
           (id,asker_user_id,recipient_user_id,request_id,question,asker_display,origin,audience,
            provider,binding_fingerprint,state,return_reference)
           VALUES (?,?,?,?,?,?,?,?,?,?,'held',?)""",
        (relay_id, actor_user_id, recipient_user_id, request_id, question, snapshot["asker_display"],
         json.dumps(snapshot["origin"], sort_keys=True), json.dumps(snapshot["audience"]),
         provider, binding_fingerprint, "relay-return:" + relay_id),
    )


_PUBLIC_COLUMNS = """id,asker_user_id,recipient_user_id,surface,question,asker_display,
    state,created_at,approved_at,expires_at,answered_at,closed_at,answer_text,
    return_state,content_expires_at,content_cleared_at"""


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

        write(conn, conn.execute("SELECT * FROM message_relays WHERE id=?", (relay_id,)).fetchone())
    return bool(changed)


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


async def verify_private_audience(config, *, actor_user_id: str, origin: dict) -> None:
    """Fresh external audience check, called outside the claim transaction."""
    if not origin.get("talk_ref"):
        return
    from .talk import TalkClient

    client = TalkClient(config)
    try:
        participants = await client.get_participants(origin["talk_ref"])
    except Exception:
        raise RequestError("unsupported_origin") from None
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


def accept_reply(conn, config, *, actor_user_id: str, relay_id: str,
                 provider: str, inbound_id: str, text: str) -> str:
    """Claim the first authorized answer; caller owns dedup and task creation."""
    from .whatsapp_requests import binding_fingerprint

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
    binding = db.get_whatsapp_binding(conn, actor_user_id)
    if (not config.whatsapp.enabled or provider != config.whatsapp.provider
            or relay["provider"] != provider or binding is None or binding.provider != provider
            or binding_fingerprint(provider, binding) != relay["binding_fingerprint"]
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
    return "accepted" if changed else "expired"


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


def _reply_task(conn, config, *, actor_user_id: str, inbound_id: str, text: str,
                relay, outcome: str, attachments=None):
    from .transport._types import IncomingMessage
    from .transport.ingest import ingest_message
    from .transport.whatsapp import whatsapp_conversation_token
    from .transport.whatsapp.webhook import WhatsAppEventResult, MEDIA_ONLY_PROMPT

    task_id = ingest_message(conn, config, IncomingMessage(
        user_id=actor_user_id, text=text if text else MEDIA_ONLY_PROMPT,
        source_type="whatsapp", surface="whatsapp",
        channel_token=whatsapp_conversation_token(actor_user_id), output_target="whatsapp",
        attachments=attachments or [], mirror_to_room=False, queue="foreground",
        # Only one task owns the accepted association. Rejected replies retain
        # a bounded snapshot of their own question/outcome in the user half.
        reply_to_content=None if outcome == "accepted" else _context(relay, outcome),
    ))
    if task_id is None:
        raise RuntimeError("relay recipient task was not created")
    if outcome == "accepted":
        conn.execute("UPDATE message_relays SET recipient_task_id=? WHERE id=? AND recipient_user_id=?",
                     (task_id, relay["id"], actor_user_id))
    disposition = "relay_answer" if outcome == "accepted" else "relay_rejected"
    conn.execute("UPDATE processed_whatsapp SET task_id=?,disposition=? WHERE message_id=? AND user_id=?",
                 (task_id, disposition, inbound_id, actor_user_id))
    return WhatsAppEventResult(disposition, user_id=actor_user_id, task_id=task_id,
                              response_text=_REPLY_NOTICES[outcome],
                              response_logical_key=f"relay-reply:{inbound_id}")


def match_whatsapp_reply(conn, config, *, actor_user_id: str, event):
    """Handle only explicit relay replies, before bare YES/NO or commands."""
    import re
    from .transport.whatsapp.webhook import WhatsAppEventResult

    text = event.text or ""
    command = re.match(r"^\s*!relay\s+reply(?:\s|$)", text, re.IGNORECASE)
    relay = None
    answer = text
    if command:
        # Consume exactly one separator after the id, preserving the rest.
        parsed = re.match(r"^\s*!relay\s+reply +([^\s]+)(?: (.*))?$", text, re.IGNORECASE | re.DOTALL)
        if parsed:
            relay = conn.execute("SELECT * FROM message_relays WHERE id=? AND recipient_user_id=?",
                                 (parsed[1], actor_user_id)).fetchone()
            answer = parsed[2] or ""
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
                               provider=config.whatsapp.provider, inbound_id=event.message_id, text=answer)
    return _reply_task(conn, config, actor_user_id=actor_user_id, inbound_id=event.message_id,
                       text=answer, relay=relay, outcome=outcome)


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
                        conn, config, actor_user_id=actor, relay_id=relay["id"], provider=candidate["provider"],
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


def reconcile_question_delivery(conn, *, logical_key: str, status: str) -> None:
    """Provider receipts cannot undo a recipient's already committed answer."""
    if not logical_key.startswith("relay-question:"):
        return
    relay_id = logical_key.removeprefix("relay-question:")
    if status == "failed":
        _close_relay(conn, relay_id, state="failed", reason="delivery_failed")
    elif status in ("accepted", "sent", "delivered", "read"):
        conn.execute("UPDATE whatsapp_skill_requests SET state='sent',error_code=NULL,updated_at=datetime('now'),closed_at=datetime('now') "
                     "WHERE relay_id=? AND state IN ('sending','uncertain')", (relay_id,))
        conn.execute("UPDATE message_relays SET state='waiting' WHERE id=? AND state IN ('sending','uncertain')",
                     (relay_id,))


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
    with write_transaction(conn):
        rows = conn.execute(
            "SELECT s.logical_key,s.status FROM message_relays r JOIN sent_whatsapp s "
            "ON s.logical_key='relay-question:' || r.id "
            "WHERE (r.state IN ('sending','uncertain') AND s.status IN ('accepted','sent','delivered','read')) "
            "OR (r.state IN ('queued','sending','waiting','uncertain') AND s.status='failed') "
            "ORDER BY r.created_at,r.id LIMIT ?", (max(0, min(limit, 100)),),
        ).fetchall()
        for row in rows:
            reconcile_question_delivery(conn, logical_key=row["logical_key"], status=row["status"])


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
