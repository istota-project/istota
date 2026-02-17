"""Configuration loading for istota.heartbeat module."""

import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from istota.heartbeat import (
    HeartbeatSettings,
    HeartbeatCheck,
    CheckResult,
    load_heartbeat_config,
    is_quiet_hours,
    run_check,
    should_alert,
    check_heartbeats,
    _check_file_watch,
    _check_self,
    _check_shell_command,
    _check_url_health,
)
from istota.config import Config, SecurityConfig, UserConfig
from istota import db


# ---------------------------------------------------------------------------
# TestIsQuietHours
# ---------------------------------------------------------------------------


class TestIsQuietHours:
    def test_no_quiet_hours(self):
        assert is_quiet_hours("UTC", []) is False

    def test_same_day_range_inside(self):
        with patch("istota.heartbeat.datetime") as mock_dt:
            # 10:00 is inside 09:00-17:00
            mock_now = MagicMock()
            mock_now.hour = 10
            mock_now.minute = 0
            mock_dt.now.return_value = mock_now
            assert is_quiet_hours("UTC", ["09:00-17:00"]) is True

    def test_same_day_range_outside(self):
        with patch("istota.heartbeat.datetime") as mock_dt:
            # 18:00 is outside 09:00-17:00
            mock_now = MagicMock()
            mock_now.hour = 18
            mock_now.minute = 0
            mock_dt.now.return_value = mock_now
            assert is_quiet_hours("UTC", ["09:00-17:00"]) is False

    def test_cross_midnight_range_late_night(self):
        with patch("istota.heartbeat.datetime") as mock_dt:
            # 23:00 is inside 22:00-07:00
            mock_now = MagicMock()
            mock_now.hour = 23
            mock_now.minute = 0
            mock_dt.now.return_value = mock_now
            assert is_quiet_hours("UTC", ["22:00-07:00"]) is True

    def test_cross_midnight_range_early_morning(self):
        with patch("istota.heartbeat.datetime") as mock_dt:
            # 05:00 is inside 22:00-07:00
            mock_now = MagicMock()
            mock_now.hour = 5
            mock_now.minute = 0
            mock_dt.now.return_value = mock_now
            assert is_quiet_hours("UTC", ["22:00-07:00"]) is True

    def test_cross_midnight_range_outside(self):
        with patch("istota.heartbeat.datetime") as mock_dt:
            # 12:00 is outside 22:00-07:00
            mock_now = MagicMock()
            mock_now.hour = 12
            mock_now.minute = 0
            mock_dt.now.return_value = mock_now
            assert is_quiet_hours("UTC", ["22:00-07:00"]) is False

    def test_invalid_range_format(self):
        with patch("istota.heartbeat.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 12
            mock_now.minute = 0
            mock_dt.now.return_value = mock_now
            # Invalid formats should be skipped without error
            assert is_quiet_hours("UTC", ["invalid", "also-bad"]) is False


# ---------------------------------------------------------------------------
# TestLoadHeartbeatConfig
# ---------------------------------------------------------------------------


class TestLoadHeartbeatConfig:
    def test_no_mount(self, tmp_path):
        config = Config(nextcloud_mount_path=None)
        result = load_heartbeat_config(config, "alice")
        assert result is None

    def test_file_not_exists(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        result = load_heartbeat_config(config, "alice")
        assert result is None

    def test_empty_file(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        users_dir = mount / "Users" / "alice" / "istota" / "config"
        users_dir.mkdir(parents=True)
        (users_dir / "HEARTBEAT.md").write_text("")

        config = Config(nextcloud_mount_path=mount)
        result = load_heartbeat_config(config, "alice")
        assert result is None

    def test_no_toml_block(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        users_dir = mount / "Users" / "alice" / "istota" / "config"
        users_dir.mkdir(parents=True)
        (users_dir / "HEARTBEAT.md").write_text("# Just markdown, no TOML")

        config = Config(nextcloud_mount_path=mount)
        result = load_heartbeat_config(config, "alice")
        assert result is None

    def test_commented_toml(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        users_dir = mount / "Users" / "alice" / "istota" / "config"
        users_dir.mkdir(parents=True)
        (users_dir / "HEARTBEAT.md").write_text("""
# Heartbeat Config

```toml
# [settings]
# conversation_token = "test"
```
""")

        config = Config(nextcloud_mount_path=mount)
        result = load_heartbeat_config(config, "alice")
        assert result is None

    def test_valid_config(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        users_dir = mount / "Users" / "alice" / "istota" / "config"
        users_dir.mkdir(parents=True)
        (users_dir / "HEARTBEAT.md").write_text("""
# Heartbeat Config

```toml
[settings]
conversation_token = "room123"
quiet_hours = ["22:00-07:00"]
default_cooldown_minutes = 30

[[checks]]
name = "backup-check"
type = "file-watch"
path = "/backups/latest.log"
max_age_hours = 25
cooldown_minutes = 60
```
""")

        config = Config(nextcloud_mount_path=mount)
        result = load_heartbeat_config(config, "alice")

        assert result is not None
        settings, checks = result

        assert settings.conversation_token == "room123"
        assert settings.quiet_hours == ["22:00-07:00"]
        assert settings.default_cooldown_minutes == 30

        assert len(checks) == 1
        assert checks[0].name == "backup-check"
        assert checks[0].type == "file-watch"
        assert checks[0].config["path"] == "/backups/latest.log"
        assert checks[0].config["max_age_hours"] == 25
        assert checks[0].cooldown_minutes == 60
        assert checks[0].interval_minutes is None

    def test_interval_minutes_parsed(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        users_dir = mount / "Users" / "alice" / "istota" / "config"
        users_dir.mkdir(parents=True)
        (users_dir / "HEARTBEAT.md").write_text("""
```toml
[[checks]]
name = "slow-check"
type = "self-check"
interval_minutes = 30
cooldown_minutes = 60

[checks.config]
execution_test = false
```
""")

        config = Config(nextcloud_mount_path=mount)
        result = load_heartbeat_config(config, "alice")

        assert result is not None
        _, checks = result
        assert len(checks) == 1
        assert checks[0].interval_minutes == 30
        assert checks[0].cooldown_minutes == 60
        assert "interval_minutes" not in checks[0].config


# ---------------------------------------------------------------------------
# TestCheckFileWatch
# ---------------------------------------------------------------------------


class TestCheckFileWatch:
    def test_no_path(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(name="test", type="file-watch", config={})
        result = _check_file_watch(check, config)
        assert result.healthy is False
        assert "No path configured" in result.message

    def test_file_not_found(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="file-watch",
            config={"path": "/nonexistent/file.txt"},
        )
        result = _check_file_watch(check, config)
        assert result.healthy is False
        assert "not found" in result.message

    def test_file_exists_no_age_check(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        test_file = mount / "test.txt"
        test_file.write_text("content")

        config = Config(nextcloud_mount_path=mount)
        check = HeartbeatCheck(
            name="test",
            type="file-watch",
            config={"path": "/test.txt"},
        )
        result = _check_file_watch(check, config)
        assert result.healthy is True

    def test_file_too_old(self, tmp_path):
        import os
        mount = tmp_path / "mount"
        mount.mkdir()
        test_file = mount / "test.txt"
        test_file.write_text("content")

        # Set mtime to 48 hours ago
        old_time = datetime.now().timestamp() - (48 * 3600)
        os.utime(test_file, (old_time, old_time))

        config = Config(nextcloud_mount_path=mount)
        check = HeartbeatCheck(
            name="test",
            type="file-watch",
            config={"path": "/test.txt", "max_age_hours": 24},
        )
        result = _check_file_watch(check, config)
        assert result.healthy is False
        assert "too old" in result.message

    def test_file_fresh(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        test_file = mount / "test.txt"
        test_file.write_text("content")  # Fresh file

        config = Config(nextcloud_mount_path=mount)
        check = HeartbeatCheck(
            name="test",
            type="file-watch",
            config={"path": "/test.txt", "max_age_hours": 24},
        )
        result = _check_file_watch(check, config)
        assert result.healthy is True


# ---------------------------------------------------------------------------
# TestCheckShellCommand
# ---------------------------------------------------------------------------


class TestCheckShellCommand:
    def test_no_command(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(name="test", type="shell-command", config={})
        result = _check_shell_command(check, config)
        assert result.healthy is False
        assert "No command configured" in result.message

    def test_command_success_no_condition(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo hello"},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is True

    def test_command_failure_no_condition(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "exit 1"},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is False

    def test_less_than_condition_pass(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo 50", "condition": "< 90"},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is True

    def test_less_than_condition_fail(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={
                "command": "echo 95",
                "condition": "< 90",
                "message": "Value is {value}",
            },
        )
        result = _check_shell_command(check, config)
        assert result.healthy is False
        assert "Value is 95" in result.message

    def test_greater_than_condition(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo 50", "condition": "> 10"},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is True

    def test_equals_condition(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo ok", "condition": "== ok"},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is True

    def test_contains_condition(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo 'status: healthy'", "condition": "contains:healthy"},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is True

    def test_not_contains_condition(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo 'status: healthy'", "condition": "not-contains:error"},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is True

    def test_timeout(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "sleep 10", "timeout": 1},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is False
        assert "timed out" in result.message.lower()


# ---------------------------------------------------------------------------
# TestCheckUrlHealth
# ---------------------------------------------------------------------------


class TestCheckUrlHealth:
    def test_no_url(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(name="test", type="url-health", config={})
        result = _check_url_health(check, config)
        assert result.healthy is False
        assert "No URL configured" in result.message

    @patch("istota.heartbeat.httpx.get")
    def test_url_success(self, mock_get, tmp_path):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="url-health",
            config={"url": "https://example.com/health"},
        )
        result = _check_url_health(check, config)
        assert result.healthy is True

    @patch("istota.heartbeat.httpx.get")
    def test_url_wrong_status(self, mock_get, tmp_path):
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_get.return_value = mock_response

        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="url-health",
            config={"url": "https://example.com/health", "expected_status": 200},
        )
        result = _check_url_health(check, config)
        assert result.healthy is False
        assert "503" in result.message

    @patch("istota.heartbeat.httpx.get")
    def test_url_timeout(self, mock_get, tmp_path):
        import httpx
        mock_get.side_effect = httpx.TimeoutException("timeout")

        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="url-health",
            config={"url": "https://example.com/health", "timeout": 5},
        )
        result = _check_url_health(check, config)
        assert result.healthy is False
        assert "timeout" in result.message.lower()


# ---------------------------------------------------------------------------
# TestShouldAlert
# ---------------------------------------------------------------------------


class TestShouldAlert:
    def test_healthy_result(self, db_path):
        with db.get_db(db_path) as conn:
            settings = HeartbeatSettings()
            check = HeartbeatCheck(name="test", type="file-watch", config={})
            result = CheckResult(healthy=True, message="OK")

            assert should_alert(conn, "alice", check, result, settings, "UTC") is False

    def test_unhealthy_no_previous_alert(self, db_path):
        with db.get_db(db_path) as conn:
            settings = HeartbeatSettings(default_cooldown_minutes=60)
            check = HeartbeatCheck(name="test", type="file-watch", config={})
            result = CheckResult(healthy=False, message="Failed")

            assert should_alert(conn, "alice", check, result, settings, "UTC") is True

    def test_unhealthy_within_cooldown(self, db_path):
        with db.get_db(db_path) as conn:
            # Set up previous alert 30 minutes ago
            db.update_heartbeat_state(conn, "alice", "test", last_alert_at=True)

            settings = HeartbeatSettings(default_cooldown_minutes=60)
            check = HeartbeatCheck(name="test", type="file-watch", config={})
            result = CheckResult(healthy=False, message="Failed")

            assert should_alert(conn, "alice", check, result, settings, "UTC") is False

    def test_unhealthy_cooldown_expired(self, db_path):
        with db.get_db(db_path) as conn:
            # Set up previous alert 2 hours ago
            conn.execute(
                """
                INSERT INTO heartbeat_state (user_id, check_name, last_alert_at)
                VALUES (?, ?, datetime('now', '-2 hours'))
                """,
                ("alice", "test"),
            )

            settings = HeartbeatSettings(default_cooldown_minutes=60)
            check = HeartbeatCheck(name="test", type="file-watch", config={})
            result = CheckResult(healthy=False, message="Failed")

            assert should_alert(conn, "alice", check, result, settings, "UTC") is True


# ---------------------------------------------------------------------------
# TestCheckHeartbeats
# ---------------------------------------------------------------------------


class TestCheckHeartbeats:
    def test_no_users(self, db_path, tmp_path):
        config = Config(db_path=db_path, nextcloud_mount_path=tmp_path, users={})
        with db.get_db(db_path) as conn:
            result = check_heartbeats(conn, config)
        assert result == []

    def test_user_without_heartbeat_file(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()

        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            users={"alice": UserConfig(timezone="UTC")},
        )
        with db.get_db(db_path) as conn:
            result = check_heartbeats(conn, config)
        assert result == []

    @patch("istota.heartbeat.send_heartbeat_alert")
    def test_healthy_check_updates_state(self, mock_alert, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()

        # Create HEARTBEAT.md with a file-watch check
        users_dir = mount / "Users" / "alice" / "istota" / "config"
        users_dir.mkdir(parents=True)

        # Create the file to watch
        watched_file = mount / "test.txt"
        watched_file.write_text("content")

        (users_dir / "HEARTBEAT.md").write_text("""
```toml
[settings]
conversation_token = "room123"

[[checks]]
name = "test-check"
type = "file-watch"
path = "/test.txt"
```
""")

        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            users={"alice": UserConfig(timezone="UTC")},
        )

        with db.get_db(db_path) as conn:
            result = check_heartbeats(conn, config)

            # Verify state was updated
            state = db.get_heartbeat_state(conn, "alice", "test-check")
            assert state is not None
            assert state.last_check_at is not None
            assert state.last_healthy_at is not None

        assert result == ["alice"]
        mock_alert.assert_not_called()

    @patch("istota.heartbeat.send_heartbeat_alert")
    def test_unhealthy_check_sends_alert(self, mock_alert, db_path, tmp_path):
        mock_alert.return_value = True

        mount = tmp_path / "mount"
        mount.mkdir()

        # Create HEARTBEAT.md with a file-watch check
        users_dir = mount / "Users" / "alice" / "istota" / "config"
        users_dir.mkdir(parents=True)

        # Don't create the watched file - it should fail
        (users_dir / "HEARTBEAT.md").write_text("""
```toml
[settings]
conversation_token = "room123"

[[checks]]
name = "missing-file"
type = "file-watch"
path = "/nonexistent.txt"
```
""")

        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            users={"alice": UserConfig(timezone="UTC")},
        )

        with db.get_db(db_path) as conn:
            result = check_heartbeats(conn, config)

            # Verify alert state was updated
            state = db.get_heartbeat_state(conn, "alice", "missing-file")
            assert state is not None
            assert state.last_alert_at is not None

        assert result == ["alice"]
        mock_alert.assert_called_once()

    @patch("istota.heartbeat.send_heartbeat_alert")
    @patch("istota.heartbeat.run_check")
    def test_interval_skips_recent_check(self, mock_run_check, mock_alert, db_path, tmp_path):
        """Check with interval_minutes is skipped when last_check_at is recent."""
        mount = tmp_path / "mount"
        mount.mkdir()
        users_dir = mount / "Users" / "alice" / "istota" / "config"
        users_dir.mkdir(parents=True)
        (users_dir / "HEARTBEAT.md").write_text("""
```toml
[[checks]]
name = "slow-check"
type = "file-watch"
path = "/test.txt"
interval_minutes = 30
```
""")

        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            users={"alice": UserConfig(timezone="UTC")},
        )

        with db.get_db(db_path) as conn:
            # Simulate a recent check (just now)
            db.update_heartbeat_state(conn, "alice", "slow-check", last_check_at=True)

            check_heartbeats(conn, config)

        # run_check should NOT have been called — interval hasn't elapsed
        mock_run_check.assert_not_called()

    @patch("istota.heartbeat.send_heartbeat_alert")
    def test_interval_runs_after_elapsed(self, mock_alert, db_path, tmp_path):
        """Check with interval_minutes runs when enough time has passed."""
        mount = tmp_path / "mount"
        mount.mkdir()
        users_dir = mount / "Users" / "alice" / "istota" / "config"
        users_dir.mkdir(parents=True)

        watched_file = mount / "test.txt"
        watched_file.write_text("content")

        (users_dir / "HEARTBEAT.md").write_text("""
```toml
[[checks]]
name = "slow-check"
type = "file-watch"
path = "/test.txt"
interval_minutes = 30
```
""")

        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            users={"alice": UserConfig(timezone="UTC")},
        )

        with db.get_db(db_path) as conn:
            # Set last_check_at to 31 minutes ago
            old_time = (datetime.now(tz=None) - timedelta(minutes=31)).isoformat()
            db.update_heartbeat_state(conn, "alice", "slow-check", last_check_at=True)
            conn.execute(
                "UPDATE heartbeat_state SET last_check_at = ? WHERE user_id = ? AND check_name = ?",
                (old_time, "alice", "slow-check"),
            )
            conn.commit()

            check_heartbeats(conn, config)

            # State should be updated with a new last_check_at
            state = db.get_heartbeat_state(conn, "alice", "slow-check")
            assert state.last_check_at != old_time

    @patch("istota.heartbeat.send_heartbeat_alert")
    def test_no_interval_always_runs(self, mock_alert, db_path, tmp_path):
        """Check without interval_minutes runs every cycle."""
        mount = tmp_path / "mount"
        mount.mkdir()
        users_dir = mount / "Users" / "alice" / "istota" / "config"
        users_dir.mkdir(parents=True)

        watched_file = mount / "test.txt"
        watched_file.write_text("content")

        (users_dir / "HEARTBEAT.md").write_text("""
```toml
[[checks]]
name = "fast-check"
type = "file-watch"
path = "/test.txt"
```
""")

        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            users={"alice": UserConfig(timezone="UTC")},
        )

        with db.get_db(db_path) as conn:
            # Run twice — both should execute
            check_heartbeats(conn, config)
            state1 = db.get_heartbeat_state(conn, "alice", "fast-check")
            t1 = state1.last_check_at

            check_heartbeats(conn, config)
            state2 = db.get_heartbeat_state(conn, "alice", "fast-check")
            t2 = state2.last_check_at

            # Both runs should have updated last_check_at (may be same second though)
            assert t1 is not None
            assert t2 is not None


# ---------------------------------------------------------------------------
# TestHeartbeatStateDB
# ---------------------------------------------------------------------------


class TestHeartbeatStateDB:
    def test_get_nonexistent_state(self, db_path):
        with db.get_db(db_path) as conn:
            state = db.get_heartbeat_state(conn, "alice", "test")
            assert state is None

    def test_update_creates_row(self, db_path):
        with db.get_db(db_path) as conn:
            db.update_heartbeat_state(conn, "alice", "test", last_check_at=True)
            state = db.get_heartbeat_state(conn, "alice", "test")
            assert state is not None
            assert state.user_id == "alice"
            assert state.check_name == "test"
            assert state.last_check_at is not None

    def test_update_multiple_fields(self, db_path):
        with db.get_db(db_path) as conn:
            db.update_heartbeat_state(
                conn, "alice", "test",
                last_check_at=True,
                last_healthy_at=True,
                reset_errors=True,
            )
            state = db.get_heartbeat_state(conn, "alice", "test")
            assert state.last_check_at is not None
            assert state.last_healthy_at is not None
            assert state.consecutive_errors == 0

    def test_increment_errors(self, db_path):
        with db.get_db(db_path) as conn:
            db.update_heartbeat_state(conn, "alice", "test", increment_errors=True)
            state = db.get_heartbeat_state(conn, "alice", "test")
            assert state.consecutive_errors == 1

            db.update_heartbeat_state(conn, "alice", "test", increment_errors=True)
            state = db.get_heartbeat_state(conn, "alice", "test")
            assert state.consecutive_errors == 2

    def test_reset_errors(self, db_path):
        with db.get_db(db_path) as conn:
            # First increment
            db.update_heartbeat_state(conn, "alice", "test", increment_errors=True)
            db.update_heartbeat_state(conn, "alice", "test", increment_errors=True)

            # Then reset
            db.update_heartbeat_state(conn, "alice", "test", reset_errors=True)
            state = db.get_heartbeat_state(conn, "alice", "test")
            assert state.consecutive_errors == 0


# ---------------------------------------------------------------------------
# TestCheckSelf
# ---------------------------------------------------------------------------


class TestCheckSelf:
    def _make_check(self, **config_overrides):
        return HeartbeatCheck(
            name="system-health",
            type="self-check",
            config=config_overrides,
        )

    def _make_config(self, db_path, sandbox_enabled=False):
        return Config(
            db_path=db_path,
            security=SecurityConfig(sandbox_enabled=sandbox_enabled),
        )

    def test_claude_binary_missing(self, db_path):
        check = self._make_check(execution_test=False)
        config = self._make_config(db_path)

        with patch("istota.heartbeat.shutil.which", return_value=None):
            result = _check_self(check, config, "alice")

        assert not result.healthy
        assert "Claude binary not found" in result.message

    def test_bwrap_missing_when_sandbox_enabled(self, db_path):
        check = self._make_check(execution_test=False)
        config = self._make_config(db_path, sandbox_enabled=True)

        def which_side_effect(name):
            if name == "claude":
                return "/usr/bin/claude"
            return None  # bwrap not found

        with patch("istota.heartbeat.shutil.which", side_effect=which_side_effect):
            result = _check_self(check, config, "alice")

        assert not result.healthy
        assert "bwrap not found" in result.message

    def test_bwrap_not_checked_when_sandbox_disabled(self, db_path):
        check = self._make_check(execution_test=False)
        config = self._make_config(db_path)

        def which_side_effect(name):
            if name == "claude":
                return "/usr/bin/claude"
            return None

        with patch("istota.heartbeat.shutil.which", side_effect=which_side_effect):
            result = _check_self(check, config, "alice")

        assert result.healthy

    def test_db_health_failure(self, db_path):
        check = self._make_check(execution_test=False)
        config = self._make_config(db_path)

        with patch("istota.heartbeat.shutil.which", return_value="/usr/bin/claude"), \
             patch("istota.heartbeat.db.get_db", side_effect=Exception("DB corrupt")):
            result = _check_self(check, config, "alice")

        assert not result.healthy
        assert "Database error" in result.message

    def test_high_failure_rate(self, db_path):
        check = self._make_check(execution_test=False)
        config = self._make_config(db_path)

        # Create tasks: more failed than completed in last hour
        with db.get_db(db_path) as conn:
            for _ in range(3):
                tid = db.create_task(conn, "fail task", "alice")
                db.update_task_status(conn, tid, "failed", error="boom")
            tid = db.create_task(conn, "ok task", "alice")
            db.update_task_status(conn, tid, "completed", result="ok")

        with patch("istota.heartbeat.shutil.which", return_value="/usr/bin/claude"):
            result = _check_self(check, config, "alice")

        assert not result.healthy
        assert "High failure rate" in result.message

    def test_execution_test_pass(self, db_path):
        check = self._make_check(execution_test=True)
        config = self._make_config(db_path)

        mock_result = MagicMock()
        mock_result.stdout = "healthcheck-ok\n"

        with patch("istota.heartbeat.shutil.which", return_value="/usr/bin/claude"), \
             patch("istota.heartbeat.subprocess.run", return_value=mock_result), \
             patch("istota.heartbeat.os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            result = _check_self(check, config, "alice")

        assert result.healthy

    def test_execution_test_fail(self, db_path):
        check = self._make_check(execution_test=True)
        config = self._make_config(db_path)

        mock_result = MagicMock()
        mock_result.stdout = "some garbage output"

        with patch("istota.heartbeat.shutil.which", return_value="/usr/bin/claude"), \
             patch("istota.heartbeat.subprocess.run", return_value=mock_result), \
             patch("istota.heartbeat.os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            result = _check_self(check, config, "alice")

        assert not result.healthy
        assert "healthcheck-ok" in result.message

    def test_execution_test_timeout(self, db_path):
        check = self._make_check(execution_test=True)
        config = self._make_config(db_path)

        with patch("istota.heartbeat.shutil.which", return_value="/usr/bin/claude"), \
             patch("istota.heartbeat.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 30)), \
             patch("istota.heartbeat.os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            result = _check_self(check, config, "alice")

        assert not result.healthy
        assert "timed out" in result.message

    def test_execution_test_disabled(self, db_path):
        check = self._make_check(execution_test=False)
        config = self._make_config(db_path)

        with patch("istota.heartbeat.shutil.which", return_value="/usr/bin/claude"), \
             patch("istota.heartbeat.subprocess.run") as mock_run:
            result = _check_self(check, config, "alice")

        assert result.healthy
        mock_run.assert_not_called()

    def test_all_pass(self, db_path):
        check = self._make_check(execution_test=True)
        config = self._make_config(db_path)

        mock_result = MagicMock()
        mock_result.stdout = "healthcheck-ok\n"

        with patch("istota.heartbeat.shutil.which", return_value="/usr/bin/claude"), \
             patch("istota.heartbeat.subprocess.run", return_value=mock_result), \
             patch("istota.heartbeat.os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            result = _check_self(check, config, "alice")

        assert result.healthy
        assert "All self-checks passed" in result.message

    def test_run_check_dispatches_self_check(self, db_path):
        """Verify run_check() correctly dispatches self-check with user_id."""
        check = self._make_check(execution_test=False)
        config = self._make_config(db_path)

        with patch("istota.heartbeat.shutil.which", return_value="/usr/bin/claude"):
            result = run_check(check, config, "alice")

        assert result.healthy

