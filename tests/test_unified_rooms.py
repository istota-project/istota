"""Tests for the unified Talk/web room sync data model (Stage 1).

Covers the surface-independent room registry (`rooms`), per-surface
`room_bindings`, the canonical `messages` store, `room_read_state`, and the
one-time migration that folds the legacy `web_chat_rooms` / `web_chat_messages`
tables plus distinct Talk `conversation_token`s into the new model.
"""

import json

import pytest

from istota import db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    with db.get_db(db_path) as c:
        yield c


# ---------------------------------------------------------------------------
# Schema presence
# ---------------------------------------------------------------------------


class TestSchema:
    def test_new_tables_exist(self, conn):
        names = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"rooms", "room_bindings", "messages", "room_read_state"} <= names

    def test_messages_accepts_system_role_and_title(self, conn):
        db.register_room(conn, "web-u-1", "u", origin="web", name="Chat")
        mid = db.add_message(
            conn,
            "web-u-1",
            role="system",
            body="disk almost full",
            origin_surface="web",
            title="Alert",
        )
        msgs = db.get_messages(conn, "web-u-1")
        assert len(msgs) == 1
        assert msgs[0].id == mid
        assert msgs[0].role == "system"
        assert msgs[0].title == "Alert"
        assert msgs[0].task_id is None


# ---------------------------------------------------------------------------
# Room registry + bindings
# ---------------------------------------------------------------------------


class TestRoomRegistry:
    def test_register_and_get_room(self, conn):
        room = db.register_room(conn, "cpzpcfx2", "u", origin="talk", name="#istota")
        assert room.token == "cpzpcfx2"
        assert room.origin == "talk"
        assert room.name == "#istota"
        assert room.archived is False
        again = db.get_room(conn, "cpzpcfx2")
        assert again is not None
        assert again.token == "cpzpcfx2"

    def test_get_unknown_room_is_none(self, conn):
        assert db.get_room(conn, "nope") is None

    def test_register_room_idempotent_keeps_first(self, conn):
        db.register_room(conn, "t", "u", origin="talk", name="First")
        # Second register must not overwrite name/origin nor duplicate the row.
        db.register_room(conn, "t", "u", origin="talk", name="Second")
        rooms = db.list_rooms(conn, "u")
        assert len(rooms) == 1
        assert rooms[0].name == "First"

    def test_list_rooms_excludes_archived_by_default(self, conn):
        db.register_room(conn, "a", "u", origin="web", name="A")
        db.register_room(conn, "b", "u", origin="web", name="B")
        db.set_room_archived(conn, "b", True)
        active = [r.token for r in db.list_rooms(conn, "u")]
        assert active == ["a"]
        allr = {r.token for r in db.list_rooms(conn, "u", include_archived=True)}
        assert allr == {"a", "b"}

    def test_list_rooms_scoped_by_user(self, conn):
        db.register_room(conn, "a", "alice", origin="web", name="A")
        db.register_room(conn, "b", "bob", origin="web", name="B")
        assert [r.token for r in db.list_rooms(conn, "alice")] == ["a"]


class TestBindings:
    def test_add_and_resolve_binding(self, conn):
        db.register_room(conn, "room1", "u", origin="web", name="R")
        db.add_room_binding(conn, "room1", "talk", "cpzpcfx2")
        assert db.resolve_room_token(conn, "talk", "cpzpcfx2") == "room1"

    def test_resolve_unknown_binding_is_none(self, conn):
        assert db.resolve_room_token(conn, "talk", "missing") is None

    def test_add_binding_idempotent(self, conn):
        db.register_room(conn, "room1", "u", origin="web", name="R")
        db.add_room_binding(conn, "room1", "talk", "cpzpcfx2")
        db.add_room_binding(conn, "room1", "talk", "cpzpcfx2")
        bindings = db.list_room_bindings(conn, "room1")
        assert len(bindings) == 1
        assert bindings[0].surface == "talk"
        assert bindings[0].surface_ref == "cpzpcfx2"

    def test_get_room_binding(self, conn):
        db.register_room(conn, "room1", "u", origin="web", name="R")
        db.add_room_binding(conn, "room1", "talk", "cpzpcfx2")
        b = db.get_room_binding(conn, "room1", "talk")
        assert b is not None and b.surface_ref == "cpzpcfx2"
        assert db.get_room_binding(conn, "room1", "email") is None


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class TestMessages:
    def test_add_user_and_assistant_message_keyed_on_task(self, conn):
        db.register_room(conn, "r", "u", origin="web", name="R")
        uid = db.add_message(
            conn, "r", role="user", body="hi", origin_surface="web", task_id=7
        )
        aid = db.add_message(
            conn, "r", role="assistant", body="hello", origin_surface="web", task_id=7
        )
        msgs = db.get_messages(conn, "r")
        assert [m.id for m in msgs] == [uid, aid]
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert all(m.task_id == 7 for m in msgs)

    def test_external_ids_roundtrip(self, conn):
        db.register_room(conn, "r", "u", origin="web", name="R")
        mid = db.add_message(
            conn,
            "r",
            role="assistant",
            body="hello",
            origin_surface="web",
            task_id=1,
            external_ids={"talk": "42"},
        )
        msg = db.get_messages(conn, "r")[0]
        assert msg.id == mid
        assert msg.external_ids == {"talk": "42"}

    def test_set_message_external_id_merges(self, conn):
        db.register_room(conn, "r", "u", origin="web", name="R")
        mid = db.add_message(
            conn, "r", role="assistant", body="hi", origin_surface="web", task_id=1
        )
        db.set_message_external_id(conn, mid, "talk", "99")
        msg = db.get_messages(conn, "r")[0]
        assert msg.external_ids == {"talk": "99"}

    def test_messages_scoped_by_room(self, conn):
        db.register_room(conn, "r1", "u", origin="web", name="R1")
        db.register_room(conn, "r2", "u", origin="web", name="R2")
        db.add_message(conn, "r1", role="user", body="a", origin_surface="web", task_id=1)
        db.add_message(conn, "r2", role="user", body="b", origin_surface="web", task_id=2)
        assert [m.body for m in db.get_messages(conn, "r1")] == ["a"]


# ---------------------------------------------------------------------------
# Read state
# ---------------------------------------------------------------------------


class TestReadState:
    def test_read_state_defaults_zero(self, conn):
        db.register_room(conn, "r", "u", origin="web", name="R")
        assert db.get_room_read_state(conn, "r", "web") == 0

    def test_set_and_get_read_state_per_surface(self, conn):
        db.register_room(conn, "r", "u", origin="web", name="R")
        db.set_room_read_state(conn, "r", "web", 12)
        db.set_room_read_state(conn, "r", "talk", 3)
        assert db.get_room_read_state(conn, "r", "web") == 12
        assert db.get_room_read_state(conn, "r", "talk") == 3

    def test_set_read_state_upserts(self, conn):
        db.register_room(conn, "r", "u", origin="web", name="R")
        db.set_room_read_state(conn, "r", "web", 5)
        db.set_room_read_state(conn, "r", "web", 9)
        assert db.get_room_read_state(conn, "r", "web") == 9


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _seed_legacy(conn):
    """Insert legacy rows and clear the migration marker, as if the new
    migration code had never run against this DB."""
    conn.execute("DELETE FROM _migration_state WHERE name = 'unified_rooms_v1'")
    conn.execute(
        "INSERT INTO web_chat_rooms (user_id, token, name, archived) "
        "VALUES ('u', 'web-u-abc', 'My Chat', 0)"
    )
    conn.execute(
        "INSERT INTO web_chat_messages (user_id, token, role, title, text) "
        "VALUES ('u', 'web-u-abc', 'system', 'Alert', 'disk full')"
    )
    # Two Talk tasks in one room + one in another, plus a non-talk task.
    conn.execute(
        "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, status) "
        "VALUES ('talk', 'u', 'cpzpcfx2', 'hi', 'completed')"
    )
    conn.execute(
        "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, status) "
        "VALUES ('talk', 'u', 'cpzpcfx2', 'again', 'completed')"
    )
    conn.execute(
        "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, status) "
        "VALUES ('talk', 'u', 'otherroom', 'yo', 'completed')"
    )
    conn.execute(
        "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, status) "
        "VALUES ('scheduled', 'u', 'jobtoken', 'cron', 'completed')"
    )
    conn.commit()


class TestMigration:
    def test_web_rooms_and_messages_migrated(self, conn):
        _seed_legacy(conn)
        db._migrate_unified_rooms(conn)
        conn.commit()

        room = db.get_room(conn, "web-u-abc")
        assert room is not None
        assert room.origin == "web"
        assert room.name == "My Chat"
        # web binding self-referential
        assert db.resolve_room_token(conn, "web", "web-u-abc") == "web-u-abc"
        # legacy system message folded in
        msgs = db.get_messages(conn, "web-u-abc")
        assert len(msgs) == 1
        assert msgs[0].role == "system"
        assert msgs[0].title == "Alert"
        assert msgs[0].body == "disk full"
        assert msgs[0].task_id is None

    def test_talk_rooms_backfilled(self, conn):
        _seed_legacy(conn)
        db._migrate_unified_rooms(conn)
        conn.commit()

        talk_room = db.get_room(conn, "cpzpcfx2")
        assert talk_room is not None
        assert talk_room.origin == "talk"
        assert db.resolve_room_token(conn, "talk", "cpzpcfx2") == "cpzpcfx2"
        assert db.get_room(conn, "otherroom") is not None
        # scheduled task's token is NOT a conversational room
        assert db.get_room(conn, "jobtoken") is None

    def test_migration_idempotent(self, conn):
        _seed_legacy(conn)
        db._migrate_unified_rooms(conn)
        conn.commit()
        db._migrate_unified_rooms(conn)
        conn.commit()

        rooms = db.list_rooms(conn, "u", include_archived=True)
        tokens = sorted(r.token for r in rooms)
        assert tokens == ["cpzpcfx2", "otherroom", "web-u-abc"]
        # exactly one system message, not duplicated
        assert len(db.get_messages(conn, "web-u-abc")) == 1


# ---------------------------------------------------------------------------
# Delete cascade
# ---------------------------------------------------------------------------


class TestDeleteCascade:
    def test_delete_web_chat_room_clears_new_tables(self, conn):
        room = db.create_web_chat_room(conn, "u", "Chat")
        token = room.token
        # create_web_chat_room should register the room + web binding
        assert db.get_room(conn, token) is not None
        assert db.resolve_room_token(conn, "web", token) == token
        db.add_message(conn, token, role="user", body="hi", origin_surface="web", task_id=1)
        db.set_room_read_state(conn, token, "web", 1)

        assert db.delete_web_chat_room(conn, room.id, "u") is True

        assert db.get_room(conn, token) is None
        assert db.get_messages(conn, token) == []
        assert db.list_room_bindings(conn, token) == []
        assert db.get_room_read_state(conn, token, "web") == 0
