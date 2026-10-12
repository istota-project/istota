"""File definitions use the scheduler parser and the shared workspace lock."""

from contextlib import closing, contextmanager
from dataclasses import asdict
import os
import sqlite3
import tomllib

from croniter import croniter

from istota import cron_loader, db, storage
from istota.brain import KNOWN_BRAIN_KINDS
from istota.lib import toml_write
from istota.lib.sqlite_util import connect_read_only
from istota.settings_plan.engine import ExecutionError, Operation, Unit
from istota.settings_plan.profiles import validate_destination
from istota.settings_plan.schema import SchemaError, validate_user_plan
from istota.transport import parse_output_target
from istota.workspace import text_files
from istota.workspace.writes import open_or_create_dirs


def _file_path(config, user, filename, field_path):
    directory = storage.resolve_user_config_dir(config, user)
    if directory is None:
        raise ExecutionError("file_unavailable", field_path)
    return directory / filename


def _read_file(path, field_path):
    content, refusal = storage.read_regular_file(path)
    if refusal is not None:
        raise ExecutionError("file_unavailable", field_path)
    return content


def _cron_definition(job):
    definition = asdict(job)
    # The loader resolves this to text for dispatch; the definition owns only
    # the reference, never a copy of the referenced file.
    if job.prompt_file:
        definition["prompt"] = ""
    return definition


def _cron_document(content, config, user):
    path = ["users", user, "scheduled_jobs"]
    doc = cron_loader.parse_cron_document(content, config, user)
    if doc is None or doc.errors or doc.skipped_entries or doc.refused_names:
        raise ExecutionError("cron_file_invalid", path)
    data = tomllib.loads(doc.block) if doc.block is not None else {}
    definitions = [_cron_definition(job) for job in doc.jobs]
    try:
        # The parser intentionally tolerates unknown fields and duplicate
        # names. A rewrite cannot retain either, so the plan refuses them.
        validate_user_plan(user, {"scheduled_jobs": data.get("jobs", [])})
        validate_user_plan(user, {"scheduled_jobs": definitions})
    except SchemaError:
        raise ExecutionError("cron_file_invalid", path) from None
    return doc, data, definitions


def snapshot_cron(conn, config, user):
    """Complete file definitions and revision, refusing pending DB migration."""
    field_path = ["users", user, "scheduled_jobs"]
    path = _file_path(config, user, "CRON.md", field_path)
    content = _read_file(path, field_path)
    doc, _, definitions = _cron_document(content, config, user)
    if doc.is_template and any(not job.name.startswith(cron_loader._MODULE_JOB_PREFIX)
                               for job in db.get_user_scheduled_jobs(conn, user)):
        raise ExecutionError("cron_file_migration_required", field_path)
    return sorted(definitions, key=lambda item: item["name"]), text_files.revision(content)


def _validate_cron(conn, config, user, job, path):
    if not croniter.is_valid(job.cron):
        raise ExecutionError("validation_error", [*path, "cron"])
    if cron_loader.fj_is_disallowed_command(job, config.is_admin(user)):
        raise ExecutionError("validation_error", [*path, "command"])
    if job.brain and (cron_loader.fj_brain_or_none(job, config.is_admin(user)) is None
                      or job.brain not in KNOWN_BRAIN_KINDS or job.brain not in config.brain.room_selectable):
        raise ExecutionError("validation_error", [*path, "brain"])
    if job.publish_shared_kv or job.publish_shared_kv_trusted:
        from istota.scheduler import _parse_shared_kv_target

        namespace, key = _parse_shared_kv_target(job.publish_shared_kv)
        if not config.is_shared_kv_writer(user) or not namespace or not key:
            raise ExecutionError("validation_error", [*path, "publish_shared_kv"])
    try:
        # Check each leaf so a dropped group destination cannot hide beside
        # a valid email destination. Explicit none is a valid no-delivery job.
        for leaf in job.target.split(","):
            if leaf.strip() and leaf.strip().lower() != "none":
                validate_destination(conn, config, user, leaf)
    except ValueError:
        raise ExecutionError("validation_error", [*path, "target"]) from None
    if job.room:
        # A cron room may be a legacy Talk binding or a canonical room token.
        token = db.resolve_room_token(conn, "talk", job.room) or job.room
        try:
            validate_destination(conn, config, user, "room:" + token)
        except ValueError:
            raise ExecutionError("validation_error", [*path, "room"]) from None
    for destination in parse_output_target(job.target):
        if destination.surface == "room" and not destination.channel and not job.room:
            raise ExecutionError("validation_error", [*path, "room"])


def apply_file(context, user, section, filename, content, expected, operations, *, validate=None):
    """Register one revision-checked atomic write, holding the workspace lock."""
    config = context.config
    field_path = ["users", user, section]
    path = _file_path(config, user, filename, field_path)
    if len(content.encode("utf-8")) > storage.USER_CONFIG_READ_CAP_BYTES:
        raise ExecutionError("file_too_large", field_path)

    @contextmanager
    def transaction():
        try:
            with text_files.file_lock(config, user, path):
                if _file_path(config, user, filename, field_path) != path:
                    raise ExecutionError("state_changed", field_path)
                yield None
        except text_files.MemoryMdLocked:
            raise ExecutionError("lock_timeout", field_path) from None

    def write(conn, ops):
        if validate is not None:
            validate()
        # Same directory creation and atomic writer as the workspace editor.
        os.close(open_or_create_dirs(config.workspace_root(user), [config.bot_dir_name, "config"]))
        if not storage.write_regular_file(path, content):
            raise ExecutionError("file_write_failed", field_path)

    context.units.append(Unit(section, user, operations, transaction,
                              lambda conn: text_files.revision(_read_file(path, field_path)),
                              write, expected_state=expected))


def plan_cron(plan, user, context, options):
    config = context.config
    field_path = ["users", user, "scheduled_jobs"]
    path = _file_path(config, user, "CRON.md", field_path)
    content = _read_file(path, field_path)
    expected = text_files.revision(content)
    doc, data, before = _cron_document(content, config, user)
    existing = {item["name"]: item for item in before}
    desired = {} if options.prune else dict(existing)
    declarations = plan.users[user].sections["scheduled_jobs"]
    for declaration in declarations:
        identity = declaration["name"]
        merged = dict(existing.get(identity, asdict(cron_loader.CronJob(identity, ""))))
        merged.update(declaration)
        desired[identity] = merged
    # Parse TOML generated from the complete mappings, not render_jobs_block:
    # the renderer selects one dispatch field, which could hide an invalid
    # prompt + command combination before the parser sees it.
    candidate = "```toml\n" + toml_write.dumps({"jobs": list(desired.values())}) + "```\n"
    parsed, _, after = _cron_document(candidate, config, user)
    desired = {item["name"]: item for item in after}
    try:
        with closing(connect_read_only(config.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            if doc.is_template and any(not job.name.startswith(cron_loader._MODULE_JOB_PREFIX)
                                       for job in db.get_user_scheduled_jobs(conn, user)):
                raise ExecutionError("cron_file_migration_required", field_path)
            for index, job in enumerate(parsed.jobs):
                _validate_cron(conn, config, user, job, [*field_path, index])
    except (sqlite3.Error, OSError):
        raise ExecutionError("cron_store_unavailable", field_path) from None
    operations = []
    for identity, definition in desired.items():
        previous = existing.get(identity)
        if previous == definition:
            continue
        changes = [{"path": [name], "before": previous[name] if previous else None, "after": value}
                   for name, value in definition.items() if name != "name" and (previous is None or previous[name] != value)]
        operations.append(Operation("scheduled_jobs", identity, "create" if previous is None else "update",
                                    user=user, changes=changes, expected_state=expected))
    for identity in sorted(existing.keys() - desired.keys()):
        operations.append(Operation("scheduled_jobs", identity, "delete", user=user, expected_state=expected))
    if not operations:
        return
    other = {name: value for name, value in data.items() if name != "jobs"}
    block = (toml_write.dumps(other) + cron_loader.render_jobs_block(parsed.jobs) if parsed.jobs
             else toml_write.dumps({"jobs": [], **other}))
    if doc.block_span is not None:
        start, end = doc.block_span
        rendered = content[:start] + block + content[end:]
    else:
        rendered = content + ("\n" if content and not content.endswith("\n") else "") + "```toml\n" + block + "```\n"
    def validate():
        try:
            current, _, _ = _cron_document(rendered, config, user)
            with closing(connect_read_only(config.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN")
                for index, job in enumerate(current.jobs):
                    _validate_cron(conn, config, user, job, [*field_path, index])
        except ExecutionError:
            raise ExecutionError("state_changed", field_path) from None

    apply_file(context, user, "scheduled_jobs", "CRON.md", rendered, expected, operations, validate=validate)
