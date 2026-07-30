"""DB layer for per-message delete (ISSUE-210).

A message delete is HARD — the `messages` row is gone — so `message_deletions`
is the ledger that lets the room stream tell another open client what vanished.
Covers the delete itself (row + star cleanup + ledger entry), the visibility
scoping on the deletion tail, the O(1) gate, retention pruning, and the
room-delete cascade.
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
    return db.create_web_chat_room(conn, user_id, name)


def _msg(conn, token, *, role="assistant", body="hi", origin="web", task_id=None):
    return db.add_message(
        conn, token, role=role, body=body, origin_surface=origin, task_id=task_id,
    )


class TestDeleteMessage:
    def test_removes_the_row_and_reports_its_room(self, conn):
        room = _room(conn, "alice", "general")
        mid = _msg(conn, room.token)
        assert db.delete_message(conn, mid, "alice") == room.token
        assert db.get_message_room(conn, mid) is None

    def test_unknown_id_is_a_clean_no_op(self, conn):
        # A repeat delete (two tabs, or a retry) must not fabricate a success.
        assert db.delete_message(conn, 999_999, "alice") is None

    def test_takes_the_stars_with_it(self, conn):
        # `PRAGMA foreign_keys` is unset, so the schema's cascade is decorative
        # — a star left behind would point at nothing and count toward the
        # Starred view forever.
        room = _room(conn, "alice", "general")
        mid, kept = _msg(conn, room.token), _msg(conn, room.token)
        db.set_message_starred(conn, mid, "alice", True)
        db.set_message_starred(conn, kept, "alice", True)
        db.delete_message(conn, mid, "alice")
        rows = conn.execute("SELECT message_id FROM message_stars").fetchall()
        assert [r["message_id"] for r in rows] == [kept]

    def test_leaves_the_rest_of_the_room_alone(self, conn):
        room = _room(conn, "alice", "general")
        first, second = _msg(conn, room.token, body="one"), _msg(conn, room.token, body="two")
        db.delete_message(conn, first, "alice")
        assert db.get_message_room(conn, second) == room.token

    def test_records_who_deleted_what(self, conn):
        room = _room(conn, "alice", "general")
        mid = _msg(conn, room.token)
        db.delete_message(conn, mid, "alice")
        row = conn.execute("SELECT * FROM message_deletions").fetchone()
        assert (row["message_id"], row["room_token"], row["deleted_by"]) == (
            mid, room.token, "alice",
        )

    def test_read_cursor_is_left_alone(self, conn):
        # The cursor is a high-water mark, not a reference: a deleted id just
        # stops existing below the line. Rewinding it would resurface every
        # message after the deleted one as unread.
        room = _room(conn, "alice", "general")
        first = _msg(conn, room.token)
        last = _msg(conn, room.token)
        db.set_room_read_state(conn, room.token, "web", last, user_id="alice")
        db.delete_message(conn, first, "alice")
        assert db.get_room_read_state(conn, room.token, "web", "alice") == last


class TestDeletionTail:
    def test_gate_is_zero_until_something_is_deleted(self, conn):
        room = _room(conn, "alice", "general")
        _msg(conn, room.token)
        assert db.max_message_deletion_id(conn) == 0

    def test_gate_tracks_the_ledger(self, conn):
        room = _room(conn, "alice", "general")
        db.delete_message(conn, _msg(conn, room.token), "alice")
        first = db.max_message_deletion_id(conn)
        db.delete_message(conn, _msg(conn, room.token), "alice")
        assert db.max_message_deletion_id(conn) > first > 0

    def test_lists_deletions_after_the_cursor(self, conn):
        room = _room(conn, "alice", "general")
        a, b = _msg(conn, room.token), _msg(conn, room.token)
        db.delete_message(conn, a, "alice")
        cursor = db.max_message_deletion_id(conn)
        db.delete_message(conn, b, "alice")

        rows = db.list_message_deletions_since(conn, "alice", since_id=cursor)
        assert [r["message_id"] for r in rows] == [b]
        assert rows[0]["room_token"] == room.token

    def test_scoped_to_rooms_the_caller_is_in(self, conn):
        # A deletion frame must not disclose that a message ever existed in a
        # room the caller was never a member of.
        mine = _room(conn, "alice", "mine")
        theirs = _room(conn, "bob", "theirs")
        db.delete_message(conn, _msg(conn, mine.token), "alice")
        db.delete_message(conn, _msg(conn, theirs.token), "bob")

        rows = db.list_message_deletions_since(conn, "alice", since_id=0)
        assert [r["room_token"] for r in rows] == [mine.token]

    def test_a_dismissed_room_contributes_nothing(self, conn):
        room = _room(conn, "alice", "hidden")
        db.dismiss_room(conn, room.token, "alice")
        db.delete_message(conn, _msg(conn, room.token), "alice")
        assert db.list_message_deletions_since(conn, "alice", since_id=0) == []

    def test_shared_room_reaches_every_member(self, conn):
        room = _room(conn, "alice", "shared")
        db.add_room_member(conn, room.token, "bob")
        db.delete_message(conn, _msg(conn, room.token), "alice")
        rows = db.list_message_deletions_since(conn, "bob", since_id=0)
        assert len(rows) == 1


class TestLedgerRetention:
    def test_prunes_only_the_aged(self, conn):
        room = _room(conn, "alice", "general")
        old, recent = _msg(conn, room.token), _msg(conn, room.token)
        db.delete_message(conn, old, "alice")
        conn.execute(
            "UPDATE message_deletions SET deleted_at = datetime('now', '-60 days') "
            "WHERE message_id = ?", (old,),
        )
        db.delete_message(conn, recent, "alice")

        assert db.prune_message_deletions(conn, 30) == 1
        rows = conn.execute("SELECT message_id FROM message_deletions").fetchall()
        assert [r["message_id"] for r in rows] == [recent]

    def test_zero_keeps_forever(self, conn):
        room = _room(conn, "alice", "general")
        db.delete_message(conn, _msg(conn, room.token), "alice")
        conn.execute("UPDATE message_deletions SET deleted_at = '2000-01-01 00:00:00'")
        assert db.prune_message_deletions(conn, 0) == 0

    def test_room_delete_takes_the_ledger_with_it(self, conn):
        room = _room(conn, "alice", "doomed")
        other = _room(conn, "alice", "kept")
        db.delete_message(conn, _msg(conn, room.token), "alice")
        db.delete_message(conn, _msg(conn, other.token), "alice")

        assert db.delete_web_chat_room(conn, room.id, "alice") is True
        rows = conn.execute("SELECT room_token FROM message_deletions").fetchall()
        assert [r["room_token"] for r in rows] == [other.token]
