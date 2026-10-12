"""Section transactions and reports through a small real SQLite adapter."""

import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager

import pytest

from istota.settings_plan import engine
from istota.settings_plan.report import result_document
from istota.settings_plan.schema import parse_plan


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "settings.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT)")
    return path


def prepare(context, store, section, user, name, *, fail=False, action="update", restart=True):
    @contextmanager
    def transaction():
        with sqlite3.connect(store) as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def current(conn):
        return engine.state_digest(conn.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone())

    with sqlite3.connect(store) as conn:
        expected = current(conn)
    operation = engine.Operation(section, name, action, user=user, expected_state=expected,
                                 changes=[{"path": ["value"], "after": "updated"}], restart_required=restart)

    def write(conn, operations):
        conn.execute("INSERT OR REPLACE INTO settings VALUES (?, 'updated')", (name,))
        if fail:
            raise RuntimeError("private value must never reach the report")

    unit = engine.Unit(section, user, [operation], transaction, current, write,
                       phase="delete" if section == "shared_briefing_blocks" and action == "delete" else "upsert")
    context.units.append(unit)
    return operation


def values(store):
    with sqlite3.connect(store) as conn:
        return dict(conn.execute("SELECT name, value FROM settings"))


def test_partial_failure_rolls_back_unit_and_continues_only_later_users(store, tmp_path):
    context = engine.Context(tmp_path / "config.toml", options=engine.Options(continue_on_error=True))
    # Deliberately prepare out of order: execution owns ordering.
    delete = prepare(context, store, "shared_briefing_blocks", None, "old", action="delete")
    bob = prepare(context, store, "profile", "bob", "bob")
    skipped = prepare(context, store, "scheduled_jobs", "alice", "jobs")
    failed = prepare(context, store, "resources", "alice", "folder", fail=True)
    first = prepare(context, store, "profile", "alice", "alice")
    result = engine.execute_plan([delete, bob, skipped, failed, first], context)
    assert values(store) == {"alice": "updated", "bob": "updated"}
    assert result.exit_code == 1
    assert result.changed and result.partial and result.restart_required
    report = result_document(result)
    assert [op["status"] for op in report["operations"]] == ["applied", "failed", "skipped", "applied", "skipped"]
    assert report["counts"] == {"planned": 5, "applied": 2, "failed": 1, "skipped": 2}
    assert "private value" not in json.dumps(report)
    assert "expected_state" not in json.dumps(report)


def test_stale_state_stops_even_continue_on_error(store, tmp_path):
    context = engine.Context(tmp_path / "config.toml", options=engine.Options(continue_on_error=True))
    alice = prepare(context, store, "profile", "alice", "alice")
    bob = prepare(context, store, "profile", "bob", "bob")
    with sqlite3.connect(store) as conn:
        conn.execute("INSERT INTO settings VALUES ('alice', 'web edit')")
    result = engine.execute_plan([alice, bob], context)
    assert values(store) == {"alice": "web edit"}
    assert result_document(result)["errors"][0]["code"] == "state_changed"
    assert not result.changed and not result.partial
    assert bob.status == "skipped"


def test_late_preflight_refusal_cannot_write_config(tmp_path):
    context = engine.Context(tmp_path / "config.toml")
    context.config_path.write_text("")

    def refuse(plan, user, context, options):
        raise engine.ExecutionError("validation_error", ["users", user, "briefings"])

    context.planners = {"profile": lambda *args: None, "briefings": refuse}
    plan = parse_plan({"config": {"scheduler": {"poll_interval": 5}}, "users": {"alice": {"briefings": []}}})
    with pytest.raises(engine.ExecutionError) as exc:
        engine.build_plan(plan, context, context.options)
    assert exc.value.code == "validation_error"
    assert context.config_path.read_text() == ""


@pytest.mark.parametrize("continue_on_error", [False, True])
def test_global_failure_stops_every_user(store, tmp_path, continue_on_error):
    context = engine.Context(tmp_path / "config.toml", options=engine.Options(continue_on_error=continue_on_error))
    first = prepare(context, store, "shared_briefing_blocks", None, "shared", fail=True)
    later = prepare(context, store, "profile", "bob", "bob")
    result = engine.execute_plan([first, later], context)
    assert values(store) == {}
    assert result.exit_code == 1 and not result.partial
    assert later.status == "skipped"


def test_default_stops_after_failed_user(store, tmp_path):
    context = engine.Context(tmp_path / "config.toml")
    failed = prepare(context, store, "profile", "alice", "alice", fail=True)
    skipped = prepare(context, store, "profile", "bob", "bob")
    engine.execute_plan([failed, skipped], context)
    assert values(store) == {} and skipped.status == "skipped"


def test_shared_delete_runs_after_all_users_and_content_needs_no_restart(store, tmp_path):
    context = engine.Context(tmp_path / "config.toml")
    delete = prepare(context, store, "shared_briefing_blocks", None, "delete", action="delete", restart=False)
    bob = prepare(context, store, "briefing_content", "bob", "bob", restart=False)
    alice = prepare(context, store, "briefing_content", "alice", "alice", restart=False)
    upsert = prepare(context, store, "shared_briefing_blocks", None, "upsert", restart=False)
    result = engine.execute_plan([delete, bob, alice, upsert], context)
    assert list(values(store)) == ["upsert", "alice", "bob", "delete"]
    assert result.exit_code == 2 and not result.restart_required


def test_upserts_before_deletes_share_one_rollback(store, tmp_path):
    context = engine.Context(tmp_path / "config.toml")
    operation = prepare(context, store, "resources", "alice", "folder")
    unit = context.units[0]
    deletion = engine.Operation("resources", "old", "delete", user="alice", expected_state=operation.expected_state)
    unit.operations.insert(0, deletion)
    original_write = unit.write

    def write(conn, operations):
        assert conn.in_transaction
        assert [op.action for op in operations] == ["update", "delete"]
        original_write(conn, operations)
        raise OSError("private failure detail")

    unit.write = write
    result = engine.execute_plan([deletion, operation], context)
    assert values(store) == {}
    assert [op.status for op in result.operations] == ["failed", "failed"]


def test_read_only_preview_never_opens_a_write_transaction(store, tmp_path):
    context = engine.Context(tmp_path / "config.toml", options=engine.Options(dry_run=True))
    operation = prepare(context, store, "profile", "alice", "alice")

    def forbidden():
        pytest.fail("dry-run entered a write transaction")

    context.units[0].transaction = forbidden
    result = engine.execute_plan([operation], context)
    assert result.exit_code == 2 and result.restart_required and values(store) == {}
    assert operation.status == "planned"


def test_apply_lock_is_shared_across_processes_and_resolved_paths(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("")
    alias = tmp_path / "alias.toml"
    alias.symlink_to(config)
    script = """
import sys
from pathlib import Path
from istota.settings_plan.engine import apply_lock, ExecutionError
try:
    with apply_lock(Path(sys.argv[1]), timeout_seconds=0.01):
        raise AssertionError('second writer acquired the lock')
except ExecutionError as exc:
    assert exc.code == 'lock_timeout'
"""
    with engine.apply_lock(config):
        completed = subprocess.run([sys.executable, "-c", script, str(alias)], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    with engine.apply_lock(alias, timeout_seconds=0.01):
        pass


def test_mixed_plan_cannot_retarget_stores_before_any_adapter_runs(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('db_path = "/data/old.db"\n')
    context = engine.Context(config)
    context.planners["resources"] = lambda *args: pytest.fail("adapter ran before roots were checked")
    plan = parse_plan({"config": {"db_path": "/data/new.db"}, "users": {"alice": {"resources": []}}})
    with pytest.raises(engine.ExecutionError) as exc:
        engine.build_plan(plan, context, context.options)
    assert exc.value.code == "target_roots_changed"
    assert config.read_text() == 'db_path = "/data/old.db"\n'


def test_scoped_plan_never_reads_instance_sections_or_other_users(store, tmp_path):
    context = engine.Context(tmp_path / "absent.toml", options=engine.Options(users=["bob"]))
    planned = []

    def plan_resources(plan, user, context, options):
        planned.append(user)
        prepare(context, store, "resources", user, user)

    context.planners["profile"] = lambda *args: None
    context.planners["resources"] = plan_resources
    plan = parse_plan({"config": {}, "shared_briefing_blocks": [], "users": {
        "alice": {"profile": {}}, "bob": {"resources": []}}})
    operations = engine.build_plan(plan, context, context.options)
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 2 and planned == ["bob"]
    assert not context.config_path.exists()


@pytest.mark.parametrize("change_mount", [False, True])
def test_mixed_plan_checks_current_workspace_mount_field(tmp_path, change_mount):
    from istota import db, user_profiles
    from istota.lib import toml_write

    database = tmp_path / "framework.db"
    db.init_db(database)
    user_profiles.provision_user(database, "alice", {})
    document = {"db_path": str(database), "workspace_path": str(tmp_path / "workspace"),
                "workspace_mount_path": str(tmp_path / "mount")}
    config = tmp_path / "config.toml"
    config.write_text(toml_write.dumps(document))
    before = database.read_bytes()
    proposed = dict(document)
    if change_mount:
        proposed["workspace_mount_path"] = str(tmp_path / "other")
    plan = parse_plan({"config": proposed, "users": {"alice": {}}})
    context = engine.Context(config)
    if change_mount:
        with pytest.raises(engine.ExecutionError) as caught:
            engine.build_plan(plan, context, engine.Options(dry_run=True))
        assert caught.value.code == "target_roots_changed"
        assert caught.value.path == ["config", "workspace_mount_path"]
    else:
        operations = engine.build_plan(plan, context, engine.Options(dry_run=True))
        assert engine.execute_plan(operations, context).exit_code == 0
    assert database.read_bytes() == before
