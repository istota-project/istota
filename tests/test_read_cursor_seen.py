"""Read-on-view cursor rule: rows seen in the All / Unread / Starred panes
advance a room's web read cursor, contiguously and forward only.

`db.advance_read_cursors_for_seen` resolves seen ids to the rooms the panes
show and moves each room's cursor to the end of the unbroken run of seen
unread rows past it. `db.advance_room_read_state` is the forward-only write.
"""

import re

import pytest

from istota import db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    with db.get_db(db_path) as c:
        yield c


def _room(conn, user_id="alice", name="room"):
    return db.create_web_chat_room(conn, user_id, name)


def _msg(conn, token, role="assistant", origin="web"):
    return db.add_message(conn, token, role=role, body="x", origin_surface=origin)


def _cursor(conn, token, user_id="alice"):
    return db.get_room_read_state(conn, token, "web", user_id)


def _by_token(result):
    return {entry["token"]: entry for entry in result}


class TestAdvanceRoomReadState:
    def test_inserts_when_no_row(self, conn):
        room = _room(conn)
        db.advance_room_read_state(conn, room.token, "web", 7, "alice")
        assert _cursor(conn, room.token) == 7

    def test_moves_forward(self, conn):
        room = _room(conn)
        db.set_room_read_state(conn, room.token, "web", 5, "alice")
        db.advance_room_read_state(conn, room.token, "web", 9, "alice")
        assert _cursor(conn, room.token) == 9

    def test_never_moves_backwards(self, conn):
        room = _room(conn)
        db.set_room_read_state(conn, room.token, "web", 9, "alice")
        db.advance_room_read_state(conn, room.token, "web", 5, "alice")
        assert _cursor(conn, room.token) == 9


class TestContiguousAdvance:
    def test_stops_at_first_unseen_unread_row(self, conn):
        room = _room(conn)
        a = _msg(conn, room.token)
        b = _msg(conn, room.token)
        c = _msg(conn, room.token)
        d = _msg(conn, room.token)
        result = db.advance_read_cursors_for_seen(conn, "alice", [a, b, d])
        entry = _by_token(result)[room.token]
        assert entry["last_read_message_id"] == b
        assert entry["advanced"] is True
        assert entry["fully_read"] is False
        assert entry["unread_count"] == 2  # c and d: d is past the gap at c
        assert _cursor(conn, room.token) == b < c

    def test_user_rows_between_seen_rows_do_not_block(self, conn):
        room = _room(conn)
        a = _msg(conn, room.token)
        _msg(conn, room.token, role="user")
        b = _msg(conn, room.token)
        c = _msg(conn, room.token)
        result = db.advance_read_cursors_for_seen(conn, "alice", [a, b])
        assert _by_token(result)[room.token]["last_read_message_id"] == b
        assert db.count_unread_messages(conn, room.token, "web", "alice") == 1
        assert _cursor(conn, room.token) < c

    def test_all_seen_goes_to_room_max_and_is_fully_read(self, conn):
        room = _room(conn)
        a = _msg(conn, room.token)
        b = _msg(conn, room.token, role="system")
        trailing_user = _msg(conn, room.token, role="user")
        result = db.advance_read_cursors_for_seen(conn, "alice", [a, b])
        entry = _by_token(result)[room.token]
        assert entry["last_read_message_id"] == trailing_user
        assert trailing_user == db.room_max_message_id(conn, room.token)
        assert entry["fully_read"] is True
        assert entry["advanced"] is True
        assert entry["unread_count"] == 0

    def test_seen_own_turn_in_a_read_room_covers_trailing_user_rows(self, conn):
        room = _room(conn)
        a = _msg(conn, room.token)
        db.set_room_read_state(conn, room.token, "web", a, "alice")
        own = _msg(conn, room.token, role="user")
        result = db.advance_read_cursors_for_seen(conn, "alice", [own])
        entry = _by_token(result)[room.token]
        assert entry["last_read_message_id"] == own
        assert entry["fully_read"] is True

    def test_newest_first_seeing_only_the_newest_advances_nothing(self, conn):
        room = _room(conn)
        a = _msg(conn, room.token)
        _msg(conn, room.token)
        newest = _msg(conn, room.token)
        db.set_room_read_state(conn, room.token, "web", a, "alice")
        result = db.advance_read_cursors_for_seen(conn, "alice", [newest])
        entry = _by_token(result)[room.token]
        assert entry["advanced"] is False
        assert entry["last_read_message_id"] == a
        assert entry["unread_count"] == 2
        assert _cursor(conn, room.token) == a

    def test_rooms_advance_independently(self, conn):
        one = _room(conn, name="one")
        two = _room(conn, name="two")
        a1 = _msg(conn, one.token)
        a2 = _msg(conn, two.token)
        b1 = _msg(conn, one.token)
        _msg(conn, two.token)
        result = _by_token(db.advance_read_cursors_for_seen(conn, "alice", [a1, a2]))
        assert result[one.token]["last_read_message_id"] == a1
        assert result[two.token]["last_read_message_id"] == a2
        assert result[one.token]["unread_count"] == 1
        assert _cursor(conn, one.token) < b1

    def test_cursor_already_past_the_seen_ids_is_not_lowered(self, conn):
        room = _room(conn)
        a = _msg(conn, room.token)
        b = _msg(conn, room.token)
        c = _msg(conn, room.token)
        db.set_room_read_state(conn, room.token, "web", c, "alice")
        result = db.advance_read_cursors_for_seen(conn, "alice", [a, b])
        entry = _by_token(result)[room.token]
        assert entry["advanced"] is False
        assert entry["last_read_message_id"] == c
        assert entry["fully_read"] is True
        assert _cursor(conn, room.token) == c

    def test_unread_count_matches_count_unread_messages(self, conn):
        room = _room(conn)
        ids = [_msg(conn, room.token, role=r)
               for r in ("assistant", "user", "system", "assistant", "user", "assistant")]
        result = db.advance_read_cursors_for_seen(conn, "alice", ids[:3])
        entry = _by_token(result)[room.token]
        assert entry["unread_count"] == db.count_unread_messages(
            conn, room.token, "web", "alice",
        )
        assert entry["unread_count"] == 2

    def test_empty_seen_list_touches_nothing(self, conn):
        room = _room(conn)
        _msg(conn, room.token)
        assert db.advance_read_cursors_for_seen(conn, "alice", []) == []
        assert _cursor(conn, room.token) == 0


class TestVisibility:
    def test_non_member_room_dropped(self, conn):
        mine = _room(conn, "alice", "mine")
        bobs = _room(conn, "bob", "bobs")
        theirs = _msg(conn, bobs.token)
        own = _msg(conn, mine.token)
        result = _by_token(db.advance_read_cursors_for_seen(conn, "alice", [theirs, own]))
        assert set(result) == {mine.token}
        assert _cursor(conn, bobs.token, "alice") == 0
        assert _cursor(conn, bobs.token, "bob") == 0

    def test_dismissed_room_dropped(self, conn):
        room = _room(conn)
        a = _msg(conn, room.token)
        db.dismiss_room(conn, room.token, "alice")
        assert db.advance_read_cursors_for_seen(conn, "alice", [a]) == []
        assert _cursor(conn, room.token) == 0

    def test_archived_room_dropped(self, conn):
        room = _room(conn)
        a = _msg(conn, room.token)
        db.set_room_archived(conn, room.token, True)
        assert db.advance_read_cursors_for_seen(conn, "alice", [a]) == []
        assert _cursor(conn, room.token) == 0

    def test_hidden_email_thread_dropped_by_default(self, conn):
        thread = db.register_room(conn, None, "alice", origin="web", name="thread")
        db.add_room_binding(conn, thread.token, "email", "thread:one")
        assert thread.token in db.hidden_room_tokens_for_member(conn, "alice")
        a = _msg(conn, thread.token)
        assert db.advance_read_cursors_for_seen(conn, "alice", [a]) == []
        assert _cursor(conn, thread.token) == 0

    def test_explicit_exclusion_set_is_used(self, conn):
        room = _room(conn)
        a = _msg(conn, room.token)
        result = db.advance_read_cursors_for_seen(
            conn, "alice", [a], exclude_tokens={room.token},
        )
        assert result == []
        assert _cursor(conn, room.token) == 0

    def test_nonexistent_ids_dropped(self, conn):
        room = _room(conn)
        a = _msg(conn, room.token)
        result = _by_token(db.advance_read_cursors_for_seen(
            conn, "alice", [a, 999_999, a + 1000],
        ))
        assert set(result) == {room.token}
        assert result[room.token]["last_read_message_id"] == a


def _role_line(sql: str) -> str:
    return next(ln for ln in sql.splitlines() if re.match(r"\s*role\s+TEXT\b", ln))


def _schema_roles(conn) -> set[str]:
    """The `messages.role` values the schema declares. The column has no CHECK
    (the table as created carries none), so the comment on its declaration in
    schema.sql is the only list."""
    stored = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages'",
    ).fetchone()[0]
    assert "CHECK" not in _role_line(stored).upper()
    schema = db._resolve_schema_path().read_text()
    block = schema[schema.index("CREATE TABLE IF NOT EXISTS messages ("):]
    line = _role_line(block)
    assert "CHECK" not in line.upper()
    return set(re.findall(r"'([a-z_]+)'", line))


class TestUnreadRowsAreShowable:
    """A row that counts as unread but no pane can show would block its room's
    cursor forever, so every unread role must pass the panes' row filter."""

    def test_schema_role_list_is_what_this_test_covers(self, conn):
        assert _schema_roles(conn) == {"user", "assistant", "system"}

    @pytest.mark.parametrize("origin", ["web", "talk", "email", "scheduled", "cli"])
    def test_every_unread_role_appears_in_the_unread_pane(self, conn, origin):
        room = _room(conn)
        ids = {role: _msg(conn, room.token, role=role, origin=origin)
               for role in sorted(_schema_roles(conn))}
        shown = {
            r["msg_id"] for r in db.list_messages_across_rooms(conn, "alice", view="unread")
        }
        unread = {mid for role, mid in ids.items() if role != "user"}
        assert unread
        assert shown == unread
        assert len(shown) == db.count_unread_messages(conn, room.token, "web", "alice")

    @pytest.mark.parametrize("origin", ["web", "talk", "email", "scheduled", "cli"])
    def test_every_unread_role_can_be_advanced_past(self, conn, origin):
        room = _room(conn)
        ids = [_msg(conn, room.token, role=role, origin=origin)
               for role in sorted(_schema_roles(conn))]
        unread = [mid for mid in ids
                  if conn.execute("SELECT role FROM messages WHERE id = ?",
                                  (mid,)).fetchone()[0] != "user"]
        result = _by_token(db.advance_read_cursors_for_seen(conn, "alice", unread))
        assert result[room.token]["fully_read"] is True
        assert db.count_unread_messages(conn, room.token, "web", "alice") == 0
