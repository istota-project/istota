"""DB layer for chat cross-room views and per-message starring.

Covers the `message_stars` table (star/unstar idempotency, per-user isolation,
room-delete cascade), the cross-room aggregate query
(`list_messages_across_rooms` for the all / unread / starred views), and the
bulk read-cursor advance (`mark_all_rooms_read`).
"""

import pytest

from istota import db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    with db.get_db(db_path) as c:
        yield c


def _room(conn, user_id, name):
    """A registered web room (registry row + membership + handle)."""
    return db.create_web_chat_room(conn, user_id, name)


def _msg(conn, token, *, role="assistant", body="hi", origin="web",
         task_id=None, title=None, created_at=None):
    mid = db.add_message(
        conn, token, role=role, body=body, origin_surface=origin,
        task_id=task_id, title=title,
    )
    if created_at is not None:
        conn.execute(
            "UPDATE messages SET created_at = ? WHERE id = ?", (created_at, mid),
        )
    return mid


# ---------------------------------------------------------------------------
# message_stars: star / unstar
# ---------------------------------------------------------------------------


class TestMessageStars:
    def test_star_then_unstar(self, conn):
        room = _room(conn, "alice", "general")
        mid = _msg(conn, room.token)
        assert db.set_message_starred(conn, mid, "alice", True) is True
        assert db.get_starred_message_ids(conn, "alice", [mid]) == {mid}
        assert db.set_message_starred(conn, mid, "alice", False) is True
        assert db.get_starred_message_ids(conn, "alice", [mid]) == set()

    def test_star_idempotent(self, conn):
        room = _room(conn, "alice", "general")
        mid = _msg(conn, room.token)
        assert db.set_message_starred(conn, mid, "alice", True)
        assert db.set_message_starred(conn, mid, "alice", True)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM message_stars WHERE message_id = ?", (mid,),
        ).fetchone()
        assert row["n"] == 1

    def test_unstar_idempotent(self, conn):
        room = _room(conn, "alice", "general")
        mid = _msg(conn, room.token)
        assert db.set_message_starred(conn, mid, "alice", False)
        assert db.set_message_starred(conn, mid, "alice", False)
        assert db.get_starred_message_ids(conn, "alice", [mid]) == set()

    def test_unknown_message_returns_false(self, conn):
        assert db.set_message_starred(conn, 999_999, "alice", True) is False

    def test_per_user_isolation(self, conn):
        room = _room(conn, "alice", "shared")
        db.add_room_member(conn, room.token, "bob")
        mid = _msg(conn, room.token)
        db.set_message_starred(conn, mid, "alice", True)
        assert db.get_starred_message_ids(conn, "alice", [mid]) == {mid}
        assert db.get_starred_message_ids(conn, "bob", [mid]) == set()

    def test_get_starred_message_ids_empty_input(self, conn):
        assert db.get_starred_message_ids(conn, "alice", []) == set()

    def test_get_message_room(self, conn):
        room = _room(conn, "alice", "general")
        mid = _msg(conn, room.token)
        assert db.get_message_room(conn, mid) == room.token
        assert db.get_message_room(conn, 999_999) is None

    def test_delete_web_chat_room_removes_stars(self, conn):
        room = _room(conn, "alice", "doomed")
        other = _room(conn, "alice", "kept")
        mid = _msg(conn, room.token)
        kept = _msg(conn, other.token)
        db.set_message_starred(conn, mid, "alice", True)
        db.set_message_starred(conn, kept, "alice", True)
        assert db.delete_web_chat_room(conn, room.id, "alice") is True
        rows = conn.execute("SELECT message_id FROM message_stars").fetchall()
        assert [r["message_id"] for r in rows] == [kept]


# ---------------------------------------------------------------------------
# list_messages_across_rooms — the All / Unread / Starred aggregate query
# ---------------------------------------------------------------------------


class TestListMessagesAcrossRooms:
    def _seed(self, conn):
        """Two alice rooms + one bob room with a mix of roles/origins."""
        r1 = _room(conn, "alice", "one")
        r2 = _room(conn, "alice", "two")
        rb = _room(conn, "bob", "bobs")
        ids = {}
        ids["u1"] = _msg(conn, r1.token, role="user", body="q1",
                         task_id=101, created_at="2026-07-01 10:00:00")
        ids["a1"] = _msg(conn, r1.token, role="assistant", body="a1",
                         task_id=101, created_at="2026-07-01 10:00:05")
        ids["s1"] = _msg(conn, r1.token, role="system", body="alert body",
                         title="Alert", created_at="2026-07-01 11:00:00")
        ids["u2"] = _msg(conn, r2.token, role="user", body="q2",
                         task_id=102, created_at="2026-07-02 09:00:00")
        ids["a2"] = _msg(conn, r2.token, role="assistant", body="a2",
                         task_id=102, created_at="2026-07-02 09:00:05")
        ids["b1"] = _msg(conn, rb.token, role="assistant", body="bob only",
                         created_at="2026-07-02 10:00:00")
        return r1, r2, rb, ids

    def test_all_view_membership_and_order(self, conn):
        r1, r2, rb, ids = self._seed(conn)
        rows = db.list_messages_across_rooms(conn, "alice")
        got = [r["msg_id"] for r in rows]
        # Newest-first, bob's room absent, own turns included.
        assert got == [ids["a2"], ids["u2"], ids["s1"], ids["a1"], ids["u1"]]

    def test_row_shape(self, conn):
        r1, _, _, ids = self._seed(conn)
        rows = db.list_messages_across_rooms(conn, "alice")
        by_id = {r["msg_id"]: r for r in rows}
        sys_row = by_id[ids["s1"]]
        assert sys_row["room_token"] == r1.token
        assert sys_row["room_name"] == "one"
        assert sys_row["title"] == "Alert"
        assert not sys_row["starred"]

    def test_scheduled_user_rows_excluded(self, conn):
        r1 = _room(conn, "alice", "one")
        _msg(conn, r1.token, role="user", body="synthetic cron prompt",
             origin="scheduled", task_id=7)
        keep = _msg(conn, r1.token, role="assistant", body="cron result",
                    origin="scheduled", task_id=7)
        rows = db.list_messages_across_rooms(conn, "alice")
        assert [r["msg_id"] for r in rows] == [keep]

    def test_dismissed_room_excluded(self, conn):
        r1, r2, rb, ids = self._seed(conn)
        db.dismiss_room(conn, r1.token, "alice")
        rows = db.list_messages_across_rooms(conn, "alice")
        assert {r["msg_id"] for r in rows} == {ids["u2"], ids["a2"]}

    def test_archived_room_excluded(self, conn):
        r1, r2, rb, ids = self._seed(conn)
        db.set_room_archived(conn, r2.token, True)
        rows = db.list_messages_across_rooms(conn, "alice")
        assert {r["msg_id"] for r in rows} == {ids["u1"], ids["a1"], ids["s1"]}

    def test_unread_view_respects_cursor_and_excludes_own(self, conn):
        r1, r2, rb, ids = self._seed(conn)
        # Read past a1 in room one; nothing read in room two (no cursor row).
        db.set_room_read_state(conn, r1.token, "web", ids["a1"], "alice")
        rows = db.list_messages_across_rooms(conn, "alice", view="unread")
        got = {r["msg_id"] for r in rows}
        # s1 (after cursor) + a2 (no cursor row → everything unread); user turns
        # never count, matching count_unread_messages.
        assert got == {ids["s1"], ids["a2"]}

    def test_unread_view_agrees_with_count_unread(self, conn):
        r1, r2, rb, ids = self._seed(conn)
        db.set_room_read_state(conn, r1.token, "web", ids["a1"], "alice")
        rows = db.list_messages_across_rooms(conn, "alice", view="unread")
        total = (
            db.count_unread_messages(conn, r1.token, "web", "alice")
            + db.count_unread_messages(conn, r2.token, "web", "alice")
        )
        assert len(rows) == total

    def test_starred_view(self, conn):
        r1, r2, rb, ids = self._seed(conn)
        db.set_message_starred(conn, ids["u1"], "alice", True)
        db.set_message_starred(conn, ids["a2"], "alice", True)
        # Bob's star in his own room must not leak into alice's view.
        db.set_message_starred(conn, ids["b1"], "bob", True)
        rows = db.list_messages_across_rooms(conn, "alice", view="starred")
        assert [r["msg_id"] for r in rows] == [ids["a2"], ids["u1"]]
        assert all(r["starred"] for r in rows)

    def test_starred_flag_in_all_view(self, conn):
        r1, r2, rb, ids = self._seed(conn)
        db.set_message_starred(conn, ids["a1"], "alice", True)
        rows = db.list_messages_across_rooms(conn, "alice")
        by_id = {r["msg_id"]: r for r in rows}
        assert by_id[ids["a1"]]["starred"]
        assert not by_id[ids["a2"]]["starred"]

    def test_keyset_paging_with_equal_timestamps(self, conn):
        r1 = _room(conn, "alice", "one")
        r2 = _room(conn, "alice", "two")
        ts = "2026-07-03 12:00:00"
        m1 = _msg(conn, r1.token, body="x1", created_at=ts)
        m2 = _msg(conn, r2.token, body="x2", created_at=ts)
        m3 = _msg(conn, r1.token, body="x3", created_at=ts)
        page1 = db.list_messages_across_rooms(conn, "alice", limit=2)
        assert [r["msg_id"] for r in page1] == [m3, m2]
        last = page1[-1]
        page2 = db.list_messages_across_rooms(
            conn, "alice", limit=2,
            before_ts=last["created_at"], before_id=last["msg_id"],
        )
        assert [r["msg_id"] for r in page2] == [m1]

    def test_limit(self, conn):
        r1, r2, rb, ids = self._seed(conn)
        rows = db.list_messages_across_rooms(conn, "alice", limit=2)
        assert len(rows) == 2

    def test_task_enrichment_joined(self, conn):
        r1 = _room(conn, "alice", "one")
        task_id = db.create_task(conn, "the prompt", "alice", source_type="web",
                                 conversation_token=r1.token)
        db.update_task_status(conn, task_id, "completed", result="done",
                              actions_taken='["Read a.txt"]')
        mid = _msg(conn, r1.token, role="assistant", body="done", task_id=task_id)
        rows = db.list_messages_across_rooms(conn, "alice")
        row = next(r for r in rows if r["msg_id"] == mid)
        assert row["status"] == "completed"
        assert row["actions_taken"] == '["Read a.txt"]'


# ---------------------------------------------------------------------------
# mark_all_rooms_read
# ---------------------------------------------------------------------------


class TestMarkAllRoomsRead:
    def test_moves_all_member_cursors_and_counts(self, conn):
        r1 = _room(conn, "alice", "one")
        r2 = _room(conn, "alice", "two")
        rb = _room(conn, "bob", "bobs")
        a1 = _msg(conn, r1.token)
        a2 = _msg(conn, r2.token)
        bmsg = _msg(conn, rb.token)
        moved = db.mark_all_rooms_read(conn, "alice")
        assert moved == 2
        assert db.get_room_read_state(conn, r1.token, "web", "alice") == a1
        assert db.get_room_read_state(conn, r2.token, "web", "alice") == a2
        # Bob's room untouched.
        assert db.get_room_read_state(conn, rb.token, "web", "bob") == 0
        assert db.count_unread_messages(conn, r1.token, "web", "alice") == 0

    def test_second_call_moves_nothing(self, conn):
        r1 = _room(conn, "alice", "one")
        _msg(conn, r1.token)
        assert db.mark_all_rooms_read(conn, "alice") == 1
        assert db.mark_all_rooms_read(conn, "alice") == 0

    def test_empty_room_not_counted(self, conn):
        _room(conn, "alice", "empty")
        assert db.mark_all_rooms_read(conn, "alice") == 0

    def test_dismissed_room_not_moved(self, conn):
        r1 = _room(conn, "alice", "one")
        _msg(conn, r1.token)
        db.dismiss_room(conn, r1.token, "alice")
        assert db.mark_all_rooms_read(conn, "alice") == 0
