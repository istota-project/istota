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
from istota.notification_resolvers import health_panel as panel_source


@pytest.fixture(autouse=True)
def _clean_registry():
    sources.reset_registry()
    yield
    sources.reset_registry()


@pytest.fixture
def config(tmp_path):
    return Config(
        db_path=tmp_path / "test.db",
        nextcloud_mount_path=tmp_path / "mount",
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
    """Alice and Bob each hold object `12` of some per-user module, in their own DB.

    A stand-in source, deliberately: these cases are about the store's own
    arithmetic, and naming a registered source here would put a real resolver on
    the read path and make the assertions depend on it. The real `health_panel`
    equivalents are at the bottom of the file.
    """
    made = {}
    for user in ("alice", "bob"):
        made[user] = store.write_notification(
            conn,
            user,
            source="per_user_module",
            dedup_key="panel:12",
            title=f"{user}'s bloodwork panel needs review",
            object_type="per_user_module",
            object_id="12",
            actionable=True,
        )
    return made


def test_resolve_by_object_touches_one_users_row(conn, panels):
    store.resolve_by_object(
        conn, "alice", "per_user_module", "per_user_module", "12", by="web"
    )

    assert _row(conn, panels["alice"].notification_id)["state"] == "resolved"
    assert _row(conn, panels["bob"].notification_id)["state"] == "open"


def test_resolve_notification_touches_one_users_row(conn, panels):
    store.resolve_notification(conn, "alice", "per_user_module", "panel:12", by="web")

    assert _row(conn, panels["alice"].notification_id)["state"] == "resolved"
    assert _row(conn, panels["bob"].notification_id)["state"] == "open"


def test_dismiss_refuses_another_users_row(conn, panels):
    assert store.dismiss(conn, panels["bob"].notification_id, "alice") is False
    assert _row(conn, panels["bob"].notification_id)["state"] == "open"


def test_mark_seen_skips_another_users_row(conn, panels):
    sources.register(_Resolver("per_user_module", auto=True))
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
    sources.register(_Resolver("per_user_module"))
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


# ---------------------------------------------------------------------------
# the real `health_panel` source, against two real per-user module DBs
# ---------------------------------------------------------------------------
#
# Everything above uses a synthetic source, which proves the store's own
# arithmetic and nothing about the source the arithmetic exists for. These drive
# the producer, the close path and the resolver for real: two users, two
# separate health module DBs, and a panel `12` in each. Any of the three reading
# a panel id without a user beside it touches the wrong user's row — and the
# store's scoping cannot catch the third one, because a resolver that opened the
# *reading* user's health DB would answer "not a draft" for a panel that is one.

PANEL_ID = 12


def _health_ctx(config, user_id: str):
    from istota.health._loader import resolve_for_user
    from istota.health._migrate import ensure_initialised

    ctx = resolve_for_user(user_id, config)
    ensure_initialised(ctx)
    return ctx


def _make_panel(ctx, *, draft: bool = True, panel_id: int = PANEL_ID) -> None:
    """A panel with a *chosen* id, so both users hold the same one."""
    from istota.health import db as health_db

    with health_db.connect(ctx.db_path) as c:
        pid = health_db.insert_panel(
            c, drawn_at="2026-08-01", lab_name="Test Lab", draft=draft,
        )
        c.execute("UPDATE panels SET id = ? WHERE id = ?", (panel_id, pid))
        c.commit()


def _set_draft(ctx, value: bool, *, panel_id: int = PANEL_ID) -> None:
    from istota.health import db as health_db

    with health_db.connect(ctx.db_path) as c:
        health_db.update_panel(c, panel_id, draft=value)
        c.commit()


def _panel_state(config, notification_id):
    with db.get_db(config.db_path) as c:
        return _row(c, notification_id)["state"]


@pytest.fixture
def health_panels(config, conn):
    """Alice and Bob each hold a draft panel `12`, and each an inbox row for it.

    Written through `raise_for_panel`, which is what the upload route calls, so
    the dedup key and the `object_id` under test are the producer's own rather
    than a hand-built approximation of them. The fixture commits and drops the
    session connection first: `raise_for_panel` opens one of its own, exactly as
    the route does, and would otherwise wait out the busy timeout.
    """
    conn.commit()
    made = {}
    for user in ("alice", "bob"):
        ctx = _health_ctx(config, user)
        _make_panel(ctx)
        made[user] = {
            "ctx": ctx,
            "notification_id": panel_source.raise_for_panel(
                config, user, panel_id=PANEL_ID,
                drawn_at="2026-08-01", lab_name="Test Lab",
            ),
        }
    assert made["alice"]["notification_id"] is not None
    assert made["bob"]["notification_id"] is not None
    assert made["alice"]["notification_id"] != made["bob"]["notification_id"]
    return made


def test_a_panel_row_appears_when_the_upload_creates_a_draft(config, health_panels):
    with db.get_db(config.db_path) as c:
        rows = c.execute(
            "SELECT user_id, dedup_key, object_type, object_id, state "
            "FROM notifications WHERE source = 'health_panel' ORDER BY user_id",
        ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("alice", f"panel:{PANEL_ID}", "health_panel", str(PANEL_ID), "open"),
        ("bob", f"panel:{PANEL_ID}", "health_panel", str(PANEL_ID), "open"),
    ]


def test_confirming_one_users_panel_12_leaves_the_others_row_open(
    config, health_panels,
):
    """The defect the index and the `user_id` argument exist for."""
    panel_source.close_for_panel(config, "alice", PANEL_ID, by="web")

    assert _panel_state(config, health_panels["alice"]["notification_id"]) == "resolved"
    assert _panel_state(config, health_panels["bob"]["notification_id"]) == "open"


def test_the_resolver_reads_the_rows_own_users_health_db(config, health_panels):
    """Alice confirms her panel. Bob's row must still render as waiting.

    A resolver that opened the *reading* user's module DB — or that skipped the
    per-user resolution and shared one — would see Alice's confirmed panel `12`
    and return None for Bob's row, closing an item he has not dealt with.
    """
    _set_draft(health_panels["alice"]["ctx"], False)

    with db.get_db(config.db_path) as c:
        bob_items, bob_total = store.list_open(config, c, "bob")
        alice_items, alice_total = store.list_open(config, c, "alice")

    assert bob_total == 1
    assert [i.id for i in bob_items] == [health_panels["bob"]["notification_id"]]
    assert bob_items[0].actions[0].id == "review"
    assert alice_items == []
    assert alice_total == 0
    assert _panel_state(config, health_panels["alice"]["notification_id"]) == "stale"
    assert _panel_state(config, health_panels["bob"]["notification_id"]) == "open"
