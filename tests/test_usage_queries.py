"""Stage 2: the `task_usage` tables, their queries, and the prune path.

Full tier — a schema plus a deletion path. The seeded fixture spans three users,
three models, two brains, three cost bases, two origins and a 40-day range, so
each grouping has something to get wrong.

Two date formats meet in this module and confusing them is the failure this
change is most likely to ship. `task_usage.created_at` is ISO-Z
(`2026-08-20T09:00:00.000Z`); `tasks.created_at` is `datetime('now')`
(`2026-08-20 09:00:00`). `' '` (0x20) sorts below `'T'` (0x54), so a bound in
the wrong format silently drops every row on the boundary day rather than
raising.
"""

from datetime import datetime, timedelta, timezone

import pytest

from istota import db
from istota.usage import BrainUsage, ModelUsage


def _iso(dt):
    """The format `task_usage.created_at` stores."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _sql_datetime(dt):
    """The format `tasks.created_at` stores."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


NOW = datetime(2026, 8, 20, 9, 0, 0, tzinfo=timezone.utc)


def _usage(**kw):
    """A BrainUsage with plausible non-zero defaults, overridden per case."""
    base = dict(
        billed_input_tokens=100,
        output_tokens=50,
        cache_read_tokens=1000,
        cache_write_tokens=200,
        cost_usd=0.01,
        cost_basis="api",
        totals_source="model_usage",
        has_totals=True,
        turns=1,
        model_requests=2,
        initial_context_tokens=10000,
        peak_context_tokens=12000,
        context_window=200000,
        duration_ms=1000,
        duration_api_ms=900,
        service_tier="standard",
        model="model-a",
    )
    models = kw.pop("models", None)
    base.update(kw)
    u = BrainUsage(**base)
    u.models = (
        models
        if models is not None
        else [
            ModelUsage(
                model=base["model"],
                billed_input_tokens=base["billed_input_tokens"],
                output_tokens=base["output_tokens"],
                cache_read_tokens=base["cache_read_tokens"],
                cache_write_tokens=base["cache_write_tokens"],
                cost_usd=base["cost_usd"],
                context_window=base["context_window"] or 0,
            )
        ]
    )
    return u


def _insert(conn, *, days_ago=0, at=None, **kw):
    """Insert one usage row, stamped relative to NOW unless `at` is given."""
    created = at if at is not None else NOW - timedelta(days=days_ago)
    kw.setdefault("user_id", "alice")
    kw.setdefault("brain_kind", "claude_code")
    kw.setdefault("origin", "task")
    usage_kw = {
        k: kw.pop(k)
        for k in list(kw)
        if k
        in {
            "billed_input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cost_usd",
            "cost_basis",
            "totals_source",
            "has_totals",
            "turns",
            "model_requests",
            "initial_context_tokens",
            "peak_context_tokens",
            "context_window",
            "model",
            "models",
        }
    }
    row_id = db.insert_task_usage(conn, usage=_usage(**usage_kw), **kw)
    conn.execute(
        "UPDATE task_usage SET created_at = ? WHERE id = ?", (_iso(created), row_id)
    )
    return row_id


@pytest.fixture
def seeded(db_conn):
    """Three users, three models, two brains, three bases, two origins, 40 days."""
    c = db_conn
    # alice: two claude_code task rows inside the 7-day window, api-priced.
    _insert(c, days_ago=1, user_id="alice", model="model-a", source_type="talk")
    _insert(c, days_ago=2, user_id="alice", model="model-b", source_type="talk")
    # alice: a non-task row, so usage rows can exceed her task count.
    _insert(c, days_ago=1, user_id="alice", origin="sleep_cycle", model="model-a")
    # bob: a native row — derived totals, NULL context, estimated cost.
    _insert(
        c,
        days_ago=3,
        user_id="bob",
        brain_kind="native",
        model="model-c",
        cost_basis="estimated",
        totals_source="derived",
        initial_context_tokens=None,
        peak_context_tokens=None,
        context_window=None,
    )
    # bob: a subscription-priced row.
    _insert(c, days_ago=4, user_id="bob", model="model-a", cost_basis="subscription")
    # carol: a timeout — real context, no usable totals.
    _insert(
        c,
        days_ago=1,
        user_id="carol",
        has_totals=False,
        totals_source="unknown",
        billed_input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.0,
        models=[],
        initial_context_tokens=99999,
        peak_context_tokens=99999,
    )
    # carol: outside every window under test.
    _insert(c, days_ago=40, user_id="carol", model="model-a")
    c.commit()
    return c


class TestSchema:
    def test_tables_exist_after_init(self, db_conn):
        names = {
            r[0]
            for r in db_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "task_usage" in names
        assert "task_usage_models" in names

    def test_task_id_is_nullable_and_not_foreign_keyed(self, db_conn):
        """Non-task callers have no task row, and a row must outlive its task."""
        row_id = db.insert_task_usage(
            db_conn, usage=_usage(), user_id="alice", brain_kind="claude_code",
            origin="sleep_cycle",
        )
        stored = db_conn.execute(
            "SELECT task_id, origin FROM task_usage WHERE id = ?", (row_id,)
        ).fetchone()
        assert stored["task_id"] is None
        assert stored["origin"] == "sleep_cycle"

    def test_created_at_default_is_iso_z(self, db_conn):
        """The whole module's date handling rests on this format."""
        row_id = db.insert_task_usage(
            db_conn, usage=_usage(), user_id="alice", brain_kind="claude_code"
        )
        created = db_conn.execute(
            "SELECT created_at FROM task_usage WHERE id = ?", (row_id,)
        ).fetchone()[0]
        assert created.endswith("Z")
        assert "T" in created
        assert " " not in created


class TestInsert:
    def test_writes_parent_and_children(self, db_conn):
        usage = _usage(
            models=[
                ModelUsage(model="model-a", billed_input_tokens=10, cost_usd=0.1),
                ModelUsage(model="model-b", billed_input_tokens=20, cost_usd=0.2),
            ]
        )
        row_id = db.insert_task_usage(
            db_conn, usage=usage, task_id=1, user_id="alice", brain_kind="claude_code"
        )

        children = db_conn.execute(
            "SELECT model FROM task_usage_models WHERE task_usage_id = ? ORDER BY model",
            (row_id,),
        ).fetchall()
        assert [r["model"] for r in children] == ["model-a", "model-b"]

    def test_null_context_is_stored_as_null_not_zero(self, db_conn):
        """SQL AVG skips NULL; a zero would halve a mixed-brain mean."""
        row_id = db.insert_task_usage(
            db_conn,
            usage=_usage(
                initial_context_tokens=None,
                peak_context_tokens=None,
                context_window=None,
            ),
            user_id="bob",
            brain_kind="native",
        )
        row = db_conn.execute(
            "SELECT initial_context_tokens, peak_context_tokens, context_window"
            " FROM task_usage WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["initial_context_tokens"] is None
        assert row["peak_context_tokens"] is None
        assert row["context_window"] is None

    def test_attempt_seq_increments_per_task(self, db_conn):
        first = db.insert_task_usage(
            db_conn, usage=_usage(), task_id=7, user_id="alice", brain_kind="claude_code"
        )
        second = db.insert_task_usage(
            db_conn, usage=_usage(), task_id=7, user_id="alice", brain_kind="native"
        )
        third = db.insert_task_usage(
            db_conn, usage=_usage(), task_id=7, user_id="alice", brain_kind="native"
        )

        seqs = [
            db_conn.execute(
                "SELECT attempt_seq FROM task_usage WHERE id = ?", (r,)
            ).fetchone()[0]
            for r in (first, second, third)
        ]
        assert seqs == [1, 2, 3]

    def test_attempt_seq_is_per_task_not_global(self, db_conn):
        db.insert_task_usage(
            db_conn, usage=_usage(), task_id=1, user_id="a", brain_kind="claude_code"
        )
        other = db.insert_task_usage(
            db_conn, usage=_usage(), task_id=2, user_id="a", brain_kind="claude_code"
        )
        seq = db_conn.execute(
            "SELECT attempt_seq FROM task_usage WHERE id = ?", (other,)
        ).fetchone()[0]
        assert seq == 1

    def test_null_task_rows_do_not_collide_on_the_unique_index(self, db_conn):
        """The index is partial (`WHERE task_id IS NOT NULL`) for this reason."""
        for _ in range(3):
            db.insert_task_usage(
                db_conn, usage=_usage(), user_id="alice",
                brain_kind="claude_code", origin="sleep_cycle",
            )
        count = db_conn.execute(
            "SELECT COUNT(*) FROM task_usage WHERE task_id IS NULL"
        ).fetchone()[0]
        assert count == 3

    def test_a_failing_child_leaves_no_parent(self, db_conn):
        """The SAVEPOINT guard. A partial per-model split is the one thing the
        bare `except` downstream must never be able to commit, because
        `--by model` depends on the parent equalling the sum of its children.

        The connection is wrapped rather than monkeypatched: `execute` on a real
        `sqlite3.Connection` is read-only, and the SAVEPOINT has to be exercised
        against a real one — a mock would prove nothing about SQLite's own
        rollback semantics."""
        usage = _usage(
            models=[
                ModelUsage(model="model-a", billed_input_tokens=10),
                ModelUsage(model="model-b", billed_input_tokens=20),
                ModelUsage(model="boom", billed_input_tokens=30),
            ]
        )

        class FailsOnThirdChild:
            """Delegates to the real connection, failing one specific insert."""

            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, params=()):
                if "INTO task_usage_models" in sql and "boom" in tuple(params):
                    raise db.sqlite3.OperationalError("disk I/O error")
                return self._conn.execute(sql, params)

        with pytest.raises(db.sqlite3.OperationalError):
            db.insert_task_usage(
                FailsOnThirdChild(db_conn), usage=usage, task_id=1,
                user_id="alice", brain_kind="claude_code",
            )

        assert db_conn.execute("SELECT COUNT(*) FROM task_usage").fetchone()[0] == 0
        assert (
            db_conn.execute("SELECT COUNT(*) FROM task_usage_models").fetchone()[0] == 0
        )

    def test_a_successful_insert_survives_the_savepoint_release(self, db_conn):
        """The other half: RELEASE must not discard the work it wrapped."""
        row_id = db.insert_task_usage(
            db_conn,
            usage=_usage(models=[ModelUsage(model="model-a", billed_input_tokens=10)]),
            task_id=1,
            user_id="alice",
            brain_kind="claude_code",
        )
        db_conn.commit()

        assert db_conn.execute("SELECT COUNT(*) FROM task_usage").fetchone()[0] == 1
        assert (
            db_conn.execute(
                "SELECT COUNT(*) FROM task_usage_models WHERE task_usage_id = ?",
                (row_id,),
            ).fetchone()[0]
            == 1
        )


class TestQueryUsage:
    def test_days_window_excludes_older_rows(self, seeded):
        rows = db.query_usage(seeded, since=_iso(NOW - timedelta(days=7)), until=None)
        assert len(rows) == 6  # the 40-day-old carol row is out

    def test_filters(self, seeded):
        since = _iso(NOW - timedelta(days=40))
        assert len(db.query_usage(seeded, since=since, user_id="alice")) == 3
        assert len(db.query_usage(seeded, since=since, brain_kind="native")) == 1
        assert len(db.query_usage(seeded, since=since, origin="sleep_cycle")) == 1
        assert len(db.query_usage(seeded, since=since, source_type="talk")) == 2

    def test_until_is_half_open(self, seeded):
        """A bare `--until D` must be expanded by the caller to D+1 at midnight;
        this asserts the bound itself excludes its own instant."""
        at = datetime(2026, 8, 10, 0, 0, 0, tzinfo=timezone.utc)
        _insert(seeded, at=at, user_id="dave")
        seeded.commit()

        included = db.query_usage(
            seeded, since=_iso(at - timedelta(days=1)), until=_iso(at + timedelta(days=1))
        )
        assert any(r["user_id"] == "dave" for r in included)

        excluded = db.query_usage(
            seeded, since=_iso(at - timedelta(days=1)), until=_iso(at)
        )
        assert not any(r["user_id"] == "dave" for r in excluded)

    def test_until_day_includes_rows_stamped_that_day(self, seeded):
        """The trap: `--until 2026-08-20` must not lose the whole of 20 Aug."""
        at = datetime(2026, 8, 20, 13, 45, 0, tzinfo=timezone.utc)
        _insert(seeded, at=at, user_id="dave")
        seeded.commit()

        rows = db.query_usage(
            seeded,
            since=_iso(datetime(2026, 8, 1, tzinfo=timezone.utc)),
            until=_iso(datetime(2026, 8, 21, tzinfo=timezone.utc)),
        )
        assert any(r["user_id"] == "dave" for r in rows)


class TestUsageSummary:
    def test_token_aggregates_exclude_unmeasured_rows(self, seeded):
        """carol's timeout row has zero tokens and real context. Counting it
        would drag every mean toward zero."""
        summary = db.usage_summary(seeded, since=_iso(NOW - timedelta(days=7)))

        assert summary["rows"] == 6
        assert summary["measured_rows"] == 5
        # 5 measured rows x 100 billed input.
        assert summary["billed_input_tokens"] == 500

    def test_context_averages_exclude_null_rows(self, seeded):
        """The mixed-brain guard. One native row (NULL) plus claude_code rows at
        10000 must average to 10000, not to half of it."""
        summary = db.usage_summary(
            seeded, since=_iso(NOW - timedelta(days=7)), user_id="bob"
        )

        # bob has one native row (NULL context) and one claude_code row (10000).
        assert summary["context_rows"] == 1
        assert summary["avg_initial_context_tokens"] == pytest.approx(10000)

    def test_cost_is_a_map_keyed_by_basis_never_a_scalar(self, seeded):
        summary = db.usage_summary(seeded, since=_iso(NOW - timedelta(days=7)))

        assert isinstance(summary["cost_by_basis"], dict)
        assert set(summary["cost_by_basis"]) == {"api", "estimated", "subscription"}
        assert "cost_usd" not in summary

    def test_no_field_sums_across_cost_basis(self, seeded):
        """Guard against a later 'convenience total'."""
        summary = db.usage_summary(seeded, since=_iso(NOW - timedelta(days=7)))
        total = sum(summary["cost_by_basis"].values())

        assert not any(
            isinstance(v, float) and v == pytest.approx(total) and k != "unreachable"
            for k, v in summary.items()
            if k != "cost_by_basis"
        )


class TestGrouping:
    @pytest.mark.parametrize(
        "by", ["day", "user", "model", "source", "brain", "origin"]
    )
    def test_group_token_sums_equal_the_ungrouped_total(self, seeded, by):
        """Tokens partition cleanly under every grouping, including `model`."""
        since = _iso(NOW - timedelta(days=7))
        groups = db.usage_summary(seeded, since=since, group_by=by)
        ungrouped = db.usage_summary(seeded, since=since)

        for column in (
            "billed_input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            assert sum(g[column] for g in groups) == ungrouped[column], column

    @pytest.mark.parametrize("by", ["day", "user", "source", "brain", "origin"])
    def test_group_row_counts_partition_the_parent_rows(self, seeded, by):
        since = _iso(NOW - timedelta(days=7))
        groups = db.usage_summary(seeded, since=since, group_by=by)
        ungrouped = db.usage_summary(seeded, since=since)

        assert sum(g["rows"] for g in groups) == ungrouped["rows"]

    def test_by_model_counts_child_rows_not_parent_rows(self, seeded):
        """`model` is the one grouping whose row counts do not partition the
        parents, and the reason is not a defect: a run with no usable totals has
        no per-model split to appear in (carol's timeout row), and a run that
        used two models appears under both. Tokens still partition — that is
        what the sum assertion above covers."""
        since = _iso(NOW - timedelta(days=7))
        groups = db.usage_summary(seeded, since=since, group_by="model")
        ungrouped = db.usage_summary(seeded, since=since)

        assert sum(g["rows"] for g in groups) == ungrouped["measured_rows"]
        assert sum(g["rows"] for g in groups) < ungrouped["rows"]

    def test_by_model_reports_no_context_averages(self, seeded):
        """A run's peak belongs to the run. Averaging it once per model the run
        used would count one measurement several times."""
        groups = db.usage_summary(
            seeded, since=_iso(NOW - timedelta(days=7)), group_by="model"
        )
        assert all(g["avg_peak_context_tokens"] is None for g in groups)

    def test_unknown_grouping_raises(self, seeded):
        with pytest.raises(ValueError):
            db.usage_summary(seeded, since=_iso(NOW), group_by="nonsense")

    def test_by_day_sorts_chronologically(self, seeded):
        groups = db.usage_summary(
            seeded, since=_iso(NOW - timedelta(days=7)), group_by="day"
        )
        assert [g["key"] for g in groups] == sorted(g["key"] for g in groups)

    def test_other_groupings_sort_by_tokens_descending(self, seeded):
        groups = db.usage_summary(
            seeded, since=_iso(NOW - timedelta(days=7)), group_by="user"
        )
        totals = [g["total_tokens"] for g in groups]
        assert totals == sorted(totals, reverse=True)

    def test_by_user_partitions_correctly(self, seeded):
        groups = db.usage_summary(
            seeded, since=_iso(NOW - timedelta(days=7)), group_by="user"
        )
        by_key = {g["key"]: g for g in groups}

        assert by_key["alice"]["rows"] == 3
        assert by_key["bob"]["rows"] == 2
        assert by_key["carol"]["rows"] == 1

    def test_by_model_reads_the_child_table(self, seeded):
        """Per-model is the one grouping the parent row cannot answer."""
        groups = db.usage_summary(
            seeded, since=_iso(NOW - timedelta(days=7)), group_by="model"
        )
        assert {g["key"] for g in groups} == {"model-a", "model-b", "model-c"}

    def test_by_origin_separates_task_from_non_task(self, seeded):
        groups = db.usage_summary(
            seeded, since=_iso(NOW - timedelta(days=7)), group_by="origin"
        )
        by_key = {g["key"]: g for g in groups}

        assert by_key["task"]["rows"] == 5
        assert by_key["sleep_cycle"]["rows"] == 1

    def test_groups_carry_cost_basis_alongside_cost(self, seeded):
        """Both surfaces apply the render rule from the same data."""
        groups = db.usage_summary(
            seeded, since=_iso(NOW - timedelta(days=7)), group_by="user"
        )
        assert all("cost_by_basis" in g for g in groups)


class TestUnmeasuredTaskCount:
    def test_counts_tasks_with_no_usage_row(self, db_conn):
        """A tmux_claude task spends real tokens and writes no row. Counting it
        as zero-cost would make every average lie."""
        measured = db.create_task(db_conn, prompt="p", user_id="alice")
        unmeasured = db.create_task(db_conn, prompt="p", user_id="alice")
        db.insert_task_usage(
            db_conn, usage=_usage(), task_id=measured, user_id="alice",
            brain_kind="claude_code",
        )
        db_conn.commit()

        since = _sql_datetime(NOW - timedelta(days=7))
        assert db.unmeasured_task_count(db_conn, since=since) == 1
        assert unmeasured  # the row exists; it is the one counted

    def test_uses_the_space_separated_format(self, db_conn):
        """The boundary-day trap. A task created at 00:30 on the `since` day is
        inside the window. Passing an ISO-Z bound here compares `'2026-...T'`
        against `'2026-... '` and silently drops it — no error, just a wrong
        number."""
        task_id = db.create_task(db_conn, prompt="p", user_id="alice")
        at = datetime(2026, 8, 13, 0, 30, 0, tzinfo=timezone.utc)
        db_conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?", (_sql_datetime(at), task_id)
        )
        db_conn.commit()

        since = _sql_datetime(datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc))
        assert db.unmeasured_task_count(db_conn, since=since) == 1

        # And the same instant in the wrong format finds nothing — which is
        # what makes the assertion above meaningful rather than incidental.
        assert (
            db.unmeasured_task_count(
                db_conn,
                since=_iso(datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc)),
            )
            == 0
        )

    def test_scopes_by_user(self, db_conn):
        db.create_task(db_conn, prompt="p", user_id="alice")
        db.create_task(db_conn, prompt="p", user_id="bob")
        db_conn.commit()

        since = _sql_datetime(NOW - timedelta(days=7))
        assert db.unmeasured_task_count(db_conn, since=since, user_id="alice") == 1


class TestRetention:
    def test_aggregates_survive_cleanup_old_tasks(self, db_conn):
        """The whole reason this is not a `task_logs` entry. Task retention is
        7 days; usage retention is 180."""
        task_id = db.create_task(db_conn, prompt="p", user_id="alice")
        db_conn.execute(
            "UPDATE tasks SET status = 'completed', completed_at = ? WHERE id = ?",
            (_sql_datetime(NOW - timedelta(days=30)), task_id),
        )
        db.insert_task_usage(
            db_conn, usage=_usage(), task_id=task_id, user_id="alice",
            brain_kind="claude_code",
        )
        db_conn.commit()

        since = _iso(NOW - timedelta(days=60))
        before = db.usage_summary(db_conn, since=since)
        deleted = db.cleanup_old_tasks(db_conn, 7)
        after = db.usage_summary(db_conn, since=since)

        assert deleted == 1
        assert before == after
        assert after["billed_input_tokens"] == 100

    def test_prune_removes_old_rows_and_their_children(self, db_conn):
        old = _insert(db_conn, days_ago=200, user_id="alice")
        keep = _insert(db_conn, days_ago=10, user_id="alice")
        db_conn.commit()

        pruned = db.prune_old_usage(db_conn, 180)

        assert pruned == 1
        ids = {r[0] for r in db_conn.execute("SELECT id FROM task_usage")}
        assert ids == {keep}
        orphans = db_conn.execute(
            "SELECT COUNT(*) FROM task_usage_models WHERE task_usage_id = ?", (old,)
        ).fetchone()[0]
        assert orphans == 0

    def test_prune_leaves_no_orphans_at_all(self, db_conn):
        _insert(db_conn, days_ago=200, user_id="alice")
        _insert(db_conn, days_ago=1, user_id="alice")
        db_conn.commit()

        db.prune_old_usage(db_conn, 180)

        orphans = db_conn.execute(
            "SELECT COUNT(*) FROM task_usage_models m"
            " WHERE NOT EXISTS (SELECT 1 FROM task_usage p WHERE p.id = m.task_usage_id)"
        ).fetchone()[0]
        assert orphans == 0

    def test_prune_disabled_at_zero(self, db_conn):
        _insert(db_conn, days_ago=500, user_id="alice")
        db_conn.commit()

        assert db.prune_old_usage(db_conn, 0) == 0
        assert db_conn.execute("SELECT COUNT(*) FROM task_usage").fetchone()[0] == 1

    def test_prune_uses_iso_z_bounds(self, db_conn):
        """`cleanup_old_tasks` uses `datetime('now', '-N days')`. Copying that
        idiom here compares a space-separated bound against ISO-Z values and
        inverts same-day comparisons."""
        recent = _insert(db_conn, days_ago=0, user_id="alice")
        db_conn.commit()

        assert db.prune_old_usage(db_conn, 1) == 0
        assert db_conn.execute(
            "SELECT COUNT(*) FROM task_usage WHERE id = ?", (recent,)
        ).fetchone()[0] == 1


class TestConfig:
    def test_default_is_180_days(self, make_config):
        assert make_config().scheduler.usage_retention_days == 180

    def test_reads_from_toml(self, tmp_path):
        from istota.config import load_config

        cfg = tmp_path / "config.toml"
        cfg.write_text("[scheduler]\nusage_retention_days = 30\n")
        assert load_config(cfg).scheduler.usage_retention_days == 30
