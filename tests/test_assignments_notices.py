"""Assignment notices in the bell: each kind written, deduplicated, and closed
when its condition clears (spec Design section 12).

Driven through the real producers (`finish_wake`, `dispatch_due`, the
lifecycle transitions); only `send_notification` is replaced, so each test can
see what an alert push carried.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from istota import db
from istota.assignments import lifecycle, store, wakes
from istota.config import UserConfig
from istota.notifications import sources
from istota.notifications import store as bell
from istota.notifications.resolvers import assignment as notices
from istota.usage.telemetry import BrainUsage
from tests.test_assignments_wakes import (
    BUDGET,
    GOOD_CSV,
    _assignment,
    _attempt,
    _complete,
    _dispatch,
    _end,
    _ended_wake,
    _file,
    _get,
    _iso,
    _later,
    _live,
    _now,
    _start,
)

STEP = "photograph the furnace label in the basement"


@pytest.fixture
def config(make_config, tmp_path):
    config = make_config(
        db_path=tmp_path / "istota.db",
        users={"alice": UserConfig(timezone="UTC")},
    )
    db.init_db(config.db_path)
    return config


@pytest.fixture
def notify():
    with patch("istota.notifications.delivery.send_notification", return_value=True) as sent:
        yield sent


def _rows(config, kind: str | None = None) -> list[dict]:
    with db.get_db(config.db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM notifications WHERE source = 'assignment' ORDER BY id",
        ).fetchall()]
    if kind is not None:
        rows = [r for r in rows if r["dedup_key"].split(":")[0] == kind]
    return rows


def _seen(config, row: dict) -> None:
    with db.get_db(config.db_path) as conn:
        bell.mark_seen(conn, "alice", [(row["id"], row["updated_at"])])


def _handoff(config, made, step=STEP) -> int:
    with db.get_db(config.db_path) as conn:
        action = lifecycle.add_handoff(
            conn, made.assignment_id, user_id="alice", wake_id=made.wake_id,
            task_id=made.task_id, step=step, report="the photo",
        )
    return action.id


def _pushed_texts(notify) -> list[str]:
    return [call.args[2] for call in notify.call_args_list]


class TestRegistration:
    def test_the_source_is_registered_with_a_push_label(self):
        from istota.push.preferences import notification_categories

        assert "assignment" in sources.all_resolvers()
        labels = {c["id"]: c["label"] for c in notification_categories()}
        assert labels["assignment"] == "Assignments"

    def test_seen_closes_report_and_closed_and_nothing_else(self):
        assert "assignment" in sources.auto_resolve_sources()
        kept = sources.kept_until_dismissed()["assignment"]
        for kind in notices.OBJECT_BACKED_KINDS:
            assert sources.is_kept_until_dismissed("assignment", notices.dedup_key(7, kind, 3), {"assignment": kept})
        for kind in notices.SEEN_KINDS:
            assert not sources.is_kept_until_dismissed("assignment", notices.dedup_key(7, kind, 3), {"assignment": kept})


class TestHandoff:
    def test_written_pushed_without_the_step_and_closed_by_the_report(self, config, notify):
        a = _live(config)
        made = _start(config, a)
        action_id = _handoff(config, made)
        _end(config, made)

        [row] = _rows(config, "handoff")
        assert row["dedup_key"] == f"handoff:{a}:{action_id}"
        assert row["title"] == "Assignment 'Heat pump quotes': a step for you"
        assert STEP in row["body"] and row["state"] == "open"
        [pushed] = _pushed_texts(notify)
        assert STEP not in pushed and notices.PUSH_POINTER in pushed

        _seen(config, row)
        assert _rows(config, "handoff")[0]["state"] == "open"

        with db.get_db(config.db_path) as conn:
            lifecycle.complete_handoff(conn, a, user_id="alice", action_id=action_id, report="sent")
        assert _rows(config, "handoff")[0]["state"] == "resolved"

    def test_a_rewrite_bumps_the_one_row_and_does_not_push_again(self, config, notify):
        a = _live(config)
        made = _start(config, a)
        action_id = _handoff(config, made)
        _end(config, made)
        with db.get_db(config.db_path) as conn:
            result = notices.write(conn, store.get_assignment(conn, a), "handoff", action_id)
        assert result is not None and result.deliver is False
        [row] = _rows(config, "handoff")
        assert row["occurrences"] == 2
        assert notify.call_count == 1

    def test_model_text_in_the_body_is_flattened_and_capped(self, config, notify):
        a = _live(config)
        made = _start(config, a)
        _handoff(config, made, step="[click](http://example.invalid) " + "x" * 900)
        _end(config, made)
        [row] = _rows(config, "handoff")
        step_line = row["body"].splitlines()[0]
        assert "](" not in step_line
        assert len(step_line) <= len("Step: ") + notices.MODEL_TEXT_CHARS


class TestPaused:
    def _stall(self, config):
        a = _live(config)
        for n in range(3):
            made = _start(config, a, now=_later())
            _end(config, made, result=f"Looked again, round {n}.")
        assert _get(config, a).state_reason == "stalled"
        return a

    def test_a_stall_lists_the_last_summaries_and_resume_closes_it(self, config, notify):
        a = self._stall(config)
        [row] = _rows(config, "paused")
        assert row["dedup_key"] == f"paused:{a}" and row["severity"] == "warning"
        for n in range(3):
            assert f"round {n}" in row["body"]
        assert all("round" not in text for text in _pushed_texts(notify))

        with db.get_db(config.db_path) as conn:
            lifecycle.resume(conn, config.assignments, a, user_id="alice")
        assert _rows(config, "paused")[0]["state"] == "resolved"

    def test_a_second_pause_after_a_resume_reopens_and_pushes(self, config, notify):
        a = self._stall(config)
        with db.get_db(config.db_path) as conn:
            lifecycle.resume(conn, config.assignments, a, user_id="alice")
        # The run of wakes without progress still stands, so one more stalls it.
        _end(config, _start(config, a, now=_later()))
        assert _get(config, a).state_reason == "stalled"
        [row] = _rows(config, "paused")
        assert row["state"] == "open"
        assert notify.call_count == 2

    def test_the_budget_pause_before_a_wake_is_written_and_cleared_by_a_topup(self, config, notify):
        a = _assignment(config, next_wake_at=_iso(_now()), budget_fields={**BUDGET, "max_wakes": 1})
        _ended_wake(config, a, ended_at=_now() - timedelta(hours=2))
        assert _dispatch(config) == []

        [row] = _rows(config, "paused")
        assert "budget is spent" in row["body"] and row["state"] == "open"
        assert notify.call_count == 1

        with db.get_db(config.db_path) as conn:
            lifecycle.topup(conn, config.assignments, a, user_id="alice", wakes=5)
        assert _rows(config, "paused")[0]["state"] == "resolved"

    def test_a_finish_that_throws_raises_a_failures_pause(self, config, notify):
        a = _live(config)
        made = _start(config, a)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, made.task_id)
        with patch.object(wakes, "_progress_signals", side_effect=RuntimeError("broken")):
            assert wakes.finish_wake(config, task, "completed") is False
        [row] = _rows(config, "paused")
        assert "could not be finished" in row["body"]
        assert notify.call_count == 1

    def test_a_user_pause_is_not_a_notice(self, config, notify):
        a = _live(config)
        with db.get_db(config.db_path) as conn:
            lifecycle.pause(conn, a, user_id="alice")
        assert _rows(config) == []


class TestBudgetLow:
    def test_raised_once_and_cleared_by_a_topup(self, config, notify):
        a = _live(config, budget_fields={**BUDGET, "max_cost_usd": 1.0})
        made = _start(config, a, now=_later())
        with db.get_db(config.db_path) as conn:
            db.insert_task_usage(
                conn, usage=BrainUsage(has_totals=True, cost_usd=0.8), task_id=made.task_id,
                user_id="alice", source_type="assignment", assignment_id=a,
            )
        _end(config, made)
        [row] = _rows(config, "budget_low")
        assert "$0.80 of $1.00" in row["body"]

        _end(config, _start(config, a, now=_later()))
        assert len(_rows(config, "budget_low")) == 1 and notify.call_count == 1

        with db.get_db(config.db_path) as conn:
            lifecycle.topup(conn, config.assignments, a, user_id="alice", usd=5)
        assert _rows(config, "budget_low")[0]["state"] == "resolved"


class TestCompletionAndClosed:
    def _proposed(self, config):
        a = _live(config, items=(("Picked an installer", "confirmed", None),))
        made = _start(config, a)
        _complete(config, made)
        _end(config, made)
        return a

    def test_proposed_then_confirmed_closes_it_and_leaves_a_closed_row(self, config, notify):
        a = self._proposed(config)
        [row] = _rows(config, "completion")
        assert "quotes.csv has the rows" in row["body"] and row["actionable"] == 1

        with db.get_db(config.db_path) as conn:
            lifecycle.close(conn, a, user_id="alice", completed=True)
        assert _rows(config, "completion")[0]["state"] == "resolved"
        [closed] = _rows(config, "closed")
        assert closed["state"] == "open" and closed["severity"] == "success"
        # The user closed it in chat; only the proposal was pushed.
        assert notify.call_count == 1

        _seen(config, closed)
        assert _rows(config, "closed")[0]["state"] == "resolved"

    def test_a_rejection_clears_the_proposal(self, config, notify):
        a = self._proposed(config)
        with db.get_db(config.db_path) as conn:
            lifecycle.close(conn, a, user_id="alice", completed=False, missing="a fourth quote")
        assert _rows(config, "completion")[0]["state"] == "resolved"
        assert _rows(config, "closed") == []

    def test_an_auto_completion_writes_and_pushes_closed(self, config, notify):
        a = _live(config, items=(("Three quotes", "mechanical", {
            "check": "csv_columns", "file": "quotes.csv", "columns": ["source", "price"], "min_rows": 2,
        }),), auto_complete=True)
        made = _start(config, a)
        _file(config, a, "quotes.csv").write_text(GOOD_CSV)
        _complete(config, made)
        _end(config, made)
        [row] = _rows(config, "closed")
        assert row["title"] == "Assignment 'Heat pump quotes': completed"
        assert notify.call_count == 1


class TestReport:
    def test_written_per_report_and_closed_by_being_seen(self, config, notify):
        a = _live(config)
        made = _start(config, a)
        with db.get_db(config.db_path) as conn:
            first = lifecycle.add_report(conn, a, user_id="alice", wake_id=made.wake_id,
                                         task_id=made.task_id, text="Two installers replied.")
            lifecycle.add_report(conn, a, user_id="alice", wake_id=made.wake_id,
                                 task_id=made.task_id, text="A third is booked.")
        _end(config, made)

        rows = _rows(config, "report")
        assert [r["dedup_key"] for r in rows][0] == f"report:{a}:{first}"
        assert [r["body"] for r in rows] == ["Two installers replied.", "A third is booked."]
        assert all("installers" not in text for text in _pushed_texts(notify))

        _seen(config, rows[0])
        assert [r["state"] for r in _rows(config, "report")] == ["resolved", "open"]


class TestTheResolverIsTheBackstop:
    def test_a_cleared_condition_nobody_settled_goes_stale_on_read(self, config, notify):
        a = _live(config)
        made = _start(config, a)
        action_id = _handoff(config, made)
        _end(config, made)
        with db.get_db(config.db_path) as conn:
            store.resolve_action(conn, a, action_id, response="done elsewhere")
            views, total = bell.list_open(config, conn, "alice")
        assert total == 0 and views == []
        assert _rows(config, "handoff")[0]["state"] == "stale"

    def test_the_view_renders_the_stored_text_and_a_status_note(self, config, notify):
        a = _live(config)
        made = _start(config, a)
        _handoff(config, made)
        _end(config, made)
        with db.get_db(config.db_path) as conn:
            [view] = bell.list_open(config, conn, "alice")[0]
        assert STEP in view.body and view.actions == ()
        assert "report the step done" in view.status_note


class TestAbandon:
    def test_abandoning_clears_every_open_condition(self, config, notify):
        a = _live(config, budget_fields={**BUDGET, "max_cost_usd": 1.0})
        made = _start(config, a)
        _handoff(config, made)
        with db.get_db(config.db_path) as conn:
            db.insert_task_usage(
                conn, usage=BrainUsage(has_totals=True, cost_usd=0.8), task_id=made.task_id,
                user_id="alice", source_type="assignment", assignment_id=a,
            )
        _attempt(config, made, "acme.example/quote", "ok")
        _end(config, made)
        assert {r["dedup_key"].split(":")[0] for r in _rows(config) if r["state"] == "open"} == {
            "handoff", "budget_low",
        }

        with db.get_db(config.db_path) as conn:
            lifecycle.abandon(conn, a, user_id="alice")
        assert {r["state"] for r in _rows(config)} == {"resolved"}
