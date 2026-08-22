"""`mark_seen`: what it stamps, and the version check on what it closes.

`mark_seen` takes `(id, updated_at)` pairs rather than bare ids. Without the
version check, two sequences close an occurrence nobody saw — a row bumped
between the client's fetch and its POST (a bump does not deliver, so the new
occurrence would vanish silently), and a late or retried POST arriving after
the row was reopened and re-delivered.
"""

import pytest

from istota import db, notification_sources as sources, notification_store as store
from istota.config import Config, UserConfig


@pytest.fixture(autouse=True)
def _clean_registry():
    sources.reset_registry()
    yield
    sources.reset_registry()


@pytest.fixture
def config(tmp_path):
    return Config(
        db_path=tmp_path / "test.db",
        users={"alice": UserConfig(display_name="Alice")},
    )


@pytest.fixture
def conn(config):
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as c:
        yield c


class _Resolver:
    def __init__(self, source, *, auto=False):
        self.source = source
        self.auto_resolve_on_seen = auto

    def resolve(self, config, conn, row):
        return None


def _row(conn, notification_id):
    return conn.execute(
        "SELECT * FROM notifications WHERE id = ?", (notification_id,)
    ).fetchone()


def _write(conn, user_id="alice", source="task_alert", dedup_key="task:1:security"):
    return store.write_notification(
        conn, user_id, source=source, dedup_key=dedup_key, title="Alert"
    )


@pytest.fixture
def registered():
    """`task_alert` auto-resolves on seen; `confirmation` does not."""
    sources.register(_Resolver("task_alert", auto=True))
    sources.register(_Resolver("confirmation", auto=False))


def test_stamps_seen_at_on_every_id(conn, registered):
    first = _write(conn, dedup_key="a:1")
    second = _write(conn, source="confirmation", dedup_key="task:7")

    store.mark_seen(
        conn,
        "alice",
        [
            (first.notification_id, _row(conn, first.notification_id)["updated_at"]),
            (second.notification_id, _row(conn, second.notification_id)["updated_at"]),
        ],
    )

    assert _row(conn, first.notification_id)["seen_at"] is not None
    assert _row(conn, second.notification_id)["seen_at"] is not None


def test_resolves_only_auto_resolving_sources(conn, registered):
    alert = _write(conn, dedup_key="a:1")
    confirmation = _write(conn, source="confirmation", dedup_key="task:7")
    pairs = [
        (alert.notification_id, _row(conn, alert.notification_id)["updated_at"]),
        (
            confirmation.notification_id,
            _row(conn, confirmation.notification_id)["updated_at"],
        ),
    ]

    store.mark_seen(conn, "alice", pairs)

    assert _row(conn, alert.notification_id)["state"] == "resolved"
    assert _row(conn, alert.notification_id)["resolved_by"] == "web"
    # An object-backed item you have looked at and not acted on still needs you.
    assert _row(conn, confirmation.notification_id)["state"] == "open"


def test_unregistered_source_is_not_auto_resolved(conn):
    """No registry entry means no `auto_resolve_on_seen` declaration to trust."""
    alert = _write(conn)
    store.mark_seen(
        conn, "alice",
        [(alert.notification_id, _row(conn, alert.notification_id)["updated_at"])],
    )
    row = _row(conn, alert.notification_id)
    assert row["state"] == "open"
    assert row["seen_at"] is not None


def test_bump_between_fetch_and_seen_stamps_but_does_not_resolve(conn, registered):
    """Tab A fetched at T0; the producer bumped the row before the POST landed."""
    alert = _write(conn)
    rendered_at = _row(conn, alert.notification_id)["updated_at"]
    conn.execute(
        "UPDATE notifications SET updated_at = ?, occurrences = 2 WHERE id = ?",
        ("2030-01-01T00:00:00.000Z", alert.notification_id),
    )

    store.mark_seen(conn, "alice", [(alert.notification_id, rendered_at)])

    row = _row(conn, alert.notification_id)
    assert row["state"] == "open"
    assert row["seen_at"] is not None


def test_late_post_after_a_reopen_does_not_close_the_new_occurrence(conn, registered):
    alert = _write(conn)
    rendered_at = _row(conn, alert.notification_id)["updated_at"]
    # Auto-resolved at T1 by the panel, then reopened and re-delivered at T2.
    store.mark_seen(conn, "alice", [(alert.notification_id, rendered_at)])
    assert _row(conn, alert.notification_id)["state"] == "resolved"
    conn.execute(
        "UPDATE notifications SET state = 'open', updated_at = ? WHERE id = ?",
        ("2030-01-01T00:00:00.000Z", alert.notification_id),
    )

    # A retry from another tab, still carrying the T0 value.
    store.mark_seen(conn, "alice", [(alert.notification_id, rendered_at)])

    assert _row(conn, alert.notification_id)["state"] == "open"


def test_another_users_id_is_skipped_silently(conn, registered):
    mine = _write(conn, dedup_key="a:1")
    theirs = _write(conn, user_id="bob", dedup_key="a:1")

    store.mark_seen(
        conn,
        "alice",
        [
            (mine.notification_id, _row(conn, mine.notification_id)["updated_at"]),
            (theirs.notification_id, _row(conn, theirs.notification_id)["updated_at"]),
        ],
    )

    assert _row(conn, mine.notification_id)["seen_at"] is not None
    bob_row = _row(conn, theirs.notification_id)
    assert bob_row["seen_at"] is None
    assert bob_row["state"] == "open"


def test_seen_does_not_move_updated_at(conn, registered):
    """`updated_at` is the sort key and the client's version token."""
    confirmation = _write(conn, source="confirmation", dedup_key="task:7")
    before = _row(conn, confirmation.notification_id)["updated_at"]

    store.mark_seen(conn, "alice", [(confirmation.notification_id, before)])

    assert _row(conn, confirmation.notification_id)["updated_at"] == before


def test_malformed_pairs_are_skipped(conn, registered):
    alert = _write(conn)
    stamp = _row(conn, alert.notification_id)["updated_at"]

    store.mark_seen(
        conn,
        "alice",
        [("not-an-int", stamp), (alert.notification_id, None), (alert.notification_id,)],
    )

    row = _row(conn, alert.notification_id)
    assert row["state"] == "open"


def test_empty_batch_is_a_no_op(conn, registered):
    store.mark_seen(conn, "alice", [])
