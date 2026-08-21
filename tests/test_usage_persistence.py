"""Stage 3: the executor's usage persistence.

Replaces `tests/native/test_usage_persistence.py`, whose three tests all
asserted on the `task_logs` text this change removes. The `conn is None` case is
carried over deliberately — the daemon path uses it (`scheduler` calls
`execute_task` outside the db context), so it is a live branch rather than
legacy.
"""

import pytest

from istota import db
from istota.executor import _persist_task_usage, persist_brain_usage
from istota.usage import BrainUsage, ModelUsage


def _usage(**kw):
    base = dict(
        billed_input_tokens=550,
        output_tokens=161,
        cache_read_tokens=14425,
        cache_write_tokens=14565,
        cost_usd=0.0319275,
        cost_basis="api",
        totals_source="model_usage",
        has_totals=True,
        turns=2,
        model_requests=2,
        initial_context_tokens=14434,
        peak_context_tokens=14573,
        context_window=200000,
        model="model-a",
    )
    models = kw.pop("models", None)
    base.update(kw)
    u = BrainUsage(**base)
    u.models = models if models is not None else [
        ModelUsage(
            model=base["model"],
            billed_input_tokens=base["billed_input_tokens"],
            output_tokens=base["output_tokens"],
            cost_usd=base["cost_usd"],
            context_window=200000,
        )
    ]
    return u


class _Cfg:
    def __init__(self, dbp):
        self.db_path = dbp


@pytest.fixture
def env(tmp_path):
    dbp = tmp_path / "istota.db"
    db.init_db(dbp)
    with db.get_db(dbp) as conn:
        tid = db.create_task(conn, prompt="x", user_id="alice", source_type="cli")
    return _Cfg(dbp), dbp, tid


def _rows(dbp):
    with db.get_db(dbp) as conn:
        return list(conn.execute("SELECT * FROM task_usage ORDER BY id").fetchall())


class TestTaskRows:
    def test_writes_one_parent_and_its_children(self, env):
        cfg, dbp, tid = env
        usage = _usage(
            models=[
                ModelUsage(model="model-a", billed_input_tokens=10, cost_usd=0.1),
                ModelUsage(model="model-b", billed_input_tokens=20, cost_usd=0.2),
            ]
        )
        with db.get_db(dbp) as conn:
            _persist_task_usage(
                cfg, conn, tid, usage, user_id="alice", source_type="cli",
                brain_kind="claude_code", success=True,
            )

        rows = _rows(dbp)
        assert len(rows) == 1
        assert rows[0]["task_id"] == tid
        assert rows[0]["origin"] == "task"
        assert rows[0]["brain_kind"] == "claude_code"
        assert rows[0]["source_type"] == "cli"
        assert rows[0]["success"] == 1
        with db.get_db(dbp) as conn:
            children = conn.execute(
                "SELECT model FROM task_usage_models WHERE task_usage_id = ?"
                " ORDER BY model",
                (rows[0]["id"],),
            ).fetchall()
        assert [c["model"] for c in children] == ["model-a", "model-b"]

    def test_carries_the_measured_columns(self, env):
        cfg, dbp, tid = env
        with db.get_db(dbp) as conn:
            _persist_task_usage(
                cfg, conn, tid, _usage(), user_id="alice", brain_kind="claude_code",
            )

        row = _rows(dbp)[0]
        assert row["billed_input_tokens"] == 550
        assert row["output_tokens"] == 161
        assert row["cache_read_tokens"] == 14425
        assert row["cache_write_tokens"] == 14565
        assert row["has_totals"] == 1
        assert row["cost_basis"] == "api"
        assert row["initial_context_tokens"] == 14434
        assert row["peak_context_tokens"] == 14573

    def test_the_callers_model_is_recorded_when_usage_carries_none(self, env):
        """A native row reports one total with no per-model split, so
        `usage.model` is empty and the caller's value is the only one there is.
        Every other test here uses a fixture that pre-populates `usage.model`,
        which is exactly why a dropped `model` argument reads as working."""
        cfg, dbp, tid = env
        native = _usage(models=[], model="")

        with db.get_db(dbp) as conn:
            _persist_task_usage(
                cfg, conn, tid, native, user_id="alice", brain_kind="native",
                model="claude-sonnet-5", effort="high",
            )

        row = _rows(dbp)[0]
        assert row["model"] == "claude-sonnet-5"
        assert row["effort"] == "high"

    def test_usage_model_is_the_fallback_when_the_caller_names_none(self, env):
        cfg, dbp, tid = env
        with db.get_db(dbp) as conn:
            _persist_task_usage(
                cfg, conn, tid, _usage(model="model-a"), user_id="alice",
                brain_kind="claude_code",
            )

        assert _rows(dbp)[0]["model"] == "model-a"

    def test_the_callers_model_wins_over_the_dominant_model(self, env):
        """They can disagree: `usage.model` is the CLI's cost-weighted dominant
        model, which is not the same answer on a run whose out-of-band calls
        outweigh a cheap main turn."""
        cfg, dbp, tid = env
        with db.get_db(dbp) as conn:
            _persist_task_usage(
                cfg, conn, tid, _usage(model="model-a"), user_id="alice",
                brain_kind="claude_code", model="model-b",
            )

        assert _rows(dbp)[0]["model"] == "model-b"

    def test_none_usage_is_a_noop(self, env):
        cfg, dbp, tid = env
        with db.get_db(dbp) as conn:
            _persist_task_usage(cfg, conn, tid, None, user_id="alice")

        assert _rows(dbp) == []

    def test_opens_own_conn_when_none(self, env):
        """Carried over from the replaced file. The daemon path uses this branch
        — the scheduler calls `execute_task` outside the db context."""
        cfg, dbp, tid = env

        _persist_task_usage(
            cfg, None, tid, _usage(), user_id="alice", brain_kind="native",
        )

        rows = _rows(dbp)
        assert len(rows) == 1
        assert rows[0]["brain_kind"] == "native"

    def test_a_failure_is_recorded_too(self, env):
        """Tokens are spent whether or not the run succeeded."""
        cfg, dbp, tid = env
        with db.get_db(dbp) as conn:
            _persist_task_usage(
                cfg, conn, tid, _usage(has_totals=False, models=[]),
                user_id="alice", brain_kind="claude_code",
                stop_reason="timeout", success=False,
            )

        row = _rows(dbp)[0]
        assert row["success"] == 0
        assert row["stop_reason"] == "timeout"
        assert row["has_totals"] == 0
        # Context survives a run with no result frame — the two measures are
        # independent, which is why they have independent filters.
        assert row["initial_context_tokens"] == 14434


class TestFallback:
    def test_both_attempts_are_recorded_with_distinct_identities(self, env):
        """The primary's numbers must not be overwritten by the fallback's. The
        fallback replaces `brain_result` in the executor, so before this both
        rows would have been one row carrying the fallback's tokens under the
        primary's brain name."""
        cfg, dbp, tid = env
        primary = _usage(billed_input_tokens=100, model="model-a")
        fallback = _usage(billed_input_tokens=999, model="model-b")

        with db.get_db(dbp) as conn:
            _persist_task_usage(
                cfg, conn, tid, primary, user_id="alice",
                brain_kind="claude_code", stop_reason="usage_limit",
            )
            _persist_task_usage(
                cfg, conn, tid, fallback, user_id="alice", brain_kind="native",
                is_fallback=True, success=True,
            )

        rows = _rows(dbp)
        assert [r["attempt_seq"] for r in rows] == [1, 2]
        assert [r["brain_kind"] for r in rows] == ["claude_code", "native"]
        assert [r["is_fallback"] for r in rows] == [0, 1]
        assert rows[0]["billed_input_tokens"] == 100
        assert rows[1]["billed_input_tokens"] == 999

    def test_a_retry_takes_the_next_attempt_seq(self, env):
        cfg, dbp, tid = env
        for _ in range(3):
            with db.get_db(dbp) as conn:
                _persist_task_usage(
                    cfg, conn, tid, _usage(), user_id="alice",
                    brain_kind="claude_code",
                )

        assert [r["attempt_seq"] for r in _rows(dbp)] == [1, 2, 3]


class TestNonTaskCallers:
    def test_writes_a_row_with_no_task_and_its_origin(self, env):
        """The sleep cycle, shared blocks and health OCR run the brain with no
        task at all. Keyed on task_id they would be invisible in both
        directions."""
        cfg, dbp, _ = env

        with db.get_db(dbp) as conn:
            persist_brain_usage(
                cfg, conn, usage=_usage(), origin="sleep_cycle",
                user_id="alice", brain_kind="claude_code", success=True,
            )

        row = _rows(dbp)[0]
        assert row["task_id"] is None
        assert row["origin"] == "sleep_cycle"
        assert row["source_type"] == ""

    def test_several_non_task_rows_do_not_collide(self, env):
        """They all carry task_id NULL; the unique index is partial for this."""
        cfg, dbp, _ = env
        with db.get_db(dbp) as conn:
            for origin in ("sleep_cycle", "shared_blocks", "health_ocr"):
                persist_brain_usage(
                    cfg, conn, usage=_usage(), origin=origin, user_id="alice",
                    brain_kind="claude_code",
                )

        assert {r["origin"] for r in _rows(dbp)} == {
            "sleep_cycle", "shared_blocks", "health_ocr",
        }


class TestBestEffort:
    def test_a_write_failure_never_raises(self, env, monkeypatch):
        """Telemetry must not turn a completed task into a failed one."""
        cfg, dbp, tid = env

        def boom(*a, **k):
            raise db.sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr("istota.executor._insert_usage_row", boom)

        with db.get_db(dbp) as conn:
            _persist_task_usage(cfg, conn, tid, _usage(), user_id="alice")

        assert _rows(dbp) == []

    def test_the_breadcrumb_survives_a_db_failure(self, env, monkeypatch, caplog):
        """The greppable figure is what is left when the DB write is the thing
        that failed, so it is logged before the write is attempted."""
        cfg, dbp, tid = env
        monkeypatch.setattr(
            "istota.executor._insert_usage_row",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")),
        )

        with caplog.at_level("INFO", logger="istota.executor"):
            with db.get_db(dbp) as conn:
                _persist_task_usage(
                    cfg, conn, tid, _usage(), user_id="alice",
                    brain_kind="claude_code",
                )

        assert any("brain_usage" in r.message for r in caplog.records)
        assert any(r.levelname == "WARNING" for r in caplog.records)

    def test_a_failing_child_leaves_no_parent(self, env):
        """The SAVEPOINT guard, through the executor's swallow: the bare except
        must only ever be able to hide a complete failure."""
        cfg, dbp, tid = env
        usage = _usage(
            models=[
                ModelUsage(model="model-a", billed_input_tokens=10),
                ModelUsage(model="boom", billed_input_tokens=20),
            ]
        )

        class FailsOnBoom:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, params=()):
                if "INTO task_usage_models" in sql and "boom" in tuple(params):
                    raise db.sqlite3.OperationalError("disk I/O error")
                return self._conn.execute(sql, params)

        with db.get_db(dbp) as conn:
            _persist_task_usage(
                cfg, FailsOnBoom(conn), tid, usage, user_id="alice",
                brain_kind="claude_code",
            )

        assert _rows(dbp) == []
        with db.get_db(dbp) as conn:
            orphans = conn.execute(
                "SELECT COUNT(*) FROM task_usage_models"
            ).fetchone()[0]
        assert orphans == 0


class TestRetention:
    def test_the_row_outlives_its_task(self, env):
        """The whole reason this is not a `task_logs` entry."""
        cfg, dbp, tid = env
        with db.get_db(dbp) as conn:
            _persist_task_usage(
                cfg, conn, tid, _usage(), user_id="alice", brain_kind="claude_code",
            )
            conn.execute(
                "UPDATE tasks SET status = 'completed',"
                " completed_at = datetime('now', '-30 days') WHERE id = ?",
                (tid,),
            )

        with db.get_db(dbp) as conn:
            deleted = db.cleanup_old_tasks(conn, 7)

        assert deleted == 1
        rows = _rows(dbp)
        assert len(rows) == 1
        assert rows[0]["billed_input_tokens"] == 550
        # The task is gone, so task_id dangles. Every aggregate reads the
        # denormalized columns rather than joining.
        assert rows[0]["user_id"] == "alice"
