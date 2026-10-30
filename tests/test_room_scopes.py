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


def test_briefing_research_withholds_safe_mutations_as_well_as_private_skills(make_task):
    task = make_task(user_id="__briefings__", source_type="group_briefing_research")
    index = _index(browse="private", skills="safe", untrusted_input="safe",
                   calendar="private", developer="private", tasks="safe",
                   email="private")

    assert room_scopes.withheld_for_task(None, task, skill_index=index) == {
        "calendar", "developer", "tasks", "email", "files", "memory",
    }


def test_briefing_research_has_no_ambient_memory_or_groups(conn, make_task):
    task = make_task(user_id="__briefings__", source_type="group_briefing_research")
    assert room_scopes.ambient_memory_off(conn, task)
    assert room_scopes.task_group_ids(conn, task) == []


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


def _email_thread_room(conn) -> str:
    """A thread room as the poller leaves it: the host the one member, two
    correspondents on the thread, bound to the root Message-ID."""
    room = db.register_bound_room(conn, "alice", origin="email", name="Dinner",
                                  surface="email", surface_ref="<root@example.com>")
    for address in ("carol@example.com", "dave@example.com"):
        db.upsert_room_participant(conn, room_token=room.token, surface="email",
                                   surface_ref=address, kind="guest")
    return room.token


#: Every default of `CORRESPONDENT_DEFAULT_SCOPES` plus one private skill nobody
#: listed and one safe skill, which is never a scope.
_CORRESPONDENT_INDEX = _index(
    calendar="private", email="private", skills="private", transcribe="private",
    whisper="private", markets="private", untrusted_input="safe",
)
_WITHHELD_FROM_A_CORRESPONDENT = {"calendar", "files", "memory"}


def _email_turn(conn, token, **author):
    tid = db.create_task(conn, user_id="alice", source_type="email",
                         prompt="p", conversation_token=token, is_group_chat=True,
                         audience="mixed")
    db.add_message(conn, token, role="user", body="p", origin_surface="email",
                   task_id=tid, **author)
    return db.get_task(conn, tid)


def _roomless_mail(conn, sender, routing_method="plus_address"):
    """An approved held mail no room admitted: no token and no stored row."""
    tid = db.create_task(conn, user_id="alice", source_type="email", prompt="p")
    db.mark_email_processed(conn, f"uid-{tid}", sender, user_id="alice", task_id=tid,
                            routing_method=routing_method)
    return db.get_task(conn, tid)


class TestAnEmailThreadRoom:
    """A correspondent's turn runs as the host and withholds all but the
    allowlist; the host's own tasks on the thread still withhold nothing."""

    INDEX = _CORRESPONDENT_INDEX

    def _withheld(self, conn, task):
        return room_scopes.withheld_for_task(conn, task, skill_index=self.INDEX)

    def test_it_is_recognised_by_its_email_binding(self, conn):
        assert room_scopes.is_email_thread_room(conn, _email_thread_room(conn))
        assert not room_scopes.is_email_thread_room(conn, _shared_room(conn))
        assert not room_scopes.is_email_thread_room(conn, "no-such-room")

    def test_a_correspondents_turn_withholds_all_but_the_allowlist(self, conn):
        task = _email_turn(conn, _email_thread_room(conn), author_label="carol@example.com")
        assert self._withheld(conn, task) == _WITHHELD_FROM_A_CORRESPONDENT

    def test_a_skill_added_later_starts_withheld(self, conn):
        task = _email_turn(conn, _email_thread_room(conn), author_label="carol@example.com")
        index = {**self.INDEX, **_index(brand_new="private")}
        assert "brand_new" in room_scopes.withheld_for_task(conn, task, skill_index=index)

    def test_its_ambient_memory_is_not_switched_off_by_the_room(self, conn):
        # The room rule stays: `executor` splits USER.md from recall by posture.
        task = _email_turn(conn, _email_thread_room(conn), author_label="carol@example.com")
        assert room_scopes.ambient_memory_off(conn, task) is False

    def test_the_hosts_own_mail_withholds_nothing(self, conn):
        task = _email_turn(conn, _email_thread_room(conn), author_user_id="alice")
        assert self._withheld(conn, task) == frozenset()

    @pytest.mark.parametrize("source_type", ["cli", "subtask"])
    def test_the_hosts_own_task_with_no_origin_surface_withholds_nothing(
        self, conn, source_type,
    ):
        tid = db.create_task(conn, user_id="alice", source_type=source_type, prompt="p",
                             conversation_token=_email_thread_room(conn), is_group_chat=True)
        assert self._withheld(conn, db.get_task(conn, tid)) == frozenset()

    def test_a_subtask_of_a_correspondents_turn_is_one(self, conn):
        token = _email_thread_room(conn)
        parent = _email_turn(conn, token, author_label="carol@example.com")
        tid = db.create_task(conn, user_id="alice", source_type="subtask", prompt="p",
                             conversation_token=token, parent_task_id=parent.id)
        assert self._withheld(conn, db.get_task(conn, tid)) == _WITHHELD_FROM_A_CORRESPONDENT

    def test_control_a_talk_guests_turn_still_withholds_everything(self, conn):
        token = _shared_room(conn)
        db.add_room_binding(conn, token, "talk", "talk-ref-9")
        task = _email_turn(conn, token, author_label="max")
        conn.execute("UPDATE tasks SET guest_participant_id = 3 WHERE id = ?", (task.id,))
        task = db.get_task(conn, task.id)
        assert self._withheld(conn, task) == room_scopes.all_scopes(self.INDEX)
        assert room_scopes.ambient_memory_off(conn, task) is True

    def test_control_a_shared_web_room_still_leaves_memory_out(self, conn):
        task = _email_turn(conn, _shared_room(conn), author_user_id="alice")
        assert room_scopes.ambient_memory_off(conn, task) is True

    def test_an_unreadable_database_withholds_everything(self, make_task):
        broken = sqlite3.connect(":memory:")
        task = make_task(user_id="alice", conversation_token="t", source_type="email")
        assert self._withheld(broken, task) == room_scopes.all_scopes(self.INDEX)


class TestARoomlessCorrespondent:
    """A held mail approved and admitted into no room (ISSUE-874)."""

    INDEX = _CORRESPONDENT_INDEX

    def test_a_strangers_mail_withholds_the_same_set(self, conn):
        task = _roomless_mail(conn, "carol@example.com")
        assert room_scopes.withheld_for_task(
            conn, task, skill_index=self.INDEX, own_addresses=["alice@example.com"],
        ) == _WITHHELD_FROM_A_CORRESPONDENT

    def test_the_hosts_own_held_mail_withholds_nothing(self, conn):
        task = _roomless_mail(conn, "alice@example.com")
        assert room_scopes.withheld_for_task(
            conn, task, skill_index=self.INDEX, own_addresses=["alice@example.com"],
        ) == frozenset()

    def test_with_no_database_an_email_task_is_not_trusted(self, make_task):
        task = make_task(user_id="alice", source_type="email")
        assert room_scopes.withheld_for_task(
            None, task, skill_index=self.INDEX,
        ) == _WITHHELD_FROM_A_CORRESPONDENT

    def test_control_with_no_database_a_cli_task_withholds_nothing(self, make_task):
        task = make_task(user_id="alice", source_type="cli")
        assert room_scopes.withheld_for_task(None, task, skill_index=self.INDEX) == frozenset()


class TestTheReach:
    INDEX = _CORRESPONDENT_INDEX

    def test_it_carries_the_posture_beside_the_set(self, conn):
        task = _roomless_mail(conn, "carol@example.com")
        reach = room_scopes.reach_for_task(conn, task, skill_index=self.INDEX)
        assert reach.author == "correspondent"
        assert reach.withheld == _WITHHELD_FROM_A_CORRESPONDENT
        assert reach.allowance.web is False
        assert reach.standing_memory_loads is True

    def test_a_guests_standing_memory_does_not_load(self, make_task):
        task = make_task(user_id="alice", source_type="web", guest_participant_id=1)
        reach = room_scopes.reach_for_task(None, task, skill_index=self.INDEX)
        assert reach.author == "guest"
        assert reach.standing_memory_loads is False

    def test_a_guest_up_the_chain_withholds_everything(self, conn):
        room = _shared_room(conn)
        parent = db.create_task(conn, user_id="alice", source_type="web", prompt="p",
                                conversation_token=room)
        conn.execute("UPDATE tasks SET guest_participant_id = 3 WHERE id = ?", (parent,))
        child = db.create_task(conn, user_id="alice", source_type="subtask", prompt="p",
                               parent_task_id=parent)
        reach = room_scopes.reach_for_task(conn, db.get_task(conn, child), skill_index=self.INDEX)
        assert reach.withheld == room_scopes.all_scopes(self.INDEX)

    def test_a_task_nobody_asked_in_a_room_loads_no_standing_memory(self, conn):
        tid = db.create_task(conn, user_id="alice", source_type="subtask", prompt="p",
                             conversation_token=_shared_room(conn))
        reach = room_scopes.reach_for_task(conn, db.get_task(conn, tid), skill_index=self.INDEX)
        assert reach.author == "own"
        assert reach.standing_memory_loads is False

    def test_an_unreadable_author_loads_no_standing_memory(self, make_task):
        task = make_task(user_id="alice", source_type="email")
        reach = room_scopes.reach_for_task(
            sqlite3.connect(":memory:"), task, skill_index=self.INDEX,
        )
        assert reach.withheld == room_scopes.all_scopes(self.INDEX)
        assert reach.standing_memory_loads is False

    def test_an_own_turn_loads_it(self, make_task):
        task = make_task(user_id="alice", source_type="cli")
        assert room_scopes.reach_for_task(
            None, task, skill_index=self.INDEX,
        ).standing_memory_loads is True


class TestTheAllowance:
    INDEX = _CORRESPONDENT_INDEX

    def test_it_is_the_default_set_in_the_index(self):
        index = _index(email="private", calendar="private", skills="private")
        allowance = room_scopes.correspondent_scopes(None, "alice", skill_index=index)
        assert allowance.scopes == {"email", "skills"}
        assert allowance.web is False

    def test_an_empty_index_warns_about_nothing(self, caplog):
        with caplog.at_level("WARNING", logger="istota.rooms.scopes"):
            allowance = room_scopes.correspondent_scopes(None, "alice", skill_index={})
        assert allowance.scopes == frozenset()
        assert caplog.records == []

    def test_an_empty_index_still_withholds_for_the_wallet(self, conn):
        task = _roomless_mail(conn, "carol@example.com")
        assert room_scopes.withheld_for_task(conn, task, skill_index={}) == {"files", "memory"}

    def test_a_widened_allowance_narrows_what_is_withheld(self, conn):
        conn.execute(
            "INSERT INTO user_profiles (user_id, correspondent_scopes) VALUES (?, ?)",
            ("alice", '["calendar", "web"]'),
        )
        task = _roomless_mail(conn, "carol@example.com")
        reach = room_scopes.reach_for_task(conn, task, skill_index=self.INDEX)
        assert "calendar" not in reach.withheld
        assert reach.allowance.web is True


class TestACorrespondentsGroups:
    def _member(self, conn):
        db.create_group(conn, "fam", kind="family", display_name="Fam", created_by="op")
        db.add_group_member(conn, "fam", "alice", added_by="op")

    def test_a_roomless_correspondent_gets_none(self, conn):
        self._member(conn)
        task = _roomless_mail(conn, "carol@example.com")
        assert room_scopes.task_group_ids(
            conn, task, own_addresses=["alice@example.com"],
        ) == []

    def test_control_the_hosts_own_roomless_mail_gets_them(self, conn):
        self._member(conn)
        task = _roomless_mail(conn, "alice@example.com")
        assert room_scopes.task_group_ids(
            conn, task, own_addresses=["alice@example.com"],
        ) == ["fam"]

    def test_a_thread_room_correspondent_gets_none(self, conn):
        self._member(conn)
        task = _email_turn(conn, _email_thread_room(conn), author_label="carol@example.com")
        assert room_scopes.task_group_ids(conn, task) == []


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
    # Widening this is a disclosure decision. The room location verbs check
    # explicit grants and refuse guests themselves; the location skill stays
    # private because its other verbs read personal history.
    empty = tmp_path / "overrides"
    empty.mkdir()
    index = load_skill_index(empty)
    safe = {name for name, meta in index.items() if meta.shared_room == "safe"}
    assert safe == {"sensitive_actions", "untrusted_input", "room"}


def test_contacts_are_withheld_on_a_guest_turn(tmp_path, make_task):
    index = load_skill_index(tmp_path)
    assert "contacts" in room_scopes.scope_names(index)
    task = make_task(user_id="alice", conversation_token="room-1", guest_participant_id=7)
    assert "contacts" in room_scopes.withheld_for_task(None, task, skill_index=index)
