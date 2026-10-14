"""Explicit owner location disclosures through existing room authority and posts."""

import json
import sqlite3
import uuid
from datetime import timedelta, timezone

from istota import db, location
from istota.location.logic import location_snapshot
from istota.relay.requests import RequestError, _question_response, _validate_input, write_transaction
from istota.rooms import private_replies
from istota.rooms.scopes import canonical_token, is_current_member


def _interactive_task(conn, config, *, actor_user_id, task_id):
    task = private_replies._owned_running_task(
        conn, actor_user_id=actor_user_id, task_id=task_id)
    if actor_user_id not in config.users:
        raise RequestError("task_unavailable")
    if (task.source_type not in {"web", "talk", "whatsapp", "sms"}
            or task.parent_task_id or task.command or task.skill or task.scheduled_job_id
            or task.guest_participant_id is not None):
        raise RequestError("unsupported_origin")
    own = canonical_token(conn, task.conversation_token)
    message = conn.execute(
        "SELECT m.*, p.kind AS participant_kind, p.user_id AS participant_user_id "
        "FROM messages m LEFT JOIN room_participants p ON p.id=m.author_participant_id "
        "WHERE m.task_id=? AND m.role='user' ORDER BY m.id LIMIT 1", (task.id,),
    ).fetchone()
    if (message is None or message["author_user_id"] != actor_user_id
            or message["room_token"] != own
            or message["origin_surface"] != task.source_type
            or (message["author_participant_id"] is not None and (
                message["participant_kind"] != "principal"
                or message["participant_user_id"] != actor_user_id))
            or not own or not is_current_member(conn, own, actor_user_id)):
        raise RequestError("task_unavailable")
    return task


def _snapshot_text(snapshot):
    from istota.confirmations import flatten

    label = flatten(snapshot["owner_user_id"])
    lines = [f"{label}: Location observed at {snapshot['observed_at']}"]
    if snapshot["freshness"] == "last_known":
        lines.append("Last known location")
    if snapshot["precision"] == "area":
        lines.append("Approximate area")
    else:
        lines.append(f"Accuracy: {snapshot['accuracy_m']:g} m")
    lines.append(f"Age when prepared: {snapshot['age_seconds']:g} seconds")
    if snapshot["observation_kind"] == "declared":
        lines.append("Declared observation")
    if snapshot["freshness"] == "fresh_clock_skew":
        lines.append("Observation clock is slightly ahead")
    lines.append(snapshot["map_url"])
    return "\n".join(lines)


def share_location_snapshot(conn, config, *, actor_user_id, task_id, request_key,
                            now, room=None, precision="exact", allow_last_known=False):
    """Return the owner's projection here, or hold one frozen post from private chat.

    Request context travels in the existing immutable post destination, so a
    retry can check its options without sampling the owner's tracker again.
    The caller owns the framework transaction.
    """
    _validate_input(request_key, "location snapshot")
    if precision not in {"exact", "area"}:
        raise RequestError("invalid_precision")
    task = _interactive_task(conn, config, actor_user_id=actor_user_id, task_id=task_id)
    own = canonical_token(conn, task.conversation_token)
    shared = private_replies.is_shared_room(conn, own, is_group_chat=task.is_group_chat)
    if shared:
        target = private_replies._live_shared_room(conn, task, actor_user_id)
        if room and private_replies._post_target(conn, task, actor_user_id, room) != target:
            raise RequestError("private_turn_required")
    else:
        if not db.is_private_room_of(conn, own, actor_user_id, allow_phone=True):
            raise RequestError("not_a_private_room")
        target = private_replies._post_target(conn, task, actor_user_id, room)
    private_replies._post_destination(conn, target, actor_user_id)
    context = {"kind": "location_snapshot", "precision": precision,
               "allow_last_known": allow_last_known}
    if not shared:
        existing = conn.execute(
            "SELECT * FROM whatsapp_skill_requests WHERE requester_user_id=? "
            "AND origin_task_id=? AND request_key=?", (actor_user_id, task_id, request_key),
        ).fetchone()
        if existing is not None:
            destination = json.loads(existing["destination"] or "{}")
            if (existing["kind"] != "room_post" or destination.get("room_token") != target
                    or destination.get("request_context") != context):
                raise RequestError("request_conflict")
            return _question_response(conn, existing)
    try:
        owner = location.resolve_for_user(actor_user_id, config, conn=conn)
        # Setup normally creates this database. Missing collection data is not
        # a reason to create a file or disclose its storage path here.
        if not owner.db_path.is_file():
            return {"error": "no_fresh_location"}
        snapshot = location_snapshot(
            owner.db_path, now=now, precision=precision,
            accuracy_threshold_m=config.location.accuracy_threshold_m,
            allow_last_known=allow_last_known,
        )
    except location.UserNotFoundError:
        raise RequestError("location_unavailable") from None
    except sqlite3.Error:
        raise RequestError("location_temporarily_unavailable") from None
    if "error" in snapshot:
        return snapshot
    snapshot["owner_user_id"] = actor_user_id
    if shared:
        return {"delivery": "reply", "snapshot": snapshot}
    return private_replies.hold_room_post(
        conn, config, actor_user_id=actor_user_id, task_id=task_id,
        request_key=request_key, room=target, text=_snapshot_text(snapshot),
        request_context=context, require_confirmation=True,
    )


def _grant_timestamp(now):
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _expire_location_grants(conn, *, now):
    conn.execute(
        "UPDATE location_share_grants SET state='expired', closed_at=?, close_reason='expired' "
        "WHERE state='active' AND expires_at<=?",
        (_grant_timestamp(now), _grant_timestamp(now)),
    )


def _hold_location_grant(conn, *, owner_user_id, origin_task_id, request_key,
                         room_token, precision, duration_seconds, audience,
                         binding_fingerprint, room_epoch, now):
    """Persist validated terms; only the future private-approval service calls this."""
    _validate_input(request_key, "location share")
    if precision not in {"exact", "area"}:
        raise RequestError("invalid_precision")
    if type(duration_seconds) is not int or not 300 <= duration_seconds <= 86400:
        raise RequestError("invalid_duration")
    with write_transaction(conn):
        row = conn.execute(
            "SELECT * FROM location_share_grants WHERE owner_user_id=? "
            "AND origin_task_id=? AND request_key=?",
            (owner_user_id, origin_task_id, request_key),
        ).fetchone()
        if row is not None:
            if (row["room_token"] != room_token or row["precision"] != precision
                    or row["duration_seconds"] != duration_seconds):
                raise RequestError("request_conflict")
            return dict(row)
        ident = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO location_share_grants "
            "(id,owner_user_id,origin_task_id,request_key,room_token,precision,duration_seconds,"
            "audience_json,binding_fingerprint,room_epoch,state,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'held',?)",
            (ident, owner_user_id, origin_task_id, request_key, room_token, precision,
             duration_seconds, json.dumps(audience), binding_fingerprint, room_epoch,
             _grant_timestamp(now)),
        )
        return dict(conn.execute("SELECT * FROM location_share_grants WHERE id=?", (ident,)).fetchone())


def _activate_location_grant(conn, *, grant_id, now):
    """Internal state transition after approval validates identity and frozen terms."""
    with write_transaction(conn):
        _expire_location_grants(conn, now=now)
        row = conn.execute("SELECT * FROM location_share_grants WHERE id=?", (grant_id,)).fetchone()
        if row is None or row["state"] not in {"held", "active"}:
            raise RequestError("grant_unavailable")
        if row["state"] == "active":
            return dict(row)
        conn.execute(
            "UPDATE location_share_grants SET state='revoked', closed_at=?, close_reason='replaced' "
            "WHERE owner_user_id=? AND room_token=? AND state='active'",
            (_grant_timestamp(now), row["owner_user_id"], row["room_token"]),
        )
        conn.execute(
            "UPDATE location_share_grants SET state='active', approved_at=?, expires_at=? WHERE id=?",
            (_grant_timestamp(now), _grant_timestamp(now + timedelta(seconds=row["duration_seconds"])), grant_id),
        )
        return dict(conn.execute("SELECT * FROM location_share_grants WHERE id=?", (grant_id,)).fetchone())


def list_location_shares(conn, *, actor_user_id, room_token=None, now):
    with write_transaction(conn):
        _expire_location_grants(conn, now=now)
        rows = conn.execute(
            "SELECT id,room_token,precision,duration_seconds,state,created_at,approved_at,expires_at "
            "FROM location_share_grants WHERE owner_user_id=? AND state IN ('held','active') "
            "AND (? IS NULL OR room_token=?) ORDER BY created_at,id",
            (actor_user_id, room_token, room_token),
        ).fetchall()
        return {"shares": [dict(row) for row in rows]}


def revoke_location_shares(conn, *, actor_user_id, room_token=None, all_rooms=False, now):
    if (not room_token and not all_rooms) or (room_token and all_rooms):
        raise RequestError("no_target_room")
    with write_transaction(conn):
        _expire_location_grants(conn, now=now)
        rows = conn.execute(
            "SELECT id,request_id FROM location_share_grants WHERE owner_user_id=? "
            "AND state IN ('held','active') AND (? IS NULL OR room_token=?)",
            (actor_user_id, room_token, room_token),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE location_share_grants SET state=CASE WHEN state='held' THEN 'cancelled' "
                "ELSE 'revoked' END, closed_at=?, close_reason='owner_stop' WHERE id=?",
                (_grant_timestamp(now), row["id"]),
            )
            conn.execute(
                "UPDATE whatsapp_skill_requests SET state='cancelled', error_code='owner_stop', "
                "closed_at=?, updated_at=? WHERE id=? AND state='held'",
                (_grant_timestamp(now), _grant_timestamp(now), row["request_id"]),
            )
        return {"stopped": len(rows)}
