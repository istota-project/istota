"""A room linked to a group (multiplayer Stage 27): `rooms.group_id`.

The link names the one group whose material the room carries. It narrows,
never widens: `room_scopes.task_group_ids` still applies the audience rule
(every reader a current member, no guest, no agent, not a guest's turn, not a
`mixed` turn), and in a linked room the linked group is the only candidate.

Who sets it: the room's host (the one member of a private room), and only to a
group they are a current member of, over `!room group <id>|none` and the web
room settings, which ask one rule.
"""

import asyncio
import sqlite3

import pytest

from istota import commands, db, room_policy
from istota.config import Config, UserConfig
from istota.room_scopes import task_group_ids


@pytest.fixture
def conn(db_path):
    with db.get_db(db_path) as c:
        db.create_group(c, "fam", kind="family", display_name="Fam",
                        created_by="operator")
        db.add_group_member(c, "fam", "alice", added_by="operator")
        db.add_group_member(c, "fam", "bob", added_by="operator")
        # A second group covering the same pair, so "only the linked one"
        # is distinguishable from "every covering group".
        db.create_group(c, "club", kind="team", display_name="Club",
                        created_by="operator")
        db.add_group_member(c, "club", "alice", added_by="operator")
        db.add_group_member(c, "club", "bob", added_by="operator")
        db.create_group(c, "work", kind="team", display_name="Work",
                        created_by="operator")
        db.add_group_member(c, "work", "alice", added_by="operator")
        db.create_group(c, "bobs", kind="team", display_name="Bobs",
                        created_by="operator")
        db.add_group_member(c, "bobs", "bob", added_by="operator")
        yield c


def _task(token, user="alice", **fields):
    return db.Task(id=7, status="running", user_id=user, source_type="web",
                   prompt="p", conversation_token=token, **fields)


def _room(conn, token, *members):
    db.register_room(conn, token, members[0], origin="web", name=token)
    for user in members[1:]:
        db.add_web_room_member(conn, token, user)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestTheColumn:
    def test_a_room_starts_unlinked_and_round_trips(self, conn):
        _room(conn, "r1", "alice")
        assert db.get_room(conn, "r1").group_id is None
        db.set_room_group(conn, "r1", "fam")
        assert db.get_room(conn, "r1").group_id == "fam"
        db.set_room_group(conn, "r1", None)
        assert db.get_room(conn, "r1").group_id is None

    def test_an_upgraded_database_matches_a_fresh_one(self, tmp_path):
        fresh = tmp_path / "fresh.db"
        db.init_db(fresh)
        old = tmp_path / "old.db"
        db.init_db(old)
        raw = sqlite3.connect(old)
        raw.execute("ALTER TABLE rooms DROP COLUMN group_id")
        raw.execute("DELETE FROM _migration_state WHERE name = 'room_group_v1'")
        raw.execute("INSERT INTO rooms (token, user_id, name, origin) "
                    "VALUES ('old-room', 'alice', 'Old', 'web')")
        raw.commit()
        raw.row_factory = sqlite3.Row
        db._run_migrations(raw)
        raw.commit()
        assert raw.execute(
            "SELECT 1 FROM _migration_state WHERE name = 'room_group_v1'"
        ).fetchone()
        raw.close()
        declared = tmp_path / "declared.db"
        from pathlib import Path
        schema = (Path(db.__file__).parent.parent.parent / "schema.sql").read_text()
        raw = sqlite3.connect(declared)
        raw.executescript(schema)
        raw.close()
        with db.get_db(declared) as a, db.get_db(old) as b, db.get_db(fresh) as c:
            info = lambda conn: [tuple(r) for r in conn.execute(  # noqa: E731
                "PRAGMA table_info(rooms)")]
            assert info(a) == info(b) == info(c)
            assert db.get_room(b, "old-room").group_id is None


# ---------------------------------------------------------------------------
# What a linked room loads
# ---------------------------------------------------------------------------


class TestTheLinkedRoomLoads:
    def test_an_unlinked_shared_room_loads_every_covering_group(self, conn):
        _room(conn, "r2", "alice", "bob")
        assert task_group_ids(conn, _task("r2")) == ["club", "fam"]

    def test_a_linked_shared_room_loads_only_its_group(self, conn):
        _room(conn, "r2", "alice", "bob")
        db.set_room_group(conn, "r2", "fam")
        assert task_group_ids(conn, _task("r2")) == ["fam"]
        assert task_group_ids(conn, _task("r2", user="bob")) == ["fam"]

    def test_a_linked_private_room_loads_only_its_group(self, conn):
        _room(conn, "r1", "alice")
        db.set_room_group(conn, "r1", "work")
        assert task_group_ids(conn, _task("r1")) == ["work"]

    def test_the_link_never_widens_past_the_audience(self, conn):
        # Carol is not in `fam`: the link does not make her a member.
        _room(conn, "r3", "alice", "bob", "carol")
        db.set_room_group(conn, "r3", "fam")
        assert task_group_ids(conn, _task("r3")) == []

    def test_a_reader_outside_the_linked_group_loads_nothing_else(self, conn):
        # `work` is linked but Bob is not in it; `fam` and `club` would cover
        # the pair, and the link still rules them out.
        _room(conn, "r2", "alice", "bob")
        db.set_room_group(conn, "r2", "work")
        assert task_group_ids(conn, _task("r2")) == []

    def test_a_link_the_principal_is_not_a_member_of_loads_nothing(self, conn):
        _room(conn, "r1", "bob")
        db.set_room_group(conn, "r1", "work")
        assert task_group_ids(conn, _task("r1", user="bob")) == []

    def test_an_archived_or_left_group_loads_nothing(self, conn):
        _room(conn, "r1", "alice")
        db.set_room_group(conn, "r1", "work")
        db.archive_group(conn, "work")
        assert task_group_ids(conn, _task("r1")) == []
        _room(conn, "r4", "alice")
        db.set_room_group(conn, "r4", "fam")
        db.end_group_membership(conn, "fam", "alice", ended_by="operator")
        assert task_group_ids(conn, _task("r4")) == []

    def test_a_guest_turn_or_a_mixed_turn_loads_nothing(self, conn):
        _room(conn, "r2", "alice", "bob")
        db.set_room_group(conn, "r2", "fam")
        assert task_group_ids(conn, _task("r2", guest_participant_id=3)) == []
        assert task_group_ids(conn, _task("r2", audience="mixed")) == []

    def test_a_present_guest_loads_nothing(self, conn):
        _room(conn, "r2", "alice", "bob")
        db.set_room_group(conn, "r2", "fam")
        db.upsert_room_participant(conn, room_token="r2", surface="talk",
                                   surface_ref="guests/max", kind="guest")
        assert task_group_ids(conn, _task("r2")) == []

    def test_a_surface_ref_reads_the_canonical_rooms_link(self, conn):
        _room(conn, "web-r5", "alice", "bob")
        db.add_room_binding(conn, "web-r5", "talk", "talkref5")
        db.set_room_group(conn, "web-r5", "fam")
        assert task_group_ids(conn, _task("talkref5")) == ["fam"]


# ---------------------------------------------------------------------------
# Who may set it
# ---------------------------------------------------------------------------


class TestWhoSetsTheLink:
    def test_the_one_member_of_a_private_room_links_it_to_their_group(self, conn):
        _room(conn, "r1", "alice")
        assert room_policy.group_link_refusal(conn, "r1", "alice", "work") is None

    def test_the_host_of_a_shared_room_links_it(self, conn):
        _room(conn, "r2", "alice", "bob")
        assert room_policy.group_link_refusal(conn, "r2", "alice", "fam") is None

    def test_another_member_is_refused(self, conn):
        _room(conn, "r2", "alice", "bob")
        refusal = room_policy.group_link_refusal(conn, "r2", "bob", "fam")
        assert refusal and "host" in refusal
        assert room_policy.group_link_refusal(conn, "r2", "bob", None)

    def test_a_non_member_of_the_room_is_refused(self, conn):
        _room(conn, "r1", "alice")
        assert room_policy.group_link_refusal(conn, "r1", "carol", None)

    @pytest.mark.parametrize("group_id", ["bobs", "nosuch", "../fam", ""])
    def test_a_group_the_host_is_not_in_reads_the_same_as_none_existing(
            self, conn, group_id):
        _room(conn, "r1", "alice")
        refusal = room_policy.group_link_refusal(conn, "r1", "alice", group_id)
        assert refusal == f"You are not a member of group '{group_id}'."

    def test_an_archived_group_is_refused(self, conn):
        _room(conn, "r1", "alice")
        db.archive_group(conn, "work")
        assert room_policy.group_link_refusal(conn, "r1", "alice", "work")

    def test_a_side_room_is_refused(self, conn):
        _room(conn, "r2", "alice", "bob")
        side = db.ensure_side_room(conn, "r2", "alice")
        assert room_policy.group_link_refusal(conn, side.token, "alice", "fam")

    def test_clearing_needs_only_the_host(self, conn):
        _room(conn, "r1", "alice")
        assert room_policy.group_link_refusal(conn, "r1", "alice", None) is None


# ---------------------------------------------------------------------------
# !room group
# ---------------------------------------------------------------------------


@pytest.fixture
def config(db_path):
    return Config(
        db_path=db_path,
        users={"alice": UserConfig(display_name="Alice"),
               "bob": UserConfig(display_name="Bob")},
    )


def _say(config, conn, user, token, text):
    return asyncio.run(commands.dispatch(
        config, user, token, text, surface="web", conn=conn)).text


class TestTheCommand:
    def test_show_set_and_clear(self, config, conn):
        _room(conn, "r2", "alice", "bob")
        assert "not linked" in _say(config, conn, "alice", "r2", "!room group")
        out = _say(config, conn, "alice", "r2", "!room group fam")
        assert "fam" in out
        assert db.get_room(conn, "r2").group_id == "fam"
        assert "fam" in _say(config, conn, "bob", "r2", "!room group")
        assert "no longer" in _say(config, conn, "alice", "r2", "!room group none")
        assert db.get_room(conn, "r2").group_id is None

    def test_a_refusal_changes_nothing(self, config, conn):
        _room(conn, "r2", "alice", "bob")
        out = _say(config, conn, "bob", "r2", "!room group fam")
        assert "host" in out
        assert db.get_room(conn, "r2").group_id is None
        out = _say(config, conn, "alice", "r2", "!room group bobs")
        assert out == "You are not a member of group 'bobs'."
        assert db.get_room(conn, "r2").group_id is None

    def test_a_side_room_says_it_has_no_link(self, config, conn):
        _room(conn, "r2", "alice", "bob")
        side = db.ensure_side_room(conn, "r2", "alice")
        out = _say(config, conn, "alice", side.token, "!room group")
        assert "side room" in out and "!room group <id>" not in out
