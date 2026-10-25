"""The assignments tables, the two `assignment_id` columns, and `assignments/store.py`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from istota import db
from istota.assignments import store
from istota.usage.telemetry import BrainUsage

SCHEMA = Path(db.__file__).parent.parent.parent / "schema.sql"

TABLES = (
    "assignments", "assignment_items", "assignment_frozen", "assignment_wakes",
    "assignment_snapshots", "assignment_inbox", "assignment_actions",
    "assignment_attempts", "assignment_events",
)

BUDGET = {"max_cost_usd": 20.0, "max_wakes": 40, "per_wake_minutes": 20, "per_wake_turns": 60}


def _assignment(conn, *, user_id="alice", slug="heat-pump-quotes", **overrides) -> int:
    fields = {
        "title": "Heat pump quotes", "goal": "Three quotes", "autonomy": "May email installers",
        "budget": BUDGET, "dir_relpath": f"assignments/{slug}",
    }
    fields.update(overrides)
    return store.insert_assignment(conn, user_id=user_id, slug=slug, **fields)


class TestSchema:
    def test_every_table_exists(self, db_conn):
        names = {r[0] for r in db_conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert set(TABLES) <= names

    def test_state_check_refuses_an_unknown_state(self, db_conn):
        assignment_id = _assignment(db_conn)
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute("UPDATE assignments SET state = 'running' WHERE id = ?", (assignment_id,))

    def test_slug_is_unique_per_user(self, db_conn):
        _assignment(db_conn)
        with pytest.raises(sqlite3.IntegrityError):
            _assignment(db_conn)
        _assignment(db_conn, user_id="bob")

    @pytest.mark.parametrize("table,column", [("tasks", "assignment_id"), ("task_usage", "assignment_id")])
    def test_an_upgraded_database_matches_a_fresh_one(self, tmp_path, table, column):
        fresh = tmp_path / "fresh.db"
        db.init_db(fresh)
        old = tmp_path / "old.db"
        db.init_db(old)
        raw = sqlite3.connect(old)
        index = "idx_tasks_assignment" if table == "tasks" else "idx_task_usage_assignment"
        raw.execute(f"DROP INDEX {index}")
        raw.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        raw.commit()
        raw.close()
        # The path exists, so this is the real init_db: migrations, then schema.sql,
        # whose index on the column fails unless the migration added it first.
        db.init_db(old)
        declared = tmp_path / "declared.db"
        raw = sqlite3.connect(declared)
        raw.executescript(SCHEMA.read_text())
        raw.close()

        def info(path):
            with db.get_db(path) as conn:
                cols = [tuple(r)[1:] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] == column]
                indexes = [r[1] for r in conn.execute(f"PRAGMA index_list({table})") if r[1] == index]
            return cols, indexes

        assert info(declared) == info(old) == info(fresh) == ([(column, "INTEGER", 0, None, 0)], [index])


class TestTaskColumns:
    def test_assignment_id_and_max_attempts_round_trip(self, db_conn):
        task_id = db.create_task(
            db_conn, prompt="[assignment wake]", user_id="alice", source_type="assignment",
            queue="assignment", assignment_id=7, max_attempts=1,
        )
        task = db.get_task(db_conn, task_id)
        assert task.assignment_id == 7
        assert task.max_attempts == 1

    def test_defaults_are_unchanged(self, db_conn):
        task = db.get_task(db_conn, db.create_task(db_conn, prompt="hi", user_id="alice"))
        assert task.assignment_id is None
        assert task.max_attempts == 3

    @pytest.mark.parametrize("bad", [0, -1])
    def test_max_attempts_below_one_is_refused(self, db_conn, bad):
        with pytest.raises(ValueError):
            db.create_task(db_conn, prompt="hi", user_id="alice", max_attempts=bad)

    def test_claim_task_returns_the_assignment_id(self, db_conn):
        db.create_task(
            db_conn, prompt="[assignment wake]", user_id="alice", source_type="assignment",
            queue="assignment", assignment_id=11, max_attempts=1,
        )
        claimed = db.claim_task(db_conn, "worker-1", user_id="alice")
        assert claimed is not None
        assert claimed.assignment_id == 11
        assert claimed.max_attempts == 1


class TestUsageColumn:
    def test_insert_task_usage_records_the_assignment(self, db_conn):
        usage = BrainUsage(has_totals=True, cost_usd=0.41)
        row_id = db.insert_task_usage(
            db_conn, usage=usage, task_id=5, user_id="alice", source_type="assignment",
            brain_kind="native", assignment_id=3,
        )
        row = db_conn.execute("SELECT assignment_id FROM task_usage WHERE id = ?", (row_id,)).fetchone()
        assert row[0] == 3

    def test_persist_brain_usage_passes_it_through(self, db_conn, make_config):
        from istota.executor import persist_brain_usage

        persist_brain_usage(
            make_config(), db_conn, usage=BrainUsage(has_totals=True, cost_usd=0.2),
            origin="task", user_id="alice", task_id=9, brain_kind="native", assignment_id=4,
        )
        row = db_conn.execute("SELECT assignment_id FROM task_usage WHERE task_id = 9").fetchone()
        assert row[0] == 4

    def test_the_executor_records_the_tasks_assignment(self, db_conn, make_config, make_task):
        from istota.executor import _persist_task_usage

        task = make_task(id=12, source_type="assignment", assignment_id=8)
        _persist_task_usage(
            make_config(), db_conn, task.id, BrainUsage(has_totals=True),
            user_id=task.user_id, source_type=task.source_type, brain_kind="native",
            assignment_id=task.assignment_id,
        )
        row = db_conn.execute("SELECT assignment_id FROM task_usage WHERE task_id = 12").fetchone()
        assert row[0] == 8


class TestAssignments:
    def test_round_trip(self, db_conn):
        assignment_id = _assignment(db_conn, deadline="2026-12-01T00:00:00.000Z", cadence_minutes=120,
                                    auto_complete=True, model="smart", effort="high", brain="native",
                                    settings={"knob": 1})
        a = store.get_assignment(db_conn, assignment_id)
        assert a.user_id == "alice" and a.slug == "heat-pump-quotes"
        assert a.title == "Heat pump quotes" and a.goal == "Three quotes"
        assert a.autonomy == "May email installers"
        assert a.state == "draft" and a.state_reason is None and a.waiting_on == []
        assert a.budget == BUDGET
        assert a.deadline == "2026-12-01T00:00:00.000Z"
        assert a.cadence_minutes == 120 and a.auto_complete is True
        assert (a.model, a.effort, a.brain) == ("smart", "high", "native")
        assert a.settings == {"knob": 1}
        assert a.dir_relpath == "assignments/heat-pump-quotes"
        assert a.created_at.endswith("Z") and a.updated_at.endswith("Z")
        assert a.next_wake_at is None and a.activated_at is None and a.closed_at is None
        assert a.budget_low_sent is False

    def test_lookup_is_scoped_by_user(self, db_conn):
        assignment_id = _assignment(db_conn)
        assert store.get_assignment(db_conn, assignment_id, user_id="alice").id == assignment_id
        assert store.get_assignment(db_conn, assignment_id, user_id="bob") is None
        assert store.get_assignment_by_slug(db_conn, "alice", "heat-pump-quotes").id == assignment_id
        assert store.get_assignment_by_slug(db_conn, "bob", "heat-pump-quotes") is None
        assert store.get_assignment(db_conn, 999) is None

    def test_list_filters_by_user_and_state(self, db_conn):
        first = _assignment(db_conn, slug="a")
        second = _assignment(db_conn, slug="b")
        _assignment(db_conn, user_id="bob", slug="c")
        store.set_state(db_conn, second, "sleeping")
        assert [a.id for a in store.list_assignments(db_conn, "alice")] == [first, second]
        assert [a.id for a in store.list_assignments(db_conn, "alice", states=("sleeping",))] == [second]
        assert [a.id for a in store.list_assignments(db_conn, states=("draft",))] == [first, 3]

    def test_update_writes_charter_fields_and_never_state(self, db_conn):
        assignment_id = _assignment(db_conn)
        before = store.get_assignment(db_conn, assignment_id).updated_at
        store.update_assignment(db_conn, assignment_id, title="New", budget={**BUDGET, "max_wakes": 50},
                                waiting_on=["user"], next_wake_at="2026-11-01T00:00:00.000Z",
                                budget_low_sent=True)
        a = store.get_assignment(db_conn, assignment_id)
        assert a.title == "New" and a.budget["max_wakes"] == 50 and a.waiting_on == ["user"]
        assert a.next_wake_at == "2026-11-01T00:00:00.000Z" and a.budget_low_sent is True
        assert a.updated_at >= before
        with pytest.raises(ValueError):
            store.update_assignment(db_conn, assignment_id, state="active")
        with pytest.raises(ValueError):
            store.update_assignment(db_conn, assignment_id, user_id="bob")

    def test_set_state(self, db_conn):
        assignment_id = _assignment(db_conn)
        store.set_state(db_conn, assignment_id, "waiting", waiting_on=["completion"])
        a = store.get_assignment(db_conn, assignment_id)
        assert a.state == "waiting" and a.waiting_on == ["completion"] and a.state_reason is None
        store.set_state(db_conn, assignment_id, "paused", reason="budget")
        a = store.get_assignment(db_conn, assignment_id)
        assert a.state == "paused" and a.state_reason == "budget" and a.waiting_on == []


class TestItemsAndFrozen:
    def test_items_round_trip_in_position_order(self, db_conn):
        assignment_id = _assignment(db_conn)
        validator = {"check": "csv_columns", "file": "quotes.csv", "columns": ["source", "date"], "min_rows": 3}
        first = store.insert_item(db_conn, assignment_id, text="Three quotes", kind="mechanical", validator=validator)
        second = store.insert_item(db_conn, assignment_id, text="I picked one", kind="confirmed")
        items = store.list_items(db_conn, assignment_id)
        assert [(i.id, i.position) for i in items] == [(first, 1), (second, 2)]
        assert items[0].validator == validator and items[0].status == "open"
        assert items[1].validator is None and items[1].kind == "confirmed"

        store.set_item_status(db_conn, first, "failing", detail="file missing", checked_at="2026-11-01T00:00:00.000Z")
        item = store.get_item(db_conn, assignment_id, first)
        assert (item.status, item.detail, item.checked_at) == ("failing", "file missing", "2026-11-01T00:00:00.000Z")
        assert store.get_item(db_conn, assignment_id + 1, first) is None

        assert store.delete_item(db_conn, assignment_id, second) is True
        assert store.delete_item(db_conn, assignment_id, second) is False
        third = store.insert_item(db_conn, assignment_id, text="Again", kind="confirmed")
        assert store.get_item(db_conn, assignment_id, third).position == 2

    def test_item_kind_is_checked(self, db_conn):
        assignment_id = _assignment(db_conn)
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_item(db_conn, assignment_id, text="x", kind="vibes")

    def test_frozen_round_trip(self, db_conn):
        assignment_id = _assignment(db_conn)
        frozen_id = store.insert_frozen(db_conn, assignment_id, name="price_ceiling", value="1200 USD")
        assert [(f.id, f.name, f.value) for f in store.list_frozen(db_conn, assignment_id)] == [
            (frozen_id, "price_ceiling", "1200 USD"),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_frozen(db_conn, assignment_id, name="price_ceiling", value="1300 USD")
        assert store.delete_frozen(db_conn, assignment_id + 1, frozen_id) is False
        assert store.delete_frozen(db_conn, assignment_id, frozen_id) is True
        assert store.list_frozen(db_conn, assignment_id) == []


class TestWakes:
    def test_seq_counts_per_assignment(self, db_conn):
        a = _assignment(db_conn, slug="a")
        b = _assignment(db_conn, slug="b")
        w1 = store.insert_wake(db_conn, a, fired_by="time", task_id=100)
        w2 = store.insert_wake(db_conn, a, fired_by="inbox", task_id=101)
        w3 = store.insert_wake(db_conn, b, fired_by="deadline")
        assert [store.get_wake(db_conn, w).seq for w in (w1, w2, w3)] == [1, 2, 1]
        assert store.count_wakes(db_conn, a) == 2

    def test_round_trip_and_update(self, db_conn):
        a = _assignment(db_conn)
        wake_id = store.insert_wake(db_conn, a, fired_by="time", task_id=100)
        wake = store.get_wake(db_conn, wake_id)
        assert (wake.assignment_id, wake.kind, wake.fired_by, wake.task_id) == (a, "work", "time", 100)
        assert wake.progress_signals == [] and wake.next_request is None and wake.ended_at is None
        assert store.get_wake_by_task(db_conn, 100).id == wake_id
        assert store.open_wake(db_conn, a).id == wake_id

        store.update_wake(db_conn, wake_id, started_at="2026-11-01T00:00:00.000Z",
                          next_request={"wait": ["user"]}, complete_request="all three in",
                          progress_note="found two", summary="did things",
                          progress_signals=["item_passing"], cost_usd=0.41,
                          ended_at="2026-11-01T00:10:00.000Z", outcome="next")
        wake = store.get_wake(db_conn, wake_id)
        assert wake.next_request == {"wait": ["user"]} and wake.progress_signals == ["item_passing"]
        assert (wake.complete_request, wake.progress_note, wake.summary) == ("all three in", "found two", "did things")
        assert (wake.cost_usd, wake.outcome) == (0.41, "next")
        assert store.open_wake(db_conn, a) is None
        with pytest.raises(ValueError):
            store.update_wake(db_conn, wake_id, seq=9)

    def test_list_is_newest_first(self, db_conn):
        a = _assignment(db_conn)
        ids = [store.insert_wake(db_conn, a, fired_by="time") for _ in range(3)]
        assert [w.id for w in store.list_wakes(db_conn, a)] == ids[::-1]
        assert [w.id for w in store.list_wakes(db_conn, a, limit=2)] == ids[:0:-1]

    def test_fired_by_is_checked(self, db_conn):
        a = _assignment(db_conn)
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_wake(db_conn, a, fired_by="whim")


class TestSnapshots:
    def test_latest_by_phase(self, db_conn):
        a = _assignment(db_conn)
        seed = store.insert_snapshot(db_conn, a, phase="seed", content="# Now\n", sha256="s0")
        wake_id = store.insert_wake(db_conn, a, fired_by="time")
        store.insert_snapshot(db_conn, a, phase="before", content="# Now\nx\n", sha256="s1", wake_id=wake_id)
        after = store.insert_snapshot(db_conn, a, phase="after", content="# Now\ny\n", sha256="s2",
                                      wake_id=wake_id, truncated=True)
        latest = store.latest_snapshot(db_conn, a, phases=("after", "seed"))
        assert latest.id == after and latest.truncated is True and latest.wake_id == wake_id
        assert store.latest_snapshot(db_conn, a, phases=("seed",)).id == seed
        assert store.latest_snapshot(db_conn, a).content == "# Now\ny\n"
        assert store.latest_snapshot(db_conn, a + 1) is None


class TestInbox:
    def test_consume_claims_open_events_in_order(self, db_conn):
        a = _assignment(db_conn)
        first = store.insert_inbox_event(db_conn, a, kind="user_note", payload={"text": "hi"}, trusted=True)
        second = store.insert_inbox_event(db_conn, a, kind="budget_low", payload={}, trusted=True)
        assert store.has_unconsumed(db_conn, a)
        assert store.has_unconsumed(db_conn, a, kinds=("user_note",))
        assert not store.has_unconsumed(db_conn, a, kinds=("handoff_done",))
        wake_id = store.insert_wake(db_conn, a, fired_by="inbox")
        assert store.consume_inbox(db_conn, a, wake_id) == 2
        events = store.list_inbox(db_conn, a, consumed_by_wake=wake_id)
        assert [e.id for e in events] == [first, second]
        assert events[0].payload == {"text": "hi"} and events[0].trusted is True
        assert not store.has_unconsumed(db_conn, a)
        later = store.insert_inbox_event(db_conn, a, kind="user_note", payload={"text": "more"}, trusted=True)
        assert [e.id for e in store.list_inbox(db_conn, a, unconsumed=True)] == [later]
        assert store.consume_inbox(db_conn, a, wake_id + 1) == 1
        assert [e.id for e in store.list_inbox(db_conn, a)] == [first, second, later]

    def test_untrusted_is_the_default(self, db_conn):
        a = _assignment(db_conn)
        store.insert_inbox_event(db_conn, a, kind="watcher", payload={"x": 1})
        assert store.list_inbox(db_conn, a)[0].trusted is False


class TestActionsAttemptsEvents:
    def test_handoff_round_trip(self, db_conn):
        a = _assignment(db_conn)
        action_id = store.insert_action(db_conn, a, text="photograph the furnace label",
                                        report_spec="the photo", due_at="2026-11-05", wake_id=3)
        action = store.get_action(db_conn, a, action_id)
        assert (action.kind, action.status, action.text, action.report_spec, action.due_at, action.wake_id) == (
            "handoff", "open", "photograph the furnace label", "the photo", "2026-11-05", 3,
        )
        assert [x.id for x in store.list_actions(db_conn, a, status="open")] == [action_id]
        assert store.resolve_action(db_conn, a, action_id, response="photo at inbox/x.jpg") is True
        assert store.resolve_action(db_conn, a, action_id, response="again") is False
        action = store.get_action(db_conn, a, action_id)
        assert action.status == "done" and action.response == "photo at inbox/x.jpg" and action.resolved_at
        assert store.list_actions(db_conn, a, status="open") == []
        assert store.get_action(db_conn, a + 1, action_id) is None

    def test_action_kind_is_checked(self, db_conn):
        a = _assignment(db_conn)
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_action(db_conn, a, text="?", kind="question")

    def test_attempts(self, db_conn):
        a = _assignment(db_conn)
        first = store.insert_attempt(db_conn, a, target="download", tried="wget", outcome="failed", wake_id=1)
        second = store.insert_attempt(db_conn, a, target="download", tried="curl", outcome="failed", wake_id=2)
        other = store.insert_attempt(db_conn, a, target="login", tried="form", outcome="ok", wake_id=2)
        assert [x.id for x in store.list_attempts(db_conn, a)] == [other, second, first]
        assert [x.id for x in store.list_attempts(db_conn, a, target="download")] == [second, first]
        assert store.list_attempts(db_conn, a, limit=1)[0].outcome == "ok"
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_attempt(db_conn, a, target="x", tried="y", outcome="meh")

    def test_events(self, db_conn):
        a = _assignment(db_conn)
        first = store.insert_event(db_conn, a, kind="created", actor="user")
        second = store.insert_event(db_conn, a, kind="scratchpad_restored", actor="system",
                                    payload={"reason": "empty"}, wake_id=4)
        events = store.list_events(db_conn, a)
        assert [e.id for e in events] == [first, second]
        assert events[0].payload == {} and events[1].payload == {"reason": "empty"} and events[1].wake_id == 4
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_event(db_conn, a, kind="x", actor="model")


class TestConfig:
    def test_defaults(self):
        from istota.config import Config

        cfg = Config().assignments
        assert (cfg.max_workers, cfg.user_max_workers, cfg.max_active_per_user) == (2, 1, 3)
        assert (cfg.max_cost_usd_per_assignment, cfg.max_wakes_per_assignment) == (50.0, 500)
        assert (cfg.min_wake_interval_minutes, cfg.stall_wakes, cfg.failure_wakes) == (15, 3, 3)
        assert cfg.budget_low_fraction == 0.75
        assert (cfg.scratchpad_prompt_chars, cfg.history_prompt_chars) == (24000, 12000)

    def test_the_section_loads(self, tmp_path):
        from istota.config import load_config

        path = tmp_path / "config.toml"
        path.write_text("[assignments]\nmax_workers = 4\nmax_cost_usd_per_assignment = 12.5\n")
        cfg = load_config(path).assignments
        assert cfg.max_workers == 4 and cfg.max_cost_usd_per_assignment == 12.5
        assert cfg.user_max_workers == 1
