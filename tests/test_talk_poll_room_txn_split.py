"""The room loop does not hold the framework write lock across Nextcloud
(ISSUE-406).

`poll_talk_conversations` used to run its whole room pass inside one
`db.get_db` block: read the registry, write to it, await Nextcloud, write
again. `db.get_db` commits at the end of the `with` and SQLite's deferred
transaction becomes a *writer* at the first write, so every await after that
first write was a WAL write lock held across a round trip, and every other
writer in the daemon queued behind it for up to the 30s lock wait.

The measurement (`tests/test_talk_poll_txn_instrumentation.py`) established
that this recurs rather than being first-encounter work: a group room nobody
has written in fails both the cursor guard and the history-cache guard on every
cycle, for ever, and pays two round trips each time.

This file covers the fix for that half — read, close, await, reopen to write.
The results loop is deliberately untouched: `poll_talk_conversations`' own
docstring records that `ingest_message` has to commit in the same transaction
as `set_talk_poll_state`, so that block cannot be split the same way.

**Why the assertions are positive rather than an empty list.** `_report_poll_txn`
says nothing about a transaction that neither waited nor ran long, so after the
fix the natural assertion is that no `phase=rooms` line appears — which is
equally true of a poll that never ran, the no-op-indistinguishable-from-success
shape `.claude/rules/testbed.md` records. So the hold-warn threshold is pinned
to 0.0, which makes every transaction emit, and the assertion is on `awaits=0`
in a line that exists.
"""

import asyncio
import logging

import pytest
from unittest.mock import AsyncMock, patch

from istota import db
from istota.config import (
    Config,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.transport.talk import inbound as poller
from istota.transport.talk.inbound import poll_talk_conversations


@pytest.fixture(autouse=True)
def _reset_poller_caches():
    poller._participant_cache.clear()
    poller._conversation_cache = None
    poller._dm_token_cache.clear()
    poller._last_full_sweep = None
    yield
    poller._participant_cache.clear()
    poller._conversation_cache = None
    poller._dm_token_cache.clear()
    poller._last_full_sweep = None


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path, tmp_path):
    config = Config()
    config.db_path = db_path
    config.temp_dir = tmp_path / "temp"
    config.temp_dir.mkdir(exist_ok=True)
    config.talk = TalkConfig(enabled=True, bot_username="istota")
    config.nextcloud = NextcloudConfig(
        url="https://nc.test", username="istota", app_password="pass",
    )
    config.users = {"alice": UserConfig()}
    config.scheduler = SchedulerConfig()
    return config


def _msg(msg_id=100, actor_id="alice", message="hello"):
    return {
        "id": msg_id,
        "actorId": actor_id,
        "actorType": "users",
        "message": message,
        "messageType": "comment",
        "messageParameters": {},
        "timestamp": 1700000000,
    }


class _Calls:
    """What the network was actually asked for, so an empty log line list can be
    read as "held nothing" rather than as "did nothing"."""

    def __init__(self):
        self.participants = 0
        self.latest = 0
        self.history = 0


async def _poll(config, *, conversations, messages, participants=None,
                history=None, latest_id=None, delay=0.0, calls=None,
                on_latest=None):
    """One poll cycle with every network call's cost under the test's control."""
    calls = calls if calls is not None else _Calls()

    async def _participants(_token):
        calls.participants += 1
        if delay:
            await asyncio.sleep(delay)
        return participants or []

    async def _history(_token, **_kw):
        calls.history += 1
        if delay:
            await asyncio.sleep(delay)
        return history or []

    async def _latest(_token):
        calls.latest += 1
        if delay:
            await asyncio.sleep(delay)
        if on_latest is not None:
            on_latest()
        return latest_id

    with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
        instance = MockClient.return_value
        instance.list_conversations = AsyncMock(return_value=conversations)
        instance.poll_messages = AsyncMock(return_value=messages)
        instance.send_message = AsyncMock()
        instance.get_participants = _participants
        instance.fetch_chat_history = _history
        instance.get_latest_message_id = _latest
        return await poll_talk_conversations(config)


def _txn_lines(caplog, phase):
    out = []
    for r in caplog.records:
        if r.name != "istota.transport.talk.txn":
            continue
        msg = r.getMessage()
        if not msg.startswith("talk_poll_txn "):
            continue
        fields = dict(p.split("=", 1) for p in msg.split()[1:])
        if fields["phase"] == phase:
            out.append(fields)
    return out


class TestTheRoomWriteTransactionHoldsNoAwait:
    async def test_an_empty_group_room_pays_no_lock_time_for_its_round_trips(
        self, config, caplog,
    ):
        """The recurring case, and the one that motivated the restructure.

        An empty group room fails the cursor guard and the history-cache guard
        on every cycle for ever, so it does two round trips per cycle. They now
        happen with no connection open; the write transaction that follows sees
        none of them.

        Three cycles, matching the measurement this replaces — one proves
        nothing about recurrence.
        """
        calls = _Calls()
        with patch.object(poller, "_TXN_HOLD_WARN_SECONDS", 0.0):
            with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
                for _ in range(3):
                    poller._participant_cache.clear()
                    await _poll(
                        config,
                        conversations=[{"token": "empty", "type": 2, "name": "team"}],
                        messages=[],
                        participants=[{"actorId": "alice"}],
                        latest_id=None,
                        history=[],
                        delay=0.03,
                        calls=calls,
                    )

        rooms = _txn_lines(caplog, "rooms")
        assert len(rooms) == 3, "the room write transaction stopped being measured"
        for fields in rooms:
            assert int(fields["awaits"]) == 0, (
                "the room write transaction is still holding a network await"
            )
            assert int(fields["await_ms"]) == 0

        # The discriminator: the round trips did happen, so `awaits=0` above is
        # the lock being free rather than the poll doing nothing.
        assert calls.latest == 3
        assert calls.history == 3

    async def test_a_first_encounter_room_holds_nothing_either(
        self, config, caplog,
    ):
        """The cold path — the three awaits ISSUE-406 names — on a room that
        registers, initializes a cursor and backfills history in one cycle."""
        calls = _Calls()
        with patch.object(poller, "_TXN_HOLD_WARN_SECONDS", 0.0):
            with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
                await _poll(
                    config,
                    conversations=[{"token": "fresh", "type": 2, "name": "team"}],
                    messages=[],
                    participants=[{"actorId": "alice"}],
                    latest_id=500,
                    history=[_msg(msg_id=499)],
                    delay=0.03,
                    calls=calls,
                )

        rooms = _txn_lines(caplog, "rooms")
        assert rooms, "the room write transaction was not measured"
        assert int(rooms[-1]["awaits"]) == 0
        assert calls.participants == 1
        assert calls.latest == 1
        assert calls.history == 1


class TestTheWorkStillLands:
    async def test_a_first_encounter_room_is_registered_seeded_and_backfilled(
        self, config,
    ):
        """The regression guard for the split: every write the single-transaction
        version made is still made, from the phase that reopens the connection.
        """
        await _poll(
            config,
            conversations=[{"token": "fresh", "type": 2, "name": "team"}],
            messages=[],
            participants=[
                {"actorId": "alice", "actorType": "users"},
                {"actorId": "istota", "actorType": "users"},
            ],
            latest_id=500,
            history=[_msg(msg_id=499)],
        )

        with db.get_db(config.db_path) as conn:
            room = db.get_room(conn, "fresh")
            assert room is not None, "the room was not registered"
            assert room.origin == "talk"
            assert room.name == "team"
            assert db.get_room_binding(conn, "fresh", "talk") is not None
            assert db.is_room_member(conn, "fresh", "alice")
            # latest_id - 1, so the next poll still returns the newest message.
            assert db.get_talk_poll_state(conn, "fresh") == 499
            assert db.has_cached_talk_messages(conn, "fresh")

    async def test_a_room_whose_name_changed_is_renamed(self, config):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk", name="old")
            db.add_room_binding(conn, "room1", "talk", "room1")
            db.set_talk_poll_state(conn, "room1", 50)
            db.upsert_talk_messages(conn, "room1", [_msg(msg_id=1)])

        await _poll(
            config,
            conversations=[{"token": "room1", "type": 2, "displayName": "new"}],
            messages=[],
        )

        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, "room1").name == "new"

    async def test_a_failed_cursor_init_skips_the_room_and_not_the_cycle(
        self, config,
    ):
        """The `continue` the single-transaction version had: a room whose
        `get_latest_message_id` raised is passed over for this cycle, and the
        room beside it is unaffected."""

        async def _latest(token):
            if token == "bad":
                raise RuntimeError("nextcloud said no")
            return 500

        with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
            instance = MockClient.return_value
            instance.list_conversations = AsyncMock(return_value=[
                {"token": "bad", "type": 2, "name": "bad"},
                {"token": "good", "type": 2, "name": "good"},
            ])
            instance.poll_messages = AsyncMock(return_value=[])
            instance.send_message = AsyncMock()
            instance.get_participants = AsyncMock(
                return_value=[{"actorId": "alice", "actorType": "users"}],
            )
            instance.fetch_chat_history = AsyncMock(return_value=[_msg(msg_id=499)])
            instance.get_latest_message_id = _latest
            await poll_talk_conversations(config)

        with db.get_db(config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "bad") is None
            assert db.get_talk_poll_state(conn, "good") == 499
            # The failed room is passed over before its backfill, exactly as the
            # single-transaction `continue` did.
            assert not db.has_cached_talk_messages(conn, "bad")
            assert db.has_cached_talk_messages(conn, "good")


class TestTheSplitDoesNotClobber:
    async def test_a_cursor_written_during_the_fetch_is_not_rewound(self, config):
        """The one hazard the split introduces, and the guard for it.

        The read phase saw no cursor and the write phase acts on that. Between
        them the connection is closed and the lock is free — which is the whole
        point — so another writer can advance the cursor while Nextcloud is
        answering. Writing `latest_id - 1` over it would rewind the room and
        re-poll messages that had already been read, so the write phase
        re-reads and only initializes a cursor that is still absent.

        The short `busy_timeout_ms` is what keeps this test fast against a
        single-transaction poller, where the inner write blocks on the lock the
        poller is holding rather than succeeding.
        """
        def _other_writer():
            with db.get_db(config.db_path, busy_timeout_ms=200) as conn:
                db.set_talk_poll_state(conn, "fresh", 999)

        await _poll(
            config,
            conversations=[{"token": "fresh", "type": 2, "name": "team"}],
            messages=[],
            participants=[{"actorId": "alice", "actorType": "users"}],
            latest_id=500,
            history=[_msg(msg_id=499)],
            on_latest=_other_writer,
        )

        with db.get_db(config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "fresh") == 999, (
                "the poller rewound a cursor another writer had advanced"
            )
