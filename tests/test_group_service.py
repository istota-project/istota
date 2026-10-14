import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from istota import db
from istota.config import Config, UserConfig


@pytest.fixture
def config(tmp_path):
    cfg = Config(db_path=tmp_path / "app.db", users={name: UserConfig() for name in ("alice", "bob", "carol")})
    db.init_db(cfg.db_path)
    return cfg


def create(conn, config, owners=("alice",)):
    from istota.groups import service
    return service.create_group(conn, config, "family", actor_id="admin", is_admin=True,
                                expected_revision=0, owners=list(owners), display_name="Family", kind="family")


def change(conn, config, user, revision, **kwargs):
    from istota.groups import service
    return service.change_membership(conn, config, "family", user, actor_id="alice",
                                     expected_revision=revision, **kwargs)


def test_create_commits_owner_and_event_without_storage(config):
    with db.get_db(config.db_path) as conn:
        result = create(conn, config)
        assert result["revision"] == 1 and result["storage_available"] is False
        assert db.list_group_members(conn, "family") == ["alice"]
        event = conn.execute("SELECT * FROM group_events").fetchone()
        assert event["actor_id"] == "admin" and event["event_type"] == "created"
        assert json.loads(event["details_json"])["owners"] == ["alice"]
        assert not conn.in_transaction


@pytest.mark.parametrize("owners", [[], ["unknown"], ["bob"]])
def test_creation_requires_enabled_existing_owners(config, owners):
    from istota.groups.policy import GroupError
    with db.get_db(config.db_path) as conn:
        conn.execute("INSERT INTO web_auth_identities (user_id, email, disabled) VALUES ('bob', 'bob@example.com', 1)")
        conn.commit()
        with pytest.raises(GroupError):
            create(conn, config, owners)
        assert db.list_groups(conn) == []


def test_acknowledgement_revision_and_role_history(config):
    from istota.groups.policy import GroupError
    with db.get_db(config.db_path) as conn:
        create(conn, config)
        with pytest.raises(GroupError) as exc:
            change(conn, config, "bob", 1, role="member")
        assert exc.value.status_code == 409
        assert exc.value.details["disclosure"]["memory"] == "Groups/family/GROUP.md"
        assert db.list_group_members(conn, "family") == ["alice"]
        change(conn, config, "bob", 1, role="member", acknowledge_history=True)
        with pytest.raises(GroupError) as exc:
            change(conn, config, "bob", 1, role="owner")
        assert exc.value.code == "revision_conflict"
        change(conn, config, "bob", 2, role="owner")
        assert len(db.group_membership_history(conn, "family")) == 2
        event = conn.execute("SELECT * FROM group_events ORDER BY id DESC").fetchone()
        assert json.loads(event["details_json"]) == {"old_role": "member", "new_role": "owner"}
        change(conn, config, "bob", 3, role=None)
        change(conn, config, "bob", 4, role="member", acknowledge_history=True)
        history = db.group_membership_history(conn, "family")
        assert len(history) == 3 and history[1]["ended_at"] is not None


def test_last_owner_atomic_replacement(config):
    from istota.groups.policy import GroupError
    with db.get_db(config.db_path) as conn:
        create(conn, config)
        change(conn, config, "bob", 1, role="member", acknowledge_history=True)
        with pytest.raises(GroupError) as exc:
            change(conn, config, "alice", 2, role=None)
        assert exc.value.code == "last_owner"
        change(conn, config, "alice", 2, role=None, replacement_owner="bob")
        assert [(h["user_id"], h["role"]) for h in db.group_membership_history(conn, "family") if h["ended_at"] is None] == [("bob", "owner")]
        assert db.get_group(conn, "family")["revision"] == 3


def test_ownerless_recovery_and_archive(config):
    from istota.groups import service
    from istota.groups.policy import GroupError
    with db.get_db(config.db_path) as conn:
        db.create_group(conn, "family", kind="family", display_name="F", created_by="alice")
        db.add_group_member(conn, "family", "alice", added_by="admin")
        conn.commit()
        with pytest.raises(GroupError) as exc:
            change(conn, config, "alice", 0, role="owner")
        assert exc.value.status_code == 403
        service.change_membership(conn, config, "family", "alice", role="owner", actor_id="admin", is_admin=True, expected_revision=0)
        service.update_group(conn, "family", actor_id="alice", expected_revision=1, display_name="Renamed")
        with pytest.raises(GroupError):
            service.archive_group(conn, "family", actor_id="alice", expected_revision=2, confirm=True)
        service.archive_group(conn, "family", actor_id="admin", is_admin=True, expected_revision=2, confirm=True)
        assert not db.is_group_member(conn, "family", "alice")
        assert len(db.group_membership_history(conn, "family")) == 1
        assert db.get_group(conn, "family", include_archived=True)["revision"] == 3
        with pytest.raises(GroupError):
            create(conn, config)


def test_concurrent_owner_departures(config):
    from istota.groups import service
    from istota.groups.policy import GroupError
    with db.get_db(config.db_path) as conn:
        create(conn, config, ("alice", "bob"))
        change(conn, config, "carol", 1, role="member", acknowledge_history=True)
    barrier = Barrier(2)
    def leave(user):
        with db.get_db(config.db_path) as conn:
            barrier.wait(timeout=5)
            try:
                service.change_membership(conn, config, "family", user, role=None, actor_id=user, expected_revision=2)
                return "ok"
            except GroupError as exc:
                return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(leave, ["alice", "bob"]))
    assert sorted(results) == ["ok", "revision_conflict"]
    with db.get_db(config.db_path) as conn:
        owners = [h["user_id"] for h in db.group_membership_history(conn, "family") if h["ended_at"] is None and h["role"] == "owner"]
        assert len(owners) == 1
        with pytest.raises(GroupError, match="last owner"):
            service.change_membership(conn, config, "family", owners[0], role=None, actor_id=owners[0], expected_revision=3)


def test_event_failure_rolls_back(config):
    with db.get_db(config.db_path) as conn:
        create(conn, config)
        conn.execute("CREATE TRIGGER reject_event BEFORE INSERT ON group_events BEGIN SELECT RAISE(ABORT, 'event failed'); END")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="event failed"):
            change(conn, config, "bob", 1, role="member", acknowledge_history=True)
        assert db.list_group_members(conn, "family") == ["alice"]
        assert db.get_group(conn, "family")["revision"] == 1


@pytest.mark.parametrize("actor,expected_status", [("bob", 403), ("outsider", 404)])
def test_members_and_nonmembers_cannot_manage(config, actor, expected_status):
    from istota.groups import service
    from istota.groups.policy import GroupError
    with db.get_db(config.db_path) as conn:
        create(conn, config)
        change(conn, config, "bob", 1, role="member", acknowledge_history=True)
        with pytest.raises(GroupError) as exc:
            service.update_group(conn, "family", actor_id=actor, expected_revision=2, display_name="Changed")
        assert exc.value.status_code == expected_status
        assert db.get_group(conn, "family")["display_name"] == "Family"
        if actor == "bob":
            with pytest.raises(GroupError) as exc:
                service.change_membership(conn, config, "family", "bob", actor_id="bob", expected_revision=2, role=None, replacement_owner="alice")
            assert exc.value.status_code == 403
            service.change_membership(conn, config, "family", "bob", actor_id="bob", expected_revision=2, role=None)
            assert not db.is_group_member(conn, "family", "bob")


def test_owner_cannot_create_and_admin_archive_requires_confirmation(config):
    from istota.groups import service
    from istota.groups.policy import GroupError
    with db.get_db(config.db_path) as conn:
        with pytest.raises(GroupError) as exc:
            service.create_group(conn, config, "family", actor_id="alice", expected_revision=0, owners=["alice"], display_name="F")
        assert exc.value.status_code == 403
        create(conn, config)
        with pytest.raises(GroupError) as exc:
            service.archive_group(conn, "family", actor_id="admin", is_admin=True, expected_revision=1)
        assert exc.value.code == "confirmation_required"
        assert db.get_group(conn, "family") is not None


def test_ownerless_members_keep_content_and_admin_can_archive(config):
    from istota.groups import service
    from istota.groups.policy import group_capabilities
    with db.get_db(config.db_path) as conn:
        db.create_group(conn, "family", kind="group", display_name="F", created_by="alice")
        db.add_group_member(conn, "family", "bob", added_by="operator")
        conn.commit()
        capabilities = group_capabilities(conn, "family", "bob", is_admin=False)
        assert capabilities["edit_memory"] and not capabilities["manage"]
        service.archive_group(conn, "family", actor_id="admin", is_admin=True, expected_revision=0, confirm=True)
        assert not db.is_group_member(conn, "family", "bob")


def test_failed_seed_is_retryable_without_duplicate_group(config, monkeypatch):
    from istota import storage
    def seed(*args, **kwargs):
        # Another connection observes the commit before any filesystem work.
        with db.get_db(config.db_path) as observer:
            assert db.is_group_member(observer, "family", "alice")
        raise OSError("storage unavailable")
    monkeypatch.setattr(storage, "ensure_group_directories", seed)
    with db.get_db(config.db_path) as conn:
        result = create(conn, config)
        assert result["storage_available"] is False
        assert len(db.list_groups(conn)) == 1


def test_noop_does_not_increment_revision_or_add_event(config):
    from istota.groups import service
    with db.get_db(config.db_path) as conn:
        create(conn, config)
        result = change(conn, config, "alice", 1, role="owner")
        assert not result["changed"] and result["revision"] == 1
        service.update_group(conn, "family", actor_id="alice", expected_revision=1, display_name="Family")
        assert conn.execute("SELECT count(*) FROM group_events").fetchone()[0] == 1


def test_creation_does_not_replace_a_missing_workspace(config, tmp_path):
    config.workspace_path = tmp_path / "missing-workspace"
    with db.get_db(config.db_path) as conn:
        result = create(conn, config)
        assert result["storage_available"] is False
        assert db.is_group_member(conn, "family", "alice")
        assert not config.workspace_path.exists()
