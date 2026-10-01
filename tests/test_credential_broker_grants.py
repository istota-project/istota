"""Grants freeze the first attempt, including an empty first attempt."""

import pytest
from istota import db, secrets_store
from istota.credential_broker import grants
from istota.credential_broker.bindings import parse_binding


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    path = tmp_path / "data.db"
    db.init_db(path)
    secrets_store.upsert_secret(path, "alice", "vault_entries", "portal", "fixture-password",
                               binding=parse_binding("https://portal.example", {}, []))
    return path


def task(conn, **kwargs):
    return db.create_task(conn, user_id="alice", prompt="test", source_type="talk",
                         conversation_token=kwargs.pop("room", "room-a"), **kwargs)


def check(conn, task_id, **kwargs):
    return grants.check_credential_grant(conn, task_id, "alice", "portal",
                                       "portal.example", kwargs.pop("method", "POST"),
                                       "authorization", **kwargs)


def test_narrow_defaults_and_live_policy(database):
    with db.get_db(database) as conn:
        first = task(conn)
    with db.get_db(database) as conn:
        assert grants.ensure_credential_grants(conn, first, "alice") == {}
        assert check(conn, first) == "credential_not_granted"
        grant = grants.put_grant(conn, "alice", "portal")
        assert grant["methods"] == ["GET", "HEAD", "POST", "PUT", "PATCH"]
        assert grant["allow_scheduled"] is False
        second = task(conn)
    with db.get_db(database) as conn:
        assert grants.ensure_credential_grants(conn, second, "alice") == {"portal": 1}
        assert check(conn, second) is None
        assert check(conn, second, method="DELETE") == "credential_method_not_allowed"
        grants.put_grant(conn, "alice", "portal", methods=["DELETE"])
        assert check(conn, second) == "credential_changed"


def test_empty_snapshot_and_retry_never_widen(database):
    with db.get_db(database) as conn:
        first = task(conn)
    with db.get_db(database) as conn:
        grants.ensure_credential_grants(conn, first, "alice")
        grants.put_grant(conn, "alice", "portal")
    with db.get_db(database) as conn:
        assert grants.ensure_credential_grants(conn, first, "alice") == {}
        second = task(conn)
    with db.get_db(database) as conn:
        assert grants.ensure_credential_grants(conn, second, "alice") == {"portal": 1}
    secrets_store.upsert_secret(database, "alice", "vault_entries", "later", "fixture-password",
                               binding=parse_binding("https://portal.example", {}, []))
    with db.get_db(database) as conn:
        grants.put_grant(conn, "alice", "later")
    with db.get_db(database) as conn:
        assert grants.ensure_credential_grants(conn, second, "alice") == {"portal": 1}


def test_one_time_grant_existing_keeps_later_credentials_narrow(database):
    with db.get_db(database) as conn:
        assert grants.grant_what_exists(conn, "alice") == 1
        assert grants.get_grant(conn, "alice", "portal")["allow_scheduled"] is True
    secrets_store.upsert_secret(database, "alice", "vault_entries", "later", "fixture-password",
                               binding=parse_binding("https://portal.example", {}, []))
    with db.get_db(database) as conn:
        assert grants.grant_what_exists(conn, "alice") == 0
        assert grants.get_grant(conn, "alice", "later") is None
        assert grants.get_grant(conn, "bob", "portal") is None


@pytest.mark.parametrize("room,scheduled", [("room-b", False), (None, False), ("room-a", True)])
def test_room_and_scheduled_scope(database, room, scheduled):
    with db.get_db(database) as conn:
        grants.put_grant(conn, "alice", "portal", scope_mode="rooms", rooms=["room-a"])
        identifier = task(conn, room=room, scheduled_job_id=1 if scheduled else None)
    with db.get_db(database) as conn:
        assert grants.ensure_credential_grants(conn, identifier, "alice") == {}


def test_delete_and_recreate_does_not_revive_snapshot(database):
    with db.get_db(database) as conn:
        grants.put_grant(conn, "alice", "portal")
        identifier = task(conn)
    with db.get_db(database) as conn:
        grants.ensure_credential_grants(conn, identifier, "alice")
        grants.delete_grant(conn, "alice", "portal")
        assert check(conn, identifier) == "credential_not_granted"
        grants.put_grant(conn, "alice", "portal")
        assert check(conn, identifier) == "credential_changed"
        assert grants.get_grant(conn, "alice", "portal")["policy_revision"] == 2


def test_binding_value_and_owner_revocation(database):
    with db.get_db(database) as conn:
        grants.put_grant(conn, "alice", "portal")
        identifier = task(conn)
    with db.get_db(database) as conn:
        grants.ensure_credential_grants(conn, identifier, "alice")
        with pytest.raises(ValueError, match="owner"):
            grants.ensure_credential_grants(conn, identifier, "bob")
        conn.execute("UPDATE credential_bindings SET hosts='[]'")
        assert check(conn, identifier) == "credential_not_bound"
    secrets_store.delete_secret(database, "alice", "vault_entries", "portal")
    with db.get_db(database) as conn:
        assert check(conn, identifier) == "credential_not_granted"


def test_schema_upgrade_is_idempotent(database):
    with db.get_db(database) as conn:
        for table in ("credential_grants", "credential_grant_rooms", "credential_task_grants"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("ALTER TABLE tasks DROP COLUMN credential_grants_initialized")
    db.init_db(database)
    db.init_db(database)
    with db.get_db(database) as conn:
        assert conn.execute("SELECT count(*) FROM credential_task_grants").fetchone()[0] == 0


def test_concurrent_snapshot_initialization_is_one_boundary(database):
    from concurrent.futures import ThreadPoolExecutor
    with db.get_db(database) as conn:
        grants.put_grant(conn, "alice", "portal")
        identifier = task(conn)
    def freeze():
        with db.get_db(database) as conn:
            return grants.ensure_credential_grants(conn, identifier, "alice")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: freeze(), range(2)))
    assert results == [{"portal": 1}, {"portal": 1}]


def test_scheduled_ancestor_and_live_header_refusal(database):
    with db.get_db(database) as conn:
        grants.put_grant(conn, "alice", "portal")
        parent = task(conn, scheduled_job_id=1)
        child = task(conn, parent_task_id=parent)
    with db.get_db(database) as conn:
        assert grants.ensure_credential_grants(conn, child, "alice") == {}
        grants.put_grant(conn, "alice", "portal", allow_scheduled=True)
        allowed = task(conn, parent_task_id=parent)
    with db.get_db(database) as conn:
        assert grants.ensure_credential_grants(conn, allowed, "alice") == {"portal": 2}
        assert grants.check_credential_grant(conn, allowed, "alice", "portal", "portal.example",
                                              "POST", "proxy-authorization") == "credential_header_not_allowed"


def test_task_retention_explicitly_removes_snapshots(database):
    with db.get_db(database) as conn:
        grants.put_grant(conn, "alice", "portal")
        identifier = task(conn)
    with db.get_db(database) as conn:
        grants.ensure_credential_grants(conn, identifier, "alice")
        conn.execute("UPDATE tasks SET status='completed', completed_at='2000-01-01' WHERE id=?", (identifier,))
        assert db.cleanup_old_tasks(conn, retention_days=1) == 1
        assert conn.execute("SELECT count(*) FROM credential_task_grants").fetchone()[0] == 0


def test_forge_uses_live_config_on_every_check(database):
    from istota.config import Config, DeveloperConfig
    from istota.credential_broker.bindings import sync_forge_bindings
    developer = DeveloperConfig(enabled=True, github_token="fixture-token")
    config = Config(developer=developer)
    with db.get_db(database) as conn:
        sync_forge_bindings(conn, "alice", developer)
        grants.put_grant(conn, "alice", "forge.github")
        identifier = task(conn)
    with db.get_db(database) as conn:
        grants.ensure_credential_grants(conn, identifier, "alice")
        args = (conn, identifier, "alice", "forge.github", "api.github.com", "GET", "authorization")
        assert grants.check_credential_grant(*args, config=config) is None
        developer.github_token = ""
        assert grants.check_credential_grant(*args, config=config) == "credential_not_bound"


def test_deleted_credential_cannot_inherit_a_previous_grant(database):
    with db.get_db(database) as conn:
        grants.put_grant(conn, "alice", "portal")
        identifier = task(conn)
    with db.get_db(database) as conn:
        grants.ensure_credential_grants(conn, identifier, "alice")
    secrets_store.delete_secret(database, "alice", "vault_entries", "portal")
    secrets_store.upsert_secret(database, "alice", "vault_entries", "portal", "replacement-password",
                               binding=parse_binding("https://portal.example", {}, []))
    with db.get_db(database) as conn:
        assert grants.get_grant(conn, "alice", "portal") is None
        assert check(conn, identifier) == "credential_not_granted"


def test_room_deletion_removes_task_snapshot(database):
    with db.get_db(database) as conn:
        room = db.create_web_chat_room(conn, "alice", "Personal")
        grants.put_grant(conn, "alice", "portal")
        identifier = task(conn, room=room.token)
    with db.get_db(database) as conn:
        grants.ensure_credential_grants(conn, identifier, "alice")
        assert db.delete_web_chat_room(conn, room.id, "alice")
        assert conn.execute("SELECT count(*) FROM credential_task_grants").fetchone()[0] == 0


def test_live_forge_check_rechecks_admin_and_enabled(database):
    from istota.config import Config, DeveloperConfig
    from istota.credential_broker.bindings import sync_forge_bindings
    config = Config(developer=DeveloperConfig(enabled=True, github_token="fixture-token"),
                    admin_users={"alice"})
    with db.get_db(database) as conn:
        sync_forge_bindings(conn, "alice", config.developer)
        grants.put_grant(conn, "alice", "forge.github")
        identifier = task(conn)
    with db.get_db(database) as conn:
        grants.ensure_credential_grants(conn, identifier, "alice")
        args = (conn, identifier, "alice", "forge.github", "api.github.com", "GET", "authorization")
        assert grants.check_credential_grant(*args, config=config) is None
        config.admin_users = {"bob"}
        assert grants.check_credential_grant(*args, config=config) == "credential_not_granted"
        config.admin_users = {"alice"}
        config.developer.enabled = False
        assert grants.check_credential_grant(*args, config=config) == "credential_not_granted"


@pytest.mark.parametrize("withheld,expected", [
    (frozenset(), {"portal": 1}),
    (frozenset({"calendar"}), {}),
    (frozenset({"files", "memory"}), {}),
])
def test_a_shared_rooms_withheld_scopes_keep_vault_grants_out(database, withheld, expected):
    """Multiplayer Stage 9: a restricted shared-room task gets none of the
    sender's vault credentials, brokered placeholders included."""
    with db.get_db(database) as conn:
        grants.put_grant(conn, "alice", "portal")
        identifier = task(conn)
    with db.get_db(database) as conn:
        assert grants.ensure_credential_grants(
            conn, identifier, "alice", withheld_scopes=withheld,
        ) == expected
        if not expected:
            assert check(conn, identifier) == "credential_not_granted"
