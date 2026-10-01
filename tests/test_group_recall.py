"""Which groups a task's material may come from: `room_scopes.task_group_ids`.

One resolved set per task decides the `## Group memory` block, the
`Groups/<id>` sandbox binds and (D21) whether `kv --group` may touch a group.
A group is in it only where everyone who reads the task's answer is a current
member: every group of the user off a room, only the groups covering the whole
audience in one, and nothing on a guest's turn, a `mixed` turn, or a room whose
audience cannot be read.
"""

import logging

import pytest

from istota import db, executor
from istota.config import Config
from istota.room_scopes import task_group_ids


@pytest.fixture
def conn(db_path):
    with db.get_db(db_path) as c:
        db.create_group(c, "fam", kind="family", display_name="Fam",
                        created_by="operator")
        db.add_group_member(c, "fam", "alice", added_by="operator")
        db.add_group_member(c, "fam", "bob", added_by="operator")
        db.create_group(c, "work", kind="team", display_name="Work",
                        created_by="operator")
        db.add_group_member(c, "work", "alice", added_by="operator")
        db.create_group(c, "bobs", kind="team", display_name="Bobs",
                        created_by="operator")
        db.add_group_member(c, "bobs", "bob", added_by="operator")
        yield c


def _task(token=None, user="alice", **fields):
    return db.Task(id=7, status="running", user_id=user, source_type="talk",
                   prompt="p", conversation_token=token, **fields)


def _room(conn, token, *members):
    db.register_room(conn, token, members[0], origin="web", name=token)
    for user in members[1:]:
        db.add_web_room_member(conn, token, user)


class TestOffARoom:
    def test_every_current_group_of_the_user(self, conn):
        assert task_group_ids(conn, _task()) == ["fam", "work"]

    def test_no_groups(self, conn):
        assert task_group_ids(conn, _task(user="carol")) == []

    def test_an_archived_group_is_left_out(self, conn):
        db.archive_group(conn, "work")
        assert task_group_ids(conn, _task()) == ["fam"]

    def test_an_ended_membership_is_left_out(self, conn):
        db.end_group_membership(conn, "work", "alice", ended_by="operator")
        assert task_group_ids(conn, _task()) == ["fam"]


class TestInARoom:
    def test_a_one_to_one_room_loads_every_group(self, conn):
        _room(conn, "r1", "alice")
        assert task_group_ids(conn, _task("r1")) == ["fam", "work"]

    def test_a_shared_room_loads_only_groups_covering_every_member(self, conn):
        _room(conn, "r2", "alice", "bob")
        assert task_group_ids(conn, _task("r2")) == ["fam"]

    def test_a_room_with_a_non_member_loads_nothing(self, conn):
        _room(conn, "r3", "alice", "bob", "carol")
        assert task_group_ids(conn, _task("r3")) == []

    def test_a_principal_participant_counts_as_a_reader(self, conn):
        _room(conn, "r4", "alice")
        db.upsert_room_participant(conn, room_token="r4", surface="talk",
                                   surface_ref="carol", kind="principal",
                                   user_id="carol")
        assert task_group_ids(conn, _task("r4")) == []

    def test_an_unregistered_room_loads_nothing_and_says_why(self, conn, caplog):
        with caplog.at_level(logging.DEBUG, logger="istota.room_scopes"):
            assert task_group_ids(conn, _task("nosuch")) == []
        assert any("group_memory_skipped reason=room_members_unknown" in r.getMessage()
                   and "nosuch" in r.getMessage() for r in caplog.records)

    def test_a_surface_ref_resolves_to_its_room(self, conn):
        _room(conn, "web-r5", "alice", "bob", "carol")
        db.add_room_binding(conn, "web-r5", "talk", "talkref5")
        assert task_group_ids(conn, _task("talkref5")) == []

    def test_a_group_chat_flag_with_no_recorded_audience_loads_nothing(self, conn):
        # The surface's roster says several people read this; the registry has
        # recorded only the speaker. Unknown readers are not group members.
        _room(conn, "r6", "alice")
        assert task_group_ids(conn, _task("r6", is_group_chat=True)) == []


class TestTheRoomModel:
    def test_a_guest_turn_loads_nothing(self, conn):
        assert task_group_ids(conn, _task(guest_participant_id=3)) == []

    def test_a_guest_turn_in_a_member_only_room_loads_nothing(self, conn):
        _room(conn, "r7", "alice")
        assert task_group_ids(conn, _task("r7", guest_participant_id=3)) == []

    def test_a_turn_stored_as_mixed_loads_nothing(self, conn):
        _room(conn, "r8", "alice")
        assert task_group_ids(conn, _task("r8", audience="mixed")) == []

    def test_a_present_guest_loads_nothing(self, conn):
        _room(conn, "r9", "alice")
        db.upsert_room_participant(conn, room_token="r9", surface="talk",
                                   surface_ref="guests/x", kind="guest")
        assert task_group_ids(conn, _task("r9")) == []

    def test_a_guest_carrying_a_members_user_id_still_loads_nothing(self, conn):
        # Kind decides, not the id: a guest row is input from outside the
        # room's principals even where a mapping put a member's id on it.
        _room(conn, "r12", "alice")
        db.upsert_room_participant(conn, room_token="r12", surface="talk",
                                   surface_ref="guests/b", kind="guest",
                                   user_id="bob")
        assert task_group_ids(conn, _task("r12")) == []

    def test_a_guest_who_left_no_longer_counts(self, conn):
        _room(conn, "r10", "alice")
        db.upsert_room_participant(conn, room_token="r10", surface="talk",
                                   surface_ref="guests/x", kind="guest")
        conn.execute("UPDATE room_participants SET left_at = datetime('now') "
                     "WHERE room_token = 'r10'")
        assert task_group_ids(conn, _task("r10")) == ["fam", "work"]

    def test_a_present_agent_loads_nothing(self, conn):
        # Another bot reads the room too, and it is not a group member.
        _room(conn, "r11", "alice")
        db.upsert_room_participant(conn, room_token="r11", surface="whatsapp",
                                   surface_ref="bot@x", kind="agent")
        assert task_group_ids(conn, _task("r11")) == []


class TestTheExecutorSeam:
    def test_a_database_error_resolves_to_nothing(self, db_path, monkeypatch):
        config = Config(db_path=db_path)
        monkeypatch.setattr(db, "list_user_groups",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        with db.get_db(db_path) as c:
            assert executor._resolve_task_groups(config, _task(), c) == []

    def test_opens_its_own_connection(self, conn, db_path):
        conn.commit()
        config = Config(db_path=db_path)
        assert executor._resolve_task_groups(config, _task(), None) == ["fam", "work"]

    def test_a_missing_database_is_not_created(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db")
        assert executor._resolve_task_groups(config, _task(), None) == []
        assert not (tmp_path / "absent.db").exists()
