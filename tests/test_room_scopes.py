"""The shared-room scope vocabulary, who withholds what, and the ``safe`` markings."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from istota import db
from istota.rooms import scopes as room_scopes
from istota.skills._loader import load_skill_index
from istota.skills._types import SkillMeta


def _index(**shared_room: str) -> dict[str, SkillMeta]:
    return {
        name: SkillMeta(name=name, description="x", shared_room=value)
        for name, value in shared_room.items()
    }


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    with db.get_db(path) as c:
        yield c


def _shared_room(conn) -> str:
    room = db.create_web_chat_room(conn, "alice", "Family")
    db.add_room_member(conn, room.token, "bob")
    return room.token


class TestTheVocabulary:
    def test_private_skills_then_the_synthetic_scopes(self):
        index = _index(calendar="private", web="safe", money="private")
        assert room_scopes.scope_names(index) == ["calendar", "money", "files", "memory"]

    def test_all_scopes_is_every_scope(self):
        index = _index(calendar="private", web="safe")
        assert room_scopes.all_scopes(index) == {"calendar", "files", "memory"}


class TestWhatATaskIsWithheld:
    """ISSUE-576: a turn runs with its sender's reach."""

    INDEX = _index(calendar="private", sensitive_actions="safe")

    def test_a_members_turn_in_a_shared_room_withholds_nothing(self, make_task):
        task = make_task(user_id="alice", conversation_token="room-1", is_group_chat=True,
                         source_type="talk")
        assert room_scopes.withheld_for_task(None, task, skill_index=self.INDEX) == frozenset()

    def test_a_members_turn_with_a_guest_present_withholds_nothing(self, make_task):
        task = make_task(user_id="alice", conversation_token="room-1", audience="mixed",
                         source_type="web")
        assert room_scopes.withheld_for_task(None, task, skill_index=self.INDEX) == frozenset()

    def test_a_guests_turn_withholds_every_scope(self, make_task):
        task = make_task(user_id="alice", conversation_token="room-1", guest_participant_id=7)
        assert room_scopes.withheld_for_task(None, task, skill_index=self.INDEX) == {
            "calendar", "files", "memory",
        }


def _job(conn, user_id: str, token: str) -> int:
    cur = conn.execute(
        "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, "
        "conversation_token) VALUES (?, 'digest', '0 9 * * *', 'p', ?)",
        (user_id, token),
    )
    return cur.lastrowid


class TestATaskNobodyAskedInTheRoom:
    """A subtask or a CLI task whose conversation is a shared room lands its
    answer there with no member asking, so it runs at room-safe reach."""

    INDEX = _index(calendar="private")

    @pytest.mark.parametrize("source_type", ["subtask", "cli", "heartbeat"])
    def test_a_shared_room_withholds_every_scope(self, conn, make_task, source_type):
        task = make_task(user_id="alice", conversation_token=_shared_room(conn),
                         source_type=source_type)
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == {
            "calendar", "files", "memory",
        }

    def test_control_a_members_turn_there_withholds_nothing(self, conn, make_task):
        task = make_task(user_id="alice", conversation_token=_shared_room(conn),
                         source_type="web")
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == frozenset()

    def test_control_a_private_room_withholds_nothing(self, conn, make_task):
        room = db.create_web_chat_room(conn, "alice", "Mine")
        task = make_task(user_id="alice", conversation_token=room.token,
                         source_type="scheduled")
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == frozenset()

    def test_an_unreadable_audience_withholds_everything(self, make_task):
        broken = sqlite3.connect(":memory:")
        task = make_task(user_id="alice", conversation_token="t", source_type="scheduled",
                         scheduled_job_id=1)
        assert room_scopes.withheld_for_task(broken, task, skill_index=self.INDEX) == {
            "calendar", "files", "memory",
        }


class TestAMembersOwnScheduleInASharedRoom:
    """ISSUE-594: a member's own CRON.md job or briefing that targets a shared
    room they are in was asked by that member, in advance and for that room,
    so it runs at their reach like their own turn."""

    INDEX = _index(calendar="private")
    ALL = {"calendar", "files", "memory"}

    def test_the_members_own_cron_job_withholds_nothing(self, conn, make_task):
        token = _shared_room(conn)
        task = make_task(user_id="alice", conversation_token=token,
                         source_type="scheduled", scheduled_job_id=_job(conn, "alice", token))
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == frozenset()

    def test_a_co_members_job_is_the_owner_too(self, conn, make_task):
        token = _shared_room(conn)
        task = make_task(user_id="bob", conversation_token=token,
                         source_type="scheduled", scheduled_job_id=_job(conn, "bob", token))
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == frozenset()

    def test_the_members_own_briefing_withholds_nothing(self, conn, make_task):
        task = make_task(user_id="alice", conversation_token=_shared_room(conn),
                         source_type="briefing", briefing_name="morning")
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == frozenset()

    def test_a_scheduled_task_with_no_job_withholds_every_scope(self, conn, make_task):
        task = make_task(user_id="alice", conversation_token=_shared_room(conn),
                         source_type="scheduled")
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == self.ALL

    def test_a_job_naming_another_user_withholds_every_scope(self, conn, make_task):
        token = _shared_room(conn)
        task = make_task(user_id="alice", conversation_token=token,
                         source_type="scheduled", scheduled_job_id=_job(conn, "bob", token))
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == self.ALL

    def test_a_briefing_with_no_name_withholds_every_scope(self, conn, make_task):
        task = make_task(user_id="alice", conversation_token=_shared_room(conn),
                         source_type="briefing")
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == self.ALL

    def test_an_owner_who_left_a_talk_room_withholds_every_scope(self, conn, make_task):
        token = _shared_room(conn)
        db.upsert_room_participant(conn, room_token=token, surface="talk",
                                   surface_ref="alice", kind="principal", user_id="alice")
        conn.execute("UPDATE room_participants SET left_at = datetime('now') "
                     "WHERE room_token = ? AND user_id = 'alice'", (token,))
        task = make_task(user_id="alice", conversation_token=token,
                         source_type="scheduled", scheduled_job_id=_job(conn, "alice", token))
        assert db.is_room_member(conn, token, "alice")
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == self.ALL

    def test_control_an_owner_present_on_talk_withholds_nothing(self, conn, make_task):
        token = _shared_room(conn)
        db.upsert_room_participant(conn, room_token=token, surface="talk",
                                   surface_ref="alice", kind="principal", user_id="alice")
        task = make_task(user_id="alice", conversation_token=token,
                         source_type="scheduled", scheduled_job_id=_job(conn, "alice", token))
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == frozenset()

    def test_an_owner_who_is_not_a_member_withholds_every_scope(self, conn, make_task):
        room = db.create_web_chat_room(conn, "carol", "Family")
        db.add_room_member(conn, room.token, "bob")
        task = make_task(user_id="alice", conversation_token=room.token,
                         source_type="scheduled",
                         scheduled_job_id=_job(conn, "alice", room.token))
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == self.ALL

    def test_a_task_flagged_group_chat_withholds_every_scope(self, conn, make_task):
        token = _shared_room(conn)
        task = make_task(user_id="alice", conversation_token=token, is_group_chat=True,
                         source_type="scheduled", scheduled_job_id=_job(conn, "alice", token))
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == self.ALL


class TestATurnWrittenBySomeoneElse:
    """An outside correspondent's reply continuing a shared room's email
    thread runs as the member it was routed to, but no member asked it."""

    INDEX = _index(calendar="private")

    def _turn(self, conn, token, **author):
        tid = db.create_task(conn, user_id="alice", source_type="email",
                             prompt="p", conversation_token=token)
        db.add_message(conn, token, role="user", body="p", origin_surface="email",
                       task_id=tid, **author)
        return db.get_task(conn, tid)

    def test_an_outside_sender_withholds_every_scope(self, conn):
        task = self._turn(conn, _shared_room(conn), author_label="carol@example.com")
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == {
            "calendar", "files", "memory",
        }

    def test_control_the_members_own_mail_withholds_nothing(self, conn):
        task = self._turn(conn, _shared_room(conn), author_user_id="alice")
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == frozenset()

    def test_control_a_private_room_withholds_nothing(self, conn):
        room = db.create_web_chat_room(conn, "alice", "Mine")
        task = self._turn(conn, room.token, author_label="carol@example.com")
        assert room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX) == frozenset()


class TestAmbientMemory:
    """The one thing a member's turn in a shared room loses."""

    def test_a_shared_room_leaves_it_out(self, conn, make_task):
        task = make_task(user_id="alice", conversation_token=_shared_room(conn))
        assert room_scopes.ambient_memory_off(conn, task) is True

    def test_a_surface_ref_is_read_against_its_room(self, conn, make_task):
        token = _shared_room(conn)
        db.add_room_binding(conn, token, "talk", "talk-ref-1")
        task = make_task(user_id="alice", conversation_token="talk-ref-1")
        assert room_scopes.ambient_memory_off(conn, task) is True

    def test_control_a_private_room_keeps_it(self, conn, make_task):
        room = db.create_web_chat_room(conn, "alice", "Mine")
        task = make_task(user_id="alice", conversation_token=room.token)
        assert room_scopes.ambient_memory_off(conn, task) is False

    def test_no_conversation_keeps_it(self, conn, make_task):
        assert room_scopes.ambient_memory_off(conn, make_task(user_id="alice")) is False

    @pytest.mark.parametrize("fields", [
        {"is_group_chat": True}, {"audience": "mixed"}, {"guest_participant_id": 3},
    ])
    def test_the_task_row_alone_can_say_shared(self, fields, make_task):
        # A Talk group's first turn, a stored mixed audience, a guest's turn:
        # no database is needed, and none is consulted.
        task = make_task(user_id="alice", conversation_token="t", **fields)
        assert room_scopes.ambient_memory_off(None, task) is True

    def test_an_unreadable_audience_leaves_it_out(self, make_task):
        broken = sqlite3.connect(":memory:")
        task = make_task(user_id="alice", conversation_token="t")
        assert room_scopes.ambient_memory_off(broken, task) is True


class TestTheManifestField:
    def _load(self, tmp_path: Path, value: str | None) -> SkillMeta:
        skill = tmp_path / "skills" / "thing"
        skill.mkdir(parents=True)
        extra = f"shared_room: {value}\n" if value is not None else ""
        (skill / "skill.md").write_text(
            f"---\nname: thing\ndescription: x\n{extra}---\nbody\n", encoding="utf-8",
        )
        empty = tmp_path / "bundled"
        empty.mkdir()
        return load_skill_index(tmp_path / "skills", bundled_dir=empty)["thing"]

    def test_absent_is_private(self, tmp_path):
        assert self._load(tmp_path, None).shared_room == "private"

    def test_safe_is_read(self, tmp_path):
        assert self._load(tmp_path, "safe").shared_room == "safe"

    def test_an_unknown_value_is_private(self, tmp_path):
        assert self._load(tmp_path, "yes").shared_room == "private"


def test_the_shipped_safe_set_is_exactly_the_reviewed_one(tmp_path):
    # Widening this is a disclosure decision: each entry has to be shown to
    # read nothing user-specific. `browse` and `markets` were on the draft's
    # list and are not here, since both drive the user's own browser profile.
    # `room` (multiplayer Stage 10) reads nothing: `whisper` writes only to the
    # principal's own side room, and `post` works only from a side room, whose
    # task is private and is held for the member's approval besides.
    empty = tmp_path / "overrides"
    empty.mkdir()
    index = load_skill_index(empty)
    safe = {name for name, meta in index.items() if meta.shared_room == "safe"}
    assert safe == {"sensitive_actions", "untrusted_input", "room"}
