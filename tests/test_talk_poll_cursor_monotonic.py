"""The Talk poll cursor only ever moves forward.

`set_talk_poll_state` was an unconditional upsert, so any writer could move a
room's cursor *backwards*. The claim that made that look harmless — "the cursor
makes redelivery idempotent" — is false, because the cursor advances at the top
of the results loop *before* every filter, and below it sit `dispatch_command`,
`handle_confirmation_reply` with its ack post, and
`confirmations.cancel_for_conversation`. None of those is idempotent and only
`ingest_message` is deduped, so a rewind re-runs that whole window: a command
dispatched twice, an ack posted twice, a confirmation cancelled twice.

There is no legitimate rewind to preserve. Talk comment ids are global and
monotonic, and neither `clear-history` nor deleting a message resets them, so a
lower id arriving at this function is always a stale writer and never a
correction. The guard therefore lives in the SQL rather than at the call sites:
`inbound._apply_room_pass` had already grown one by hand ("is the cursor still
absent"), which is what a hazard looks like when it is fixed one caller at a
time.

The first-write case is deliberately unguarded: an INSERT has nothing to
compare against, and a room's first cursor is whatever the initialising caller
computed.
"""

import pytest

from istota import db


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    with db.get_db(path) as connection:
        yield connection


class TestTheCursorNeverMovesBackwards:
    def test_a_lower_id_leaves_the_cursor_where_it_was(self, conn):
        db.set_talk_poll_state(conn, "room1", 500)

        db.set_talk_poll_state(conn, "room1", 400)

        assert db.get_talk_poll_state(conn, "room1") == 500

    def test_a_much_lower_id_does_not_replay_the_room(self, conn):
        """The shape the fix exists for: a stale batch writing an id from
        before the room was ever polled would re-open the non-idempotent window
        between the cursor advance and task creation."""
        db.set_talk_poll_state(conn, "room1", 12345)

        db.set_talk_poll_state(conn, "room1", 1)

        assert db.get_talk_poll_state(conn, "room1") == 12345

    def test_an_equal_id_is_a_no_op(self, conn):
        db.set_talk_poll_state(conn, "room1", 500)

        db.set_talk_poll_state(conn, "room1", 500)

        assert db.get_talk_poll_state(conn, "room1") == 500


class TestTheCursorStillAdvances:
    def test_a_higher_id_moves_it(self, conn):
        db.set_talk_poll_state(conn, "room1", 500)

        db.set_talk_poll_state(conn, "room1", 501)

        assert db.get_talk_poll_state(conn, "room1") == 501

    def test_the_first_write_takes_the_value_it_was_given(self, conn):
        """No row means nothing to compare against — `latest_id - 1` on a room
        being initialised is lower than anything the room will later hold, and
        it has to land."""
        db.set_talk_poll_state(conn, "fresh", 42)

        assert db.get_talk_poll_state(conn, "fresh") == 42

    def test_each_room_is_compared_against_its_own_cursor(self, conn):
        db.set_talk_poll_state(conn, "ahead", 900)
        db.set_talk_poll_state(conn, "behind", 10)

        db.set_talk_poll_state(conn, "behind", 20)

        assert db.get_talk_poll_state(conn, "ahead") == 900
        assert db.get_talk_poll_state(conn, "behind") == 20

    def test_the_row_is_still_stamped_when_a_lower_write_is_refused(self, conn):
        """`updated_at` says when a writer last reported on the room, which is
        what an operator reads to tell a quiet room from a stalled poller. It
        moves on every write; only the id is guarded."""
        db.set_talk_poll_state(conn, "room1", 500)
        conn.execute(
            "UPDATE talk_poll_state SET updated_at = '2000-01-01 00:00:00' "
            "WHERE conversation_token = 'room1'",
        )

        db.set_talk_poll_state(conn, "room1", 400)

        row = conn.execute(
            "SELECT last_known_message_id, updated_at FROM talk_poll_state "
            "WHERE conversation_token = 'room1'",
        ).fetchone()
        assert row["last_known_message_id"] == 500
        assert row["updated_at"] != "2000-01-01 00:00:00"
