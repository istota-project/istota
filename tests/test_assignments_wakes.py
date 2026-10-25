"""Creating an assignment's wakes, and the lane that runs them.

`wakes.dispatch_due` is the body of the scheduler's `assignments` gate: it
turns a sleeping or waiting assignment that is due into one wake task in the
assignment queue, one transaction per assignment, after the budget check.
`wakes.reap_orphans` finishes a wake whose task ended without anyone finishing
it. The lane is the third phase of `WorkerPool.dispatch`.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from istota import db
from istota import scheduler as sched
from istota.assignments import budget, charter, lifecycle, store, wakes
from istota.config import AssignmentsConfig, BrainConfig, SchedulerConfig, UserConfig
from istota.usage.telemetry import BrainUsage

FLAG = "skill_assignment"
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
    config.experimental.features = [FLAG]
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


def _inbox(config, assignment_id, kind="user_note"):
    with db.get_db(config.db_path) as conn:
        store.insert_inbox_event(conn, assignment_id, kind=kind, trusted=True, payload={"text": "hi"})


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


class TestTheDeadlineWake:
    def test_a_passed_deadline_wakes_a_sleeping_assignment_once(self, config):
        deadline = _iso(_now() - timedelta(minutes=1))
        assignment_id = _assignment(
            config, next_wake_at=_iso(_now() + timedelta(hours=5)), deadline=deadline,
        )

        assert [c.fired_by for c in _dispatch(config)] == ["deadline"]

        # Back to sleeping as a finished wake would leave it: no second deadline wake.
        (wake,) = _wakes(config, assignment_id)
        wakes.finish_wake(config, None, "completed", wake_id=wake.id)
        assert _get(config, assignment_id).state == "sleeping"
        with db.get_db(config.db_path) as conn:
            store.update_assignment(conn, assignment_id, next_wake_at=_iso(_now() + timedelta(hours=5)))
        assert _dispatch(config) == []

    def test_the_deadline_wakes_a_wait_on_a_proposed_completion(self, config):
        assignment_id = _assignment(
            config, state="waiting", waiting_on=["completion"],
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


class TestTheFlag:
    def test_with_the_flag_off_nothing_is_created(self, config):
        config.experimental.features = []
        assignment_id = _assignment(config, next_wake_at=_iso(_now()))

        assert _dispatch(config) == []
        assert _get(config, assignment_id).state == "sleeping"


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

    def test_the_lane_drains_with_the_flag_off(self, config):
        """A wake queued before the flag went off still reaches pickup, which ends it."""
        config.experimental.features = []
        with db.get_db(config.db_path) as conn:
            _pending_wake_task(conn, "alice")

        pool = sched.WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 1
            pool.shutdown()

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
        assert _get(config, assignment_id).state == "active"


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
