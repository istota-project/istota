"""Operator definitions use the same ownership, revision and grant checks as HTTP."""

import json

import pytest

from istota import cli_briefings, db, user_profiles
from istota.briefings import group_store as store
from istota.config import load_config
from istota.lib import toml_write
from istota.settings_plan import engine, snapshot
from istota.settings_plan.schema import SchemaError, parse_plan


@pytest.fixture
def install(tmp_path, monkeypatch):
    admins = tmp_path / "admins"
    admins.write_text("admin\n")
    monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(admins))
    database = tmp_path / "framework.db"
    db.init_db(database)
    path = tmp_path / "config.toml"
    path.write_text(toml_write.dumps({"db_path": str(database), "workspace_path": str(tmp_path / "workspace"),
                                    "default_briefings": [], "briefing_shared_blocks": []}))
    for user in ("alice", "bob", "admin"):
        user_profiles.provision_user(database, user, {})
    with db.get_db(database) as conn:
        db.create_group(conn, "team", kind="group", display_name="Team", created_by="alice")
        db.add_group_member(conn, "team", "alice", role="owner", added_by="alice")
        db.add_group_member(conn, "team", "bob", added_by="alice")
    return path, load_config(path, apply_db_overlays=False)


def definition(**changes):
    return {"group_id": "team", "name": "daily", "title": "Daily", "cron": "0 7 * * *",
            "timezone": "UTC", "channel": "in_app", "generation_mode": "prompt",
            "generation": {"version": 1, "prompt": "Write a short edition", "research": {"enabled": False}, "inputs": []},
            **changes}


def preview(install, declarations, *, actor="alice", **options):
    context = engine.Context(install[0])
    operations = engine.build_plan(parse_plan({"group_briefings": declarations}), context,
                                   engine.Options(actor=actor, **options))
    return context, operations


def execute(install, declarations, **options):
    context, operations = preview(install, declarations, **options)
    return engine.execute_plan(operations, context)


def test_draft_partial_update_snapshot_roundtrip_and_no_grants(install):
    before = install[1].db_path.read_bytes()
    assert execute(install, [definition()], dry_run=True).exit_code == 2
    assert install[1].db_path.read_bytes() == before
    assert execute(install, [definition()]).exit_code == 2
    assert execute(install, [{"group_id": "team", "name": "daily", "title": "Edited in DB"}]).exit_code == 2
    document, _ = snapshot.gather(install[0])
    assert document["group_briefings"][0]["title"] == "Edited in DB"
    assert document["group_briefings"][0]["enabled"] is False
    assert set(document["group_briefings"][0]) == set(definition()) | {"enabled", "excluded_user_ids"}
    assert execute(install, document["group_briefings"]).exit_code == 0
    selected, _ = snapshot.gather(install[0], users=["alice"])
    assert "group_briefings" not in selected
    with db.get_db(install[1].db_path) as conn:
        assert store.list_definitions(conn, "team")[0]["revision"] == 2
        assert conn.execute("SELECT COUNT(*) FROM group_briefing_source_grants").fetchone()[0] == 0


@pytest.mark.parametrize("actor,code", [(None, "group_briefing_actor_required"), ("bob", "briefing_forbidden"),
                                         ("admin", "group_not_found"), ("__operator__", "group_not_found")])
def test_plan_requires_current_owner(install, actor, code):
    with pytest.raises(engine.ExecutionError, match=code):
        preview(install, [definition()], actor=actor)


def test_successor_authority_and_stale_plan(install):
    assert execute(install, [definition()]).exit_code == 2
    context, operations = preview(install, [{"group_id": "team", "name": "daily", "title": "Stale"}])
    with db.get_db(install[1].db_path) as conn:
        conn.execute("UPDATE group_members SET role='owner' WHERE group_id='team' AND user_id='bob'")
        conn.execute("UPDATE group_members SET role='member' WHERE group_id='team' AND user_id='alice'")
    assert engine.execute_plan(operations, context).errors[0]["code"] == "briefing_forbidden"
    assert execute(install, [{"group_id": "team", "name": "daily", "title": "Successor"}], actor="bob").exit_code == 2
    with db.get_db(install[1].db_path) as conn:
        saved = store.list_definitions(conn, "team")[0]
        assert saved["created_by"] == "alice" and saved["updated_by"] == "bob"
        assert saved["revision"] == 2


def test_new_definitions_cannot_be_enabled_and_plan_cannot_embed_grants(install):
    with pytest.raises(engine.ExecutionError, match="draft_required"):
        preview(install, [definition(enabled=True)])
    with pytest.raises(SchemaError):
        parse_plan({"group_briefings": [definition(grants=[{"approved_by": "bob"}])]})
    personal = definition()
    personal["generation"]["inputs"] = [{"id": "file", "kind": "personal_file", "config": {"owner_id": "bob", "path": "notes.md"}}]
    assert execute(install, [personal]).exit_code == 2
    with pytest.raises(engine.ExecutionError, match="source_approval_required"):
        preview(install, [{"group_id": "team", "name": "daily", "enabled": True}])
    with db.get_db(install[1].db_path) as conn:
        assert store.list_definitions(conn, "team")[0]["enabled"] is False


def test_all_users_admin_exception(install):
    from istota.groups.system import reconcile_all_users
    with db.get_db(install[1].db_path) as conn:
        group = reconcile_all_users(conn, install[1])
    assert execute(install, [definition(group_id=group["group_id"])], actor="admin").exit_code == 2
    with pytest.raises(engine.ExecutionError, match="briefing_forbidden"):
        preview(install, [definition(group_id=group["group_id"], title="No")])


def test_cli_revision_owner_and_draft_contract(install, tmp_path, capsys):
    path = tmp_path / "definition.json"
    raw = definition()
    raw.pop("group_id")
    path.write_text(json.dumps(raw))
    args = ["group", "save", "--group-id", "team", "--actor", "alice", "--file", str(path), "--revision", "0"]
    assert cli_briefings.dispatch(args, install[1]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["revision"] == 1 and saved["enabled"] is False
    assert cli_briefings.dispatch(args, install[1]) == 1
    assert "revision_conflict" in capsys.readouterr().err
    args[-1] = "1"
    args[5] = "bob"
    assert cli_briefings.dispatch(args, install[1]) == 1
    assert "briefing_forbidden" in capsys.readouterr().err


def approved_file(install):
    from istota.briefings.source_grants import approve_source, source_approval_state

    config = install[1]
    root = config.workspace_root("bob")
    root.mkdir(parents=True)
    (root / "notes.md").write_text("PRIVATE SOURCE BODY CANARY")
    raw = definition()
    raw["generation"]["inputs"] = [{"id": "file", "kind": "personal_file", "config": {"owner_id": "bob", "path": "notes.md"}}]
    assert execute(install, [raw]).exit_code == 2
    with db.get_db(config.db_path) as conn:
        saved = store.list_definitions(conn, "team")[0]
        state = source_approval_state(conn, config, saved["id"], "file", actor_id="bob")
        grant = approve_source(conn, config, saved["id"], "file", actor_id="bob",
                               expected_definition_revision=saved["revision"], expected_grant_revision=0,
                               acknowledged=True, audience_contract=state["audience_contract"])
    return saved, grant


def test_grants_stay_local_snapshot_and_source_change_invalidates(install):
    from istota.briefings.source_grants import resolve_source_grant
    from istota.groups.policy import GroupError

    saved, grant = approved_file(install)
    assert execute(install, [{"group_id": "team", "name": "daily", "enabled": True}]).exit_code == 2
    document, _ = snapshot.gather(install[0])
    assert "PRIVATE SOURCE BODY CANARY" not in json.dumps(document)
    assert "approved_at" not in json.dumps(document["group_briefings"])
    assert execute(install, document["group_briefings"]).exit_code == 0
    # The same input in a different definition has no transferable consent.
    copied = dict(document["group_briefings"][0], name="copy", enabled=False)
    assert execute(install, [copied]).exit_code == 2
    with pytest.raises(engine.ExecutionError, match="source_approval_required"):
        preview(install, [{"group_id": "team", "name": "copy", "enabled": True}])
    change = {"group_id": "team", "name": "daily", "channel": "ntfy"}
    with pytest.raises(engine.ExecutionError, match="source_approval_changed"):
        preview(install, [change])
    assert execute(install, [{**change, "enabled": False}]).exit_code == 2
    with db.get_db(install[1].db_path) as conn:
        current = store.get_definition(conn, saved["id"])
        with pytest.raises(GroupError):
            resolve_source_grant(conn, install[1], current, "file")
        assert conn.execute("SELECT revoked_at FROM group_briefing_source_grants WHERE id=?", (grant["id"],)).fetchone()[0]
    with pytest.raises(engine.ExecutionError, match="source_approval_required"):
        preview(install, [{"group_id": "team", "name": "daily", "enabled": True}])


def test_grant_revocation_after_preview_refuses_activation(install):
    from istota.briefings.source_grants import revoke_source

    saved, grant = approved_file(install)
    context, operations = preview(install, [{"group_id": "team", "name": "daily", "enabled": True}])
    with db.get_db(install[1].db_path) as conn:
        revoke_source(conn, install[1], saved["id"], "file", actor_id="bob", expected_grant_revision=grant["revision"])
    result = engine.execute_plan(operations, context)
    assert result.errors[0]["code"] == "source_approval_required"
    with db.get_db(install[1].db_path) as conn:
        assert store.get_definition(conn, saved["id"])["enabled"] is False


def test_failed_later_save_rolls_back_definitions_and_grant_invalidation(install):
    saved, grant = approved_file(install)
    assert execute(install, [definition(name="second")]).exit_code == 2
    context, operations = preview(install, [{"group_id": "team", "name": "daily", "channel": "ntfy"},
                                           {"group_id": "team", "name": "second", "title": "Refused"}])
    with db.get_db(install[1].db_path) as conn:
        conn.execute("CREATE TRIGGER refuse_second BEFORE UPDATE ON group_briefing_configs "
                     "WHEN OLD.name='second' BEGIN SELECT RAISE(ABORT, 'refused'); END")
    assert engine.execute_plan(operations, context).exit_code == 1
    with db.get_db(install[1].db_path) as conn:
        assert store.get_definition(conn, saved["id"]) == saved
        assert conn.execute("SELECT revoked_at FROM group_briefing_source_grants WHERE id=?", (grant["id"],)).fetchone()[0] is None
        assert store.list_definitions(conn, "team")[1]["title"] == "Daily"


def test_definition_revision_is_checked_after_preview(install):
    assert execute(install, [definition()]).exit_code == 2
    context, operations = preview(install, [{"group_id": "team", "name": "daily", "title": "Stale"}])
    assert execute(install, [{"group_id": "team", "name": "daily", "title": "Concurrent"}]).exit_code == 2
    assert engine.execute_plan(operations, context).errors[0]["code"] == "state_changed"
    with db.get_db(install[1].db_path) as conn:
        assert store.list_definitions(conn, "team")[0]["title"] == "Concurrent"


def test_admin_revocation_after_preview(install, tmp_path):
    from istota.groups.system import reconcile_all_users

    with db.get_db(install[1].db_path) as conn:
        group = reconcile_all_users(conn, install[1])
    context, operations = preview(install, [definition(group_id=group["group_id"])], actor="admin")
    (tmp_path / "admins").write_text("alice\n")
    assert engine.execute_plan(operations, context).errors[0]["code"] == "briefing_forbidden"


def test_prune_retains_history_and_omitted_definitions(install):
    assert execute(install, [definition()]).exit_code == 2
    result = execute(install, [definition(name="other")], prune=True)
    assert result.exit_code == 2 and "disable" in result.warnings[0]
    with db.get_db(install[1].db_path) as conn:
        assert len(store.list_definitions(conn, "team")) == 2


@pytest.mark.parametrize("source", [
    {"id": "file", "kind": "personal_file", "grant_id": 1, "config": {"owner_id": "bob", "path": "notes.md"}},
    {"id": "calendar", "kind": "calendar", "config": {}},
])
def test_common_validator_refuses_forged_sources(install, source):
    raw = definition()
    raw["generation"]["inputs"] = [source]
    with pytest.raises(engine.ExecutionError, match="invalid_definition"):
        preview(install, [raw])


def test_real_apply_cli_actor_flag_and_unchanged_reapply(install, tmp_path, monkeypatch, capsys):
    from istota import cli

    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"group_briefings": [definition()]}))
    args = ["istota", "-c", str(install[0]), "apply", "-f", str(path), "--format", "json"]
    for actor, expected in [(None, 1), ("alice", 2), ("alice", 0), ("bob", 0)]:
        monkeypatch.setattr("sys.argv", args + (["--actor", actor] if actor else []))
        code = 0
        try:
            cli.main()
        except SystemExit as result:
            code = result.code
        assert code == expected
        report = json.loads(capsys.readouterr().out)
        if actor is None:
            assert report["errors"][0]["code"] == "group_briefing_actor_required"


def test_snapshot_with_multiple_owners_only_authorizes_actual_writes(install):
    assert execute(install, [definition()]).exit_code == 2
    with db.get_db(install[1].db_path) as conn:
        db.create_group(conn, "other", kind="group", display_name="Other", created_by="bob")
        db.add_group_member(conn, "other", "bob", role="owner", added_by="bob")
    assert execute(install, [definition(group_id="other")], actor="bob").exit_code == 2
    document, _ = snapshot.gather(install[0])
    assert execute(install, document["group_briefings"], actor=None).exit_code == 0
    document["group_briefings"][1]["title"] = "Owned update"
    assert execute(install, document["group_briefings"]).exit_code == 2
    document["group_briefings"][0]["title"] = "Forbidden update"
    with pytest.raises(engine.ExecutionError, match="group_not_found"):
        preview(install, document["group_briefings"])
    with db.get_db(install[1].db_path) as conn:
        assert store.list_definitions(conn, "other")[0]["title"] == "Daily"


def test_exclusion_cli_apply_snapshot_and_partial_saves(install, tmp_path, capsys):
    raw = definition(excluded_user_ids=["bob"])
    raw.pop("group_id")
    path = tmp_path / "definition.json"
    path.write_text(json.dumps(raw))
    args = ["group", "save", "--group-id", "team", "--actor", "alice", "--file", str(path), "--revision", "0"]
    assert cli_briefings.dispatch(args, install[1]) == 0
    assert json.loads(capsys.readouterr().out)["excluded_user_ids"] == ["bob"]
    del raw["excluded_user_ids"]
    path.write_text(json.dumps(raw))
    args[-1] = "1"
    assert cli_briefings.dispatch(args, install[1]) == 0
    assert json.loads(capsys.readouterr().out)["excluded_user_ids"] == ["bob"]
    assert execute(install, [{"group_id": "team", "name": "daily", "title": "Edited"}]).exit_code == 2
    document, _ = snapshot.gather(install[0])
    assert document["group_briefings"][0]["excluded_user_ids"] == ["bob"]
    assert execute(install, document["group_briefings"]).exit_code == 0
    with pytest.raises(engine.ExecutionError, match="invalid_excluded_user"):
        preview(install, [{"group_id": "team", "name": "daily", "excluded_user_ids": ["admin"]}])
    with pytest.raises(engine.ExecutionError, match="briefing_forbidden"):
        preview(install, [{"group_id": "team", "name": "daily", "excluded_user_ids": []}], actor="bob")
    assert execute(install, [{"group_id": "team", "name": "daily", "excluded_user_ids": []}]).exit_code == 2
    document, _ = snapshot.gather(install[0])
    assert document["group_briefings"][0]["excluded_user_ids"] == []


def test_exclusion_keeps_source_grant_and_all_users_admin_exception(install):
    from istota.briefings.source_grants import resolve_source_grant
    from istota.groups.system import reconcile_all_users
    saved, grant = approved_file(install)
    assert execute(install, [{"group_id": "team", "name": "daily", "excluded_user_ids": ["bob"]}]).exit_code == 2
    with db.get_db(install[1].db_path) as conn:
        current = store.get_definition(conn, saved["id"])
        assert resolve_source_grant(conn, install[1], current, "file")["id"] == grant["id"]
        group = reconcile_all_users(conn, install[1])
    assert execute(install, [definition(group_id=group["group_id"], excluded_user_ids=["bob"])], actor="admin").exit_code == 2


def blocks_definition():
    return definition(generation_mode="blocks", generation={"version": 1, "blocks": [
        {"title": "Quotes", "directive": "", "sources": [{"id": "quotes", "kind": "markets", "config": {}}]},
        {"title": "Notes", "sources": [{"id": "notebook", "kind": "group_memory", "config": {}}]},
    ]})


def test_empty_block_directive_snapshots_and_applies_unchanged(install):
    assert execute(install, [blocks_definition()]).exit_code == 2
    document, _ = snapshot.gather(install[0])
    blocks = document["group_briefings"][0]["generation"]["blocks"]
    assert [block["directive"] for block in blocks] == ["", ""]
    assert execute(install, document["group_briefings"]).exit_code == 0


def test_stored_null_directive_snapshots_and_applies_unchanged(install):
    # A definition saved while the store still wrote null for an empty directive.
    assert execute(install, [blocks_definition()]).exit_code == 2
    with db.get_db(install[1].db_path) as conn:
        row = conn.execute("SELECT id, generation_json FROM group_briefing_configs").fetchone()
        generation = json.loads(row["generation_json"])
        for block in generation["blocks"]:
            block["directive"] = None
        conn.execute("UPDATE group_briefing_configs SET generation_json=? WHERE id=?",
                     (json.dumps(generation), row["id"]))
    document, _ = snapshot.gather(install[0])
    assert [block["directive"] for block in document["group_briefings"][0]["generation"]["blocks"]] == ["", ""]
    assert execute(install, document["group_briefings"]).exit_code == 0
    with db.get_db(install[1].db_path) as conn:
        assert store.list_definitions(conn, "team")[0]["revision"] == 1
    _, operations = preview(install, [{"group_id": "team", "name": "daily", "title": "Renamed"}])
    assert [change["path"] for change in operations[0].changes] == [["title"]]
