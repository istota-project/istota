"""Operator-reviewed default migration; personal stores and archives stay personal.

The registry journal freezes upgrade provenance, never live group membership.
Each default then has its own restartable preservation, import and cutover record.
Module content commits before its framework checkpoint; the existing content
sentinel makes replay safe across these two databases.
"""

from contextlib import ExitStack
from dataclasses import asdict
import json
import re

from istota import db, user_briefings
from istota.briefings import _migrate, db as bdb, group_store as store
from istota.config import BriefingConfig
from istota.groups.policy import GroupError
from istota.groups.service import _transaction
from istota.groups.system import reconcile_all_users_in_transaction
from istota.settings_plan.briefings import snapshot_blocks
from istota.settings_plan.group_briefings import operator_is_admin


PAYLOAD_FIELDS = ("name", "title", "cron", "timezone", "enabled", "channel", "generation_mode", "generation")


def _error(code):
    raise GroupError(409, code, code)


def _record(conn, key):
    row = conn.execute("SELECT * FROM group_briefing_migrations WHERE legacy_key=?", (key,)).fetchone()
    if row is None:
        return None
    return {**dict(row), **json.loads(row["data_json"])}


def _write(conn, key, state, data, definition_id=None):
    data = {key: value for key, value in data.items()
            if key not in ("legacy_key", "state", "data_json", "definition_id", "updated_at")}
    conn.execute("INSERT INTO group_briefing_migrations (legacy_key, definition_id, state, data_json) "
                 "VALUES (?, ?, ?, ?) ON CONFLICT(legacy_key) DO UPDATE SET definition_id=excluded.definition_id, "
                 "state=excluded.state, data_json=excluded.data_json, updated_at=datetime('now')",
                 (key, definition_id, state, store._json(data)))


def _normalized_blocks(blocks):
    return [{"title": block["title"], "directive": block.get("directive") or "",
             "render_mode": block.get("render_mode", "synthesis"), "options": block.get("options") or {},
             "sources": [{"kind": source["kind"], "config": source.get("config") or {},
                          "enabled": source.get("enabled", True)} for source in block["sources"]]}
            for block in blocks]


def _schedule(briefing):
    return {field: getattr(briefing, field) for field in
            ("name", "cron", "title", "output", "conversation_token", "components")} | {
                "enabled": getattr(briefing, "enabled", True)}


def preserve_legacy_defaults(config):
    """Freeze existing effective defaults before the retired overlay disappears."""
    if not config.db_path or not config.db_path.exists():
        return
    with db.get_db(config.db_path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='group_briefing_migrations'").fetchone():
            # Refuse a pre-init upgrade rather than silently dropping its defaults.
            if config.default_briefings:
                raise ValueError("default_briefings migration requires istota init")
            return
        with _transaction(conn):
            registry = _record(conn, "registry:v1") or {"capture": False, "users": []}
            if registry["capture"]:
                if not config._user_registry_complete:
                    _error("legacy_registry_incomplete")
                registry.update(capture=False, users=sorted(user for user in config.users if not user.startswith("__")),
                                defaults=[asdict(item) for item in config.default_briefings])
                group = reconcile_all_users_in_transaction(conn, config)
                for user in registry["users"]:
                    if not config.users[user].default_briefings:
                        conn.execute("INSERT OR IGNORE INTO group_briefing_group_preferences "
                                     "(group_id,user_id,outbound_muted,in_app_muted) VALUES (?,?,1,1)", (group["group_id"], user))
                _write(conn, "registry:v1", "registry", registry)
            for raw in registry.get("defaults", []):
                key = "default:" + raw["name"]
                if _record(conn, key):
                    continue
                users = {}
                for user in registry["users"]:
                    configured = config.users.get(user)
                    if configured is None:
                        _error("legacy_registry_incomplete")
                    # `briefings` is empty on the overlay-free load `istota init` runs this from.
                    explicit = next((b for b in [*configured.briefings, *configured.legacy_briefing_seeds]
                                     if b.name == raw["name"]), None)
                    users[user] = {"explicit": explicit is not None, "optout": not configured.default_briefings,
                                   "seed": asdict(explicit) if explicit else raw, "preserved": False}
                _write(conn, key, "preserving", {"legacy": raw, "users": users})
        # Each checkpoint owns its transaction. A failed user never resets earlier edits.
        keys = [row[0] for row in conn.execute("SELECT legacy_key FROM group_briefing_migrations WHERE state='preserving'")]
        for key in keys:
            record = _record(conn, key)
            for user in record["users"]:
                with _transaction(conn):
                    record = _record(conn, key)
                    entry = record["users"][user]
                    if entry["preserved"]:
                        continue
                    seed = BriefingConfig(**entry["seed"])
                    row = user_briefings.get_briefing(config.db_path, user, seed.name, conn=conn)
                    entry["origin"] = "existing" if row else "explicit" if entry["explicit"] else "materialized_default"
                    if row is None and entry["optout"] and not entry["explicit"]:
                        entry.update(preserved=True, origin="optout")
                    else:
                        if row is None:
                            row, _ = user_briefings.ensure_briefing(config.db_path, user_id=user, conn=conn, **_schedule(seed))
                        content_seed = seed if seed.blocks else row
                        path = config.module_db_path(user, "briefings")
                        bdb.init_db(path)
                        with bdb.connect(path) as content:
                            content.execute("BEGIN IMMEDIATE")
                            sentinel = _migrate._briefing_sentinel(seed.name)
                            if bdb.meta_get(content, sentinel) != "1":
                                _migrate._seed_blocks(content, seed.name, _migrate.block_specs(content_seed))
                                bdb.meta_set(content, sentinel, "1")
                            entry["before"] = {"schedule": _schedule(row),
                                               "blocks": _normalized_blocks(snapshot_blocks(content, seed.name, user))}
                            content.commit()
                        entry["preserved"] = True
                    _write(conn, key, "preserving", record)
            with _transaction(conn):
                _write(conn, key, "preserved", _record(conn, key))


def _current(conn, config, user, name):
    row = user_briefings.get_briefing(config.db_path, user, name, conn=conn)
    if row is None:
        configured = config.users.get(user)
        if (configured and any(item.name == name for item in configured.legacy_briefing_seeds)
                and user_briefings.config_import_pending(conn, user)):
            _error("legacy_personal_initialization_required")
        return None
    path = config.module_db_path(user, "briefings")
    if not path.exists():
        _error("legacy_content_unavailable")
    with bdb.connect(path) as content:
        blocks = _normalized_blocks(snapshot_blocks(content, name, user))
    return {"schedule": _schedule(row), "blocks": blocks}


def migration_report(conn, config, name=None):
    """Read-only candidates and journal state; no source bodies or personal archives."""
    records = []
    names = [name] if name else sorted({item.name for item in config.default_briefings} | {
        row[0].removeprefix("default:") for row in conn.execute(
            "SELECT legacy_key FROM group_briefing_migrations WHERE legacy_key LIKE 'default:%'")})
    for name in names:
        record = _record(conn, "default:" + name)
        if not record:
            records.append({"name": name, "state": "not_imported", "candidates": []})
            continue
        candidates = []
        legacy = BriefingConfig(**record["legacy"])
        baseline = {"schedule": _schedule(legacy), "blocks": _normalized_blocks(_migrate.block_specs(legacy))}
        # Rows that appeared after preservation are ambiguous too, never omitted.
        users = set(record.get("users", {})) | {row[0] for row in conn.execute(
            "SELECT user_id FROM briefing_configs WHERE name=?", (name,))}
        for user in sorted(users):
            entry = record.get("users", {}).get(user, {})
            current = _current(conn, config, user, name)
            unchanged = current == baseline
            attributable = entry.get("origin") == "materialized_default" and not entry.get("explicit")
            candidates.append({"user_id": user, "explicit_override": entry.get("explicit", False),
                               "optout": entry.get("optout", False), "unchanged": unchanged,
                               "proposed": "retire" if unchanged and attributable else "mute" if current is None else "unresolved",
                               "enabled": current["schedule"]["enabled"] if current else False,
                               "timezone": config.resolve_user_timezone(user, conn=conn), "current": current})
        definition = _live_definition(conn, record)
        records.append({"name": name, "state": record["state"], "definition_id": definition["id"] if definition else None,
                        "definition_name": definition["name"] if definition else None,
                        "revision": definition["revision"] if definition else None,
                        "timezone": definition["timezone"] if definition else None,
                        "source_issues": record.get("source_issues", []), "candidates": candidates,
                        "outcomes": record.get("outcomes", []), "not_before": record.get("not_before")})
    return records


def _live_definition(conn, record):
    """The record's definition, or None once an administrator deleted it."""
    if not record or not record["definition_id"]:
        return None
    definition = store.get_definition(conn, record["definition_id"])
    return None if definition is None or definition["deleted_at"] else definition


def _owned(conn, config, name, actor_id, expected_revision=None, *, key=None):
    record = _record(conn, key or "default:" + name)
    if not record or not record["definition_id"]:
        _error("migration_import_required")
    definition = _live_definition(conn, record)
    if definition is None:
        _error("migration_definition_deleted")
    store._require_author(conn, config, definition["group_id"], actor_id, operator_is_admin(conn, config, actor_id))
    if expected_revision is not None and definition["revision"] != expected_revision:
        _error("revision_conflict")
    return record, definition


def import_default(conn, config, name, *, actor_id, timezone):
    with _transaction(conn):
        group = reconcile_all_users_in_transaction(conn, config)
        store._require_author(conn, config, group["group_id"], actor_id, operator_is_admin(conn, config, actor_id))
        key = "default:" + name
        record = _record(conn, key)
        if _live_definition(conn, record):
            return migration_report(conn, config, name)[0]
        if not record:
            legacy = next((item for item in config.default_briefings if item.name == name), None)
            if legacy is None:
                _error("legacy_default_not_found")
            record = {"legacy": asdict(legacy), "users": {}}
        if record.get("state") == "preserving":
            _error("legacy_preservation_incomplete")
        legacy = BriefingConfig(**record["legacy"])
        blocks, issues = [], []
        for index, block in enumerate(_migrate.block_specs(legacy)):
            sources = []
            for pos, source in enumerate(block["sources"]):
                if source["kind"] not in ("browse", "markets", "email", "shared_block"):
                    issues.append({"block": index, "source": pos, "kind": source["kind"]})
                else:
                    sources.append({"id": f"source-{index}-{pos}", **source})
            if sources:
                blocks.append({**block, "sources": sources})
        if not blocks:
            issues.append({"kind": "no_supported_content"})
        channel = legacy.output if legacy.output in store.available_channels(config) else "in_app"
        if channel != legacy.output or legacy.conversation_token:
            issues.append({"kind": "delivery_requires_review"})
        generation = {"version": 1, "blocks": blocks} if blocks else {
            "version": 1, "prompt": "Replace this migration draft with the intended group content.",
            "research": {"enabled": False}, "inputs": []}
        # Leave room for a numeric suffix under the 100-character name limit.
        stem = re.sub(r"[^a-z0-9_-]+", "-", legacy.name.lower())[:90].strip("-_") or "briefing"
        target_name = stem
        suffix = 2
        while conn.execute("SELECT 1 FROM group_briefing_configs WHERE group_id=? AND name=?",
                           (group["group_id"], target_name)).fetchone():
            target_name = f"{stem}-{suffix}"
            suffix += 1
        definition = store.save_definition(conn, config, group["group_id"], {
            "name": target_name, "title": legacy.title, "cron": legacy.cron, "timezone": timezone,
            "enabled": False, "channel": channel, "generation_mode": "blocks" if blocks else "prompt",
            "generation": generation}, actor_id=actor_id, is_admin=True, expected_revision=0, caller_transaction=True)
        record.update(source_issues=issues, imported_generation=definition["generation"], imported_revision=definition["revision"])
        _write(conn, key, "imported", record, definition["id"])
        for user, entry in record.get("users", {}).items():
            if entry.get("optout"):
                conn.execute("INSERT OR IGNORE INTO group_briefing_group_preferences "
                             "(group_id,user_id,outbound_muted,in_app_muted) VALUES (?,?,1,1)", (group["group_id"], user))
            if entry.get("before", {}).get("schedule", {}).get("enabled") is False:
                conn.execute("INSERT OR IGNORE INTO group_briefing_preferences "
                             "(definition_id,user_id,outbound_muted,in_app_muted) VALUES (?,?,1,1)", (definition["id"], user))
        return migration_report(conn, config, name)[0]


def review_default(conn, config, name, *, actor_id, expected_revision, timezone, dispositions,
                   accept_source_changes=False):
    with _transaction(conn):
        record, definition = _owned(conn, config, name, actor_id, expected_revision)
        if record["state"] == "cutover":
            _error("migration_already_active")
        if timezone != definition["timezone"]:
            _error("migration_timezone_review_required")
        if record["source_issues"] and not accept_source_changes:
            _error("migration_sources_unresolved")
        if any(issue["kind"] == "no_supported_content" for issue in record["source_issues"]):
            if definition["generation"] == record["imported_generation"]:
                _error("migration_sources_unresolved")
        candidates = migration_report(conn, config, name)[0]["candidates"]
        if set(dispositions) - {item["user_id"] for item in candidates}:
            _error("migration_unknown_candidate")
        reviewed = {}
        for item in candidates:
            choice = dispositions.get(item["user_id"], item["proposed"])
            if choice not in ("retire", "keep", "mute"):
                _error("migration_candidate_unresolved")
            reviewed[item["user_id"]] = {"choice": choice, "current": item["current"], "timezone": item["timezone"]}
        record["review"] = {"revision": definition["revision"], "timezone": timezone, "users": reviewed,
                            "source_changes": accept_source_changes}
        _write(conn, "default:" + name, "reviewed", record, definition["id"])
        # Review cannot create personal-source approval.
        store._validate_run_sources(conn, config, definition)
        return migration_report(conn, config, name)[0]


def validate_migration(conn, definition, *, activation=False, due_at=None):
    if definition.get("id") is None:
        return
    row = conn.execute("SELECT legacy_key FROM group_briefing_migrations WHERE definition_id=?", (definition["id"],)).fetchone()
    if row is None:
        return
    record = _record(conn, row[0])
    if activation and record["state"] != "cutover":
        _error("migration_cutover_required")
    if record["legacy_key"].startswith("publisher:"):
        if (record.get("source_issues") and definition["generation_mode"] == "prompt"
                and definition["generation"]["prompt"] == record["imported_generation"]["prompt"]):
            _error("migration_sources_unresolved")
    elif record.get("source_issues") and record["state"] != "cutover":
        if record.get("review", {}).get("revision") != definition.get("revision"):
            _error("migration_sources_unresolved")
    if due_at is not None and record.get("not_before") and due_at <= record["not_before"]:
        _error("migration_transition_occurrence")


def cutover_default(conn, config, name, *, actor_id, expected_revision, now=None):
    with ExitStack() as locks, _transaction(conn):
        record, definition = _owned(conn, config, name, actor_id, expected_revision)
        if record["state"] == "cutover":
            return migration_report(conn, config, name)[0]
        review = record.get("review", {})
        if review.get("revision") != definition["revision"]:
            _error("migration_review_required")
        for user, item in sorted(review["users"].items()):
            if item["current"] is not None:
                content = locks.enter_context(bdb.connect(config.module_db_path(user, "briefings")))
                content.execute("BEGIN IMMEDIATE")
        current = migration_report(conn, config, name)[0]["candidates"]
        if {item["user_id"]: (item["current"], item["timezone"]) for item in current} != {
                user: (item["current"], item["timezone"]) for user, item in review["users"].items()}:
            _error("migration_candidates_changed")
        outcomes = []
        for user, item in review["users"].items():
            if item["choice"] == "keep":
                conn.execute("INSERT INTO group_briefing_preferences (definition_id,user_id,outbound_muted,in_app_muted) "
                             "VALUES (?,?,1,1) ON CONFLICT(definition_id,user_id) DO UPDATE SET outbound_muted=1,in_app_muted=1",
                             (definition["id"], user))
                continue
            tasks = conn.execute("SELECT id,status FROM tasks WHERE source_type='briefing' AND user_id=? AND briefing_name=?",
                                 (user, name)).fetchall()
            if any(task["status"] not in ("pending", "completed", "failed", "cancelled") for task in tasks):
                _error("legacy_tasks_inflight")
            cancelled = user_briefings.cancel_queued_tasks(conn, user, name,
                                                           statuses=("pending", "failed"))
            for task in tasks:
                outcomes.append({"task_id": task["id"], "before": task["status"],
                                 "outcome": "cancelled" if task["id"] in cancelled else task["status"]})
            row = user_briefings.get_briefing(config.db_path, user, name, conn=conn)
            if row:
                user_briefings.ensure_briefing(config.db_path, user_id=user, conn=conn, **(_schedule(row) | {"enabled": False}))
            if item["choice"] == "mute":
                conn.execute("INSERT INTO group_briefing_preferences (definition_id,user_id,outbound_muted,in_app_muted) "
                             "VALUES (?,?,1,1) ON CONFLICT(definition_id,user_id) DO UPDATE SET outbound_muted=1,in_app_muted=1",
                             (definition["id"], user))
        record.update(outcomes=outcomes, not_before=store._timestamp(now))
        _write(conn, "default:" + name, "cutover", record, definition["id"])
        store.save_definition(conn, config, definition["group_id"], {key: definition[key] for key in PAYLOAD_FIELDS} | {"enabled": True},
                              actor_id=actor_id, is_admin=operator_is_admin(conn, config, actor_id),
                              expected_revision=expected_revision, definition_id=definition["id"], caller_transaction=True)
        return migration_report(conn, config, name)[0]


def rollback_default(conn, config, name, *, actor_id, expected_revision):
    with _transaction(conn):
        record, definition = _owned(conn, config, name, actor_id, expected_revision)
        if record["state"] != "cutover":
            _error("migration_not_active")
        if conn.execute("SELECT 1 FROM group_briefing_recipients WHERE run_id IN "
                        "(SELECT id FROM group_briefing_runs WHERE definition_id=?) AND state IN ('sending','uncertain')",
                        (definition["id"],)).fetchone():
            _error("group_deliveries_inflight")
        # Restore admission in the same transaction as the old schedules.
        _write(conn, "default:" + name, "rolled_back", record, definition["id"])
        for user, item in record["review"]["users"].items():
            if item["choice"] == "keep" or item["current"] is None:
                continue
            before = item["current"]["schedule"]
            row = user_briefings.get_briefing(config.db_path, user, name, conn=conn)
            if row is None or _schedule(row) != before | {"enabled": False}:
                _error("legacy_schedule_changed")
            user_briefings.ensure_briefing(config.db_path, user_id=user, conn=conn, **before)
            db.set_briefing_last_run(conn, user, name)
        store.save_definition(conn, config, definition["group_id"], {key: definition[key] for key in PAYLOAD_FIELDS} | {"enabled": False},
                              actor_id=actor_id, is_admin=operator_is_admin(conn, config, actor_id),
                              expected_revision=expected_revision, definition_id=definition["id"], caller_transaction=True)
        record.pop("review", None)
        _write(conn, "default:" + name, "rolled_back", record, definition["id"])
        return migration_report(conn, config, name)[0]


def require_personal_schedule(conn, user_id, name):
    """Serialize admission with cutover even when the caller holds cached config."""
    key = "default:" + name
    conn.execute("UPDATE group_briefing_migrations SET state=state WHERE legacy_key=?", (key,))
    record = _record(conn, key)
    if record and record["state"] == "cutover":
        item = record["review"]["users"].get(user_id)
        if item and item["choice"] != "keep":
            raise ValueError("briefing_retired")


def _publisher_key(user_id, name):
    return "publisher:" + store._json([user_id, name])


def publisher_retired(conn, user_id, name):
    # Take the same write lock as cutover before a cached job can be admitted.
    key = _publisher_key(user_id, name)
    conn.execute("UPDATE group_briefing_migrations SET state=state WHERE legacy_key=?", (key,))
    record = _record(conn, key)
    return bool(record and record["state"] == "cutover")


def _publisher_job(conn, config, user_id, name, actor_id):
    from istota.cron_loader import FILE_OWNED_COLUMNS
    if not operator_is_admin(conn, config, actor_id):
        raise GroupError(403, "publisher_admin_required", "Publishing migration requires an installation admin")
    job = db.get_scheduled_job_by_name(conn, user_id, name)
    if job is None or name.startswith("_module."):
        _error("publisher_not_found")
    return job, {field: getattr(job, field) for field in sorted(FILE_OWNED_COLUMNS)}


def _publisher_consumers(conn, config, outputs, dependent_jobs):
    """Inventory structured references and operator-declared opaque jobs, without reading scripts."""
    from istota.briefings.sources.kv import SHARED_BLOCK_NAMESPACE
    consumers = {}

    def collect(identity, sources):
        used = {source.get("config", {}).get("name") or source.get("config", {}).get("key")
                for source in sources if source["kind"] == "shared_block" and source.get("enabled", True)}
        targets = [SHARED_BLOCK_NAMESPACE + "/" + name for name in used if name]
        matched = sorted(set(targets) & set(outputs))
        if matched:
            consumers[identity] = {"id": identity, "outputs": matched}

    users = set(config.users) | {row[0] for row in conn.execute("SELECT DISTINCT user_id FROM briefing_configs")}
    for user in sorted(users):
        configured = config.users.get(user)
        definitions = {item.name: _migrate.block_specs(item) for item in configured.briefings} if configured else {}
        path = config.module_db_path(user, "briefings")
        if path.exists():
            with bdb.connect(path) as content:
                names = set(bdb.list_briefing_names(content))
                for name in names | set(definitions):
                    if name in names or bdb.meta_get(content, _migrate._briefing_sentinel(name)) == "1":
                        definitions[name] = snapshot_blocks(content, name, user)
        for name, blocks in definitions.items():
            migrated = _record(conn, "default:" + name)
            if migrated and migrated["state"] == "cutover" and migrated["review"]["users"].get(user, {}).get("choice") in ("retire", "mute"):
                continue
            collect(f"personal:{user}:{name}", [source for block in blocks for source in block["sources"]])
    for row in conn.execute("SELECT id FROM group_briefing_configs WHERE deleted_at IS NULL ORDER BY id"):
        definition = store.get_definition(conn, row[0])
        collect(f"group:{definition['id']}", store.definition_sources(definition))
    for block in config.briefing_shared_blocks:
        collect("shared:" + block.name, [asdict(source) for source in block.sources])
    for job_id in dependent_jobs:
        job = db.get_scheduled_job(conn, job_id)
        if job is None:
            _error("dependent_job_not_found")
        identity = "job:" + str(job_id)
        consumers[identity] = {"id": identity, "outputs": outputs, "unsupported": "operator_declared_dependency"}
    return sorted(consumers.values(), key=lambda item: item["id"])


def publisher_report(conn, config, user_id, name, *, actor_id):
    _publisher_job(conn, config, user_id, name, actor_id)
    record, definition = _owned(conn, config, name, actor_id, key=_publisher_key(user_id, name))
    return {"producer_user": user_id, "producer_name": name, "definition_id": definition["id"],
            "revision": definition["revision"], "state": record["state"], "outputs": record["outputs"],
            "source_issues": record.get("source_issues", []) if (definition["generation_mode"] == "prompt"
                and definition["generation"]["prompt"] == record["imported_generation"].get("prompt")) else [],
            "consumers": _publisher_consumers(conn, config, record["outputs"], record["dependent_jobs"]),
            "inventory_limit": "Script and prompt dependencies require operator review; no scripts are read or executed.",
            "outcomes": record.get("outcomes", [])}


def import_publisher(conn, config, user_id, name, document, *, actor_id):
    from istota.scheduler import _parse_shared_kv_target
    from istota.briefings.sources.kv import SHARED_BLOCK_NAMESPACE
    if not isinstance(document, dict) or not isinstance(document.get("group_id"), str):
        raise ValueError("Publishing import requires a group_id and a definition")
    with _transaction(conn):
        job, before = _publisher_job(conn, config, user_id, name, actor_id)
        key = _publisher_key(user_id, name)
        if _record(conn, key):
            return publisher_report(conn, config, user_id, name, actor_id=actor_id)
        targets = document.get("outputs", [job.publish_shared_kv] if job.publish_shared_kv else [])
        if not isinstance(targets, list) or not targets or any(not isinstance(value, str) for value in targets):
            _error("publisher_outputs_required")
        outputs = sorted({"/".join(_parse_shared_kv_target(value)) for value in targets})
        if any(not value.startswith(SHARED_BLOCK_NAMESPACE + "/") or not value.split("/", 1)[1] for value in outputs):
            _error("publisher_output_unsupported")
        if job.publish_shared_kv and "/".join(_parse_shared_kv_target(job.publish_shared_kv)) not in outputs:
            _error("publisher_output_missing")
        dependents = document.get("dependent_jobs")
        if not isinstance(dependents, list) or any(type(value) is not int or value == job.id for value in dependents):
            _error("publisher_dependency_inventory_required")
        value = {field: document[field] for field in PAYLOAD_FIELDS if field in document}
        issues = []
        if "generation" not in value:
            issues = [{"kind": "replacement_prompt_required"}]
            value.update(generation_mode="prompt", generation={"version": 1,
                "prompt": "Supply the intended replacement prompt before preview.", "research": {"enabled": False}, "inputs": []})
        value["enabled"] = False
        definition = store.save_definition(conn, config, document["group_id"], value, actor_id=actor_id,
            is_admin=True, expected_revision=0, caller_transaction=True)
        record = {"producer_user": user_id, "producer_name": name, "before": before, "outputs": outputs,
                  "dependent_jobs": dependents, "source_issues": issues, "imported_generation": definition["generation"],
                  "disabled_at": job.disabled_at}
        _write(conn, key, "imported", record, definition["id"])
        return publisher_report(conn, config, user_id, name, actor_id=actor_id)


def cutover_publisher(conn, config, user_id, name, *, actor_id, expected_revision, replacements, confirmed=False, now=None):
    from istota.scheduler import _parse_shared_kv_target
    if not isinstance(replacements, dict):
        raise ValueError("Replacement producers must be a map of consumer ids to job ids")
    with ExitStack() as locks, _transaction(conn):
        job, current = _publisher_job(conn, config, user_id, name, actor_id)
        key = _publisher_key(user_id, name)
        record, definition = _owned(conn, config, name, actor_id, expected_revision, key=key)
        if record["state"] == "cutover":
            return publisher_report(conn, config, user_id, name, actor_id=actor_id)
        if not confirmed:
            _error("publisher_confirmation_required")
        if current != record["before"]:
            _error("publisher_changed")
        preview = conn.execute("SELECT 1 FROM group_briefing_runs WHERE definition_id=? AND state='preview_ready' "
                               "AND json_extract(snapshot_json, '$.definition.revision')=?",
                               (definition["id"], definition["revision"])).fetchone()
        if not preview:
            _error("publisher_preview_required")
        store._validate_run_sources(conn, config, definition)
        # Freeze the same module stores whose source references the inventory reads.
        users = set(config.users) | {row[0] for row in conn.execute("SELECT DISTINCT user_id FROM briefing_configs")}
        for user in sorted(users):
            path = config.module_db_path(user, "briefings")
            if path.exists():
                locks.enter_context(bdb.connect(path)).execute("BEGIN IMMEDIATE")
        consumers = _publisher_consumers(conn, config, record["outputs"], record["dependent_jobs"])
        for consumer in consumers:
            replacement_id = replacements.get(consumer["id"])
            replacement = db.get_scheduled_job(conn, replacement_id) if type(replacement_id) is int else None
            if (not replacement or replacement.id == job.id or not replacement.enabled or replacement.auto_disabled_at
                    or not operator_is_admin(conn, config, replacement.user_id)
                    or publisher_retired(conn, replacement.user_id, replacement.name)
                    or consumer["outputs"] != ["/".join(_parse_shared_kv_target(replacement.publish_shared_kv))]):
                _error("publisher_consumer_unresolved")
        tasks = conn.execute("SELECT id,status FROM tasks WHERE scheduled_job_id=?", (job.id,)).fetchall()
        if any(task["status"] not in ("pending", "failed", "completed", "cancelled") for task in tasks):
            _error("legacy_tasks_inflight")
        outcomes = []
        for task in tasks:
            if task["status"] in ("pending", "failed"):
                db.cancel_task(conn, task["id"])
            outcomes.append({"task_id": task["id"], "before": task["status"],
                             "outcome": "cancelled" if task["status"] in ("pending", "failed") else task["status"]})
        db.disable_scheduled_job(conn, job.id)
        record.update(outcomes=outcomes, replacements=replacements, not_before=store._timestamp(now))
        _write(conn, key, "cutover", record, definition["id"])
        store.save_definition(conn, config, definition["group_id"], {field: definition[field] for field in PAYLOAD_FIELDS} | {"enabled": True},
            actor_id=actor_id, is_admin=True, expected_revision=expected_revision, definition_id=definition["id"], caller_transaction=True)
        return publisher_report(conn, config, user_id, name, actor_id=actor_id)


def rollback_publisher(conn, config, user_id, name, *, actor_id, expected_revision):
    with _transaction(conn):
        job, current = _publisher_job(conn, config, user_id, name, actor_id)
        key = _publisher_key(user_id, name)
        record, definition = _owned(conn, config, name, actor_id, expected_revision, key=key)
        if record["state"] != "cutover":
            _error("migration_not_active")
        if current != record["before"] | {"enabled": False}:
            _error("publisher_changed")
        if conn.execute("SELECT 1 FROM group_briefing_recipients WHERE run_id IN "
                        "(SELECT id FROM group_briefing_runs WHERE definition_id=?) AND state IN ('sending','uncertain')",
                        (definition["id"],)).fetchone():
            _error("group_deliveries_inflight")
        store.save_definition(conn, config, definition["group_id"], {field: definition[field] for field in PAYLOAD_FIELDS} | {"enabled": False},
            actor_id=actor_id, is_admin=True, expected_revision=expected_revision, definition_id=definition["id"], caller_transaction=True)
        _write(conn, key, "rolled_back", record, definition["id"])
        conn.execute("UPDATE scheduled_jobs SET enabled=?,disabled_at=?,last_run_at=datetime('now') WHERE id=?",
                     (record["before"]["enabled"], record["disabled_at"], job.id))
        return publisher_report(conn, config, user_id, name, actor_id=actor_id)
