"""Automatic group membership through real account and group seams."""
import sqlite3

import pytest

from istota import db, user_profiles
from istota.config import Config, UserConfig, _apply_user_profiles, refresh_user_profiles_if_changed
from istota.groups import service
from istota.groups.policy import GroupError, group_capabilities
from istota.webui import auth


@pytest.fixture
def config(tmp_path):
    config = Config(db_path=tmp_path / "app.db", users={"alice": UserConfig(), "__system__": UserConfig()})
    db.init_db(config.db_path)
    user_profiles.ensure_profile(config.db_path, "bob")
    auth.upsert_identity(config.db_path, "bob", "bob@example.com")
    _apply_user_profiles(config)
    return config


def reconcile(config, **kwargs):
    from istota.groups.system import reconcile_all_users
    with db.get_db(config.db_path) as conn:
        return reconcile_all_users(conn, config, **kwargs)


def test_collision_membership_and_idempotent_reconciliation(config):
    with db.get_db(config.db_path) as conn:
        db.create_group(conn, "all-users", display_name="All users", kind="family", created_by="alice")
    group = reconcile(config)
    assert group["system_key"] == "all_users" and group["group_id"] != "all-users"
    assert db.is_valid_group_id(group["group_id"])
    assert reconcile(config)["group_id"] == group["group_id"]
    with db.get_db(config.db_path) as conn:
        assert db.list_group_members(conn, group["group_id"]) == ["alice", "bob"]
        assert {m["role"] for m in db.group_membership_history(conn, group["group_id"])} == {"member"}
        assert len(db.group_membership_history(conn, group["group_id"])) == 2
        assert db.get_group(conn, "all-users")["system_key"] is None
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE groups SET system_key = 'all_users' WHERE group_id = 'all-users'")


def test_account_lifecycle_preserves_intervals_and_disabled_gate(config):
    group = reconcile(config)
    gid = group["group_id"]
    user_profiles.ensure_profile(config.db_path, "carol")
    with db.get_db(config.db_path) as conn:
        assert db.is_group_member(conn, gid, "carol")
    auth.set_disabled(config.db_path, "bob", True)
    with db.get_db(config.db_path) as conn:
        assert not db.is_group_member(conn, gid, "bob")
        assert gid not in db.list_user_groups(conn, "bob")
    auth.set_disabled(config.db_path, "bob", False)
    with db.get_db(config.db_path) as conn:
        history = [m for m in db.group_membership_history(conn, gid) if m["user_id"] == "bob"]
        assert len(history) == 2 and history[0]["ended_at"] and history[1]["ended_at"] is None
    user_profiles.delete_profile(config.db_path, "carol")
    reconcile(config)
    with db.get_db(config.db_path) as conn:
        assert not db.is_group_member(conn, gid, "carol")
        # Even a delayed lifecycle hook cannot let a disabled identity read.
        conn.execute("UPDATE web_auth_identities SET disabled = 1 WHERE user_id = 'bob'")
        db.add_group_member(conn, gid, "bob", added_by="__system__")
        assert not db.is_group_member(conn, gid, "bob")


def test_complete_config_reload_and_failed_registry_load(config, monkeypatch):
    gid = reconcile(config)["group_id"]
    config.users = {"bob": config.users["bob"]}
    reconcile(config, registry_complete=False)
    with db.get_db(config.db_path) as conn:
        assert db.is_group_member(conn, gid, "alice")
    reconcile(config)
    with db.get_db(config.db_path) as conn:
        assert not db.is_group_member(conn, gid, "alice")
    config.users["alice"] = UserConfig()
    reconcile(config)
    monkeypatch.setattr(user_profiles, "list_profiles", lambda *a, **k: (_ for _ in ()).throw(OSError("unavailable")))
    config.users = {}
    _apply_user_profiles(config)
    reconcile(config)
    with db.get_db(config.db_path) as conn:
        assert db.is_group_member(conn, gid, "alice")
        assert db.is_group_member(conn, gid, "bob")


def test_deleted_overlay_user_is_not_readded_from_stale_config(config):
    gid = reconcile(config)["group_id"]
    user_profiles.delete_profile(config.db_path, "bob")
    assert "bob" in config.users
    refresh_user_profiles_if_changed(config)
    reconcile(config)
    with db.get_db(config.db_path) as conn:
        assert not db.is_group_member(conn, gid, "bob")


def test_system_policy_and_mutation_refusals(config):
    group = reconcile(config)
    gid = group["group_id"]
    with db.get_db(config.db_path) as conn:
        member = group_capabilities(conn, gid, "alice", is_admin=False)
        admin = group_capabilities(conn, gid, "alice", is_admin=True)
        assert member["read_memory"] and member["edit_memory"] and member["read_briefings"]
        assert member["receive_briefings"] and not member["author_briefings"]
        assert admin["author_briefings"] and not admin["owner"]
        assert not admin["manage"] and not admin["archive"] and not admin["leave"]
        for role in (None, "member", "owner"):
            with pytest.raises(GroupError) as exc:
                service.change_membership(conn, config, gid, "alice", actor_id="alice", is_admin=True,
                                          expected_revision=group["revision"], role=role)
            assert exc.value.code == "automatic_membership"
        with pytest.raises(GroupError) as exc:
            service.archive_group(conn, gid, actor_id="alice", is_operator=True,
                                  expected_revision=group["revision"], confirm=True)
        assert exc.value.code == "system_group"
        assert not db.archive_group(conn, gid)
        assert "retained" in " ".join(service.history_disclosure(gid)["briefings"])


def test_automatic_membership_does_not_load_ambient_group_memory(config):
    from istota.rooms.scopes import task_group_ids
    gid = reconcile(config)["group_id"]
    with db.get_db(config.db_path) as conn:
        db.create_group(conn, "team", display_name="Team", kind="group", created_by="alice")
        db.add_group_member(conn, "team", "alice", added_by="alice")
        task_id = db.create_task(conn, user_id="alice", prompt="Hello")
        task = db.get_task(conn, task_id)
        assert task_group_ids(conn, task) == ["team"]
        room = db.create_web_chat_room(conn, "alice", "Installation")
        db.set_room_group(conn, room.token, gid)
        task.conversation_token = room.token
        assert task_group_ids(conn, task) == [gid]


def test_startup_provisions_shared_storage_and_single_user(tmp_path):
    from istota.groups.system import refresh_all_users
    config = Config(db_path=tmp_path / "app.db", workspace_path=tmp_path / "workspace",
                    users={"alice": UserConfig(disabled_modules=["briefings"])})
    config.workspace_path.mkdir()
    db.init_db(config.db_path)
    refresh_all_users(config, provision=True)
    with db.get_db(config.db_path) as conn:
        group = conn.execute("SELECT group_id FROM groups WHERE system_key = 'all_users'").fetchone()
        assert db.list_group_members(conn, group["group_id"]) == ["alice"]
        assert (config.workspace_path / "Groups" / group["group_id"] / "GROUP.md").is_file()


def test_upgrade_retains_ordinary_group_and_is_idempotent(config):
    with db.get_db(config.db_path) as conn:
        db.create_group(conn, "team", kind="family", display_name="All users", created_by="alice")
        db.add_group_member(conn, "team", "alice", added_by="alice")
        conn.execute("DROP INDEX idx_groups_system_key")
        conn.execute("ALTER TABLE groups DROP COLUMN system_key")
        conn.execute("DELETE FROM _migration_state WHERE name = 'system_groups_v1'")
    db.init_db(config.db_path)
    db.init_db(config.db_path)
    gid = reconcile(config)["group_id"]
    with db.get_db(config.db_path) as conn:
        assert db.get_group(conn, "team")["system_key"] is None
        assert db.list_group_members(conn, "team") == ["alice"]
        assert db.get_group(conn, gid)["system_key"] == "all_users"
        assert conn.execute("SELECT 1 FROM _migration_state WHERE name = 'system_groups_v1'").fetchone()


def test_concurrent_provisioning_uses_one_audience(config):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        groups = list(pool.map(lambda _: reconcile(config), range(2)))
    assert groups[0]["group_id"] == groups[1]["group_id"]
    with db.get_db(config.db_path) as conn:
        assert len(db.group_membership_history(conn, groups[0]["group_id"])) == 2
        assert conn.execute("SELECT COUNT(*) FROM group_events WHERE event_type = 'created'").fetchone()[0] == 1


def test_cli_refuses_manual_system_membership_and_archive(config, monkeypatch, capsys):
    from types import SimpleNamespace
    from istota import cli
    group = reconcile(config)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)
    args = SimpleNamespace(config=None, group_id=group["group_id"], user_id="alice",
                           revision=group["revision"], replacement_owner=None, confirm=True,
                           role="owner", acknowledge_history=True)
    for operation in (cli.cmd_group_add_member, cli.cmd_group_remove_member, cli.cmd_group_archive):
        with pytest.raises(SystemExit) as exc:
            operation(args)
        assert exc.value.code == 1
        assert "automatic" in capsys.readouterr().err or operation == cli.cmd_group_archive


def test_local_admin_can_author_without_becoming_an_owner(config):
    from istota.groups.routes import _detail
    config.users = {"alice": UserConfig()}
    config.web.auth = ["none"]
    gid = reconcile(config)["group_id"]
    with db.get_db(config.db_path) as conn:
        capabilities = _detail(conn, config, "alice", gid)["capabilities"]
        assert capabilities["author_briefings"] and not capabilities["owner"]
