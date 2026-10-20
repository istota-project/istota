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
