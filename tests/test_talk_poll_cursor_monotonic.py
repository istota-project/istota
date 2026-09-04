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

import asyncio

import pytest

from istota import db
from istota.config import Config, NextcloudConfig, SchedulerConfig, TalkConfig
from istota.transport.talk import inbound as poller


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


class TestANonIntegerIdIsRefusedRatherThanStored:
    """`MAX` compares whatever is in the column, and SQLite is not typed. A
    numeric string converts on the way in (`INTEGER` affinity), but a
    non-numeric one is stored as TEXT, sorts above every integer, and then no
    later id can ever win the comparison — the room goes deaf permanently,
    where before this guard the next poll simply overwrote the nonsense. The
    id comes off Nextcloud's JSON with no coercion in front of it."""

    def test_a_string_id_does_not_replace_the_cursor(self, conn):
        db.set_talk_poll_state(conn, "room1", 500)

        db.set_talk_poll_state(conn, "room1", "zzz")

        assert db.get_talk_poll_state(conn, "room1") == 500

    def test_a_string_id_cannot_wedge_a_fresh_room(self, conn):
        db.set_talk_poll_state(conn, "fresh", "zzz")

        assert db.get_talk_poll_state(conn, "fresh") is None
        db.set_talk_poll_state(conn, "fresh", 7)
        assert db.get_talk_poll_state(conn, "fresh") == 7

    def test_a_bool_is_not_an_id(self, conn):
        """A `bool` is an `int` in Python and would compare as 0 or 1 against a
        real cursor — the same refusal `_has_news` applies to the same field."""
        db.set_talk_poll_state(conn, "room1", 500)

        db.set_talk_poll_state(conn, "room1", True)

        assert db.get_talk_poll_state(conn, "room1") == 500

    def test_none_is_refused_rather_than_raising_inside_the_batch(self, conn):
        """The results loop guards with `if message_id:`, so this is the second
        layer — and it must not raise: the caller is inside the transaction
        that also creates the tasks, and an exception there rolls back a batch
        whose Talk posts have already gone out."""
        db.set_talk_poll_state(conn, "room1", 500)

        db.set_talk_poll_state(conn, "room1", None)

        assert db.get_talk_poll_state(conn, "room1") == 500


class TestTheCursorSeedDoesNotJumpAConcurrentWriter:
    """`_apply_room_pass` plans with the cursor absent and applies it after
    Nextcloud has answered, so another writer can initialise the room in
    between. The re-read has to decide **both** halves — what is written and
    where the room is polled from — because the seed is `latest_id - 1`, which
    is ahead of anything a second writer would have set.
    """

    def _config(self, tmp_path, db_path):
        config = Config()
        config.db_path = db_path
        config.temp_dir = tmp_path / "temp"
        config.temp_dir.mkdir(exist_ok=True)
        config.talk = TalkConfig(enabled=True, bot_username="istota")
        config.nextcloud = NextcloudConfig(
            url="https://nc.test", username="istota", app_password="pass",
        )
        config.scheduler = SchedulerConfig()
        return config

    def _plan(self):
        return poller._RoomPlan(
            conv={"token": "grp", "type": 2},
            token="grp",
            conv_type=2,
            display_name="team",
            canonical="grp",
            known_cursor=None,
            last_message_id=None,
            needs_participants=False,
            needs_cursor_init=True,
            needs_backfill=False,
            participants=[],
            latest_id=500,
        )

    def _poll_start(self, conn, config):
        """The id `_apply_room_pass` decided to poll this room from."""
        seen = []

        async def fake_poll(_client, token, last_message_id, _timeout):
            seen.append(last_message_id)
            return token, []

        original = poller._poll_single_conversation
        poller._poll_single_conversation = fake_poll
        try:
            tasks, _gated = poller._apply_room_pass(
                conn, config, object(), [self._plan()], full_sweep=True,
            )
            for coro in tasks:
                asyncio.run(coro)
        finally:
            poller._poll_single_conversation = original
        return seen

    def test_a_room_with_no_cursor_is_polled_from_the_seed(self, tmp_path, conn):
        config = self._config(tmp_path, tmp_path / "unused.db")

        assert self._poll_start(conn, config) == [499]
        assert db.get_talk_poll_state(conn, "grp") == 499

    def test_a_cursor_written_since_the_plan_wins_over_the_seed(
        self, tmp_path, conn,
    ):
        """Guarding only the write leaves the poll starting at 499 and the
        results loop advancing the stored cursor over 6..499 — the messages are
        lost, and nothing says so."""
        config = self._config(tmp_path, tmp_path / "unused.db")
        db.set_talk_poll_state(conn, "grp", 5)

        assert self._poll_start(conn, config) == [5]
        assert db.get_talk_poll_state(conn, "grp") == 5
