"""Folder definitions over the existing resource store and transaction helpers."""

from contextlib import closing, contextmanager
from copy import deepcopy
import sqlite3

from istota import db
from istota.config import ResourceConfig
from istota.config_editor.secrets import redact_document
from istota.lib.sqlite_util import connect_read_only
from istota.settings_plan.engine import ExecutionError, Operation, Unit, state_digest
from istota.settings_plan.schema import SchemaError, validate_user_plan


def _refuse_credentials(extra, path):
    marker = object()
    if redact_document(extra, placeholder=marker) != extra:
        raise ExecutionError("credential_resource_extra", path)


def snapshot_resources(conn, user):
    """Read complete, sorted folder definitions; refuse invalid or private extras."""
    path = ["users", user, "resources"]
    try:
        rows = db.get_user_resources(conn, user, "folder", strict_extras=True)
        definitions = [{"type": row.resource_type, "path": row.resource_path,
                        "name": row.display_name or "", "permissions": row.permissions,
                        "extra": row.extras} for row in rows]
        definitions.sort(key=lambda item: item["path"])
        validate_user_plan(user, {"resources": definitions})
    except (ValueError, SchemaError):
        raise ExecutionError("resource_store_invalid", path) from None
    for index, definition in enumerate(definitions):
        _refuse_credentials(definition["extra"], [*path, index, "extra"])
    return definitions


def plan_resources(plan, user, context, options):
    config = context.config
    path = ["users", user, "resources"]
    try:
        with closing(connect_read_only(config.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            before = snapshot_resources(conn, user)
    except (sqlite3.Error, OSError):
        raise ExecutionError("resource_store_unavailable", path) from None
    expected = state_digest(before)
    existing = {item["path"]: item for item in before}
    desired = {}
    operations = []
    for index, declaration in enumerate(plan.users[user].sections["resources"]):
        identity = declaration["path"]
        previous = existing.get(identity)
        defaults = ResourceConfig(type="folder", path=identity)
        merged = deepcopy(previous) if previous else {
            "type": defaults.type, "path": defaults.path, "name": defaults.name,
            "permissions": defaults.permissions, "extra": defaults.extra,
        }
        merged.update(deepcopy(declaration))
        _refuse_credentials(merged["extra"], [*path, index, "extra"])
        desired[identity] = merged
        if previous == merged:
            continue
        changes = [{"path": [name], "before": previous[name] if previous else None, "after": merged[name]}
                   for name in ("name", "permissions", "extra") if previous is None or previous[name] != merged[name]]
        operations.append(Operation("resources", identity, "create" if previous is None else "update", user=user,
                                    changes=changes, expected_state=expected, restart_required=True))
    if options.prune:
        for identity in sorted(existing.keys() - desired.keys()):
            operations.append(Operation("resources", identity, "delete", user=user,
                                        expected_state=expected, restart_required=True))
    if not operations:
        return

    @contextmanager
    def transaction():
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def write(conn, operations):
        # Resolve row IDs under the transaction; portable identities contain no IDs.
        current = {row.resource_path: row for row in db.get_user_resources(conn, user, "folder")}
        for operation in operations:
            if operation.action == "delete":
                db.delete_user_resource(conn, user, current[operation.identity].id)
            else:
                definition = desired[operation.identity]
                db.upsert_user_resource(conn, user, "folder", definition["path"],
                                        display_name=definition["name"], permissions=definition["permissions"],
                                        extras=definition["extra"])

    context.units.append(Unit("resources", user, operations, transaction,
                              lambda conn: state_digest(snapshot_resources(conn, user)), write,
                              expected_state=expected))
