"""Explicit owner location disclosures through existing room authority and posts."""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

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


# The projected fields a map card carries, beside its owner's label and id.
_CARD_FIELDS = ("precision", "lat", "lon", "accuracy_m", "observed_at", "freshness", "map_url")
MAX_CARDS_PER_MESSAGE = 3


def _owner_label(config, owner_user_id):
    from istota.confirmations import flatten

    user = config.users.get(owner_user_id)
    return flatten((user.display_name if user else "") or owner_user_id)


# Every key of a card's stored observation: a message's card element minus `card_id`.
CARD_OBSERVATION_KEYS = frozenset(("owner_user_id", "owner_label") + _CARD_FIELDS)


def _card_observation(config, snapshot):
    """The projection a card draws, which the text already discloses, and nothing else."""
    observation = {"owner_user_id": snapshot["owner_user_id"],
                   "owner_label": _owner_label(config, snapshot["owner_user_id"])}
    observation.update({key: snapshot[key] for key in _CARD_FIELDS})
    return observation


def _insert_card(conn, *, task_id, actor_user_id, room_token, observation,
                 grant_id=None, request_id=None):
    return conn.execute(
        "INSERT INTO location_share_cards "
        "(task_id, request_id, owner_user_id, actor_user_id, room_token, grant_id, "
        " observation, map_url) VALUES (?,?,?,?,?,?,?,?) RETURNING id",
        (task_id, request_id, observation["owner_user_id"], actor_user_id, room_token,
         grant_id, json.dumps(observation), observation["map_url"]),
    ).fetchone()[0]


def _record_card(conn, config, *, task_id, actor_user_id, room_token, snapshot, grant_id=None):
    """Record a map card for one share result, on the caller's transaction.

    It reaches a room only if the task's answer carries its `map_url`
    (`location_cards_for_answer`).
    """
    _insert_card(conn, task_id=task_id, actor_user_id=actor_user_id, room_token=room_token,
                 observation=_card_observation(config, snapshot), grant_id=grant_id)


def record_post_card(conn, *, request_id, task_id, actor_user_id, room_token, observation):
    """Record a released location post's card, on `_claim`'s transaction.

    The observation is the one frozen into the held request and bound by its
    approved preview. Returns the message's card element.
    """
    card_id = _insert_card(conn, task_id=task_id, actor_user_id=actor_user_id,
                           room_token=room_token, observation=observation,
                           request_id=request_id)
    return {"card_id": card_id, **observation}


def share_location_snapshot(conn, config, *, actor_user_id, task_id, request_key,
                            now, room=None, precision="exact", allow_last_known=False,
                            map_card=True):
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
        if map_card:
            _record_card(conn, config, task_id=task_id, actor_user_id=actor_user_id,
                         room_token=target, snapshot=snapshot)
        return {"delivery": "reply", "snapshot": snapshot}
    return private_replies.hold_room_post(
        conn, config, actor_user_id=actor_user_id, task_id=task_id,
        request_key=request_key, room=target, text=_snapshot_text(snapshot),
        request_context=context, require_confirmation=True,
        attachment=_card_observation(config, snapshot) if map_card else None,
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
    """Persist validated terms; only the private-approval service calls this."""
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
            "SELECT id,request_id,origin_task_id,state FROM location_share_grants WHERE owner_user_id=? "
            "AND state IN ('held','active') AND (? IS NULL OR room_token=?)",
            (actor_user_id, room_token, room_token),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE location_share_grants SET state=CASE WHEN state='held' THEN 'cancelled' "
                "ELSE 'revoked' END, closed_at=?, close_reason='owner_stop' WHERE id=?",
                (_grant_timestamp(now), row["id"]),
            )
            if row["state"] == "held" and row["origin_task_id"] is not None:
                from istota.relay.relays import close_task_questions
                close_task_questions(conn, row["origin_task_id"], reason="owner_stop")
            conn.execute(
                "UPDATE whatsapp_skill_requests SET state='cancelled', error_code='owner_stop', "
                "closed_at=?, updated_at=? WHERE id=? AND state='held'",
                (_grant_timestamp(now), _grant_timestamp(now), row["request_id"]),
            )
        return {"stopped": len(rows)}


def invalidate_room_location_shares(conn, room_token, *, reason="audience_changed"):
    """Invalidate consent without committing the caller's room mutation."""
    db._invalidate_room_location_shares(conn, room_token, reason=reason)


def _timed_room_terms(conn, config, room_token, owner_user_id):
    """Freeze actual readers separately from the owner's current membership."""
    from istota.transport import ParticipantRef
    from istota.transport.participants import is_the_bot

    destination = private_replies._post_destination(conn, room_token, owner_user_id)
    if not db.room_is_shared(conn, room_token):
        raise RequestError("not_a_shared_room")
    bindings = db.list_room_bindings(conn, room_token)
    if not bindings or any(b.surface not in {"web", "talk", "whatsapp"} for b in bindings):
        raise RequestError("unsupported_room")
    if any(b.surface == "whatsapp" for b in bindings):
        from istota.transport.whatsapp.groups import group_destination
        if config.whatsapp.provider != "baileys" or not group_destination(conn, room_token):
            raise RequestError("unsupported_room")
    readers = set(db.list_room_members(conn, room_token))
    for row in conn.execute(
            "SELECT * FROM room_participants WHERE room_token=? AND left_at IS NULL", (room_token,)):
        ref = ParticipantRef(surface=row["surface"], surface_ref=row["surface_ref"], user_id=row["user_id"])
        if is_the_bot(config, ref):
            readers.discard(row["user_id"])
            continue
        if row["kind"] != "principal" or row["user_id"] not in config.users:
            raise RequestError("audience_unavailable")
        readers.add(row["user_id"])
    if not readers or any(user not in config.users for user in readers):
        raise RequestError("audience_unavailable")
    # The post fingerprint covers transport identity; include every binding so
    # replacing a web handle cannot silently change a held grant's destination.
    fingerprint = private_replies._fingerprint(
        destination["fingerprint"], [(b.surface, b.surface_ref) for b in bindings])
    epoch = conn.execute("SELECT COALESCE(MAX(epoch),0) FROM room_epochs WHERE room_token=?",
                         (room_token,)).fetchone()[0]
    return destination, sorted(readers), fingerprint, epoch


def request_location_share(conn, config, *, actor_user_id, task_id, request_key,
                           duration_seconds, precision, now, room=None):
    from istota.confirmations import check_preview_delivery, flatten
    from istota.relay.requests import MAX_CONTENT_CHARS, _store_request

    _validate_input(request_key, "location share")
    if precision not in {"exact", "area"}:
        raise RequestError("invalid_precision")
    if type(duration_seconds) is not int or not 300 <= duration_seconds <= 86400:
        raise RequestError("invalid_duration")
    with write_transaction(conn):
        task = _interactive_task(conn, config, actor_user_id=actor_user_id, task_id=task_id)
        own = canonical_token(conn, task.conversation_token)
        if private_replies.is_shared_room(conn, own, is_group_chat=task.is_group_chat):
            target = private_replies._live_shared_room(conn, task, actor_user_id)
            if room and private_replies._post_target(conn, task, actor_user_id, room) != target:
                raise RequestError("private_turn_required")
        else:
            if not db.is_private_room_of(conn, own, actor_user_id, allow_phone=True):
                raise RequestError("not_a_private_room")
            target = private_replies._post_target(conn, task, actor_user_id, room)
        existing = conn.execute(
            "SELECT * FROM location_share_grants WHERE owner_user_id=? AND origin_task_id=? AND request_key=?",
            (actor_user_id, task_id, request_key),
        ).fetchone()
        if existing is not None:
            if (existing["room_token"] != target or existing["precision"] != precision
                    or existing["duration_seconds"] != duration_seconds):
                raise RequestError("request_conflict")
            req = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id=?", (existing["request_id"],)).fetchone()
            if req is None:
                raise RequestError("grant_unavailable")
            return _question_response(conn, req)
        try:
            location.resolve_for_user(actor_user_id, config, conn=conn)
        except location.UserNotFoundError:
            raise RequestError("location_unavailable") from None
        destination, audience, fingerprint, epoch = _timed_room_terms(conn, config, target, actor_user_id)
        readers = ", ".join(flatten(user) for user in audience)
        preview = (
            f"Share your location with {flatten(destination['label'])}?\n"
            f"Readers: {readers}\n"
            "Answers are visible to the whole room.\n"
            f"Precision: {precision} ({'exact coordinates' if precision == 'exact' else 'approximate area, rounded coordinates'}).\n"
            f"Duration: {duration_seconds / 60:g} minutes from approval.\n"
            "Members may request fresh observations supplied by your existing tracker. "
            "This does not start tracking or send periodic updates.\n"
            "Stop with room location-stop or ask me to stop sharing. "
            "Stopping prevents later reads; it cannot erase messages or screenshots already shared."
        )
        if len(preview) > MAX_CONTENT_CHARS:
            raise RequestError("invalid_preview")
        check_preview_delivery(conn, config, task, preview)
        grant = _hold_location_grant(
            conn, owner_user_id=actor_user_id, origin_task_id=task_id, request_key=request_key,
            room_token=target, precision=precision, duration_seconds=duration_seconds,
            audience=audience, binding_fingerprint=fingerprint, room_epoch=epoch, now=now)
        req = _store_request(
            conn, actor_user_id=actor_user_id, task_id=task_id, request_key=request_key,
            kind="location_share", recipient_user_id=actor_user_id, text=preview,
            service_body=preview, template_body=None, provider="room", preview=preview,
            binding_fingerprint=fingerprint, origin={"surface": task.source_type, "room_token": own},
            destination={"grant_id": grant["id"], "room_token": target, "precision": precision,
                         "duration_seconds": duration_seconds, "audience": audience,
                         "binding_fingerprint": fingerprint, "room_epoch": epoch},
        )
        conn.execute("UPDATE location_share_grants SET request_id=? WHERE id=?", (req["id"], grant["id"]))
        return _question_response(conn, req)


def approve_location_share(conn, config, *, grant_id, request_id, preview_digest,
                           actor_user_id, now):
    """Daemon-only authorization; cancelled terms return an error and stay closed."""
    from istota.relay.requests import text_hash

    with write_transaction(conn):
        grant = conn.execute("SELECT * FROM location_share_grants WHERE id=?", (grant_id,)).fetchone()
        req = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id=?", (request_id,)).fetchone()
        if (grant is None or req is None or grant["owner_user_id"] != actor_user_id
                or req["requester_user_id"] != actor_user_id or req["recipient_user_id"] != actor_user_id
                or req["kind"] != "location_share" or grant["request_id"] != request_id
                or grant["origin_task_id"] != req["origin_task_id"]
                or req["preview_digest"] != preview_digest or text_hash(req["preview"] or "") != preview_digest):
            raise RequestError("confirmation_unavailable")
        destination = json.loads(req["destination"] or "{}")
        expected = {"grant_id": grant_id, "room_token": grant["room_token"], "precision": grant["precision"],
                    "duration_seconds": grant["duration_seconds"], "audience": json.loads(grant["audience_json"]),
                    "binding_fingerprint": grant["binding_fingerprint"], "room_epoch": grant["room_epoch"]}
        if destination != expected or req["binding_fingerprint"] != grant["binding_fingerprint"]:
            raise RequestError("confirmation_unavailable")
        if req["state"] == "approved" and grant["state"] == "active":
            if grant["expires_at"] <= _grant_timestamp(now):
                raise RequestError("grant_unavailable")
            return dict(grant)
        task = db.get_task(conn, req["origin_task_id"])
        if (req["state"] != "held" or grant["state"] != "held" or task is None
                or task.user_id != actor_user_id or task.status != "pending_confirmation"
                or task.whatsapp_confirmation_request_id != request_id
                or text_hash(task.confirmation_prompt or "") != preview_digest):
            raise RequestError("confirmation_unavailable")
        try:
            if config is None:
                raise RequestError("location_unavailable")
            location.resolve_for_user(actor_user_id, config, conn=conn)
            _, audience, fingerprint, epoch = _timed_room_terms(conn, config, grant["room_token"], actor_user_id)
            if (audience != expected["audience"] or fingerprint != grant["binding_fingerprint"]
                    or epoch != grant["room_epoch"]):
                raise RequestError("audience_changed")
        except (RequestError, location.UserNotFoundError):
            # Return rather than raise: the caller's approval savepoint must
            # retain cancellation, including the request-to-grant trigger.
            conn.execute("UPDATE whatsapp_skill_requests SET state='cancelled', error_code='authority_changed', "
                         "closed_at=?, updated_at=? WHERE id=?",
                         (_grant_timestamp(now), _grant_timestamp(now), request_id))
            db.cancel_task(conn, task.id)
            return {"error": "grant_unavailable"}
        active = _activate_location_grant(conn, grant_id=grant_id, now=now)
        conn.execute("UPDATE whatsapp_skill_requests SET state='approved', approved_digest=?, approved_at=?, "
                     "closed_at=?, updated_at=? WHERE id=?",
                     (preview_digest, _grant_timestamp(now), _grant_timestamp(now), _grant_timestamp(now), request_id))
        return active


def _location_query_room(conn, config, actor_user_id, task_id):
    task = _interactive_task(conn, config, actor_user_id=actor_user_id, task_id=task_id)
    room = private_replies._live_shared_room(conn, task, actor_user_id)
    _, audience, fingerprint, epoch = _timed_room_terms(conn, config, room, actor_user_id)
    return room, audience, fingerprint, epoch


def _location_query_subject(conn, config, room, subject):
    members = [user for user in db.list_room_members(conn, room)
               if user in config.users and is_current_member(conn, room, user)]
    if subject in members:
        return subject
    matches = sorted(user for user in members
                     if config.users[user].display_name.casefold() == subject.strip().casefold())
    if len(matches) == 1:
        return matches[0]
    if matches:
        return {"error": "ambiguous_user", "candidates": [
            {"user_id": user, "display_name": config.users[user].display_name} for user in matches]}
    return {"error": "location_unavailable"}


def _readable_location_grant(conn, config, grant, terms, now):
    room, audience, fingerprint, epoch = terms
    owner = grant["owner_user_id"]
    return (grant["state"] == "active" and grant["expires_at"] is not None
            and grant["expires_at"] > _grant_timestamp(now)
            and grant["room_token"] == room and owner in config.users
            and is_current_member(conn, room, owner)
            and json.loads(grant["audience_json"]) == audience
            and grant["binding_fingerprint"] == fingerprint and grant["room_epoch"] == epoch
            and config.is_module_enabled(owner, "location", conn=conn))


def _read_room_locations(config, *, actor_user_id, task_id, subject_user_id, now,
                         map_card=False):
    try:
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN")
            terms = _location_query_room(conn, config, actor_user_id, task_id)
            subject = None
            if subject_user_id is not None:
                subject = _location_query_subject(conn, config, terms[0], subject_user_id)
                if isinstance(subject, dict):
                    return subject
            rows = conn.execute(
                "SELECT * FROM location_share_grants WHERE room_token=? AND state='active' "
                "AND expires_at>? AND (? IS NULL OR owner_user_id=?) ORDER BY owner_user_id",
                (terms[0], _grant_timestamp(datetime.now(timezone.utc)), subject, subject),
            ).fetchall()
            grants = [dict(row) for row in rows
                      if _readable_location_grant(conn, config, row, terms, datetime.now(timezone.utc))]
            if len(grants) > 50:
                raise RequestError("location_share_limit")

        collected = []
        for grant in grants:
            # Recheck before each owner's I/O as an earlier read may have taken
            # time. A module path is resolved only after its grant is accepted.
            with db.get_db(config.db_path) as conn:
                conn.execute("BEGIN")
                if _location_query_room(conn, config, actor_user_id, task_id) != terms:
                    raise RequestError("audience_changed")
                current = conn.execute("SELECT * FROM location_share_grants WHERE id=?", (grant["id"],)).fetchone()
                if current is None or not _readable_location_grant(
                        conn, config, current, terms, datetime.now(timezone.utc)):
                    continue
                try:
                    owner = location.resolve_for_user(grant["owner_user_id"], config, conn=conn)
                except location.UserNotFoundError:
                    continue
            snapshot = {"error": "no_fresh_location"}
            if owner.db_path.is_file():
                snapshot = location_snapshot(owner.db_path, now=now, precision=grant["precision"],
                                             accuracy_threshold_m=config.location.accuracy_threshold_m)
            collected.append((grant, snapshot))

        # One final transaction covers the entire aggregate. Earlier successful
        # reads cannot survive a stop committed while a later owner was read.
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN")
            final_now = datetime.now(timezone.utc)
            if _location_query_room(conn, config, actor_user_id, task_id) != terms:
                raise RequestError("audience_changed")
            results = []
            for grant, snapshot in collected:
                current = conn.execute("SELECT * FROM location_share_grants WHERE id=?", (grant["id"],)).fetchone()
                if (current is None or current["precision"] != grant["precision"]
                        or not _readable_location_grant(conn, config, current, terms, final_now)):
                    continue
                result = {"owner_user_id": grant["owner_user_id"], **snapshot}
                if "error" not in snapshot:
                    result["expires_at"] = current["expires_at"]
                    if map_card and subject_user_id is not None:
                        _record_card(conn, config, task_id=task_id, actor_user_id=actor_user_id,
                                     room_token=terms[0], snapshot=result, grant_id=grant["id"])
                results.append(result)
            if subject_user_id is not None:
                return results[0] if results else {"error": "location_unavailable"}
            return {"locations": results}
    except RequestError as exc:
        if str(exc) == "audience_unavailable":
            return {"error": "location_unavailable"}
        raise
    except (sqlite3.Error, OSError):
        raise RequestError("location_temporarily_unavailable") from None


def read_room_location(config, *, actor_user_id, task_id, subject_user_id, now, map_card=True):
    return _read_room_locations(config, actor_user_id=actor_user_id, task_id=task_id,
                                subject_user_id=subject_user_id, now=now, map_card=map_card)


def read_room_locations(config, *, actor_user_id, task_id, now):
    return _read_room_locations(config, actor_user_id=actor_user_id, task_id=task_id,
                                subject_user_id=None, now=now)


def _card_grant_readable(conn, config, card, observation, now):
    """The room-query stage's final check, asked again when the answer is stored."""
    grant = conn.execute("SELECT * FROM location_share_grants WHERE id=?",
                         (card["grant_id"],)).fetchone()
    if (grant is None or grant["owner_user_id"] != card["owner_user_id"]
            or grant["precision"] != observation.get("precision")
            or not is_current_member(conn, card["room_token"], card["actor_user_id"])):
        return False
    try:
        _, audience, fingerprint, epoch = _timed_room_terms(
            conn, config, card["room_token"], card["actor_user_id"])
    except RequestError:
        return False
    terms = (card["room_token"], audience, fingerprint, epoch)
    return _readable_location_grant(conn, config, grant, terms, now)


def location_cards_for_answer(conn, config, *, task_id, room_token, body):
    """The cards a task's answer carries, and their ids, for the row about to be stored.

    A card goes with the answer only when the answer has its `map_url`
    verbatim, which is the deterministic sign the model shared it, and only
    into the room it was recorded for. A granted read's card is dropped when
    the grant no longer reads. One card per link, in recording order, at most
    `MAX_CARDS_PER_MESSAGE`.
    """
    if not body:
        return [], []
    room = canonical_token(conn, room_token)
    rows = conn.execute(
        "SELECT * FROM location_share_cards WHERE task_id=? AND message_id IS NULL ORDER BY id",
        (task_id,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    shares, card_ids, seen = [], [], set()
    for card in rows:
        if len(shares) >= MAX_CARDS_PER_MESSAGE:
            break
        if card["room_token"] != room or card["map_url"] in seen or card["map_url"] not in body:
            continue
        try:
            observation = json.loads(card["observation"])
        except ValueError:
            continue
        if card["grant_id"] is not None and not _card_grant_readable(
                conn, config, card, observation, now):
            continue
        seen.add(card["map_url"])
        shares.append({"card_id": card["id"], **observation})
        card_ids.append(card["id"])
    return shares, card_ids


def mark_location_cards_attached(conn, card_ids, message_id):
    """Point the attached cards at their row, in the transaction that inserted it."""
    for card_id in card_ids:
        conn.execute(
            "UPDATE location_share_cards SET message_id=?, attached_at=datetime('now') WHERE id=?",
            (message_id, card_id),
        )
