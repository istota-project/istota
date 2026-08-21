"""Stage 5: the admin dashboard's usage data.

This is where the two date formats meet. `tasks.created_at` is
`2026-08-20 09:00:00`; `task_usage.created_at` is ISO-Z. `' '` (0x20) sorts
below `'T'` (0x54), so a bound in the wrong format drops every row on the
boundary day and reports a plausible smaller number instead of raising. The
boundary-day test below is the one that catches it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from istota import db, web_app
from istota.usage import BrainUsage, ModelUsage

NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _sql(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _usage(**kw):
    base = dict(
        billed_input_tokens=100,
        output_tokens=50,
        cache_read_tokens=1000,
        cache_write_tokens=200,
        cost_usd=0.25,
        cost_basis="api",
        totals_source="model_usage",
        has_totals=True,
        turns=1,
        model_requests=2,
        initial_context_tokens=10000,
        peak_context_tokens=12000,
        context_window=200000,
        model="model-a",
    )
    base.update(kw)
    u = BrainUsage(**base)
    u.models = [ModelUsage(model=base["model"], billed_input_tokens=base["billed_input_tokens"])]
    return u


@pytest.fixture
def conn(tmp_path, monkeypatch):
    dbp = tmp_path / "istota.db"
    db.init_db(dbp)
    monkeypatch.setattr(web_app, "_config", None)
    with db.get_db(dbp) as c:
        yield c


def _add_usage(conn, *, user, at, **kw):
    origin = kw.pop("origin", "task")
    task_id = kw.pop("task_id", None)
    rid = db.insert_task_usage(
        conn, usage=_usage(**kw), user_id=user, brain_kind="claude_code",
        origin=origin, task_id=task_id, success=True,
    )
    conn.execute("UPDATE task_usage SET created_at = ? WHERE id = ?", (_iso(at), rid))
    return rid


def _add_task(conn, *, user, at):
    tid = db.create_task(conn, prompt="p", user_id=user, source_type="talk")
    conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (_sql(at), tid))
    return tid


def _row(conn, user):
    rows = web_app._admin_users_section(conn, NOW)
    return next(r for r in rows if r["username"] == user)


class TestBoundaryDay:
    def test_a_task_and_a_usage_row_on_the_boundary_are_both_counted(self, conn):
        """The single most likely mistake in this change, and the one that
        produces plausible-looking numbers rather than an error. Seeds one task
        and one usage row both stamped 00:30 on the `since` day, each in its own
        format. This fails if the usage query reuses the task cutoff."""
        at = NOW - timedelta(hours=23, minutes=30)  # inside the 24h window
        tid = _add_task(conn, user="alice", at=at)
        _add_usage(conn, user="alice", at=at, task_id=tid)
        conn.commit()

        row = _row(conn, "alice")

        assert row["tasks_last_24h"] == 1
        assert row["usage_rows_24h"] == 1
        assert row["usage_tokens_24h"] == 1350

    def test_a_row_outside_the_window_is_excluded(self, conn):
        _add_usage(conn, user="alice", at=NOW - timedelta(hours=25))
        conn.commit()

        row = _row(conn, "alice")

        assert row["usage_rows_24h"] == 0
        assert row["usage_tokens_24h"] == 0
        # Still inside 30 days.
        assert row["usage_tokens_30d"] == 1350


class TestCostIsAMap:
    def test_a_user_spanning_two_bases_gets_a_two_key_map(self, conn):
        """An operator switching the CLI's auth mid-window is exactly this. A
        scalar would silently add a plan-equivalent to real spend on one row."""
        at = NOW - timedelta(hours=2)
        _add_usage(conn, user="alice", at=at, cost_basis="api", cost_usd=1.5)
        _add_usage(
            conn, user="alice", at=at, cost_basis="subscription", cost_usd=9.0
        )
        conn.commit()

        row = _row(conn, "alice")

        assert row["usage_cost_24h"] == {"api": 1.5, "subscription": 9.0}
        assert not isinstance(row["usage_cost_24h"], (int, float))

    def test_no_key_holds_the_sum_across_bases(self, conn):
        at = NOW - timedelta(hours=2)
        _add_usage(conn, user="alice", at=at, cost_basis="api", cost_usd=1.5)
        _add_usage(
            conn, user="alice", at=at, cost_basis="subscription", cost_usd=9.0
        )
        conn.commit()

        row = _row(conn, "alice")

        assert 10.5 not in row["usage_cost_24h"].values()
        assert "usage_cost_usd_24h" not in row


class TestNonTaskSpend:
    def test_a_user_whose_only_spend_is_non_task_reads_legibly(self, conn):
        """`usage_rows_24h` exceeding `tasks_last_24h` is by design — the
        column includes spend with no task row at all. `usage_by_origin_24h` is
        what makes that legible instead of looking like an arithmetic error."""
        _add_usage(
            conn, user="alice", at=NOW - timedelta(hours=2), origin="sleep_cycle"
        )
        conn.commit()

        row = _row(conn, "alice")

        assert row["tasks_last_24h"] == 0
        assert row["usage_rows_24h"] == 1
        assert "sleep_cycle" in row["usage_by_origin_24h"]
        assert row["usage_by_origin_24h"]["sleep_cycle"]["rows"] == 1

    def test_usage_rows_may_exceed_task_count(self, conn):
        at = NOW - timedelta(hours=2)
        tid = _add_task(conn, user="alice", at=at)
        _add_usage(conn, user="alice", at=at, task_id=tid)
        _add_usage(conn, user="alice", at=at, origin="health_ocr")
        conn.commit()

        row = _row(conn, "alice")

        assert row["tasks_last_24h"] == 1
        assert row["usage_rows_24h"] == 2
        assert set(row["usage_by_origin_24h"]) == {"task", "health_ocr"}


class TestContextAverages:
    def test_null_context_rows_are_excluded_from_the_average(self, conn):
        """The mixed-brain guard. One native row (NULL context) plus one
        measured row at 10000 averages to 10000, not to half of it."""
        at = NOW - timedelta(hours=2)
        _add_usage(conn, user="alice", at=at, initial_context_tokens=10000,
                   peak_context_tokens=12000)
        _add_usage(conn, user="alice", at=at, initial_context_tokens=None,
                   peak_context_tokens=None, context_window=None)
        conn.commit()

        row = _row(conn, "alice")

        assert row["usage_avg_initial_context"] == 10000.0
        assert row["usage_avg_peak_context"] == 12000.0


class TestEmptyUser:
    def test_a_user_with_no_rows_gets_zeros_and_none_contexts(self, conn):
        _add_task(conn, user="alice", at=NOW - timedelta(hours=2))
        conn.commit()

        row = _row(conn, "alice")

        assert row["usage_tokens_24h"] == 0
        assert row["usage_rows_24h"] == 0
        assert row["usage_cost_24h"] == {}
        assert row["usage_avg_initial_context"] is None
        assert row["usage_avg_peak_context"] is None
        assert row["usage_cache_hit_rate_24h"] == 0.0

    def test_the_unmeasured_counter_names_the_gap(self, conn):
        """A tmux-brain task spends real tokens and writes no row."""
        _add_task(conn, user="alice", at=NOW - timedelta(hours=2))
        conn.commit()

        row = _row(conn, "alice")

        assert row["usage_unmeasured_24h"] == 1


class TestFleetSection:
    def test_it_carries_totals_and_the_three_groupings(self, conn):
        at = NOW - timedelta(hours=2)
        _add_usage(conn, user="alice", at=at, model="model-a")
        _add_usage(conn, user="bob", at=at, model="model-b", origin="sleep_cycle")
        conn.commit()

        section = web_app._admin_usage_section(conn, NOW)

        assert section["totals_24h"]["rows"] == 2
        assert {g["key"] for g in section["by_model_30d"]} == {"model-a", "model-b"}
        assert {g["key"] for g in section["by_brain_30d"]} == {"claude_code"}
        assert {g["key"] for g in section["by_origin_24h"]} == {"task", "sleep_cycle"}

    def test_it_carries_no_per_user_list(self, conn):
        """Per-user belongs to the Users section. Duplicating it here would give
        the same data two places to disagree."""
        section = web_app._admin_usage_section(conn, NOW)

        assert not any("user" in key for key in section)

    def test_cost_basis_travels_with_cost_on_every_group(self, conn):
        """Both surfaces apply the render rule from the same data rather than
        re-deriving it."""
        at = NOW - timedelta(hours=2)
        _add_usage(conn, user="alice", at=at, cost_basis="subscription")
        conn.commit()

        section = web_app._admin_usage_section(conn, NOW)

        assert "cost_by_basis" in section["totals_24h"]
        for grouping in ("by_model_30d", "by_brain_30d", "by_origin_24h"):
            for group in section[grouping]:
                assert "cost_by_basis" in group

    def test_the_honesty_counters_are_present(self, conn):
        _add_task(conn, user="alice", at=NOW - timedelta(hours=2))
        _add_usage(
            conn, user="alice", at=NOW - timedelta(hours=2),
            initial_context_tokens=None, peak_context_tokens=None,
        )
        conn.commit()

        section = web_app._admin_usage_section(conn, NOW)

        assert section["unmeasured_tasks_24h"] == 1
        assert section["context_unmeasured_rows_30d"] == 1

    def test_unmeasured_uses_the_space_separated_bound(self, conn):
        """Reads `tasks`, so it needs the other format. An ISO-Z bound here
        would drop the boundary-day task and report zero."""
        _add_task(conn, user="alice", at=NOW - timedelta(hours=23, minutes=30))
        conn.commit()

        section = web_app._admin_usage_section(conn, NOW)

        assert section["unmeasured_tasks_24h"] == 1


class TestPayloadDegradation:
    def test_a_raising_usage_section_degrades_to_an_error_string(
        self, tmp_path, monkeypatch
    ):
        """Best-effort like every other section — an error string in the
        payload, not a 500 on the whole dashboard."""
        dbp = tmp_path / "istota.db"
        db.init_db(dbp)

        from istota.config import Config

        config = Config()
        config.db_path = dbp
        monkeypatch.setattr(web_app, "_config", config)
        monkeypatch.setattr(
            web_app, "_admin_usage_section",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        payload = web_app._gather_admin_stats()

        assert payload["usage"] == {"error": "boom"}
        # And the rest of the payload survived.
        assert "tasks" in payload
        assert "error" not in payload

    def test_the_usage_key_is_always_present(self, tmp_path, monkeypatch):
        dbp = tmp_path / "istota.db"
        db.init_db(dbp)

        from istota.config import Config

        config = Config()
        config.db_path = dbp
        monkeypatch.setattr(web_app, "_config", config)

        payload = web_app._gather_admin_stats()

        assert "usage" in payload
        assert "totals_24h" in payload["usage"]
