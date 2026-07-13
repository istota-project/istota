"""Tests for the scheduler's DB-backup problem alert (ISSUE-159 issue #6)."""

from __future__ import annotations

from unittest.mock import patch

from istota.config import Config, UserConfig
from istota.scheduler import _alert_backup_problems


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
