"""The two backstop sweeps.

`sweep_expired_alerts` is what keeps the fire-and-forget class bounded: a row
below the render limit, or one belonging to a user who never opens the panel, is
never seen and so never auto-resolves. Without the sweep, "open rows are never
swept" plus "only rendered rows auto-resolve" means the badge climbs forever.

`sweep_retention` is the other end — closed rows are kept for reopen and for
post-hoc debugging, not indefinitely.
"""

import pytest

from istota import db, notification_sources as sources, notification_store as store
from istota.config import Config, UserConfig


# The synthetic fire-and-forget source these tests use. Deliberately *not*
# `task_alert`, which is a real registered source since stage 6: every reader
# primes the registry on read, so `register()`ing a fake under that name is
# undone by the next `get_resolver` / `auto_resolve_sources` call and the test
# then exercises the real resolver instead of its own stub.
ALERT_SOURCE = "alert_thing"


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


@pytest.fixture(autouse=True)
def registered():
    sources.register(_Resolver(ALERT_SOURCE, auto=True))
    sources.register(_Resolver("confirmation", auto=False))


def _row(conn, notification_id):
    return conn.execute(
        "SELECT * FROM notifications WHERE id = ?", (notification_id,)
    ).fetchone()


def _aged(conn, *, days, source=ALERT_SOURCE, dedup_key="a:1", state="open",
          user_id="alice"):
    """A row whose timestamps sit `days` in the past."""
    result = store.write_notification(
        conn, user_id, source=source, dedup_key=dedup_key, title="Alert"
    )
    stamp = db.iso_utc_days_ago(days)
    conn.execute(
        "UPDATE notifications SET created_at = ?, updated_at = ?, state = ?, "
        "resolved_at = ? WHERE id = ?",
        (stamp, stamp, state, None if state == "open" else stamp,
         result.notification_id),
    )
    return result.notification_id


class TestSweepExpiredAlerts:
    def test_closes_an_auto_resolving_row_past_the_age(self, conn):
        old = _aged(conn, days=15)

        assert store.sweep_expired_alerts(conn) == 1

        row = _row(conn, old)
        assert row["state"] == "resolved"
        assert row["resolved_by"] == "system"
        assert row["resolved_at"] is not None

    def test_leaves_an_auto_resolving_row_inside_the_age(self, conn):
        young = _aged(conn, days=13)
        assert store.sweep_expired_alerts(conn) == 0
        assert _row(conn, young)["state"] == "open"

    def test_leaves_an_object_backed_row_of_any_age(self, conn):
        """Its close condition is the object, not the clock."""
        held = _aged(conn, days=400, source="confirmation", dedup_key="task:7")
        assert store.sweep_expired_alerts(conn) == 0
        assert _row(conn, held)["state"] == "open"

    def test_leaves_an_unregistered_source_alone(self, conn):
        """Exempt from this sweep, and that is decided rather than inherited.

        An unregistered source is in neither the auto set nor the object-backed
        one, so nothing on a clock closes its rows. Sweeping them instead would
        let one resolver module failing to import *in the scheduler process* —
        which `_register_all` guards per module precisely so it costs only its
        own source — silently close every open row of a live, object-backed
        source, fourteen days at a time, while the user's panel still showed
        them. The failure modes are not symmetric: an unswept row renders from
        its stored title with a `status_note` and a working Dismiss, and
        `list_open` logs the source id so the state is visible rather than
        merely tolerated.
        """
        orphan = _aged(conn, days=400, source="retired_module", dedup_key="x:1")
        assert store.sweep_expired_alerts(conn) == 0
        assert _row(conn, orphan)["state"] == "open"

    def test_ages_from_the_last_occurrence(self, conn):
        """A row bumped yesterday is not an old row, whatever its `created_at`."""
        bumped = _aged(conn, days=40)
        conn.execute(
            "UPDATE notifications SET updated_at = ? WHERE id = ?",
            (db.iso_utc_days_ago(1), bumped),
        )
        assert store.sweep_expired_alerts(conn) == 0
        assert _row(conn, bumped)["state"] == "open"

    def test_ignores_already_closed_rows(self, conn):
        closed = _aged(conn, days=15, state="dismissed")
        assert store.sweep_expired_alerts(conn) == 0
        assert _row(conn, closed)["state"] == "dismissed"

    def test_no_registered_auto_sources_is_a_no_op(self, conn, monkeypatch):
        """The empty-set branch, which the real registry can no longer produce.

        `reset_registry()` is not a way to reach it: every reader primes the
        registry on read, so `auto_resolve_sources` re-imports the built-ins and
        `task_alert` is one of them. Stubbing the reader is what states the
        property — the sweep must issue no statement at all rather than one with
        an empty `IN ()`, which SQLite parses as matching everything.
        """
        monkeypatch.setattr(sources, "auto_resolve_sources", set)
        old = _aged(conn, days=15)
        assert store.sweep_expired_alerts(conn) == 0
        assert _row(conn, old)["state"] == "open"


class TestSweepRetention:
    @pytest.mark.parametrize("state", ["resolved", "dismissed", "stale"])
    def test_deletes_closed_rows_past_retention(self, conn, state):
        old = _aged(conn, days=31, state=state)
        assert store.sweep_retention(conn) == 1
        assert _row(conn, old) is None

    def test_keeps_closed_rows_inside_retention(self, conn):
        recent = _aged(conn, days=29, state="resolved")
        assert store.sweep_retention(conn) == 0
        assert _row(conn, recent) is not None

    def test_keeps_open_rows_of_any_age(self, conn):
        ancient = _aged(conn, days=400, source="confirmation", dedup_key="task:7")
        assert store.sweep_retention(conn) == 0
        assert _row(conn, ancient) is not None

    def test_falls_back_to_updated_at_when_resolved_at_is_missing(self, conn):
        row_id = _aged(conn, days=31, state="stale")
        conn.execute(
            "UPDATE notifications SET resolved_at = NULL WHERE id = ?", (row_id,)
        )
        assert store.sweep_retention(conn) == 1
        assert _row(conn, row_id) is None

    def test_counts_only_what_it_deleted(self, conn):
        _aged(conn, days=31, state="resolved", dedup_key="a:1")
        _aged(conn, days=31, state="dismissed", dedup_key="a:2")
        _aged(conn, days=1, state="resolved", dedup_key="a:3")
        assert store.sweep_retention(conn) == 2
