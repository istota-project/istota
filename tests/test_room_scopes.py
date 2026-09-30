"""The shared-room scope vocabulary, the grant reads and the ``safe`` markings."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from istota import db, room_scopes
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


def _grant(conn, token, user_id, *scopes):
    for scope in scopes:
        conn.execute(
            "INSERT INTO room_data_grants (room_token, user_id, scope) VALUES (?, ?, ?)",
            (token, user_id, scope),
        )


def _shared_room(conn) -> str:
    room = db.create_web_chat_room(conn, "alice", "Family")
    db.add_room_member(conn, room.token, "bob")
    return room.token


class TestTheVocabulary:
    def test_private_skills_then_the_synthetic_scopes(self):
        index = _index(calendar="private", web="safe", money="private")
        assert room_scopes.scope_names(index) == ["calendar", "money", "files", "memory"]

    def test_withheld_is_the_complement_of_granted(self):
        index = _index(calendar="private", money="private")
        withheld = room_scopes.withheld_scopes(index, frozenset({"calendar", "files"}))
        assert withheld == {"money", "memory"}


class TestTheGrantReads:
    def test_nothing_is_granted_by_default(self, conn):
        assert room_scopes.granted_scopes(conn, _shared_room(conn), "alice") == frozenset()

    def test_a_grant_is_the_granting_users_own(self, conn):
        token = _shared_room(conn)
        _grant(conn, token, "alice", "calendar")
        assert room_scopes.is_scope_granted(conn, token, "alice", "calendar")
        assert not room_scopes.is_scope_granted(conn, token, "bob", "calendar")

    def test_a_read_error_grants_nothing(self):
        broken = sqlite3.connect(":memory:")
        assert room_scopes.granted_scopes(broken, "t", "alice") == frozenset()


class TestWhatATaskIsWithheld:
    INDEX = _index(calendar="private", sensitive_actions="safe")

    def _withheld(self, conn, token, *, policy="restrict", user_id="alice"):
        return room_scopes.task_withheld_scopes(
            conn, policy=policy, conversation_token=token, user_id=user_id,
            skill_index=self.INDEX,
        )

    def test_a_private_room_withholds_nothing(self, conn):
        room = db.create_web_chat_room(conn, "alice", "Mine")
        assert self._withheld(conn, room.token) == frozenset()

    def test_a_shared_room_withholds_every_ungranted_scope(self, conn):
        token = _shared_room(conn)
        _grant(conn, token, "alice", "files")
        assert self._withheld(conn, token) == {"calendar", "memory"}

    def test_a_task_marked_group_chat_is_restricted_before_the_roster_is(self, conn):
        # A Talk group's first turn, or one whose roster fetch failed: the
        # surface said group, the participants table does not know yet.
        room = db.create_web_chat_room(conn, "alice", "Mine")
        assert room_scopes.task_withheld_scopes(
            conn, policy="restrict", conversation_token=room.token, user_id="alice",
            skill_index=self.INDEX, assume_shared=True,
        ) == {"calendar", "files", "memory"}

    def test_policy_off_withholds_nothing(self, conn):
        assert self._withheld(conn, _shared_room(conn), policy="off") == frozenset()

    def test_no_conversation_withholds_nothing(self, conn):
        assert self._withheld(conn, "") == frozenset()

    def test_a_surface_ref_is_read_against_its_room(self, conn):
        # A promoted room's Talk ref differs from its canonical token, and a
        # task can carry either; the audience and the grants are the room's.
        token = _shared_room(conn)
        db.add_room_binding(conn, token, "talk", "talk-ref-1")
        _grant(conn, token, "alice", "calendar", "files", "memory")
        assert self._withheld(conn, "talk-ref-1") == frozenset()

    def test_an_unreadable_audience_withholds_everything(self):
        broken = sqlite3.connect(":memory:")
        assert room_scopes.task_withheld_scopes(
            broken, policy="restrict", conversation_token="t", user_id="alice",
            skill_index=self.INDEX,
        ) == {"calendar", "files", "memory"}


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
    empty = tmp_path / "overrides"
    empty.mkdir()
    index = load_skill_index(empty)
    safe = {name for name, meta in index.items() if meta.shared_room == "safe"}
    assert safe == {"sensitive_actions", "untrusted_input"}
