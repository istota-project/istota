"""The scheduler's pressure-check seam: sampler → gate → snapshot → alert.

``host_pressure`` is covered against fixture ``/proc`` trees in
``test_host_pressure.py`` and the gate arithmetic in
``test_worker_pool_admission.py``. What is under test here is the wiring in
between: that a reading reaches the pool, that a crossing writes exactly one
snapshot and sends exactly one notification per cooldown window, and — the one
that matters most — that nothing this path does can take the daemon loop down.

That last point is not defensive boilerplate. The whole spec exists because a
host died in a way nobody could attribute afterwards; an instrument that raises
into the main loop would be the monitoring causing the outage it was added to
explain. Every test that patches a reader to raise is asserting that property.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from istota import db, host_pressure, scheduler
from istota.config import Config, SchedulerConfig
from istota.scheduler import WorkerPool, _check_host_pressure

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

# 2026-08-21 15:42 UTC on the production host. zram absorbed it: PSI 0.07,
# 2.9 GB still available. Neither original trigger fires; the residue does.
BURST = host_pressure.PressureSample(
    mem_total_kb=8138624,
    mem_available_kb=3002716,
    shmem_kb=1558528,
    swap_total_kb=4068860,
    swap_free_kb=1739260,
    cached_kb=1204812,
    psi_mem_some_avg10=0.07,
    psi_mem_full_avg10=0.0,
    psi_io_some_avg10=0.4,
    psi_cpu_some_avg10=0.0,
    load1=1.59,
)


@pytest.fixture
def config(db_path, tmp_path):
    (tmp_path / "mount").mkdir(exist_ok=True)
    # An admin has to exist or the alert path resolves no recipient and returns
    # early, which would make every notification assertion below vacuous.
    return Config(
        admin_users=["alice"],
        db_path=db_path,
        scheduler=SchedulerConfig(
            host_pressure_enabled=True,
            host_pressure_sample_interval_seconds=30,
            host_pressure_psi_threshold=40.0,
            min_available_memory_mb=768,
            host_pressure_alert_cooldown_seconds=900,
            host_pressure_shmem_unaccounted_alert_mb=1024,
        ),
        nextcloud_mount_path=tmp_path / "mount",
        temp_dir=tmp_path / "temp",
    )


def _check(config, pool, *, last_alert=0.0, now=1000.0, clocks=None, checks=None):
    """Call `_check_host_pressure` and wait for the snapshot it may spawn.

    The snapshot runs on a short-lived thread (it makes Docker round-trips and
    walks `/proc`, which must not block the dispatch loop), so every assertion
    about the log line or the operator alert would otherwise race it. Joining
    the thread `_spawn_background_check` records is what makes these tests
    deterministic under `-n auto` rather than merely usually-passing.
    """
    checks = {} if checks is None else checks
    result = _check_host_pressure(
        config,
        pool,
        last_alert=last_alert,
        alert_clocks={} if clocks is None else clocks,
        background_checks=checks,
        now=now,
    )
    thread = checks.get("host_pressure_snapshot")
    if thread is not None:
        thread.join(timeout=10)
        assert not thread.is_alive(), "snapshot thread did not finish"
    return result


def _snapshot_lines(caplog):
    return [
        r.message
        for r in caplog.records
        if r.message.startswith("host_pressure_snapshot")
    ]


class TestSamplerFeedsTheGate:
    def test_reading_reaches_the_pool(self, config):
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=HEALTHY), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]):
            _check(config, pool,last_alert=0.0, now=1000.0)
        assert pool._pressure_sample == HEALTHY

    def test_unreadable_sample_clears_rather_than_latches(self, config):
        pool = WorkerPool(config)
        pool.update_pressure(STARVED)
        with patch.object(host_pressure, "read_sample", return_value=None):
            _check(config, pool,last_alert=0.0, now=1000.0)
        assert pool._pressure_sample is None

    def test_disabled_does_not_sample_at_all(self, config):
        config.scheduler.host_pressure_enabled = False
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample") as read:
            _check(config, pool,last_alert=0.0, now=1000.0)
        read.assert_not_called()


class TestSnapshotOnCrossing:
    def test_incident_writes_a_snapshot(self, config, caplog):
        pool = WorkerPool(config)
        with caplog.at_level(logging.WARNING, logger="istota.scheduler"), \
             patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot\n  x=1"), \
             patch.object(scheduler, "_send_operator_alert"):
            _check(config, pool,last_alert=0.0, now=1000.0)
        assert len(_snapshot_lines(caplog)) == 1

    def test_quiet_host_writes_nothing(self, config, caplog):
        pool = WorkerPool(config)
        with caplog.at_level(logging.WARNING, logger="istota.scheduler"), \
             patch.object(host_pressure, "read_sample", return_value=HEALTHY), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot") as snap, \
             patch.object(scheduler, "_send_operator_alert") as alert:
            _check(config, pool,last_alert=0.0, now=1000.0)
        snap.assert_not_called()
        alert.assert_not_called()
        assert _snapshot_lines(caplog) == []

    def test_shmem_burst_snapshots_without_closing_the_gate(self, config, caplog):
        """The 2026-08-21 event, end to end.

        The evidence is collected *and* the queue keeps moving. Asserting only
        the first half would pass an implementation that folded the residue into
        ``is_under_pressure`` and stalled dispatch through a burst that zram had
        already handled.
        """
        pool = WorkerPool(config)
        with caplog.at_level(logging.WARNING, logger="istota.scheduler"), \
             patch.object(host_pressure, "read_sample", return_value=BURST), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot\n  x=1"), \
             patch.object(scheduler, "_send_operator_alert"):
            _check(config, pool,last_alert=0.0, now=1000.0)

        assert len(_snapshot_lines(caplog)) == 1
        assert pool._admission_open() is True

    def test_the_reason_is_logged_with_the_snapshot(self, config, caplog):
        pool = WorkerPool(config)
        with caplog.at_level(logging.WARNING, logger="istota.scheduler"), \
             patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert"):
            _check(config, pool,last_alert=0.0, now=1000.0)
        messages = " ".join(r.message for r in caplog.records)
        assert "psi_mem_some_avg10" in messages

    def test_snapshot_gets_the_docker_socket_from_config(self, config):
        """The handle is root-equivalent. It comes from config, never from the
        module default, so an operator can point it elsewhere or switch it off."""
        config.scheduler.host_pressure_docker_socket = "/run/somewhere/docker.sock"
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot") as snap, \
             patch.object(scheduler, "_send_operator_alert"):
            _check(config, pool,last_alert=0.0, now=1000.0)
        assert str(snap.call_args.kwargs["docker_socket"]) == "/run/somewhere/docker.sock"


class TestAlertCooldown:
    def test_one_notification_per_window(self, config):
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert") as alert:
            clocks = {}
            last = _check(config, pool, last_alert=0.0, now=1000.0, clocks=clocks)
            last = _check(config, pool, last_alert=last, now=1030.0, clocks=clocks)
            _check(config, pool, last_alert=last, now=1060.0, clocks=clocks)
        assert alert.call_count == 1

    def test_a_new_window_notifies_again(self, config):
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert") as alert:
            clocks = {}
            last = _check(config, pool, last_alert=0.0, now=1000.0, clocks=clocks)
            _check(config, pool, last_alert=last, now=1000.0 + 901, clocks=clocks)
        assert alert.call_count == 2

    def test_the_clock_only_advances_when_something_fired(self, config):
        """A quiet tick must not push the cooldown window forward, or a
        sustained squeeze would go unreported for as long as it kept sampling."""
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=HEALTHY), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]):
            assert _check(config, pool,last_alert=500.0, now=1000.0) == 500.0


class TestEscalatedWording:
    def test_names_istota_as_victim_when_it_is_running_nothing(self, config):
        """Gate shut past the cooldown with no worker of ours running means the
        pressure comes from elsewhere on the box. The operator needs to be told
        which of the two it is, because the remedy is different."""
        import time as _time

        pool = WorkerPool(config)
        pool.update_pressure(STARVED)
        pool.dispatch()  # closes the gate and stamps _gate_closed_since

        # Backdate the stamp rather than stubbing `gate_closed_seconds`, so the
        # duration half of the condition is computed for real. Stubbing the
        # method would leave only `active_count == 0` under test, and the edge
        # case in the spec is specifically about a duration being reached.
        with pool._pressure_lock:
            pool._gate_closed_since = _time.monotonic() - 1200.0
        assert pool.gate_closed_seconds() >= 1200.0

        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert") as alert:
            _check(config, pool, last_alert=0.0, now=1000.0)

        assert alert.call_count == 1
        message = alert.call_args.args[-1]
        assert "no istota worker" in message.lower()

    def test_a_briefly_closed_gate_does_not_escalate(self, config):
        """Just below the window: the gate is shut and nothing is running, but
        not for long enough to conclude the pressure is someone else's."""
        pool = WorkerPool(config)
        pool.update_pressure(STARVED)
        pool.dispatch()

        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert") as alert:
            _check(config, pool, last_alert=0.0, now=1000.0)

        assert "no istota worker" not in alert.call_args.args[-1].lower()

    def test_a_held_backlog_is_not_reported_as_someone_elses_problem(self, config):
        """The regression for keying the escalation on worker count.

        Gating the claim paths drains the pool within `worker_idle_timeout`
        (10s), far inside the cooldown window the duration arm compares against
        (900s), so `active_count == 0` became true of *every* sustained squeeze
        rather than only the ones istota is innocent of. Keyed on workers, this
        alert would tell the operator to look off-box during exactly the
        2026-08-20 shape — istota's own task allocated the memory, finished, and
        left the residue. A queued backlog is the signal the gate does not
        itself destroy.
        """
        import time as _time

        pool = WorkerPool(config)
        pool.update_pressure(STARVED)
        pool.dispatch()
        with pool._pressure_lock:
            pool._gate_closed_since = _time.monotonic() - 1200.0

        with db.get_db(config.db_path) as conn:
            db.create_task(conn, prompt="queued", user_id="alice")

        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert") as alert:
            _check(config, pool, last_alert=0.0, now=1000.0)

        message = alert.call_args.args[-1]
        assert "not causing it" not in message
        assert "queued and waiting for the gate" in message

    def test_the_alert_does_not_consume_the_closed_since_clock(self, config):
        """The wording path must ask the *pure* predicate.

        `_admission_open()` stamps `_gate_closed_since` when it is None, so
        asking it here would reset the clock read a few lines later and
        suppress the escalation on any pass where the snapshot asked first.
        """
        pool = WorkerPool(config)
        pool.update_pressure(STARVED)

        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert"):
            _check(config, pool, last_alert=0.0, now=1000.0)

        # Nothing on the alert path started the clock; only dispatch() does.
        assert pool.gate_closed_seconds() == 0.0

    def test_the_held_wording_covers_task_starts_not_just_workers(self, config):
        """"New workers are held" was the same thread-vs-start conflation the
        claim-path gate exists to fix."""
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert") as alert:
            _check(config, pool, last_alert=0.0, now=1000.0)

        message = alert.call_args.args[-1]
        assert "No new task starts" in message
        assert "New workers are held" not in message

    def test_ordinary_wording_when_the_gate_just_closed(self, config):
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert") as alert:
            _check(config, pool,last_alert=0.0, now=1000.0)

        message = alert.call_args.args[-1]
        assert "no istota worker" not in message.lower()


class TestNeverRaisesIntoTheLoop:
    def test_a_sampler_that_raises_is_contained(self, config):
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", side_effect=OSError("boom")):
            _check(config, pool, last_alert=0.0, now=1000.0)

    def test_a_sampler_that_raises_clears_the_last_reading(self, config):
        """The gate must not stay shut on evidence nobody is refreshing.

        Returning early from the exception path without clearing leaves the
        last sample latched in the pool. If that sample was a starved one, the
        gate stays closed for the life of the process: dispatch spawns nothing,
        forever, with one WARNING per cooldown as the only symptom — an
        unexplained total outage caused by the instrument added to explain one.

        The pool is seeded with a starved sample first. Without that seeding
        the assertion is vacuous, because a fresh pool's sample is already
        `None` and would pass against the unfixed code.
        """
        pool = WorkerPool(config)
        pool.update_pressure(STARVED)
        assert pool._admission_open() is False

        with patch.object(host_pressure, "read_sample", side_effect=OSError("boom")):
            _check(config, pool, last_alert=0.0, now=1000.0)

        assert pool._pressure_sample is None
        assert pool._admission_open() is True

    def test_a_snapshot_that_raises_is_contained(self, config):
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", side_effect=RuntimeError("boom")), \
             patch.object(scheduler, "_send_operator_alert"):
            _check(config, pool,last_alert=0.0, now=1000.0)

    def test_a_failing_notification_is_contained(self, config):
        """Notification is best-effort and must never propagate into dispatch."""
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert", side_effect=RuntimeError("boom")):
            _check(config, pool,last_alert=0.0, now=1000.0)

    def test_the_gate_still_gets_a_reading_when_the_snapshot_fails(self, config):
        """A failure in the attribution half must not cost the admission half
        its input — they are separate concerns sharing one sample."""
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", side_effect=OSError("boom")), \
             patch.object(scheduler, "_send_operator_alert"):
            _check(config, pool, last_alert=0.0, now=1000.0)
        assert pool._pressure_sample == STARVED


class TestAlertBodyMatchesReality:
    def _message(self, alert):
        return alert.call_args.args[-1]

    def test_residue_trigger_does_not_claim_the_queue_is_held(self, config):
        """The alert must describe the queue, not the usual case.

        The residue arm deliberately leaves admission open, so on the
        2026-08-21 burst — the event that arm exists for — telling the operator
        that workers are held sends them after a stall that is not happening.
        """
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=BURST), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert") as alert:
            _check(config, pool, last_alert=0.0, now=1000.0)

        message = self._message(alert)
        assert "held" not in message.lower()
        assert "admission is still open" in message.lower()

    def test_pressure_trigger_does_say_the_queue_is_held(self, config):
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert") as alert:
            _check(config, pool, last_alert=0.0, now=1000.0)

        assert "no new task starts" in self._message(alert).lower()

    def test_a_failed_snapshot_is_not_advertised_as_present(self, config):
        """Goal 2 of the spec is that an incident is attributable from the logs.
        Sending the operator after an artefact that was never written is worse
        than saying nothing."""
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", side_effect=RuntimeError("boom")), \
             patch.object(scheduler, "_send_operator_alert") as alert:
            _check(config, pool, last_alert=0.0, now=1000.0)

        message = self._message(alert)
        assert "could not be gathered" in message
        assert "naming the holders is in the log" not in message


class TestPerTriggerCooldown:
    def test_a_residue_alert_does_not_mute_a_memory_collapse(self, config):
        """One shared window would let the cheapest alert suppress the worst.

        The residue arm is the one that fires while the host is healthy, so it
        is the arm most likely to be holding the window when something real
        starts. A MemAvailable collapse halts dispatch; it must not wait out
        another trigger's cooldown to reach the operator.
        """
        pool = WorkerPool(config)
        clocks = {}
        with patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert") as alert:
            with patch.object(host_pressure, "read_sample", return_value=BURST):
                _check(config, pool, last_alert=0.0, now=1000.0, clocks=clocks)
            assert alert.call_count == 1

            # 60s later, well inside the 900s window the residue alert opened.
            with patch.object(host_pressure, "read_sample", return_value=STARVED):
                _check(config, pool, last_alert=1000.0, now=1060.0, clocks=clocks)
            assert alert.call_count == 2

    def test_the_same_trigger_still_respects_its_own_window(self, config):
        pool = WorkerPool(config)
        clocks = {}
        with patch.object(host_pressure, "read_sample", return_value=BURST), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert") as alert:
            _check(config, pool, last_alert=0.0, now=1000.0, clocks=clocks)
            _check(config, pool, last_alert=1000.0, now=1060.0, clocks=clocks)
        assert alert.call_count == 1

    def test_a_backward_clock_step_does_not_mute_alerts(self, config):
        """`now` is wall clock. A backward NTP correction makes the delta
        negative, and reading that as "inside the window" would silence every
        alert for the size of the step."""
        pool = WorkerPool(config)
        clocks = {}
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert") as alert:
            _check(config, pool, last_alert=0.0, now=100_000.0, clocks=clocks)
            _check(config, pool, last_alert=100_000.0, now=90_000.0, clocks=clocks)
        assert alert.call_count == 2


class TestTmpfsReadFailure:
    def test_a_failed_tmpfs_read_does_not_fake_a_residue(self, config):
        """`Shmem - Σ tmpfs` with an empty list reports all of Shmem as
        unaccounted. An empty list on the failure path means "the read failed",
        not "there are none" — and reading it as the latter fires a false burst
        on any host holding more than the threshold in ordinary tmpfs."""
        big_shmem = host_pressure.PressureSample(
            **{**HEALTHY.__dict__, "shmem_kb": 2 * 1024 * 1024}
        )
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=big_shmem), \
             patch.object(host_pressure, "read_tmpfs_usage", side_effect=OSError("boom")), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot") as snap, \
             patch.object(scheduler, "_send_operator_alert") as alert:
            _check(config, pool, last_alert=0.0, now=1000.0)

        snap.assert_not_called()
        alert.assert_not_called()

    def test_a_real_empty_tmpfs_list_still_arms_the_residue(self, config):
        """The distinction is the failure, not the emptiness. A host that
        genuinely has no tmpfs mounts still gets the residue arm."""
        big_shmem = host_pressure.PressureSample(
            **{**HEALTHY.__dict__, "shmem_kb": 2 * 1024 * 1024}
        )
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=big_shmem), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert") as alert:
            _check(config, pool, last_alert=0.0, now=1000.0)

        assert alert.call_count == 1


class TestSnapshotRunsOffTheLoopThread:
    def test_the_snapshot_is_dispatched_as_a_background_check(self, config):
        """It makes a Docker round-trip per container at 2s apiece and walks
        `/proc`, at the moment I/O is slowest, and the operator alert joins for
        30s on top. Inline that is a minute of dispatch starvation during an
        incident, which the spec's own Track B constraint forbids."""
        pool = WorkerPool(config)
        checks = {}
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert"):
            _check(config, pool, last_alert=0.0, now=1000.0, checks=checks)

        assert "host_pressure_snapshot" in checks

    def test_the_sample_still_reaches_the_gate_synchronously(self, config):
        """Only the snapshot is deferred. The gate needs this tick's reading,
        not whenever a thread gets to it."""
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot"), \
             patch.object(scheduler, "_send_operator_alert"):
            _check(config, pool, last_alert=0.0, now=1000.0)

        assert pool._pressure_sample == STARVED


class TestRunningTaskPidsReachTheSnapshot:
    """ISSUE-286: the sandbox tmpfs the snapshot could not see.

    The reader lives in ``host_pressure`` and is covered against a fixture
    ``/proc`` there. What is under test here is the seam — that the scheduler
    reads the task table and hands the pids over, since without that the
    attribution is a function nothing calls.
    """

    def _running_task(self, config, pid):
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="hello", user_id="alice")
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
            db.update_task_pid(conn, task_id, pid)
        return task_id

    def _snapshot_kwargs(self, config, pool):
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot") as snap, \
             patch.object(scheduler, "_send_operator_alert"):
            _check(config, pool, last_alert=0.0, now=1000.0)
        return snap.call_args.kwargs

    def test_a_running_tasks_pid_is_handed_to_the_snapshot(self, config):
        task_id = self._running_task(config, 533787)

        kwargs = self._snapshot_kwargs(config, WorkerPool(config))

        assert kwargs["task_pids"] == [(task_id, 533787)]

    def test_a_finished_task_is_not_offered_as_a_holder(self, config):
        """``worker_pid`` is cleared on the way out of ``running``, and the
        query leans on that. A stale pid would attribute the residue to a
        sandbox that no longer exists — or to whatever now owns that number."""
        task_id = self._running_task(config, 533787)
        with db.get_db(config.db_path) as conn:
            db.update_task_status(conn, task_id, "completed", result="done")

        kwargs = self._snapshot_kwargs(config, WorkerPool(config))

        assert kwargs["task_pids"] == []

    def test_a_running_task_with_no_pid_is_reported_not_dropped(self, config):
        """Reachable in production, and the reason the query does not filter on
        ``worker_pid``: NativeBrain never calls ``on_pid``, and neither does
        ClaudeCodeBrain's non-streaming path. Dropping those rows printed
        ``sandbox none-running`` on a host that was running work."""
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="hello", user_id="alice")
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))

        kwargs = self._snapshot_kwargs(config, WorkerPool(config))

        assert kwargs["task_pids"] == [(task_id, 0)]

    def test_no_running_tasks_is_an_empty_list_not_none(self, config):
        """The two render differently and mean different things: an empty list
        is "nothing is running", ``None`` is "the table could not be read"."""
        kwargs = self._snapshot_kwargs(config, WorkerPool(config))

        assert kwargs["task_pids"] == []

    def test_an_unreadable_task_table_costs_the_section_not_the_snapshot(self, config):
        """The outer net already stops the loop dying. This asserts the finer
        promise: a database that will not open must still leave the headline
        figures, the tmpfs list and the process table in the log."""
        pool = WorkerPool(config)
        with patch.object(host_pressure, "read_sample", return_value=STARVED), \
             patch.object(host_pressure, "read_tmpfs_usage", return_value=[]), \
             patch.object(db, "get_running_task_pids", side_effect=OSError("disk gone")), \
             patch.object(host_pressure, "snapshot", return_value="host_pressure_snapshot") as snap, \
             patch.object(scheduler, "_send_operator_alert"):
            _check(config, pool, last_alert=0.0, now=1000.0)

        assert snap.called
        assert snap.call_args.kwargs["task_pids"] is None
