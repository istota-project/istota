"""Off-thread periodic checks in the scheduler daemon loop (ISSUE-144 Tier 1).

The DB-health sweep and the DB-backup snapshot used to run synchronously on the
dispatch thread, wrapped in ``LoopWatchdog.suspended()`` so a healthy nightly run
didn't page. That blocked ``pool.dispatch()`` for their whole duration and left
the stall watchdog blind for two windows a day. Both now run on short-lived
daemon threads via ``_spawn_background_check``.
"""

from __future__ import annotations

import signal
import threading
from unittest.mock import patch

import pytest

from istota import db
from istota.config import (
    Config,
    SchedulerConfig,
    SecurityConfig,
    TalkConfig,
    UserConfig,
    WebConfig,
)
from istota.scheduler import _run_db_backup, _spawn_background_check


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
        users={"alice": UserConfig(display_name="Alice")},
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

    def test_db_checks_add_no_watchdog_suspension(self, tmp_path, monkeypatch):
        """The suspend wrappers are gone — the watchdog keeps full coverage.

        The sleep-cycle sites are still suspended (Tier 2), so the assertion is
        differential: run the loop once with the DB checks due and once with them
        not due, and require the suspend count to be identical. If either block
        still wrapped itself in ``suspended()`` the counts would diverge.
        """
        import istota.scheduler as sched

        def _one_pass(sub_path, *, due):
            cfg = _daemon_config(sub_path)
            # An interval far beyond epoch-seconds never comes due; 1s always does.
            interval = 1 if due else 10**12
            cfg.scheduler.db_health_check_interval = interval
            cfg.scheduler.db_backup_enabled = due
            cfg.scheduler.db_backup_interval = interval

            suspends: list[int] = []
            sweeps = threading.Event()
            real_suspended = sched.LoopWatchdog.suspended

            def _tracking_suspended(self):
                suspends.append(1)
                return real_suspended(self)

            monkeypatch.setattr(sched.LoopWatchdog, "suspended", _tracking_suspended)
            monkeypatch.setattr(
                sched, "check_db_health", lambda config: sweeps.set() or [],
            )
            monkeypatch.setattr(sched, "_run_db_backup", lambda config: None)
            monkeypatch.setattr(sched.WorkerPool, "shutdown", lambda self: None)

            t = _run_daemon_isolated(cfg, monkeypatch, lambda self: sched.request_shutdown())
            t.join(timeout=10.0)
            assert not t.is_alive()
            return len(suspends), sweeps

        due_suspends, due_sweeps = _one_pass(tmp_path / "due", due=True)
        # The sweep is spawned, not awaited — give the thread a moment to land.
        assert due_sweeps.wait(timeout=5.0), "health sweep never ran when due"

        sched._shutdown_requested = False
        idle_suspends, idle_sweeps = _one_pass(tmp_path / "idle", due=False)
        assert not idle_sweeps.wait(timeout=0.5), "health sweep ran when not due"

        assert due_suspends == idle_suspends, (
            "a DB check is still suspending the watchdog "
            f"(due={due_suspends}, not due={idle_suspends})"
        )
