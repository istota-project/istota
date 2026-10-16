"""Group definition plans over the common save and source-approval checks."""

from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import replace
import sqlite3

from istota import db
from istota.briefings import group_store as store
from istota.briefings.models import validate_group_definition_payload
from istota.config import is_admin_with_stored_users, load_admin_users, stored_user_ids
from istota.config_editor.secrets import redact_document
from istota.groups.policy import GroupError
from istota.lib.sqlite_util import connect_read_only
from istota.settings_plan.engine import ExecutionError, Operation, Unit, state_digest
from istota.settings_plan.profiles import _load_config
from istota.settings_plan.schema import GROUP_BRIEFING_FIELDS


def snapshot_group_briefings(conn):
    """Only restorable definitions; grants, attribution and editions stay local."""
    result = []
    try:
        for row in conn.execute("SELECT id FROM group_briefing_configs WHERE deleted_at IS NULL ORDER BY group_id, name"):
            definition = store.get_definition(conn, row["id"])
            payload = {key: definition[key] for key in GROUP_BRIEFING_FIELDS if key != "group_id"}
            result.append({"group_id": definition["group_id"], **validate_group_definition_payload(payload)})
    except (ValueError, TypeError, KeyError):
        raise ExecutionError("group_briefing_store_invalid", ["group_briefings"]) from None
    return result


def operator_is_admin(conn, config, actor):
    """Resolve current operator-selected authority with the stored-user fallback."""
    return is_admin_with_stored_users(replace(config, admin_users=load_admin_users()), actor, stored_user_ids(conn))


def prepare_definition(conn, config, declaration, *, actor, expected_revision=None, allow_unchanged=False):
    """Merge supplied fields only, then run the store's read-only save checks."""
    group_id, name = declaration["group_id"], declaration["name"]
    row = conn.execute("SELECT id FROM group_briefing_configs WHERE group_id=? AND name=?", (group_id, name)).fetchone()
    old = store.get_definition(conn, row["id"]) if row else None
    payload = {key: deepcopy(old[key]) for key in GROUP_BRIEFING_FIELDS if key != "group_id"} if old else {}
    payload.update({key: deepcopy(value) for key, value in declaration.items() if key != "group_id"})
    try:
        payload = validate_group_definition_payload(payload)
    except (ValueError, TypeError, KeyError, OSError):
        raise GroupError(422, "invalid_definition", "Invalid briefing definition") from None
    if allow_unchanged and old and all(payload[key] == old[key] for key in payload):
        return payload, old
    if not actor:
        raise ExecutionError("group_briefing_actor_required", ["group_briefings", "--actor"])
    is_admin = operator_is_admin(conn, config, actor)
    store._require_author(conn, config, group_id, actor, is_admin)
    if old is None and payload.get("enabled", False):
        raise GroupError(422, "draft_required", "Create a disabled draft before enabling")
    revision = expected_revision if expected_revision is not None else old["revision"] if old else 0
    value, _ = store.validate_definition_save(
        conn, config, group_id, payload, actor_id=actor, is_admin=is_admin,
        definition_id=old["id"] if old else None, expected_revision=revision)
    return value, old


def plan_group_briefings(plan, user, context, options):
    if not plan.group_briefings:
        return
    if context.config is None:
        context.config = _load_config(plan, context)
    config = context.config
    desired, existing = {}, {}
    try:
        with closing(connect_read_only(config.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            for index, declaration in enumerate(plan.group_briefings):
                try:
                    value, old = prepare_definition(conn, config, declaration, actor=options.actor, allow_unchanged=True)
                except GroupError as error:
                    raise ExecutionError(error.code, ["group_briefings", index]) from None
                identity = declaration["group_id"] + "/" + declaration["name"]
                desired[identity] = {"group_id": declaration["group_id"], **value}
                existing[identity] = old
    except (OSError, sqlite3.Error):
        raise ExecutionError("group_briefing_store_unavailable", ["group_briefings"]) from None
    if options.prune:
        context.warnings.append("group_briefings: definitions and history are retained; disable unwanted definitions explicitly")

    @contextmanager
    def transaction():
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def digest(conn):
        current = {}
        for identity, value in desired.items():
            row = conn.execute("SELECT id FROM group_briefing_configs WHERE group_id=? AND name=?",
                               (value["group_id"], value["name"])).fetchone()
            current[identity] = store.get_definition(conn, row["id"]) if row else None
        return state_digest(current)

    expected = state_digest(existing)
    operations = []
    for identity, after in desired.items():
        before = existing[identity]
        changes = [{"path": [key], "before": redact_document(before[key]) if before else None,
                    "after": redact_document(after[key])}
                   for key in GROUP_BRIEFING_FIELDS if before is None or before[key] != after[key]]
        if changes:
            operations.append(Operation("group_briefings", identity, "create" if before is None else "update",
                                        changes=changes, expected_state=expected))

    def write(conn, operations):
        try:
            for operation in operations:
                declaration = desired[operation.identity]
                old = existing[operation.identity]
                value = {key: item for key, item in declaration.items() if key != "group_id"}
                store.save_definition(conn, config, declaration["group_id"], value, actor_id=options.actor,
                                      expected_revision=old["revision"] if old else 0,
                                      definition_id=old["id"] if old else None,
                                      is_admin=operator_is_admin(conn, config, options.actor), caller_transaction=True)
        except GroupError as error:
            raise ExecutionError(error.code, ["group_briefings"]) from None

    if operations:
        context.units.append(Unit("group_briefings", None, operations, transaction, digest, write))
