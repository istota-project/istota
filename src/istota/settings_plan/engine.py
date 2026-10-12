"""Plan every section before writing; commit and report one store unit at a time.

Adapters keep private desired values in their callbacks. Operations contain
only reportable changes and a digest of the expected definitions. A unit's
context manager owns its transaction or file lock; its digest is read inside
that boundary before its writer runs. Database adapters must use one caller-
owned connection for both reads and writes, with BEGIN IMMEDIATE and rollback
on failure. File adapters must hold the workspace writer's lock throughout.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from istota import apply
from istota.lib.file_lock import exclusive_lock
from istota.memory.curation.file_lock import deferred_lock_dir, lock_path_for
from istota.settings_plan.schema import Plan, select_users

USER_SECTIONS = ("profile", "resources", "briefings", "briefing_content", "scheduled_jobs", "heartbeat", "secrets")


class ExecutionError(Exception):
    """Only a code and a field path may cross the adapter error boundary."""

    def __init__(self, code: str, path=(), *, committed: bool = False):
        self.code = code
        self.path = list(path)
        self.committed = committed
        super().__init__(code)


@dataclass
class Options:
    dry_run: bool = False
    continue_on_error: bool = False
    users: list[str] | None = None
    prune: bool = False
    prune_secrets: bool = False


@dataclass
class Operation:
    section: str
    identity: str
    action: str
    user: str | None = None
    changes: list[dict] = field(default_factory=list)
    expected_state: str = field(default="", repr=False)
    restart_required: bool = False
    status: str = "planned"


@dataclass
class Unit:
    section: str
    user: str | None
    operations: list[Operation]
    transaction: Callable = field(repr=False)
    read_digest: Callable = field(repr=False)
    write: Callable = field(repr=False)
    phase: str = "upsert"
    # Empty units can update ownership metadata without a settings operation.
    expected_state: str = field(default="", repr=False)


@dataclass
class Context:
    config_path: Path
    options: Options = field(default_factory=Options)
    backup: int | None = None
    units: list[Unit] = field(default_factory=list, repr=False)
    planners: dict[str, Callable] = field(default_factory=dict, repr=False)
    warnings: list[str] = field(default_factory=list)
    config: object | None = field(default=None, repr=False)
    profiles: dict = field(default_factory=dict, repr=False)
    profile_claims: dict = field(default_factory=dict, repr=False)
    briefing_blocks: dict = field(default_factory=dict, repr=False)
    marker_pending: bool = False
    marker_updated: bool = False


@dataclass
class ApplyResult:
    operations: list[Operation] = field(default_factory=list)
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def changed(self):
        status = "planned" if self.dry_run else "applied"
        return any(op.status == status for op in self.operations)

    @property
    def partial(self):
        return bool(self.errors) and any(op.status == "applied" for op in self.operations)

    @property
    def restart_required(self):
        status = "planned" if self.dry_run else "applied"
        return any(op.status == status and op.restart_required for op in self.operations)

    @property
    def exit_code(self):
        return 1 if self.errors else 2 if self.changed else 0


def state_digest(value) -> str:
    """Hash only definitions, excluding runtime counters and access times."""
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def _file_digest(path):
    try:
        return state_digest(path.read_bytes())
    except FileNotFoundError:
        return state_digest(None)


@contextmanager
def apply_lock(config_path: Path, *, timeout_seconds: float = 5.0):
    """One anchor per resolved config path, independent of proposed settings."""
    from istota.config import Config

    anchor = lock_path_for(config_path.resolve(), lock_dir=deferred_lock_dir(Config().temp_dir))
    anchor.parent.mkdir(parents=True, exist_ok=True)
    try:
        with exclusive_lock(anchor, timeout_seconds=timeout_seconds, nofollow=True):
            yield
    except TimeoutError:
        raise ExecutionError("lock_timeout") from None


def _unit_order(unit):
    if unit.section == "config":
        return (0, "", 0, 0)
    if unit.section == "config_metadata":
        return (0, "", 1, 0)
    if unit.section == "shared_briefing_blocks":
        return (3 if unit.phase == "delete" else 1, "", 0, 0)
    return (2, unit.user or "", USER_SECTIONS.index(unit.section), 0)


def _unit_path(unit):
    if unit.user is None:
        return [unit.section]
    return ["users", unit.user, unit.section]


def _plan_config(plan, user, context, options):
    expected = _file_digest(context.config_path)
    section = apply.plan_config(plan.config, context.config_path, backup=context.backup)
    # Refuse a file edited during the read/compare itself too.
    if expected != _file_digest(context.config_path):
        raise ExecutionError("state_changed", ["config"])
    context.warnings.extend(section.warnings)
    operations = []
    if section.changed:
        operations.append(Operation("config", "config", "update", changes=[{"diff": section.diff}],
                                    expected_state=expected, restart_required=True))

    def write_config(conn, ops):
        if not ops:
            return
        try:
            section.write()
        except OSError:
            # Backup retention runs after the atomic replacement. A failure
            # there must not report an already changed config as unwritten.
            try:
                committed = apply._same_document(context.config_path.read_text(encoding="utf-8"), plan.config)
            except OSError:
                committed = False
            raise ExecutionError("execution_failed", ["config"], committed=committed) from None

    context.units.append(Unit("config", None, operations, nullcontext,
                              lambda conn: _file_digest(context.config_path),
                              write_config,
                              expected_state=expected))
    preserve_marker = plan.meta.source == "snapshot" and "managed" not in plan.metadata
    context.marker_pending = not preserve_marker and apply.managed_differs(
        context.config_path.parent, "config", plan.meta.managed, plan.meta.source)
    if context.marker_pending:
        marker_path = context.config_path.parent / apply.MANAGED_FILE

        def write_marker(conn, ops):
            apply.write_managed(context.config_path.parent, {"config": (plan.meta.managed, plan.meta.source)})
            context.marker_updated = True

        context.units.append(Unit("config_metadata", None, [], nullcontext,
                                  lambda conn: _file_digest(marker_path), write_marker,
                                  expected_state=_file_digest(marker_path)))


def _check_target_roots(plan, context):
    from istota.config import load_config
    from istota.lib import toml_write

    if not context.config_path.is_file():
        raise ExecutionError("current_config_required", ["config"])
    try:
        current = load_config(context.config_path, apply_db_overlays=False)
    except Exception:
        raise ExecutionError("current_config_invalid", ["config"]) from None
    with tempfile.TemporaryDirectory(prefix="istota-apply-") as scratch:
        path = Path(scratch) / "config.toml"
        path.write_text(toml_write.dumps(plan.config), encoding="utf-8")
        try:
            proposed = load_config(path, apply_db_overlays=False)
        except Exception:
            raise ExecutionError("validation_error", ["config"]) from None
    for name in ("db_path", "workspace_path", "nextcloud_mount_path", "temp_dir"):
        if getattr(current, name) != getattr(proposed, name):
            raise ExecutionError("target_roots_changed", ["config", name])
    if current.module_db_root() != proposed.module_db_root():
        raise ExecutionError("target_roots_changed", ["config", "module_data_dir"])
    if current.bot_dir_name != proposed.bot_dir_name:
        raise ExecutionError("target_roots_changed", ["config", "bot_name"])
    if current.developer.repos_dir != proposed.developer.repos_dir:
        raise ExecutionError("target_roots_changed", ["config", "developer", "repos_dir"])


def build_plan(plan: Plan, context: Context, options: Options) -> list[Operation]:
    """Adapters validate all resulting definitions before execution is allowed."""
    if options.prune_secrets and not options.prune:
        raise ExecutionError("prune_required", ["prune_secrets"])
    plan = select_users(plan, options.users)
    context.options = options
    context.units.clear()
    from istota.settings_plan.profiles import plan_profile
    from istota.settings_plan.resources import plan_resources
    from istota.settings_plan.briefings import plan_schedules
    from istota.settings_plan.shared_briefings import plan_shared_blocks, validate_references

    context.config = None
    context.profiles.clear()
    context.profile_claims.clear()
    context.briefing_blocks.clear()
    planners = {"config": _plan_config, "profile": plan_profile, "resources": plan_resources,
                "briefings": plan_schedules, "shared_briefing_blocks": plan_shared_blocks, **context.planners}
    sections = []
    if plan.config is not None:
        sections.append(("config", None))
    if plan.shared_briefing_blocks is not None:
        sections.append(("shared_briefing_blocks", None))
    for user, user_plan in sorted((plan.users or {}).items()):
        sections.append(("profile", user))
        for section in USER_SECTIONS[1:]:
            if section in user_plan.sections:
                sections.append((section, user))
    for section, user in sections:
        if section not in planners:
            path = [section] if user is None else ["users", user, section]
            raise ExecutionError("unsupported_section", path)
    mixed = plan.config is not None and any(section != "config" for section, user in sections)
    config_revision = _file_digest(context.config_path) if mixed else None
    if mixed:
        _check_target_roots(plan, context)
    # Every adapter sees all selected users after their profile updates.
    for section, user in sections:
        if section == "profile":
            planners[section](plan, user, context, options)
    for section, user in sections:
        if section != "profile":
            planners[section](plan, user, context, options)
    if context.config is not None and (plan.shared_briefing_blocks is not None or context.briefing_blocks):
        validate_references(plan, context, options)
    if mixed and config_revision != _file_digest(context.config_path):
        raise ExecutionError("state_changed", ["config"])
    context.units.sort(key=_unit_order)
    operations = []
    for unit in context.units:
        unit.operations.sort(key=lambda op: op.action == "delete")
        operations.extend(unit.operations)
    return operations


def execute_plan(operations: list[Operation], context: Context) -> ApplyResult:
    """Execute a completed preview; callers hold apply_lock across both phases."""
    units = sorted(context.units, key=_unit_order)
    ordered = [op for unit in units for op in sorted(unit.operations, key=lambda op: op.action == "delete")]
    if {id(op) for op in operations} != {id(op) for op in ordered}:
        raise ExecutionError("invalid_plan")
    result = ApplyResult(ordered, context.options.dry_run, list(context.warnings))
    if result.dry_run:
        return result
    stopped = False
    failed_users = set()
    for unit in units:
        if stopped or unit.user in failed_users or (unit.phase == "delete" and
                unit.section == "shared_briefing_blocks" and result.errors):
            for op in unit.operations:
                op.status = "skipped"
            continue
        try:
            with unit.transaction() as conn:
                current = unit.read_digest(conn)
                expected = [op.expected_state for op in unit.operations] or [unit.expected_state]
                if any(value != current for value in expected):
                    raise ExecutionError("state_changed", _unit_path(unit))
                unit.write(conn, sorted(unit.operations, key=lambda op: op.action == "delete"))
        except Exception as exc:
            if isinstance(exc, ExecutionError):
                error = {"code": exc.code, "path": exc.path or _unit_path(unit)}
            else:
                code = "execution_failed"
                if isinstance(exc, sqlite3.OperationalError) and getattr(exc, "sqlite_errorcode", None) in (
                        sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    code = "db_busy"
                error = {"code": code, "path": _unit_path(unit)}
            # Preserve the shipped config-only contract: a failed marker does
            # not hide an already written config or suppress its restart.
            config_only = all(item.section in ("config", "config_metadata") for item in units)
            if unit.section == "config_metadata" and config_only and result.changed and error["code"] != "state_changed":
                result.warnings.append("writing managed.json failed; config changes were applied")
                continue
            result.errors.append(error)
            for op in unit.operations:
                op.status = "applied" if isinstance(exc, ExecutionError) and exc.committed else "failed"
            if unit.user is not None:
                failed_users.add(unit.user)
            if unit.user is None or error["code"] == "state_changed" or not context.options.continue_on_error:
                stopped = True
        else:
            for op in unit.operations:
                op.status = "applied"
    return result
