"""Cross-user scoping, which is the defect this store is easiest to get wrong on.

`object_id` is opaque `TEXT` and, for a per-user module source, it is an id from
that user's own module DB — every user has a health panel `12`. A close path
keyed on `(source, object_type, object_id)` alone would resolve every user's row
for their panel 12 when one user confirms theirs. So every lifecycle function
takes `user_id` first-class and required, and `idx_notifications_object` leads
with it.
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
        users={
            "alice": UserConfig(display_name="Alice"),
            "bob": UserConfig(display_name="Bob"),
        },
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


@pytest.fixture
def panels(conn):
    """Alice and Bob each hold a draft health panel with id 12 in their own DB."""
    made = {}
    for user in ("alice", "bob"):
        made[user] = store.write_notification(
            conn,
            user,
            source="health_panel",
            dedup_key="panel:12",
            title=f"{user}'s bloodwork panel needs review",
            object_type="health_panel",
            object_id="12",
            actionable=True,
        )
    return made


def test_resolve_by_object_touches_one_users_row(conn, panels):
    store.resolve_by_object(
        conn, "alice", "health_panel", "health_panel", "12", by="web"
    )

    assert _row(conn, panels["alice"].notification_id)["state"] == "resolved"
    assert _row(conn, panels["bob"].notification_id)["state"] == "open"


def test_resolve_notification_touches_one_users_row(conn, panels):
    store.resolve_notification(conn, "alice", "health_panel", "panel:12", by="web")

    assert _row(conn, panels["alice"].notification_id)["state"] == "resolved"
    assert _row(conn, panels["bob"].notification_id)["state"] == "open"


def test_dismiss_refuses_another_users_row(conn, panels):
    assert store.dismiss(conn, panels["bob"].notification_id, "alice") is False
    assert _row(conn, panels["bob"].notification_id)["state"] == "open"


def test_mark_seen_skips_another_users_row(conn, panels):
    sources.register(_Resolver("health_panel", auto=True))
    bob_row = _row(conn, panels["bob"].notification_id)

    store.mark_seen(
        conn, "alice", [(panels["bob"].notification_id, bob_row["updated_at"])]
    )

    after = _row(conn, panels["bob"].notification_id)
    assert after["state"] == "open"
    assert after["seen_at"] is None


def test_counts_are_scoped(conn, panels):
    store.write_notification(
        conn, "bob", source="task_alert", dedup_key="a:1", title="Bob's alert"
    )
    assert store.counts(conn, "alice") == {"open": 1, "actionable": 1}
    assert store.counts(conn, "bob") == {"open": 2, "actionable": 1}


def test_list_open_is_scoped(conn, config, panels):
    items, total = store.list_open(config, conn, "alice")
    assert total == 1
    assert [i.id for i in items] == [panels["alice"].notification_id]


def test_the_stale_sweep_from_a_read_touches_one_users_row(conn, config, panels):
    """The liveness pass closes rows for the reading user only."""
    sources.register(_Resolver("health_panel"))
    conn.commit()

    store.list_open(config, conn, "alice")

    with db.get_db(config.db_path) as c:
        assert _row(c, panels["alice"].notification_id)["state"] == "stale"
        assert _row(c, panels["bob"].notification_id)["state"] == "open"


def test_the_same_key_for_two_users_is_two_rows(conn, panels):
    assert panels["alice"].notification_id != panels["bob"].notification_id
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2


def test_the_object_index_leads_with_user_id(conn):
    """Stated in the spec as load-bearing, so it is asserted rather than assumed."""
    columns = [
        r["name"]
        for r in conn.execute("PRAGMA index_info(idx_notifications_object)").fetchall()
    ]
    assert columns[0] == "user_id"
    assert columns == ["user_id", "source", "object_type", "object_id"]
