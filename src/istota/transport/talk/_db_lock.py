"""One database write transaction at a time on the async-runtime loop.

**The hazard is a deadlock, not a data race**, and the obvious framing is the
wrong one. `poll_talk_conversations`' results block opens `db.get_db`, writes —
which under WAL turns the deferred transaction into a *writer* and takes the
write lock — and then awaits Nextcloud up to five times with that lock held.
`db.get_db` is synchronous `sqlite3`. So a second coroutine scheduled during
one of those awaits, opening its own write transaction, blocks **the loop
thread** on a lock held by a coroutine that can only resume on that same
thread. Nothing on the loop makes progress until the busy timeout expires:
every Talk delivery, every signaling watcher and the poll itself stall, and the
second writer then raises `OperationalError` anyway. The neighbouring shapes are
already treated as real elsewhere — `db.get_db`'s own docstring bounds the
dispatch loop's scans for the thread-blocking version of it, and
`notifications._MIRROR_LOCK_WAIT_MS` is 250 ms because "a caller holding a
transaction is a stall on whatever thread it runs on".

**The scope is `transport/talk`, and the claim is not wider than that.** These
are the loop-reachable database blocks *here*; the loop's other residents are
covered by not doing this at all — `WebTransport.deliver` hands its write to
`loop.run_in_executor`, so it contends with the poll through SQLite from another
thread, which is what the busy timeout below is for. A coroutine that opens a
connection on the loop thread itself belongs behind this lock, and there is
currently none outside this package.

Today there is one writer on the loop, so the invariant holds by there being
nobody to break it — `run_coro` blocks its submitting thread and `_talk_poll_loop`
is the only caller. The signaling event stream's drain is the second, which is
why this lands before it rather than with it.

**It must be an `asyncio.Lock`, and a `threading.Lock` is the plausible wrong
fix.** Awaiting an asyncio lock yields to the loop, so the coroutine holding
the transaction gets to finish it. A threading lock blocks the loop thread,
which is precisely the deadlock the lock is here to prevent, in one line and
with no error message.

**One lock per loop, keyed on the running loop.** The invariant is about a
single loop's thread; two loops are two threads, where SQLite's own busy
timeout is the only mechanism there can be and a bounded one is the whole
remedy. A single module-level lock would also be a hazard rather than a
tightening: `asyncio.Lock` binds itself to the first loop that *contends* on it
and raises `RuntimeError` for every loop after that, which is a live case in
any process that runs more than one loop over its life — the suite, where each
`asyncio.run` is a new loop, and the daemon's own `reset_async_runtime`.

The map is a `WeakKeyDictionary` so a closed loop's lock goes with it, and the
plain `threading.Lock` around it guards a dict lookup only. It is never held
across an await, and it is not the transport lock: taking it out would leave
two threads able to mint two locks for one loop.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from ... import db

# How long a connection on the loop waits for a writer *outside* the loop —
# the scheduler's own threads, the web process — before giving up. The lock
# above orders the loop's own coroutines and says nothing about those, so
# without a bound the loop thread would sit in `sqlite3` for `get_db`'s 30s
# default and trip the stall watchdog. 2000 is the dispatch loop's number, for
# the same reason. The caller's retry is the next poll cycle, which costs at
# most one cycle's latency, because the cursor advance and the task creation
# commit together — a lost transaction is re-polled rather than half applied.
#
# **"Half applied" is a claim about the database and not about the room**, and
# the difference is the cost of choosing a bound at all. The results block posts
# to Talk from inside its transaction — a `!model` usage reply, a `!command`
# dispatch, a confirmation ack, the channel-gate notice — and a rollback does
# not retract any of those. So a lost transaction re-polls messages whose side
# effects already happened, and a shorter wait makes that more likely than a
# 30-second one did. It is still the right trade in both directions: the wait it
# replaces stalls every watcher and every delivery on the loop for the whole 30
# seconds and then usually fails anyway, and the same replay is reachable from
# any exception in that block, so the bound changes the odds rather than opening
# the case. Hoisting those posts out of the transaction is what would close it.
DB_BUSY_TIMEOUT_MS = 2000

_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)
_locks_guard = threading.Lock()


def loop_db_lock() -> asyncio.Lock:
    """The transport's database lock for the running loop.

    Raises `RuntimeError` off a loop, which is the honest answer: there is no
    invariant to enforce for a caller that is not on one, and returning
    something acquirable would let a synchronous caller believe it had taken
    the lock the loop's coroutines are ordered by.
    """
    loop = asyncio.get_running_loop()
    with _locks_guard:
        lock = _locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _locks[loop] = lock
        return lock


@asynccontextmanager
async def talk_db(
    db_path: Path, *, busy_timeout_ms: int = DB_BUSY_TIMEOUT_MS,
) -> AsyncIterator[sqlite3.Connection]:
    """Open a database connection on the loop, behind the transport lock.

    The lock is taken *before* the connection and released after it closes, so
    the window it covers is the whole transaction rather than the first write.

    A caller that needs something wrapped around the lock as well — the poller
    instruments its transactions from outside, so that the wait another writer
    pays is part of the number it reports — uses `contextlib.AsyncExitStack`
    with `loop_db_lock()` and `db.get_db` directly, in that order. There is one
    lock and one timeout either way.

    **Not reentrant, and a self-acquire is worse than what it replaced.**
    `asyncio.Lock` has no owner and no timeout, so a coroutine that reaches this
    again from inside its own transaction — through anything the results block
    awaits, say — waits for a lock only it can release, with no error, no busy
    timeout and nothing to log. Code called from inside one of these blocks
    takes the connection it was handed; it does not open its own.
    """
    async with loop_db_lock():
        with db.get_db(db_path, busy_timeout_ms=busy_timeout_ms) as conn:
            yield conn
