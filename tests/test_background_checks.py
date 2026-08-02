"""Off-thread periodic checks in the scheduler daemon loop (ISSUE-144).

The DB-health sweep, the DB-backup snapshot (Tier 1) and the two nightly
sleep-cycle passes (Tier 2) all used to run synchronously on the dispatch
thread, wrapped in ``LoopWatchdog.suspended()`` so a healthy nightly run didn't
page. That blocked ``pool.dispatch()`` for their whole duration and left the
stall watchdog blind for those windows. All of them now run on short-lived
daemon threads via ``_spawn_background_check``, and no ``suspended()`` call site
remains in ``run_daemon``.
"""

from __future__ import annotations

import signal
import threading
from unittest.mock import patch

import pytest

from istota import db
from istota.config import (
    ChannelSleepCycleConfig,
    Config,
    SchedulerConfig,
    SecurityConfig,
    SleepCycleConfig,
    TalkConfig,
    UserConfig,
    WebConfig,
)
from istota.scheduler import (
    _run_db_backup,
    _run_sleep_cycles,
    _spawn_background_check,
)


@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    import istota.scheduler as sched

    sched._shutdown_requested = False
    yield
    sched._shutdown_requested = False


# ---------------------------------------------------------------------------
# _spawn_background_check
# ---------------------------------------------------------------------------


class TestSpawnBackgroundCheck:
    def test_runs_fn_off_the_caller_thread(self):
        inflight: dict[str, threading.Thread] = {}
        seen: list[int] = []
        done = threading.Event()

        def _fn():
            seen.append(threading.get_ident())
            done.set()

        assert _spawn_background_check("probe", _fn, inflight) is True
        assert done.wait(timeout=5.0)
        assert seen and seen[0] != threading.get_ident()

    def test_thread_is_named_and_daemon(self):
        inflight: dict[str, threading.Thread] = {}
        release = threading.Event()
        started = threading.Event()

        def _fn():
            started.set()
            release.wait(timeout=5.0)

        _spawn_background_check("db-health", _fn, inflight)
        assert started.wait(timeout=5.0)
        thread = inflight["db-health"]
        assert thread.daemon is True
        assert "db-health" in thread.name
        release.set()
        thread.join(timeout=5.0)

    def test_skips_while_previous_still_running(self):
        inflight: dict[str, threading.Thread] = {}
        calls: list[int] = []
        release = threading.Event()
        started = threading.Event()

        def _fn():
            calls.append(1)
            started.set()
            release.wait(timeout=5.0)

        assert _spawn_background_check("db-backup", _fn, inflight) is True
        assert started.wait(timeout=5.0)

        # Second tick while the first is still in flight: no overlap.
        assert _spawn_background_check("db-backup", _fn, inflight) is False
        assert len(calls) == 1

        release.set()
        inflight["db-backup"].join(timeout=5.0)

    def test_respawns_once_previous_finished(self):
        inflight: dict[str, threading.Thread] = {}
        calls: list[int] = []
        done = threading.Event()

        def _fn():
            calls.append(1)
            done.set()

        assert _spawn_background_check("db-health", _fn, inflight) is True
        assert done.wait(timeout=5.0)
        inflight["db-health"].join(timeout=5.0)

        done.clear()
        assert _spawn_background_check("db-health", _fn, inflight) is True
        assert done.wait(timeout=5.0)
        inflight["db-health"].join(timeout=5.0)
        assert len(calls) == 2

    def test_contains_exception_from_fn(self):
        inflight: dict[str, threading.Thread] = {}

        def _boom():
            raise RuntimeError("sweep exploded")

        assert _spawn_background_check("db-health", _boom, inflight) is True
        inflight["db-health"].join(timeout=5.0)
        assert not inflight["db-health"].is_alive()

        # A crashed run must not wedge the slot — the next tick spawns again.
        done = threading.Event()
        assert _spawn_background_check("db-health", done.set, inflight) is True
        assert done.wait(timeout=5.0)

    def test_skip_logs_warning_by_default(self, caplog):
        """An unexpected overrun (the DB checks) stays a WARNING."""
        inflight: dict[str, threading.Thread] = {}
        release = threading.Event()
        started = threading.Event()

        def _fn():
            started.set()
            release.wait(timeout=5.0)

        _spawn_background_check("db-health", _fn, inflight)
        assert started.wait(timeout=5.0)
        with caplog.at_level("DEBUG", logger="istota.scheduler"):
            assert _spawn_background_check("db-health", _fn, inflight) is False
        release.set()
        inflight["db-health"].join(timeout=5.0)

        records = [r for r in caplog.records if "background_check_still_running" in r.message]
        assert records and records[0].levelname == "WARNING"

    def test_skip_logs_debug_when_overlap_expected(self, caplog):
        """The sleep cycles are polled far more often than they run.

        A nightly pass spanning several 60s poll ticks is by design, so the skip
        must not read as a warning-worthy overrun.
        """
        inflight: dict[str, threading.Thread] = {}
        release = threading.Event()
        started = threading.Event()

        def _fn():
            started.set()
            release.wait(timeout=5.0)

        _spawn_background_check("sleep-cycles", _fn, inflight, overlap_expected=True)
        assert started.wait(timeout=5.0)
        with caplog.at_level("DEBUG", logger="istota.scheduler"):
            assert _spawn_background_check(
                "sleep-cycles", _fn, inflight, overlap_expected=True,
            ) is False
        release.set()
        inflight["sleep-cycles"].join(timeout=5.0)

        records = [r for r in caplog.records if "background_check_still_running" in r.message]
        assert records and records[0].levelname == "DEBUG"


# ---------------------------------------------------------------------------
# _run_sleep_cycles (per-user + per-channel, as one off-thread unit)
# ---------------------------------------------------------------------------


class TestRunSleepCycles:
    def _config(self, tmp_path):
        cfg = Config(
            db_path=tmp_path / "istota.db",
            users={"alice": UserConfig()},
            sleep_cycle=SleepCycleConfig(),
            channel_sleep_cycle=ChannelSleepCycleConfig(),
        )
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        db.init_db(cfg.db_path)
        return cfg

    def test_runs_both_passes_each_with_its_own_connection(self, tmp_path):
        """Neither half borrows a caller connection, and both run in order."""
        cfg = self._config(tmp_path)
        order: list[str] = []
        conns: list[object] = []

        def _users(conn, config):
            order.append("users")
            conns.append(conn)
            return ["alice"]

        def _channels(conn, config):
            order.append("channels")
            conns.append(conn)
            return ["tok"]

        with patch("istota.memory.sleep_cycle.check_sleep_cycles", _users), \
                patch("istota.memory.sleep_cycle.check_channel_sleep_cycles", _channels):
            _run_sleep_cycles(cfg)

        assert order == ["users", "channels"]
        assert len(conns) == 2 and conns[0] is not conns[1]

    def test_channel_pass_runs_even_if_user_pass_raises(self, tmp_path):
        cfg = self._config(tmp_path)
        ran = threading.Event()

        def _boom(conn, config):
            raise RuntimeError("extraction exploded")

        with patch("istota.memory.sleep_cycle.check_sleep_cycles", _boom), \
                patch(
                    "istota.memory.sleep_cycle.check_channel_sleep_cycles",
                    lambda conn, config: ran.set() or [],
                ):
            _run_sleep_cycles(cfg)  # must not raise

        assert ran.is_set()

    def test_channel_pass_failure_is_contained(self, tmp_path):
        """A crash in the last half must not escape onto the background thread."""
        cfg = self._config(tmp_path)

        def _boom(conn, config):
            raise RuntimeError("channel extraction exploded")

        with patch(
            "istota.memory.sleep_cycle.check_sleep_cycles",
            lambda conn, config: [],
        ), patch("istota.memory.sleep_cycle.check_channel_sleep_cycles", _boom):
            _run_sleep_cycles(cfg)  # must not raise


# ---------------------------------------------------------------------------
# _run_db_backup (snapshot + problem alert, as one off-thread unit)
# ---------------------------------------------------------------------------


class TestRunDbBackup:
    def _config(self):
        return Config(users={"alice": UserConfig()}, admin_users={"alice"})

    def test_alerts_with_backup_results(self):
        cfg = self._config()
        results = [{"label": "money:alice", "status": "error", "error": "disk full"}]
        with patch("istota.db_backup.backup_databases", return_value=results) as backup, \
                patch("istota.scheduler._alert_backup_problems") as alert:
            _run_db_backup(cfg)
        backup.assert_called_once_with(cfg)
        alert.assert_called_once_with(cfg, results)

    def test_alert_runs_even_for_clean_results(self):
        cfg = self._config()
        with patch("istota.db_backup.backup_databases", return_value=[]), \
                patch("istota.scheduler._alert_backup_problems") as alert:
            _run_db_backup(cfg)
        alert.assert_called_once_with(cfg, [])


# ---------------------------------------------------------------------------
# Daemon-loop integration: neither check blocks dispatch
# ---------------------------------------------------------------------------


def _daemon_config(tmp_path):
    cfg = Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "workspace",
        users={"bob": UserConfig(display_name="Bob")},
        talk=TalkConfig(enabled=False),
        security=SecurityConfig(sandbox_enabled=False),
        web=WebConfig(enabled=False, auth="none"),
        scheduler=SchedulerConfig(
            db_health_check_interval=1,
            db_backup_enabled=False,
            loop_stall_alert_seconds=0,
        ),
    )
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(cfg.db_path)
    return cfg


def _run_daemon_isolated(cfg, monkeypatch, dispatch):
    """Drive run_daemon on a thread with external subsystems neutralized."""
    import istota.async_runtime as ar
    import istota.scheduler as sched
    import istota.status_writer as sw

    monkeypatch.setattr(sched, "DAEMON_LOCK_PATH", cfg.db_path.parent / "daemon.lock")
    monkeypatch.setattr(ar, "get_async_runtime", lambda: None)
    monkeypatch.setattr(sched, "reset_async_runtime", lambda: None)
    monkeypatch.setattr(sw, "init_status_writer", lambda *a, **k: None)
    monkeypatch.setattr(sw, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(signal, "signal", lambda sig, handler: None)
    monkeypatch.setattr(sched.WorkerPool, "dispatch", dispatch)

    ready = threading.Event()
    t = threading.Thread(
        target=lambda: sched.run_daemon(
            cfg, install_signal_handlers=False, ready_event=ready
        ),
        daemon=True,
    )
    t.start()
    assert ready.wait(timeout=10.0), "ready_event never set"
    return t


class TestDaemonLoopNotBlocked:
    def test_db_health_sweep_does_not_block_dispatch(self, tmp_path, monkeypatch):
        """A wedged health sweep must not starve pool.dispatch()."""
        import istota.scheduler as sched

        cfg = _daemon_config(tmp_path)
        sweep_started = threading.Event()
        release = threading.Event()
        dispatches = []

        def _slow_sweep(config):
            sweep_started.set()
            release.wait(timeout=10.0)
            return []

        def _dispatch(self):
            dispatches.append(1)
            # Keep ticking until the sweep is demonstrably in flight, then a few
            # more times to prove dispatch is still alive while it hangs.
            if sweep_started.is_set() and len(dispatches) > 3:
                sched.request_shutdown()

        monkeypatch.setattr(sched, "check_db_health", _slow_sweep)
        t = _run_daemon_isolated(cfg, monkeypatch, _dispatch)
        try:
            assert sweep_started.wait(timeout=10.0), "sweep never ran"
            t.join(timeout=10.0)
            assert not t.is_alive(), "daemon loop was blocked by the health sweep"
            assert len(dispatches) > 3
        finally:
            release.set()
            sched.request_shutdown()
            t.join(timeout=10.0)

    def test_sleep_cycle_does_not_block_dispatch(self, tmp_path, monkeypatch):
        """A wedged nightly sleep cycle must not starve pool.dispatch() (Tier 2)."""
        import istota.scheduler as sched

        cfg = _daemon_config(tmp_path)
        # Keep the DB checks out of the way; this test is about the sleep cycle.
        cfg.scheduler.db_health_check_interval = 10**12
        started = threading.Event()
        release = threading.Event()
        runs: list[int] = []
        dispatches: list[int] = []

        def _wedged(config):
            runs.append(1)
            started.set()
            release.wait(timeout=10.0)

        def _dispatch(self):
            dispatches.append(1)
            # Keep ticking until the pass is demonstrably in flight, then a few
            # more times to prove dispatch is still alive while it hangs.
            if started.is_set() and len(dispatches) > 3:
                sched.request_shutdown()

        monkeypatch.setattr(sched, "_run_sleep_cycles", _wedged)
        t = _run_daemon_isolated(cfg, monkeypatch, _dispatch)
        try:
            assert started.wait(timeout=10.0), "sleep cycle never ran"
            t.join(timeout=10.0)
            assert not t.is_alive(), "daemon loop was blocked by the sleep cycle"
            assert len(dispatches) > 3
            # The in-flight guard, not the poll clock, prevents a re-fire: the
            # loop polled the cron several times while the pass was wedged.
            assert len(runs) == 1, f"sleep cycle re-fired while in flight: {len(runs)}"
        finally:
            release.set()
            sched.request_shutdown()
            t.join(timeout=10.0)

    def test_no_check_suspends_the_watchdog(self, tmp_path, monkeypatch):
        """The suspend wrappers are all gone — the watchdog keeps full coverage.

        With Tier 2 done there is no known-long synchronous check left, so this
        is an absolute assertion rather than the differential one Tier 1 needed:
        driving the loop with every off-thread check due must not suspend the
        watchdog even once.
        """
        import istota.scheduler as sched

        cfg = _daemon_config(tmp_path)
        cfg.scheduler.db_health_check_interval = 1
        cfg.scheduler.db_backup_enabled = True
        cfg.scheduler.db_backup_interval = 1

        suspends: list[int] = []
        sweeps = threading.Event()
        backups = threading.Event()
        sleeps = threading.Event()
        real_suspended = sched.LoopWatchdog.suspended

        def _tracking_suspended(self):
            suspends.append(1)
            return real_suspended(self)

        monkeypatch.setattr(sched.LoopWatchdog, "suspended", _tracking_suspended)
        monkeypatch.setattr(sched, "check_db_health", lambda config: sweeps.set() or [])
        monkeypatch.setattr(sched, "_run_db_backup", lambda config: backups.set())
        monkeypatch.setattr(sched, "_run_sleep_cycles", lambda config: sleeps.set())
        monkeypatch.setattr(sched.WorkerPool, "shutdown", lambda self: None)

        t = _run_daemon_isolated(
            cfg, monkeypatch, lambda self: sched.request_shutdown()
        )
        t.join(timeout=10.0)
        assert not t.is_alive()

        # All three are spawned, not awaited — give the threads a moment to land.
        assert sweeps.wait(timeout=5.0), "health sweep never ran"
        assert backups.wait(timeout=5.0), "backup never ran"
        assert sleeps.wait(timeout=5.0), "sleep cycle never ran"
        assert suspends == [], f"a check is still suspending the watchdog ({len(suspends)})"
