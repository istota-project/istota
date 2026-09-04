"""Two database-writing coroutines on the runtime loop never overlap.

**The hazard is a deadlock, not a data race**, and the difference decides what
this file asserts. `poll_talk_conversations`' results block opens `db.get_db`,
writes (which takes the WAL write lock), and then awaits Nextcloud up to five
times with that lock still held. `db.get_db` is synchronous `sqlite3` — so a
second coroutine scheduled during one of those awaits, opening its own write
transaction, blocks **the loop thread** on a lock held by a coroutine that can
only resume on that same thread. Nothing makes progress: every watcher, every
Talk delivery and the poll itself stall for the whole busy timeout, and the
second writer then raises `OperationalError`.

That second writer arrives with the signaling event stream (the drain), which
is why this lands before it. The invariant is enforced by one `asyncio.Lock`
per loop in `transport/talk/_db_lock.py`, taken around every `db.get_db` block
reachable from the loop.

**What is asserted, and what was deliberately not.** An earlier draft of this
test asserted that both coroutines ran on the async-runtime loop thread. That
is the wrong property and it passes against the racy code: co-residency on a
loop is exactly what does not prevent interleaving across an `await`. The
property is **non-overlap**, instrumented at the connection factory — the open
intervals must be disjoint.

**Why the scenario runs in its own thread with a deadline.** The plausible
wrong fix is a `threading.Lock`, which blocks the loop thread and reproduces
the very deadlock it was added to prevent. A deadlocked loop cannot service
`asyncio.wait_for`, so the timeout has to come from outside the loop: the
scenario runs on its own thread and the test fails if that thread does not
finish. Recorded negative controls: removing the lock fails with
`sqlite3.OperationalError: database is locked`; replacing it with a
`threading.Lock` fails on this deadline.
"""

import asyncio
import contextlib
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest

from istota import db
from istota.config import (
    Config,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.transport.talk import inbound as poller
from istota.transport.talk._db_lock import talk_db
from istota.transport.talk.inbound import poll_talk_conversations

# The scenario is sub-second when the invariant holds and bounded by the
# sqlite busy timeout when it does not. A threading lock hangs for ever, and
# this is what turns that into a failure instead of a hung session.
_SCENARIO_DEADLINE_SECONDS = 30.0


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
def config(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    config = Config()
    config.db_path = path
    config.temp_dir = tmp_path / "temp"
    config.temp_dir.mkdir(exist_ok=True)
    config.talk = TalkConfig(enabled=True, bot_username="istota")
    config.nextcloud = NextcloudConfig(
        url="https://nc.test", username="istota", app_password="pass",
    )
    config.users = {"alice": UserConfig()}
    config.scheduler = SchedulerConfig()

    # A room the poll pass has nothing cold to do for: registered, bound,
    # cursor set, history cached. That keeps the room pass off the network so
    # the only await inside an open write transaction is the results block's
    # participant fetch — the one the drain has to be scheduled during.
    with db.get_db(path) as conn:
        db.register_room(conn, "grp", "alice", origin="talk", name="team")
        db.add_room_binding(conn, "grp", "talk", "grp")
        db.add_room_member(conn, "grp", "alice")
        db.set_talk_poll_state(conn, "grp", 50)
        db.upsert_talk_messages(conn, "grp", [_msg(msg_id=50)])
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


class _Opens:
    """Every `db.get_db` block opened during the scenario, as an interval.

    The connection factory rather than a thread id: an interval is what says
    two writers overlapped, and a thread id is true of both the safe and the
    unsafe arrangement.
    """

    def __init__(self):
        self.records: list[dict] = []

    def install(self, monkeypatch):
        real = db.get_db

        @contextlib.contextmanager
        def recording(db_path, **kwargs):
            record = {
                "opened": time.monotonic(),
                "closed": None,
                "busy_timeout_ms": kwargs.get("busy_timeout_ms"),
            }
            self.records.append(record)
            try:
                with real(db_path, **kwargs) as conn:
                    yield conn
            finally:
                record["closed"] = time.monotonic()

        monkeypatch.setattr(db, "get_db", recording)

    def overlaps(self) -> list[tuple[dict, dict]]:
        """Every pair of connections whose open windows intersect.

        A record with no `closed` stamp is a connection still open, which
        overlaps everything after it — the honest answer, and better than the
        `TypeError` a comparison against `None` would raise from inside the
        helper that is meant to be diagnosing the overlap.
        """
        def closed(record):
            return record["closed"] if record["closed"] is not None else float("inf")

        out = []
        for i, a in enumerate(self.records):
            for b in self.records[i + 1:]:
                if a["opened"] < closed(b) and b["opened"] < closed(a):
                    out.append((a, b))
        return out


def _run_with_deadline(factory, deadline=_SCENARIO_DEADLINE_SECONDS):
    """Run one coroutine on its own loop, on its own thread, or fail.

    A loop thread blocked on a synchronous lock cannot time itself out, so the
    deadline lives out here. The thread is a daemon: under the `threading.Lock`
    control it never returns, and the run has to be able to end anyway.

    **A fired deadline leaves that thread alive for the rest of the session**,
    holding an open connection to this test's `tmp_path` database and still
    calling the recording wrapper after `monkeypatch` has restored `db.get_db`.
    Nothing can be done about it — a thread blocked in C cannot be killed from
    Python — so treat a failure here as poisoning the worker and re-run the file
    on its own (`-n0`) rather than reading the tests after it.
    """
    outcome: dict = {}

    def runner():
        try:
            outcome["value"] = asyncio.run(factory())
        except BaseException as exc:  # noqa: BLE001 — re-raised on the test thread
            outcome["error"] = exc

    thread = threading.Thread(target=runner, daemon=True, name="talk-db-lock-scenario")
    thread.start()
    thread.join(deadline)
    if thread.is_alive():
        pytest.fail(
            f"the runtime loop did not finish within {deadline}s — a database "
            "write on the loop blocked the loop thread itself"
        )
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _scenario(config):
    """One poll cycle, with a second writer scheduled inside its results
    transaction — the shape the signaling drain will have."""

    async def run():
        inside_the_results_txn = asyncio.Event()
        drained = asyncio.Event()

        async def _participants(_token):
            # Reached from the results block, with the write transaction open
            # and the WAL write lock already taken by the message upsert above
            # it. Yielding here is what schedules the drain.
            inside_the_results_txn.set()
            await asyncio.sleep(0.2)
            return [
                {"actorId": "alice", "actorType": "users"},
                {"actorId": "bob", "actorType": "users"},
                {"actorId": "istota", "actorType": "users"},
            ]

        async def poll_side():
            with patch(
                "istota.transport.talk.inbound.get_talk_client"
            ) as MockClient:
                instance = MockClient.return_value
                instance.list_conversations = AsyncMock(return_value=[{
                    "token": "grp",
                    "type": 2,
                    "name": "team",
                    "displayName": "team",
                    "lastMessage": {"id": 100},
                }])
                instance.poll_messages = AsyncMock(return_value=[_msg()])
                instance.send_message = AsyncMock()
                instance.get_participants = _participants
                instance.fetch_chat_history = AsyncMock(return_value=[])
                instance.get_latest_message_id = AsyncMock(return_value=100)
                return await poll_talk_conversations(config)

        async def drain_side():
            await asyncio.wait_for(inside_the_results_txn.wait(), timeout=10)
            async with talk_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "drain_room", 999)
            drained.set()

        await asyncio.gather(poll_side(), drain_side())
        assert drained.is_set()

    return run


class TestTheLoopNeverHoldsTwoWriteTransactionsAtOnce:
    def test_the_poll_and_a_second_writer_do_not_overlap(
        self, config, monkeypatch,
    ):
        opens = _Opens()
        opens.install(monkeypatch)

        _run_with_deadline(_scenario(config))

        # The discriminator: this ran the real poll path and a real second
        # writer, so "no overlap" is not "nothing opened a connection".
        assert len(opens.records) >= 3, opens.records
        assert not opens.overlaps(), (
            "two database connections were open at once on the runtime loop — "
            "the second writer can only resume the first one's thread"
        )

        with db.get_db(config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "drain_room") == 999
            assert db.get_talk_poll_state(conn, "grp") == 100

    def test_the_polls_own_transactions_bound_their_lock_wait(
        self, config, monkeypatch,
    ):
        """The lock orders the two coroutines on the loop; the busy timeout is
        what keeps a writer *outside* it — the scheduler's threads, the web
        process — from turning into a 30-second stall of the loop thread. It has
        to surface as an `OperationalError` the caller can retry instead.

        Scoped to what the scenario opens, which is this poll's three blocks and
        the drain's: an out-of-loop writer's connection is a different call site
        in a different process and is not what this can speak for."""
        opens = _Opens()
        opens.install(monkeypatch)

        _run_with_deadline(_scenario(config))

        assert opens.records
        for record in opens.records:
            assert record["busy_timeout_ms"] is not None, (
                "a connection on the runtime loop kept the default 30s lock "
                "wait, which is the stall this bounds"
            )
            assert 0 < record["busy_timeout_ms"] <= 5000


def _real_drain_scenario(config):
    """The same shape with the **real** drain as the second writer.

    `_scenario` stands the drain in with a bare `talk_db` write, which was the
    honest instrument while the drain did not exist. It does now, and its write
    path is longer than that stand-in: `poll_one_conversation` opens a
    connection to read the cursor, closes it, fetches, and then opens the
    results transaction — three scopes rather than one, any of which could have
    been left outside the lock.
    """

    async def run():
        inside_the_results_txn = asyncio.Event()
        drained = asyncio.Event()

        async def _participants(_token):
            inside_the_results_txn.set()
            await asyncio.sleep(0.2)
            return [
                {"actorId": "alice", "actorType": "users"},
                {"actorId": "bob", "actorType": "users"},
                {"actorId": "istota", "actorType": "users"},
            ]

        client = AsyncMock()
        client.list_conversations = AsyncMock(return_value=[{
            "token": "grp", "type": 2, "name": "team", "displayName": "team",
            "lastMessage": {"id": 100},
        }])
        client.poll_messages = AsyncMock(return_value=[_msg()])
        client.send_message = AsyncMock()
        client.get_participants = _participants
        client.fetch_chat_history = AsyncMock(return_value=[])
        client.get_latest_message_id = AsyncMock(return_value=100)

        async def poll_side():
            with patch(
                "istota.transport.talk.inbound.get_talk_client",
                return_value=client,
            ):
                return await poll_talk_conversations(config)

        async def drain_side():
            await asyncio.wait_for(inside_the_results_txn.wait(), timeout=10)
            drain_client = AsyncMock()
            drain_client.poll_messages = AsyncMock(
                return_value=[_msg(msg_id=200, message="second room")],
            )
            drain_client.get_participants = AsyncMock(return_value=[])
            drain_client.send_message = AsyncMock()
            await poller.poll_one_conversation(
                config, "drain_room", conv_type=1, display_name="dm",
                client=drain_client,
            )
            drained.set()

        await asyncio.gather(poll_side(), drain_side())
        assert drained.is_set()

    return run


class TestTheRealDrainDoesNotOverlapThePoll:
    """The second writer this lock was added for, driven end to end."""

    def test_the_drain_and_the_poll_never_hold_a_connection_together(
        self, config, monkeypatch,
    ):
        opens = _Opens()
        opens.install(monkeypatch)

        _run_with_deadline(_real_drain_scenario(config))

        # Four at least: the poll's room pass and results block, plus the
        # drain's cursor read and its own results block.
        assert len(opens.records) >= 4, opens.records
        assert not opens.overlaps(), (
            "the signaling drain held a database connection while the poll's "
            "results transaction was open — the drain can only resume on the "
            "thread the poll is blocking"
        )

        with db.get_db(config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "drain_room") == 200
            assert db.get_talk_poll_state(conn, "grp") == 100


class TestTheLockIsPerLoopAndAsync:
    async def test_a_second_loop_gets_its_own_lock(self, config):
        """`asyncio.Lock` binds to the first loop that contends on it and
        raises for every loop after that. One lock per loop is what keeps the
        invariant — which is about one loop's own thread — from becoming a
        hazard of its own in a process that runs more than one loop over time.
        The suite is such a process: every `asyncio.run` is a new loop.

        **Asserted as lock identity, not as an absent `RuntimeError`.** A lock
        binds itself on the first *contended* acquire — `Lock.acquire`'s
        uncontended fast path never asks for the loop — so a sequential
        two-loop scenario passes against a single module-level lock and proves
        nothing. Two loops getting two objects is the property, and it is what
        a single lock fails."""
        from istota.transport.talk import _db_lock

        mine = _db_lock.loop_db_lock()
        assert _db_lock.loop_db_lock() is mine, "one loop, one lock"

        theirs: list = []

        def other_loop():
            async def write():
                theirs.append(_db_lock.loop_db_lock())
                async with talk_db(config.db_path) as conn:
                    db.set_talk_poll_state(conn, "loop_b", 2)
            asyncio.run(write())

        await asyncio.wait_for(asyncio.to_thread(other_loop), timeout=10)

        assert theirs and theirs[0] is not mine, (
            "a second loop was handed the first loop's lock — it binds to the "
            "loop that first contends on it and raises for every loop after"
        )
        async with talk_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "loop_a", 1)
        with db.get_db(config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "loop_a") == 1
            assert db.get_talk_poll_state(conn, "loop_b") == 2

    async def test_the_lock_is_an_asyncio_lock(self):
        """Named rather than implied: a `threading.Lock` here blocks the loop
        thread, which is the deadlock the lock exists to prevent."""
        from istota.transport.talk import _db_lock

        assert isinstance(_db_lock.loop_db_lock(), asyncio.Lock)

    def test_asking_for_the_lock_off_a_loop_raises(self):
        """There is no invariant to enforce for a caller that is not on a
        loop, and handing one something acquirable would let it believe it had
        taken the lock the loop's coroutines are ordered by."""
        from istota.transport.talk import _db_lock

        with pytest.raises(RuntimeError):
            _db_lock.loop_db_lock()
