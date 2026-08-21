"""The memory admission gate on ``WorkerPool.dispatch`` (spec Track C2).

What is under test is admission and only admission. The gate refuses to *start*
work while the host has no room for it; it never stops, preempts or evicts
anything already running, and it never fails a task. A pending task that is held
by the gate stays pending and is picked up by a later tick, which is what
dispatch already does for every other reason it declines to spawn.

The assertion that matters most here is the fail-open one. A sampler that
returns ``None`` — macOS, a kernel without the fields, a bug in the reader —
must leave dispatch behaving exactly as it did before this feature existed. The
inverse failure, a sampler defect silently halting all dispatch, would present
as an unexplained total outage: precisely the thing this spec was written to
prevent, caused by the instrument meant to prevent it.

``host_pressure.read_sample`` is not patched here. The pool is handed samples
directly through ``update_pressure``, because the seam under test is what
dispatch does with a reading, not how the reading is obtained — that half is
covered against fixture ``/proc`` trees in ``test_host_pressure.py``.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from istota import db, host_pressure
from istota.config import Config, SchedulerConfig
from istota.scheduler import WorkerPool


# A healthy box: 4.4 GB available, quiet PSI. Comfortably above any floor these
# tests set, so it can never be the reason a spawn is refused.
HEALTHY = host_pressure.PressureSample(
    mem_total_kb=8138624,
    mem_available_kb=4594688,
    shmem_kb=84992,
    swap_total_kb=4068860,
    swap_free_kb=4068860,
    cached_kb=1204812,
    psi_mem_some_avg10=0.0,
    psi_mem_full_avg10=0.0,
    psi_io_some_avg10=0.3,
    psi_cpu_some_avg10=0.0,
    load1=0.16,
)

# 2026-08-20, mid-incident: 289 MB available against a 768 MB floor, PSI 87.2.
STARVED = host_pressure.PressureSample(
    mem_total_kb=8129380,
    mem_available_kb=296284,
    shmem_kb=4641344,
    swap_total_kb=0,
    swap_free_kb=0,
    cached_kb=1508,
    psi_mem_some_avg10=87.2,
    psi_mem_full_avg10=60.0,
    psi_io_some_avg10=39.1,
    psi_cpu_some_avg10=0.0,
    load1=76.12,
)


def _config(db_path, tmp_path, **scheduler_kwargs):
    kwargs = {
        "worker_idle_timeout": 1,
        "poll_interval": 1,
        "host_pressure_enabled": True,
        "min_available_memory_mb": 768,
        "host_pressure_psi_threshold": 40.0,
    }
    kwargs.update(scheduler_kwargs)
    (tmp_path / "mount").mkdir(exist_ok=True)
    return Config(
        db_path=db_path,
        scheduler=SchedulerConfig(**kwargs),
        nextcloud_mount_path=tmp_path / "mount",
        temp_dir=tmp_path / "temp",
    )


def _pending_task_ids(db_path):
    with db.get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status = 'pending' ORDER BY id"
        ).fetchall()
    return [r["id"] for r in rows]


class TestAdmissionGate:
    def test_closed_gate_spawns_nothing(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        pool.update_pressure(STARVED)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 0

        pool.shutdown()

    def test_closed_gate_leaves_pending_rows_untouched(self, db_path, tmp_path):
        """Held, not failed, not expired, not claimed."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")
            db.create_task(conn, prompt="t2", user_id="bob")
        before = _pending_task_ids(db_path)

        pool = WorkerPool(config)
        pool.update_pressure(STARVED)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()

        assert _pending_task_ids(db_path) == before
        pool.shutdown()

    def test_gate_reopens_and_the_held_task_then_runs(self, db_path, tmp_path):
        """The hold is transient. This is what makes it a gate and not a drop."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.update_pressure(STARVED)
            pool.dispatch()
            assert pool.active_count == 0

            pool.update_pressure(HEALTHY)
            pool.dispatch()
            assert pool.active_count >= 1

        pool.shutdown()

    def test_closed_gate_does_not_stop_running_workers(self, db_path, tmp_path):
        """Admission only. Nothing already running is touched."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        pool.update_pressure(HEALTHY)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            running = pool.active_count
            assert running >= 1

            pool.update_pressure(STARVED)
            pool.dispatch()
            assert pool.active_count == running

        pool.shutdown()

    def test_low_available_alone_closes_the_gate(self, db_path, tmp_path):
        """A squeeze that has not yet turned into stalling still holds."""
        squeezed = host_pressure.PressureSample(
            **{**HEALTHY.__dict__, "mem_available_kb": 400 * 1024}
        )
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        pool.update_pressure(squeezed)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 0

        pool.shutdown()

    def test_large_shmem_residue_does_not_close_the_gate(self, db_path, tmp_path):
        """The 2026-08-21 burst: 1.5 GB of shmem, and zram handling it fine.

        The residue triggers a *snapshot* because the evidence is worth having.
        It must not trigger a hold: memory that swap is absorbing is not memory
        the next task cannot have, and refusing work here would turn a
        successfully-mitigated event into a self-inflicted queue stall.
        """
        burst = host_pressure.PressureSample(
            **{
                **HEALTHY.__dict__,
                "shmem_kb": 1558528,
                "mem_available_kb": 3002716,
                "swap_free_kb": 1739260,
                "psi_mem_some_avg10": 0.07,
            }
        )
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        pool.update_pressure(burst)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count >= 1

        pool.shutdown()


class TestFailOpen:
    def test_no_sample_dispatches_exactly_as_before(self, db_path, tmp_path):
        """The load-bearing assertion. No reading means no information, and no
        information must never mean "refuse"."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        assert pool._pressure_sample is None
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count >= 1

        pool.shutdown()

    def test_explicit_none_sample_reopens_the_gate(self, db_path, tmp_path):
        """A sampler that starts failing must not leave the last bad reading
        latched. ``None`` clears the gate rather than preserving it."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.update_pressure(STARVED)
            pool.dispatch()
            assert pool.active_count == 0

            pool.update_pressure(None)
            pool.dispatch()
            assert pool.active_count >= 1

        pool.shutdown()

    def test_disabled_feature_ignores_even_a_starved_sample(self, db_path, tmp_path):
        """``host_pressure_enabled = false`` leaves the host exactly as it was."""
        config = _config(db_path, tmp_path, host_pressure_enabled=False)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        pool.update_pressure(STARVED)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count >= 1

        pool.shutdown()

    def test_zero_floor_disables_the_floor_arm(self, db_path, tmp_path):
        """PSI is left armed here, and the sample is quiet on it, so the only
        thing that could refuse the spawn is the floor."""
        config = _config(db_path, tmp_path, min_available_memory_mb=0)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        pool.update_pressure(
            host_pressure.PressureSample(
                **{**STARVED.__dict__, "psi_mem_some_avg10": 0.0}
            )
        )
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count >= 1

        pool.shutdown()

    def test_both_thresholds_zero_opens_the_gate_on_the_worst_sample(
        self, db_path, tmp_path
    ):
        """The explicit "both switches off" branch, which nothing else reaches.

        Fed the incident's own sample — 289 MB available, PSI 87.2 — so every
        arm would fire if either were still armed. An implementation that
        deleted the branch passes the sibling test above and fails this one.
        """
        config = _config(
            db_path,
            tmp_path,
            min_available_memory_mb=0,
            host_pressure_psi_threshold=0.0,
        )
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        pool.update_pressure(STARVED)
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count >= 1

        pool.shutdown()

    def test_zero_psi_threshold_alone_does_not_disarm_the_floor(
        self, db_path, tmp_path
    ):
        """Switching one arm off must not switch the other off with it."""
        config = _config(db_path, tmp_path, host_pressure_psi_threshold=0.0)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        pool.update_pressure(STARVED)  # 289 MB available, under the 768 floor
        with patch("istota.scheduler.process_one_task", return_value=None):
            pool.dispatch()
            assert pool.active_count == 0

        pool.shutdown()


class TestGateLogCooldown:
    def _closed_lines(self, caplog):
        return [
            r.message
            for r in caplog.records
            if r.message.startswith("dispatch_admission_closed")
        ]

    def test_two_closed_ticks_log_once(self, db_path, tmp_path, caplog):
        """A 40-minute squeeze produces a handful of lines, not thousands."""
        config = _config(db_path, tmp_path, host_pressure_alert_cooldown_seconds=900)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        pool.update_pressure(STARVED)
        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            pool.dispatch()
            pool.dispatch()

        assert len(self._closed_lines(caplog)) == 1
        pool.shutdown()

    def test_the_line_says_why(self, db_path, tmp_path, caplog):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        pool.update_pressure(STARVED)
        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            pool.dispatch()

        lines = self._closed_lines(caplog)
        assert len(lines) == 1
        assert "mem_available_mb=289" in lines[0]

    def test_reopening_re_arms_the_log(self, db_path, tmp_path, caplog):
        """A second squeeze after a recovery is a new event and says so, even
        inside the cooldown window of the first."""
        config = _config(db_path, tmp_path, host_pressure_alert_cooldown_seconds=900)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="t1", user_id="alice")

        pool = WorkerPool(config)
        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            with patch("istota.scheduler.process_one_task", return_value=None):
                pool.update_pressure(STARVED)
                pool.dispatch()
                pool.update_pressure(HEALTHY)
                pool.dispatch()
                pool.update_pressure(STARVED)
                pool.dispatch()

        assert len(self._closed_lines(caplog)) == 2
        pool.shutdown()


class TestGateClosedDuration:
    def test_closed_since_tracks_the_first_closed_tick(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        pool = WorkerPool(config)

        assert pool.gate_closed_seconds() == 0.0

        pool.update_pressure(STARVED)
        pool.dispatch()
        first = pool.gate_closed_seconds()
        assert first >= 0.0

        pool.dispatch()
        assert pool.gate_closed_seconds() >= first

        pool.update_pressure(HEALTHY)
        pool.dispatch()
        assert pool.gate_closed_seconds() == 0.0

        pool.shutdown()
