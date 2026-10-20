"""Group-owned rooms: the relationship, the access predicate and the policy.

A room linked to a group (`rooms.group_id`) keeps its own roster and host;
a group-owned room (`rooms.membership_source = 'group'`) holds the group's
current membership and is governed by the group's current owners. Both carry
the group link, which is what filters group memory in each case. Nothing
converts an existing linked room.
"""

import sqlite3
from pathlib import Path

import pytest

from istota import db
from istota.groups.rooms import group_room_access
from istota.rooms import policy as room_policy
from istota.rooms import scopes as room_scopes


@pytest.fixture
def conn(db_path):
    with db.get_db(db_path) as c:
        db.create_group(c, "fam", kind="family", display_name="Fam",
                        created_by="operator")
        db.add_group_member(c, "fam", "alice", role="owner", added_by="operator")
        db.add_group_member(c, "fam", "bob", added_by="operator")
        db.create_group(c, "club", kind="team", display_name="Club",
                        created_by="operator")
        db.add_group_member(c, "club", "alice", added_by="operator")
        yield c


def _room(conn, creator, *members, group=None, managed=False):
    token = db.create_web_chat_room(conn, creator, "Planning").token
    for user in members:
        db.add_web_room_member(conn, token, user)
    if group:
        db.set_room_group(conn, token, group)
    if managed:
        db.set_room_membership_source(conn, token, "group")
    return token


def _task(token, user="alice", **fields):
    return db.Task(id=7, status="running", user_id=user, source_type="web",
                   prompt="p", conversation_token=token, **fields)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestTheRelationship:
    def test_a_room_starts_with_its_own_roster(self, conn):
        token = _room(conn, "alice")
        assert db.get_room(conn, token).membership_source == "room"
        assert not db.is_group_owned_room(db.get_room(conn, token))

    def test_a_linked_room_becomes_group_owned_only_when_asked(self, conn):
        token = _room(conn, "alice", "bob", group="fam")
        assert db.get_room(conn, token).membership_source == "room"
        db.set_room_membership_source(conn, token, "group")
        room = db.get_room(conn, token)
        assert room.membership_source == "group"
        assert db.is_group_owned_room(room)

    def test_group_ownership_needs_a_live_linked_group(self, conn):
        unlinked = _room(conn, "alice")
        with pytest.raises(ValueError):
            db.set_room_membership_source(conn, unlinked, "group")
        archived = _room(conn, "alice", group="club")
        db.archive_group(conn, "club")
        with pytest.raises(ValueError):
            db.set_room_membership_source(conn, archived, "group")
        assert db.get_room(conn, unlinked).membership_source == "room"
        assert db.get_room(conn, archived).membership_source == "room"

    def test_an_unknown_source_is_refused(self, conn):
        token = _room(conn, "alice", group="fam")
        with pytest.raises(ValueError):
            db.set_room_membership_source(conn, token, "talk")

    def test_a_group_owned_room_never_becomes_ordinary(self, conn):
        token = _room(conn, "alice", "bob", group="fam", managed=True)
        with pytest.raises(ValueError):
            db.set_room_membership_source(conn, token, "room")
        db.set_room_membership_source(conn, token, "group")  # idempotent
        assert db.get_room(conn, token).membership_source == "group"

    def test_a_group_owned_rooms_link_cannot_change(self, conn):
        token = _room(conn, "alice", "bob", group="fam", managed=True)
        with pytest.raises(ValueError):
            db.set_room_group(conn, token, None)
        with pytest.raises(ValueError):
            db.set_room_group(conn, token, "club")
        db.set_room_group(conn, token, "fam")  # the same link is no change
        assert db.get_room(conn, token).group_id == "fam"

    def test_an_unexpected_stored_source_counts_as_group_owned(self, conn):
        # Application validation is the only constraint, so a row written
        # around it fails toward the stricter relationship.
        token = _room(conn, "alice")
        conn.execute("UPDATE rooms SET membership_source = 'other' WHERE token = ?",
                     (token,))
        assert db.is_group_owned_room(db.get_room(conn, token))

    def test_creation_requests_are_unique_per_group(self, conn):
        token = _room(conn, "alice", group="fam")
        sql = ("INSERT INTO group_room_requests "
               "(group_id, request_id, room_token, actor_id, request_hash) "
               "VALUES (?, ?, ?, ?, ?)")
        conn.execute(sql, ("fam", "req-1", token, "alice", "h"))
        conn.execute(sql, ("club", "req-1", token, "alice", "h"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, ("fam", "req-1", token, "alice", "h2"))


def _schema_of(conn):
    tables = {}
    for table in ("rooms", "group_room_requests"):
        tables[table] = [tuple(r) for r in conn.execute(f"PRAGMA table_info({table})")]
    indexes = sorted(
        (r["name"], r["sql"]) for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name IN ('rooms', 'group_room_requests')")
    )
    return tables, indexes


class TestTheMigration:
    def _old_database(self, tmp_path):
        old = tmp_path / "old.db"
        db.init_db(old)
        raw = sqlite3.connect(old)
        raw.execute("DROP INDEX idx_rooms_group_source")
        raw.execute("ALTER TABLE rooms DROP COLUMN membership_source")
        raw.execute("DROP TABLE group_room_requests")
        raw.execute("DELETE FROM _migration_state WHERE name = 'group_owned_rooms_v1'")
        raw.execute("INSERT INTO groups (group_id, created_by) VALUES ('fam', 'operator')")
        for user in ("alice", "bob"):
            raw.execute("INSERT INTO group_members (group_id, user_id, added_by) "
                        "VALUES ('fam', ?, 'operator')", (user,))
        # A linked room whose roster is exactly the group: still not converted.
        raw.execute("INSERT INTO rooms (token, user_id, name, origin, group_id) "
                    "VALUES ('linked', 'alice', 'Old', 'web', 'fam')")
        for user in ("alice", "bob"):
            raw.execute("INSERT INTO room_members (room_token, user_id) "
                        "VALUES ('linked', ?)", (user,))
        raw.commit()
        raw.close()
        return old

    def test_an_upgraded_database_matches_a_fresh_one(self, tmp_path):
        old = self._old_database(tmp_path)
        db.init_db(old)
        db.init_db(old)  # idempotent
        fresh = tmp_path / "fresh.db"
        db.init_db(fresh)
        declared = tmp_path / "declared.db"
        schema = (Path(db.__file__).parent.parent.parent / "schema.sql").read_text()
        raw = sqlite3.connect(declared)
        raw.executescript(schema)
        raw.close()
        with db.get_db(declared) as a, db.get_db(old) as b, db.get_db(fresh) as c:
            assert _schema_of(a) == _schema_of(b) == _schema_of(c)
            assert b.execute(
                "SELECT 1 FROM _migration_state WHERE name = 'group_owned_rooms_v1'"
            ).fetchone()

    def test_no_existing_linked_room_is_converted(self, tmp_path):
        old = self._old_database(tmp_path)
        db.init_db(old)
        with db.get_db(old) as conn:
            room = db.get_room(conn, "linked")
            assert room.group_id == "fam"
            assert room.membership_source == "room"
            assert conn.execute(
                "SELECT COUNT(*) FROM rooms WHERE membership_source != 'room'"
            ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# The access predicate
# ---------------------------------------------------------------------------


class TestGroupRoomAccess:
    def test_a_current_member_reaches_a_group_owned_room(self, conn):
        token = _room(conn, "alice", "bob", group="fam", managed=True)
        assert group_room_access(conn, token, "alice")
        assert group_room_access(conn, token, "bob")

    def test_someone_outside_the_group_does_not(self, conn):
        token = _room(conn, "alice", "bob", group="fam", managed=True)
        # Even with a room roster row: membership follows the group.
        db.add_web_room_member(conn, token, "carol")
        assert not group_room_access(conn, token, "carol")

    def test_a_removed_member_does_not(self, conn):
        token = _room(conn, "alice", "bob", group="fam", managed=True)
        db.end_group_membership(conn, "fam", "bob", ended_by="alice")
        assert not group_room_access(conn, token, "bob")
        assert group_room_access(conn, token, "alice")

    def test_an_archived_group_suspends_its_rooms(self, conn):
        token = _room(conn, "alice", "bob", group="fam", managed=True)
        db.archive_group(conn, "fam")
        assert not group_room_access(conn, token, "alice")

    def test_a_disabled_identity_does_not(self, conn):
        token = _room(conn, "alice", "bob", group="fam", managed=True)
        conn.execute("INSERT INTO web_auth_identities (user_id, email, disabled) "
                     "VALUES ('bob', 'bob@example.com', 1)")
        assert not group_room_access(conn, token, "bob")

    def test_a_broken_group_owned_row_fails_closed(self, conn):
        token = _room(conn, "alice", "bob", group="fam", managed=True)
        conn.execute("UPDATE rooms SET group_id = NULL WHERE token = ?", (token,))
        assert not group_room_access(conn, token, "alice")

    def test_an_unknown_room_is_refused(self, conn):
        assert not group_room_access(conn, "no-such-room", "alice")

    def test_a_linked_room_is_left_to_its_own_roster(self, conn):
        # The link filters memory; it never decides who is in the room.
        token = _room(conn, "alice", "carol", group="fam")
        assert group_room_access(conn, token, "carol")
        assert group_room_access(conn, _room(conn, "carol"), "carol")


# ---------------------------------------------------------------------------
# Who changes a group-owned room's settings
# ---------------------------------------------------------------------------


class TestOwnersGovernSettings:
    def test_an_owner_who_is_not_the_host_may_change_them(self, conn):
        # Bob created the room, so he is its accountable host; alice owns
        # the group, and that is what governs.
        token = _room(conn, "bob", "alice", group="fam", managed=True)
        assert room_policy.current_host(
            conn, room_policy.ensure_policy(conn, token)) == "bob"
        assert room_policy.settings_refusal(conn, token, "alice") is None
        assert room_policy.guest_reply_refusal(conn, token, "alice") is None
        assert room_policy.speech_mode_refusal(conn, token, "alice") is None

    def test_a_member_who_is_not_an_owner_may_not(self, conn):
        token = _room(conn, "bob", "alice", group="fam", managed=True)
        for refusal in (
            room_policy.settings_refusal(conn, token, "bob"),
            room_policy.guest_reply_refusal(conn, token, "bob"),
            room_policy.speech_mode_refusal(conn, token, "bob"),
        ):
            assert refusal and "owner" in refusal

    def test_a_singleton_group_room_is_still_owner_governed(self, conn):
        db.create_group(conn, "solo", kind="team", display_name="Solo",
                        created_by="operator")
        db.add_group_member(conn, "solo", "bob", added_by="operator")
        token = _room(conn, "bob", group="solo", managed=True)
        # One member, and not an owner: a private room's free hand does not
        # apply, because the room's history belongs to the group.
        assert room_policy.settings_refusal(conn, token, "bob")

    def test_a_former_owner_may_not(self, conn):
        token = _room(conn, "alice", "bob", group="fam", managed=True)
        db.end_group_membership(conn, "fam", "alice", ended_by="operator")
        assert room_policy.settings_refusal(conn, token, "alice")

    def test_an_archived_groups_owner_may_not(self, conn):
        token = _room(conn, "alice", "bob", group="fam", managed=True)
        db.archive_group(conn, "fam")
        assert room_policy.settings_refusal(conn, token, "alice")

    def test_nobody_relinks_or_unlinks_a_group_owned_room(self, conn):
        token = _room(conn, "alice", "bob", group="fam", managed=True)
        for group_id in (None, "club", "fam"):
            refusal = room_policy.group_link_refusal(conn, token, "alice", group_id)
            assert refusal and "group" in refusal

    def test_a_linked_room_keeps_its_host_rule(self, conn):
        # Compatibility: alice owns `fam`, bob hosts the linked room.
        token = _room(conn, "bob", "alice", group="fam")
        assert room_policy.settings_refusal(conn, token, "bob") is None
        assert "host" in room_policy.settings_refusal(conn, token, "alice")
        assert room_policy.group_link_refusal(conn, token, "bob", None) is None


# ---------------------------------------------------------------------------
# Ambient memory, independent of headcount
# ---------------------------------------------------------------------------


class TestAmbientMemory:
    def test_a_one_member_group_room_leaves_it_out(self, conn):
        db.end_group_membership(conn, "fam", "bob", ended_by="operator")
        token = _room(conn, "alice", group="fam", managed=True)
        assert not db.room_is_shared(conn, token)
        assert room_scopes.ambient_memory_off(conn, _task(token)) is True

    def test_a_room_that_shrank_to_one_leaves_it_out(self, conn):
        token = _room(conn, "alice", "bob", group="fam", managed=True)
        db.end_group_membership(conn, "fam", "bob", ended_by="alice")
        db.drop_web_room_member(conn, token, "bob")
        assert not db.room_is_shared(conn, token)
        assert room_scopes.ambient_memory_off(conn, _task(token)) is True

    def test_control_a_one_member_linked_room_loads_it(self, conn):
        token = _room(conn, "alice", group="fam")
        assert room_scopes.ambient_memory_off(conn, _task(token)) is False

    def test_the_singleton_keeps_the_members_reach(self, conn):
        token = _room(conn, "alice", group="fam", managed=True)
        assert room_scopes.withheld_for_task(
            conn, _task(token), skill_index={}) == frozenset()

    def test_the_singleton_loads_its_own_group_only(self, conn):
        db.end_group_membership(conn, "fam", "bob", ended_by="operator")
        token = _room(conn, "alice", group="fam", managed=True)
        assert room_scopes.task_group_ids(conn, _task(token)) == ["fam"]


# ---------------------------------------------------------------------------
# Transactional group rooms: creation, reconciliation and host succession,
# through `groups.service`, on committed state and real connections.
# ---------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    from istota.config import Config, UserConfig

    cfg = Config(db_path=tmp_path / "app.db", users={
        name: UserConfig() for name in ("alice", "bob", "carol", "dave", "erin")
    })
    db.init_db(cfg.db_path)
    return cfg


@pytest.fixture
def team(config):
    """`team`: alice owns it, bob is a member. Committed; no open transaction."""
    from istota.groups import service

    with db.get_db(config.db_path) as c:
        service.create_group(c, config, "team", actor_id="admin", is_admin=True,
                             expected_revision=0, owners=["alice"],
                             display_name="Team", kind="team")
        _change(c, config, "bob", role="member", acknowledge_history=True)
    return config


def _revision(conn, group_id="team"):
    return db.get_group(conn, group_id, include_archived=True)["revision"]


def _change(conn, config, user, *, actor="alice", **kwargs):
    from istota.groups import service

    return service.change_membership(
        conn, config, "team", user, actor_id=actor,
        expected_revision=_revision(conn), **kwargs)


def _leave(conn, config, user, **kwargs):
    return _change(conn, config, user, actor=user, role=None, **kwargs)


def _create(conn, actor="alice", name="Planning", request_id="req-00000001",
            group_id="team"):
    from istota.groups.rooms import create_group_room

    return create_group_room(conn, actor, group_id, name, request_id)


def _rooms_of(conn, group_id="team"):
    return conn.execute(
        "SELECT COUNT(*) FROM rooms WHERE group_id = ?", (group_id,),
    ).fetchone()[0]


def _handles(conn, token):
    return sorted(r["user_id"] for r in conn.execute(
        "SELECT user_id FROM web_chat_rooms WHERE token = ?", (token,)))


def _present(conn, token):
    return sorted(r["user_id"] for r in conn.execute(
        "SELECT user_id FROM room_participants WHERE room_token = ? "
        "AND surface = 'web' AND kind = 'principal' AND left_at IS NULL",
        (token,)))


def _host(conn, token):
    return room_policy.get_policy(conn, token).host_user_id


def _epochs(conn, token):
    return conn.execute(
        "SELECT COUNT(*) FROM room_epochs WHERE room_token = ?", (token,),
    ).fetchone()[0]


class TestCreateGroupRoom:
    def test_an_owner_makes_a_room_holding_the_whole_group(self, team):
        with db.get_db(team.db_path) as conn:
            result = _create(conn)
            assert not conn.in_transaction
            token = result["room"].token
            room = db.get_room(conn, token)
            assert db.is_group_owned_room(room) and room.group_id == "team"
            assert room.name == "Planning"
            assert db.list_room_members(conn, token) == ["alice", "bob"]
            assert _handles(conn, token) == ["alice", "bob"]
            assert _present(conn, token) == ["alice", "bob"]
            # The creator is the accountable host, recorded at creation.
            assert _host(conn, token) == "alice"
            assert result["created"] is True
            assert result["handle"].user_id == "alice"
            assert result["handle"].token == token
            # A new room has no history for anyone to be kept from.
            assert _epochs(conn, token) == 0

    def test_a_replay_returns_the_same_room(self, team):
        with db.get_db(team.db_path) as conn:
            first = _create(conn)
            again = _create(conn)
            assert again["created"] is False
            assert again["room"].token == first["room"].token
            assert again["handle"].id == first["handle"].id
            assert _rooms_of(conn) == 1

    @pytest.mark.parametrize("kwargs", [{"name": "Other"}, {"actor": "dave"}])
    def test_a_reused_request_id_with_other_parameters_is_a_conflict(
        self, team, kwargs,
    ):
        from istota.groups.policy import GroupError

        with db.get_db(team.db_path) as conn:
            _change(conn, team, "dave", role="owner", acknowledge_history=True)
            _create(conn)
            with pytest.raises(GroupError) as exc:
                _create(conn, **kwargs)
            assert exc.value.status_code == 409
            assert _rooms_of(conn) == 1

    def test_concurrent_retries_make_one_room(self, team):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        barrier = Barrier(2)

        def attempt(_):
            with db.get_db(team.db_path) as conn:
                barrier.wait()
                return _create(conn)["room"].token

        with ThreadPoolExecutor(max_workers=2) as pool:
            tokens = list(pool.map(attempt, range(2)))
        assert tokens[0] == tokens[1]
        with db.get_db(team.db_path) as conn:
            assert _rooms_of(conn) == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM group_room_requests").fetchone()[0] == 1

    @pytest.mark.parametrize("actor,status", [("bob", 403), ("carol", 404)])
    def test_only_an_owner_creates(self, team, actor, status):
        from istota.groups.policy import GroupError

        with db.get_db(team.db_path) as conn:
            with pytest.raises(GroupError) as exc:
                _create(conn, actor=actor)
            assert exc.value.status_code == status
            assert conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0
            assert not conn.in_transaction

    def test_an_ownerless_legacy_group_appoints_an_owner_first(self, team):
        from istota.groups.policy import GroupError

        with db.get_db(team.db_path) as conn:
            conn.execute("UPDATE group_members SET role = 'member' WHERE group_id = 'team'")
            conn.commit()
            with pytest.raises(GroupError) as exc:
                _create(conn)
            assert exc.value.status_code == 403

    def test_an_archived_group_creates_nothing(self, team):
        from istota.groups.policy import GroupError

        with db.get_db(team.db_path) as conn:
            db.archive_group(conn, "team")
            conn.commit()
            with pytest.raises(GroupError) as exc:
                _create(conn)
            assert exc.value.status_code == 404
            assert conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0

    @pytest.mark.parametrize("name,request_id", [
        ("", "req-00000001"), ("   ", "req-00000001"), ("x" * 201, "req-00000001"),
        (None, "req-00000001"), ("Planning", ""), ("Planning", "short"),
        ("Planning", "bad id with spaces"), ("Planning", None),
    ])
    def test_bad_input_is_refused(self, team, name, request_id):
        from istota.groups.policy import GroupError

        with db.get_db(team.db_path) as conn:
            with pytest.raises(GroupError) as exc:
                _create(conn, name=name, request_id=request_id)
            assert exc.value.status_code == 422
            assert conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0

    def test_the_disclosure_names_the_groups_rooms(self, team):
        from istota.groups import service

        with db.get_db(team.db_path) as conn:
            assert service.history_disclosure("team", conn)["rooms"] == []
            _create(conn)
            assert service.history_disclosure("team", conn)["rooms"] == ["Planning"]


class TestReconciliation:
    def test_a_new_member_joins_every_group_room_with_its_history(self, team):
        with db.get_db(team.db_path) as conn:
            token = _create(conn)["room"].token
            db.add_message(conn, token, role="user", body="before carol",
                           origin_surface="web", author_user_id="alice")
            conn.commit()
            _change(conn, team, "carol", role="member", acknowledge_history=True)
            assert not conn.in_transaction
            assert db.list_room_members(conn, token) == ["alice", "bob", "carol"]
            assert "carol" in _handles(conn, token)
            assert "carol" in _present(conn, token)
            # The group add acknowledged the history: no epoch splits it off.
            assert _epochs(conn, token) == 0
            assert group_room_access(conn, token, "carol")

    def test_a_removed_member_loses_membership_presence_and_handle(self, team):
        with db.get_db(team.db_path) as conn:
            token = _create(conn)["room"].token
            mid = db.add_message(conn, token, role="user", body="bob was here",
                                 origin_surface="web", author_user_id="bob")
            conn.commit()
            _change(conn, team, "bob", role=None)
            assert db.list_room_members(conn, token) == ["alice"]
            assert _handles(conn, token) == ["alice"]
            assert _present(conn, token) == ["alice"]
            # Their words stay in the transcript.
            assert conn.execute(
                "SELECT body FROM messages WHERE id = ?", (mid,),
            ).fetchone()[0] == "bob was here"

    def test_a_rejoining_member_gets_the_room_back(self, team):
        with db.get_db(team.db_path) as conn:
            token = _create(conn)["room"].token
            _leave(conn, team, "bob")
            _change(conn, team, "bob", role="member", acknowledge_history=True)
            assert db.list_room_members(conn, token) == ["alice", "bob"]
            assert _handles(conn, token) == ["alice", "bob"]
            assert _present(conn, token) == ["alice", "bob"]

    def test_a_stale_handle_and_an_outsider_are_dropped(self, team):
        with db.get_db(team.db_path) as conn:
            token = _create(conn)["room"].token
            # Written around the group: an outsider on the roster, and a
            # handle with no membership behind it.
            db.add_web_room_member(conn, token, "erin")
            db.ensure_web_chat_handle(conn, "erin", token, "Planning")
            db.ensure_web_chat_handle(conn, "dave", token, "Planning")
            conn.commit()
            _change(conn, team, "carol", role="member", acknowledge_history=True)
            assert db.list_room_members(conn, token) == ["alice", "bob", "carol"]
            assert _handles(conn, token) == ["alice", "bob", "carol"]
            assert _present(conn, token) == ["alice", "bob", "carol"]

    def test_a_linked_room_keeps_its_own_roster(self, team):
        with db.get_db(team.db_path) as conn:
            linked = db.create_web_chat_room(conn, "alice", "Linked").token
            db.add_web_room_member(conn, linked, "bob")
            db.add_web_room_member(conn, linked, "erin")
            db.set_room_group(conn, linked, "team")
            conn.commit()
            _change(conn, team, "bob", role=None)
            _change(conn, team, "carol", role="member", acknowledge_history=True)
            assert db.list_room_members(conn, linked) == ["alice", "bob", "erin"]

    def test_a_failed_reconciliation_rolls_back_the_membership_change(
        self, team, monkeypatch,
    ):
        from istota.groups.policy import GroupError

        with db.get_db(team.db_path) as conn:
            token = _create(conn)["room"].token
            before = _revision(conn)

            def broken(*args, **kwargs):
                raise sqlite3.OperationalError("disk I/O error")

            monkeypatch.setattr(db, "add_web_room_member", broken)
            with pytest.raises(GroupError) as exc:
                _change(conn, team, "carol", role="member", acknowledge_history=True)
            assert exc.value.status_code == 503
            assert exc.value.details.get("retryable") is True
            assert not conn.in_transaction
            assert _revision(conn) == before
            assert "carol" not in db.list_group_members(conn, "team")
            assert db.list_room_members(conn, token) == ["alice", "bob"]
            assert conn.execute(
                "SELECT COUNT(*) FROM group_events WHERE subject_user_id = 'carol'",
            ).fetchone()[0] == 0

    def test_an_archived_group_is_left_alone(self, team):
        from istota.groups.rooms import reconcile_group_room_members

        with db.get_db(team.db_path) as conn:
            token = _create(conn)["room"].token
            db.archive_group(conn, "team")
            reconcile_group_room_members(conn, "team")
            # Archiving suspends access; it does not empty the room.
            assert db.list_room_members(conn, token) == ["alice", "bob"]
            assert _handles(conn, token) == ["alice", "bob"]


class TestHostSuccession:
    def test_an_owner_takes_over_when_the_creator_leaves(self, team):
        with db.get_db(team.db_path) as conn:
            token = _create(conn)["room"].token
            # dave becomes an owner before carol: membership row order, not
            # name order, picks the successor. bob is no owner and is passed.
            _change(conn, team, "dave", role="owner", acknowledge_history=True)
            _change(conn, team, "carol", role="owner", acknowledge_history=True)
            _leave(conn, team, "alice")
            assert _host(conn, token) == "dave"
            assert room_policy.current_host(
                conn, room_policy.get_policy(conn, token)) == "dave"
            # The creator field stays history.
            assert db.get_room(conn, token).user_id == "alice"

    def test_a_promoted_member_succeeds_a_departing_last_owner(self, team):
        with db.get_db(team.db_path) as conn:
            token = _create(conn)["room"].token
            _change(conn, team, "alice", role=None, replacement_owner="bob")
            assert _host(conn, token) == "bob"

    def test_a_room_with_no_owner_left_is_hostless_until_one_is_appointed(self, team):
        from istota.groups import service

        with db.get_db(team.db_path) as conn:
            token = _create(conn)["room"].token
            # An operator demotes and removes around the last-owner rule.
            conn.execute("UPDATE group_members SET role = 'member' "
                         "WHERE group_id = 'team' AND user_id = 'alice'")
            conn.commit()
            service.change_membership(conn, team, "team", "alice", actor_id="op",
                                      is_operator=True, role=None,
                                      expected_revision=_revision(conn))
            assert _host(conn, token) is None
            service.change_membership(conn, team, "team", "bob", actor_id="op",
                                      is_operator=True, role="owner",
                                      expected_revision=_revision(conn))
            assert _host(conn, token) == "bob"

    def test_a_present_host_is_not_replaced(self, team):
        with db.get_db(team.db_path) as conn:
            token = _create(conn)["room"].token
            _change(conn, team, "carol", role="owner", acknowledge_history=True)
            _change(conn, team, "bob", role=None)
            assert _host(conn, token) == "alice"


class TestHostClaims:
    def _hostless(self, conn):
        token = _create(conn)["room"].token
        conn.execute("UPDATE room_policy SET host_user_id = NULL WHERE room_token = ?",
                     (token,))
        return token

    def test_a_member_who_is_no_owner_cannot_claim(self, team):
        with db.get_db(team.db_path) as conn:
            token = self._hostless(conn)
            assert room_policy.claim_host(conn, token, "bob") == "group_managed"
            assert _host(conn, token) is None
            assert room_policy.claim_host(conn, token, "alice") == "claimed"

    def test_an_outsider_on_the_roster_cannot_claim(self, team):
        with db.get_db(team.db_path) as conn:
            token = self._hostless(conn)
            db.add_web_room_member(conn, token, "erin")
            # The room-access resolver asks the group first, so a roster row
            # the group does not back is not membership at all.
            assert room_policy.claim_host(conn, token, "erin") == "not_a_member"
            assert room_policy.get_policy(conn, token).host_user_id is None

    def test_a_linked_room_member_still_claims(self, team):
        with db.get_db(team.db_path) as conn:
            linked = db.create_web_chat_room(conn, "alice", "Linked").token
            db.add_web_room_member(conn, linked, "bob")
            db.set_room_group(conn, linked, "team")
            room_policy.ensure_policy(conn, linked)
            room_policy.lose_host(conn, linked, "alice")
            assert room_policy.claim_host(conn, linked, "bob") == "claimed"

    def test_the_command_says_why(self, team):
        from istota import commands

        with db.get_db(team.db_path) as conn:
            token = self._hostless(conn)
            reply = commands._room_host(conn, token, "bob")
            assert "owner" in reply and "team" in reply
