"""The liveness pass covers the whole open set, not just what renders.

The badge is `counts()` — plain SQL over open rows, no resolvers — and the panel
is `list_open`, which renders at most `limit` rows. If the liveness pass were
bounded by the render limit, a user whose held items were mostly answered
elsewhere would open the panel, see three live rows, and keep a badge reading
sixty forever: the dead rows below the cut would never have their resolver
called, so nothing would ever mark them `stale`.

The rows most likely to have dead objects are the *oldest* ones — an object-backed
row closes when its object closes, so the ones still open after weeks are exactly
the ones whose close path was missed. Those sort last under `updated_at DESC`,
which is to say they are precisely the rows a render-bounded sweep never reaches.

So: 60 open rows, 55 of them with dead objects, a render limit of 10. One panel
open must converge the badge to 5.
"""

from __future__ import annotations

import pytest

from istota import db
from istota import notification_sources as sources
from istota import notification_store as store
from istota.config import Config, UserConfig
from istota.notification_resolvers import confirmation as confirmation_source

TOTAL_ROWS = 60
DEAD_ROWS = 55
LIVE_ROWS = TOTAL_ROWS - DEAD_ROWS
RENDER_LIMIT = 10


@pytest.fixture(autouse=True)
def _registry():
    sources.reset_registry()
    yield
    sources.reset_registry()


@pytest.fixture
def config(tmp_path):
    return Config(
        db_path=tmp_path / "test.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={"alice": UserConfig(display_name="Alice")},
    )


@pytest.fixture
def conn(config):
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as c:
        yield c


def _held_task(conn, n: int) -> int:
    task_id = db.create_task(
        conn, prompt=f"task {n}", user_id="alice", source_type="web",
        conversation_token="room-1",
    )
    db.set_task_confirmation(conn, task_id, f"Shall I do {n}?")
    confirmation_source.write(
        conn, "alice", task_id=task_id, title=f"Question {n}",
    )
    return task_id


def _kill(conn, task_id: int) -> None:
    """Answer the task behind the store's back — the missed-close-path case.

    Deliberately not through `confirmations.approve`, which closes the row
    itself: what is under test is the resolver noticing that the object moved on
    without anyone having told the store.
    """
    conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (task_id,))


def _seed(conn, *, dead: int = DEAD_ROWS, total: int = TOTAL_ROWS) -> list[int]:
    """`total` open rows, the oldest `dead` of them pointing at answered tasks.

    `updated_at` is stamped in ascending order so the dead ones sort *below* the
    render limit under `updated_at DESC`. Without that the test would pass with
    a render-bounded liveness pass, which is the exact bug it exists to catch.
    """
    task_ids = []
    for n in range(total):
        task_ids.append(_held_task(conn, n))
    for position, task_id in enumerate(task_ids):
        conn.execute(
            "UPDATE notifications SET updated_at = ? WHERE object_id = ?",
            (f"2026-01-{position + 1:02d}T00:00:00.000Z", str(task_id)),
        )
    for task_id in task_ids[:dead]:
        _kill(conn, task_id)
    return task_ids


class TestLiveness:
    def test_badge_converges_after_one_panel_open(self, config, conn):
        _seed(conn)
        # Before: the badge counts every open row, because `counts` runs no
        # resolvers by design — the bell polls it every thirty seconds and a
        # resolver pass on a timer would open per-user module DBs repeatedly.
        assert store.counts(conn, "alice")["open"] == TOTAL_ROWS

        rendered, total_open = store.list_open(
            config, conn, "alice", limit=RENDER_LIMIT,
        )

        assert len(rendered) == LIVE_ROWS
        assert total_open == LIVE_ROWS
        assert store.counts(conn, "alice")["open"] == LIVE_ROWS

    def test_rows_below_the_render_limit_are_swept(self, config, conn):
        task_ids = _seed(conn)
        store.list_open(config, conn, "alice", limit=RENDER_LIMIT)

        # Every dead row went `stale`, including the ~45 that could never have
        # been rendered at a limit of 10.
        states = {
            row["object_id"]: row["state"]
            for row in conn.execute(
                "SELECT object_id, state FROM notifications WHERE user_id = 'alice'"
            )
        }
        assert all(states[str(t)] == "stale" for t in task_ids[:DEAD_ROWS])
        assert all(states[str(t)] == "open" for t in task_ids[DEAD_ROWS:])

    def test_stale_rows_are_stamped_by_the_system(self, config, conn):
        _seed(conn)
        store.list_open(config, conn, "alice", limit=RENDER_LIMIT)
        row = conn.execute(
            "SELECT resolved_by, resolved_at FROM notifications "
            "WHERE state = 'stale' LIMIT 1"
        ).fetchone()
        assert row["resolved_by"] == "system"
        assert row["resolved_at"]

    def test_second_open_is_idempotent(self, config, conn):
        _seed(conn)
        store.list_open(config, conn, "alice", limit=RENDER_LIMIT)
        rendered, total_open = store.list_open(
            config, conn, "alice", limit=RENDER_LIMIT,
        )
        assert len(rendered) == LIVE_ROWS
        assert total_open == LIVE_ROWS

    def test_truncated_scan_may_over_count_and_says_so(self, config, conn):
        """Past `LIVENESS_SCAN_MAX` the badge is allowed to be high.

        Simulated by shrinking the cap rather than writing 500 rows: what is
        under test is the branch, not the number. With the scan truncated,
        `total_open` falls back to the SQL count, which still includes the dead
        rows the pass never reached — 500 open rows is itself a fault to
        investigate, so the footer says so rather than hiding it.
        """
        _seed(conn)
        original = store.LIVENESS_SCAN_MAX
        store.LIVENESS_SCAN_MAX = 20
        try:
            _, total_open = store.list_open(
                config, conn, "alice", limit=RENDER_LIMIT,
            )
        finally:
            store.LIVENESS_SCAN_MAX = original
        # The 20 newest rows are the 5 live ones plus 15 dead; only those 15
        # were swept, so 40 dead rows remain open and the count is high.
        assert total_open > LIVE_ROWS

    def test_liveness_is_scoped_to_the_reading_user(self, config, conn):
        """One user's panel open must not sweep another user's rows.

        The whole pass is driven off `WHERE user_id = ?`, but the sweep it feeds
        takes bare ids — so a scoping mistake here would close rows belonging to
        somebody who never opened anything.
        """
        _seed(conn, dead=2, total=3)
        bob_task = db.create_task(
            conn, prompt="bob's", user_id="bob", source_type="web",
            conversation_token="room-b",
        )
        db.set_task_confirmation(conn, bob_task, "Shall I?")
        confirmation_source.write(conn, "bob", task_id=bob_task, title="Bob's question")
        conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (bob_task,))

        store.list_open(config, conn, "alice", limit=RENDER_LIMIT)

        row = conn.execute(
            "SELECT state FROM notifications WHERE user_id = 'bob'"
        ).fetchone()
        assert row["state"] == "open"
        assert store.counts(conn, "bob")["open"] == 1
