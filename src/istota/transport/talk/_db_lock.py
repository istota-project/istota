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
second writer then raises `OperationalError` anyway. `db.get_db`'s own
docstring names this shape, and the scheduler's dispatch loop already carries a
bounded `busy_timeout_ms` because of it.

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
    """
    async with loop_db_lock():
        with db.get_db(db_path, busy_timeout_ms=busy_timeout_ms) as conn:
            yield conn
