"""Configuration loading for istota.heartbeat module."""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock


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
from istota.config import Config, NextcloudConfig, SecurityConfig, UserConfig
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

    def test_experimental_features_propagated(self, tmp_path):
        """Heartbeat shell-command checks must propagate
        ISTOTA_EXPERIMENTAL_FEATURES so any heartbeat invoking a gated CLI
        gets the same view of enabled flags as the scheduler subprocess
        paths."""
        from istota.config import ExperimentalConfig
        config = Config(
            nextcloud_mount_path=tmp_path,
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

    def test_a_failing_stage_of_a_pipeline_is_unhealthy(self, tmp_path):
        """`shell=True` is `/bin/sh -c`, which starts with `pipefail` off, so a
        probe ending in a pipe reported the last stage and a broken check read
        as healthy forever. The counterpart of ISSUE-307 on this surface."""
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "false | tail -1"},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is False

    def test_a_succeeding_pipeline_is_still_healthy(self, tmp_path):
        """Control — a piped probe that works must not start alerting.

        Deliberately carries no `condition`: `_check_shell_command` only reads
        `result.returncode` in the `if not condition:` branch, so a control with
        one would decide health from stdout alone and pass identically against
        the pre-pipefail code — proving nothing about the change it guards.
        """
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo hi | tail -1"},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is True

    def test_a_sigpipe_probe_says_what_141_means(self, tmp_path):
        """The message reaches an operator through an alert, with no stderr
        beside it — a SIGPIPE'd producer writes none. A bare `exit 141` on a
        correct probe is the kind of thing someone debugs at 3am."""
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "yes | head -1"},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is False
        assert "141" in result.message
        assert "SIGPIPE" in result.message, result.message

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

    def test_config_path_propagated(self, tmp_path):
        """Module-skill subprocesses (e.g. `istota-skill feeds`) load the
        daemon's config from ISTOTA_CONFIG_PATH. Without it they fall back
        to a default Config() with empty users and exit with a JSON error
        envelope, while the shell exit 0 makes the heartbeat look healthy."""
        config = Config(nextcloud_mount_path=tmp_path)
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
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo $ISTOTA_USER_ID"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True
        assert "alice" in result.details["value"]

    def test_db_path_propagated(self, tmp_path):
        config = Config(db_path=tmp_path / "istota.db", nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo $ISTOTA_DB_PATH"},
        )
        result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True
        assert str(config.db_path) in result.details["value"]

    def test_setup_env_hooks_dispatched(self, tmp_path):
        """ISSUE-097-shaped regression: heartbeat shell-commands must run
        ``dispatch_setup_env_hooks`` so vars declared ``from: "setup_env"``
        (``LOCATION_DB_PATH``, ``HEALTH_DB_PATH``) reach the subprocess.
        Without this, ``shell-command`` checks that shell out to
        ``istota-skill location …`` / ``istota-skill health …`` would fail
        silently — the skill CLI prints a JSON error envelope while exiting 0,
        which the no-condition path used to treat as healthy."""
        from unittest.mock import patch
        config = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path,
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "temp").mkdir(exist_ok=True)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={
                "command": "echo loc=[$LOCATION_DB_PATH]:health=[$HEALTH_DB_PATH]",
            },
        )
        with patch(
            "istota.skills._env.dispatch_setup_env_hooks",
            return_value={
                "LOCATION_DB_PATH": "/srv/data/alice/location.db",
                "HEALTH_DB_PATH": "/srv/data/alice/health.db",
            },
        ):
            result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True
        assert "loc=[/srv/data/alice/location.db]" in result.details["value"]
        assert "health=[/srv/data/alice/health.db]" in result.details["value"]

    def test_setup_env_hooks_skipped_without_user_id(self, tmp_path):
        """No user_id means we can't resolve per-user paths — fall back
        cleanly without invoking the hook dispatcher."""
        from unittest.mock import patch
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test", type="shell-command", config={"command": "echo ok"},
        )
        with patch(
            "istota.heartbeat._build_heartbeat_skill_env",
        ) as mock_build:
            result = _check_shell_command(check, config)
        mock_build.assert_not_called()
        assert result.healthy is True

    def test_skill_env_failure_does_not_break_check(self, tmp_path):
        """If hook dispatch raises, the heartbeat should still run the
        command — better degraded than silently dead. The warning surfaces
        the resolution failure to logs."""
        from unittest.mock import patch
        config = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path,
            temp_dir=tmp_path / "temp",
        )
        (tmp_path / "temp").mkdir(exist_ok=True)
        check = HeartbeatCheck(
            name="test", type="shell-command", config={"command": "echo ok"},
        )
        with patch(
            "istota.heartbeat._build_heartbeat_skill_env",
            side_effect=RuntimeError("kaboom"),
        ):
            result = _check_shell_command(check, config, user_id="alice")
        assert result.healthy is True
        assert "ok" in result.details["value"]

    def test_json_error_envelope_marks_unhealthy(self, tmp_path):
        """ISSUE-097-shaped silent-rot: istota-skill CLIs emit
        ``{"status":"error","error":"…"}`` to stdout while exiting 0 when
        they catch their own errors. The no-condition path used to treat
        that as healthy — masking exactly the failure mode the setup_env
        hook fix is supposed to eliminate."""
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={
                "command": (
                    """printf '{"status":"error","error":"LOCATION_DB_PATH not set"}'"""
                ),
            },
        )
        result = _check_shell_command(check, config)
        assert result.healthy is False
        assert "LOCATION_DB_PATH not set" in result.message

    def test_json_error_envelope_without_error_field(self, tmp_path):
        """Fallback message when the envelope omits ``error``."""
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": """printf '{"status":"error"}'"""},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is False
        assert "status=error" in result.message

    def test_json_ok_envelope_stays_healthy(self, tmp_path):
        """Positive envelope is the normal success shape — don't flag it."""
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": """printf '{"status":"ok","count":3}'"""},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is True

    def test_malformed_json_stdout_unaffected(self, tmp_path):
        """Output starting with ``{`` but not valid JSON shouldn't break
        the envelope check — treat as opaque success."""
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo '{not really json'"},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is True

    def test_non_json_stdout_unaffected(self, tmp_path):
        """Plain-text heartbeats (the original shape) keep working."""
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="test",
            type="shell-command",
            config={"command": "echo 'all systems nominal: status error none here'"},
        )
        result = _check_shell_command(check, config)
        assert result.healthy is True


# ---------------------------------------------------------------------------
# TestRunCheckAdminGate
# ---------------------------------------------------------------------------


class TestRunCheckAdminGate:
    """shell-command heartbeats are admin-only because the subprocess env
    inherits ISTOTA_SECRET_KEY (the master Fernet key for the secrets table)."""

    def test_admin_shell_command_runs(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path, admin_users={"alice"})
        check = HeartbeatCheck(
            name="t", type="shell-command", config={"command": "echo ok"},
        )
        result = run_check(check, config, "alice")
        assert result.healthy is True

    def test_non_admin_shell_command_rejected(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path, admin_users={"root"})
        check = HeartbeatCheck(
            name="t", type="shell-command", config={"command": "echo PWNED"},
        )
        result = run_check(check, config, "alice")
        assert result.healthy is False
        assert "admin-only" in result.message

    def test_non_admin_other_check_types_unaffected(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path, admin_users={"root"})
        url_check = HeartbeatCheck(
            name="t", type="url-health", config={"url": ""},
        )
        result = run_check(url_check, config, "alice")
        # Fails for "no URL" reasons, not for admin-only — distinct path.
        assert "admin-only" not in result.message

    def test_empty_admin_users_treats_all_as_admin(self, tmp_path):
        """Back-compat: empty admin_users = all users admin (Config.is_admin)."""
        config = Config(nextcloud_mount_path=tmp_path)
        check = HeartbeatCheck(
            name="t", type="shell-command", config={"command": "echo ok"},
        )
        result = run_check(check, config, "alice")
        assert result.healthy is True


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
            nextcloud_mount_path=mount,
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
        """The disclosure boundary `cmd_check` draws, drawn here too.

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

        assert load_heartbeat_config(Config(nextcloud_mount_path=mount), "alice") is None

    def test_a_fifo_at_heartbeat_md_does_not_block_the_scheduler(self, tmp_path):
        from .support.blocking import fails_if_it_blocks

        mount, d = self._config_dir(tmp_path)
        os.mkfifo(d / "HEARTBEAT.md")
        config = Config(nextcloud_mount_path=mount)
        with fails_if_it_blocks(what="load_heartbeat_config"):
            assert load_heartbeat_config(config, "alice") is None

    def test_an_ordinary_heartbeat_md_still_parses(self, tmp_path):
        mount, d = self._config_dir(tmp_path)
        (d / "HEARTBEAT.md").write_text(
            '```toml\n[[checks]]\nname = "site"\ntype = "url-health"\nurl = "http://x"\n```\n'
        )
        result = load_heartbeat_config(Config(nextcloud_mount_path=mount), "alice")
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
