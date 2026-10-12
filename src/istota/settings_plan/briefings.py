"""Framework schedules and ordered content in separate store transactions."""

from contextlib import closing, contextmanager
from copy import deepcopy
import sqlite3

from croniter import croniter

from istota import db, user_briefings
from istota.briefings import db as bdb
from istota.briefings.models import BriefingBlock, BlockSource, parse_json_dict
from istota.config import BriefingConfig
from istota.config_editor.secrets import redact_document
from istota.lib.sqlite_util import connect_read_only
from istota.settings_plan.engine import ExecutionError, Operation, Unit, state_digest
from istota.settings_plan.profiles import validate_destination
from istota.settings_plan.schema import SchemaError, validate_user_plan
from istota.transport import parse_output_target

SCHEDULE_FIELDS = ("name", "cron", "title", "conversation_token", "output", "enabled")


def snapshot_schedules(conn, user):
    """Complete portable definitions, excluding runtime and legacy components."""
    definitions = [{name: getattr(row, name) for name in SCHEDULE_FIELDS}
                   for row in user_briefings.list_briefings(None, user, conn=conn)]
    try:
        validate_user_plan(user, {"briefings": definitions})
    except SchemaError:
        raise ExecutionError("briefing_store_invalid", ["users", user, "briefings"]) from None
    return definitions


def _schedule_digest(conn, user):
    # Components are not exported, but ensure must not replace a concurrent edit.
    rows = user_briefings.list_briefings(None, user, conn=conn)
    return state_digest([{"definition": {name: getattr(row, name) for name in SCHEDULE_FIELDS},
                          "components": row.components} for row in rows])


def _validate_schedule(conn, config, user, definition, path):
    if not croniter.is_valid(definition["cron"]):
        raise ExecutionError("validation_error", [*path, "cron"])
    try:
        validate_destination(conn, config, user, definition["output"])
    except ValueError:
        raise ExecutionError("validation_error", [*path, "output"]) from None
    token = definition["conversation_token"]
    if any(dest.surface == "talk" for dest in parse_output_target(definition["output"])) and not token:
        raise ExecutionError("validation_error", [*path, "conversation_token"])
    if token:
        try:
            validate_destination(conn, config, user, "talk:" + token)
        except ValueError:
            raise ExecutionError("validation_error", [*path, "conversation_token"]) from None


def _check_schedule_seeds(config, user, retained):
    explicit = config.users[user].briefings
    for index, definition in enumerate(explicit):
        if definition.name not in retained and definition.cron:
            raise ExecutionError("briefing_seed_conflict", ["config", "users", user, "briefings", index])
    if config.users[user].default_briefings:
        explicit_names = {definition.name for definition in explicit}
        for index, definition in enumerate(config.default_briefings):
            if definition.name not in retained | explicit_names and definition.cron:
                raise ExecutionError("briefing_seed_conflict", ["config", "default_briefings", index])


def plan_schedules(plan, user, context, options):
    config = context.config
    path = ["users", user, "briefings"]
    declarations = plan.users[user].sections["briefings"]
    desired = {}
    operations = []
    try:
        with closing(connect_read_only(config.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            existing = {item["name"]: item for item in snapshot_schedules(conn, user)}
            expected = _schedule_digest(conn, user)
            for index, declaration in enumerate(declarations):
                identity = declaration["name"]
                previous = existing.get(identity)
                defaults = BriefingConfig(name=identity, cron="")
                merged = deepcopy(previous) if previous else {
                    name: getattr(defaults, name) for name in SCHEDULE_FIELDS if name != "enabled"}
                if previous is None:
                    merged["enabled"] = True
                merged.update({name: deepcopy(value) for name, value in declaration.items() if name != "blocks"})
                _validate_schedule(conn, config, user, merged, [*path, index])
                desired[identity] = merged
                if previous == merged:
                    continue
                changes = [{"path": [name], "before": previous[name] if previous else None, "after": merged[name]}
                           for name in SCHEDULE_FIELDS[1:] if previous is None or previous[name] != merged[name]]
                operations.append(Operation("briefings", identity, "create" if previous is None else "update", user=user,
                                            changes=changes, expected_state=expected, restart_required=True))
    except (sqlite3.Error, OSError):
        raise ExecutionError("briefing_store_unavailable", path) from None
    if options.prune:
        _check_schedule_seeds(config, user, set(desired))
        for identity in sorted(existing.keys() - desired.keys()):
            operations.append(Operation("briefings", identity, "delete", user=user,
                                        expected_state=expected, restart_required=True))
    plan_blocks(plan, user, context, options)
    if not operations:
        return

    @contextmanager
    def transaction():
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def write(conn, operations):
        for operation in operations:
            identity = operation.identity
            if operation.action == "delete":
                user_briefings.delete_briefing(config.db_path, user, identity, conn=conn)
            else:
                definition = desired[identity]
                try:
                    _validate_schedule(conn, config, user, definition, path)
                except ExecutionError:
                    raise ExecutionError("state_changed", path) from None
                previous = user_briefings.get_briefing(config.db_path, user, identity, conn=conn)
                user_briefings.ensure_briefing(config.db_path, user_id=user, **definition,
                                              components=previous.components if previous else None, conn=conn)

    context.units.append(Unit("briefings", user, operations, transaction,
                              lambda conn: _schedule_digest(conn, user), write, expected_state=expected))


def _content_state(conn, selected=None):
    # Read raw configuration columns so malformed JSON cannot disappear through a decoder.
    definitions = {}
    for name in bdb.list_briefing_names(conn):
        if selected is not None and name not in selected:
            continue
        blocks = conn.execute(
            "SELECT id, position, title, directive, render_mode, options FROM briefing_blocks "
            "WHERE briefing_name = ? ORDER BY position, id", (name,)).fetchall()
        definitions[name] = []
        for block in blocks:
            sources = conn.execute(
                "SELECT id, position, kind, config, enabled FROM briefing_block_sources "
                "WHERE block_id = ? ORDER BY position, id", (block["id"],)).fetchall()
            definitions[name].append({"block": dict(block), "sources": [dict(row) for row in sources]})
    return definitions


BLOCK_FIELDS = ("title", "directive", "render_mode", "options")
SOURCE_FIELDS = ("kind", "config", "enabled")


def snapshot_blocks(conn, name, user):
    """Read complete ordered definitions; refuse malformed stored JSON."""
    result = []
    try:
        for row in _content_state(conn, {name}).get(name, []):
            block = {field: row["block"][field] for field in BLOCK_FIELDS}
            block["directive"] = "" if block["directive"] is None else block["directive"]
            block["options"] = parse_json_dict(block["options"], strict=True)
            block["sources"] = []
            for source in row["sources"]:
                if source["enabled"] not in (0, 1):
                    raise ValueError("invalid enabled flag")
                block["sources"].append({"kind": source["kind"], "enabled": bool(source["enabled"]),
                                         "config": parse_json_dict(source["config"], strict=True)})
            result.append(block)
        _validate_blocks(result, user, ["users", user, "briefings", name, "blocks"])
    except (ValueError, SchemaError, ExecutionError):
        raise ExecutionError("briefing_store_invalid", ["users", user, "briefing_content"]) from None
    return result


def _validate_blocks(blocks, user, path):
    # Schema owns field types and the domain's source/render-mode sets.
    validate_user_plan(user, {"briefings": [{"name": "definition", "blocks": blocks}]})
    for index, block in enumerate(blocks):
        if not block["title"].strip():
            raise ExecutionError("validation_error", [*path, index, "title"])
        for slot, source in enumerate(block["sources"]):
            if not source["kind"]:
                raise ExecutionError("validation_error", [*path, index, "sources", slot, "kind"])


def _merge_prefix(previous, declarations, defaults, path, context, prune, *, blocks=False):
    merged = []
    for index, declaration in enumerate(declarations):
        item = deepcopy(previous[index] if index < len(previous) else defaults)
        for field, value in declaration.items():
            if blocks and field == "sources":
                source = BlockSource(0, 0, 0, "")
                source_defaults = {name: getattr(source, name) for name in SOURCE_FIELDS}
                item[field] = _merge_prefix(item[field], value, source_defaults,
                                            [*path, index, field], context, prune)
            else:
                item[field] = deepcopy(value)
        merged.append(item)
    if not prune and len(previous) > len(declarations):
        merged.extend(deepcopy(previous[len(declarations):]))
        context.warnings.append("Preserved briefing array tail at " + ".".join(map(str, path)))
    return merged


def plan_blocks(plan, user, context, options):
    declarations = plan.users[user].sections["briefings"]
    selected = {item["name"] for item in declarations if "blocks" in item}
    if not selected and not options.prune:
        return
    database = context.config.module_db_path(user, "briefings")
    path = ["users", user, "briefing_content"]
    before, existing = {}, {}
    # Prune includes orphan definitions left by a partially committed schedule prune.
    scope = None if options.prune else selected
    try:
        try:
            database.stat()
        except FileNotFoundError:
            pass
        else:
            with closing(connect_read_only(database)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN")
                before = _content_state(conn, scope)
                existing = {name: snapshot_blocks(conn, name, user) for name in before}
    except (sqlite3.Error, OSError):
        raise ExecutionError("briefing_store_unavailable", path) from None
    expected = state_digest(before)
    desired, operations = {}, []
    for index, declaration in enumerate(declarations):
        if "blocks" not in declaration:
            continue
        name = declaration["name"]
        block = BriefingBlock(0, name, 0, "", directive="")
        defaults = {field: getattr(block, field) for field in (*BLOCK_FIELDS, "sources")}
        block_path = ["users", user, "briefings", index, "blocks"]
        merged = _merge_prefix(existing.get(name, []), declaration["blocks"], defaults,
                               block_path, context, options.prune, blocks=True)
        try:
            _validate_blocks(merged, user, block_path)
        except SchemaError:
            raise ExecutionError("validation_error", block_path) from None
        desired[name] = merged
        if merged != existing.get(name, []):
            operations.append(Operation("briefing_content", name, "update" if name in existing else "create", user=user,
                                        changes=[{"path": ["blocks"], "before": redact_document(existing.get(name, [])),
                                                  "after": redact_document(merged)}], expected_state=expected))
    if options.prune:
        retained = {item["name"] for item in declarations}
        for name in sorted(existing.keys() - retained):
            operations.append(Operation("briefing_content", name, "delete", user=user, expected_state=expected))
    if not operations:
        return

    @contextmanager
    def transaction():
        if not database.exists():
            bdb.init_db(database)
        with bdb.connect(database) as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()

    def write(conn, operations):
        for operation in operations:
            if operation.action == "delete":
                bdb.delete_briefing_definitions(conn, operation.identity)
            else:
                apply_blocks(conn, operation.identity, desired[operation.identity])

    context.units.append(Unit("briefing_content", user, operations, transaction,
                              lambda conn: state_digest(_content_state(conn, scope)), write,
                              expected_state=expected))


def apply_blocks(conn, name, definitions):
    """Reconcile positions through module helpers, retaining ordinary-update IDs."""
    previous = bdb.list_blocks(conn, name)
    for index, definition in enumerate(definitions):
        fields = {field: definition[field] for field in BLOCK_FIELDS}
        if index < len(previous):
            block = previous[index]
            block_id, sources = block.id, block.sources
            previous_fields = {field: getattr(block, field) for field in BLOCK_FIELDS}
            previous_fields["directive"] = block.directive or ""
            changes = {field: value for field, value in fields.items() if value != previous_fields[field]}
            bdb.update_block(conn, block_id, **changes)
        else:
            block_id = bdb.add_block(conn, briefing_name=name, **fields)
            sources = []
        for slot, source in enumerate(definition["sources"]):
            if slot < len(sources):
                current = sources[slot]
                if current.kind != source["kind"]:
                    bdb.delete_source(conn, current.id)
                    bdb.add_source(conn, block_id=block_id, position=current.position, **source)
                else:
                    changes = {field: source[field] for field in ("config", "enabled")
                               if getattr(current, field) != source[field]}
                    bdb.update_source(conn, current.id, **changes)
            else:
                bdb.add_source(conn, block_id=block_id, **source)
        for source in sources[len(definition["sources"]):]:
            bdb.delete_source(conn, source.id)
    for block in previous[len(definitions):]:
        bdb.delete_block(conn, block.id)
