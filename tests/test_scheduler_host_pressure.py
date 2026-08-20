"""Tests for the scheduler's host-pressure breadcrumb seam.

``host_pressure`` itself is covered against fixture ``/proc`` trees in
``test_host_pressure.py``. What is under test here is the wiring: that one line
reaches the dedicated logger, that a platform with no ``/proc`` says so once
instead of once per interval, and — the one that matters most — that nothing the
instrumentation does can take the daemon loop down. An emitter that crashes the
scheduler would be the instrument causing the outage it exists to explain.

``host_pressure.read_sample`` is patched here rather than pointed at a fixture
tree because ``/proc`` is the system boundary this module sits on, and the
scheduler deliberately does not make the proc root configurable.
"""

from __future__ import annotations

import logging

from istota import host_pressure, scheduler
from istota.config import SchedulerConfig
from istota.scheduler import _emit_host_pressure_breadcrumb

SAMPLE = host_pressure.PressureSample(
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


def _reset_warn_latch(monkeypatch):
    """The "no PSI here" latch is module state; leave it as we found it.

    The suite runs under ``-n auto`` and these tests are order-independent only
    if neither of them inherits the other's latch.
    """
    monkeypatch.setattr(scheduler, "_host_pressure_unavailable_warned", False)


def _breadcrumb_lines(caplog):
    """Match the record, not merely its prefix.

    The failure notice is prefixed ``host_pressure_error`` precisely so it
    cannot be mistaken for a record, but a helper filtering on
    ``"host_pressure "`` alone would still be one rename away from classifying
    a warning as data. Anchor on the first field instead.
    """
    return [
        r.message
        for r in caplog.records
        if r.message.startswith("host_pressure mem_total_kb=")
    ]


class TestEmitHostPressureBreadcrumb:
    def test_emits_one_line_on_the_dedicated_logger(self, monkeypatch, caplog):
        _reset_warn_latch(monkeypatch)
        monkeypatch.setattr(host_pressure, "read_sample", lambda *a, **k: SAMPLE)
        monkeypatch.setattr(
            host_pressure,
            "read_tmpfs_usage",
            lambda *a, **k: [host_pressure.TmpfsUsage("/dev/shm", 4064688 * 1024, 400_000 * 1024)],
        )

        with caplog.at_level(logging.INFO, logger="istota.scheduler.pressure"):
            _emit_host_pressure_breadcrumb()

        lines = _breadcrumb_lines(caplog)
        assert len(lines) == 1
        assert "shmem_kb=4641344" in lines[0]
        assert "shmem_unaccounted_kb=4241344" in lines[0]
        assert "tmpfs_used_kb=/dev/shm:400000" in lines[0]

    def test_absent_proc_says_so_once_and_then_stays_quiet(self, monkeypatch, caplog):
        """macOS, or any tree that is not a Linux procfs. A platform fact
        rather than a fault, so it is not worth a line every five minutes
        forever. Note this is *not* the no-PSI case: a kernel with PSI switched
        off still produces a breadcrumb, carrying `?` for the PSI fields."""
        _reset_warn_latch(monkeypatch)
        monkeypatch.setattr(host_pressure, "read_sample", lambda *a, **k: None)

        with caplog.at_level(logging.INFO, logger="istota.scheduler"):
            _emit_host_pressure_breadcrumb()
            _emit_host_pressure_breadcrumb()
            _emit_host_pressure_breadcrumb()

        notices = [r for r in caplog.records if "no memory breadcrumb" in r.message]
        assert len(notices) == 1
        assert notices[0].levelno == logging.INFO
        assert _breadcrumb_lines(caplog) == []

    def test_a_failing_reader_warns_and_does_not_propagate(self, monkeypatch, caplog):
        _reset_warn_latch(monkeypatch)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("proc went away")

        monkeypatch.setattr(host_pressure, "read_sample", _boom)

        with caplog.at_level(logging.WARNING, logger="istota.scheduler.pressure"):
            _emit_host_pressure_breadcrumb()  # must not raise

        warnings = [r for r in caplog.records if "breadcrumb failed" in r.message]
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING
        # Must not land inside the series: the documented retrieval is
        # `journalctl … | grep host_pressure`, so a failure notice carrying the
        # record's own prefix would parse as a row with no fields.
        assert warnings[0].message.startswith("host_pressure_error ")
        assert _breadcrumb_lines(caplog) == []

    def test_a_failing_tmpfs_read_does_not_lose_the_line_silently(self, monkeypatch, caplog):
        _reset_warn_latch(monkeypatch)
        monkeypatch.setattr(host_pressure, "read_sample", lambda *a, **k: SAMPLE)

        def _boom(*_args, **_kwargs):
            raise OSError("mounts unreadable")

        monkeypatch.setattr(host_pressure, "read_tmpfs_usage", _boom)

        with caplog.at_level(logging.WARNING, logger="istota.scheduler.pressure"):
            _emit_host_pressure_breadcrumb()

        assert [r for r in caplog.records if "breadcrumb failed" in r.message]


class TestSchedulerConfigDefaults:
    def test_breadcrumb_is_on_by_default_at_five_minutes(self):
        """288 lines a day, which the existing logrotate config absorbs. Off by
        default would mean the next incident is as unattributable as the last."""
        sched = SchedulerConfig()
        assert sched.host_pressure_enabled is True
        assert sched.host_pressure_breadcrumb_interval_seconds == 300


def _loop_gate(sched: SchedulerConfig, now: float, last: float) -> bool:
    """The `run_daemon` emit condition, replicated.

    Mirrors `tests/test_scheduler_stats.py`'s handling of the sibling stats
    line: the tests above call the emitter directly and so never exercise the
    gate that decides whether it is called at all. Kept here so a refactor of
    the loop condition is caught rather than silently changing the cadence.
    """
    return bool(
        sched.host_pressure_enabled
        and sched.host_pressure_breadcrumb_interval_seconds
        and now - last >= sched.host_pressure_breadcrumb_interval_seconds
    )


class TestLoopGate:
    def test_disabled_suppresses_the_emit_entirely(self):
        """The spec's inertness proof: with this false the feature costs
        nothing and the daemon behaves exactly as it did before."""
        sched = SchedulerConfig(host_pressure_enabled=False)
        assert not _loop_gate(sched, now=1_000_000.0, last=0.0)

    def test_zero_interval_suppresses_the_emit(self):
        sched = SchedulerConfig(host_pressure_breadcrumb_interval_seconds=0)
        assert not _loop_gate(sched, now=1_000_000.0, last=0.0)

    def test_fires_on_the_first_tick(self):
        """`last_pressure_breadcrumb` inits to 0.0, not to now, so a restart
        records the post-restart baseline instead of discarding it."""
        sched = SchedulerConfig()
        assert _loop_gate(sched, now=1_000_000.0, last=0.0)

    def test_holds_until_the_interval_elapses(self):
        sched = SchedulerConfig()
        now = 1_000_000.0
        assert not _loop_gate(sched, now=now, last=now - 299)
        assert _loop_gate(sched, now=now, last=now - 300)
