"""Shared definitions and references, using the framework's existing store."""

from contextlib import closing, contextmanager
from copy import deepcopy
import sqlite3

from istota import db, user_profiles
from istota.briefings.models import validate_shared_block_payload
from istota.config import BriefingSharedBlock
from istota.config_editor.secrets import redact_document
from istota.lib.sqlite_util import connect_read_only
from istota.settings_plan.briefings import snapshot_blocks
from istota.settings_plan.engine import ExecutionError, Operation, Unit, state_digest
from istota.settings_plan.profiles import _load_config
from istota.settings_plan.schema import SHARED_FIELDS, SchemaError, parse_plan


def _definition(row):
    definition = {field: deepcopy(getattr(row, field)) for field in SHARED_FIELDS}
    definition["directive"] = definition["directive"] or ""
    return definition


def snapshot_shared_blocks(conn):
    """Read complete live definitions, refusing invalid stored content."""
    try:
        definitions = [_definition(row) for row in db.list_shared_block_configs(conn, strict=True)]
        parse_plan({"shared_briefing_blocks": definitions})
        for definition in definitions:
            validate_shared_block_payload(definition)
            for source in definition["sources"]:
                source.setdefault("config", {})
        return definitions
    except (ValueError, SchemaError):
        raise ExecutionError("shared_block_store_invalid", ["shared_briefing_blocks"]) from None


def _read_shared(config):
    try:
        with closing(connect_read_only(config.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            return {item["name"]: item for item in snapshot_shared_blocks(conn)}
    except (sqlite3.Error, OSError):
        raise ExecutionError("shared_block_store_unavailable", ["shared_briefing_blocks"]) from None


def _merge_shared(existing, declarations):
    desired = deepcopy(existing)
    for index, declaration in enumerate(declarations):
        name = declaration["name"]
        merged = deepcopy(existing.get(name, _definition(BriefingSharedBlock(name=name, cron=""))))
        merged.update(deepcopy(declaration))
        try:
            merged = validate_shared_block_payload(merged)
        except ValueError:
            raise ExecutionError("validation_error", ["shared_briefing_blocks", index]) from None
        if merged["name"] != name:
            raise ExecutionError("validation_error", ["shared_briefing_blocks", index, "name"])
        merged["directive"] = merged["directive"] or ""
        desired[name] = merged
    return desired


def plan_shared_blocks(plan, user, context, options):
    if context.config is None:
        context.config = _load_config(plan, context)
    config = context.config
    existing = _read_shared(config)
    desired = _merge_shared(existing, plan.shared_briefing_blocks)
    retained = {item["name"] for item in plan.shared_briefing_blocks}
    removed = existing.keys() - retained if options.prune else set()
    if options.prune:
        for index, seed in enumerate(config.briefing_shared_blocks):
            if seed.name not in retained and seed.cron:
                raise ExecutionError("shared_block_seed_conflict", ["config", "briefing_shared_blocks", index])

    @contextmanager
    def transaction():
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def digest(conn):
        return state_digest({item["name"]: item for item in snapshot_shared_blocks(conn)})

    expected = state_digest(existing)
    upserts = []
    for name in sorted(retained):
        before, after = existing.get(name), desired[name]
        if before != after:
            upserts.append(Operation("shared_briefing_blocks", name, "create" if before is None else "update",
                                     changes=[{"path": [field], "before": redact_document(before[field]) if before else None,
                                               "after": redact_document(after[field])}
                                              for field in SHARED_FIELDS if before is None or before[field] != after[field]],
                                     expected_state=expected, restart_required=True))

    def write(conn, operations):
        for operation in operations:
            db.upsert_shared_block_config(conn, **desired[operation.identity])

    if upserts:
        context.units.append(Unit("shared_briefing_blocks", None, upserts, transaction, digest, write))
    if removed:
        # The delete unit follows this plan's committed upserts.
        expected = state_digest(desired)
        operations = [Operation("shared_briefing_blocks", name, "delete", expected_state=expected,
                                restart_required=True) for name in sorted(removed)]

        def delete(conn, operations):
            try:
                _check_references(plan, context, options, conn, set(desired) - removed,
                                  removed, apply_updates=False, all_users=True)
            except ExecutionError:
                raise ExecutionError("state_changed", ["shared_briefing_blocks"]) from None
            for operation in operations:
                db.delete_shared_block_config(conn, operation.identity)

        context.units.append(Unit("shared_briefing_blocks", None, operations, transaction, digest, delete, phase="delete"))


def _check_blocks(blocks, available, removed, path, *, retained_only=False):
    for index, block in enumerate(blocks):
        for slot, source in enumerate(block.get("sources", [])):
            if source.get("kind") != "shared_block":
                continue
            config = source.get("config", {})
            name = config.get("name") or config.get("key") or ""
            if not isinstance(name, str) or name in removed or (not retained_only and name not in available):
                raise ExecutionError("shared_block_reference", [*path, index, "sources", slot, "config", "name"])


def _available_names(config, conn, available):
    available = set(available)
    available.update(seed.name for seed in config.briefing_shared_blocks)
    # Custom publishers have no definition row; keep their live keys valid.
    available.update(row[0] for row in conn.execute(
        "SELECT key FROM shared_kv WHERE namespace = 'briefing_shared_blocks'"))
    return available


def validate_live_references(config, user, definitions):
    with closing(connect_read_only(config.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        available = _available_names(config, conn, {row["name"] for row in snapshot_shared_blocks(conn)})
        for name, blocks in definitions.items():
            _check_blocks(blocks, available, set(), ["users", user, "briefings", name, "blocks"])


def _check_references(plan, context, options, conn, available, removed, *, apply_updates, all_users):
    available = _available_names(context.config, conn, available)
    users = set(plan.users or {})
    profiles = {}
    if all_users:
        profiles = {row[0]: user_profiles.get_profile(None, row[0], conn=conn)
                    for row in conn.execute("SELECT user_id FROM user_profiles")}
        users.update(profiles)
        users.update(context.config.users)
        users.update(row[0] for row in conn.execute("SELECT DISTINCT user_id FROM briefing_configs"))
    for user in sorted(users):
        path = context.config.module_db_path(user, "briefings")
        definitions = {}
        try:
            try:
                path.stat()
            except FileNotFoundError:
                pass
            else:
                from istota.briefings import db as bdb
                with closing(connect_read_only(path)) as module:
                    module.row_factory = sqlite3.Row
                    module.execute("BEGIN")
                    definitions = {name: snapshot_blocks(module, name, user) for name in bdb.list_briefing_names(module)}
        except (sqlite3.Error, OSError):
            raise ExecutionError("briefing_store_unavailable", ["users", user, "briefing_content"]) from None
        if apply_updates:
            definitions.update(context.briefing_blocks.get(user, {}))
        for name, blocks in definitions.items():
            _check_blocks(blocks, available, removed, ["users", user, "briefings", name, "blocks"],
                          retained_only=user not in context.briefing_blocks)
        if not removed:
            continue
        configured = context.config.users.get(user)
        if configured:
            for index, seed in enumerate(configured.briefings):
                _check_blocks(seed.blocks, available, removed,
                              ["config", "users", user, "briefings", index, "blocks"], retained_only=True)
        profile = context.profiles.get(user) or profiles.get(user)
        defaults = profile.default_briefings if profile else configured.default_briefings if configured else True
        if defaults:
            explicit = {seed.name for seed in configured.briefings} if configured else set()
            for index, seed in enumerate(context.config.default_briefings):
                if seed.name not in explicit:
                    _check_blocks(seed.blocks, available, removed, ["config", "default_briefings", index, "blocks"],
                                  retained_only=True)


def validate_references(plan, context, options):
    existing = _read_shared(context.config)
    desired = existing
    removed = set()
    if plan.shared_briefing_blocks is not None:
        desired = _merge_shared(existing, plan.shared_briefing_blocks)
        if options.prune:
            removed = existing.keys() - {item["name"] for item in plan.shared_briefing_blocks}
    available = set(desired) - removed
    try:
        with closing(connect_read_only(context.config.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            _check_references(plan, context, options, conn, available, removed,
                              apply_updates=True, all_users=bool(removed))
    except (sqlite3.Error, OSError):
        raise ExecutionError("shared_block_store_unavailable", ["shared_briefing_blocks"]) from None
