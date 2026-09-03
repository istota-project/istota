"""What `poll_talk_conversations` does to the framework database while it waits
on Nextcloud (ISSUE-406).

The poller opens a synchronous `sqlite3` connection and awaits the network
inside it. `db.get_db` commits at the end of the `with`, and SQLite's default
deferred transaction starts at the first write — so any write in the room loop
takes a WAL write lock that is then held across every later await in the same
block. Every other writer in the daemon queues behind it for as long as
Nextcloud takes to answer.

That much is a reading of the code. Whether it *happens*, and for how long, was
inference when the issue was filed, and the issue says so: measure first, and
decide the restructure on the numbers. This file covers the measurement.

**The room half has since been fixed and its cases moved.** The measurement
established what the issue could not: the room loop's awaits recur rather than
being first-encounter work, so an empty group room paid two round trips of lock
time on every cycle for ever. That block is now read → close → await → reopen
to write, and `tests/test_talk_poll_room_txn_split.py` is where the room pass is
covered — including the recurrence finding, which it keeps by counting the round
trips rather than by measuring the lock they no longer hold. What is left here is
the `results` block, which cannot be split the same way (`ingest_message` has to
commit in the same transaction as the cursor advance) and so still holds every
await this instrument was built to count.

**These tests assert on a log line because the log line is the deliverable.**
`talk_poll_txn` is a data format, not chatter: fixed key order, `key=value`, on
its own logger, so a series can be pulled out of the journal whole and lined up
against the `ReadTimeout` records the issue is trying to explain. A rename is a
breaking change.

**These do not use `fake_talk`**, which is otherwise the rule for anything
patching `get_talk_client` (`.claude/rules/testbed.md`). The subject here is how
long a call takes, so each method needs a delay under the test's control, and
the double has no way to express one; nothing here asserts on a token, so the
misroute the double exists to catch is not in scope. Adding per-method delays to
the double for this would be the better answer if a second file ever wants them.
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


async def _poll(config, *, conversations, messages, participants=None,
                participants_delay=0.0, history=None, history_delay=0.0,
                latest_id=None, latest_delay=0.0):
    """Drive one poll cycle with each network call's cost under the test's control."""

    async def _participants(_token):
        if participants_delay:
            await asyncio.sleep(participants_delay)
        return participants or []

    async def _history(_token, **_kw):
        if history_delay:
            await asyncio.sleep(history_delay)
        return history or []

    async def _latest(_token):
        if latest_delay:
            await asyncio.sleep(latest_delay)
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


def _txn_lines(caplog):
    return [
        r for r in caplog.records
        if r.name == "istota.transport.talk.txn"
        and r.getMessage().startswith("talk_poll_txn ")
    ]


def _fields(record) -> dict[str, str]:
    parts = record.getMessage().split()[1:]
    return dict(p.split("=", 1) for p in parts)


def _settled_room(config, token="room1"):
    """A room the poller has seen before: registered, with a cursor and cached
    history.

    All three matter. Without the registry row the room loop takes the
    first-sight branch and fetches participants *there*, which warms the cache
    and leaves the results loop with nothing to wait for — so a test meaning to
    measure the results loop measures the room loop instead and reads 0ms.
    """
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, token, "alice", origin="talk", name="team")
        db.add_room_binding(conn, token, "talk", token)
        db.set_talk_poll_state(conn, token, 50)
        db.upsert_talk_messages(conn, token, [_msg(msg_id=1)])


class TestTheHoldIsRecorded:
    async def test_a_network_call_inside_the_results_transaction_is_named(
        self, config, caplog,
    ):
        """The steady-state exposure, and the only one that recurs.

        The three awaits in the room loop are all first-encounter work. This one
        — the participant fetch that decides whether a room needs an @mention —
        runs per message on a TTL of five minutes, inside a transaction that
        `upsert_talk_messages` and `set_talk_poll_state` have already made a
        writer.
        """
        _settled_room(config)

        with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
            await _poll(
                config,
                conversations=[{"token": "room1", "type": 2, "name": "team"}],
                messages=[_msg(msg_id=100)],
                participants=[{"actorId": "a"}, {"actorId": "b"}, {"actorId": "c"}],
                participants_delay=0.05,
            )

        lines = _txn_lines(caplog)
        assert lines, "the transaction held a network await and said nothing"
        fields = _fields(lines[-1])
        assert fields["phase"] == "results"
        assert int(fields["awaits"]) >= 1
        assert int(fields["await_ms"]) >= 40
        # The hold is at least as long as the wait inside it, which is the whole
        # claim: the connection was open for the duration of the round trip.
        assert int(fields["held_ms"]) >= int(fields["await_ms"])

    async def test_a_long_hold_is_a_warning_and_a_short_one_is_not(
        self, config, caplog,
    ):
        """An operator reads warnings; a series is read out of the journal on
        purpose. Both exist, and the threshold is what separates them."""
        _settled_room(config)

        with patch.object(poller, "_TXN_HOLD_WARN_SECONDS", 0.02):
            with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
                await _poll(
                    config,
                    conversations=[{"token": "room1", "type": 2, "name": "team"}],
                    messages=[_msg(msg_id=100)],
                    participants=[{"actorId": "a"}],
                    participants_delay=0.05,
                )
        results = [r for r in _txn_lines(caplog)
                   if _fields(r)["phase"] == "results"]
        assert [r.levelno for r in results] == [logging.WARNING]

        caplog.clear()
        poller._participant_cache.clear()
        with patch.object(poller, "_TXN_HOLD_WARN_SECONDS", 30.0):
            with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
                await _poll(
                    config,
                    conversations=[{"token": "room1", "type": 2, "name": "team"}],
                    messages=[_msg(msg_id=101)],
                    participants=[{"actorId": "a"}],
                    participants_delay=0.05,
                )
        assert [_fields(r)['phase'] for r in _txn_lines(caplog)
                if r.levelno == logging.WARNING] == []


class TestTheQuietCaseSaysNothing:
    async def test_a_cache_hit_is_not_reported_as_a_round_trip(
        self, config, caplog,
    ):
        """`_get_participants` is awaited whether or not it goes to the network,
        so counting entries rather than round trips would put a line on every
        cycle of every busy room and drown the ones that mean something.

        The control is the first half: the same poll with a cold cache does
        report, so an empty second half is the cache and not a broken fixture.
        """
        _settled_room(config)

        conversations = [{"token": "room1", "type": 2, "name": "team"}]
        with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
            await _poll(
                config, conversations=conversations, messages=[_msg(msg_id=100)],
                participants=[{"actorId": "a"}], participants_delay=0.05,
            )
        assert _txn_lines(caplog), "cold cache must report, or the control is dead"

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
            await _poll(
                config, conversations=conversations, messages=[_msg(msg_id=101)],
                participants=[{"actorId": "a"}], participants_delay=0.05,
            )
        assert _txn_lines(caplog) == []

    async def test_many_cache_hits_do_not_sum_into_a_round_trip(
        self, config, caplog,
    ):
        """Why the floor is per await rather than on the total.

        `_get_participants` is awaited once per message. At 25µs a warm hit,
        a couple of hundred messages sum past any floor worth setting, and a
        total-based rule would then report a transaction that never touched the
        network — on the busiest rooms, which are exactly the ones an
        investigator would look at first.

        The sibling above is the positive control: same shape, cold cache, one
        message, and it does report.
        """
        _settled_room(config)
        conversations = [{"token": "room1", "type": 2, "name": "team"}]

        # Warm the cache, and take the line that produces out of the way.
        await _poll(
            config, conversations=conversations, messages=[_msg(msg_id=100)],
            participants=[{"actorId": "a"}], participants_delay=0.05,
        )

        with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
            await _poll(
                config, conversations=conversations,
                messages=[_msg(msg_id=200 + i) for i in range(300)],
                participants=[{"actorId": "a"}],
            )

        results = [ln for ln in _txn_lines(caplog)
                   if _fields(ln)["phase"] == "results"]
        assert results == [], "300 cache hits were reported as network waiting"

    async def test_a_poll_with_nothing_to_do_reports_nothing(self, config, caplog):
        """`_txn_lines == []` is equally true of a poll that never ran, so the
        second assertion is what makes the first one mean "quiet"."""
        _settled_room(config)

        with caplog.at_level(logging.INFO, logger="istota.transport.talk.txn"):
            await _poll(
                config,
                conversations=[{"token": "room1", "type": 1, "name": "alice"}],
                messages=[_msg(msg_id=100)],
            )

        assert _txn_lines(caplog) == []
        with db.get_db(config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "room1") == 100


class TestTheReporterNeverRaises:
    def test_a_broken_clock_costs_a_line_and_not_the_poll(self):
        """One caller is the daemon's busiest loop and the call sits in a
        `finally`, where a raise would replace whatever the block was already
        propagating.

        The `assert` is the whole point: without it the test passes if the
        emission rule ever stops reaching the logger for this hold, at which
        point it exercises the `except` it is named for not at all — the
        no-op-indistinguishable-from-success shape `.claude/rules/testbed.md`
        records.
        """
        hold = poller._TxnHold(label="results", opened=0.0)
        hold.awaits = 1
        hold.await_seconds = 1.0

        with patch.object(
            poller._POLL_TXN_LOGGER, "warning", side_effect=RuntimeError("boom"),
        ) as warn:
            poller._report_poll_txn(hold, held_seconds=99.0)

        assert warn.call_count == 1
