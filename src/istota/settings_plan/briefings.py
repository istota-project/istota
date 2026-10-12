"""Framework schedules and separate, definition-only module pruning."""

from contextlib import closing, contextmanager
from copy import deepcopy
import sqlite3

from croniter import croniter

from istota import db, user_briefings
from istota.briefings import db as bdb
from istota.config import BriefingConfig
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
    # The content adapter will own supplied arrays; until it is installed, refuse them.
    for index, declaration in enumerate(declarations):
        if "blocks" in declaration:
            raise ExecutionError("unsupported_section", [*path, index, "blocks"])
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
                merged.update(deepcopy(declaration))
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
        # Include orphan definitions so retrying a partially committed prune converges.
        _plan_definition_deletes(config, user, set(desired), context)
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


def _definition_delete_state(conn, retained):
    # Read raw configuration columns so malformed JSON cannot disappear through a decoder.
    definitions = {}
    for name in bdb.list_briefing_names(conn):
        if name in retained:
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


def _plan_definition_deletes(config, user, retained, context):
    database = config.module_db_path(user, "briefings")
    path = ["users", user, "briefing_content"]
    try:
        database.stat()
    except FileNotFoundError:
        return
    except OSError:
        raise ExecutionError("briefing_store_unavailable", path) from None
    try:
        with closing(connect_read_only(database)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            before = _definition_delete_state(conn, retained)
    except (sqlite3.Error, OSError):
        raise ExecutionError("briefing_store_unavailable", path) from None
    if not before:
        return
    expected = state_digest(before)
    operations = [Operation("briefing_content", name, "delete", user=user, expected_state=expected)
                  for name in sorted(before)]

    @contextmanager
    def transaction():
        with bdb.connect(database) as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()

    def write(conn, operations):
        for operation in operations:
            bdb.delete_briefing_definitions(conn, operation.identity)

    context.units.append(Unit("briefing_content", user, operations, transaction,
                              lambda conn: state_digest(_definition_delete_state(conn, retained)), write,
                              expected_state=expected))
