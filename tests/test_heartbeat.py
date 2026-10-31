"""Configuration loading for istota.heartbeat module."""

import http.server
import os
import threading
from datetime import datetime, timedelta, timezone
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
from istota.config import Config, NextcloudConfig, SecurityConfig, UserConfig, load_config
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
        config = Config(workspace_path=None)
        result = load_heartbeat_config(config, "alice")
        assert result is None

    def test_file_not_exists(self, tmp_path):
        config = Config(workspace_path=tmp_path)
        result = load_heartbeat_config(config, "alice")
        assert result is None

    def test_empty_file(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        users_dir = mount / "Users" / "alice" / "istota" / "config"
        users_dir.mkdir(parents=True)
        (users_dir / "HEARTBEAT.md").write_text("")

        config = Config(workspace_path=mount)
        result = load_heartbeat_config(config, "alice")
        assert result is None

    def test_no_toml_block(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        users_dir = mount / "Users" / "alice" / "istota" / "config"
        users_dir.mkdir(parents=True)
        (users_dir / "HEARTBEAT.md").write_text("# Just markdown, no TOML")

        config = Config(workspace_path=mount)
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

        config = Config(workspace_path=mount)
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

        config = Config(workspace_path=mount)
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
execution_test = false
```
""")

        config = Config(workspace_path=mount)
        result = load_heartbeat_config(config, "alice")

        assert result is not None
        _, checks = result
        assert len(checks) == 1
        assert checks[0].interval_minutes == 30
        assert checks[0].cooldown_minutes == 60
        assert "interval_minutes" not in checks[0].config
        assert checks[0].config["execution_test"] is False


# ---------------------------------------------------------------------------
# TestCheckFileWatch
# ---------------------------------------------------------------------------


class TestCheckFileWatch:
    def _mount(self, tmp_path):
        mount = tmp_path / "mount"
        (mount / "Users/alice").mkdir(parents=True)
        return mount

    def _check(self, config, path, **extra):
        check = HeartbeatCheck(
            name="test", type="file-watch", config={"path": path, **extra},
        )
        return _check_file_watch(check, config, "alice")

    def test_no_path(self, tmp_path):
        config = Config(workspace_path=tmp_path)
        check = HeartbeatCheck(name="test", type="file-watch", config={})
        result = _check_file_watch(check, config, "alice")
        assert result.healthy is False
        assert "No path configured" in result.message

    def test_file_not_found(self, tmp_path):
        config = Config(workspace_path=self._mount(tmp_path))
        result = self._check(config, "/Users/alice/nonexistent.txt")
        assert result.healthy is False
        assert "not found" in result.message

    def test_file_exists_no_age_check(self, tmp_path):
        mount = self._mount(tmp_path)
        (mount / "Users/alice/test.txt").write_text("content")
        config = Config(workspace_path=mount)
        assert self._check(config, "/Users/alice/test.txt").healthy is True

    def test_file_too_old(self, tmp_path):
        import os
        mount = self._mount(tmp_path)
        test_file = mount / "Users/alice/test.txt"
        test_file.write_text("content")
        old_time = datetime.now().timestamp() - (48 * 3600)
        os.utime(test_file, (old_time, old_time))

        config = Config(workspace_path=mount)
        result = self._check(config, "/Users/alice/test.txt", max_age_hours=24)
        assert result.healthy is False
        assert "too old" in result.message

    def test_file_fresh(self, tmp_path):
        mount = self._mount(tmp_path)
        (mount / "Users/alice/test.txt").write_text("content")
        config = Config(workspace_path=mount)
        result = self._check(config, "/Users/alice/test.txt", max_age_hours=24)
        assert result.healthy is True

    # ISSUE-596: the daemon answers existence and age, so the path is scoped
    # to the owner's own tree exactly as a CRON.md prompt_file is.

    def test_another_users_file_reads_as_not_found(self, tmp_path):
        mount = self._mount(tmp_path)
        (mount / "Users/bob").mkdir(parents=True)
        (mount / "Users/bob/diary.txt").write_text("x")
        config = Config(workspace_path=mount)
        result = self._check(config, "/Users/bob/diary.txt")
        assert result.healthy is False
        assert "not found" in result.message

    def test_dotdot_and_mount_root_paths_read_as_not_found(self, tmp_path):
        mount = self._mount(tmp_path)
        (mount / "Users/bob").mkdir(parents=True)
        (mount / "Users/bob/diary.txt").write_text("x")
        (mount / "top.txt").write_text("x")
        (tmp_path / "outside.txt").write_text("x")
        config = Config(workspace_path=mount)
        for path in (
            "/Users/alice/../bob/diary.txt",
            "/Users/alice/../../outside.txt",
            "/top.txt",
        ):
            assert self._check(config, path).healthy is False, path

    def test_symlinks_are_not_followed(self, tmp_path):
        mount = self._mount(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "f.txt").write_text("x")
        (mount / "Users/alice/leaf.txt").symlink_to(outside / "f.txt")
        (mount / "Users/alice/dir").symlink_to(outside)
        config = Config(workspace_path=mount)
        assert self._check(config, "/Users/alice/leaf.txt").healthy is False
        assert self._check(config, "/Users/alice/dir/f.txt").healthy is False


# ---------------------------------------------------------------------------
# TestCheckShellCommand
# ---------------------------------------------------------------------------


class TestCheckShellCommand:
    def test_no_command(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(name="test", type="shell-command", config={})
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is False
        assert "No command configured" in result.message

    def test_command_success_no_condition(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo hello"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True

    def test_experimental_features_propagated(self, tmp_path):
        """Heartbeat shell-command checks must propagate
        ISTOTA_EXPERIMENTAL_FEATURES so any heartbeat invoking a gated CLI
        gets the same view of enabled flags as the scheduler subprocess
        paths."""
        from istota.config import ExperimentalConfig
        config = Config(
            db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp",
            experimental=ExperimentalConfig(features=["money_tax", "money_wash_sales"]),
        )
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={
                "command": "echo flags=[$ISTOTA_EXPERIMENTAL_FEATURES]",
                "condition": "contains:money_tax,money_wash_sales",
            },
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True

    def test_command_failure_no_condition(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "exit 1"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is False

    def test_a_failing_stage_of_a_pipeline_is_unhealthy(self, tmp_path):
        """`shell=True` is `/bin/sh -c`, which starts with `pipefail` off, so a
        probe ending in a pipe reported the last stage and a broken check read
        as healthy forever. The counterpart of ISSUE-307 on this surface."""
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "false | tail -1"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is False

    def test_a_succeeding_pipeline_is_still_healthy(self, tmp_path):
        """Control — a piped probe that works must not start alerting.

        Deliberately carries no `condition`: `_check_shell_command` only reads
        `result.returncode` in the `if not condition:` branch, so a control with
        one would decide health from stdout alone and pass identically against
        the pre-pipefail code — proving nothing about the change it guards.
        """
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo hi | tail -1"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True

    def test_a_sigpipe_probe_says_what_141_means(self, tmp_path):
        """The message reaches an operator through an alert, with no stderr
        beside it — a SIGPIPE'd producer writes none. A bare `exit 141` on a
        correct probe is the kind of thing someone debugs at 3am."""
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "yes | head -1"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is False
        assert "141" in result.message
        assert "SIGPIPE" in result.message, result.message

    def test_less_than_condition_pass(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo 50", "condition": "< 90"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True

    def test_less_than_condition_fail(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={
                "command": "echo 95",
                "condition": "< 90",
                "message": "Value is {value}",
            },
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is False
        assert "Value is 95" in result.message

    def test_greater_than_condition(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo 50", "condition": "> 10"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True

    def test_equals_condition(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo ok", "condition": "== ok"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True

    def test_contains_condition(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo 'status: healthy'", "condition": "contains:healthy"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True

    def test_not_contains_condition(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo 'status: healthy'", "condition": "not-contains:error"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True

    def test_timeout(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "sleep 10", "timeout": 0.2},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is False
        assert "timed out" in result.message.lower()

    def test_config_path_propagated(self, tmp_path):
        """Module-skill subprocesses (e.g. `istota-skill feeds`) load the
        daemon's config from ISTOTA_CONFIG_PATH. Without it they fall back
        to a default Config() with empty users and exit with a JSON error
        envelope, while the shell exit 0 makes the heartbeat look healthy."""
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        config.config_path = tmp_path / "config.toml"
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo $ISTOTA_CONFIG_PATH"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True
        assert str(config.config_path) in result.details["value"]

    def test_user_id_propagated(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo $ISTOTA_USER_ID"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True
        assert "alice" in result.details["value"]

    def test_db_path_stays_out_of_the_shell(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo $ISTOTA_DB_PATH"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True
        assert str(config.db_path) not in result.details["value"]

    def test_secret_file_variable_not_propagated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS", '{"X-Key": "header-value"}')
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo headers=[$ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS]"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True
        assert "headers=[]" in result.details["value"]

    def test_json_error_envelope_marks_unhealthy(self, tmp_path):
        """ISSUE-097-shaped silent-rot: istota-skill CLIs emit
        ``{"status":"error","error":"…"}`` to stdout while exiting 0 when
        they catch their own errors. The no-condition path used to treat
        that as healthy — masking exactly the failure mode the setup_env
        hook fix is supposed to eliminate."""
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={
                "command": (
                    """printf '{"status":"error","error":"LOCATION_DB_PATH not set"}'"""
                ),
            },
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is False
        assert "LOCATION_DB_PATH not set" in result.message

    def test_json_error_envelope_without_error_field(self, tmp_path):
        """Fallback message when the envelope omits ``error``."""
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": """printf '{"status":"error"}'"""},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is False
        assert "status=error" in result.message

    def test_json_ok_envelope_stays_healthy(self, tmp_path):
        """Positive envelope is the normal success shape — don't flag it."""
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": """printf '{"status":"ok","count":3}'"""},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True

    def test_malformed_json_stdout_unaffected(self, tmp_path):
        """Output starting with ``{`` but not valid JSON shouldn't break
        the envelope check — treat as opaque success."""
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo '{not really json'"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True

    def test_non_json_stdout_unaffected(self, tmp_path):
        """Plain-text heartbeats (the original shape) keep working."""
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo 'all systems nominal: status error none here'"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True


# ---------------------------------------------------------------------------
# TestRunCheckAdminGate
# ---------------------------------------------------------------------------


class TestRunCheckAdminGate:
    """shell-command heartbeats are admin-only because the subprocess env
    inherits ISTOTA_SECRET_KEY (the master Fernet key for the secrets table)."""

    def test_admin_shell_command_runs(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp", admin_users={"alice"})
        check = HeartbeatCheck(
            name="t", type="shell-command", config={"command": "echo ok"},
        )
        result = run_check(check, config, "alice")
        assert result.healthy is True

    def test_non_admin_shell_command_rejected(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp", admin_users={"root"})
        check = HeartbeatCheck(
            name="t", type="shell-command", config={"command": "echo PWNED"},
        )
        result = run_check(check, config, "alice")
        assert result.healthy is False
        assert "admin-only" in result.message

    def test_non_admin_other_check_types_unaffected(self, tmp_path):
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp", admin_users={"root"})
        watch = HeartbeatCheck(name="t", type="file-watch", config={"path": ""})
        result = run_check(watch, config, "alice")
        # Fails for "no path" reasons, not for admin-only — distinct path.
        assert "admin-only" not in result.message

    @patch("istota.heartbeat.httpx.Client")
    def test_non_admin_url_health_is_refused_before_any_request(
        self, mock_stream, tmp_path,
    ):
        """ISSUE-596: a daemon-netns GET to a URL the user chose is an SSRF probe."""
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp", admin_users={"root"})
        check = HeartbeatCheck(
            name="t", type="url-health",
            config={"url": "http://169.254.169.254/latest/meta-data/"},
        )
        result = run_check(check, config, "alice")
        assert result.healthy is False
        assert "admin-only" in result.message
        mock_stream.assert_not_called()

    def test_empty_admin_users_treats_all_as_admin(self, tmp_path):
        """Back-compat: empty admin_users = all users admin (Config.is_admin)."""
        config = Config(db_path=tmp_path / "absent.db", workspace_path=tmp_path, temp_dir=tmp_path / "temp")
        check = HeartbeatCheck(
            name="t", type="shell-command", config={"command": "echo ok"},
        )
        result = run_check(check, config, "alice")
        assert result.healthy is True


# ---------------------------------------------------------------------------
# TestCheckUrlHealth
# ---------------------------------------------------------------------------


def _url_stream(mock_client):
    return mock_client.return_value.__enter__.return_value.stream


def _url_config(tmp_path, *hosts):
    config = Config(workspace_path=tmp_path)
    config.heartbeat.private_hosts = list(hosts) or ["example.com"]
    return config


class TestCheckUrlHealth:
    def test_no_url(self, tmp_path):
        config = Config(workspace_path=tmp_path)
        check = HeartbeatCheck(name="test", type="url-health", config={})
        result = _check_url_health(check, config)
        assert result.healthy is False
        assert "No URL configured" in result.message

    @patch("istota.heartbeat.httpx.Client")
    def test_url_success(self, mock_get, tmp_path):
        mock_response = MagicMock()
        mock_response.status_code = 200
        _url_stream(mock_get).return_value.__enter__.return_value = mock_response

        config = _url_config(tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="url-health",
            config={"url": "https://example.com/health"},
        )
        result = _check_url_health(check, config)
        assert result.healthy is True

    @patch("istota.heartbeat.httpx.Client")
    def test_url_wrong_status(self, mock_get, tmp_path):
        mock_response = MagicMock()
        mock_response.status_code = 503
        _url_stream(mock_get).return_value.__enter__.return_value = mock_response

        config = _url_config(tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="url-health",
            config={"url": "https://example.com/health", "expected_status": 200},
        )
        result = _check_url_health(check, config)
        assert result.healthy is False
        assert "503" in result.message

    @patch("istota.heartbeat.httpx.Client")
    def test_url_timeout(self, mock_get, tmp_path):
        import httpx
        _url_stream(mock_get).side_effect = httpx.TimeoutException("timeout")

        config = _url_config(tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="url-health",
            config={"url": "https://example.com/health", "timeout": 5},
        )
        result = _check_url_health(check, config)
        assert result.healthy is False
        assert "timeout" in result.message.lower()

    @patch("istota.heartbeat.httpx.Client")
    def test_the_timeout_is_clamped(self, mock_stream, tmp_path):
        _url_stream(mock_stream).return_value.__enter__.return_value = MagicMock(status_code=200)
        config = _url_config(tmp_path)
        for given, used in ((100000, 60.0), (0, 1.0), ("x", 10.0)):
            check = HeartbeatCheck(
                name="t", type="url-health",
                config={"url": "https://example.com", "timeout": given},
            )
            _check_url_health(check, config)
            assert mock_stream.call_args.kwargs["timeout"] == used

    @patch("istota.heartbeat.httpx.Client")
    def test_an_unlisted_host_is_refused_before_any_request(self, mock_stream, tmp_path):
        """ISSUE-907: a task can write this URL, and a GET carries data in its name."""
        config = _url_config(tmp_path)
        check = HeartbeatCheck(
            name="t", type="url-health",
            config={"url": "https://canary-host.attacker.test/canary-path?q=canary-query"},
        )
        result = _check_url_health(check, config)
        assert result.healthy is False
        assert "[private_hosts]" in result.message
        assert "canary" not in result.message
        assert result.details is None
        _url_stream(mock_stream).assert_not_called()

    @patch("istota.heartbeat.httpx.Client")
    def test_nothing_is_listed_by_default(self, mock_stream, tmp_path):
        check = HeartbeatCheck(name="t", type="url-health", config={"url": "https://example.com"})
        result = _check_url_health(check, Config(workspace_path=tmp_path))
        assert result.healthy is False
        _url_stream(mock_stream).assert_not_called()

    @pytest.mark.parametrize("url", [
        "https://attacker.test#@example.com/",
        "https://example.com\\@attacker.test/",
        "https://example.com.attacker.test/",
        "https://example.com@attacker.test/",
        "https://xn--a.attacker.test/",
        "https://xn--canary-secret-3ya.attacker.test/",
        "file:///etc/passwd",
        "not a url",
        "http://[bad/",
        12345,
    ])
    @patch("istota.heartbeat.httpx.Client")
    def test_a_url_that_only_looks_listed_is_refused(self, mock_stream, url, tmp_path):
        check = HeartbeatCheck(name="t", type="url-health", config={"url": url})
        result = _check_url_health(check, _url_config(tmp_path))
        assert result.message == "URL host has no heartbeat grant in [private_hosts]"
        assert result.details is None
        _url_stream(mock_stream).assert_not_called()

    @patch("istota.heartbeat.httpx.Client")
    def test_the_host_name_rule_is_the_listed_name_rule(self, mock_stream, tmp_path):
        _url_stream(mock_stream).return_value.__enter__.return_value = MagicMock(status_code=200)
        check = HeartbeatCheck(name="t", type="url-health", config={"url": "https://EXAMPLE.com./x"})
        assert _check_url_health(check, _url_config(tmp_path)).healthy is True

    @patch("istota.heartbeat.httpx.Client")
    def test_a_grant_for_a_punycode_name_matches(self, mock_client, tmp_path):
        _url_stream(mock_client).return_value.__enter__.return_value = MagicMock(status_code=200)
        check = HeartbeatCheck(name="t", type="url-health", config={"url": "https://xn--bcher-kva.example/"})
        assert _check_url_health(check, _url_config(tmp_path, "xn--bcher-kva.example")).healthy is True


def _redirecting_server(hops, *, cookie_gate=False):
    """A loopback server answering each path from ``hops``: ``(status, location)``.

    With ``cookie_gate`` a request carrying a cookie gets a 200 and any other a cookie.
    """
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            status, location = hops.get(self.path, (404, None))
            if cookie_gate and self.headers.get("Cookie"):
                status, location = 200, None
            self.send_response(status)
            if cookie_gate and status != 200:
                self.send_header("Set-Cookie", "ok=1; Path=/")
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


class TestUrlHealthRedirects:
    """Every hop answers to the list, through a real server and real httpx."""

    def _run(self, tmp_path, monkeypatch, hops, *, cookie_gate=False):
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(name, raising=False)
        server, seen = _redirecting_server(hops, cookie_gate=cookie_gate)
        self.addr = f"127.0.0.1:{server.server_address[1]}"
        hops.update({
            path: (status, location.replace("SELF", self.addr) if location else None)
            for path, (status, location) in hops.items()
        })
        config = _url_config(tmp_path, "127.0.0.1")
        config.admin_users = {"root"}
        check = HeartbeatCheck(
            name="t", type="url-health",
            config={"url": f"http://{self.addr}/start", "timeout": 5},
        )
        try:
            return run_check(check, config, "root"), seen
        finally:
            server.shutdown()
            server.server_close()

    def test_a_redirect_to_a_listed_host_is_followed_to_its_answer(self, tmp_path, monkeypatch):
        result, seen = self._run(tmp_path, monkeypatch, {
            "/start": (302, "http://SELF/next"), "/next": (200, None),
        })
        assert result.healthy is True
        assert seen == ["/start", "/next"]

    def test_a_relative_redirect_stays_on_the_host(self, tmp_path, monkeypatch):
        result, seen = self._run(tmp_path, monkeypatch, {
            "/start": (301, "/next"), "/next": (503, None),
        })
        assert result.healthy is False
        assert "503" in result.message
        assert seen == ["/start", "/next"]

    def test_a_granted_host_cannot_bounce_the_check_elsewhere(self, tmp_path, monkeypatch):
        """An open redirect on a granted host must not carry the data on."""
        other, other_seen = _redirecting_server({})
        target = f"http://localhost:{other.server_address[1]}/canary-path"
        try:
            result, seen = self._run(tmp_path, monkeypatch, {"/start": (302, target)})
        finally:
            other.shutdown()
            other.server_close()
        assert result.healthy is False
        assert "[private_hosts]" in result.message
        assert "canary" not in result.message
        assert seen == ["/start"]
        assert other_seen == []

    def test_an_invalid_redirect_target_is_refused_in_fixed_text(self, tmp_path, monkeypatch):
        result, seen = self._run(tmp_path, monkeypatch, {
            "/start": (302, "http://canary.test:notaport/"),
        })
        assert result.healthy is False
        assert "canary" not in result.message and "notaport" not in result.message
        assert seen == ["/start"]

    def test_a_cookie_set_on_a_redirect_comes_back(self, tmp_path, monkeypatch):
        result, seen = self._run(tmp_path, monkeypatch, {"/start": (302, "/start")}, cookie_gate=True)
        assert result.healthy is True
        assert seen == ["/start", "/start"]

    def test_a_redirect_loop_ends(self, tmp_path, monkeypatch):
        result, seen = self._run(tmp_path, monkeypatch, {"/start": (302, "/start")})
        assert result.healthy is False
        assert "redirect" in result.message.lower()
        assert len(seen) == 6


class TestHeartbeatGrants:
    def test_a_heartbeat_grant_in_the_table_reaches_the_check(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            '[private_hosts]\n"status.internal" = ["heartbeat"]\n"feeds.internal" = ["feeds"]\n'
        )
        config = load_config(path)
        assert config.heartbeat.private_hosts == ["status.internal"]
        assert config.feeds.private_hosts == ["feeds.internal"]

    def test_empty_by_default(self):
        assert Config().heartbeat.private_hosts == []


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
        config = Config(db_path=db_path, workspace_path=tmp_path, users={})
        with db.get_db(db_path) as conn:
            result = check_heartbeats(conn, config)
        assert result == []

    def test_user_without_heartbeat_file(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()

        config = Config(
            db_path=db_path,
            workspace_path=mount,
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
        watched_file = mount / "Users" / "alice" / "test.txt"
        watched_file.write_text("content")

        (users_dir / "HEARTBEAT.md").write_text("""
```toml
[settings]
conversation_token = "room123"

[[checks]]
name = "test-check"
type = "file-watch"
path = "/Users/alice/test.txt"
```
""")

        config = Config(
            db_path=db_path,
            workspace_path=mount,
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
            workspace_path=mount,
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
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
    def test_unconfigured_channel_skips_alert_without_bumping_errors(
        self, mock_alert, db_path, tmp_path,
    ):
        """A check whose channel isn't configured for the user is skipped
        with a log warning — not counted as an alert-pipeline failure.

        Regression: previously, a heartbeat with `channel = "ntfy"` and no
        per-user ntfy topic would call send_notification, get back False,
        and bump consecutive_errors on every cycle. The fix introduces
        `is_channel_configured` which short-circuits the alert path
        without touching error state.
        """
        mount = tmp_path / "mount"
        mount.mkdir()
        users_dir = mount / "Users" / "alice" / "istota" / "config"
        users_dir.mkdir(parents=True)
        (users_dir / "HEARTBEAT.md").write_text("""
```toml
[settings]
conversation_token = "room123"

[[checks]]
name = "missing-file"
type = "file-watch"
path = "/nonexistent.txt"
channel = "ntfy"
```
""")

        # Config has no ntfy secret → ntfy channel is "not configured".
        config = Config(
            db_path=db_path,
            workspace_path=mount,
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig(timezone="UTC")},
        )

        with db.get_db(db_path) as conn:
            check_heartbeats(conn, config)
            state = db.get_heartbeat_state(conn, "alice", "missing-file")
            assert state is not None
            # Skip-without-bump: alert was never recorded, errors stayed at 0,
            # last_error_at was not stamped.
            assert state.last_alert_at is None
            assert state.consecutive_errors == 0
            assert state.last_error_at is None

        # send_heartbeat_alert is short-circuited before being called.
        mock_alert.assert_not_called()

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
path = "/Users/alice/test.txt"
interval_minutes = 30
```
""")

        config = Config(
            db_path=db_path,
            workspace_path=mount,
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

        watched_file = mount / "Users" / "alice" / "test.txt"
        watched_file.write_text("content")

        (users_dir / "HEARTBEAT.md").write_text("""
```toml
[[checks]]
name = "slow-check"
type = "file-watch"
path = "/Users/alice/test.txt"
interval_minutes = 30
```
""")

        config = Config(
            db_path=db_path,
            workspace_path=mount,
            users={"alice": UserConfig(timezone="UTC")},
        )

        with db.get_db(db_path) as conn:
            # Set last_check_at to 31 minutes ago. check_heartbeats compares
            # against datetime.now(UTC), so use UTC here regardless of local tz.
            old_time = (
                datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=31)
            ).isoformat()
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

        watched_file = mount / "Users" / "alice" / "test.txt"
        watched_file.write_text("content")

        (users_dir / "HEARTBEAT.md").write_text("""
```toml
[[checks]]
name = "fast-check"
type = "file-watch"
path = "/Users/alice/test.txt"
```
""")

        config = Config(
            db_path=db_path,
            workspace_path=mount,
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
    """`self-check` renders `doctor.run_checks`; it asserts nothing of its own.

    Every case here patches `doctor.run_checks` and asserts on the kwargs it
    was handed, rather than running the registry — the same shape as
    `tests/test_cli_doctor.py`, which tests `cmd_doctor` by capturing the call.
    What the checks themselves do is `tests/test_doctor.py`'s job, and running
    the real registry from here would make this file pay for it twice.
    """

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

    def _capture(self, results, check, config, user_id="alice"):
        """Run `_check_self` against a stubbed registry; return (result, kwargs)."""
        calls = {}

        def fake_run_checks(cfg, **kwargs):
            calls["config"] = cfg
            calls["kwargs"] = kwargs
            return list(results)

        with patch("istota.doctor.run_checks", side_effect=fake_run_checks):
            result = _check_self(check, config, user_id)
        return result, calls

    def _result(self, name, status, detail="detail", remedy=""):
        from istota.doctor import CheckResult as DoctorResult

        return DoctorResult(name, status, detail, remedy=remedy)

    def test_execution_test_selects_live(self, db_path):
        """`execution_test` gates exactly one thing — the live model call — and
        `live` is exactly one thing. Mapping it to `deep` would have widened
        `execution_test: false` from "spawn nothing" to "run the whole
        registry"."""
        from istota.doctor import OK

        _, calls = self._capture(
            [self._result("runtime.platform", OK)],
            self._make_check(execution_test=True),
            self._make_config(db_path),
        )

        assert calls["kwargs"]["live"] is True

    def test_execution_test_false_clears_live(self, db_path):
        from istota.doctor import OK

        _, calls = self._capture(
            [self._result("runtime.platform", OK)],
            self._make_check(execution_test=False),
            self._make_config(db_path),
        )

        assert calls["kwargs"]["live"] is False

    def test_live_defaults_on(self, db_path):
        """`execution_test` keeps its current default of True, so a
        `HEARTBEAT.md` that never mentioned the key behaves as it did."""
        from istota.doctor import OK

        _, calls = self._capture(
            [self._result("runtime.platform", OK)],
            self._make_check(),
            self._make_config(db_path),
        )

        assert calls["kwargs"]["live"] is True

    def test_deep_is_never_asked_for(self, db_path):
        """`self-check` is not admin-gated (`run_check` gates only
        `shell-command`), so any user's `HEARTBEAT.md` reaches this on a
        cadence they choose. A namespace spawn here multiplies by users and by
        check definitions; `security.sandbox_effective` answers the
        availability question from the warm memo at no cost instead."""
        from istota.doctor import OK

        _, calls = self._capture(
            [self._result("runtime.platform", OK)],
            self._make_check(execution_test=True),
            self._make_config(db_path),
        )

        assert calls["kwargs"].get("deep", False) is False

    def test_the_skip_list_is_passed(self, db_path):
        from istota.doctor import OK
        from istota.heartbeat import _SELF_CHECK_SKIPPED

        _, calls = self._capture(
            [self._result("runtime.platform", OK)],
            self._make_check(),
            self._make_config(db_path),
        )

        assert calls["kwargs"]["skip"] == _SELF_CHECK_SKIPPED
        # Each entry is a real registry name, not a prefix nothing matches: a
        # typo here is a check that goes on running per user, silently.
        from istota.doctor import CHECKS

        names = {name for name, _ in CHECKS}
        for skipped in _SELF_CHECK_SKIPPED:
            assert skipped in names, f"{skipped} is not a registry name"

    def test_a_clean_registry_is_healthy(self, db_path):
        from istota.doctor import OK, SKIP

        result, _ = self._capture(
            [
                self._result("runtime.platform", OK),
                self._result("runtime.bwrap", SKIP),
            ],
            self._make_check(execution_test=False),
            self._make_config(db_path),
        )

        assert result.healthy
        assert "1 ok" in result.message

    def test_the_message_names_the_failing_checks(self, db_path):
        """Today's message is a semicolon-joined list of the specific
        failures, and that is what makes the alert actionable. A count alone
        would be a regression, so `failing` supplies the content."""
        from istota.doctor import FAIL, OK

        result, _ = self._capture(
            [
                self._result("runtime.platform", OK),
                self._result("security.sandbox_effective", FAIL, "no namespace"),
                self._result("runtime.model_execution", FAIL, "marker not in output"),
            ],
            self._make_check(),
            self._make_config(db_path),
        )

        assert not result.healthy
        assert "security.sandbox_effective: no namespace" in result.message
        assert "runtime.model_execution: marker not in output" in result.message
        assert result.details["failures"] == [
            "runtime.model_execution",
            "security.sandbox_effective",
        ]

    def test_a_warning_does_not_page(self, db_path):
        """The deliberate behaviour change: the old copy appended its
        high-failure-rate finding to `failures` and returned unhealthy for it.
        `runtime.task_failure_rate` is a WARN and a WARN is not a page."""
        from istota.doctor import OK, WARN

        result, _ = self._capture(
            [
                self._result("runtime.platform", OK),
                self._result("runtime.task_failure_rate", WARN, "3 failed, 1 completed"),
            ],
            self._make_check(execution_test=False),
            self._make_config(db_path),
        )

        assert result.healthy
        assert "1 warn" in result.message

    def test_it_redacts_before_the_message_leaves(self, db_path):
        """This path delivers to a user. `scheduler` and `web_app` both redact
        before anything leaves the process and there was no reason for the
        heartbeat to be the exception."""
        from istota.doctor import FAIL

        config = self._make_config(db_path)
        config.nextcloud = NextcloudConfig(
            url="https://cloud.example.com",
            username="bot",
            app_password="s3cr3t-app-password",
        )

        result, _ = self._capture(
            [self._result("web.static", FAIL, "upstream said s3cr3t-app-password")],
            self._make_check(execution_test=False),
            config,
        )

        assert "s3cr3t-app-password" not in result.message
        assert "web.static" in result.message

    def test_it_spawns_no_probe_of_its_own(self, db_path):
        """The whole point of the stage: no `shutil.which`, no
        `subprocess.run`, no `build_bwrap_cmd` on this path. Whatever probing
        happens is the registry's, behind the flags above.

        `execution_test=True` deliberately, which is the arm that used to exec
        `claude` — with `execution_test=False` this assertion is equally true
        of the copy it replaces and proves nothing.
        """
        from istota.doctor import OK

        with patch("subprocess.run") as spawned, patch("shutil.which") as looked_up:
            self._capture(
                [self._result("runtime.platform", OK)],
                self._make_check(execution_test=True),
                self._make_config(db_path),
            )

        spawned.assert_not_called()
        looked_up.assert_not_called()


    def test_a_non_admin_gets_the_count_and_no_details(self, db_path):
        """The disclosure boundary the removed `!check` drew, kept here.

        `self-check` is not admin-gated and `check_heartbeats` delivers the
        message to whichever user's `HEARTBEAT.md` defined the check, so a
        registry detail reaches a non-admin. Several of them are cross-user:
        `config.skill_overlays` labels overlays `{user_id}/{filename}` across
        every user's tree, `developer.repos_layout` names what is filed on disk,
        and `runtime.model_execution` names the admin it probed as.
        """
        from istota.doctor import FAIL

        config = self._make_config(db_path)
        config.admin_users = {"boss"}

        result, _ = self._capture(
            [
                self._result(
                    "config.skill_overlays", FAIL, "bob/coding.md (unknown skill)"
                ),
                self._result(
                    "developer.repos_layout", FAIL, "/srv/repos still holds acme, widgets"
                ),
            ],
            self._make_check(execution_test=False),
            config,
            user_id="alice",
        )

        assert not result.healthy, "the alert must still fire, only quieter"
        assert "bob" not in result.message
        assert "coding.md" not in result.message
        assert "acme" not in result.message
        assert "/srv/repos" not in result.message
        assert "2 fail" in result.message

    def test_an_admin_still_gets_the_details(self, db_path):
        """The control for the case above. Without it, the absence assertions
        pass against a handler that returns nothing at all — and naming the
        failures is what makes the alert actionable for whoever can act."""
        from istota.doctor import FAIL

        config = self._make_config(db_path)
        config.admin_users = {"alice"}

        result, _ = self._capture(
            [
                self._result(
                    "config.skill_overlays", FAIL, "bob/coding.md (unknown skill)"
                )
            ],
            self._make_check(execution_test=False),
            config,
            user_id="alice",
        )

        assert "config.skill_overlays: bob/coding.md (unknown skill)" in result.message

    def test_an_empty_admin_list_means_everyone(self, db_path):
        """`Config.is_admin` reads an empty `admin_users` as "everyone is
        admin", which is the single-user shape. The gate must inherit that
        rather than reading the empty list as "nobody"."""
        from istota.doctor import FAIL

        config = self._make_config(db_path)
        assert not config.admin_users

        result, _ = self._capture(
            [self._result("web.static", FAIL, "no built frontend")],
            self._make_check(execution_test=False),
            config,
            user_id="alice",
        )

        assert "web.static: no built frontend" in result.message

    def test_a_doctor_defect_does_not_page_the_user(self, db_path):
        """`run_checks` is not exception-proof end to end: a check returning a
        non-iterable escapes the per-check `try`, and `redact` sits outside it.
        Both scheduler callers wrap this pair; `run_check`'s blanket handler
        would otherwise turn a defect in doctor into an alert for every user
        with a `self-check`."""
        config = self._make_config(db_path)

        with patch("istota.doctor.run_checks", side_effect=TypeError("boom")):
            result = _check_self(
                self._make_check(execution_test=False), config, "alice"
            )

        assert result.healthy, "a broken diagnostic must not page a user"
        assert "could not be run" in result.message
        assert result.details["failures"] == []

    def test_a_redaction_failure_is_caught_too(self, db_path):
        """The half that is easy to miss: `redact` is outside `run_checks`'
        own per-check guard, so it is the call that actually reaches a user."""
        from istota.doctor import OK

        config = self._make_config(db_path)

        with (
            patch(
                "istota.doctor.run_checks",
                return_value=[self._result("runtime.platform", OK)],
            ),
            patch("istota.doctor.redact", side_effect=RuntimeError("boom")),
        ):
            result = _check_self(
                self._make_check(execution_test=False), config, "alice"
            )

        assert result.healthy
        assert "could not be run" in result.message

    def test_run_check_dispatches_self_check(self, db_path):
        """Verify run_check() correctly dispatches self-check with user_id."""
        from istota.doctor import OK

        calls = {}

        def fake_run_checks(cfg, **kwargs):
            calls["kwargs"] = kwargs
            return [self._result("runtime.platform", OK)]

        with patch("istota.doctor.run_checks", side_effect=fake_run_checks):
            result = run_check(
                self._make_check(execution_test=False),
                self._make_config(db_path),
                "alice",
            )

        assert result.healthy
        assert calls["kwargs"]["live"] is False




class TestHeartbeatPlantedPaths:
    """HEARTBEAT.md and TASKS.md live in the same read-write sandbox bind as
    USER.md, and both are read on the scheduler's own tick (ISSUE-339)."""

    def _config_dir(self, tmp_path):
        mount = tmp_path / "mount"
        d = mount / "Users" / "alice" / "istota" / "config"
        d.mkdir(parents=True)
        return mount, d

    def test_a_symlink_at_heartbeat_md_is_not_followed(self, tmp_path):
        mount, d = self._config_dir(tmp_path)
        secret = tmp_path / "secret.md"
        secret.write_text('```toml\n[[checks]]\nname = "planted"\ntype = "url-health"\nurl = "http://x"\n```\n')
        (d / "HEARTBEAT.md").symlink_to(secret)

        assert load_heartbeat_config(Config(workspace_path=mount), "alice") is None

    def test_a_fifo_at_heartbeat_md_does_not_block_the_scheduler(self, tmp_path):
        from .support.blocking import fails_if_it_blocks

        mount, d = self._config_dir(tmp_path)
        os.mkfifo(d / "HEARTBEAT.md")
        config = Config(workspace_path=mount)
        with fails_if_it_blocks(what="load_heartbeat_config"):
            assert load_heartbeat_config(config, "alice") is None

    def test_an_ordinary_heartbeat_md_still_parses(self, tmp_path):
        mount, d = self._config_dir(tmp_path)
        (d / "HEARTBEAT.md").write_text(
            '```toml\n[[checks]]\nname = "site"\ntype = "url-health"\nurl = "http://x"\n```\n'
        )
        result = load_heartbeat_config(Config(workspace_path=mount), "alice")
        assert result is not None
        _settings, checks = result
        assert [c.name for c in checks] == ["site"]


# ---------------------------------------------------------------------------
# TestTheStandingFailureGate
# ---------------------------------------------------------------------------


class TestTheStandingFailureGate:
    """A failure that has not changed since the last page does not page again.

    `should_alert`'s cooldown rate-limits a standing failure; it never ends it.
    That was right while `_check_self` ran five probes that were all expected to
    pass, and it stopped being right when it started running the doctor
    registry: `security.sandbox_effective` FAILs *by design* on the shipped
    Docker stack (AGENTS.md documents that as the normal state there), so every
    user with a `self-check` would be paged once per cooldown, forever, for a
    condition nobody can act on from that surface and which the scheduler's own
    doctor sweep already reports with a real transition gate.

    The gate is opt-in through `CheckResult.alert_signature`, and that is the
    whole reason it is safe. A check that leaves it `None` — the other five
    types — keeps today's cooldown behaviour exactly, which is what a
    `url-health` check wants: a site that is still down an hour later is worth
    saying again. Only a check that can name *what* is failing gets to claim
    that an unchanged answer is not news.
    """

    def _self_check(self):
        return HeartbeatCheck(name="self", type="self-check", config={})

    def test_an_unchanged_failure_set_does_not_page_twice(self, db_path):
        with db.get_db(db_path) as conn:
            settings = HeartbeatSettings(default_cooldown_minutes=0)
            check = self._self_check()
            result = CheckResult(
                healthy=False,
                message="security.sandbox_effective: unsandboxed",
                alert_signature="security.sandbox_effective",
            )

            # First time: nobody has been told.
            assert should_alert(conn, "alice", check, result, settings, "UTC") is True
            db.update_heartbeat_state(
                conn, "alice", "self",
                last_alert_at=True,
                last_alert_signature=result.alert_signature,
            )

            # Cooldown is zero, so only the signature can suppress this.
            assert should_alert(conn, "alice", check, result, settings, "UTC") is False

    def test_a_changed_failure_set_pages_again(self, db_path):
        with db.get_db(db_path) as conn:
            settings = HeartbeatSettings(default_cooldown_minutes=0)
            check = self._self_check()
            db.update_heartbeat_state(
                conn, "alice", "self",
                last_alert_at=True,
                last_alert_signature="security.sandbox_effective",
            )

            worse = CheckResult(
                healthy=False,
                message="two checks failing",
                alert_signature="runtime.model_cli|security.sandbox_effective",
            )
            assert should_alert(conn, "alice", check, worse, settings, "UTC") is True

    def test_a_check_with_no_signature_is_unaffected(self, db_path):
        """The other five types must behave exactly as they did."""
        with db.get_db(db_path) as conn:
            settings = HeartbeatSettings(default_cooldown_minutes=0)
            check = HeartbeatCheck(name="site", type="url-health", config={})
            result = CheckResult(healthy=False, message="down")
            assert result.alert_signature is None

            assert should_alert(conn, "alice", check, result, settings, "UTC") is True
            db.update_heartbeat_state(conn, "alice", "site", last_alert_at=True)
            # No signature to compare, cooldown expired: still pages.
            assert should_alert(conn, "alice", check, result, settings, "UTC") is True

    def test_recovery_then_the_same_failure_pages_again(self, db_path):
        """A signature is cleared on recovery, so a recurrence is news again.

        Without this, a deployment that broke, was fixed, and broke the same way
        a month later would stay silent for ever.
        """
        with db.get_db(db_path) as conn:
            settings = HeartbeatSettings(default_cooldown_minutes=0)
            check = self._self_check()
            db.update_heartbeat_state(
                conn, "alice", "self",
                last_alert_at=True,
                last_alert_signature="security.sandbox_effective",
            )
            # Recovered: the handler clears the signature.
            db.update_heartbeat_state(
                conn, "alice", "self", last_healthy_at=True, clear_alert_signature=True,
            )

            again = CheckResult(
                healthy=False,
                message="unsandboxed again",
                alert_signature="security.sandbox_effective",
            )
            assert should_alert(conn, "alice", check, again, settings, "UTC") is True

    def _config(self, db_path):
        return Config(db_path=db_path, security=SecurityConfig(sandbox_enabled=False))

    def _doctor_result(self, name, status, detail="detail"):
        from istota.doctor import CheckResult as DoctorResult

        return DoctorResult(name, status, detail, remedy="")

    def test_suppression_expires_so_a_standing_failure_is_reported_again(self, db_path):
        """The bound that makes this a rate limit rather than an off switch.

        The signature is the failing *names*, so a condition getting steadily
        worse without changing which checks fail produces the same string; and
        an alert delivered to one of two configured surfaces reports success.
        Neither should be silenced for ever.
        """
        with db.get_db(db_path) as conn:
            settings = HeartbeatSettings(default_cooldown_minutes=0)
            check = self._self_check()
            conn.execute(
                """
                INSERT INTO heartbeat_state
                    (user_id, check_name, last_alert_at, last_alert_signature)
                VALUES (?, ?, datetime('now', '-48 hours'), ?)
                """,
                ("alice", "self", "security.sandbox_effective"),
            )
            result = CheckResult(
                healthy=False,
                message="still unsandboxed",
                alert_signature="security.sandbox_effective",
            )

            assert should_alert(conn, "alice", check, result, settings, "UTC") is True

    def test_an_unparseable_alert_time_reports_rather_than_silences(self, db_path):
        """A gate whose job is withholding must fail towards a duplicate."""
        with db.get_db(db_path) as conn:
            settings = HeartbeatSettings(default_cooldown_minutes=0)
            check = self._self_check()
            conn.execute(
                """
                INSERT INTO heartbeat_state
                    (user_id, check_name, last_alert_at, last_alert_signature)
                VALUES (?, ?, 'not-a-timestamp', ?)
                """,
                ("alice", "self", "security.sandbox_effective"),
            )
            result = CheckResult(
                healthy=False,
                message="still unsandboxed",
                alert_signature="security.sandbox_effective",
            )

            assert should_alert(conn, "alice", check, result, settings, "UTC") is True

    def test_the_self_check_names_its_failures_as_the_signature(self, db_path):
        """The signature is the sorted failing names, so it is order-stable.

        Sorted rather than as-returned: `run_checks` walks the registry in
        declaration order, so inserting a check between two failing ones would
        otherwise change the signature and page everybody about a failure set
        that had not changed.
        """
        results = [
            self._doctor_result("security.sandbox_effective", "fail", "unsandboxed"),
            self._doctor_result("runtime.model_cli", "fail", "missing"),
        ]
        with patch("istota.doctor.run_checks", return_value=results):
            result = _check_self(self._self_check(), self._config(db_path), "alice")

        assert result.healthy is False
        assert result.alert_signature == "runtime.model_cli|security.sandbox_effective"

    def test_a_healthy_self_check_carries_no_signature(self, db_path):
        with patch("istota.doctor.run_checks", return_value=[]):
            result = _check_self(self._self_check(), self._config(db_path), "alice")

        assert result.healthy is True
        assert result.alert_signature is None


# ---------------------------------------------------------------------------
# TestCheckCalendarConflicts (ISSUE-673)
# ---------------------------------------------------------------------------


class TestCheckCalendarConflicts:
    ALICE = "https://cloud.example.com/remote.php/dav/calendars/istota/a/"
    BOB = "https://cloud.example.com/remote.php/dav/calendars/istota/b/"

    def _calendar(self, name, url, owner):
        cal = MagicMock()
        cal.name = name
        cal.url = url
        cal.get_properties.return_value = {
            "{DAV:}owner": f"/remote.php/dav/principals/users/{owner}/",
        }
        return cal

    def _run(self, events_by_url, *, make_events=None):
        from istota.heartbeat import _check_calendar_conflicts
        from istota.skills.calendar import CalendarEvent

        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.principal.return_value.calendars.return_value = [
            self._calendar("Alice", self.ALICE, "alice"),
            self._calendar("Bob", self.BOB, "bob"),
        ]
        soon = datetime.now() + timedelta(hours=1)

        def events(_client, url, _start, _end):
            if make_events is not None:
                return make_events(url)
            return [
                CalendarEvent(uid=s, summary=s, start=soon, end=soon + timedelta(hours=1))
                for s in events_by_url.get(url, [])
            ]

        config = Config(nextcloud=NextcloudConfig(
            url="https://cloud.example.com", username="istota", app_password="pw",
        ))
        check = HeartbeatCheck(name="cal", type="calendar-conflicts", config={})
        with patch("istota.skills.calendar.get_caldav_client", return_value=client), \
                patch("istota.skills.calendar.get_events", side_effect=events) as get_events_:
            result = _check_calendar_conflicts(check, config, "alice")
        return result, [c.args[1] for c in get_events_.call_args_list]

    def test_reads_only_the_users_own_calendars(self):
        result, read = self._run({self.ALICE: ["Standup"], self.BOB: ["Dentist"]})
        assert read == [self.ALICE]
        assert result.healthy is True
        assert result.message == "No calendar conflicts"

    def test_finds_an_overlap_in_the_users_calendars(self):
        result, _ = self._run({self.ALICE: ["Standup", "Review"]})
        assert result.healthy is False
        assert "'Standup' and 'Review'" in result.message

    def _two_events(self, tz_a, tz_b):
        from istota.skills.calendar import CalendarEvent

        # get_events hands back wall-clock times with the zone stripped.
        wall = (datetime.now() + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)

        def make(url):
            if url != self.ALICE:
                return []
            return [
                CalendarEvent(uid="a", summary="A", start=wall,
                              end=wall + timedelta(hours=1), timezone=tz_a),
                CalendarEvent(uid="b", summary="B", start=wall,
                              end=wall + timedelta(hours=1), timezone=tz_b),
            ]
        return make

    def test_same_wall_clock_in_different_zones_is_not_a_conflict(self):
        result, _ = self._run({}, make_events=self._two_events("UTC", "Asia/Tokyo"))
        assert result.healthy is True, result.message

    def test_same_zone_same_wall_clock_is_a_conflict(self):
        result, _ = self._run({}, make_events=self._two_events("Asia/Tokyo", "Asia/Tokyo"))
        assert result.healthy is False


# ---------------------------------------------------------------------------
# parse_heartbeat_text: the loader's parse half, for the web editor
# ---------------------------------------------------------------------------


_HEARTBEAT_TEXTS = {
    "empty": "",
    "whitespace": "  \n",
    "no fence": "# Just markdown, no TOML",
    "commented": "```toml\n# [[checks]]\n# name = \"x\"\n```\n",
    "empty fence": "```toml\n```\n",
    "toml error": "```toml\n[[checks]\nname = \"x\"\n```\n",
    "no checks": "```toml\n[settings]\nquiet_hours = [\"22:00-07:00\"]\n```\n",
    "check without type": "```toml\n[[checks]]\nname = \"x\"\n```\n",
    "two checks": (
        "# Heartbeat\n\n```toml\n[settings]\nconversation_token = \"room1\"\n"
        "default_cooldown_minutes = 30\n\n[[checks]]\nname = \"site\"\n"
        "type = \"url-health\"\nurl = \"https://example.com\"\ncooldown_minutes = 5\n\n"
        "[[checks]]\nname = \"disk\"\ntype = \"shell-command\"\ncommand = \"df\"\n"
        "interval_minutes = 10\nchannel = \"room2\"\n```\n"
    ),
}


class TestParseHeartbeatText:
    @pytest.mark.parametrize("name", sorted(_HEARTBEAT_TEXTS))
    def test_agrees_with_the_loader(self, tmp_path, name):
        from istota.heartbeat import parse_heartbeat_text

        content = _HEARTBEAT_TEXTS[name]
        config_dir = tmp_path / "mount" / "Users" / "alice" / "istota" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "HEARTBEAT.md").write_text(content)

        loaded = load_heartbeat_config(Config(workspace_path=tmp_path / "mount"), "alice")
        assert parse_heartbeat_text(content, "alice") == loaded

    def test_a_valid_file_parses_to_its_checks(self):
        from istota.heartbeat import parse_heartbeat_text

        settings, checks = parse_heartbeat_text(_HEARTBEAT_TEXTS["two checks"])
        assert settings.conversation_token == "room1"
        assert settings.default_cooldown_minutes == 30
        assert [(c.name, c.type) for c in checks] == [
            ("site", "url-health"), ("disk", "shell-command")]
        assert checks[0].config == {"url": "https://example.com"}
        assert checks[1].channel == "room2" and checks[1].interval_minutes == 10


@pytest.mark.parametrize("text,reason", [
    ('```toml\n[[checks]]\n', "unreadable_fence"),
    ('```toml\ninvalid = [\n```\n', "invalid_toml"),
    ('```toml\nchecks = [1]\n```\n', "invalid_shape"),
    ('```toml\n[[checks]]\nname = "incomplete"\n```\n', "skipped_check"),
])
def test_parse_heartbeat_diagnostics_for_rewrite(text, reason, caplog):
    from istota.heartbeat import parse_heartbeat_text

    diagnostics = []
    assert parse_heartbeat_text(text, diagnostics=diagnostics, include_empty=True) in (None, (HeartbeatSettings(), []))
    assert diagnostics == [reason]
    assert not caplog.text


def test_parse_heartbeat_retains_settings_without_checks_when_requested():
    from istota.heartbeat import parse_heartbeat_text

    text = '```toml\n[settings]\ndefault_cooldown_minutes = 120\n```\n'
    assert parse_heartbeat_text(text) is None
    assert parse_heartbeat_text(text, include_empty=True) == (HeartbeatSettings(default_cooldown_minutes=120), [])
