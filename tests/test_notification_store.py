"""The notification inbox store: the three upsert branches, and which deliver.

The branch a write takes depends on the row's state *before* the write, which is
why the store does a read-modify-write on the caller's connection rather than a
single-statement upsert. These tests pin the three branches, the delivery flag
each produces, and the never-raises contract every producer relies on.
"""

import json
import sqlite3

import pytest

from istota import db, notification_sources as sources, notification_store as store
from istota.config import Config, UserConfig


@pytest.fixture(autouse=True)
def _clean_registry():
    """The resolver registry is a process global; xdist reuses the process."""
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


# A source id no real resolver claims. These are *store* tests: registering a
# fake under a live source id would not hold, because `get_resolver` calls
# `_register_all` lazily and the real module would replace the fake on the first
# read — so every `list_open` assertion here would silently be testing
# `notification_resolvers.confirmation` instead of the store.
_SOURCE = "held_thing"


def _write(conn, **overrides):
    kwargs = {
        "source": _SOURCE,
        "dedup_key": "task:7",
        "title": "Held email from a stranger",
        "body": "Subject: Invite",
        "object_type": "task",
        "object_id": "7",
        "actionable": True,
    }
    kwargs.update(overrides)
    return store.write_notification(conn, "alice", **kwargs)


def _row(conn, notification_id):
    return conn.execute(
        "SELECT * FROM notifications WHERE id = ?", (notification_id,)
    ).fetchone()


def _backdate(conn, notification_id, stamp="2020-01-01T00:00:00.000Z"):
    """Push a row's timestamps into the past deterministically.

    The stored format has millisecond resolution, so two writes inside the same
    millisecond produce equal strings and an "updated_at was refreshed"
    assertion would be flaky. Backdating first makes the comparison exact.
    """
    conn.execute(
        "UPDATE notifications SET created_at = ?, updated_at = ? WHERE id = ?",
        (stamp, stamp, notification_id),
    )


class _BoomConn:
    """A connection whose every call raises, for the never-raises contract."""

    in_transaction = False

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("boom")

    def executemany(self, *args, **kwargs):
        raise sqlite3.OperationalError("boom")

    def cursor(self, *args, **kwargs):
        raise sqlite3.OperationalError("boom")

    def commit(self):
        raise sqlite3.OperationalError("boom")


class _Resolver:
    def __init__(self, source, *, auto=False, view=None, raises=False):
        self.source = source
        self.auto_resolve_on_seen = auto
        self._view = view
        self._raises = raises
        self.calls = []

    def resolve(self, config, conn, row):
        self.calls.append(row.id)
        if self._raises:
            raise RuntimeError("resolver exploded")
        return self._view


# --- the insert branch ---------------------------------------------------


class TestInsertBranch:
    def test_first_write_inserts_and_delivers(self, conn):
        result = _write(conn)

        assert result is not None
        assert result.deliver is True
        assert result.user_id == "alice"
        assert result.purpose == "alert"
        assert result.title == "Held email from a stranger"

        row = _row(conn, result.notification_id)
        assert row["user_id"] == "alice"
        assert row["source"] == "held_thing"
        assert row["dedup_key"] == "task:7"
        assert row["state"] == "open"
        assert row["occurrences"] == 1
        assert row["actionable"] == 1
        assert row["seen_at"] is None
        assert row["last_delivered_at"] is None
        assert row["created_at"] and row["updated_at"]

    def test_params_round_trip_as_json(self, conn):
        result = _write(conn, params={"sender": "a@b.invalid", "n": 3})
        row = _row(conn, result.notification_id)
        assert json.loads(row["params"]) == {"sender": "a@b.invalid", "n": 3}

    def test_defaults_fill_in(self, conn):
        result = store.write_notification(
            conn, "alice", source="task_alert", dedup_key="task:1:security", title="t"
        )
        row = _row(conn, result.notification_id)
        assert row["body"] == ""
        assert row["params"] == "{}"
        assert row["severity"] == "info"
        assert row["actionable"] == 0
        assert row["object_type"] is None

    def test_unknown_severity_falls_back_to_info(self, conn):
        result = _write(conn, severity="catastrophic")
        assert _row(conn, result.notification_id)["severity"] == "info"

    def test_missing_identity_is_refused(self, conn):
        assert _write(conn, source="") is None
        assert _write(conn, dedup_key="") is None
        assert _write(conn, title="") is None
        assert store.write_notification(
            conn, "", source="s", dedup_key="k", title="t"
        ) is None
        assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0

    def test_delivery_text_carries_title_and_body(self, conn):
        result = _write(conn, title="Garmin expired", body="Reconnect to resume sync")
        assert "Garmin expired" in result.text
        assert "Reconnect to resume sync" in result.text


# --- the open branch -----------------------------------------------------


class TestOpenBranch:
    def test_repeat_bumps_occurrences_and_does_not_deliver(self, conn):
        first = _write(conn)
        second = _write(conn)

        assert second.notification_id == first.notification_id
        assert second.deliver is False
        assert _row(conn, first.notification_id)["occurrences"] == 2

        third = _write(conn)
        assert third.deliver is False
        assert _row(conn, first.notification_id)["occurrences"] == 3

    def test_repeat_refreshes_text_and_severity(self, conn):
        first = _write(conn)
        _write(
            conn,
            title="Held email from a stranger (again)",
            body="Subject: Invite v2",
            severity="warning",
            params={"n": 2},
        )
        row = _row(conn, first.notification_id)
        assert row["title"] == "Held email from a stranger (again)"
        assert row["body"] == "Subject: Invite v2"
        assert row["severity"] == "warning"
        assert json.loads(row["params"]) == {"n": 2}

    def test_repeat_refreshes_updated_at_and_keeps_created_at(self, conn):
        first = _write(conn)
        _backdate(conn, first.notification_id)

        _write(conn)

        row = _row(conn, first.notification_id)
        assert row["created_at"] == "2020-01-01T00:00:00.000Z"
        assert row["updated_at"] > "2020-01-01T00:00:00.000Z"

    def test_same_key_for_a_different_user_is_a_different_row(self, conn):
        first = _write(conn)
        other = store.write_notification(
            conn, "bob", source="held_thing", dedup_key="task:7", title="Bob's"
        )
        assert other.notification_id != first.notification_id
        assert other.deliver is True


# --- the reopen branch ---------------------------------------------------


class TestReopenBranch:
    @pytest.mark.parametrize("closed_state", ["resolved", "dismissed", "stale"])
    def test_closed_row_reopens_and_delivers(self, conn, closed_state):
        first = _write(conn)
        conn.execute(
            "UPDATE notifications SET state = ?, resolved_at = ?, resolved_by = ? "
            "WHERE id = ?",
            (closed_state, "2020-01-02T00:00:00.000Z", "web", first.notification_id),
        )
        _backdate(conn, first.notification_id)

        again = _write(conn)

        assert again.notification_id == first.notification_id
        assert again.deliver is True
        row = _row(conn, first.notification_id)
        assert row["state"] == "open"
        assert row["occurrences"] == 2
        assert row["resolved_at"] is None
        assert row["resolved_by"] is None
        assert row["created_at"] == "2020-01-01T00:00:00.000Z"
        assert row["updated_at"] > "2020-01-01T00:00:00.000Z"

    def test_reopen_clears_seen_at(self, conn):
        """A reopened row is a new occurrence, so it is not one you have seen."""
        first = _write(conn)
        conn.execute(
            "UPDATE notifications SET state = 'resolved', seen_at = ? WHERE id = ?",
            ("2020-01-02T00:00:00.000Z", first.notification_id),
        )
        _write(conn)
        assert _row(conn, first.notification_id)["seen_at"] is None


# --- delivery ------------------------------------------------------------


class TestDeliverPending:
    def test_stamps_last_delivered_at_only_on_a_successful_send(
        self, conn, config, monkeypatch
    ):
        from istota import notifications

        sent = _write(conn, dedup_key="task:1")
        unsent = _write(conn, dedup_key="task:2")
        conn.commit()

        calls = []

        def send(cfg, user_id, message, **kwargs):
            calls.append((user_id, message, kwargs.get("purpose")))
            # First call reaches a destination; second finds none configured.
            return len(calls) == 1

        monkeypatch.setattr(notifications, "send_notification", send)

        store.deliver_pending(config, [sent, unsent])

        assert [c[0] for c in calls] == ["alice", "alice"]
        assert calls[0][2] == "alert"
        assert _row(conn, sent.notification_id)["last_delivered_at"] is not None
        assert _row(conn, unsent.notification_id)["last_delivered_at"] is None

    def test_skips_results_that_must_not_deliver(self, conn, config, monkeypatch):
        from istota import notifications

        first = _write(conn)
        repeat = _write(conn)
        conn.commit()

        calls = []
        monkeypatch.setattr(
            notifications,
            "send_notification",
            lambda *a, **k: calls.append(a) or True,
        )

        store.deliver_pending(config, [repeat])
        assert calls == []
        assert _row(conn, first.notification_id)["last_delivered_at"] is None

    def test_a_raising_send_does_not_escape(self, conn, config, monkeypatch):
        from istota import notifications

        result = _write(conn)
        conn.commit()

        def boom(*args, **kwargs):
            raise RuntimeError("smtp down")

        monkeypatch.setattr(notifications, "send_notification", boom)
        store.deliver_pending(config, [result])  # must not raise
        assert _row(conn, result.notification_id)["last_delivered_at"] is None

    def test_none_entries_are_tolerated(self, config):
        store.deliver_pending(config, [None])  # a producer's unfiltered buffer


class TestRaiseNotification:
    def test_writes_and_delivers_on_its_own_connection(self, config, monkeypatch):
        from istota import notifications

        db.init_db(config.db_path)
        calls = []
        monkeypatch.setattr(
            notifications,
            "send_notification",
            lambda *a, **k: calls.append(a) or True,
        )

        notification_id = store.raise_notification(
            config, "alice", source="task_alert", dedup_key="task:9:security",
            title="Security alert",
        )

        assert notification_id is not None
        assert len(calls) == 1
        with db.get_db(config.db_path) as c:
            row = _row(c, notification_id)
        assert row["state"] == "open"
        assert row["last_delivered_at"] is not None

    def test_returns_none_when_the_write_is_refused(self, config):
        db.init_db(config.db_path)
        assert store.raise_notification(
            config, "alice", source="", dedup_key="k", title="t"
        ) is None


# --- lifecycle -----------------------------------------------------------


class TestLifecycle:
    def test_resolve_notification_closes_an_open_row(self, conn):
        result = _write(conn)
        store.resolve_notification(conn, "alice", "held_thing", "task:7", by="talk")
        row = _row(conn, result.notification_id)
        assert row["state"] == "resolved"
        assert row["resolved_by"] == "talk"
        assert row["resolved_at"] is not None

    def test_resolve_notification_is_idempotent(self, conn):
        result = _write(conn)
        store.resolve_notification(conn, "alice", "held_thing", "task:7", by="talk")
        first_at = _row(conn, result.notification_id)["resolved_at"]
        store.resolve_notification(conn, "alice", "held_thing", "task:7", by="web")
        row = _row(conn, result.notification_id)
        assert row["resolved_by"] == "talk"
        assert row["resolved_at"] == first_at

    def test_resolve_by_object_closes_the_row_for_that_object(self, conn):
        result = _write(conn, object_type="task", object_id="7")
        _write(conn, dedup_key="task:8", object_id="8")

        store.resolve_by_object(conn, "alice", "held_thing", "task", "7", by="web")

        assert _row(conn, result.notification_id)["state"] == "resolved"
        assert conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE state = 'open'"
        ).fetchone()[0] == 1

    def test_dismiss_closes_and_reports_ownership(self, conn):
        result = _write(conn)
        assert store.dismiss(conn, result.notification_id, "alice") is True
        row = _row(conn, result.notification_id)
        assert row["state"] == "dismissed"
        assert row["resolved_at"] is not None

        # A second dismiss is a no-op that still reports the row as the user's.
        assert store.dismiss(conn, result.notification_id, "alice") is True
        assert store.dismiss(conn, 9999, "alice") is False

    def test_mark_stale_closes_only_open_rows(self, conn):
        open_row = _write(conn, dedup_key="task:1")
        closed = _write(conn, dedup_key="task:2")
        store.dismiss(conn, closed.notification_id, "alice")

        store.mark_stale(conn, [open_row.notification_id, closed.notification_id])

        assert _row(conn, open_row.notification_id)["state"] == "stale"
        assert _row(conn, open_row.notification_id)["resolved_by"] == "system"
        assert _row(conn, closed.notification_id)["state"] == "dismissed"

    def test_mark_stale_tolerates_an_empty_list(self, conn):
        store.mark_stale(conn, [])


class TestCounts:
    def test_counts_open_and_actionable(self, conn):
        _write(conn, dedup_key="task:1", actionable=True)
        _write(conn, dedup_key="task:2", actionable=True)
        _write(conn, dedup_key="task:3", actionable=False)
        closed = _write(conn, dedup_key="task:4", actionable=True)
        store.dismiss(conn, closed.notification_id, "alice")

        assert store.counts(conn, "alice") == {"open": 3, "actionable": 2}

    def test_empty_counts(self, conn):
        assert store.counts(conn, "alice") == {"open": 0, "actionable": 0}


class TestListOpen:
    def test_unregistered_source_falls_back_to_stored_text(self, conn, config):
        _write(conn, title="Held email", body="Subject: Invite")
        items, total = store.list_open(config, conn, "alice")

        assert total == 1
        assert len(items) == 1
        assert items[0].title == "Held email"
        assert items[0].body == "Subject: Invite"
        assert items[0].actions == ()
        assert items[0].status_note

    def test_resolver_view_wins_over_stored_text(self, conn, config):
        view = sources.NotificationView(
            title="Email from a stranger",
            body="Confirm to let it through",
            severity="warning",
            actions=(
                sources.NotificationAction(
                    id="confirm", label="Confirm", kind="primary",
                    method="POST", endpoint="/chat/tasks/7/confirm",
                ),
            ),
            link=None,
            status_note=None,
        )
        sources.register(_Resolver("held_thing", view=view))
        _write(conn, title="stored title")

        items, _ = store.list_open(config, conn, "alice")
        assert items[0].title == "Email from a stranger"
        assert items[0].severity == "warning"
        assert items[0].actions[0].endpoint == "/chat/tasks/7/confirm"

    def test_resolver_returning_none_marks_the_row_stale(self, conn, config):
        result = _write(conn)
        sources.register(_Resolver("held_thing", view=None))
        conn.commit()

        items, total = store.list_open(config, conn, "alice")

        assert items == []
        assert total == 0
        with db.get_db(config.db_path) as c:
            assert _row(c, result.notification_id)["state"] == "stale"

    def test_total_open_counts_the_survivors_not_the_pre_sweep_rows(
        self, conn, config
    ):
        """The sweep commits on its own connection, so a re-query already
        excludes the dead rows — subtracting them again would under-count."""
        _write(conn, source="held_thing", dedup_key="task:1")
        _write(conn, source="held_thing", dedup_key="task:2")
        _write(conn, source="task_alert", dedup_key="a:1")
        sources.register(_Resolver("held_thing", view=None))  # both objects gone
        conn.commit()

        items, total = store.list_open(config, conn, "alice")

        assert total == 1
        assert len(items) == 1

    def test_a_raising_resolver_degrades_one_row_only(self, conn, config):
        _write(conn, source="held_thing", dedup_key="task:1", title="fallback text")
        _write(conn, source="task_alert", dedup_key="a:1", title="alert text")
        sources.register(_Resolver("held_thing", raises=True))
        sources.register(
            _Resolver(
                "task_alert",
                view=sources.NotificationView(
                    title="rendered", body="", severity="info",
                    actions=(), link=None, status_note=None,
                ),
            )
        )

        items, total = store.list_open(config, conn, "alice")
        by_title = {i.title for i in items}
        assert by_title == {"fallback text", "rendered"}
        assert total == 2

    def test_action_filter_selects_actionable_rows(self, conn, config):
        actionable = _write(conn, dedup_key="task:1", actionable=True)
        _write(conn, dedup_key="task:2", actionable=False)
        sources.register(
            _Resolver(
                "held_thing",
                view=sources.NotificationView(
                    title="rendered", body="", severity="info",
                    actions=(
                        sources.NotificationAction(
                            id="confirm", label="Confirm", kind="primary",
                            method="POST", endpoint="/chat/tasks/1/confirm",
                        ),
                    ),
                    link=None, status_note=None,
                ),
            )
        )

        items, total = store.list_open(config, conn, "alice", filter="action")
        assert [i.id for i in items] == [actionable.notification_id]
        # `total_open` is the honest open count, not the filtered one.
        assert total == 2

    def test_a_fallback_row_is_never_filed_under_needs_action(self, conn, config):
        """No resolver means no actions can be offered, whatever the row says."""
        _write(conn, dedup_key="task:1", actionable=True)

        all_items, _ = store.list_open(config, conn, "alice")
        action_items, _ = store.list_open(config, conn, "alice", filter="action")

        assert len(all_items) == 1
        assert all_items[0].actionable is False
        assert action_items == []

    def test_newest_updated_at_sorts_first_and_limit_applies(self, conn, config):
        old = _write(conn, dedup_key="task:1")
        _backdate(conn, old.notification_id)
        new = _write(conn, dedup_key="task:2")

        items, total = store.list_open(config, conn, "alice", limit=1)
        assert [i.id for i in items] == [new.notification_id]
        assert total == 2

    def test_a_truncated_scan_falls_back_to_the_db_count(
        self, conn, config, monkeypatch
    ):
        """Past `LIVENESS_SCAN_MAX` the badge may over-count, and says the
        honest open total rather than the size of the pass."""
        monkeypatch.setattr(store, "LIVENESS_SCAN_MAX", 3)
        for n in range(5):
            _write(conn, dedup_key=f"task:{n}")

        items, total = store.list_open(config, conn, "alice", limit=50)

        assert len(items) == 3          # only the scan's survivors render
        assert total == 5               # but the count is the real open set

    def test_a_view_with_an_unsafe_path_is_downgraded(self, conn, config):
        view = sources.NotificationView(
            title="rendered", body="", severity="info",
            actions=(
                sources.NotificationAction(
                    id="confirm", label="Confirm", kind="primary",
                    method="POST", endpoint="/chat/tasks/1/../../admin/x/confirm",
                ),
            ),
            link=None, status_note=None,
        )
        sources.register(_Resolver("held_thing", view=view))
        _write(conn, title="stored title")

        items, _ = store.list_open(config, conn, "alice")
        assert items[0].title == "stored title"
        assert items[0].actions == ()
        assert items[0].status_note

    @pytest.mark.parametrize(
        "action",
        [
            # The field the method does not name is serialized anyway, so it
            # cannot be left unchecked.
            sources.NotificationAction(
                id="confirm", label="Confirm", kind="primary", method="POST",
                endpoint="/chat/tasks/1/confirm",
                href="javascript:fetch('//evil.example')",
            ),
            sources.NotificationAction(
                id="open", label="Open", kind="default", method="LINK",
                href="/chat", endpoint="https://evil.example/x",
            ),
            # The field it does name has to be there at all.
            sources.NotificationAction(
                id="confirm", label="Confirm", kind="primary", method="POST",
                endpoint=None,
            ),
            # And an unknown method is not a shape the client can act on.
            sources.NotificationAction(
                id="confirm", label="Confirm", kind="primary", method="GET",
                endpoint="/chat/tasks/1/confirm",
            ),
        ],
    )
    def test_every_url_field_is_checked_whatever_the_method_says(
        self, conn, config, action
    ):
        sources.register(
            _Resolver(
                "held_thing",
                view=sources.NotificationView(
                    title="rendered", body="", severity="info",
                    actions=(action,), link=None, status_note=None,
                ),
            )
        )
        _write(conn, title="stored title")

        items, _ = store.list_open(config, conn, "alice")
        assert items[0].title == "stored title"
        assert items[0].actions == ()

    def test_an_offsite_link_is_downgraded(self, conn, config):
        sources.register(
            _Resolver(
                "held_thing",
                view=sources.NotificationView(
                    title="rendered", body="", severity="info",
                    actions=(), link="https://evil.example/x", status_note=None,
                ),
            )
        )
        _write(conn, title="stored title")

        items, _ = store.list_open(config, conn, "alice")
        assert items[0].title == "stored title"
        assert items[0].link is None

    def test_a_structurally_invalid_view_degrades_one_row_only(self, conn, config):
        """A view is a resolver-supplied object, so validating and rendering it
        can raise just as `resolve` can — and that must not blank the panel."""
        broken = sources.NotificationView.__new__(sources.NotificationView)
        object.__setattr__(broken, "title", "x")
        object.__setattr__(broken, "body", "")
        object.__setattr__(broken, "severity", "info")
        # Truthy and not iterable, so the validation pass raises rather than
        # treating it as an empty action tuple.
        object.__setattr__(broken, "actions", object())
        object.__setattr__(broken, "link", None)
        object.__setattr__(broken, "status_note", None)

        _write(conn, source="held_thing", dedup_key="task:1", title="fallback text")
        _write(conn, source="task_alert", dedup_key="a:1", title="alert text")
        sources.register(_Resolver("held_thing", view=broken))
        sources.register(
            _Resolver(
                "task_alert",
                view=sources.NotificationView(
                    title="rendered", body="", severity="info",
                    actions=(), link=None, status_note=None,
                ),
            )
        )

        items, total = store.list_open(config, conn, "alice")

        assert {i.title for i in items} == {"fallback text", "rendered"}
        assert total == 2


class TestRawConnection:
    """A connection opened without `row_factory = sqlite3.Row`.

    `db.get_db` sets it; a producer opening its own connection is under no
    obligation to. Every read here indexes by name, so on a tuple-returning
    connection the whole module used to fail silently — and the *insert* branch
    reads nothing, so the first write to a key worked and every later one
    vanished, which is the shape that hides it.
    """

    @pytest.fixture
    def raw(self, config):
        db.init_db(config.db_path)
        conn = sqlite3.connect(config.db_path)
        assert conn.row_factory is None
        yield conn
        conn.close()

    def test_the_full_lifecycle_works_on_a_tuple_connection(self, raw, config):
        first = _write(raw)
        assert first is not None

        second = _write(raw)
        assert second is not None
        assert second.notification_id == first.notification_id
        assert second.deliver is False

        assert store.counts(raw, "alice") == {"open": 1, "actionable": 1}
        items, total = store.list_open(config, raw, "alice")
        assert total == 1
        assert items[0].title == "Held email from a stranger"

        assert store.dismiss(raw, first.notification_id, "alice") is True
        assert store.counts(raw, "alice") == {"open": 0, "actionable": 0}

    def test_mark_seen_works_on_a_tuple_connection(self, raw):
        sources.register(_Resolver("task_alert", auto=True))
        written = store.write_notification(
            raw, "alice", source="task_alert", dedup_key="a:1", title="Alert"
        )
        stamp = raw.execute(
            "SELECT updated_at FROM notifications WHERE id = ?",
            (written.notification_id,),
        ).fetchone()[0]

        store.mark_seen(raw, "alice", [(written.notification_id, stamp)])

        state, seen_at = raw.execute(
            "SELECT state, seen_at FROM notifications WHERE id = ?",
            (written.notification_id,),
        ).fetchone()
        assert state == "resolved"
        assert seen_at is not None


class TestConcurrentWrite:
    def test_an_insert_that_loses_the_race_bumps_instead_of_vanishing(
        self, conn, config
    ):
        """Two producers, no shared lock, same key.

        The module's contract says producers hold a write transaction and SQLite
        therefore serialises them — but nothing enforces that, and
        `raise_notification` opens its own connection and holds no lock at the
        SELECT. A losing insert used to raise `IntegrityError` into the
        never-raises handler: no row bump, no delivery, and a `None` the producer
        is told to ignore.
        """
        db.init_db(config.db_path)
        other = sqlite3.connect(config.db_path)
        other.row_factory = sqlite3.Row
        try:
            # A writes and commits between B's SELECT and B's INSERT.
            real_read = store._read
            state = {"fired": False}

            def racing_read(c, sql, params=()):
                cursor = real_read(c, sql, params)
                if not state["fired"] and "SELECT id, state" in sql:
                    state["fired"] = True
                    other.execute(
                        "INSERT INTO notifications "
                        "(user_id, source, dedup_key, title) "
                        "VALUES ('alice', 'held_thing', 'task:7', 'A')"
                    )
                    other.commit()
                return cursor

            store._read = racing_read
            try:
                with db.get_db(config.db_path) as b:
                    result = store.write_notification(
                        b, "alice", source="held_thing", dedup_key="task:7",
                        title="B", actionable=True,
                    )
            finally:
                store._read = real_read

            assert result is not None
            assert result.deliver is False       # A's insert was the delivery
            row = other.execute(
                "SELECT occurrences, title, state FROM notifications"
            ).fetchall()
            assert len(row) == 1
            assert row[0]["occurrences"] == 2    # B's write was not lost
            assert row[0]["title"] == "B"
        finally:
            other.close()


class TestPurpose:
    def test_the_purpose_list_matches_the_delivery_layer(self):
        """`DELIVERY_PURPOSES` is a copy; this is what stops it drifting."""
        from istota import notifications

        assert sources.DELIVERY_PURPOSES == notifications.PURPOSES

    def test_an_unknown_purpose_falls_back(self, conn, config, monkeypatch):
        from istota import notifications

        result = _write(conn, purpose="urgent-ish")
        assert result.purpose == "alert"
        conn.commit()

        seen = []
        monkeypatch.setattr(
            notifications,
            "send_notification",
            lambda *a, **k: seen.append(k.get("purpose")) or True,
        )
        store.deliver_pending(config, [result])
        assert seen == ["alert"]


class TestDeliveryVerifiesTheRow:
    def test_a_rolled_back_write_is_not_delivered(self, config, monkeypatch):
        """The result is handed back on the caller's *open* transaction."""
        from istota import notifications

        db.init_db(config.db_path)
        calls = []
        monkeypatch.setattr(
            notifications,
            "send_notification",
            lambda *a, **k: calls.append(a) or True,
        )

        conn = sqlite3.connect(config.db_path)
        conn.row_factory = sqlite3.Row
        try:
            result = store.write_notification(
                conn, "alice", source="held_thing", dedup_key="task:7", title="t"
            )
            conn.rollback()
        finally:
            conn.close()

        store.deliver_pending(config, [result])
        assert calls == []

    def test_a_row_closed_before_delivery_is_not_delivered(self, config, monkeypatch):
        from istota import notifications

        db.init_db(config.db_path)
        calls = []
        monkeypatch.setattr(
            notifications,
            "send_notification",
            lambda *a, **k: calls.append(a) or True,
        )

        with db.get_db(config.db_path) as conn:
            result = store.write_notification(
                conn, "alice", source="held_thing", dedup_key="task:7", title="t"
            )
        with db.get_db(config.db_path) as conn:
            store.resolve_notification(
                conn, "alice", "held_thing", "task:7", by="talk"
            )

        store.deliver_pending(config, [result])
        assert calls == []


# --- the never-raises contract -------------------------------------------


class TestNeverRaises:
    def test_every_public_function_survives_a_broken_connection(self, config):
        # Both sweeps and `mark_seen` short-circuit on an empty registry before
        # they ever touch the connection, so without a registration the
        # assertions below would pass without exercising anything.
        sources.register(_Resolver("task_alert", auto=True))
        bad = _BoomConn()
        assert store.write_notification(
            bad, "alice", source="s", dedup_key="k", title="t"
        ) is None
        assert store.resolve_notification(bad, "alice", "s", "k", by="web") == 0
        assert store.resolve_by_object(bad, "alice", "s", "task", "1", by="web") == 0
        assert store.dismiss(bad, 1, "alice") is False
        assert store.mark_stale(bad, [1]) is None
        assert store.mark_seen(bad, "alice", [(1, "2026-01-01T00:00:00.000Z")]) is None
        assert store.sweep_expired_alerts(bad) == 0
        assert store.sweep_retention(bad) == 0
        assert store.counts(bad, "alice") == {"open": 0, "actionable": 0}
        assert store.list_open(config, bad, "alice") == ([], 0)

    def test_raise_notification_survives_a_missing_database(self, tmp_path):
        config = Config(db_path=tmp_path / "nonexistent" / "no.db")
        assert store.raise_notification(
            config, "alice", source="s", dedup_key="k", title="t"
        ) is None
