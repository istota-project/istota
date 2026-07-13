"""Tests for the scheduler's DB-backup problem alert (ISSUE-159 issue #6)."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

from istota.config import Config, SchedulerConfig, UserConfig
from istota.scheduler import (
    _alert_backup_problems,
    _maybe_alert_backup_stale,
    _send_operator_alert,
)


class TestAlertBackupProblems:
    def _config(self):
        return Config(users={"alice": UserConfig()}, admin_users={"alice"})

    def test_no_alert_when_all_ok(self):
        cfg = self._config()
        results = [
            {"label": "framework", "status": "ok"},
            {"label": "location:alice", "status": "skip_missing"},
        ]
        with patch("istota.scheduler.send_notification") as notify:
            _alert_backup_problems(cfg, results)
        notify.assert_not_called()

    def test_alerts_on_suspect(self):
        cfg = self._config()
        results = [
            {"label": "framework", "status": "ok"},
            {"label": "location:alice", "status": "suspect", "prior_rows": 10, "new_rows": 0},
        ]
        with patch("istota.scheduler.send_notification") as notify:
            _alert_backup_problems(cfg, results)
        notify.assert_called_once()
        args, kwargs = notify.call_args
        assert args[1] == "alice"  # operator user
        assert "location:alice" in args[2]
        assert kwargs.get("purpose") == "alert"

    def test_alerts_on_error(self):
        cfg = self._config()
        results = [{"label": "money:alice", "status": "error", "error": "disk full"}]
        with patch("istota.scheduler.send_notification") as notify:
            _alert_backup_problems(cfg, results)
        notify.assert_called_once()

    def test_no_alert_when_no_users(self):
        cfg = Config(users={})
        results = [{"label": "framework", "status": "error"}]
        with patch("istota.scheduler.send_notification") as notify:
            _alert_backup_problems(cfg, results)
        notify.assert_not_called()

    def test_swallows_notification_failure(self):
        cfg = self._config()
        results = [{"label": "framework", "status": "error"}]
        with patch("istota.scheduler.send_notification", side_effect=Exception("no channel")):
            # Must not raise into the loop.
            _alert_backup_problems(cfg, results)


class TestSendOperatorAlertBounded:
    def _config(self):
        return Config(users={"alice": UserConfig()}, admin_users={"alice"})

    def test_delivers_when_send_is_fast(self):
        cfg = self._config()
        with patch("istota.scheduler.send_notification") as notify:
            _send_operator_alert(cfg, "alice", "hi")
        notify.assert_called_once()

    def test_returns_before_a_hung_send_completes(self):
        cfg = self._config()
        release = threading.Event()

        def _hang(*a, **k):
            release.wait(5)  # simulate a wedged Talk delivery

        started = time.monotonic()
        with patch("istota.scheduler.send_notification", side_effect=_hang):
            _send_operator_alert(cfg, "alice", "hi", timeout=0.2)
        elapsed = time.monotonic() - started
        release.set()  # let the background thread finish
        assert elapsed < 2.0  # did not block on the full 5s hang


class TestMaybeAlertBackupStale:
    def _config(self):
        return Config(
            users={"alice": UserConfig()},
            admin_users={"alice"},
            scheduler=SchedulerConfig(db_backup_enabled=True, db_backup_interval=86400),
        )

    def test_fires_once_when_stale(self):
        cfg = self._config()
        now = 1_000_000.0
        persisted = now - 3 * 86400  # 3 days old (> 2x interval)
        with patch("istota.scheduler.send_notification") as notify:
            armed = _maybe_alert_backup_stale(cfg, now, persisted, already_alerted=False)
            assert armed is True
            notify.assert_called_once()
            # Second call while still stale does NOT re-alert.
            armed2 = _maybe_alert_backup_stale(cfg, now, persisted, already_alerted=armed)
            assert armed2 is True
            notify.assert_called_once()

    def test_gated_on_prior_run(self):
        # persisted == 0 (never backed up) must not false-alarm on a fresh deploy.
        cfg = self._config()
        with patch("istota.scheduler.send_notification") as notify:
            armed = _maybe_alert_backup_stale(cfg, 1_000_000.0, 0.0, already_alerted=False)
        assert armed is False
        notify.assert_not_called()

    def test_not_stale_when_recent(self):
        cfg = self._config()
        now = 1_000_000.0
        persisted = now - 3600  # 1h old, well within interval
        with patch("istota.scheduler.send_notification") as notify:
            armed = _maybe_alert_backup_stale(cfg, now, persisted, already_alerted=False)
        assert armed is False
        notify.assert_not_called()

    def test_rearms_on_recovery(self):
        cfg = self._config()
        now = 1_000_000.0
        stale = now - 3 * 86400
        with patch("istota.scheduler.send_notification"):
            armed = _maybe_alert_backup_stale(cfg, now, stale, already_alerted=True)
            assert armed is True
            # Backup recovers (fresh persisted) -> disarm.
            armed = _maybe_alert_backup_stale(cfg, now, now - 100, already_alerted=armed)
            assert armed is False

    def test_disabled_backup_never_alerts(self):
        cfg = Config(
            users={"alice": UserConfig()},
            admin_users={"alice"},
            scheduler=SchedulerConfig(db_backup_enabled=False, db_backup_interval=86400),
        )
        with patch("istota.scheduler.send_notification") as notify:
            armed = _maybe_alert_backup_stale(cfg, 1_000_000.0, 1.0, already_alerted=False)
        assert armed is False
        notify.assert_not_called()
