"""Creating an assignment's wakes, and the lane that runs them.

`wakes.dispatch_due` is the body of the scheduler's `assignments` gate: it
turns a sleeping or waiting assignment that is due into one wake task in the
assignment queue, one transaction per assignment, after the budget check.
`wakes.reap_orphans` finishes a wake whose task ended without anyone finishing
it. The lane is the third phase of `WorkerPool.dispatch`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from istota import db
from istota import scheduler as sched
from istota.assignments import budget, charter, lifecycle, scratchpad, store, validators, wakes
from istota.config import AssignmentsConfig, BrainConfig, SchedulerConfig, UserConfig
from istota.usage.telemetry import BrainUsage

BUDGET = {"max_cost_usd": 20.0, "max_wakes": 40, "per_wake_minutes": 20, "per_wake_turns": 60}


@pytest.fixture
def config(make_config, tmp_path):
    config = make_config(
        db_path=tmp_path / "istota.db",
        users={"alice": UserConfig(timezone="UTC"), "bob": UserConfig(timezone="UTC")},
        scheduler=SchedulerConfig(
            worker_idle_timeout=1, worker_idle_poll_interval=0.02, poll_interval=1,
        ),
    )
    db.init_db(config.db_path)
    return config


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return charter.iso_z(moment)


def _assignment(
    config,
    *,
    user_id: str = "alice",
    state: str = "sleeping",
    next_wake_at: str | None = None,
    waiting_on: list[str] | None = None,
    budget_fields: dict | None = None,
    deadline: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    brain: str | None = None,
    slug: str | None = None,
) -> int:
    with db.get_db(config.db_path) as conn:
        assignment_id = store.insert_assignment(
            conn, user_id=user_id, slug=slug or f"a-{_now().timestamp()}",
            title="Heat pump quotes", goal="Get three quotes",
            budget=budget_fields or BUDGET, dir_relpath="assignments/x",
            deadline=deadline, cadence_minutes=360, model=model, effort=effort, brain=brain,
        )
        store.set_state(conn, assignment_id, state, waiting_on=waiting_on)
        store.update_assignment(conn, assignment_id, next_wake_at=next_wake_at)
    return assignment_id


def _get(config, assignment_id) -> store.Assignment:
    with db.get_db(config.db_path) as conn:
        return store.get_assignment(conn, assignment_id)


def _wakes(config, assignment_id) -> list[store.Wake]:
    with db.get_db(config.db_path) as conn:
        return store.list_wakes(conn, assignment_id)


def _dispatch(config, now: datetime | None = None):
    with db.get_db(config.db_path) as conn:
        return wakes.dispatch_due(conn, config, now=now)


def _inbox(config, assignment_id, kind="user_note", *, trusted=True):
    with db.get_db(config.db_path) as conn:
        store.insert_inbox_event(conn, assignment_id, kind=kind, trusted=trusted, payload={"text": "hi"})


def _ended_wake(config, assignment_id, *, ended_at: datetime, outcome="no_next_step") -> int:
    with db.get_db(config.db_path) as conn:
        wake_id = store.insert_wake(conn, assignment_id, fired_by="time")
        store.update_wake(conn, wake_id, ended_at=_iso(ended_at), outcome=outcome)
    return wake_id


# ---------------------------------------------------------------------------
# The four firing rules
# ---------------------------------------------------------------------------


class TestTheTimeRule:
    def test_a_sleeping_assignment_that_is_due_gets_one_wake_task(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now() - timedelta(minutes=1)))

        created = _dispatch(config)

        assert [c.assignment_id for c in created] == [assignment_id]
        (wake,) = _wakes(config, assignment_id)
        assert (wake.seq, wake.kind, wake.fired_by) == (1, "work", "time")
        assert wake.ended_at is None
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, wake.task_id)
            max_attempts = conn.execute(
                "SELECT max_attempts FROM tasks WHERE id = ?", (task.id,),
            ).fetchone()[0]
        assert task.source_type == "assignment"
        assert task.queue == "assignment"
        assert task.assignment_id == assignment_id
        assert task.user_id == "alice"
        assert task.conversation_token is None
        assert task.output_target is None
        assert task.prompt == wakes.WAKE_PROMPT
        assert task.skip_log_channel is True
        assert task.status == "pending"
        assert max_attempts == 1
        after = _get(config, assignment_id)
        assert after.state == "active"
        assert after.next_wake_at is None

    def test_a_wake_not_yet_due_is_not_created_ahead(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now() + timedelta(hours=1)))

        assert _dispatch(config) == []
        assert _wakes(config, assignment_id) == []
        assert _get(config, assignment_id).state == "sleeping"

    def test_the_wake_records_a_creation_event(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now()))
        _dispatch(config)
        with db.get_db(config.db_path) as conn:
            kinds = [e.kind for e in store.list_events(conn, assignment_id)]
        assert "wake_created" in kinds

    @pytest.mark.parametrize("state", ["draft", "active", "paused", "completed", "abandoned"])
    def test_only_a_sleeping_or_waiting_assignment_is_woken(self, config, state):
        assignment_id = _assignment(config, state=state, next_wake_at=_iso(_now() - timedelta(hours=1)))
        _inbox(config, assignment_id)

        assert _dispatch(config) == []
        assert _get(config, assignment_id).state == state


class TestTheInboxPullsAWakeForward:
    def test_an_inbox_event_wakes_a_sleeping_assignment_early(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now() + timedelta(hours=5)))
        _inbox(config, assignment_id)

        created = _dispatch(config)

        assert [c.fired_by for c in created] == ["inbox"]
        assert _get(config, assignment_id).state == "active"

    def test_the_floor_holds_it_back_and_moves_the_next_wake_to_the_floor(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now() + timedelta(hours=5)))
        ended = _now() - timedelta(minutes=5)
        _ended_wake(config, assignment_id, ended_at=ended)
        _inbox(config, assignment_id)

        assert _dispatch(config) == []

        after = _get(config, assignment_id)
        assert after.state == "sleeping"
        floor = ended + timedelta(minutes=config.assignments.min_wake_interval_minutes)
        assert after.next_wake_at == _iso(floor)

    def test_once_the_floor_has_passed_the_inbox_wakes_it(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now() + timedelta(hours=5)))
        _ended_wake(config, assignment_id, ended_at=_now() - timedelta(minutes=30))
        _inbox(config, assignment_id)

        assert [c.fired_by for c in _dispatch(config)] == ["inbox"]

    def test_a_consumed_event_does_not_pull_a_wake(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now() + timedelta(hours=5)))
        wake_id = _ended_wake(config, assignment_id, ended_at=_now() - timedelta(hours=1))
        _inbox(config, assignment_id)
        with db.get_db(config.db_path) as conn:
            store.consume_inbox(conn, assignment_id, wake_id)

        assert _dispatch(config) == []

    def test_a_budget_low_notice_does_not_pull_a_wake(self, config):
        """The host writes it at the end of a wake; it is not something new to act on."""
        assignment_id = _assignment(config, next_wake_at=_iso(_now() + timedelta(hours=5)))
        _inbox(config, assignment_id, kind="budget_low")

        assert _dispatch(config) == []

    def test_an_outside_scratchpad_change_does_not_pull_a_wake(self, config):
        """Any task of the user's can write the file; a change there is not the user acting."""
        assignment_id = _assignment(config, next_wake_at=_iso(_now() + timedelta(hours=5)))
        _inbox(config, assignment_id, kind="scratchpad_changed", trusted=False)

        assert _dispatch(config) == []
        assert _get(config, assignment_id).state == "sleeping"


class TestAWaitEndsOnlyOnTheKindsItNames:
    def test_the_users_wake_request_ends_a_wait_on_the_user(self, config):
        """`assignment wake` on a waiting assignment writes a `user_note`; that must wake it."""
        assignment_id = _assignment(config, state="waiting", waiting_on=["user"])
        with db.get_db(config.db_path) as conn:
            lifecycle.request_wake(conn, assignment_id, user_id="alice")

        created = _dispatch(config)

        assert [c.fired_by for c in created] == ["inbox"]
        assert _get(config, assignment_id).state == "active"

    @pytest.mark.parametrize("kind", ["user_note", "handoff_done", "charter_change"])
    def test_each_user_kind_ends_a_wait_on_the_user(self, config, kind):
        assignment_id = _assignment(config, state="waiting", waiting_on=["user"])
        _inbox(config, assignment_id, kind=kind)

        assert [c.fired_by for c in _dispatch(config)] == ["inbox"]

    def test_a_note_does_not_end_a_wait_on_a_proposed_completion(self, config):
        assignment_id = _assignment(config, state="waiting", waiting_on=["completion"])
        with db.get_db(config.db_path) as conn:
            lifecycle.add_note(conn, assignment_id, user_id="alice", text="looks good")

        assert _dispatch(config) == []
        assert _get(config, assignment_id).state == "waiting"

    def test_a_wait_with_nothing_in_the_inbox_stays_waiting(self, config):
        assignment_id = _assignment(
            config, state="waiting", waiting_on=["user"],
            next_wake_at=_iso(_now() - timedelta(hours=1)),
        )

        assert _dispatch(config) == []
        assert _get(config, assignment_id).state == "waiting"

    def test_a_kind_the_wait_does_not_name_leaves_it_waiting(self, config):
        assignment_id = _assignment(config, state="waiting", waiting_on=["user"])
        _inbox(config, assignment_id, kind="budget_low")

        assert _dispatch(config) == []

    def test_an_outside_scratchpad_change_does_not_end_a_wait_on_the_user(self, config):
        assignment_id = _assignment(config, state="waiting", waiting_on=["user"])
        _inbox(config, assignment_id, kind="scratchpad_changed", trusted=False)

        assert _dispatch(config) == []
        assert _get(config, assignment_id).state == "waiting"


class TestTheDeadlineWake:
    def test_a_passed_deadline_wakes_a_sleeping_assignment_once(self, config):
        deadline = _iso(_now() - timedelta(minutes=1))
        assignment_id = _assignment(
            config, next_wake_at=_iso(_now() + timedelta(hours=5)), deadline=deadline,
        )

        assert [c.fired_by for c in _dispatch(config)] == ["deadline"]

        # The deadline wake ends in a deadline pause; sleeping again (as after a
        # resume), the same deadline does not fire a second wake.
        (wake,) = _wakes(config, assignment_id)
        wakes.finish_wake(config, None, "completed", wake_id=wake.id)
        assert _get(config, assignment_id).state_reason == "deadline"
        with db.get_db(config.db_path) as conn:
            store.set_state(conn, assignment_id, "sleeping")
            store.update_assignment(conn, assignment_id, next_wake_at=_iso(_now() + timedelta(hours=5)))
        assert _dispatch(config) == []

    def test_a_pending_completion_proposal_blocks_the_deadline_wake(self, config):
        """ISSUE-835: the proposal is the user's to answer; a wake would drop it."""
        assignment_id = _assignment(
            config, state="waiting", waiting_on=["completion"],
            deadline=_iso(_now() - timedelta(minutes=1)),
        )

        assert _dispatch(config) == []
        after = _get(config, assignment_id)
        assert (after.state, after.waiting_on) == ("waiting", ["completion"])

    def test_the_deadline_still_wakes_a_wait_on_the_user(self, config):
        assignment_id = _assignment(
            config, state="waiting", waiting_on=["user"],
            deadline=_iso(_now() - timedelta(minutes=1)),
        )

        assert [(c.assignment_id, c.fired_by) for c in _dispatch(config)] == [
            (assignment_id, "deadline"),
        ]

    def test_an_extended_deadline_fires_again_when_it_passes(self, config):
        assignment_id = _assignment(
            config, next_wake_at=_iso(_now() + timedelta(hours=5)),
            deadline=_iso(_now() - timedelta(days=2)),
        )
        _dispatch(config)
        (wake,) = _wakes(config, assignment_id)
        wakes.finish_wake(config, None, "completed", wake_id=wake.id)
        with db.get_db(config.db_path) as conn:
            store.set_state(conn, assignment_id, "sleeping")
            store.update_assignment(
                conn, assignment_id, deadline=_iso(_now() - timedelta(minutes=1)),
                next_wake_at=_iso(_now() + timedelta(hours=5)),
            )

        assert [c.fired_by for c in _dispatch(config)] == ["deadline"]

    def test_a_deadline_still_ahead_does_not_fire(self, config):
        _assignment(
            config, next_wake_at=_iso(_now() + timedelta(hours=5)),
            deadline=_iso(_now() + timedelta(days=1)),
        )

        assert _dispatch(config) == []


# ---------------------------------------------------------------------------
# Budget, overlap, isolation, the flag
# ---------------------------------------------------------------------------


class TestTheBudgetCheckComesFirst:
    def test_spent_wakes_pause_it_for_budget_with_no_wake(self, config):
        assignment_id = _assignment(
            config, next_wake_at=_iso(_now()), budget_fields={**BUDGET, "max_wakes": 1},
        )
        _ended_wake(config, assignment_id, ended_at=_now() - timedelta(hours=2))

        assert _dispatch(config) == []

        after = _get(config, assignment_id)
        assert (after.state, after.state_reason) == ("paused", "budget")
        assert len(_wakes(config, assignment_id)) == 1
        with db.get_db(config.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE source_type = 'assignment'",
            ).fetchone()[0] == 0

    def test_spent_cost_pauses_it_for_budget(self, config):
        assignment_id = _assignment(
            config, next_wake_at=_iso(_now()), budget_fields={**BUDGET, "max_cost_usd": 1.0},
        )
        with db.get_db(config.db_path) as conn:
            db.insert_task_usage(
                conn, usage=BrainUsage(has_totals=True, cost_usd=1.5), task_id=None,
                user_id="alice", assignment_id=assignment_id,
            )

        assert _dispatch(config) == []
        assert _get(config, assignment_id).state_reason == "budget"

    def test_check_before_wake_reads_the_ledger(self, config):
        assignment_id = _assignment(config, budget_fields={**BUDGET, "max_wakes": 2})
        with db.get_db(config.db_path) as conn:
            assert budget.check_before_wake(conn, store.get_assignment(conn, assignment_id))
            store.insert_wake(conn, assignment_id, fired_by="time")
            store.insert_wake(conn, assignment_id, fired_by="time")
            assert not budget.check_before_wake(conn, store.get_assignment(conn, assignment_id))


class TestOverlappingTicks:
    def test_a_second_tick_finds_it_active_and_creates_nothing(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now()))

        assert len(_dispatch(config)) == 1
        assert _dispatch(config) == []
        assert len(_wakes(config, assignment_id)) == 1

    def test_two_ticks_racing_on_two_connections_create_one_wake(self, config):
        assignment_ids = [
            _assignment(config, next_wake_at=_iso(_now()), slug=f"race-{i}") for i in range(5)
        ]
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def tick():
            try:
                barrier.wait()
                _dispatch(config)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=tick) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []
        for assignment_id in assignment_ids:
            assert len(_wakes(config, assignment_id)) == 1
        with db.get_db(config.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE source_type = 'assignment'",
            ).fetchone()[0] == len(assignment_ids)


class TestOneFailingAssignmentDoesNotStopTheOthers:
    def test_a_bad_row_is_logged_and_skipped(self, config, caplog):
        # An owner that cannot name a directory: `create_task` refuses it.
        broken = _assignment(config, user_id="", next_wake_at=_iso(_now()), slug="broken")
        fine = _assignment(config, next_wake_at=_iso(_now()), slug="fine")

        created = _dispatch(config)

        assert [c.assignment_id for c in created] == [fine]
        assert _get(config, broken).state == "sleeping"
        assert _wakes(config, broken) == []
        assert any("assignment" in r.getMessage() and str(broken) in r.getMessage()
                   for r in caplog.records)


class TestNoFlag:
    def test_a_due_assignment_wakes_with_no_experimental_features(self, config):
        assert config.experimental.features == []
        assignment_id = _assignment(config, next_wake_at=_iso(_now()))

        assert [c.assignment_id for c in _dispatch(config)] == [assignment_id]


class TestModelEffortAndBrain:
    def test_model_and_effort_come_from_the_assignment(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now()), model="sonnet", effort="high")
        _dispatch(config)
        (wake,) = _wakes(config, assignment_id)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, wake.task_id)
        from istota.brain.claude_code import SONNET
        assert (task.model, task.effort) == (SONNET, "high")

    def test_a_brain_the_operator_did_not_list_is_not_pinned(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now()), brain="native")
        _dispatch(config)
        (wake,) = _wakes(config, assignment_id)
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, wake.task_id).brain is None

    def test_a_listed_brain_is_pinned(self, config):
        config.brain = BrainConfig(room_selectable=["claude_code"])
        assignment_id = _assignment(config, next_wake_at=_iso(_now()), brain="claude_code")
        _dispatch(config)
        (wake,) = _wakes(config, assignment_id)
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, wake.task_id).brain == "claude_code"


# ---------------------------------------------------------------------------
# Orphaned wakes
# ---------------------------------------------------------------------------


def _wake_with_task(config, assignment_id, *, status: str, ended_minutes_ago: int | None = 10):
    """A wake created by the gate, its task then moved to `status`."""
    created = _dispatch(config)
    (made,) = [c for c in created if c.assignment_id == assignment_id]
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, made.task_id))
        if ended_minutes_ago is not None:
            conn.execute(
                "UPDATE tasks SET completed_at = datetime('now', ?) WHERE id = ?",
                (f"-{ended_minutes_ago} minutes", made.task_id),
            )
    return made


def _reap(config):
    with db.get_db(config.db_path) as conn:
        return wakes.reap_orphans(conn, config)


class TestReapingOrphans:
    def test_a_failed_task_ends_its_wake_and_the_assignment_sleeps(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now()))
        made = _wake_with_task(config, assignment_id, status="failed")

        assert _reap(config) == [made.wake_id]

        with db.get_db(config.db_path) as conn:
            wake = store.get_wake(conn, made.wake_id)
        assert wake.ended_at is not None
        assert wake.outcome == "failed"
        after = _get(config, assignment_id)
        assert after.state == "sleeping"
        assert after.next_wake_at > _iso(_now() + timedelta(minutes=300))

    def test_a_task_gone_from_the_table_is_reaped_as_failed(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now()))
        made = _wake_with_task(config, assignment_id, status="failed")
        with db.get_db(config.db_path) as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (made.task_id,))

        assert _reap(config) == [made.wake_id]
        with db.get_db(config.db_path) as conn:
            assert store.get_wake(conn, made.wake_id).outcome == "failed"

    def test_a_cancelled_task_is_recorded_as_cancelled(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now()))
        made = _wake_with_task(config, assignment_id, status="cancelled")

        _reap(config)
        with db.get_db(config.db_path) as conn:
            assert store.get_wake(conn, made.wake_id).outcome == "cancelled"

    @pytest.mark.parametrize("status", ["pending", "locked", "running", "pending_confirmation"])
    def test_a_live_task_is_left_alone(self, config, status):
        assignment_id = _assignment(config, next_wake_at=_iso(_now()))
        _wake_with_task(config, assignment_id, status=status, ended_minutes_ago=None)

        assert _reap(config) == []
        assert _get(config, assignment_id).state == "active"

    def test_a_task_that_just_ended_is_left_for_its_own_worker(self, config):
        """The worker that ran it finishes it; the reaper waits out a grace period."""
        assignment_id = _assignment(config, next_wake_at=_iso(_now()))
        _wake_with_task(config, assignment_id, status="failed", ended_minutes_ago=0)

        assert _reap(config) == []

    def test_a_pause_that_landed_during_the_wake_is_kept(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now()))
        made = _wake_with_task(config, assignment_id, status="completed")
        with db.get_db(config.db_path) as conn:
            lifecycle.pause(conn, assignment_id, user_id="alice")

        _reap(config)
        after = _get(config, assignment_id)
        assert (after.state, after.state_reason) == ("paused", "user")
        with db.get_db(config.db_path) as conn:
            assert store.get_wake(conn, made.wake_id).ended_at is not None

    def test_finishing_twice_changes_nothing_the_second_time(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now()))
        made = _wake_with_task(config, assignment_id, status="failed")
        _reap(config)
        with db.get_db(config.db_path) as conn:
            first = store.get_wake(conn, made.wake_id)
            first_next = store.get_assignment(conn, assignment_id).next_wake_at

        wakes.finish_wake(config, None, "completed", wake_id=made.wake_id)

        with db.get_db(config.db_path) as conn:
            assert store.get_wake(conn, made.wake_id) == first
            assert store.get_assignment(conn, assignment_id).next_wake_at == first_next


# ---------------------------------------------------------------------------
# The lane
# ---------------------------------------------------------------------------


def _pending_wake_task(conn, user_id: str) -> int:
    return db.create_task(
        conn, prompt=wakes.WAKE_PROMPT, user_id=user_id, source_type="assignment",
        queue="assignment", max_attempts=1,
    )


class TestTheAssignmentLane:
    def test_a_pending_wake_gets_an_assignment_worker(self, config):
        with db.get_db(config.db_path) as conn:
            _pending_wake_task(conn, "alice")
            assert db.get_users_with_pending_assignment_queue_tasks(conn) == ["alice"]

        pool = sched.WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            queues = {qt for (_, qt, _) in pool._workers}
            pool.shutdown()
        assert queues == {"assignment"}

    def test_the_instance_cap_binds(self, config):
        config.assignments = AssignmentsConfig(max_workers=1, user_max_workers=1)
        with db.get_db(config.db_path) as conn:
            _pending_wake_task(conn, "alice")
            _pending_wake_task(conn, "bob")

        pool = sched.WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 1
            pool.shutdown()

    def test_the_per_user_cap_binds(self, config):
        config.assignments = AssignmentsConfig(max_workers=4, user_max_workers=2)
        with db.get_db(config.db_path) as conn:
            for _ in range(3):
                _pending_wake_task(conn, "alice")

        pool = sched.WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 2
            pool.shutdown()

    def test_it_neither_starves_nor_takes_the_other_lanes(self, config):
        config.scheduler.max_foreground_workers = 1
        config.scheduler.max_background_workers = 1
        config.assignments = AssignmentsConfig(max_workers=1, user_max_workers=1)
        with db.get_db(config.db_path) as conn:
            db.create_task(conn, prompt="fg", user_id="alice", source_type="talk", queue="foreground")
            db.create_task(conn, prompt="bg", user_id="alice", source_type="scheduled", queue="background")
            _pending_wake_task(conn, "alice")

        pool = sched.WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            queues = sorted(qt for (_, qt, _) in pool._workers)
            pool.shutdown()
        assert queues == ["assignment", "background", "foreground"]

    def test_the_backlog_counts_the_assignment_queue(self, config):
        with db.get_db(config.db_path) as conn:
            _pending_wake_task(conn, "alice")
        assert sched._claimable_backlog(config) == 1

    def test_the_status_file_counts_the_assignment_queue(self, config):
        with db.get_db(config.db_path) as conn:
            _pending_wake_task(conn, "alice")
        pool = sched.WorkerPool(config)
        gate = {g.name: g for g in sched.build_interval_gates(config, pool=pool)}["status-write"]
        with patch("istota.status_writer.write_status") as write_status:
            gate.run(0.0)
        assert write_status.call_args.kwargs["pending_assignment"] == 1


# ---------------------------------------------------------------------------
# No result delivery
# ---------------------------------------------------------------------------


class TestAWakeDeliversNothing:
    def test_no_surface_is_derived_for_the_source_type(self):
        from istota.transport.registry import _surface_for_source_type

        assert _surface_for_source_type("assignment") != "talk"

    def test_the_plan_is_empty_even_with_an_output_target(self, config, make_task):
        from istota.transport.registry import make_registry
        from istota.transport.routing import resolve_delivery_plan

        task = make_task(source_type="assignment", output_target="talk,email,ntfy")
        assert resolve_delivery_plan(config, task, make_registry(config)) == []

    def test_process_one_task_posts_nothing_and_stores_no_room_row(self, config):
        assignment_id = _assignment(config, next_wake_at=_iso(_now()))
        (made,) = _dispatch(config)
        posted: list = []

        async def _post(*args, **kwargs):
            posted.append(args)
            return 1

        with patch("istota.scheduler.execute_task",
                   return_value=(True, "Found two installers.", None, None)), \
             patch("istota.scheduler.post_result_to_talk", side_effect=_post), \
             patch("istota.scheduler.post_result_to_email") as email, \
             patch("istota.notifications.delivery.send_notification") as notify:
            result = sched.process_one_task(config, user_id="alice", queue="assignment")

        assert result == (made.task_id, True)
        assert posted == []
        email.assert_not_called()
        notify.assert_not_called()
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, made.task_id).status == "completed"
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
            assert store.get_wake(conn, made.wake_id).summary == "Found two installers."
        # The worker finished the wake itself: no reaper grace was waited out.
        assert _get(config, assignment_id).state == "sleeping"


# ---------------------------------------------------------------------------
# Every source-type tuple, and what `assignment` is to it
# ---------------------------------------------------------------------------


def _decisions():
    """(where, the collection, whether `assignment` is in it, why)."""
    from istota import commands, context, executor
    from istota.credentials import recovery_fill
    from istota.memory import sleep_cycle
    from istota.notifications.resolvers import job_failure
    from istota.session import result as session_result
    from istota.transport import registry, routing
    from istota.webui import app as web_app

    return [
        ("scheduler._AUTOMATED_SOURCE_TYPES", sched._AUTOMATED_SOURCE_TYPES, True,
         "unattended: no ancient-pending notice"),
        ("memory.sleep_cycle.AUTOMATED_SOURCE_TYPES", sleep_cycle.AUTOMATED_SOURCE_TYPES, True,
         "unattended"),
        ("session.result._AUTOMATED_SOURCE_TYPES", session_result._AUTOMATED_SOURCE_TYPES, True,
         "_is_automated_task: no terse-answer recovery"),
        ("webui.app._AUTOMATED_SOURCES", web_app._AUTOMATED_SOURCES, True,
         "the admin split counts it as automated"),
        ("transport.registry.NO_DELIVERY_SOURCE_TYPES", registry.NO_DELIVERY_SOURCE_TYPES, True,
         "a wake's result is the wake row's summary, delivered nowhere"),
        ("executor._INTERACTIVE_SOURCE_TYPES", executor._INTERACTIVE_SOURCE_TYPES, False,
         "no conversation context"),
        ("transport.routing._INTERACTIVE_SOURCE_TYPES", routing._INTERACTIVE_SOURCE_TYPES, False,
         "no reply-to-origin fallback"),
        ("transport.registry._TRANSFORMED_SOURCE_TYPES", registry._TRANSFORMED_SOURCE_TYPES, False,
         "nothing is stored, so nothing is transformed"),
        ("memory.sleep_cycle.INTERACTIVE_SOURCE_TYPES", sleep_cycle.INTERACTIVE_SOURCE_TYPES, False,
         "not the user speaking"),
        ("commands._INTERACTIVE_SOURCE_TYPES", commands._INTERACTIVE_SOURCE_TYPES, False,
         "not the user speaking"),
        ("commands._RETRYABLE_SOURCE_TYPES", commands._RETRYABLE_SOURCE_TYPES, False,
         "the assignment has its own wake verb"),
        ("commands._RUN_NOW_SOURCE_TYPES", commands._RUN_NOW_SOURCE_TYPES, False,
         "the assignment has its own wake verb"),
        ("notifications.resolvers.job_failure._SOURCE_TYPES", job_failure._SOURCE_TYPES, False,
         "finish_wake owns the failure notice"),
        ("webui.app._INTERACTIVE_SOURCES", web_app._INTERACTIVE_SOURCES, False,
         "not a person"),
        ("credentials.recovery_fill.INTERACTIVE_SOURCE_TYPES", recovery_fill.INTERACTIVE_SOURCE_TYPES,
         False, "no recovery fill from an unattended run"),
        ("db.TRANSCRIPT_SURFACES", db.TRANSCRIPT_SURFACES, False, "a wake writes no room row"),
        ("db.INLINE_ONLY_SOURCE_TYPES", db.INLINE_ONLY_SOURCE_TYPES, False,
         "claimed by the assignment lane"),
        ("context._SCHEDULED_SOURCE_TYPES", context._SCHEDULED_SOURCE_TYPES, False,
         "a wake has no conversation, so it is never in a history"),
        ("caller.USER_TURN_SOURCES",
         __import__("istota.assignments.caller", fromlist=["x"]).USER_TURN_SOURCES, False,
         "a wake is never a user turn"),
    ]


@pytest.mark.parametrize("where,collection,member,why", _decisions(), ids=lambda v: v if isinstance(v, str) else "")
def test_each_source_type_collection_decides_assignment(where, collection, member, why):
    assert ("assignment" in collection) is member, f"{where}: {why}"


def test_the_two_source_type_maps_name_no_surface_for_a_wake():
    from istota.rooms.surfaces import origin_surface_for_source_type
    from istota.transport.registry import _surface_for_source_type

    assert origin_surface_for_source_type("assignment") is None
    assert _surface_for_source_type("assignment") == "none"


def test_the_credential_broker_never_scopes_a_grant_to_a_wake():
    """`_in_scope` needs a conversation token, and a wake has none."""
    from istota.credentials.broker.grants import _in_scope

    grant = {"scope_mode": "all", "rooms": [], "allow_scheduled": True}
    assert not _in_scope(grant, {"conversation_token": None}, False)


# ---------------------------------------------------------------------------
# Finishing a wake
# ---------------------------------------------------------------------------
#
# Each case runs a wake the way the scheduler does: the gate creates it,
# `start_wake` picks it up (scratchpad snapshot, inbox claim, deliverable
# digests), the wake writes through `lifecycle`'s wake verbs and the files, the
# task row is moved to its terminal status, and `finish_wake` ends it.

CSV_CHECK = {"check": "csv_columns", "file": "quotes.csv", "columns": ["source", "price"], "min_rows": 2}
GOOD_CSV = "source,price\nacme,1200\nbeta,1350\n"


def _live(
    config,
    *,
    items: tuple = (),
    auto_complete: bool = False,
    budget_fields: dict | None = None,
    deadline: str | None = None,
) -> int:
    """A sleeping, due assignment with its directory and seed scratchpad."""
    slug = f"f{int(_now().timestamp() * 1e6)}"
    with db.get_db(config.db_path) as conn:
        assignment_id = store.insert_assignment(
            conn, user_id="alice", slug=slug, title="Heat pump quotes", goal="Get quotes",
            budget=budget_fields or BUDGET, dir_relpath=charter.dir_relpath(slug),
            auto_complete=auto_complete, deadline=deadline,
        )
        for text, kind, validator in items:
            store.insert_item(conn, assignment_id, text=text, kind=kind, validator=validator)
        store.set_state(conn, assignment_id, "sleeping")
        store.update_assignment(conn, assignment_id, next_wake_at=_iso(_now() - timedelta(minutes=1)))
        assert charter.create_directory(config, conn, store.get_assignment(conn, assignment_id))
    return assignment_id


def _file(config, assignment_id, name):
    assignment = _get(config, assignment_id)
    return (
        config.workspace_root(assignment.user_id) / config.bot_dir_name
        / assignment.dir_relpath / name
    )


def _start(config, assignment_id, *, now: datetime | None = None) -> wakes.CreatedWake:
    """Let the gate create the next wake and pick it up as `execute_task` would."""
    made = [c for c in _dispatch(config, now) if c.assignment_id == assignment_id]
    assert len(made) == 1, made
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, made[0].task_id)
    assert wakes.start_wake(config, task) is not None
    return made[0]


def _end(config, made, *, status="completed", result="Did the work.", error=None) -> store.Wake:
    with db.get_db(config.db_path) as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, result = ?, error = ? WHERE id = ?",
            (status, result, error, made.task_id),
        )
        task = db.get_task(conn, made.task_id)
    assert wakes.finish_wake(config, task, status) is True
    with db.get_db(config.db_path) as conn:
        return store.get_wake(conn, made.wake_id)


def _next(config, made, **kwargs):
    with db.get_db(config.db_path) as conn:
        return lifecycle.request_next(
            conn, config.assignments, made.assignment_id, user_id="alice",
            wake_id=made.wake_id, task_id=made.task_id, **kwargs,
        )


def _attempt(config, made, target, outcome):
    with db.get_db(config.db_path) as conn:
        lifecycle.add_attempt(
            conn, made.assignment_id, user_id="alice", wake_id=made.wake_id, task_id=made.task_id,
            target=target, tried="the web form", outcome=outcome,
        )


def _complete(config, made):
    with db.get_db(config.db_path) as conn:
        lifecycle.request_complete(
            conn, made.assignment_id, user_id="alice", wake_id=made.wake_id, task_id=made.task_id,
            evidence="quotes.csv has the rows",
        )


def _later() -> datetime:
    """A gate tick past any cadence a test set, so the time rule fires."""
    return _now() + timedelta(days=2)


def _finished_event(config, assignment_id, wake_id) -> dict:
    with db.get_db(config.db_path) as conn:
        [event] = [e for e in store.list_events(conn, assignment_id)
                   if e.kind == "wake_finished" and e.wake_id == wake_id]
    return event.payload


def _minutes_ahead(iso: str) -> float:
    return (datetime.fromisoformat(iso.replace("Z", "+00:00")) - _now()).total_seconds() / 60


class TestTheNextStep:
    def test_no_next_is_the_default_sleep_at_the_cadence(self, config):
        a = _live(config)
        wake = _end(config, _start(config, a), result="Looked around.")

        assert (wake.outcome, wake.summary, wake.progress_signals) == ("no_next_step", "Looked around.", [])
        assert wake.ended_at is not None
        after = _get(config, a)
        assert after.state == "sleeping"
        assert 355 < _minutes_ahead(after.next_wake_at) <= 360
        assert _finished_event(config, a, wake.id)["state"] == "sleeping"

    def test_next_in_sets_the_next_wake(self, config):
        a = _live(config)
        made = _start(config, a)
        _next(config, made, mode="in", requested="2h", wake_at=_now() + timedelta(hours=2))

        wake = _end(config, made)
        assert wake.outcome == "next"
        assert 115 < _minutes_ahead(_get(config, a).next_wake_at) <= 120

    def test_a_time_inside_the_floor_from_the_wakes_end_is_moved_to_it(self, config):
        a = _live(config)
        made = _start(config, a)
        with db.get_db(config.db_path) as conn:
            store.update_wake(conn, made.wake_id, next_request={
                "mode": "in", "requested": "15m", "wake_at": _iso(_now() - timedelta(minutes=30)),
                "wait": None, "clamped": "floor", "recorded_at": _iso(_now() - timedelta(minutes=45)),
            })

        _end(config, made)
        assert 14 < _minutes_ahead(_get(config, a).next_wake_at) <= 15

    def test_next_wait_user_waits_on_the_user(self, config):
        a = _live(config)
        made = _start(config, a)
        _next(config, made, mode="wait", requested="user", wait="user")

        _end(config, made)
        after = _get(config, a)
        assert (after.state, after.waiting_on, after.next_wake_at) == ("waiting", ["user"], None)

    def test_a_failed_wake_still_applies_its_next(self, config):
        a = _live(config)
        made = _start(config, a)
        _next(config, made, mode="in", requested="2h", wake_at=_now() + timedelta(hours=2))

        wake = _end(config, made, status="failed", result=None, error="brain timed out")
        assert (wake.outcome, wake.summary) == ("failed", "brain timed out")
        after = _get(config, a)
        assert after.state == "sleeping"
        assert 115 < _minutes_ahead(after.next_wake_at) <= 120

    def test_a_pause_the_user_set_during_the_wake_is_kept_and_next_ignored(self, config):
        a = _live(config)
        made = _start(config, a)
        _next(config, made, mode="in", requested="2h", wake_at=_now() + timedelta(hours=2))
        with db.get_db(config.db_path) as conn:
            lifecycle.pause(conn, a, user_id="alice")

        wake = _end(config, made)
        assert wake.outcome == "next"
        after = _get(config, a)
        assert (after.state, after.state_reason, after.next_wake_at) == ("paused", "user", None)

    def test_abandon_during_the_wake_is_kept(self, config):
        a = _live(config)
        made = _start(config, a)
        with db.get_db(config.db_path) as conn:
            lifecycle.abandon(conn, a, user_id="alice")

        _end(config, made)
        assert _get(config, a).state == "abandoned"


class TestPauseAndResumeDuringAWake:
    def test_no_second_wake_while_the_first_is_open_and_one_after_it_ends(self, config):
        a = _live(config)
        made = _start(config, a)
        with db.get_db(config.db_path) as conn:
            lifecycle.pause(conn, a, user_id="alice")
            lifecycle.resume(conn, config.assignments, a, user_id="alice")

        assert _dispatch(config, _later()) == []
        assert [w.id for w in _wakes(config, a)] == [made.wake_id]
        assert _wakes(config, a)[0].ended_at is None

        _end(config, made)
        after = _get(config, a)
        assert after.state == "sleeping"

        created = _dispatch(config, _later())
        assert [c.assignment_id for c in created] == [a]
        assert len(_wakes(config, a)) == 2
        assert _dispatch(config, _later()) == []

    def test_an_earlier_wake_ending_leaves_a_later_wakes_state_alone(self, config):
        a = _live(config)
        first = _start(config, a)
        with db.get_db(config.db_path) as conn:
            # A later wake opened by some other route while the first is open.
            later = store.insert_wake(conn, a, fired_by="time")

        _end(config, first)
        after = _get(config, a)
        assert after.state == "active"
        with db.get_db(config.db_path) as conn:
            assert store.get_wake(conn, later).ended_at is None


class TestTheScratchpadAtTheEnd:
    def test_lines_from_an_outside_change_are_not_put_back(self, config):
        a = _live(config)
        _end(config, _start(config, a, now=_later()))
        pad = _file(config, a, charter.SCRATCHPAD_FILE)
        pad.write_text(pad.read_text() + "Call installer B after 5pm.\n")

        made = _start(config, a, now=_later())
        pad.write_text(charter.SCRATCHPAD_SEED)  # the wake rewrote its notes
        _end(config, made)

        assert pad.read_text() == charter.SCRATCHPAD_SEED
        with db.get_db(config.db_path) as conn:
            assert store.wake_snapshot(conn, made.wake_id, "after").content == charter.SCRATCHPAD_SEED
            kinds = [e.kind for e in store.list_events(conn, a)]
        assert "scratchpad_changed" in kinds and "user_lines_kept" not in kinds

    def test_a_wake_that_never_started_takes_no_after_snapshot(self, config):
        _live(config)
        (made,) = _dispatch(config)
        wake = _end(config, made, status="failed", result=None, error="assignment wake pickup failed")
        assert wake.outcome == "failed"
        with db.get_db(config.db_path) as conn:
            assert store.wake_snapshot(conn, made.wake_id, "after") is None


def _started_digests(config, wake_id) -> dict:
    with db.get_db(config.db_path) as conn:
        row = conn.execute(
            "SELECT payload FROM assignment_events WHERE wake_id = ? AND kind = 'wake_started'", (wake_id,),
        ).fetchone()
    return json.loads(row[0])["deliverables"]


class TestNoFileIOUnderTheWriterLock:
    """ISSUE-833: a workspace read can stall on the mount, and the whole
    scheduler waits on the framework DB's writer lock while it does."""

    def _probe(self, config, calls: list):
        """Record, for each file access, whether another connection could take
        the writer lock at that moment."""

        def wrap(name, real):
            def probed(*args, **kwargs):
                other = sqlite3.connect(config.db_path, timeout=0)
                try:
                    other.execute("BEGIN IMMEDIATE")
                    other.rollback()
                    calls.append((name, "free"))
                except sqlite3.OperationalError:
                    calls.append((name, "held"))
                finally:
                    other.close()
                return real(*args, **kwargs)
            return probed

        return [
            patch.object(scratchpad, "read_file", wrap("read_file", scratchpad.read_file)),
            patch.object(scratchpad, "write_file", wrap("write_file", scratchpad.write_file)),
            patch.object(validators, "run_item", wrap("run_item", validators.run_item)),
            patch.object(
                validators, "deliverable_digests",
                wrap("deliverable_digests", validators.deliverable_digests),
            ),
        ]

    def test_pickup_and_finish_read_and_write_files_with_the_lock_free(self, config):
        a = _live(config, items=(("Three quotes", "mechanical", CSV_CHECK),))
        pad = _file(config, a, charter.SCRATCHPAD_FILE)
        pad.write_text("")  # restored at pickup
        calls: list = []
        probes = self._probe(config, calls)
        for probe in probes:
            probe.start()
        try:
            made = _start(config, a)
            assert pad.read_text() == charter.SCRATCHPAD_SEED
            _file(config, a, "quotes.csv").write_text(GOOD_CSV)
            pad.write_text("")  # broken by the wake, restored at finish
            wake = _end(config, made)
        finally:
            for probe in probes:
                probe.stop()

        assert {name for name, _ in calls} == {
            "read_file", "write_file", "run_item", "deliverable_digests",
        }
        assert [c for c in calls if c[1] == "held"] == []
        # The outcomes are what they were with the reads under the lock.
        assert wake.progress_signals == ["item_passing", "deliverable_changed"]
        assert pad.read_text() == charter.SCRATCHPAD_SEED
        with db.get_db(config.db_path) as conn:
            restored = [e.payload for e in store.list_events(conn, a) if e.kind == "scratchpad_restored"]
            assert store.wake_snapshot(conn, made.wake_id, "after").content == charter.SCRATCHPAD_SEED
        assert [(p["phase"], p["reason"]) for p in restored] == [("begin", "empty"), ("end", "empty")]

    def test_an_item_added_after_the_read_is_read_again_before_it_is_recorded(self, config):
        a = _live(config, items=(("Three quotes", "mechanical", CSV_CHECK),))
        made = _start(config, a)
        _file(config, a, "quotes.csv").write_text(GOOD_CSV)
        real = wakes._read_finish_files
        reads: list = []

        def read_then_add(*args, **kwargs):
            files = real(*args, **kwargs)
            reads.append(files)
            if len(reads) == 1:
                with db.get_db(config.db_path) as other:
                    store.insert_item(
                        other, a, text="Has a header", kind="mechanical",
                        validator={"check": "contains", "file": "quotes.csv", "text": "source"},
                    )
            return files

        with patch.object(wakes, "_read_finish_files", side_effect=read_then_add):
            _end(config, made)

        assert len(reads) == 2
        with db.get_db(config.db_path) as conn:
            statuses = [(item.text, item.status) for item in store.list_items(conn, a)]
        assert statuses == [("Three quotes", "passing"), ("Has a header", "passing")]

    def _add_item_on(self, config, a, real, reads, *, every=False):
        def read_then_add(*args, **kwargs):
            files = real(*args, **kwargs)
            reads.append(files)
            if every or len(reads) == 1:
                with db.get_db(config.db_path) as other:
                    store.insert_item(
                        other, a, text=f"Header {len(reads)}", kind="mechanical",
                        validator={"check": "contains", "file": "quotes.csv", "text": "source"},
                    )
            return files
        return read_then_add

    def test_pickup_reads_again_when_an_item_is_added_after_the_read(self, config):
        a = _live(config, items=(("Three quotes", "mechanical", CSV_CHECK),))
        _file(config, a, "quotes.csv").write_text(GOOD_CSV)
        (made,) = _dispatch(config)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, made.task_id)
        reads: list = []
        with patch.object(wakes, "_read_pickup_files",
                          side_effect=self._add_item_on(config, a, wakes._read_pickup_files, reads)):
            assert wakes.start_wake(config, task) is not None

        assert len(reads) == 2
        assert set(_started_digests(config, made.wake_id)) == {"quotes.csv"}

    def test_a_pickup_whose_files_keep_moving_is_turned_away(self, config):
        a = _live(config, items=(("Three quotes", "mechanical", CSV_CHECK),))
        (made,) = _dispatch(config)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, made.task_id)
        reads: list = []
        with patch.object(wakes, "_read_pickup_files",
                          side_effect=self._add_item_on(config, a, wakes._read_pickup_files, reads, every=True)):
            assert wakes.start_wake(config, task) is None

        assert len(reads) == wakes._IO_ATTEMPTS
        (wake,) = _wakes(config, a)
        assert (wake.started_at, wake.outcome) == (None, "not_active")
        assert _get(config, a).state == "sleeping"

    def test_a_finish_whose_files_keep_moving_leaves_the_wake_open(self, config):
        a = _live(config, items=(("Three quotes", "mechanical", CSV_CHECK),))
        made = _start(config, a)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (made.task_id,))
            task = db.get_task(conn, made.task_id)
        reads: list = []
        with patch.object(wakes, "_read_finish_files",
                          side_effect=self._add_item_on(config, a, wakes._read_finish_files, reads, every=True)):
            assert wakes.finish_wake(config, task, "completed") is False

        assert len(reads) == wakes._IO_ATTEMPTS
        (wake,) = _wakes(config, a)
        assert wake.ended_at is None
        assert _get(config, a).state == "active"
        with db.get_db(config.db_path) as conn:
            assert "finish_failed" not in [e.kind for e in store.list_events(conn, a)]
        # The reaper's later call reads a settled list and ends it.
        assert wakes.finish_wake(config, task, "completed") is True

    def test_a_finish_read_before_pickup_set_started_at_is_read_again(self, config):
        _live(config)
        (made,) = _dispatch(config)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, made.task_id)
        real = wakes._read_finish_files
        reads: list = []

        def read_then_start(conn, config_, wake, outcome):
            files = real(conn, config_, wake, outcome)
            reads.append(files)
            if len(reads) == 1:
                with db.get_db(config.db_path) as other:
                    store.update_wake(other, wake.id, started_at=_iso(_now()))
            return files

        with patch.object(wakes, "_read_finish_files", side_effect=read_then_start):
            assert wakes.finish_wake(config, task, "failed") is True

        assert [files.started_at is None for files in reads] == [True, False]
        assert [files.scratchpad is None for files in reads] == [True, False]


class TestValidatorsAndSignals:
    def test_a_deliverable_that_passes_is_item_passing_and_deliverable_changed(self, config):
        a = _live(config, items=(("Three quotes", "mechanical", CSV_CHECK),))
        made = _start(config, a)
        _file(config, a, "quotes.csv").write_text(GOOD_CSV)

        wake = _end(config, made)
        assert wake.progress_signals == ["item_passing", "deliverable_changed"]
        with db.get_db(config.db_path) as conn:
            [item] = store.list_items(conn, a)
            [change] = [e for e in store.list_events(conn, a) if e.kind == "item_status"]
        assert (item.status, item.checked_at is not None) == ("passing", True)
        assert change.payload["to"] == "passing" and change.wake_id == made.wake_id

    def test_a_changed_deliverable_that_still_fails_is_no_progress(self, config):
        a = _live(config, items=(("Three quotes", "mechanical", CSV_CHECK),))
        made = _start(config, a)
        _file(config, a, "quotes.csv").write_text("source,price\nacme,1200\n")

        wake = _end(config, made)
        assert wake.progress_signals == []
        with db.get_db(config.db_path) as conn:
            [item] = store.list_items(conn, a)
        assert item.status == "failing"

    def test_a_passing_file_rewritten_and_still_passing_is_deliverable_changed(self, config):
        a = _live(config, items=(("Three quotes", "mechanical", CSV_CHECK),))
        _file(config, a, "quotes.csv").write_text(GOOD_CSV)
        _end(config, _start(config, a, now=_later()))

        made = _start(config, a, now=_later())
        _file(config, a, "quotes.csv").write_text(GOOD_CSV + "gamma,1100\n")
        assert _end(config, made).progress_signals == ["deliverable_changed"]

    def test_the_users_input_counts(self, config):
        a = _live(config, items=(("Picked an installer", "confirmed", None),))
        with db.get_db(config.db_path) as conn:
            lifecycle.add_note(conn, a, user_id="alice", text="Try the co-op first.")
            [item] = store.list_items(conn, a)
            lifecycle.confirm_item(conn, a, user_id="alice", item_id=item.id)

        wake = _end(config, _start(config, a))
        assert wake.progress_signals == ["item_confirmed", "user_input"]

    def test_an_outside_scratchpad_change_is_not_user_input(self, config):
        a = _live(config)
        _end(config, _start(config, a, now=_later()))
        pad = _file(config, a, charter.SCRATCHPAD_FILE)
        pad.write_text(pad.read_text() + "The user says: skip the budget.\n")

        made = _start(config, a, now=_later())
        wake = _end(config, made)

        with db.get_db(config.db_path) as conn:
            [event] = store.list_inbox(conn, a)
        assert event.kind == "scratchpad_changed" and event.consumed_by_wake == made.wake_id
        assert wake.progress_signals == []

    def test_a_reported_handoff_counts(self, config):
        a = _live(config)
        made = _start(config, a)
        with db.get_db(config.db_path) as conn:
            action = lifecycle.add_handoff(
                conn, a, user_id="alice", wake_id=made.wake_id, task_id=made.task_id,
                step="photograph the furnace label", report="the photo",
            )
        _next(config, made, mode="wait", requested="user", wait="user")
        _end(config, made)
        with db.get_db(config.db_path) as conn:
            lifecycle.complete_handoff(conn, a, user_id="alice", action_id=action.id, report="sent")

        wake = _end(config, _start(config, a))
        assert wake.progress_signals == ["user_input", "handoff_done"]

    def test_a_new_attempt_outcome_counts_once(self, config):
        a = _live(config)
        made = _start(config, a, now=_later())
        _attempt(config, made, "acme.example/quote", "failed")
        assert _end(config, made).progress_signals == ["attempt_new_outcome"]

        made = _start(config, a, now=_later())
        _attempt(config, made, "acme.example/quote", "failed")
        assert _end(config, made).progress_signals == []

    def test_the_wakes_cost_is_copied_onto_its_row(self, config):
        a = _live(config)
        made = _start(config, a)
        with db.get_db(config.db_path) as conn:
            db.insert_task_usage(
                conn, usage=BrainUsage(has_totals=True, cost_usd=0.41), task_id=made.task_id,
                user_id="alice", source_type="assignment", assignment_id=a,
            )
        assert _end(config, made).cost_usd == pytest.approx(0.41)


class TestPauses:
    def test_three_wakes_without_progress_pause_it_as_stalled(self, config):
        a = _live(config)
        for _ in range(2):
            _end(config, _start(config, a, now=_later()))
            assert _get(config, a).state == "sleeping"
        made = _start(config, a, now=_later())
        _next(config, made, mode="in", requested="1h", wake_at=_now() + timedelta(hours=1))
        _end(config, made)

        after = _get(config, a)
        assert (after.state, after.state_reason, after.next_wake_at) == ("paused", "stalled", None)
        assert ["paused", "stalled"] in _finished_event(config, a, made.wake_id)["notices"]

    def test_progress_breaks_a_stall(self, config):
        a = _live(config)
        for _ in range(2):
            _end(config, _start(config, a, now=_later()))
        with db.get_db(config.db_path) as conn:
            lifecycle.add_note(conn, a, user_id="alice", text="Here is the address.")
        _end(config, _start(config, a, now=_later()))
        assert _get(config, a).state == "sleeping"

    def test_three_failed_wakes_pause_it_with_the_last_error(self, config):
        a = _live(config)
        for n in range(3):
            made = _start(config, a, now=_later())
            _attempt(config, made, f"target-{n}", "ok")  # progress, so no stall
            _end(config, made, status="failed", result=None, error=f"error {n}")

        after = _get(config, a)
        assert (after.state, after.state_reason) == ("paused", "failures")
        with db.get_db(config.db_path) as conn:
            [pause] = [e for e in store.list_events(conn, a) if e.kind == "paused"]
        assert pause.payload["error"] == "error 2"

    def test_a_cancelled_wake_does_not_count_toward_failures(self, config):
        a = _live(config)
        for n, status in enumerate(("failed", "cancelled", "failed")):
            made = _start(config, a, now=_later())
            _attempt(config, made, f"target-{n}", "ok")
            _end(config, made, status=status, result=None, error="boom")
        assert _get(config, a).state == "sleeping"

        made = _start(config, a, now=_later())
        _attempt(config, made, "target-last", "ok")
        _end(config, made, status="failed", result=None, error="boom")
        assert _get(config, a).state_reason == "failures"

    def test_a_fourth_failure_on_one_target_pauses_it_for_thrash(self, config):
        a = _live(config)
        made = _start(config, a, now=_later())
        for _ in range(3):
            _attempt(config, made, "acme.example/quote", "failed")
        _end(config, made)
        assert _get(config, a).state == "sleeping"

        made = _start(config, a, now=_later())
        _attempt(config, made, "acme.example/quote", "failed")
        _next(config, made, mode="in", requested="1h", wake_at=_now() + timedelta(hours=1))
        _end(config, made)

        after = _get(config, a)
        assert (after.state, after.state_reason) == ("paused", "thrash")
        with db.get_db(config.db_path) as conn:
            [pause] = [e for e in store.list_events(conn, a) if e.kind == "paused"]
        assert pause.payload["target"] == "acme.example/quote"

    def test_an_ok_in_between_is_not_thrash(self, config):
        a = _live(config)
        made = _start(config, a, now=_later())
        for outcome in ("failed", "failed", "ok", "failed"):
            _attempt(config, made, "acme.example/quote", outcome)
        _end(config, made)
        assert _get(config, a).state == "sleeping"

    def test_a_spent_budget_pauses_it_over_its_next(self, config):
        a = _live(config, budget_fields={**BUDGET, "max_wakes": 1})
        made = _start(config, a)
        _next(config, made, mode="in", requested="1h", wake_at=_now() + timedelta(hours=1))
        _end(config, made)

        after = _get(config, a)
        assert (after.state, after.state_reason) == ("paused", "budget")

    def test_crossing_the_low_mark_raises_budget_low_once(self, config):
        a = _live(config, budget_fields={**BUDGET, "max_cost_usd": 1.0})
        made = _start(config, a, now=_later())
        with db.get_db(config.db_path) as conn:
            db.insert_task_usage(
                conn, usage=BrainUsage(has_totals=True, cost_usd=0.8), task_id=made.task_id,
                user_id="alice", source_type="assignment", assignment_id=a,
            )
        _end(config, made)
        assert _get(config, a).budget_low_sent is True
        assert ["budget_low", None] in _finished_event(config, a, made.wake_id)["notices"]

        made = _start(config, a, now=_later())
        _end(config, made)
        with db.get_db(config.db_path) as conn:
            lows = [e for e in store.list_inbox(conn, a) if e.kind == "budget_low"]
        assert len(lows) == 1 and lows[0].trusted
        assert lows[0].consumed_by_wake == made.wake_id

    def test_the_deadline_wake_ends_in_a_deadline_pause(self, config):
        a = _live(config, deadline=_iso(_now() - timedelta(minutes=5)))
        made = _start(config, a)
        assert made.fired_by == "deadline"
        _end(config, made)

        after = _get(config, a)
        assert (after.state, after.state_reason) == ("paused", "deadline")


class TestCompletion:
    def test_a_failing_mechanical_item_refuses_it_and_next_applies(self, config):
        a = _live(config, items=(("Three quotes", "mechanical", CSV_CHECK),))
        made = _start(config, a)
        _complete(config, made)
        _next(config, made, mode="in", requested="2h", wake_at=_now() + timedelta(hours=2))

        wake = _end(config, made)
        assert wake.outcome == "complete_refused"
        after = _get(config, a)
        assert after.state == "sleeping"
        assert 115 < _minutes_ahead(after.next_wake_at) <= 120
        with db.get_db(config.db_path) as conn:
            [refused] = [e for e in store.list_events(conn, a) if e.kind == "complete_refused"]
            [item] = store.list_items(conn, a)
        assert refused.payload["failing_items"] == [item.id]

    def test_auto_complete_with_every_item_met_completes_it(self, config):
        a = _live(config, items=(("Three quotes", "mechanical", CSV_CHECK),), auto_complete=True)
        made = _start(config, a)
        _file(config, a, "quotes.csv").write_text(GOOD_CSV)
        _complete(config, made)

        wake = _end(config, made)
        assert wake.outcome == "completed"
        after = _get(config, a)
        assert (after.state, after.closed_at is not None) == ("completed", True)
        assert ["closed", None] in _finished_event(config, a, made.wake_id)["notices"]

    def test_an_open_confirmed_item_proposes_and_waits_then_close_completes(self, config):
        a = _live(config, items=(
            ("Three quotes", "mechanical", CSV_CHECK), ("Picked an installer", "confirmed", None),
        ), auto_complete=True)
        made = _start(config, a)
        _file(config, a, "quotes.csv").write_text(GOOD_CSV)
        _complete(config, made)

        assert _end(config, made).outcome == "complete_proposed"
        after = _get(config, a)
        assert (after.state, after.waiting_on) == ("waiting", ["completion"])
        assert ["completion", None] in _finished_event(config, a, made.wake_id)["notices"]
        assert _dispatch(config, _later()) == []

        with db.get_db(config.db_path) as conn:
            lifecycle.close(conn, a, user_id="alice", completed=True)
            items = store.list_items(conn, a)
        assert _get(config, a).state == "completed"
        assert [i.status for i in items] == ["passing", "confirmed"]

    def test_rejecting_a_proposed_completion_wakes_it_with_what_is_missing(self, config):
        a = _live(config, items=(("Picked an installer", "confirmed", None),))
        made = _start(config, a)
        _complete(config, made)
        _end(config, made)
        with db.get_db(config.db_path) as conn:
            lifecycle.close(conn, a, user_id="alice", completed=False, missing="a fourth quote")

        made = _start(config, a)
        with db.get_db(config.db_path) as conn:
            [event] = store.list_inbox(conn, a, consumed_by_wake=made.wake_id)
        assert (event.kind, event.payload) == ("completion_rejected", {"missing": "a fourth quote"})

    def test_a_proposed_completion_on_the_deadline_wake_is_not_paused(self, config):
        a = _live(config, deadline=_iso(_now() - timedelta(minutes=5)))
        made = _start(config, a)
        _complete(config, made)
        _end(config, made)
        after = _get(config, a)
        assert (after.state, after.waiting_on) == ("waiting", ["completion"])

    def test_a_deadline_passing_on_a_proposal_keeps_it_until_the_user_rejects(self, config):
        """ISSUE-835, end to end: the deadline waits for the user's answer."""
        deadline = _now() + timedelta(hours=1)
        a = _live(config, deadline=_iso(deadline))
        made = _start(config, a)
        _complete(config, made)
        _end(config, made)
        bell_row = "SELECT state FROM notifications WHERE source = 'assignment' AND dedup_key = ?"
        key = f"completion:{a}"

        past_deadline = deadline + timedelta(minutes=5)
        assert _dispatch(config, past_deadline) == []
        after = _get(config, a)
        assert (after.state, after.waiting_on) == ("waiting", ["completion"])
        with db.get_db(config.db_path) as conn:
            assert conn.execute(bell_row, (key,)).fetchone()["state"] == "open"

        with db.get_db(config.db_path) as conn:
            lifecycle.close(conn, a, user_id="alice", completed=False, missing="a fourth quote")
        (deadline_wake,) = _dispatch(config, past_deadline)
        assert (deadline_wake.assignment_id, deadline_wake.fired_by) == (a, "deadline")

    def test_a_failed_wakes_completion_is_ignored(self, config):
        a = _live(config, auto_complete=True)
        made = _start(config, a)
        _complete(config, made)

        assert _end(config, made, status="failed", result=None, error="boom").outcome == "failed"
        assert _get(config, a).state == "sleeping"
        with db.get_db(config.db_path) as conn:
            assert [e.kind for e in store.list_events(conn, a)].count("complete_ignored") == 1


class TestFinishingIsSafe:
    def test_a_turned_away_wake_is_finished_once(self, config):
        a = _live(config)
        (made,) = _dispatch(config)
        with db.get_db(config.db_path) as conn:
            lifecycle.pause(conn, a, user_id="alice")
            task = db.get_task(conn, made.task_id)

        assert wakes.start_wake(config, task) is None
        assert wakes.finish_wake(config, task, "not_active") is False
        with db.get_db(config.db_path) as conn:
            assert store.get_wake(conn, made.wake_id).outcome == "not_active"
        assert _get(config, a).state_reason == "user"

    def test_a_finish_that_throws_ends_the_wake_and_pauses_for_failures(self, config):
        a = _live(config)
        made = _start(config, a)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, made.task_id)
        with patch.object(wakes, "_progress_signals", side_effect=RuntimeError("broken")):
            assert wakes.finish_wake(config, task, "completed") is False

        with db.get_db(config.db_path) as conn:
            wake = store.get_wake(conn, made.wake_id)
            kinds = [e.kind for e in store.list_events(conn, a)]
        assert (wake.outcome, wake.ended_at is not None) == ("failed", True)
        assert "finish_failed" in kinds and "wake_finished" not in kinds
        after = _get(config, a)
        assert (after.state, after.state_reason) == ("paused", "failures")


# ---------------------------------------------------------------------------
# The loop, through process_one_task
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(config, monkeypatch):
    """The skill CLI's environment, as the proxy gives a wake of alice's."""
    config.admin_users = {"alice"}
    monkeypatch.setattr("istota.config.load_config", lambda: config)
    monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.delenv("ISTOTA_WITHHELD_SCOPES", raising=False)
    return monkeypatch


class TestTheLoopThroughProcessOneTask:
    def test_gate_worker_finish_and_the_next_tick(self, config, cli_env):
        """Only `execute_task` is replaced: it picks the wake up as the
        executor does, then does what a model would, through the real CLI."""
        from istota.skills.assignment import main
        from tests.support.skill_cli import run_skill_main

        a = _live(config, items=(("Three quotes", "mechanical", CSV_CHECK),))

        def fake_execute(task, config_, user_resources, **kwargs):
            assert wakes.start_wake(config_, task) is not None
            cli_env.setenv("ISTOTA_TASK_ID", str(task.id))
            _file(config_, a, "quotes.csv").write_text("source,price\nacme,1200\n")
            for argv in (
                ["handoff", "photograph the furnace label", "--report", "the photo"],
                ["attempt", "--target", "beta.example/form", "--tried", "the form", "--outcome", "failed"],
                ["next", "--wait", "user", "--progress", "one quote in"],
            ):
                result = run_skill_main(main, argv)
                assert result.exit_code == 0, result.envelope
            return True, "One quote in; the label photo is with the user.", None, None

        (first,) = _dispatch(config)
        with patch("istota.scheduler.execute_task", side_effect=fake_execute), \
             patch("istota.notifications.delivery.send_notification", return_value=True) as notify:
            processed = sched.process_one_task(config, user_id="alice", queue="assignment")
        assert processed == (first.task_id, True)
        # One push, for the hand-off, carrying the title and not the step.
        [call] = notify.call_args_list
        assert "a step for you" in call.args[2] and "furnace" not in call.args[2]

        with db.get_db(config.db_path) as conn:
            wake = store.get_wake(conn, first.wake_id)
            [item] = store.list_items(conn, a)
            [action] = store.list_actions(conn, a, status="open")
            assert store.wake_snapshot(conn, first.wake_id, "after") is not None
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert wake.outcome == "next"
        assert wake.summary == "One quote in; the label photo is with the user."
        assert wake.progress_note == "one quote in"
        assert wake.progress_signals == ["attempt_new_outcome"]
        assert (item.status, item.detail is not None) == ("failing", True)
        after = _get(config, a)
        assert (after.state, after.waiting_on) == ("waiting", ["user"])
        assert ["handoff", action.id] in _finished_event(config, a, first.wake_id)["notices"]
        bell_row = "SELECT state, body FROM notifications WHERE source = 'assignment' AND dedup_key = ?"
        key = f"handoff:{a}:{action.id}"
        with db.get_db(config.db_path) as conn:
            row = conn.execute(bell_row, (key,)).fetchone()
        assert row["state"] == "open" and "furnace label" in row["body"]

        # Nothing fires while it waits; the user's report back wakes it.
        assert _dispatch(config, _later()) == []
        with db.get_db(config.db_path) as conn:
            lifecycle.complete_handoff(conn, a, user_id="alice", action_id=action.id, report="photo sent")
            assert conn.execute(bell_row, (key,)).fetchone()["state"] == "resolved"
        (second,) = _dispatch(config)
        assert (second.assignment_id, second.fired_by) == (a, "inbox")
        with db.get_db(config.db_path) as conn:
            assert store.get_wake(conn, second.wake_id).seq == 2


class TestAWakeSpawnsNoSubtasks:
    def test_deferred_subtasks_from_a_wake_are_refused_and_recorded(self, config, tmp_path):
        import json

        from istota.scheduler_deferred import _process_deferred_subtasks

        a = _live(config)
        made = _start(config, a)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, made.task_id)
        config.admin_users = {"alice"}
        user_temp = tmp_path / "deferred"
        user_temp.mkdir()
        path = user_temp / f"task_{task.id}_subtasks.json"
        path.write_text(json.dumps([{"prompt": "Go research quotes"}]))

        assert _process_deferred_subtasks(config, task, user_temp) == 0

        assert not path.exists()
        with db.get_db(config.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE parent_task_id = ?", (task.id,),
            ).fetchone()[0] == 0
            refused = [e for e in store.list_events(conn, a) if e.kind == "subtasks_refused"]
        assert [e.payload for e in refused] == [{"task_id": task.id, "count": 1}]
