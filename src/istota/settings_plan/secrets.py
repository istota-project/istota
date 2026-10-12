"""Private inputs for settings apply; credential values never enter reports."""

from __future__ import annotations

import os
import stat
import sqlite3
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path

from istota import apply
from istota.config_editor import schema as config_schema
from istota.config_editor import secrets
from istota.settings_plan.schema import Plan, SchemaError, Sidecar, parse_sidecar


def read_sidecar(path: Path | str) -> Sidecar:
    """Read once from a private regular file, checking the opened descriptor."""
    if str(path) == "-":
        raise SchemaError(["secrets_file"], "--secrets-file requires a private file, not stdin")
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise SchemaError(["secrets_file"], "--secrets-file must be a private regular file (mode 0600)")
            raw = source.read()
    except OSError:
        raise SchemaError(["secrets_file"], "cannot read --secrets-file as a private regular file without following links") from None
    try:
        document = apply.parse_document(raw, is_json=path.suffix.lower() == ".json", where="in --secrets-file")
        return parse_sidecar(document)
    except SchemaError:
        raise
    except apply.PlanError as exc:
        raise SchemaError(["secrets_file"], str(exc)) from None


def select_sidecar(plan: Plan, sidecar: Sidecar, users: list[str] | None) -> Sidecar:
    """Require declared targets, and discard values outside the selected scope."""
    selected = {}
    for user, services in sidecar.users.items():
        declared = (plan.users or {}).get(user)
        if declared is None:
            raise SchemaError(["users", user, "secrets"], "sidecar user is not declared in the plan")
        if users and user not in users:
            continue
        slots = {entry["service"]: entry["keys"] for entry in declared.sections.get("secrets", [])}
        for service, values in services.items():
            if service not in slots:
                raise SchemaError(["users", user, "secrets", service], "sidecar service is not declared in the plan")
            for key in values:
                if key not in slots[service]:
                    raise SchemaError(["users", user, "secrets", service, key], "sidecar key is not declared in the plan")
        selected[user] = services
    config = [] if users else sidecar.config
    if config and plan.config is None:
        raise SchemaError(["config"], "sidecar config targets require a submitted config section")
    return Sidecar(selected, config)


def resolve_config(document: dict, config_path: Path, sidecar: Sidecar) -> dict:
    """Resolve through the editor's sentinel contract, retaining segment identity."""
    candidate = deepcopy(document)
    replacements = {}
    for entry in sidecar.config:
        path = entry["path"]
        node = candidate
        for segment in path[:-1]:
            if isinstance(node, dict) and isinstance(segment, str) and segment in node:
                node = node[segment]
            elif isinstance(node, list) and type(segment) is int and 0 <= segment < len(node):
                node = node[segment]
            else:
                raise SchemaError(["config", *path], "sidecar target is not a submitted credential leaf")
        last = path[-1]
        present = (isinstance(node, dict) and isinstance(last, str) and last in node) or (
            isinstance(node, list) and type(last) is int and 0 <= last < len(node))
        text_path = tuple(str(segment) for segment in path)
        if not present or isinstance(node[last], (dict, list)) or not secrets.is_secret_path(text_path):
            raise SchemaError(["config", *path], "sidecar target is not a submitted credential leaf")
        if text_path in config_schema.file_credentials() or config_schema.env_override_for(text_path):
            raise SchemaError(["config", *path], "credential is deployment-routed; use its existing credential writer")
        if entry["value"] == secrets.SENTINEL:
            raise SchemaError(["config", *path], "sidecar replacement must supply a credential value")
        replacements[secrets.label(text_path)] = entry["value"]
        node[last] = secrets.SENTINEL
    try:
        current = apply._parsed_or_empty(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except UnicodeError:
        raise SchemaError(["config"], "current config is not valid UTF-8") from None
    try:
        return secrets.restore_sentinels(candidate, current, replacements)
    except secrets.SentinelError as exc:
        path = ["config"]
        node = candidate
        for segment in exc.path:
            key = int(segment) if isinstance(node, list) else segment
            path.append(key)
            node = node[key]
        raise SchemaError(path, f"{exc}; supply an inline replacement through --secrets-file") from None


# Deliberate subset of secret ensure's registry: session cookies, OAuth material,
# vault entries and generated credentials are outside declarative settings.
PROVISIONABLE_SERVICES = frozenset({"karakeep", "ntfy", "native_brain", "feeds", "overland", "carto"})


def provisionable_keys():
    from istota.credentials.schema import known_service_keys

    registry = known_service_keys()
    return {service: registry[service] for service in sorted(PROVISIONABLE_SERVICES)}


def snapshot_secrets(conn, user, warnings):
    """Eligible stored slots only, without decrypting or disclosing other names."""
    from istota.credentials import store

    allowed = provisionable_keys()
    declarations = []
    excluded = False
    for service, entries in store.list_user_services(None, user, connection=conn).items():
        keys = sorted(entry["key"] for entry in entries if entry["key"] in allowed.get(service, ()))
        excluded |= len(keys) != len(entries)
        if keys:
            declarations.append({"service": service, "keys": keys})
    if excluded:
        warnings.append(f"users.{user}.secrets: excluded vault, generated, session or other non-provisionable credentials")
    return declarations


def _secret_state(conn, user, scopes):
    """Ciphertext revisions detect replacement without decrypting unrelated slots."""
    rows = conn.execute("SELECT service, key, encrypted_value FROM secrets WHERE user_id=? ORDER BY service, key",
                        (user,)).fetchall()
    return [[service, key, bytes(value).hex()] for service, key, value in rows if key in scopes.get(service, ())]


def plan_secrets(plan, user, context, options):
    from istota import db
    from istota.credentials import audit, store
    from istota.lib.sqlite_util import connect_read_only
    from istota.settings_plan.engine import ExecutionError, Operation, Unit, state_digest

    path = ["users", user, "secrets"]
    allowed = provisionable_keys()
    declared = {}
    for index, entry in enumerate(plan.users[user].sections["secrets"]):
        service = entry["service"]
        if service not in allowed:
            raise ExecutionError("secret_service_not_provisionable", [*path, index, "service"])
        for offset, key in enumerate(entry["keys"]):
            if key not in allowed[service]:
                raise ExecutionError("secret_key_not_provisionable", [*path, index, "keys", offset])
        declared[service] = set(entry["keys"])
    scopes = {service: allowed[service] if options.prune_secrets else keys for service, keys in declared.items()}
    supplied = context.sidecar.users.get(user, {})
    database = context.config.db_path
    operations = []
    try:
        with closing(connect_read_only(database)) as conn:
            conn.execute("BEGIN")
            before = _secret_state(conn, user, scopes)
            expected = state_digest(before)
            existing = {(service, key) for service, key, value in before}
            for service, keys in sorted(declared.items()):
                for key in sorted(keys):
                    value = supplied.get(service, {}).get(key)
                    present = (service, key) in existing
                    if value is None:
                        if not present:
                            context.warnings.append(f"users.{user}.secrets.{service}.{key}: missing; supply a value through --secrets-file")
                        continue
                    try:
                        current = store.get_secret(database, user, service, key, connection=conn, touch=False, strict=True)
                    except (store.SecretKeyMissingError, store.SecretKeyTooWeakError, store.SecretDecryptionError):
                        raise ExecutionError("secret_comparison_failed", [*path, service, key]) from None
                    if current != value:
                        operations.append(Operation("secrets", f"{service}.{key}", "update" if present else "create",
                                                    user=user, expected_state=expected))
            if options.prune_secrets:
                for service, key in sorted(existing):
                    if key not in declared[service]:
                        operations.append(Operation("secrets", f"{service}.{key}", "delete", user=user, expected_state=expected))
    except (sqlite3.Error, OSError):
        raise ExecutionError("secret_store_unavailable", path) from None
    if not operations:
        return

    @contextmanager
    def transaction():
        with db.get_db(database) as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def write(conn, operations):
        for operation in operations:
            service, key = operation.identity.split(".", 1)
            if operation.action == "delete":
                removed = store.delete_secret(database, user, service, key, connection=conn, actor="apply")
                if removed:
                    audit.record(conn, user, action="delete", actor="apply", name=operation.identity, detail={"count": 1})
            else:
                store.set_secret(database, user, service, key, supplied[service][key], connection=conn, actor="apply")

    context.units.append(Unit("secrets", user, operations, transaction,
                              lambda conn: state_digest(_secret_state(conn, user, scopes)), write, expected_state=expected))
