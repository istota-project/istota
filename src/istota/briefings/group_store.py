"""Framework transactions for group definitions, generation claims and editions.

Callers pass trusted actor authority. Each mutation owns its transaction; model,
source and delivery I/O belong outside it. Readers here are internal DB helpers;
surface authorization belongs to their callers.
"""

import json
from contextlib import nullcontext
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from istota import db, modules
from istota.briefings.models import BriefingBlock, BlockSource, validate_group_definition_payload
from istota.groups.policy import GroupError, group_capabilities
from istota.groups.service import _transaction
from istota.groups.system import eligible_user_ids, reconcile_all_users_in_transaction


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _timestamp(value=None):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise GroupError(422, "invalid_time", "An explicit UTC offset is required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def briefing_capabilities(conn, config, group_id, actor_id, *, is_admin=False):
    caps = group_capabilities(conn, group_id, actor_id, is_admin=is_admin)
    eligible = actor_id in eligible_user_ids(conn, config)
    available = modules.module_available("briefings")
    return {
        "read_briefings": eligible and caps["read_briefings"],
        "author_briefings": eligible and caps["author_briefings"],
        "generate_briefings": eligible and caps["author_briefings"] and available,
        "receive_briefings": eligible and caps["receive_briefings"] and config.is_module_enabled(actor_id, "briefings", conn=conn),
    }


def _require_author(conn, config, group_id, actor_id, is_admin):
    caps = briefing_capabilities(conn, config, group_id, actor_id, is_admin=is_admin)
    if not caps["read_briefings"]:
        raise GroupError(404, "group_not_found", "Group not found")
    if not caps["author_briefings"]:
        raise GroupError(403, "briefing_forbidden", "Group briefing author required")


def available_channels(config):
    channels = ["in_app", "ntfy"]
    if config.email.enabled:
        channels.append("email")
    if config.talk.enabled and config.nextcloud.url:
        channels.append("talk")
    return channels


def definition_sources(definition):
    generation = definition["generation"]
    if definition["generation_mode"] == "prompt":
        return generation["inputs"]
    return [source for block in generation["blocks"] for source in block["sources"]]


def definition_blocks(definition):
    """Adapt stored block specs to the existing gathering and rendering model."""
    blocks = []
    for position, raw in enumerate(definition["generation"].get("blocks", [])):
        block = BriefingBlock(id=position, briefing_name=definition["name"], position=position,
                              title=raw["title"], directive=raw["directive"],
                              render_mode=raw["render_mode"], options=raw["options"])
        block.sources = [BlockSource(id=source["id"], block_id=position, position=index,
                                     kind=source["kind"], config=source["config"])
                         for index, source in enumerate(raw["sources"])]
        blocks.append(block)
    return blocks


def _validate_run_sources(conn, config, definition):
    from istota.briefings.source_grants import validate_definition_grants
    from istota.briefings.group_migrate import validate_migration
    validate_migration(conn, definition)
    generation = definition["generation"]
    if generation["contains_personal_information"] and not generation["audience_acknowledged"]:
        raise GroupError(422, "audience_acknowledgement_required", "Acknowledge the group audience for personal prompt content")
    return validate_definition_grants(conn, config, definition)


def get_definition(conn, definition_id):
    row = conn.execute("SELECT * FROM group_briefing_configs WHERE id = ?", (definition_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["generation"] = json.loads(result.pop("generation_json"))
    result["excluded_user_ids"] = json.loads(result.pop("excluded_user_ids_json"))
    result["enabled"] = bool(result["enabled"])
    return result


def supersede_definition_runs(conn, definition_id):
    conn.execute("""UPDATE group_briefing_runs SET state = 'superseded', error_code = 'definition_changed',
                    attempt_token = NULL, lease_expires_at = NULL, updated_at = datetime('now')
                    WHERE definition_id = ? AND state IN ('queued', 'researching', 'generating')""",
                 (definition_id,))


def stop_definition_work(conn, definition_id, reason):
    """Stop a definition's unsent work; shared by disabling and an administrator's hold.

    Active runs are superseded and pending or failed legs skipped with ``reason``.
    A leg already sending keeps its state and carries the reason, which
    ``delivery_refusal`` reads before any later part goes out.
    """
    supersede_definition_runs(conn, definition_id)
    conn.execute("UPDATE group_briefing_recipients SET state='skipped', error_code=? "
                 "WHERE run_id IN (SELECT id FROM group_briefing_runs WHERE definition_id=?) "
                 "AND state IN ('pending', 'failed')", (reason, definition_id))
    conn.execute("UPDATE group_briefing_recipients SET error_code=? "
                 "WHERE run_id IN (SELECT id FROM group_briefing_runs WHERE definition_id=?) "
                 "AND state='sending'", (reason, definition_id))


def list_definitions(conn, group_id):
    return [get_definition(conn, row["id"]) for row in conn.execute(
        "SELECT id FROM group_briefing_configs WHERE group_id = ? AND deleted_at IS NULL ORDER BY name", (group_id,)
    )]


def validate_definition_save(conn, config, group_id, payload, *, actor_id, expected_revision,
                             definition_id=None, is_admin=False):
    """Read-only save checks, shared with operator plan previews."""
    try:
        value = validate_group_definition_payload(payload)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise GroupError(422, "invalid_definition", "Invalid briefing definition") from exc
    if type(expected_revision) is not int or expected_revision < 0:
        raise GroupError(422, "invalid_revision", "A nonnegative definition revision is required")
    _require_author(conn, config, group_id, actor_id, is_admin)
    if value["channel"] not in available_channels(config):
        raise GroupError(422, "channel_unavailable", "Delivery channel is unavailable")
    old = get_definition(conn, definition_id) if definition_id is not None else None
    if definition_id is not None and (old is None or old["deleted_at"]):
        raise GroupError(404, "definition_not_found", "Briefing not found")
    if old and old["group_id"] != group_id:
        raise GroupError(409, "immutable_target", "Duplicate the briefing to change its target")
    if old and old["name"] != value["name"]:
        raise GroupError(409, "immutable_name", "Briefing name is fixed")
    if expected_revision != (old["revision"] if old else 0):
        raise GroupError(409, "revision_conflict", "Briefing changed; reload before saving")
    if old and old["admin_hold_at"] and value["enabled"] and not old["enabled"]:
        raise GroupError(409, "definition_held", "An administrator has paused this briefing")
    retained = set(old["excluded_user_ids"]) if old else set()
    if old and "excluded_user_ids" not in payload:
        value["excluded_user_ids"] = old["excluded_user_ids"]
    current_members = set(db.list_group_members(conn, group_id))
    if set(value["excluded_user_ids"]) - retained - current_members:
        raise GroupError(422, "invalid_excluded_user", "New exclusions must be current group members")
    collision = conn.execute("SELECT id FROM group_briefing_configs WHERE group_id = ? AND name = ?",
                             (group_id, value["name"])).fetchone()
    if collision and (not old or collision["id"] != old["id"]):
        raise GroupError(409, "definition_exists", "Briefing name already exists")
    candidate = {**value, "id": definition_id, "group_id": group_id}
    if value["enabled"]:
        from istota.briefings.group_migrate import validate_migration
        candidate["revision"] = old["revision"] if old else 0
        validate_migration(conn, candidate, activation=True)
        _validate_run_sources(conn, config, candidate)
    return value, old


def save_definition(conn, config, group_id, payload, *, actor_id, expected_revision,
                    definition_id=None, is_admin=False, caller_transaction=False):
    if caller_transaction and not conn.in_transaction:
        raise ValueError("A caller-owned transaction is required")
    with nullcontext() if caller_transaction else _transaction(conn):
        value, old = validate_definition_save(
            conn, config, group_id, payload, actor_id=actor_id, expected_revision=expected_revision,
            definition_id=definition_id, is_admin=is_admin)
        from istota.briefings.source_grants import invalidate_changed_grants
        candidate = {**value, "id": definition_id, "group_id": group_id}
        invalidate_changed_grants(conn, old, candidate)
        fields = (value["title"], value["cron"], value["timezone"], int(value["enabled"]),
                  value["generation_mode"], _json(value["generation"]), value["channel"], _json(value["excluded_user_ids"]), actor_id)
        if old:
            conn.execute("""UPDATE group_briefing_configs SET title = ?, cron = ?, timezone = ?, enabled = ?,
                            generation_mode = ?, generation_json = ?, channel = ?, excluded_user_ids_json = ?, updated_by = ?,
                            revision = revision + 1, updated_at = datetime('now') WHERE id = ?""",
                         (*fields, definition_id))
            for user_id in value["excluded_user_ids"]:
                conn.execute("UPDATE group_briefing_recipients SET state='skipped', error_code='excluded' "
                             "WHERE run_id IN (SELECT id FROM group_briefing_runs WHERE definition_id=?) "
                             "AND user_id=? AND state IN ('pending', 'failed')", (definition_id, user_id))
                conn.execute("UPDATE group_briefing_recipients SET error_code='excluded' "
                             "WHERE run_id IN (SELECT id FROM group_briefing_runs WHERE definition_id=?) "
                             "AND user_id=? AND state='sending'", (definition_id, user_id))
            if value["enabled"]:
                supersede_definition_runs(conn, definition_id)
            else:
                stop_definition_work(conn, definition_id, "definition_disabled")
        else:
            cursor = conn.execute("""INSERT INTO group_briefing_configs
                (title, cron, timezone, enabled, generation_mode, generation_json, channel, excluded_user_ids_json, updated_by,
                 group_id, name, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                  (*fields, group_id, value["name"], actor_id))
            definition_id = cursor.lastrowid
        definition = get_definition(conn, definition_id)
        from istota.notifications.resolvers.group_briefing import sync_approvals
        sync_approvals(conn, config, definition)
        return definition


def set_hold(conn, config, definition_id, *, admin_id):
    """Hold a definition for an administrator; None for an unknown id.

    Idempotent: a held definition keeps its first time and administrator and
    its revision. Placing the hold stops unsent work as disabling does and
    bumps the revision, so an open editor reloads rather than overwrites it.
    The owners' ``enabled`` is not touched.
    """
    from istota.notifications.resolvers.group_briefing import sync_approvals
    with _transaction(conn):
        current = get_definition(conn, definition_id)
        if current is None or current["deleted_at"]:
            return None
        if current["admin_hold_at"] is not None:
            return current
        conn.execute("UPDATE group_briefing_configs SET admin_hold_at = ?, admin_hold_by = ?, "
                     "revision = revision + 1, updated_at = datetime('now') WHERE id = ?",
                     (_timestamp(), admin_id, definition_id))
        stop_definition_work(conn, definition_id, "definition_held")
        definition = get_definition(conn, definition_id)
        sync_approvals(conn, config, definition)
        return definition


def clear_hold(conn, config, definition_id):
    """Lift an administrator's hold; None for an unknown id. Idempotent."""
    from istota.notifications.resolvers.group_briefing import sync_approvals
    with _transaction(conn):
        current = get_definition(conn, definition_id)
        if current is None or current["deleted_at"]:
            return None
        if current["admin_hold_at"] is None:
            return current
        conn.execute("UPDATE group_briefing_configs SET admin_hold_at = NULL, admin_hold_by = NULL, "
                     "revision = revision + 1, updated_at = datetime('now') WHERE id = ?", (definition_id,))
        definition = get_definition(conn, definition_id)
        sync_approvals(conn, config, definition)
        return definition


def require_no_pending_migration(conn, definition_id):
    """Refuse a delete while a migration still acts on the definition.

    Only ``cutover`` and ``rolled_back`` are settled; every migration step
    refuses a deleted definition afterwards. The admin route asks this
    before it spends a step-up code.
    """
    migration = conn.execute("SELECT state FROM group_briefing_migrations WHERE definition_id = ?",
                             (definition_id,)).fetchone()
    if migration is not None and migration["state"] not in ("cutover", "rolled_back"):
        raise GroupError(409, "migration_pending", "Finish or roll back the briefing's migration first")


def delete_definition(conn, definition_id, *, actor_id, expected_revision=None, draft_only=False):
    """Soft-delete a definition, for an administrator or a group owner.

    Published editions stay readable to members, so the row stays and only
    leaves authoring: unsent work stops as on a hold, source approvals are
    revoked, and the name is freed for the group by renaming the row. An
    owner's delete passes ``draft_only`` and its editor's revision, so it
    removes only a disabled definition, and only the one they last saw.
    """
    from istota.briefings.source_grants import revoke_definition_grants
    from istota.notifications.resolvers.group_briefing import close_approvals
    with _transaction(conn):
        current = get_definition(conn, definition_id)
        if current is None or current["deleted_at"]:
            raise GroupError(404, "definition_not_found", "Briefing not found")
        if expected_revision is not None and expected_revision != current["revision"]:
            raise GroupError(409, "revision_conflict", "Briefing changed; reload before deleting")
        if draft_only and current["enabled"]:
            raise GroupError(409, "disable_first", "Turn the briefing off before deleting it")
        require_no_pending_migration(conn, definition_id)
        stop_definition_work(conn, definition_id, "definition_deleted")
        revoke_definition_grants(conn, definition_id)
        close_approvals(conn, definition_id)
        stem = f"deleted-{definition_id}-{current['name']}"[:100]
        name, suffix = stem, 2
        while conn.execute("SELECT 1 FROM group_briefing_configs WHERE group_id = ? AND name = ?",
                           (current["group_id"], name)).fetchone():
            name = f"{stem[:99 - len(str(suffix))]}-{suffix}"
            suffix += 1
        conn.execute("UPDATE group_briefing_configs SET deleted_at = ?, deleted_by = ?, enabled = 0, name = ?, "
                     "revision = revision + 1, updated_at = datetime('now') WHERE id = ?",
                     (_timestamp(), actor_id, name, definition_id))


def get_run(conn, run_id):
    row = conn.execute("SELECT * FROM group_briefing_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    for field in ("snapshot", "request", "research", "provenance", "usage", "document"):
        raw = result.pop(field + "_json")
        result[field] = json.loads(raw) if raw is not None else None
    return result


def enqueue_run(conn, config, definition_id, *, actor_id=None, is_admin=False,
                request_id=None, due_at=None, mode="publish", now=None):
    at = _timestamp(now)
    if mode not in ("publish", "preview"):
        raise GroupError(422, "invalid_mode", "Run mode must be publish or preview")
    if request_id is not None:
        try:
            occurrence = "request:" + str(UUID(request_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise GroupError(422, "invalid_request_id", "A request UUID is required") from exc
        if actor_id is None or due_at is not None:
            raise GroupError(422, "invalid_request", "Manual requests require an author and no scheduled time")
        due = at
    else:
        if due_at is None or mode != "publish" or actor_id is not None:
            raise GroupError(422, "invalid_request", "A scheduled UTC occurrence is required")
        due = _timestamp(due_at)
        occurrence = "scheduled:" + due
    with _transaction(conn):
        definition = get_definition(conn, definition_id)
        if definition is None or definition["deleted_at"] or db.get_group(conn, definition["group_id"]) is None:
            raise GroupError(404, "definition_not_found", "Briefing not found")
        if actor_id is not None:
            _require_author(conn, config, definition["group_id"], actor_id, is_admin)
        if not modules.module_available("briefings"):
            raise GroupError(503, "briefings_unavailable", "Briefing generation is unavailable")
        request = {"mode": mode, "revision": definition["revision"]}
        existing = conn.execute("SELECT id, request_json FROM group_briefing_runs WHERE definition_id = ? AND occurrence_key = ?",
                                (definition_id, occurrence)).fetchone()
        if existing:
            if request_id is not None and existing["request_json"] != _json(request):
                raise GroupError(409, "request_conflict", "Request UUID was already used for another payload")
            return get_run(conn, existing["id"])
        if mode == "publish" and definition["admin_hold_at"]:
            raise GroupError(409, "definition_held", "An administrator has paused this briefing")
        if mode == "publish" and not definition["enabled"]:
            raise GroupError(409, "definition_disabled", "Enable the briefing before publishing")
        if definition["channel"] not in available_channels(config):
            raise GroupError(422, "channel_unavailable", "Delivery channel is unavailable")
        from istota.briefings.group_migrate import validate_migration
        validate_migration(conn, definition, activation=mode == "publish", due_at=due if request_id is None else None)
        grants = _validate_run_sources(conn, config, definition)
        snapshot = {"definition": definition, "grants": grants}
        cursor = conn.execute("""INSERT INTO group_briefing_runs
            (definition_id, occurrence_key, mode, due_at, request_json, snapshot_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                              (definition_id, occurrence, mode, due, _json(request), _json(snapshot), at, at))
        return get_run(conn, cursor.lastrowid)


def claim_run(conn, *, run_id=None, now=None, lease_seconds=300):
    at = _timestamp(now)
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise GroupError(422, "invalid_lease", "Lease must be 1–3600 seconds")
    expiry = _timestamp(datetime.fromisoformat(at.replace("Z", "+00:00")) + timedelta(seconds=lease_seconds))
    with _transaction(conn):
        row = conn.execute("""SELECT id FROM group_briefing_runs WHERE
            (state = 'queued' OR (state IN ('researching', 'generating') AND lease_expires_at <= ?))
            AND due_at <= ? AND (? IS NULL OR id = ?) ORDER BY due_at, id LIMIT 1""", (at, at, run_id, run_id)).fetchone()
        if row is None:
            return None
        run = get_run(conn, row["id"])
        research = run["snapshot"]["definition"]["generation"].get("research", {}).get("enabled", False)
        state = "researching" if research and run["research"] is None else "generating"
        conn.execute("""UPDATE group_briefing_runs SET state = ?, attempt_token = ?, lease_expires_at = ?,
                        error_code = NULL, updated_at = ? WHERE id = ?""",
                     (state, uuid4().hex, expiry, at, run["id"]))
        return get_run(conn, run["id"])


def _owned_run(conn, run_id, token, at):
    run = get_run(conn, run_id)
    if (run is None or run["state"] not in ("researching", "generating") or not token
            or run["attempt_token"] != token or not run["lease_expires_at"] or run["lease_expires_at"] <= at):
        raise GroupError(409, "lease_lost", "Generation claim is no longer current")
    return run


def renew_run_lease(conn, run_id, token, *, now=None, lease_seconds=300):
    at = _timestamp(now)
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise GroupError(422, "invalid_lease", "Lease must be 1–3600 seconds")
    expiry = _timestamp(datetime.fromisoformat(at.replace("Z", "+00:00")) + timedelta(seconds=lease_seconds))
    with _transaction(conn):
        _owned_run(conn, run_id, token, at)
        conn.execute("UPDATE group_briefing_runs SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                     (expiry, at, run_id))
        return get_run(conn, run_id)


def save_research(conn, run_id, token, artifact, *, now=None, config=None):
    at = _timestamp(now)
    if not isinstance(artifact, dict):
        raise GroupError(422, "invalid_research", "Research must be an object")
    encoded = _json(artifact)
    with _transaction(conn):
        run = _owned_run(conn, run_id, token, at)
        if run["state"] != "researching" or run["research"] is not None:
            raise GroupError(409, "research_conflict", "Research was already completed or not requested")
        if config is not None:
            from istota.briefings.source_grants import validate_run_grants
            validate_run_grants(conn, config, run)
        conn.execute("""UPDATE group_briefing_runs SET research_json = ?, research_attempt_token = ?,
                        state = 'generating', updated_at = ? WHERE id = ?""", (encoded, token, at, run_id))
        return get_run(conn, run_id)


def finish_run(conn, run_id, token, *, error_code, state="failed", now=None):
    if state not in ("failed", "cancelled") or not isinstance(error_code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", error_code):
        raise GroupError(422, "invalid_run_outcome", "A bounded failure code is required")
    at = _timestamp(now)
    with _transaction(conn):
        _owned_run(conn, run_id, token, at)
        conn.execute("""UPDATE group_briefing_runs SET state = ?, error_code = ?, attempt_token = NULL,
                        lease_expires_at = NULL, updated_at = ? WHERE id = ?""", (state, error_code, at, run_id))
        return get_run(conn, run_id)


def list_recipients(conn, run_id):
    return [dict(row) for row in conn.execute(
        "SELECT * FROM group_briefing_recipients WHERE run_id = ? ORDER BY user_id, channel", (run_id,)
    )]


def publish_run(conn, config, run_id, token, *, subject, body_md, provenance=None, usage=None,
                document=None, now=None):
    if document is not None:
        from istota.briefings.document import document_to_markdown
        from istota.briefings.models import validate_briefing_document
        try:
            document = validate_briefing_document(document)
        except ValueError as exc:
            raise GroupError(422, "invalid_document", "Edition document is invalid") from exc
        if document["title"] != subject or document_to_markdown(document) != body_md:
            raise GroupError(422, "invalid_document", "Edition document does not match its body")
    if not isinstance(subject, str) or not subject.strip() or not isinstance(body_md, str) or not body_md.strip():
        raise GroupError(422, "empty_edition", "An edition requires a subject and body")
    at = _timestamp(now)
    with _transaction(conn):
        run = _owned_run(conn, run_id, token, at)
        if run["state"] != "generating":
            raise GroupError(409, "research_incomplete", "Complete research before publication")
        definition = get_definition(conn, run["definition_id"])
        if definition is None or db.get_group(conn, definition["group_id"]) is None:
            raise GroupError(409, "definition_unavailable", "Briefing target is unavailable")
        if definition["revision"] != run["snapshot"]["definition"]["revision"]:
            raise GroupError(409, "revision_conflict", "Briefing changed during generation")
        if not modules.module_available("briefings"):
            raise GroupError(503, "briefings_unavailable", "Briefing generation is unavailable")
        if run["mode"] == "publish" and not definition["enabled"]:
            raise GroupError(409, "definition_disabled", "Briefing was disabled during generation")
        if _validate_run_sources(conn, config, definition) != run["snapshot"]["grants"]:
            raise GroupError(409, "source_approval_changed", "Source approval changed during generation")
        conn.execute("""UPDATE group_briefing_runs SET state = ?, subject = ?, body_md = ?, document_json = ?,
                        provenance_json = ?, usage_json = ?, published_at = ?, updated_at = ?,
                        attempt_token = NULL, lease_expires_at = NULL WHERE id = ?""",
                     ("published" if run["mode"] == "publish" else "preview_ready", subject, body_md,
                      _json(document) if document is not None else None, _json(provenance or {}), _json(usage or {}), at, at, run_id))
        if run["mode"] == "publish":
            # Reconcile in this transaction so a newly enabled account joins this
            # snapshot, while no historical interval is reused on rejoining.
            group = db.get_group(conn, definition["group_id"])
            if group["system_key"] == "all_users":
                reconcile_all_users_in_transaction(conn, config)
            eligible = eligible_user_ids(conn, config)
            members = conn.execute("SELECT id, user_id FROM group_members WHERE group_id = ? AND ended_at IS NULL ORDER BY user_id",
                                   (definition["group_id"],)).fetchall()
            for member in members:
                user_id = member["user_id"]
                if user_id not in eligible or not db.is_group_member(conn, definition["group_id"], user_id):
                    continue
                channels = ["in_app"]
                if definition["channel"] != "in_app":
                    channels.append(definition["channel"])
                for channel in channels:
                    muted_module = channel != "in_app" and not config.is_module_enabled(user_id, "briefings", conn=conn)
                    refusal = "excluded" if user_id in definition["excluded_user_ids"] else "module_disabled" if muted_module else None
                    conn.execute("""INSERT INTO group_briefing_recipients
                        (run_id, user_id, membership_id, channel, state, error_code, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                 (run_id, user_id, member["id"], channel, "skipped" if refusal else "pending",
                                  refusal, at, at))
        return get_run(conn, run_id)


def delivery_preferences(conn, group_id, user_id, *, definition_id=None, include_group=True):
    """Resolve effective mutes, or only local values for the settings editor."""
    rows = []
    if include_group:
        rows.extend(conn.execute("SELECT outbound_muted, in_app_muted FROM group_briefing_group_preferences "
                                 "WHERE group_id = ? AND user_id = ?", (group_id, user_id)))
    if definition_id is not None:
        rows.extend(conn.execute("SELECT outbound_muted, in_app_muted FROM group_briefing_preferences "
                                 "WHERE definition_id = ? AND user_id = ?", (definition_id, user_id)))
    return {key: any(row[key] for row in rows) for key in ("outbound_muted", "in_app_muted")}


def set_delivery_preferences(conn, config, group_id, *, actor_id, outbound_muted, in_app_muted,
                             definition_id=None):
    if type(outbound_muted) is not bool or type(in_app_muted) is not bool:
        raise GroupError(422, "invalid_preference", "Mute values must be booleans")
    with _transaction(conn):
        if not briefing_capabilities(conn, config, group_id, actor_id)["read_briefings"]:
            raise GroupError(404, "group_not_found", "Group not found")
        if definition_id is None:
            table, key, value = "group_briefing_group_preferences", "group_id", group_id
        else:
            definition = get_definition(conn, definition_id)
            if definition is None or definition["group_id"] != group_id:
                raise GroupError(404, "definition_not_found", "Briefing not found")
            table, key, value = "group_briefing_preferences", "definition_id", definition_id
        conn.execute(f"INSERT INTO {table} ({key}, user_id, outbound_muted, in_app_muted) VALUES (?, ?, ?, ?) "
                     f"ON CONFLICT ({key}, user_id) DO UPDATE SET outbound_muted=excluded.outbound_muted, "
                     "in_app_muted=excluded.in_app_muted", (value, actor_id, outbound_muted, in_app_muted))
        return delivery_preferences(conn, group_id, actor_id, definition_id=definition_id)


def get_recipient(conn, recipient_id):
    row = conn.execute("SELECT * FROM group_briefing_recipients WHERE id = ?", (recipient_id,)).fetchone()
    return dict(row) if row else None


def delivery_refusal(conn, config, recipient):
    """Live checks for an already snapshotted member; never admit a new interval."""
    from istota.briefings.source_grants import validate_run_grants
    if recipient["error_code"] in ("definition_disabled", "definition_held", "definition_deleted", "excluded"):
        return recipient["error_code"]
    run = get_run(conn, recipient["run_id"])
    definition = get_definition(conn, run["definition_id"])
    if not definition or not db.get_group(conn, definition["group_id"]):
        return "definition_unavailable"
    if definition["deleted_at"]:
        return "definition_deleted"
    if not definition["enabled"]:
        return "definition_disabled"
    if definition["admin_hold_at"]:
        return "definition_held"
    member = conn.execute("SELECT * FROM group_members WHERE id = ? AND ended_at IS NULL",
                          (recipient["membership_id"],)).fetchone()
    user = recipient["user_id"]
    if (not member or member["user_id"] != user or member["group_id"] != definition["group_id"]
            or user not in eligible_user_ids(conn, config)):
        return "membership_ended"
    try:
        validate_run_grants(conn, config, run)
    except GroupError:
        return "source_approval_revoked"
    if user in definition["excluded_user_ids"]:
        return "excluded"
    prefs = delivery_preferences(conn, definition["group_id"], user, definition_id=definition["id"])
    if prefs["in_app_muted" if recipient["channel"] == "in_app" else "outbound_muted"]:
        return "muted"
    if recipient["channel"] != "in_app":
        if not config.is_module_enabled(user, "briefings", conn=conn):
            return "module_disabled"
        if recipient["channel"] not in available_channels(config):
            return "channel_unavailable"
    return None


def expire_delivery_claims(conn, *, now=None):
    """An abandoned send is ambiguous, never an automatic resend."""
    at = _timestamp(now)
    with _transaction(conn):
        rows = conn.execute("SELECT id FROM group_briefing_recipients WHERE state='sending' "
                            "AND lease_expires_at <= ?", (at,)).fetchall()
        for row in rows:
            conn.execute("UPDATE group_briefing_parts SET state='uncertain' "
                         "WHERE recipient_id=? AND state='sending'", (row["id"],))
            conn.execute("UPDATE group_briefing_recipients SET state='uncertain', destination_locked=1, "
                         "error_code='acceptance_unknown', dispatch_token=NULL, lease_expires_at=NULL, updated_at=? "
                         "WHERE id=?", (at, row["id"]))


def claim_recipient(conn, config, recipient_id, *, now=None):
    at = _timestamp(now)
    with _transaction(conn):
        row = get_recipient(conn, recipient_id)
        if not row or row["state"] != "pending":
            return None
        refusal = delivery_refusal(conn, config, row)
        if refusal:
            conn.execute("UPDATE group_briefing_recipients SET state='skipped', error_code=?, updated_at=? WHERE id=?",
                         (refusal, at, recipient_id))
            return None
        expiry = _timestamp(datetime.fromisoformat(at.replace("Z", "+00:00")) + timedelta(minutes=5))
        conn.execute("UPDATE group_briefing_recipients SET state='sending', attempts=attempts+1, dispatch_token=?, "
                     "lease_expires_at=?, send_identity=COALESCE(send_identity, ?), updated_at=? WHERE id=?",
                     (uuid4().hex, expiry, uuid4().hex, at, recipient_id))
        return get_recipient(conn, recipient_id)


def _owned_recipient(conn, recipient_id, token, now=None):
    row = get_recipient(conn, recipient_id)
    if (not row or row["state"] != "sending" or row["dispatch_token"] != token
            or not token or row["lease_expires_at"] <= _timestamp(now)):
        raise GroupError(409, "delivery_claim_lost", "Delivery claim is no longer current")
    return row


def prepare_delivery(conn, recipient_id, token, destination, parts, *, now=None):
    with _transaction(conn):
        row = _owned_recipient(conn, recipient_id, token, now)
        encoded = _json(destination)
        sent = conn.execute("SELECT 1 FROM group_briefing_parts WHERE recipient_id=? AND state='sent'",
                            (recipient_id,)).fetchone()
        if (row["destination_locked"] or sent) and row["destination_json"] != encoded:
            raise GroupError(409, "destination_changed", "Accepted or uncertain delivery keeps its original destination")
        conn.execute("UPDATE group_briefing_recipients SET destination_json=? WHERE id=?", (encoded, recipient_id))
        if not conn.execute("SELECT 1 FROM group_briefing_parts WHERE recipient_id=?", (recipient_id,)).fetchone():
            for index, body in enumerate(parts):
                conn.execute("INSERT INTO group_briefing_parts (recipient_id, part_index, body, send_identity) "
                             "VALUES (?, ?, ?, ?)", (recipient_id, index, body, f"{row['send_identity']}-{index}"))
        return [dict(p) for p in conn.execute("SELECT * FROM group_briefing_parts WHERE recipient_id=? ORDER BY part_index",
                                             (recipient_id,))]


def start_delivery_part(conn, config, recipient_id, token, index, *, now=None):
    with _transaction(conn):
        row = _owned_recipient(conn, recipient_id, token, now)
        refusal = delivery_refusal(conn, config, row)
        if refusal:
            raise GroupError(409, refusal, "Delivery is no longer eligible")
        expiry = _timestamp(datetime.fromisoformat(_timestamp(now).replace("Z", "+00:00")) + timedelta(minutes=5))
        conn.execute("UPDATE group_briefing_recipients SET lease_expires_at=? WHERE id=?", (expiry, recipient_id))
        changed = conn.execute("UPDATE group_briefing_parts SET state='sending' WHERE recipient_id=? "
                              "AND part_index=? AND state='pending'", (recipient_id, index)).rowcount
        return bool(changed)


def finish_delivery_part(conn, recipient_id, token, index, state, *, reference=None, now=None):
    if state not in ("sent", "failed", "uncertain"):
        raise ValueError("Invalid part outcome")
    with _transaction(conn):
        _owned_recipient(conn, recipient_id, token, now)
        conn.execute("UPDATE group_briefing_parts SET state=?, provider_reference=? "
                     "WHERE recipient_id=? AND part_index=? AND state='sending'",
                     (state, reference, recipient_id, index))


def finish_delivery(conn, recipient_id, token, state, *, error_code=None, now=None):
    if state not in ("sent", "failed", "uncertain", "skipped"):
        raise ValueError("Invalid delivery outcome")
    if error_code is not None and not re.fullmatch(r"[a-z0-9_]{1,64}", error_code):
        raise ValueError("A bounded error code is required")
    with _transaction(conn):
        _owned_recipient(conn, recipient_id, token, now)
        conn.execute("UPDATE group_briefing_recipients SET state=?, error_code=?, dispatch_token=NULL, "
                     "lease_expires_at=NULL, destination_locked=MAX(destination_locked, ?), updated_at=? WHERE id=?",
                     (state, error_code, state == "uncertain", _timestamp(now), recipient_id))


def retry_deliveries(conn, config, run_id, *, actor_id, is_admin=False, acknowledge_duplicates=False, now=None):
    """Current owners may retry existing failed legs, never generate an edition."""
    with _transaction(conn):
        run = get_run(conn, run_id)
        if not run or run["state"] != "published":
            raise GroupError(404, "run_not_found", "Published edition not found")
        _require_author(conn, config, run["snapshot"]["definition"]["group_id"], actor_id, is_admin)
        count = 0
        for row in list_recipients(conn, run_id):
            if row["state"] not in ("failed", "uncertain"):
                continue
            if row["state"] == "uncertain" and acknowledge_duplicates is not True:
                continue
            refusal = delivery_refusal(conn, config, row)
            if refusal:
                conn.execute("UPDATE group_briefing_recipients SET state='skipped', error_code=? WHERE id=?",
                             (refusal, row["id"]))
                continue
            conn.execute("UPDATE group_briefing_parts SET state='pending' WHERE recipient_id=? "
                         "AND state IN ('failed', 'uncertain')", (row["id"],))
            conn.execute("UPDATE group_briefing_recipients SET state='pending', error_code=NULL, updated_at=? WHERE id=?",
                         (_timestamp(now), row["id"]))
            count += 1
        return count


def prune_runs(conn, *, retention_days, now=None):
    """Prune terminal editions and their ledger in the caller's cleanup transaction."""
    if retention_days <= 0:
        return 0
    cutoff = _timestamp((now or datetime.now(timezone.utc)) - timedelta(days=retention_days))
    rows = conn.execute("SELECT id FROM group_briefing_runs WHERE state IN "
                        "('published', 'preview_ready', 'failed', 'superseded', 'cancelled') "
                        "AND julianday(COALESCE(published_at, updated_at)) < julianday(?) "
                        "AND NOT EXISTS (SELECT 1 FROM group_briefing_recipients r WHERE r.run_id=group_briefing_runs.id "
                        "AND r.state IN ('pending', 'sending', 'uncertain'))", (cutoff,)).fetchall()
    from istota.notifications.store import resolve_by_object
    for row in rows:
        run_id = row["id"]
        for notice in conn.execute("SELECT user_id FROM notifications WHERE source='group_briefing' "
                                   "AND object_type='group_edition' AND object_id=?", (str(run_id),)):
            resolve_by_object(conn, notice["user_id"], "group_briefing", "group_edition", str(run_id), by="retention")
        conn.execute("DELETE FROM group_briefing_parts WHERE recipient_id IN "
                     "(SELECT id FROM group_briefing_recipients WHERE run_id=?)", (run_id,))
        conn.execute("DELETE FROM group_briefing_recipients WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM group_briefing_runs WHERE id=?", (run_id,))
    return len(rows)
