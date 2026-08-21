"""Elapsed-time slot reclassification for foreground workers (spec Track C1).

The 2026-08-20 head-of-line block was a per-user foreground cap of 2 consumed by
two long-running tasks. A short question sent to the same user's queue could not
be answered for forty minutes, because nothing distinguished "still thinking
about your test suite" from "still holding an interactive slot".

The fix is reactive, not predictive. Nothing observable at enqueue time separates
a developer-skill task on a fresh worktree from "what time is my meeting" — the
task that caused the incident arrived as an ordinary chat message. Elapsed time
is the only signal that cannot be wrong, so a *running* task older than
``long_task_threshold_minutes`` stops counting against the interactive cap and
counts against a separate long allowance instead. The task itself is untouched:
it keeps running, in place, on the same worker. Nothing is preempted, migrated
or killed anywhere in this feature.

Two assertions here carry more weight than the rest.

``max_long_workers`` bounds *discounts*, not long tasks. A task becomes long
while it is already running, so the cap cannot refuse it retroactively; long
tasks beyond the cap keep counting as ordinary interactive occupancy. An
implementation that read the cap as a limit on long tasks would either have to
kill work or would let the allowance grow without bound as more tasks aged past
the threshold, and it would pass every test here except that one.

The instance-wide thread ceiling does not move. Per user the long allowance is
additive (2 interactive + 1 long = 3 threads), so one person's long job cannot
eat their own interactivity. Instance-wide it is partitioned: total foreground
threads stay capped at ``max_foreground_workers`` exactly as before, with at
most ``max_long_workers`` of them discounted. The box's worst-case memory
exposure is the whole subject of this spec and must not grow to buy fairness.

Workers are planted directly into ``pool._workers`` rather than spawned, and
``UserWorker`` is patched out. What is under test is the accounting — how many
threads a user may hold and which slot indices exist — not the worker loop,
which ``test_worker_pool_admission.py`` drives for real.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, SchedulerConfig
from istota.scheduler import (
    WorkerPool,
    allocate_long_discounts,
    plan_foreground_slots,
)


class _StubWorker:
    """A ``UserWorker`` that holds a slot and does nothing else."""

    def __init__(self, *args, **kwargs):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def request_stop(self):
        self.stopped = True

    def join(self, timeout=None):
        return None


def _config(db_path, tmp_path, **scheduler_kwargs):
    kwargs = {
        "worker_idle_timeout": 1,
        "poll_interval": 1,
        # The gate is not what these tests are about; keep it out of the way so
        # a refused spawn can only ever mean the slot accounting refused it.
        "host_pressure_enabled": False,
        "long_task_threshold_minutes": 10,
        "user_max_long_workers": 1,
        "max_long_workers": 2,
    }
    kwargs.update(scheduler_kwargs)
    (tmp_path / "mount").mkdir(exist_ok=True)
    return Config(
        db_path=db_path,
        scheduler=SchedulerConfig(**kwargs),
        nextcloud_mount_path=tmp_path / "mount",
        temp_dir=tmp_path / "temp",
    )


def _running_task(conn, user_id, *, age_minutes, queue="foreground"):
    """A task in ``running`` with a ``started_at`` the given age.

    ``update_task_status`` stamps ``datetime('now')``, so the age is written
    afterwards in the same SQL vocabulary the counter reads it in.
    """
    task_id = db.create_task(conn, prompt="long", user_id=user_id, queue=queue)
    db.update_task_status(conn, task_id, "running")
    conn.execute(
        "UPDATE tasks SET started_at = datetime('now', ?) WHERE id = ?",
        (f"-{age_minutes} minutes", task_id),
    )
    conn.commit()
    return task_id


def _plant_workers(pool, user_id, count, *, queue="foreground"):
    for slot in range(count):
        pool._workers[(user_id, queue, slot)] = _StubWorker()


def _slots_for(pool, user_id, queue="foreground"):
    return sorted(
        s for (uid, qt, s) in pool._workers if uid == user_id and qt == queue
    )


class TestPlanForegroundSlots:
    """The per-user arithmetic, isolated from the DB and the pool."""

    def test_no_long_tasks_matches_the_old_cap(self):
        plan = plan_foreground_slots(
            threads=1, discounted=0, pending=5, user_fg_cap=2, user_max_long_workers=1
        )
        assert plan.interactive == 1
        assert plan.may_spawn == 1

    def test_a_discounted_long_task_frees_an_interactive_slot(self):
        """The incident: two threads held, one of them long past the threshold."""
        plan = plan_foreground_slots(
            threads=2, discounted=1, pending=1, user_fg_cap=2, user_max_long_workers=1
        )
        assert plan.interactive == 1
        assert plan.may_spawn == 1
        assert plan.slot_range == 3

    def test_the_additive_ceiling_binds_at_three_threads(self):
        plan = plan_foreground_slots(
            threads=3, discounted=1, pending=5, user_fg_cap=2, user_max_long_workers=1
        )
        assert plan.may_spawn == 0

    def test_long_tasks_beyond_the_discount_stay_interactive_occupancy(self):
        """The allowance bounds discounts, not long tasks.

        Three long tasks against a discount cap of 1: exactly one is discounted
        and the other two still occupy interactive slots, so the allowance does
        not grow with the number of long tasks. An implementation that read the
        cap as a limit on long tasks would discount all three and report
        ``interactive == 0``.

        Composed through `allocate_long_discounts` deliberately rather than
        passing ``discounted=1`` as a literal. The decision this pins lives in
        the allocator, so a test that hands `plan_foreground_slots` the already
        correct answer asserts nothing about it — it would pass just as well
        against the inverted implementation.
        """
        discounts = allocate_long_discounts(
            {"alice": 3}, priority=["alice"], user_cap=1, instance_cap=2
        )
        plan = plan_foreground_slots(
            threads=3,
            discounted=discounts.get("alice", 0),
            pending=5,
            user_fg_cap=2,
            user_max_long_workers=1,
        )
        assert plan.interactive == 2
        assert plan.may_spawn == 0

    def test_more_discount_than_threads_floors_at_zero_occupancy(self):
        """The counts are read before the lock, so a task can complete in
        between and leave ``discounted`` momentarily larger than ``threads``.
        Occupancy must floor at zero rather than going negative and taking the
        caller's ``range`` arithmetic with it."""
        plan = plan_foreground_slots(
            threads=1, discounted=2, pending=9, user_fg_cap=2, user_max_long_workers=1
        )
        assert plan.interactive == 0
        assert plan.may_spawn == 2  # bounded by the 3-thread additive ceiling

    def test_an_over_grant_is_still_bounded_by_the_additive_ceiling(self):
        """The ghost-row case, pinned as bounded rather than as impossible.

        A `running` row that outlives its worker excuses occupancy no thread is
        holding, and nothing in this function can tell that row from a real
        one. What it can guarantee is the bound: even with the discount fully
        unearned, the user cannot exceed `user_fg_cap + user_max_long_workers`.
        An implementation that dropped the second term of the `min` would
        return 3 here and let the over-grant compound.
        """
        plan = plan_foreground_slots(
            threads=2, discounted=2, pending=99, user_fg_cap=2, user_max_long_workers=1
        )
        assert plan.may_spawn == 1
        assert plan.slot_range == 3

    def test_pending_bounds_the_spawn_count(self):
        plan = plan_foreground_slots(
            threads=0, discounted=0, pending=1, user_fg_cap=2, user_max_long_workers=1
        )
        assert plan.may_spawn == 1

    def test_zero_long_allowance_is_exactly_the_old_behaviour(self):
        plan = plan_foreground_slots(
            threads=2, discounted=0, pending=5, user_fg_cap=2, user_max_long_workers=0
        )
        assert plan.may_spawn == 0
        assert plan.slot_range == 2

    def test_may_spawn_never_goes_negative(self):
        """A user over their cap — config lowered under a live pool — asks for
        no spawns rather than a negative count that a ``range`` would read as
        an empty generator only by accident."""
        plan = plan_foreground_slots(
            threads=4, discounted=0, pending=5, user_fg_cap=2, user_max_long_workers=1
        )
        assert plan.may_spawn == 0


class TestAllocateLongDiscounts:
    """The instance-wide budget: at most ``max_long_workers`` discounts exist."""

    def test_each_user_is_capped_by_the_per_user_allowance(self):
        granted = allocate_long_discounts(
            {"alice": 3},
            priority=["alice"],
            user_cap=1,
            instance_cap=2,
        )
        assert granted == {"alice": 1}

    def test_the_instance_budget_bounds_the_total(self):
        """Three users each holding a long task, against an instance cap of 2:
        two are discounted and the third's long task keeps counting as
        interactive occupancy. This is what keeps the box's exposure fixed
        while the per-user allowance is additive."""
        granted = allocate_long_discounts(
            {"alice": 1, "bob": 1, "carol": 1},
            priority=["alice", "bob", "carol"],
            user_cap=1,
            instance_cap=2,
        )
        assert sum(granted.values()) == 2
        assert granted.get("carol", 0) == 0

    def test_priority_order_decides_who_gets_the_scarce_discount(self):
        granted = allocate_long_discounts(
            {"alice": 1, "bob": 1},
            priority=["bob", "alice"],
            user_cap=1,
            instance_cap=1,
        )
        assert granted == {"bob": 1}

    def test_users_outside_the_priority_list_are_still_counted(self):
        """A user with a long task but nothing pending never reaches dispatch's
        loop, and would otherwise consume none of the budget while still
        holding an extra thread — which is how the instance ceiling leaks."""
        granted = allocate_long_discounts(
            {"alice": 1, "zed": 1},
            priority=["alice"],
            user_cap=1,
            instance_cap=1,
        )
        assert granted == {"alice": 1}

    def test_a_zero_instance_cap_grants_nothing(self):
        granted = allocate_long_discounts(
            {"alice": 2}, priority=["alice"], user_cap=1, instance_cap=0
        )
        assert granted == {}

    def test_a_zero_user_cap_grants_nothing(self):
        granted = allocate_long_discounts(
            {"alice": 2}, priority=["alice"], user_cap=0, instance_cap=5
        )
        assert granted == {}


class TestCountLongRunningTasks:
    def test_it_counts_only_tasks_past_the_threshold(self, db_path):
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=40)
            _running_task(conn, "alice", age_minutes=1)
            counts = db.count_long_running_tasks_by_user(conn, "foreground", 10)
        assert counts == {"alice": 1}

    def test_it_groups_by_user(self, db_path):
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=40)
            _running_task(conn, "alice", age_minutes=30)
            _running_task(conn, "bob", age_minutes=20)
            counts = db.count_long_running_tasks_by_user(conn, "foreground", 10)
        assert counts == {"alice": 2, "bob": 1}

    def test_it_is_scoped_to_the_queue(self, db_path):
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=40)
            _running_task(conn, "alice", age_minutes=40, queue="background")
            fg = db.count_long_running_tasks_by_user(conn, "foreground", 10)
            bg = db.count_long_running_tasks_by_user(conn, "background", 10)
        assert fg == {"alice": 1}
        assert bg == {"alice": 1}

    def test_a_pending_task_is_not_long_however_old(self, db_path):
        """Only *running* work holds a slot. A task waiting in the queue has no
        worker to discount, and counting it would hand out an allowance against
        occupancy that does not exist."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="old", user_id="alice")
            conn.execute(
                "UPDATE tasks SET created_at = datetime('now', '-90 minutes') "
                "WHERE id = ?",
                (task_id,),
            )
            conn.commit()
            counts = db.count_long_running_tasks_by_user(conn, "foreground", 10)
        assert counts == {}

    def test_a_null_started_at_counts_as_short(self, db_path):
        """The conservative reading: an unknown age keeps the interactive cap
        tight rather than loose."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="x", user_id="alice")
            conn.execute(
                "UPDATE tasks SET status = 'running', started_at = NULL WHERE id = ?",
                (task_id,),
            )
            conn.commit()
            counts = db.count_long_running_tasks_by_user(conn, "foreground", 10)
        assert counts == {}

    def test_a_completed_task_does_not_count(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = _running_task(conn, "alice", age_minutes=40)
            conn.execute(
                "UPDATE tasks SET status = 'completed' WHERE id = ?", (task_id,)
            )
            conn.commit()
            counts = db.count_long_running_tasks_by_user(conn, "foreground", 10)
        assert counts == {}

    @pytest.mark.parametrize("threshold", [0, -5])
    def test_a_non_positive_threshold_returns_nothing(self, db_path, threshold):
        """The off switch. A threshold of zero would otherwise make every
        running task long the instant it started."""
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=40)
            counts = db.count_long_running_tasks_by_user(
                conn, "foreground", threshold
            )
        assert counts == {}


class TestDispatchReclassification:
    """End to end through ``dispatch``, against a real DB."""

    def test_a_long_task_frees_a_slot_for_a_waiting_question(
        self, db_path, tmp_path
    ):
        """The incident, reproduced. Two foreground threads held by tasks past
        the threshold, one short message waiting. Against the pre-change code
        this spawns nothing and the message waits for the suite to finish."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=40)
            _running_task(conn, "alice", age_minutes=12)
            db.create_task(conn, prompt="we back?", user_id="alice")

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 2)
        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        assert _slots_for(pool, "alice") == [0, 1, 2]
        pool.shutdown()

    def test_short_tasks_still_block(self, db_path, tmp_path):
        """The old cap is intact for work that has not demonstrated it is long."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=2)
            _running_task(conn, "alice", age_minutes=1)
            db.create_task(conn, prompt="we back?", user_id="alice")

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 2)
        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        assert _slots_for(pool, "alice") == [0, 1]
        pool.shutdown()

    def test_the_long_workers_are_not_touched(self, db_path, tmp_path):
        """Reclassification is accounting. Nothing is stopped or migrated."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=40)
            _running_task(conn, "alice", age_minutes=40)
            db.create_task(conn, prompt="we back?", user_id="alice")

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 2)
        held = [pool._workers[("alice", "foreground", s)] for s in (0, 1)]
        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        assert all(not w.stopped for w in held)
        assert [pool._workers[("alice", "foreground", s)] for s in (0, 1)] == held
        pool.shutdown()

    def test_the_per_user_allowance_stops_at_one_extra_thread(
        self, db_path, tmp_path
    ):
        """Three threads is the ceiling; a fourth pending task waits."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            for _ in range(3):
                _running_task(conn, "alice", age_minutes=40)
            db.create_task(conn, prompt="q1", user_id="alice")
            db.create_task(conn, prompt="q2", user_id="alice")

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 3)
        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        assert _slots_for(pool, "alice") == [0, 1, 2]
        pool.shutdown()

    def test_the_feature_is_inert_with_a_zero_threshold(self, db_path, tmp_path):
        """The off switch, end to end: identical to the pre-change behaviour."""
        config = _config(db_path, tmp_path, long_task_threshold_minutes=0)
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=90)
            _running_task(conn, "alice", age_minutes=90)
            db.create_task(conn, prompt="we back?", user_id="alice")

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 2)
        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        assert _slots_for(pool, "alice") == [0, 1]
        pool.shutdown()

    def test_the_feature_is_inert_with_a_zero_instance_budget(
        self, db_path, tmp_path
    ):
        config = _config(db_path, tmp_path, max_long_workers=0)
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=90)
            _running_task(conn, "alice", age_minutes=90)
            db.create_task(conn, prompt="we back?", user_id="alice")

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 2)
        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        assert _slots_for(pool, "alice") == [0, 1]
        pool.shutdown()

    @pytest.mark.parametrize(
        "off",
        [
            {"long_task_threshold_minutes": 0},
            {"user_max_long_workers": 0},
            {"max_long_workers": 0},
        ],
    )
    def test_an_off_switch_skips_the_query_rather_than_discarding_it(
        self, db_path, tmp_path, off
    ):
        """"0 disables" has to mean the tick does not pay for it.

        Each of the three keys is documented as an off switch, and dispatch
        runs every ~0.5s. An implementation that guards only the allocator
        still runs the grouped scan on every tick and throws the result away,
        which is a cost the wording does not promise.
        """
        config = _config(db_path, tmp_path, **off)
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=90)
            db.create_task(conn, prompt="we back?", user_id="alice")

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 1)
        with patch(
            "istota.db.count_long_running_tasks_by_user"
        ) as counter, patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        counter.assert_not_called()
        pool.shutdown()

    def test_the_feature_is_inert_with_a_zero_user_allowance(
        self, db_path, tmp_path
    ):
        config = _config(db_path, tmp_path, user_max_long_workers=0)
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=90)
            _running_task(conn, "alice", age_minutes=90)
            db.create_task(conn, prompt="we back?", user_id="alice")

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 2)
        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        assert _slots_for(pool, "alice") == [0, 1]
        pool.shutdown()


class TestInstanceCeiling:
    def test_total_threads_never_exceed_max_foreground_workers(
        self, db_path, tmp_path
    ):
        """The box's total exposure does not grow to buy per-user fairness.

        Deliberately given headroom: two users at their interactive cap, each
        with a discountable long task, against `max_foreground_workers = 5`.
        Four threads are planted, so the loop starts *below* the ceiling and
        has to reach it mid-iteration — alice takes slot 2 and bob is refused
        at `active_fg == 5`.

        Saturating the ceiling before the loop starts would make this vacuous:
        `if active_fg >= fg_cap: break` fires on the first iteration, none of
        the new arithmetic runs, and the assertion holds against an
        implementation with no ceiling check at all.
        """
        config = _config(
            db_path, tmp_path, max_foreground_workers=5, max_long_workers=2
        )
        users = ["alice", "bob"]
        with db.get_db(db_path) as conn:
            for user in users:
                _running_task(conn, user, age_minutes=40)
                _running_task(conn, user, age_minutes=40)
                db.create_task(conn, prompt="q", user_id=user)

        pool = WorkerPool(config)
        for user in users:
            _plant_workers(pool, user, 2)

        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        total = sum(1 for (_, qt, _) in pool._workers if qt == "foreground")
        assert total == 5
        # One user got the extra thread, the other was cut off by the ceiling.
        assert sorted(
            len(_slots_for(pool, u)) for u in users
        ) == [2, 3]
        pool.shutdown()

    def test_the_budget_is_not_spent_on_users_who_are_not_blocked(
        self, db_path, tmp_path
    ):
        """A discount must go to the user it unblocks.

        `priority` is oldest-pending-first, which does not correlate with "at
        cap". Alice and bob each hold one thread against a cap of two, so they
        spawn a second thread on the ordinary cap whether or not they are
        discounted — the discount buys them nothing. Carol is at cap with a
        question waiting, and is the only user in the tick a discount can help.

        Allocating over every long-task holder rather than every *blocked* one
        let alice and bob take the whole budget and leave carol refused, which
        is precisely the head-of-line block this feature exists to remove.
        """
        config = _config(
            db_path, tmp_path, max_foreground_workers=20, max_long_workers=2
        )
        with db.get_db(db_path) as conn:
            # Oldest pending first, so alice and bob are ahead of carol in the
            # priority order and would win a naive allocation.
            for user in ("alice", "bob"):
                _running_task(conn, user, age_minutes=40)
                for _ in range(3):
                    db.create_task(conn, prompt="q", user_id=user)
            _running_task(conn, "carol", age_minutes=40)
            _running_task(conn, "carol", age_minutes=3)
            db.create_task(conn, prompt="we back?", user_id="carol")

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 1)
        _plant_workers(pool, "bob", 1)
        _plant_workers(pool, "carol", 2)

        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        assert _slots_for(pool, "carol") == [0, 1, 2], "the blocked user got the slot"
        # The unblocked pair reach their ordinary cap of 2, not the 3 a
        # discount would have bought them.
        assert _slots_for(pool, "alice") == [0, 1]
        assert _slots_for(pool, "bob") == [0, 1]
        pool.shutdown()

    def test_the_instance_discount_budget_bounds_who_gets_an_extra_thread(
        self, db_path, tmp_path
    ):
        """Three users at their interactive cap, each with a long task, against
        ``max_long_workers = 2`` and instance room to spare. Two users get the
        extra thread; the third's long task keeps counting as occupancy."""
        config = _config(
            db_path, tmp_path, max_foreground_workers=20, max_long_workers=2
        )
        users = ["alice", "bob", "carol"]
        with db.get_db(db_path) as conn:
            for user in users:
                _running_task(conn, user, age_minutes=40)
                _running_task(conn, user, age_minutes=40)
                db.create_task(conn, prompt="q", user_id=user)

        pool = WorkerPool(config)
        for user in users:
            _plant_workers(pool, user, 2)

        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        extra = [u for u in users if len(_slots_for(pool, u)) == 3]
        assert len(extra) == 2
        pool.shutdown()

    def test_background_dispatch_is_unaffected(self, db_path, tmp_path):
        """Reclassification is foreground-only. Background work is scheduled,
        not interactive, so there is no head-of-line question to unblock."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=90, queue="background")
            db.create_task(conn, prompt="bg", user_id="alice", queue="background")

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 1, queue="background")
        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        assert _slots_for(pool, "alice", queue="background") == [0]
        pool.shutdown()


class TestTheAllowanceIsTakenBack:
    """The extra thread is on loan, not a permanent raise.

    `dispatch` only ever adds threads, and a `UserWorker` re-claims on its own
    without rechecking its slot. So without an explicit retirement the moment a
    long task ended while its user still had backlog, the borrowed thread went
    on serving ordinary interactive work — "2 interactive + 1 long" quietly
    becoming "3 interactive" for as long as the queue stayed non-empty.
    """

    def test_the_extra_worker_is_asked_to_stop_when_the_long_task_ends(
        self, db_path, tmp_path
    ):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            long_id = _running_task(conn, "alice", age_minutes=40)
            _running_task(conn, "alice", age_minutes=1)
            db.create_task(conn, prompt="we back?", user_id="alice")

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 2)
        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()
            assert _slots_for(pool, "alice") == [0, 1, 2], "the loan was made"
            borrowed = pool._workers[("alice", "foreground", 2)]

            # The long task finishes; the user still has backlog.
            with db.get_db(db_path) as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'completed' WHERE id = ?", (long_id,)
                )
                conn.commit()
                db.create_task(conn, prompt="another", user_id="alice")
            pool.dispatch()

        assert borrowed.stopped, "the borrowed thread should be retired"
        pool.shutdown()

    def test_it_finishes_its_task_rather_than_being_killed(
        self, db_path, tmp_path
    ):
        """Retirement is a `request_stop`, which `UserWorker.run` honours at the
        top of its loop — so a worker mid-task completes that task first. The
        same rule the long task itself is held to: nothing is preempted."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            long_id = _running_task(conn, "alice", age_minutes=40)
            _running_task(conn, "alice", age_minutes=1)
            db.create_task(conn, prompt="q", user_id="alice")

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 2)
        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()
            with db.get_db(db_path) as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'completed' WHERE id = ?", (long_id,)
                )
                conn.commit()
            pool.dispatch()

        # Still registered — it unregisters itself via _on_worker_exit when its
        # own loop ends, not synchronously from dispatch.
        assert ("alice", "foreground", 2) in pool._workers
        pool.shutdown()

    def test_a_worker_still_covered_by_a_discount_is_left_alone(
        self, db_path, tmp_path
    ):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=40)
            _running_task(conn, "alice", age_minutes=1)
            db.create_task(conn, prompt="q", user_id="alice")

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 2)
        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()
            borrowed = pool._workers[("alice", "foreground", 2)]
            with db.get_db(db_path) as conn:
                db.create_task(conn, prompt="more", user_id="alice")
            pool.dispatch()

        assert not borrowed.stopped
        pool.shutdown()

    def test_a_worker_stranded_by_a_lowered_cap_is_retired(
        self, db_path, tmp_path
    ):
        """Pre-existing gap, closed by the same sweep: lowering a cap under a
        live pool used to strand a worker at an index outside the new range."""
        config = _config(db_path, tmp_path, user_max_long_workers=0)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="q", user_id="alice")

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 4)  # cap is 2; slots 2 and 3 are surplus
        stranded = [pool._workers[("alice", "foreground", s)] for s in (2, 3)]
        kept = [pool._workers[("alice", "foreground", s)] for s in (0, 1)]

        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        assert all(w.stopped for w in stranded)
        assert not any(w.stopped for w in kept)
        pool.shutdown()


class TestTheSameConversationGateStillBinds:
    def test_a_follow_up_in_the_same_room_is_not_unblocked(
        self, db_path, tmp_path
    ):
        """A documented limit, not a defect.

        `count_claimable_tasks_for_user_queue` applies the per-channel gate on
        the foreground queue, so a pending task in the same conversation as a
        running one is not claimable and counts as 0 pending. Freeing a slot
        cannot help — there is nothing the freed slot may claim.

        That gate is deliberate and older than this feature (it is what answers
        "Still working on a previous request"). What the long allowance fixes
        is a long job in one room blocking a question in *another*, which is
        the case where a slot really was the binding constraint.
        """
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=40)
            _running_task(conn, "alice", age_minutes=12)
            db.create_task(
                conn, prompt="we back?", user_id="alice",
                conversation_token="room-abc", source_type="talk",
            )
            # The two running tasks are in that room too.
            conn.execute(
                "UPDATE tasks SET conversation_token = 'room-abc' "
                "WHERE status = 'running'"
            )
            conn.commit()

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 2)
        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        assert _slots_for(pool, "alice") == [0, 1]
        pool.shutdown()

    def test_a_question_in_a_different_room_is_unblocked(
        self, db_path, tmp_path
    ):
        """The case the feature actually addresses."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            _running_task(conn, "alice", age_minutes=40)
            _running_task(conn, "alice", age_minutes=12)
            conn.execute(
                "UPDATE tasks SET conversation_token = 'room-work' "
                "WHERE status = 'running'"
            )
            db.create_task(
                conn, prompt="we back?", user_id="alice",
                conversation_token="room-chat", source_type="talk",
            )
            conn.commit()

        pool = WorkerPool(config)
        _plant_workers(pool, "alice", 2)
        with patch("istota.scheduler.UserWorker", _StubWorker):
            pool.dispatch()

        assert _slots_for(pool, "alice") == [0, 1, 2]
        pool.shutdown()


