"""Folder plans use the framework resource store without touching organizer rows."""

import json

import pytest

from istota import cli, db, user_profiles
from istota.lib import toml_write
from istota.settings_plan import engine
from istota.settings_plan.schema import parse_plan


@pytest.fixture
def install(tmp_path):
    database = tmp_path / "framework.db"
    db.init_db(database)
    config = tmp_path / "config.toml"
    config.write_text(toml_write.dumps({"db_path": str(database)}))
    user_profiles.provision_user(database, "alice", {})
    return config, database


def preview(install, resources, **options):
    context = engine.Context(install[0])
    plan = parse_plan({"users": {"alice": {"resources": resources}}})
    operations = engine.build_plan(plan, context, engine.Options(**options))
    return context, operations


def rows(install, user="alice"):
    with db.get_db(install[1]) as conn:
        return db.get_user_resources(conn, user)


def test_cli_dry_run_apply_and_idempotence(install, tmp_path, monkeypatch, capsys):
    config, database = install
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"users": {"alice": {"resources": [
        {"type": "folder", "path": "/Documents", "name": "Papers", "extra": {"label": "work"}},
    ]}}}))
    before = database.read_bytes()
    for flags, expected in [(["--dry-run"], 2), ([], 2), ([], 0)]:
        monkeypatch.setattr("sys.argv", ["istota", "-c", str(config), "apply", "-f", str(plan), "--format", "json", *flags])
        code = 0
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
        assert code == expected
        report = json.loads(capsys.readouterr().out)
        assert report["restart_required"] is (expected == 2)
        if flags:
            assert database.read_bytes() == before
            assert rows(install) == []
    resource, = rows(install)
    assert (resource.display_name, resource.permissions, resource.extras) == ("Papers", "read", {"label": "work"})


def test_partial_update_and_explicit_clear_preserve_row_identity(install):
    with db.get_db(install[1]) as conn:
        original_id = db.add_user_resource(conn, "alice", "folder", "/Documents", "Papers", "readwrite", {"label": "work"})
    for declaration, expected in [({"name": "New"}, {"label": "work"}), ({"name": "", "extra": {}}, {})]:
        context, operations = preview(install, [{"type": "folder", "path": "/Documents", **declaration}])
        assert engine.execute_plan(operations, context).exit_code == 2
        resource, = rows(install)
        assert resource.id == original_id and resource.permissions == "readwrite"
        assert resource.extras == expected and resource.display_name == declaration["name"]


def test_prune_scopes_only_declared_user_folders(install):
    with db.get_db(install[1]) as conn:
        db.add_user_resource(conn, "alice", "folder", "/Documents")
        organizer_id = db.add_user_resource(conn, "alice", "shared_file", "/shared/report.pdf", extras={"share_id": 42})
        db.add_user_resource(conn, "bob", "folder", "/Other")
    context, operations = preview(install, [])
    assert engine.execute_plan(operations, context).exit_code == 0
    context = engine.Context(install[0])
    operations = engine.build_plan(parse_plan({"users": {"alice": {}}}), context, engine.Options(prune=True))
    assert engine.execute_plan(operations, context).exit_code == 0
    context, operations = preview(install, [], prune=True)
    assert [(op.action, op.identity) for op in operations] == [("delete", "/Documents")]
    assert engine.execute_plan(operations, context).exit_code == 2
    assert [r.id for r in rows(install)] == [organizer_id]
    assert len(rows(install, "bob")) == 1


@pytest.mark.parametrize("stored", [False, True])
def test_credential_extras_refused_before_any_write(install, stored):
    extra = {"nested": [{"authorization": "private-placeholder"}]}
    if stored:
        with db.get_db(install[1]) as conn:
            db.add_user_resource(conn, "alice", "folder", "/Documents", extras=extra)
    before = install[1].read_bytes()
    declaration = {"type": "folder", "path": "/Documents"}
    if not stored:
        declaration["extra"] = extra
    with pytest.raises(engine.ExecutionError, match="credential_resource_extra") as exc:
        preview(install, [declaration], prune=True)
    assert "private-placeholder" not in str(exc.value)
    assert install[1].read_bytes() == before


def test_snapshot_folders_emit_defaults_and_refuse_secret_extras(install):
    from istota.settings_plan.resources import snapshot_resources

    with db.get_db(install[1]) as conn:
        db.add_user_resource(conn, "alice", "folder", "/Documents")
        db.add_user_resource(conn, "alice", "shared_file", "/shared/report.pdf", extras={"token": "private-placeholder"})
        assert snapshot_resources(conn, "alice") == [
            {"type": "folder", "path": "/Documents", "name": "", "permissions": "read", "extra": {}}]
        db.add_user_resource(conn, "alice", "folder", "/Documents", extras={"api_key": "private-placeholder"})
        with pytest.raises(engine.ExecutionError, match="credential_resource_extra"):
            snapshot_resources(conn, "alice")


@pytest.mark.parametrize("raw", ['{broken', '[1]', '{"value": NaN}'])
def test_corrupt_stored_extras_refused(install, raw):
    with db.get_db(install[1]) as conn:
        resource_id = db.add_user_resource(conn, "alice", "folder", "/Documents")
        conn.execute("UPDATE user_resources SET extras = ? WHERE id = ?", (raw, resource_id))
    with pytest.raises(engine.ExecutionError, match="resource_store_invalid"):
        preview(install, [], prune=True)


def test_stale_folder_refused_but_organizer_changes_do_not_block(install):
    declaration = [{"type": "folder", "path": "/Documents", "name": "Planned"}]
    context, operations = preview(install, declaration)
    with db.get_db(install[1]) as conn:
        db.add_user_resource(conn, "alice", "folder", "/Documents", "Concurrent")
    result = engine.execute_plan(operations, context)
    assert result.errors == [{"code": "state_changed", "path": ["users", "alice", "resources"]}]
    assert rows(install)[0].display_name == "Concurrent"
    context, operations = preview(install, declaration)
    with db.get_db(install[1]) as conn:
        db.add_user_resource(conn, "alice", "shared_file", "/shared/new.pdf")
    assert engine.execute_plan(operations, context).exit_code == 2


def test_upserts_and_prune_rollback_together(install):
    with db.get_db(install[1]) as conn:
        db.add_user_resource(conn, "alice", "folder", "/Old")
        conn.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON user_resources BEGIN SELECT RAISE(ABORT, 'failure'); END")
    context, operations = preview(install, [{"type": "folder", "path": "/New"}], prune=True)
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 1 and not result.changed
    assert [r.resource_path for r in rows(install)] == ["/Old"]
