"""Durable WhatsApp intents. Storage helpers never send or commit."""

from __future__ import annotations

from contextlib import contextmanager
import asyncio
import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid

from . import db

MAX_CONTENT_CHARS = 2000
QUEUE_DEADLINE_SECONDS = 600
CLAIM_RECOVERY_SECONDS = 120
CONTENT_RETENTION_DAYS = 30
REQUEST_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


#: Every request kind. The two side-room kinds (multiplayer D4, D16) ride this
#: table rather than a second hold table: `side_whisper` is a task in a shared
#: room writing to its principal's side room, `room_post` a side-room task's
#: post into the parent room, held for the member's approval.
KINDS = ("self_send", "relay_question", "side_whisper", "room_post")
ROOM_KINDS = ("side_whisper", "room_post")
_SELF_KINDS = ("self_send", "side_whisper", "room_post")
_HELD_KINDS = ("relay_question", "room_post")


class RequestError(ValueError):
    """A fixed, body-free refusal code, safe to pass to a caller."""


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    """Serialize reads followed by writes, without committing caller work.

    Start an outer transaction before the savepoint: RELEASE must not commit.
    Existing transactions acquire the writer lock before reading state. A
    caller with a stale WAL read snapshot gets SQLite's conflict and must retry
    its whole transaction, never just a state-changing fragment.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("UPDATE whatsapp_skill_requests SET id=id WHERE 0")
    savepoint = "wa_request_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE {savepoint}")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def binding_fingerprint(provider: str, binding: db.WhatsAppBinding) -> str:
    """Freeze identity and destination, never last-seen/window timestamps."""
    values = [provider, binding.provider, binding.user_id, binding.bsuid,
              binding.jid, binding.bootstrap_phone_number, binding.send_id]
    return text_hash(json.dumps(values, ensure_ascii=True, separators=(",", ":")))


def _validate_input(request_key: str, text: str) -> None:
    if not REQUEST_KEY_RE.fullmatch(request_key):
        raise RequestError("invalid_request_key")
    if not text or len(text) > MAX_CONTENT_CHARS:
        raise RequestError("invalid_text")
    if not any(not ch.isspace() and not unicodedata.category(ch).startswith("C") for ch in text):
        raise RequestError("invalid_text")


def _store_request(
    conn: sqlite3.Connection, *, actor_user_id: str, task_id: int,
    request_key: str, kind: str, recipient_user_id: str, text: str,
    service_body: str, template_body: str | None, provider: str,
    binding_fingerprint: str, preview: str | None = None,
    relay_snapshot: dict | None = None, request_id: str | None = None,
    relay_id: str | None = None, origin: dict | None = None,
    destination: dict | None = None,
) -> dict:
    """Internal insertion after rendering and surface validation by the host.

    This is not a skill entry point. Its caller supplies validated immutable
    snapshots; it enforces task ownership, replay, consent and reservations.

    Four kinds. A `self_send` and a `side_whisper` go to the requester and are
    queued at once; a `relay_question` and a `room_post` are held for the
    requester's approval of the exact preview. The two side-room kinds carry
    their own `origin` and `destination` here, where a relay keeps them on its
    `message_relays` row.
    """
    from . import message_relays

    _validate_input(request_key, text)
    if kind not in KINDS:
        raise RequestError("invalid_kind")
    if kind in _SELF_KINDS and recipient_user_id != actor_user_id:
        raise RequestError("recipient_unavailable")
    if kind == "relay_question" and recipient_user_id == actor_user_id:
        raise RequestError("use_send")
    if not service_body:
        raise RequestError("invalid_text")
    payload_hash = text_hash(json.dumps([kind, recipient_user_id, text], ensure_ascii=True))
    with write_transaction(conn):
        task = conn.execute("SELECT * FROM tasks WHERE id=? AND user_id=?",
                            (task_id, actor_user_id)).fetchone()
        if task is None or task["status"] != "running":
            raise RequestError("task_unavailable")
        existing = conn.execute(
            "SELECT * FROM whatsapp_skill_requests WHERE requester_user_id=? "
            "AND origin_task_id=? AND request_key=?", (actor_user_id, task_id, request_key),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != payload_hash:
                raise RequestError("request_conflict")
            return dict(existing)
        if kind in _HELD_KINDS:
            if not preview or (kind == "relay_question" and relay_snapshot is None):
                raise RequestError("invalid_preview")
            # One held request per task: the task parks on one confirmation.
            if task["whatsapp_confirmation_request_id"] or conn.execute(
                "SELECT 1 FROM whatsapp_skill_requests WHERE origin_task_id=? AND state='held'",
                (task_id,),
            ).fetchone():
                raise RequestError("confirmation_pending")
        if kind == "relay_question":
            message_relays._check_reservation(conn, actor_user_id=actor_user_id,
                                              recipient_user_id=recipient_user_id)
        request_id = request_id or str(uuid.uuid4())
        relay_id = (relay_id or str(uuid.uuid4())) if kind == "relay_question" else None
        state = "held" if kind in _HELD_KINDS else "queued"
        conn.execute(
            """INSERT INTO whatsapp_skill_requests
            (id,requester_user_id,origin_task_id,request_key,kind,recipient_user_id,
             relay_id,text,content_hash,service_body,service_hash,template_body,
             template_hash,preview,preview_digest,provider,binding_fingerprint,state,
             origin,destination,queue_deadline)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    CASE WHEN ?='queued' THEN datetime('now', ?) END)""",
            (request_id, actor_user_id, task_id, request_key, kind, recipient_user_id,
             relay_id, text, payload_hash, service_body, text_hash(service_body), template_body,
             text_hash(template_body) if template_body is not None else None,
             preview, text_hash(preview) if preview else None, provider, binding_fingerprint,
             state, json.dumps(origin) if origin is not None else None,
             json.dumps(destination) if destination is not None else None,
             state, f"+{QUEUE_DEADLINE_SECONDS} seconds"),
        )
        if relay_id:
            message_relays._insert_relay(
                conn, relay_id=relay_id, request_id=request_id, actor_user_id=actor_user_id,
                recipient_user_id=recipient_user_id, question=text, provider=provider,
                binding_fingerprint=binding_fingerprint, snapshot=relay_snapshot,
            )
        return dict(conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id=?", (request_id,)).fetchone())


def get_request(conn: sqlite3.Connection, *, actor_user_id: str, request_id: str) -> dict | None:
    row = conn.execute(
        """SELECT id,request_key,kind,recipient_user_id,relay_id,text,state,preview,
                  created_at,updated_at,approved_at,queue_deadline,closed_at,
                  content_cleared_at,
                  CASE WHEN kind='relay_question' AND error_code IS NOT NULL
                       THEN 'could_not_deliver' ELSE error_code END AS error_code
           FROM whatsapp_skill_requests WHERE id=? AND requester_user_id=?""",
        (request_id, actor_user_id),
    ).fetchone()
    return dict(row) if row is not None else None


def associate_confirmation(
    conn: sqlite3.Connection, *, actor_user_id: str, task_id: int,
    request_id: str, preview_digest: str,
) -> None:
    """Attach only the exact held preview to its owner's parked task."""
    with write_transaction(conn):
        changed = conn.execute(
            """UPDATE tasks SET whatsapp_confirmation_request_id=?
               WHERE id=? AND user_id=? AND status='pending_confirmation'
               AND (whatsapp_confirmation_request_id IS NULL OR whatsapp_confirmation_request_id=?)
               AND EXISTS (SELECT 1 FROM whatsapp_skill_requests r WHERE r.id=?
                           AND r.origin_task_id=tasks.id AND r.requester_user_id=tasks.user_id
                           AND r.state='held' AND r.preview_digest=?)""",
            (request_id, task_id, actor_user_id, request_id, request_id, preview_digest),
        ).rowcount
        if not changed:
            raise RequestError("confirmation_unavailable")


def cleanup_content(conn: sqlite3.Connection, *, limit: int = 100) -> int:
    """Clear bounded content batches; identity/hashes continue to prevent replay.

    Undelivered answers have the same retention deadline as delivered answers.
    Their expiration closes the return outbox before erasing its payload.
    """
    if limit <= 0:
        return 0
    with write_transaction(conn):
        relays = conn.execute(
            """SELECT id,request_id FROM message_relays WHERE content_cleared_at IS NULL
               AND content_expires_at <= datetime('now')
               AND state IN ('answered','failed','cancelled','expired')
               ORDER BY content_expires_at,id LIMIT ?""", (limit,),
        ).fetchall()
        for row in relays:
            conn.execute(
                """UPDATE message_relays SET question=NULL,answer_text=NULL,asker_display=NULL,
                   origin=NULL,audience=NULL,content_cleared_at=datetime('now'),
                   return_state=CASE WHEN return_state IN ('pending','sending','blocked','uncertain')
                                     THEN 'expired' ELSE return_state END WHERE id=?""", (row["id"],),
            )
            _clear_request_content(conn, row["request_id"])
        remaining = limit - len(relays)
        requests = conn.execute(
            """SELECT id FROM whatsapp_skill_requests WHERE content_cleared_at IS NULL
               AND closed_at <= datetime('now', ?)
               AND state IN ('sent','failed','cancelled','expired','uncertain')
               AND NOT EXISTS (SELECT 1 FROM message_relays r WHERE r.request_id=whatsapp_skill_requests.id
                               AND r.content_cleared_at IS NULL)
               ORDER BY closed_at,id LIMIT ?""",
            (f"-{CONTENT_RETENTION_DAYS} days", remaining),
        ).fetchall()
        for row in requests:
            _clear_request_content(conn, row["id"])
        return len(relays) + len(requests)


def _clear_request_content(conn: sqlite3.Connection, request_id: str) -> None:
    conn.execute(
        """UPDATE whatsapp_skill_requests SET text=NULL,service_body=NULL,template_body=NULL,
           preview=NULL,content_cleared_at=datetime('now'),updated_at=datetime('now') WHERE id=?""",
        (request_id,),
    )


def _request_response(row) -> dict:
    state = row["state"]
    return {"status": state, "request_id": row["id"],
            "delivery_status": "pending" if state == "queued" else state,
            "independent_of_task_result": True}


def enqueue_self_send(conn, config, *, actor_user_id: str, task_id: int,
                 request_key: str, text: str) -> dict:
    """Persist a self-send; only the daemon may attempt its delivery."""
    from .transport.whatsapp.outbound import (
        active_adapter, _destination, render_whatsapp_result, render_template_result,
        template_available,
    )

    _validate_input(request_key, text)
    with write_transaction(conn):
        task = conn.execute("SELECT status FROM tasks WHERE id=? AND user_id=?",
                            (task_id, actor_user_id)).fetchone()
        if task is None or task["status"] != "running":
            raise RequestError("task_unavailable")
        existing = conn.execute(
            "SELECT * FROM whatsapp_skill_requests WHERE requester_user_id=? "
            "AND origin_task_id=? AND request_key=?", (actor_user_id, task_id, request_key),
        ).fetchone()
        if existing:
            digest = text_hash(json.dumps(["self_send", actor_user_id, text], ensure_ascii=True))
            if digest != existing["content_hash"]:
                raise RequestError("request_conflict")
            return _request_response(existing)
        if actor_user_id not in config.users or not config.whatsapp.enabled:
            raise RequestError("recipient_unavailable")
        adapter = active_adapter(config)
        binding = db.get_whatsapp_binding(conn, actor_user_id)
        if (adapter is None or binding is None or binding.provider != config.whatsapp.provider
                or not _destination(binding, adapter.caps)):
            raise RequestError("recipient_unavailable")
        service, truncated = render_whatsapp_result(text, limit=adapter.caps.service_body_limit)
        if not service or truncated:
            raise RequestError("invalid_rendering")
        template = None
        if adapter.caps.supports_templates and template_available(config):
            rendered, truncated = render_template_result(text)
            if rendered and not truncated:
                template = rendered
        row = _store_request(
            conn, actor_user_id=actor_user_id, task_id=task_id, request_key=request_key,
            kind="self_send", recipient_user_id=actor_user_id, text=text,
            service_body=service, template_body=template, provider=config.whatsapp.provider,
            binding_fingerprint=binding_fingerprint(config.whatsapp.provider, binding),
        )
        return _request_response(row)


def logical_key(row) -> str:
    if row["kind"] == "relay_question":
        return "relay-question:" + row["relay_id"]
    if row["kind"] == "room_post":
        return "room-post:" + row["id"]
    if row["kind"] == "side_whisper":
        return "room-whisper:" + row["id"]
    return "skill-whatsapp:" + row["id"]


def _check_binding(conn, config, row):
    binding = db.get_whatsapp_binding(conn, row["recipient_user_id"])
    if (row["provider"] != config.whatsapp.provider or binding is None
            or binding.provider != row["provider"]
            or binding_fingerprint(config.whatsapp.provider, binding) != row["binding_fingerprint"]):
        raise RequestError("binding_changed")
    return binding


def _check_sms_number(config, row) -> str:
    """The recipient's number, read once and matched against the frozen hash."""
    number = config.sms_phone_number_for(row["recipient_user_id"]) if config.sms.enabled else None
    if not number or text_hash(number) != row["binding_fingerprint"]:
        raise RequestError("binding_changed")
    return number


def admit_request(conn, config, *, request_id: str, user_id: str,
                  logical_key: str, send_kind: str, status: str,
                  ignore_opt_out: bool = False, surface: str = "whatsapp") -> dict:
    """Validate immutable intent under the send ledger's writer lock.

    No caller may use this hook to bypass the ordinary delivery gate.
    `surface` is the caller's own delivery surface, and a request whose frozen
    destination is another surface is refused, so a room or SMS question can
    never be admitted by a WhatsApp send nor the other way round. A room
    admission also returns the room it re-resolved, and an SMS admission the
    number, so the caller sends to the same read the fingerprint was checked
    against. Only `status="pending"` moves the request; any other value checks
    and returns the stored bodies without writing.
    """
    row = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id=?", (request_id,)).fetchone()
    if (row is None or row["state"] != "queued" or row["recipient_user_id"] != user_id
            or logical_key != ("relay-question:" + row["relay_id"] if row["relay_id"] else "skill-whatsapp:" + request_id) or ignore_opt_out):
        raise RequestError("request_unavailable")
    if row["queue_deadline"] is None or row["queue_deadline"] <= db.sql_datetime_now():
        raise RequestError("queue_expired")
    if user_id not in config.users:
        raise RequestError("recipient_unavailable")
    relay = None
    if row["relay_id"]:
        relay = conn.execute("SELECT * FROM message_relays WHERE id=?", (row["relay_id"],)).fetchone()
    # A self-send is always WhatsApp; a question goes where its relay froze it.
    destination_kind = relay["surface"] if relay is not None else ("whatsapp" if row["kind"] == "self_send" else None)
    if destination_kind != surface or (row["provider"] == "room") != (surface == "room"):
        raise RequestError("request_unavailable")
    room = number = None
    if surface == "room":
        from . import relay_destinations
        room = relay_destinations.check_room(conn, config, recipient_user_id=user_id,
                                             fingerprint=row["binding_fingerprint"])
    elif surface == "sms":
        number = _check_sms_number(config, row)
    else:
        _check_binding(conn, config, row)
    if row["kind"] == "relay_question":
        from . import message_relays
        if (relay is None or relay["state"] != "queued" or not row["approved_at"]
                or row["approved_digest"] != row["preview_digest"]
                or text_hash(row["preview"] or "") != row["approved_digest"]
                or relay["expires_at"] <= db.sql_datetime_now()
                or relay["request_id"] != row["id"] or relay["asker_user_id"] != row["requester_user_id"]
                or relay["recipient_user_id"] != user_id or row["requester_user_id"] not in config.users
                or relay["provider"] != row["provider"] or relay["binding_fingerprint"] != row["binding_fingerprint"]
                or message_relays.is_blocked(conn, actor_user_id=user_id, asker_user_id=row["requester_user_id"])):
            raise RequestError("request_unavailable")
        task = db.get_task(conn, row["origin_task_id"]) if row["origin_task_id"] else None
        origin = json.loads(relay["origin"])
        if (task is None or task.user_id != row["requester_user_id"] or task.source_type != origin["surface"]
                or task.conversation_token not in (origin["channel"], origin.get("room_token"))):
            raise RequestError("unsupported_origin")
        message_relays.validate_origin(conn, config, actor_user_id=row["requester_user_id"], origin=origin)
    elif row["requester_user_id"] != user_id:
        raise RequestError("request_unavailable")
    if not row["service_body"] or text_hash(row["service_body"]) != row["service_hash"]:
        raise RequestError("invalid_rendering")
    template = row["template_body"]
    if template is not None and text_hash(template) != row["template_hash"]:
        raise RequestError("invalid_rendering")
    if status == "pending" and send_kind == "template" and template is None:
        raise RequestError("template_unavailable")
    if status == "pending":
        conn.execute("UPDATE whatsapp_skill_requests SET state='sending',updated_at=datetime('now') WHERE id=?",
                     (request_id,))
        if row["relay_id"]:
            conn.execute("UPDATE message_relays SET state='sending' WHERE id=? AND state='queued'", (row["relay_id"],))
    admitted = {"service": row["service_body"], "template": template or ""}
    if room is not None:
        admitted.update(room_token=room["room_token"], talk_ref=room["talk_ref"])
    if number is not None:
        admitted["number"] = number
    return admitted


def admit_sms_question(conn, config, *, relay_id: str, user_id: str, status: str) -> dict:
    """`admit_request` for the SMS ledger, which keys a question by its relay."""
    row = conn.execute("SELECT id FROM whatsapp_skill_requests WHERE relay_id=? AND kind='relay_question'",
                       (relay_id,)).fetchone()
    if row is None:
        raise RequestError("request_unavailable")
    return admit_request(conn, config, request_id=row["id"], user_id=user_id,
                         logical_key="relay-question:" + relay_id, send_kind="service",
                         status=status, surface="sms")


def request_destination(config, *, request_id: str, user_id: str, caps) -> str:
    """Check and address the same binding snapshot after the ledger claim."""
    from .transport.whatsapp.outbound import _destination

    with db.get_db(config.db_path) as conn:
        row = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id=? AND recipient_user_id=?",
                           (request_id, user_id)).fetchone()
        if row is None:
            raise RequestError("request_unavailable")
        return _destination(_check_binding(conn, config, row), caps)


def _finish_request(config, request_id: str, *, record=None, reason: str | None = None) -> None:
    from .transport.sms._types import REACHED_PROVIDER, SmsDeliveryRecord
    from .transport.whatsapp._types import REACHED_META
    from .notification_resolvers import task_alert
    from .transport._alerts import push_off_surface

    state = "failed"
    if record is not None:
        reached = REACHED_PROVIDER if isinstance(record, SmsDeliveryRecord) else REACHED_META
        if record.status in reached:
            state = "sent"
        elif record.status in ("pending", "unknown"):
            state = "uncertain"
        reason = record.error_code or (None if state == "sent" else "unknown" if state == "uncertain" else record.status)
    if reason == "queue_expired":
        state = "expired"
    notice = None
    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            return
        if record is not None and record.status == "pending":
            # A competing poller can observe a live claim. Only the stale
            # recovery path may call that an uncertain outcome.
            stale = conn.execute("SELECT ? <= datetime('now', ?)",
                                 (row["updated_at"], f"-{CLAIM_RECOVERY_SECONDS} seconds")).fetchone()[0]
            if not stale:
                return
        changed = conn.execute(
            "UPDATE whatsapp_skill_requests SET state=?,error_code=?,closed_at=datetime('now'),"
            "updated_at=datetime('now') WHERE id=? AND state IN ('queued','sending','uncertain')",
            (state, reason, request_id),
        ).rowcount
        if changed and row["relay_id"]:
            from . import message_relays
            if state in ("sent", "uncertain"):
                if conn.execute("UPDATE message_relays SET state=? WHERE id=? AND state IN ('queued','sending','uncertain')",
                                ("waiting" if state == "sent" else "uncertain", row["relay_id"])).rowcount:
                    # A phone question is its own push, so this only writes.
                    message_relays.write_recipient_notice(conn, row["relay_id"])
            else:
                message_relays._close_relay(conn, row["relay_id"], state=state, reason=reason or "delivery_failed")
        if changed and row["relay_id"] and state == "uncertain":
            from .notification_resolvers.message_relay import write

            write(conn, conn.execute("SELECT * FROM message_relays WHERE id=?", (row["relay_id"],)).fetchone())
        if changed and state != "sent" and not row["relay_id"]:
            notice = task_alert.write(
                conn, row["requester_user_id"], dedup_key="whatsapp-request:" + request_id,
                title="WhatsApp request " + state,
                body=f"WhatsApp request {request_id} is {state}. Check its status before trying again.",
                params={"request_id": request_id, "status": state},
            )
    push_off_surface(config, notice, exclude_surface="whatsapp", reference_prefix="whatsapp-request")


def reconcile_self_send_delivery(conn, *, logical_key: str, status: str, error_code=None) -> None:
    """Follow an applied ledger receipt without admitting another provider attempt."""
    if not logical_key.startswith("skill-whatsapp:"):
        return
    if status == "failed":
        state, reason = "failed", error_code or "delivery_failed"
    elif status in ("accepted", "sent", "delivered", "read"):
        state, reason = "sent", None
    else:
        return
    conn.execute(
        "UPDATE whatsapp_skill_requests SET state=?,error_code=?,closed_at=datetime('now'),"
        "updated_at=datetime('now') WHERE id=? AND kind='self_send' "
        "AND state IN ('sending','sent','uncertain')",
        (state, reason, logical_key.removeprefix("skill-whatsapp:")),
    )


def _pending_requests(config, limit: int) -> list[dict]:
    with db.get_db(config.db_path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM whatsapp_skill_requests WHERE "
            "(state='queued' OR (state='sending' AND (updated_at <= datetime('now', ?) "
            "OR EXISTS (SELECT 1 FROM sent_whatsapp s WHERE s.logical_key=CASE WHEN kind='self_send' THEN 'skill-whatsapp:' || whatsapp_skill_requests.id ELSE 'relay-question:' || relay_id END "
            "AND s.status <> 'pending') "
            "OR EXISTS (SELECT 1 FROM sent_sms s WHERE s.logical_key='relay-question:' || relay_id "
            "AND s.status <> 'pending')))) "
            "ORDER BY created_at,id LIMIT ?", (f"-{CLAIM_RECOVERY_SECONDS} seconds", limit),
        )]


async def drain_requests(config, *, limit: int = 20) -> int:
    """Bounded daemon-only poll; SQLite work stays off the shared event loop."""
    from .transport.whatsapp.outbound import deliver_whatsapp

    rows = await asyncio.to_thread(_pending_requests, config, max(0, min(limit, 100)))
    for row in rows:
        if row["kind"] in ROOM_KINDS:
            from . import side_rooms
            await side_rooms.deliver_request(config, row)
            continue
        if row["relay_id"]:
            from . import message_relays
            await message_relays.deliver_question(config, row)
            continue
        try:
            record = await deliver_whatsapp(
                config, logical_key=logical_key(row), user_id=row["recipient_user_id"],
                text="", task_id=None, request_id=row["id"],
            )
        except RequestError as exc:
            await asyncio.to_thread(_finish_request, config, row["id"], reason=str(exc))
        else:
            await asyncio.to_thread(_finish_request, config, row["id"], record=record)
    from .message_relays import reconcile_reply_candidates
    from .transport.whatsapp.webhook import deliver_event_responses

    replies = await asyncio.to_thread(reconcile_reply_candidates, config, limit=limit)
    await deliver_event_responses(config, replies)
    from .message_relays import poll_relays

    await poll_relays(config, limit=limit)
    return len(rows)


def _question_response(conn, row, *, approval: str | None = None) -> dict:
    result = {"status": row["state"], "request_id": row["id"], "relay_id": row["relay_id"],
              "delivery_status": "pending" if row["state"] == "queued" else row["state"]}
    if row["state"] == "held":
        result.update(needs_confirmation=True, preview=row["preview"])
    relay = conn.execute("SELECT approval FROM message_relays WHERE id=?", (row["relay_id"],)).fetchone()
    if relay is not None and relay["approval"]:
        result["approval"] = relay["approval"]
    elif approval:
        result["approval"] = approval
    return result


# The surfaces whose `tasks.prompt` is the sender's own text; stage 7 of the
# relay spec checked each producer. A task from anywhere else is held.
_CLEAN_TURN_SURFACES = frozenset({"web", "talk", "whatsapp", "sms"})


def _names_recipient(config, task, recipient_user_id: str) -> bool:
    """Whether the task's own prompt names the recipient, whole-word.

    Only `tasks.prompt`: never conversation context, memory or attachments.
    Attachment file names are cut out first, because a web send with no typed
    text stores a stand-in prompt that lists its files.
    """
    prompt = task.prompt or ""
    for path in task.attachments or []:
        name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
        if name:
            prompt = re.sub(re.escape(name), " ", prompt, flags=re.IGNORECASE)
    user = config.users.get(recipient_user_id)
    for name in (recipient_user_id, getattr(user, "display_name", "") or ""):
        name = name.strip()
        # A one-character id would be matched by an article.
        if len(name) < 2:
            continue
        if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", prompt, flags=re.IGNORECASE):
            return True
    return False


_QUOTED_SPAN = re.compile(r'"([^"\n]+)"|“([^”\n]+)”')


def _quotes_prompt(task, text: str) -> bool:
    """Whether ``text`` is the member's own words, as a unit they wrote.

    The whole prompt, one whole line of it, or one whole double-quoted span,
    compared after stripping; attachment names cut out first as
    `_names_recipient` does. Never a substring: a fragment can reverse what
    was said ("do not tell them X" contains "X"), and a side-room task reads
    the parent's transcript, so its first call can be shaped by other people.
    """
    wanted = text.strip()
    if not wanted:
        return False
    prompt = task.prompt or ""
    for path in task.attachments or []:
        name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
        if name:
            prompt = prompt.replace(name, " ")
    units = {prompt.strip()}
    units.update(line.strip() for line in prompt.splitlines())
    for match in _QUOTED_SPAN.finditer(prompt):
        units.add((match.group(1) or match.group(2) or "").strip())
    return wanted in units


def _clean_turn(conn, config, task, recipient_user_id: str, *, post_text: str | None = None) -> bool:
    """ISSUE-565: skip the asker's approval only for a turn the daemon can vouch for.

    The recipient is named in this task's own prompt, and this ask is the
    attempt's first and only tool call so far, so nothing the task read in
    this attempt can have shaped it. The executor writes the count as it reads
    each call; a call not yet counted reads as zero, which is held.

    A `room_post` (``post_text`` set) takes the same rule with one test
    swapped: the text to post must be the member's own words, verbatim in
    their prompt, rather than the prompt naming a recipient (multiplayer D4).
    """
    if task.source_type not in _CLEAN_TURN_SURFACES:
        return False
    # A confirmed re-run counts from zero, but its prompt carries the previous
    # attempt's output ("Execute the action you proposed"), which that attempt's
    # tool calls may have shaped.
    if task.confirmed_at or (task.confirmation_prompt or "").strip():
        return False
    calls, first_is_relay = db.get_attempt_tool_calls(conn, task.id)
    if calls != 1 or not first_is_relay:
        return False
    if post_text is not None:
        return _quotes_prompt(task, post_text)
    return _names_recipient(config, task, recipient_user_id)


def _queue_question(conn, *, request_id: str, relay_id: str | None, digest: str, approval: str) -> None:
    """Release a held request to the delivery queue, recording who approved it.

    A `room_post` has no relay row; its approval is the request's own
    `approved_digest`, which delivery checks against the preview.
    """
    from . import message_relays
    conn.execute(
        "UPDATE whatsapp_skill_requests SET state='queued',approved_at=datetime('now'),approved_digest=?,"
        "queue_deadline=datetime('now',?),updated_at=datetime('now') WHERE id=?",
        (digest, f"+{QUEUE_DEADLINE_SECONDS} seconds", request_id),
    )
    if relay_id:
        conn.execute(
            "UPDATE message_relays SET state='queued',approved_at=datetime('now'),expires_at=datetime('now',?),"
            "approval=? WHERE id=? AND state='held'",
            (f"+{message_relays.RELAY_LIFETIME_SECONDS} seconds", approval, relay_id),
        )


def hold_question(conn, config, *, actor_user_id: str, task_id: int,
                  recipient_user_id: str, request_key: str, text: str,
                  via: str | None = None) -> dict:
    from . import message_relays, relay_destinations

    _validate_input(request_key, text)
    task = db.get_task(conn, task_id)
    if task is None or task.user_id != actor_user_id or task.status != "running":
        raise RequestError("task_unavailable")
    if recipient_user_id == actor_user_id:
        raise RequestError("use_send")
    if via is not None and via not in relay_destinations.KINDS:
        raise RequestError("invalid_via")
    existing = conn.execute(
        "SELECT * FROM whatsapp_skill_requests WHERE requester_user_id=? AND origin_task_id=? AND request_key=?",
        (actor_user_id, task_id, request_key),
    ).fetchone()
    if existing:
        if existing["content_hash"] != text_hash(json.dumps(["relay_question", recipient_user_id, text], ensure_ascii=True)):
            raise RequestError("request_conflict")
        return _question_response(conn, existing)
    # Asking is open by default within the installation (ISSUE-566). A block is
    # the one cause kept behind the generic code, so it is never revealed; it is
    # checked before the destination, so a blocked asker learns nothing about
    # where the recipient could have been reached.
    if recipient_user_id not in config.users:
        raise RequestError("unknown_user")
    if message_relays.is_blocked(conn, actor_user_id=recipient_user_id, asker_user_id=actor_user_id):
        raise RequestError("recipient_unavailable")
    if task.is_group_chat or task.parent_task_id or task.command or task.skill or task.scheduled_job_id:
        raise RequestError("unsupported_origin")
    origin = message_relays.private_origin(conn, config, actor_user_id=actor_user_id,
                                           surface=task.source_type, conversation_token=task.conversation_token)
    # No Talk audience call here: this runs in the skill subprocess, which holds
    # no Nextcloud credential. present_question checks it before the preview.
    with write_transaction(conn):
        message_relays.validate_origin(conn, config, actor_user_id=actor_user_id, origin=origin)
        destination = relay_destinations.resolve_destination(
            conn, config, recipient_user_id=recipient_user_id, requested=via)
        relay_id = str(uuid.uuid4())
        display = relay_destinations.display_name(config, actor_user_id)
        wording = relay_destinations.render_question(
            config, asker=actor_user_id, text=text, destination=destination, relay_id=relay_id)
        service, template = relay_destinations.fit_question(config, destination, wording)
        preview = (f"Send a question to {relay_destinations.label_text(recipient_user_id)} through {destination['label']}?\n"
                   f"Answer returns to {origin['surface']}:{origin['channel']}.\n"
                   "Expires 24 hours after approval; sending must start within 10 minutes.\n\n")
        if destination["kind"] == "whatsapp":
            preview += (f"Service message:\n{service}\n\n"
                        f"Template message:\n{template if template is not None else '(unavailable)'}")
        else:
            preview += f"Message:\n{service}"
        # Phone previews must fit intact. No approval of a shortened preview.
        if origin["surface"] == "sms":
            from .transport.sms.outbound import render_sms
            if render_sms(preview, config.sms.max_segments).text != preview:
                raise RequestError("invalid_preview")
        if origin["surface"] == "whatsapp":
            from .transport.whatsapp.outbound import active_adapter
            adapter = active_adapter(config)
            if adapter is None or len(preview) > adapter.caps.service_body_limit:
                raise RequestError("invalid_preview")
        row = _store_request(
            conn, actor_user_id=actor_user_id, task_id=task_id, request_key=request_key,
            kind="relay_question", recipient_user_id=recipient_user_id, text=text,
            service_body=service, template_body=template, provider=destination["provider"],
            binding_fingerprint=destination["fingerprint"], preview=preview, relay_id=relay_id,
            relay_snapshot={"origin": origin, "audience": [actor_user_id], "asker_display": display,
                            "destination": relay_destinations.stored_destination(destination)},
        )
        # Decided after the store, which is where the reservation, the caps and
        # the pending-confirmation check ran, and inside the same transaction.
        # The preview is stored either way, so the audit trail matches a held
        # question's.
        if row["state"] == "held" and _clean_turn(conn, config, task, recipient_user_id):
            _queue_question(conn, request_id=row["id"], relay_id=row["relay_id"],
                            digest=row["preview_digest"], approval="clean_turn")
            row = dict(conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id=?",
                                    (row["id"],)).fetchone())
        return _question_response(conn, row)


def park_question(conn, config, *, task) -> dict | None:
    """Persist a deterministic preview and its exact association together."""
    from . import message_relays
    with write_transaction(conn):
        row = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE origin_task_id=? AND state='held'", (task.id,)).fetchone()
        if row is None:
            return None
        current = db.get_task(conn, task.id)
        cancelled = conn.execute("SELECT cancel_requested FROM tasks WHERE id=?", (task.id,)).fetchone()
        if current is not None and current.status == "running" and cancelled and cancelled[0]:
            db.cancel_task(conn, task.id)
            return None
        if current is None or current.status != "running":
            message_relays.close_task_questions(conn, task.id)
            return None
        message_relays.validate_origin(conn, config, actor_user_id=task.user_id,
                                       origin=request_origin(conn, row))
        db.set_task_confirmation(conn, task.id, row["preview"])
        associate_confirmation(conn, actor_user_id=task.user_id, task_id=task.id,
                               request_id=row["id"], preview_digest=row["preview_digest"])
        return dict(row)


def approve_request(conn, *, task, request_id: str, preview_digest: str) -> None:
    """Only the currently displayed immutable request receives authority."""
    from . import message_relays
    with write_transaction(conn):
        current = db.get_task(conn, task.id)
        row = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id=?", (request_id,)).fetchone()
        if (current is None or row is None or current.user_id != task.user_id
                or current.status != "pending_confirmation" or current.whatsapp_confirmation_request_id != request_id
                or row["requester_user_id"] != task.user_id or row["origin_task_id"] != task.id
                or row["state"] != "held" or row["preview_digest"] != preview_digest
                or text_hash(current.confirmation_prompt or "") != preview_digest
                or text_hash(row["preview"] or "") != preview_digest):
            raise RequestError("confirmation_unavailable")
        if row["relay_id"] and message_relays.is_blocked(
                conn, actor_user_id=row["recipient_user_id"], asker_user_id=task.user_id):
            raise RequestError("recipient_unavailable")
        _queue_question(conn, request_id=request_id, relay_id=row["relay_id"],
                        digest=preview_digest, approval="user")
        conn.execute("UPDATE tasks SET whatsapp_confirmation_request_id=NULL WHERE id=?", (task.id,))


async def present_question(config, *, task, success: bool) -> bool:
    """Scheduler's private confirmation path; True means it consumed the turn.

    Keep this separate from result routing: output overrides, mirrors and log
    subscribers must never acquire the persisted preview.
    """
    from . import message_relays
    from .events import EventWriter
    from .notification_resolvers import confirmation, task_alert

    with db.get_db(config.db_path) as conn:
        row = held_question(conn, task.id)
    if row is None:
        return False
    if not success:
        with db.get_db(config.db_path) as conn:
            message_relays.close_task_questions(conn, task.id, reason="attempt_failed")
        return False
    origin = json.loads(row["origin"])
    post = row["kind"] == "room_post"
    title = ("Room post awaiting approval" if post
             else "Private relay question awaiting approval")
    try:
        await message_relays.verify_private_audience(config, actor_user_id=task.user_id, origin=origin)
        with db.get_db(config.db_path) as conn:
            parked = park_question(conn, config, task=task)
            if parked is None:
                current = db.get_task(conn, task.id)
                if current is not None and current.status == "cancelled":
                    raise RequestError("cancelled")
                return True
            confirmation.write(conn, task.user_id, task_id=task.id,
                               title=title,
                               body=("Open the side room to review this post." if post else
                                     "Open the private conversation to review this relay question."),
                               room_token=origin.get("room_token"))
            db.drop_pending_steers(conn, task.id)
        # Check again at delivery, after releasing the parking transaction.
        await message_relays.verify_private_audience(config, actor_user_id=task.user_id, origin=origin)
        with db.get_db(config.db_path) as conn:
            message_relays.validate_origin(conn, config, actor_user_id=task.user_id, origin=origin)
            current = db.get_task(conn, task.id)
            if current is None or current.status != "pending_confirmation" or current.whatsapp_confirmation_request_id != parked["id"]:
                return True
        writer = EventWriter(task.id, config.db_path)
        if origin["surface"] == "web":
            # No subscribers on this writer: only the task's authenticated SSE
            # can read this event. Log and push subscribers receive no preview.
            writer.emit("confirmation", {"prompt": parked["preview"]})
        else:
            from .transport import make_registry
            delivery_config = config
            if origin["surface"] == "whatsapp":
                # A preview is an exact approval document. A Cloud template
                # can flatten or truncate it, so a closed window blocks it.
                from dataclasses import replace
                delivery_config = replace(config, whatsapp=replace(config.whatsapp,
                    cloud=replace(config.whatsapp.cloud, proactive_template=replace(
                        config.whatsapp.cloud.proactive_template, enabled=False))))
            transport = make_registry(delivery_config).get(origin["surface"])
            if transport is None:
                raise RequestError("unsupported_origin")
            await transport.deliver(origin["channel"], parked["preview"], task=task,
                                    reference_id=f"relay-preview:{parked['id']}")
            writer.emit("confirmation", {"prompt": title + "."})
        writer.emit("done", {"stop_reason": "completed", "duration_seconds": 0})
        writer.finish()
    except RequestError as exc:
        with db.get_db(config.db_path) as conn:
            db.cancel_task(conn, task.id)
            confirmation.resolve_for_task(conn, task.user_id, task.id, by="system")
            if str(exc) != "cancelled":
                task_alert.write(conn, task.user_id, dedup_key=f"relay-preview:{row['id']}",
                                 title="Relay question cancelled", body="The private origin is no longer available.")
        writer = EventWriter(task.id, config.db_path)
        writer.emit("cancelled")
        writer.emit("done", {"stop_reason": "cancelled", "duration_seconds": 0})
    return True


def request_origin(conn, row) -> dict:
    """The frozen private origin of a held request: the relay's, or its own."""
    if row["relay_id"]:
        relay = conn.execute("SELECT origin FROM message_relays WHERE id=?", (row["relay_id"],)).fetchone()
        return json.loads(relay["origin"]) if relay and relay["origin"] else {}
    return json.loads(row["origin"] or "{}")


def held_question(conn, task_id: int) -> dict | None:
    """The task's held relay question or room post, with its frozen origin."""
    row = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE origin_task_id=? AND state='held'",
                       (task_id,)).fetchone()
    if row is None or row["kind"] not in _HELD_KINDS:
        return None
    held = dict(row)
    held["origin"] = json.dumps(request_origin(conn, row))
    return held
