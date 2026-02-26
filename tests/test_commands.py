"""Tests for !command dispatch system."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from istota import db
from istota.commands import (
    _build_export_metadata, _filter_user_messages, _format_messages_markdown,
    _format_messages_text, _format_utilization, _parse_export_metadata,
    _read_claude_oauth_token,
    cmd_check, cmd_cron, cmd_export, cmd_help, cmd_memory, cmd_status, cmd_stop, cmd_usage,
    dispatch, parse_command,
)
from istota.config import Config, NextcloudConfig, SchedulerConfig, SecurityConfig, TalkConfig, UserConfig


@pytest.fixture
def db_path(tmp_path):
    """Create and initialize a temporary SQLite database."""
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def make_config(db_path, tmp_path):
    """Create a Config object with tmp paths and test DB."""

    def _make(**overrides):
        config = Config()
        config.db_path = db_path
        config.temp_dir = tmp_path / "temp"
        config.temp_dir.mkdir(exist_ok=True)
        config.skills_dir = tmp_path / "skills"
        config.skills_dir.mkdir(exist_ok=True)
        config.talk = TalkConfig(enabled=True, bot_username="istota")
        config.nextcloud = NextcloudConfig(
            url="https://nc.test", username="istota", app_password="pass"
        )
        config.users = {"alice": UserConfig()}
        config.scheduler = SchedulerConfig()
        config.nextcloud_mount_path = tmp_path / "mount"
        (config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config").mkdir(
            parents=True, exist_ok=True
        )
        (config.nextcloud_mount_path / "Channels" / "room1").mkdir(
            parents=True, exist_ok=True
        )
        for key, val in overrides.items():
            setattr(config, key, val)
        return config

    return _make


# =============================================================================
# TestParseCommand
# =============================================================================


class TestParseCommand:
    def test_basic_command(self):
        assert parse_command("!stop") == ("stop", "")

    def test_command_with_args(self):
        assert parse_command("!status foo bar") == ("status", "foo bar")

    def test_case_insensitive(self):
        assert parse_command("!HELP") == ("help", "")
        assert parse_command("!Stop") == ("stop", "")

    def test_not_a_command(self):
        assert parse_command("hello world") is None

    def test_empty_string(self):
        assert parse_command("") is None

    def test_just_exclamation(self):
        assert parse_command("!") is None

    def test_exclamation_space(self):
        assert parse_command("! space") is None

    def test_leading_whitespace(self):
        assert parse_command("  !help") == ("help", "")

    def test_multiline_args(self):
        result = parse_command("!cmd line1\nline2")
        assert result == ("cmd", "line1\nline2")


# =============================================================================
# TestDispatch
# =============================================================================


class TestDispatch:
    @pytest.mark.asyncio
    async def test_non_command_returns_false(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            result = await dispatch(config, conn, "alice", "room1", "hello world")
        assert result is False

    @pytest.mark.asyncio
    async def test_known_command_handled(self, make_config):
        config = make_config()
        with (
            db.get_db(config.db_path) as conn,
            patch("istota.commands.TalkClient") as MockClient,
        ):
            mock_instance = MockClient.return_value
            mock_instance.send_message = AsyncMock()
            result = await dispatch(config, conn, "alice", "room1", "!help")

        assert result is True
        mock_instance.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_command_posts_error(self, make_config):
        config = make_config()
        with (
            db.get_db(config.db_path) as conn,
            patch("istota.commands.TalkClient") as MockClient,
        ):
            mock_instance = MockClient.return_value
            mock_instance.send_message = AsyncMock()
            result = await dispatch(config, conn, "alice", "room1", "!nonexistent")

        assert result is True
        msg = mock_instance.send_message.call_args[0][1]
        assert "Unknown command" in msg
        assert "!nonexistent" in msg
        assert "!help" in msg


# =============================================================================
# TestCmdHelp
# =============================================================================


class TestCmdHelp:
    @pytest.mark.asyncio
    async def test_lists_all_commands(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_help(config, conn, "alice", "room1", "", client)

        assert "!help" in result
        assert "!stop" in result
        assert "!status" in result
        assert "!memory" in result


# =============================================================================
# TestCmdStop
# =============================================================================


class TestCmdStop:
    @pytest.mark.asyncio
    async def test_no_active_task(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_stop(config, conn, "alice", "room1", "", client)
        assert "No active task" in result

    @pytest.mark.asyncio
    async def test_cancels_running_task(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn,
                prompt="Do something long",
                user_id="alice",
                source_type="talk",
                conversation_token="room1",
            )
            db.update_task_status(conn, task_id, "running")

            client = AsyncMock()
            result = await cmd_stop(config, conn, "alice", "room1", "", client)

        assert f"#{task_id}" in result
        assert "Cancelling" in result

        with db.get_db(config.db_path) as conn:
            assert db.is_task_cancelled(conn, task_id) is True

    @pytest.mark.asyncio
    async def test_cancels_pending_confirmation(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn,
                prompt="Do risky thing",
                user_id="alice",
                source_type="talk",
                conversation_token="room1",
            )
            db.set_task_confirmation(conn, task_id, "Are you sure?")

            client = AsyncMock()
            result = await cmd_stop(config, conn, "alice", "room1", "", client)

        assert f"#{task_id}" in result

    @pytest.mark.asyncio
    async def test_only_cancels_own_tasks(self, make_config):
        config = make_config()
        config.users["bob"] = UserConfig()

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn,
                prompt="Bob's task",
                user_id="bob",
                source_type="talk",
                conversation_token="room1",
            )
            db.update_task_status(conn, task_id, "running")

            client = AsyncMock()
            result = await cmd_stop(config, conn, "alice", "room1", "", client)

        assert "No active task" in result

        with db.get_db(config.db_path) as conn:
            assert db.is_task_cancelled(conn, task_id) is False


# =============================================================================
# TestCmdStatus
# =============================================================================


class TestCmdStatus:
    @pytest.mark.asyncio
    async def test_no_tasks(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_status(config, conn, "alice", "room1", "", client)
        assert "No active or pending tasks" in result
        assert "System:" in result

    @pytest.mark.asyncio
    async def test_shows_user_tasks(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            t1 = db.create_task(
                conn,
                prompt="Task one",
                user_id="alice",
                source_type="talk",
            )
            t2 = db.create_task(
                conn,
                prompt="Task two",
                user_id="alice",
                source_type="talk",
            )
            db.update_task_status(conn, t2, "running")

            client = AsyncMock()
            result = await cmd_status(config, conn, "alice", "room1", "", client)

        assert "Your tasks (2)" in result
        assert "Task one" in result
        assert "Task two" in result
        assert "[running]" in result

    @pytest.mark.asyncio
    async def test_excludes_other_users(self, make_config):
        config = make_config()
        config.users["bob"] = UserConfig()

        with db.get_db(config.db_path) as conn:
            db.create_task(
                conn,
                prompt="Bob's task",
                user_id="bob",
                source_type="talk",
            )

            client = AsyncMock()
            result = await cmd_status(config, conn, "alice", "room1", "", client)

        assert "No active or pending tasks" in result
        # But system stats should show bob's pending task
        assert "1 queued" in result

    @pytest.mark.asyncio
    async def test_system_stats(self, make_config):
        config = make_config()
        config.users["bob"] = UserConfig()

        with db.get_db(config.db_path) as conn:
            t1 = db.create_task(
                conn, prompt="Running", user_id="bob", source_type="talk"
            )
            db.update_task_status(conn, t1, "running")
            db.create_task(
                conn, prompt="Pending", user_id="alice", source_type="talk"
            )

            client = AsyncMock()
            result = await cmd_status(config, conn, "alice", "room1", "", client)

        assert "1 running" in result
        assert "1 queued" in result

    @pytest.mark.asyncio
    async def test_groups_interactive_and_background(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.create_task(conn, prompt="Talk task", user_id="alice", source_type="talk")
            db.create_task(conn, prompt="Scheduled job", user_id="alice", source_type="scheduled")
            db.create_task(conn, prompt="Briefing", user_id="alice", source_type="briefing")

            client = AsyncMock()
            result = await cmd_status(config, conn, "alice", "room1", "", client)

        assert "Your tasks (1)" in result
        assert "Talk task" in result
        assert "Background (2)" in result
        assert "[scheduled]" in result

    @pytest.mark.asyncio
    async def test_only_background_tasks(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.create_task(conn, prompt="Cron job", user_id="alice", source_type="scheduled")

            client = AsyncMock()
            result = await cmd_status(config, conn, "alice", "room1", "", client)

        assert "Background (1)" in result
        assert "Your tasks" not in result


# =============================================================================
# TestCmdCron
# =============================================================================


class TestCmdCron:
    @pytest.mark.asyncio
    async def test_no_jobs(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_cron(config, conn, "alice", "room1", "", client)
        assert "No scheduled jobs" in result

    @pytest.mark.asyncio
    async def test_list_jobs(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "daily-check", "0 9 * * *", "check stuff"),
            )
            client = AsyncMock()
            result = await cmd_cron(config, conn, "alice", "room1", "", client)

        assert "daily-check" in result
        assert "0 9 * * *" in result
        assert "enabled" in result

    @pytest.mark.asyncio
    async def test_list_shows_failures(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled, consecutive_failures)
                   VALUES (?, ?, ?, ?, 1, 3)""",
                ("alice", "flaky", "0 * * * *", "flaky job"),
            )
            client = AsyncMock()
            result = await cmd_cron(config, conn, "alice", "room1", "", client)

        assert "3 failures" in result

    @pytest.mark.asyncio
    async def test_enable_job_updates_file_and_db(self, make_config):
        config = make_config()
        # Write CRON.md with disabled job
        cron_path = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "CRON.md"
        cron_path.write_text("""\
# Scheduled Jobs

```toml
[[jobs]]
name = "broken"
cron = "0 * * * *"
prompt = "stuff"
enabled = false
```
""")
        with db.get_db(config.db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled, consecutive_failures)
                   VALUES (?, ?, ?, ?, 0, 5)""",
                ("alice", "broken", "0 * * * *", "stuff"),
            )
            client = AsyncMock()
            result = await cmd_cron(config, conn, "alice", "room1", "enable broken", client)

        assert "Enabled" in result
        assert "DB-only" not in result
        # DB updated
        with db.get_db(config.db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "broken")
            assert job.enabled is True
            assert job.consecutive_failures == 0
        # File updated
        from istota.cron_loader import load_cron_jobs
        jobs = load_cron_jobs(config, "alice")
        assert jobs[0].enabled is True

    @pytest.mark.asyncio
    async def test_enable_job_resets_last_run_at(self, make_config):
        """Enabling a job resets last_run_at so it won't fire immediately as catch-up."""
        config = make_config()
        cron_path = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "CRON.md"
        cron_path.write_text("""\
# Scheduled Jobs

```toml
[[jobs]]
name = "nightly"
cron = "0 22 * * *"
prompt = "stuff"
enabled = false
```
""")
        with db.get_db(config.db_path) as conn:
            # Insert job with an old last_run_at (simulating a job disabled long ago)
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled, last_run_at)
                   VALUES (?, ?, ?, ?, 0, '2026-01-01 00:00:00')""",
                ("alice", "nightly", "0 22 * * *", "stuff"),
            )
            client = AsyncMock()
            result = await cmd_cron(config, conn, "alice", "room1", "enable nightly", client)

        assert "Enabled" in result
        with db.get_db(config.db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "nightly")
            assert job.enabled is True
            # last_run_at must have been reset — not the old 2026-01-01 value
            assert job.last_run_at is not None
            assert "2026-01-01" not in job.last_run_at

    @pytest.mark.asyncio
    async def test_disable_job_updates_file_and_db(self, make_config):
        config = make_config()
        # Write CRON.md with enabled job
        cron_path = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "CRON.md"
        cron_path.write_text("""\
# Scheduled Jobs

```toml
[[jobs]]
name = "active-job"
cron = "0 * * * *"
prompt = "stuff"
```
""")
        with db.get_db(config.db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                ("alice", "active-job", "0 * * * *", "stuff"),
            )
            client = AsyncMock()
            result = await cmd_cron(config, conn, "alice", "room1", "disable active-job", client)

        assert "Disabled" in result
        assert "DB-only" not in result
        # DB updated
        with db.get_db(config.db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "active-job")
            assert job.enabled is False
        # File updated
        from istota.cron_loader import load_cron_jobs
        jobs = load_cron_jobs(config, "alice")
        assert jobs[0].enabled is False

    @pytest.mark.asyncio
    async def test_enable_without_cron_file_warns(self, make_config):
        """Without CRON.md, enable falls back to DB-only with a warning."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled, consecutive_failures)
                   VALUES (?, ?, ?, ?, 0, 5)""",
                ("alice", "broken", "0 * * * *", "stuff"),
            )
            client = AsyncMock()
            result = await cmd_cron(config, conn, "alice", "room1", "enable broken", client)

        assert "Enabled" in result
        assert "DB-only" in result
        with db.get_db(config.db_path) as conn:
            job = db.get_scheduled_job_by_name(conn, "alice", "broken")
            assert job.enabled is True

    @pytest.mark.asyncio
    async def test_enable_nonexistent(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_cron(config, conn, "alice", "room1", "enable nope", client)
        assert "not found" in result or "No scheduled job" in result


# =============================================================================
# TestCmdMemory
# =============================================================================


class TestCmdMemory:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_memory(config, conn, "alice", "room1", "", client)
        assert "!memory user" in result
        assert "!memory channel" in result

    @pytest.mark.asyncio
    async def test_user_memory_empty(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_memory(config, conn, "alice", "room1", "user", client)
        assert "User memory:** (empty)" in result

    @pytest.mark.asyncio
    async def test_user_memory_with_content(self, make_config):
        config = make_config()
        user_mem_path = (
            config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "USER.md"
        )
        user_mem_path.write_text("Alice likes coffee")

        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_memory(config, conn, "alice", "room1", "user", client)
        assert "Alice likes coffee" in result
        assert "User memory**" in result

    @pytest.mark.asyncio
    async def test_user_memory_not_truncated(self, make_config):
        config = make_config()
        user_mem_path = (
            config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "USER.md"
        )
        long_content = "A" * 5000
        user_mem_path.write_text(long_content)

        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_memory(config, conn, "alice", "room1", "user", client)
        # Full content should be present, not truncated
        assert long_content in result

    @pytest.mark.asyncio
    async def test_channel_memory_empty(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_memory(config, conn, "alice", "room1", "channel", client)
        assert "Channel memory:** (empty)" in result

    @pytest.mark.asyncio
    async def test_channel_memory_with_content(self, make_config):
        config = make_config()
        channel_mem_path = (
            config.nextcloud_mount_path / "Channels" / "room1" / "CHANNEL.md"
        )
        channel_mem_path.write_text("This is the dev channel")

        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_memory(config, conn, "alice", "room1", "channel", client)
        assert "This is the dev channel" in result
        assert "Channel memory**" in result

    @pytest.mark.asyncio
    async def test_no_mount_configured(self, make_config):
        config = make_config()
        config.nextcloud_mount_path = None

        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_memory(config, conn, "alice", "room1", "user", client)
        assert "mount not configured" in result


# =============================================================================
# TestDbHelpers
# =============================================================================


class TestDbHelpers:
    def test_update_task_pid(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="test", user_id="alice", source_type="cli"
            )
            db.update_task_pid(conn, task_id, 12345)
            row = conn.execute(
                "SELECT worker_pid FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            assert row[0] == 12345

    def test_is_task_cancelled_false(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="test", user_id="alice", source_type="cli"
            )
            assert db.is_task_cancelled(conn, task_id) is False

    def test_is_task_cancelled_true(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="test", user_id="alice", source_type="cli"
            )
            conn.execute(
                "UPDATE tasks SET cancel_requested = 1 WHERE id = ?", (task_id,)
            )
            assert db.is_task_cancelled(conn, task_id) is True


# =============================================================================
# TestPollerInterception
# =============================================================================


class TestPollerInterception:
    """Test that !commands are intercepted in the Talk poller and don't create tasks."""

    @pytest.mark.asyncio
    async def test_command_does_not_create_task(self, make_config):
        from istota.talk_poller import poll_talk_conversations

        config = make_config()

        msg = {
            "id": 101,
            "actorId": "alice",
            "actorType": "users",
            "message": "!status",
            "messageType": "comment",
            "messageParameters": {},
        }

        with patch("istota.talk_poller.TalkClient") as MockTalkClient, patch(
            "istota.commands.TalkClient"
        ) as MockCmdClient:
            # Talk poller client
            mock_talk = MockTalkClient.return_value
            mock_talk.list_conversations = AsyncMock(
                return_value=[{"token": "room1", "type": 1}]
            )
            mock_talk.poll_messages = AsyncMock(return_value=[msg])

            # Command dispatcher client
            mock_cmd = MockCmdClient.return_value
            mock_cmd.send_message = AsyncMock()

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        # No tasks should have been created
        assert result == []

        # Command should have posted a response
        mock_cmd.send_message.assert_called_once()
        sent_msg = mock_cmd.send_message.call_args[0][1]
        assert "System:" in sent_msg  # !status output

    @pytest.mark.asyncio
    async def test_normal_message_still_creates_task(self, make_config):
        from istota.talk_poller import poll_talk_conversations

        config = make_config()

        msg = {
            "id": 102,
            "actorId": "alice",
            "actorType": "users",
            "message": "What's the weather?",
            "messageType": "comment",
            "messageParameters": {},
        }

        with patch("istota.talk_poller.TalkClient") as MockTalkClient:
            mock_talk = MockTalkClient.return_value
            mock_talk.list_conversations = AsyncMock(
                return_value=[{"token": "room1", "type": 1}]
            )
            mock_talk.poll_messages = AsyncMock(return_value=[msg])

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert len(result) == 1


# =============================================================================
# TestCmdCheck
# =============================================================================


class TestCmdCheck:
    @pytest.mark.asyncio
    async def test_all_pass_no_sandbox(self, make_config):
        """All fast checks pass, sandbox disabled, Claude execution passes."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            # Create a completed task in the last hour
            t = db.create_task(conn, prompt="test", user_id="alice", source_type="cli")
            db.update_task_status(conn, t, "completed")

            client = AsyncMock()
            with (
                patch("istota.commands.shutil.which", return_value="/usr/local/bin/claude"),
                patch("istota.commands.subprocess.run") as mock_run,
            ):
                # First call: claude --version; Second call: Claude execution
                mock_run.side_effect = [
                    MagicMock(stdout="claude 1.0.0", stderr="", returncode=0),
                    MagicMock(stdout="healthcheck-ok", stderr="", returncode=0),
                ]
                result = await cmd_check(config, conn, "alice", "room1", "", client)

        assert "Claude binary: PASS" in result
        assert "Sandbox: skipped" in result
        assert "Database: PASS" in result
        assert "Recent tasks (1h):" in result
        assert "1 completed" in result
        assert "Claude + Bash: PASS" in result

    @pytest.mark.asyncio
    async def test_claude_not_found(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            with (
                patch("istota.commands.shutil.which", return_value=None),
                patch("istota.commands.subprocess.run") as mock_run,
            ):
                # Only the execution check runs subprocess
                mock_run.return_value = MagicMock(
                    stdout="healthcheck-ok", stderr="", returncode=0,
                )
                result = await cmd_check(config, conn, "alice", "room1", "", client)

        assert "Claude binary: **FAIL**" in result
        assert "not found in PATH" in result

    @pytest.mark.asyncio
    async def test_sandbox_enabled_bwrap_found(self, make_config):
        config = make_config()
        config.security = SecurityConfig(sandbox_enabled=True)

        with db.get_db(config.db_path) as conn:
            client = AsyncMock()

            def which_side_effect(name):
                return f"/usr/bin/{name}"

            with (
                patch("istota.commands.shutil.which", side_effect=which_side_effect),
                patch("istota.commands.subprocess.run") as mock_run,
            ):
                mock_run.side_effect = [
                    MagicMock(stdout="claude 1.0.0", stderr="", returncode=0),
                    MagicMock(stdout="bubblewrap 0.8.0", stderr="", returncode=0),
                    MagicMock(stdout="healthcheck-ok", stderr="", returncode=0),
                ]
                result = await cmd_check(config, conn, "alice", "room1", "", client)

        assert "Sandbox (bwrap): PASS" in result

    @pytest.mark.asyncio
    async def test_sandbox_enabled_bwrap_missing(self, make_config):
        config = make_config()
        config.security = SecurityConfig(sandbox_enabled=True)

        with db.get_db(config.db_path) as conn:
            client = AsyncMock()

            def which_side_effect(name):
                if name == "bwrap":
                    return None
                return f"/usr/bin/{name}"

            with (
                patch("istota.commands.shutil.which", side_effect=which_side_effect),
                patch("istota.commands.subprocess.run") as mock_run,
            ):
                mock_run.side_effect = [
                    MagicMock(stdout="claude 1.0.0", stderr="", returncode=0),
                    MagicMock(stdout="healthcheck-ok", stderr="", returncode=0),
                ]
                result = await cmd_check(config, conn, "alice", "room1", "", client)

        assert "Sandbox (bwrap): **FAIL**" in result

    @pytest.mark.asyncio
    async def test_execution_timeout(self, make_config):
        import subprocess as real_subprocess

        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()

            def run_side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                if cmd and cmd[0] == "claude" and "-p" in cmd:
                    raise real_subprocess.TimeoutExpired(cmd, 30)
                return MagicMock(stdout="claude 1.0.0", stderr="", returncode=0)

            with (
                patch("istota.commands.shutil.which", return_value="/usr/bin/claude"),
                patch("istota.commands.subprocess.run", side_effect=run_side_effect),
            ):
                result = await cmd_check(config, conn, "alice", "room1", "", client)

        assert "timed out" in result

    @pytest.mark.asyncio
    async def test_execution_wrong_output(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            with (
                patch("istota.commands.shutil.which", return_value="/usr/bin/claude"),
                patch("istota.commands.subprocess.run") as mock_run,
            ):
                mock_run.side_effect = [
                    MagicMock(stdout="claude 1.0.0", stderr="", returncode=0),
                    MagicMock(stdout="something else", stderr="error msg", returncode=1),
                ]
                result = await cmd_check(config, conn, "alice", "room1", "", client)

        assert "Claude + Bash: **FAIL**" in result
        assert "stderr: error msg" in result

    @pytest.mark.asyncio
    async def test_high_failure_rate_warning(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            # Create more failures than successes
            for _ in range(3):
                t = db.create_task(conn, prompt="fail", user_id="alice", source_type="cli")
                db.update_task_status(conn, t, "failed", error="boom")
            t = db.create_task(conn, prompt="ok", user_id="alice", source_type="cli")
            db.update_task_status(conn, t, "completed")

            client = AsyncMock()
            with (
                patch("istota.commands.shutil.which", return_value="/usr/bin/claude"),
                patch("istota.commands.subprocess.run") as mock_run,
            ):
                mock_run.side_effect = [
                    MagicMock(stdout="claude 1.0.0", stderr="", returncode=0),
                    MagicMock(stdout="healthcheck-ok", stderr="", returncode=0),
                ]
                result = await cmd_check(config, conn, "alice", "room1", "", client)

        assert "3 failed" in result
        assert "warning: high failure rate" in result

    @pytest.mark.asyncio
    async def test_help_includes_check(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_help(config, conn, "alice", "room1", "", client)
        assert "!check" in result


# =============================================================================
# TestCmdUsage
# =============================================================================


class TestReadClaudeOauthToken:
    def test_file_missing(self, tmp_path):
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        with patch("istota.commands.Path.home", return_value=fake_home):
            assert _read_claude_oauth_token() is None

    def test_invalid_json(self, tmp_path):
        fake_home = tmp_path / "fake_home"
        creds_dir = fake_home / ".claude"
        creds_dir.mkdir(parents=True)
        (creds_dir / ".credentials.json").write_text("not json")
        with patch("istota.commands.Path.home", return_value=fake_home):
            assert _read_claude_oauth_token() is None

    def test_missing_oauth_section(self, tmp_path):
        fake_home = tmp_path / "fake_home"
        creds_dir = fake_home / ".claude"
        creds_dir.mkdir(parents=True)
        (creds_dir / ".credentials.json").write_text('{"other": "stuff"}')
        with patch("istota.commands.Path.home", return_value=fake_home):
            assert _read_claude_oauth_token() is None

    def test_valid_token(self, tmp_path):
        import json as json_mod
        fake_home = tmp_path / "fake_home"
        creds_dir = fake_home / ".claude"
        creds_dir.mkdir(parents=True)
        creds = {"claudeAiOauth": {"accessToken": "sk-ant-oat01-test123"}}
        (creds_dir / ".credentials.json").write_text(json_mod.dumps(creds))
        with patch("istota.commands.Path.home", return_value=fake_home):
            assert _read_claude_oauth_token() == "sk-ant-oat01-test123"


class TestFormatUtilization:
    def test_zero_utilization(self):
        result = _format_utilization("5-hour", {"utilization": 0, "resets_at": None})
        assert "0%" in result
        assert "5-hour" in result
        assert "resets" not in result

    def test_half_utilization(self):
        result = _format_utilization("7-day", {"utilization": 50, "resets_at": None})
        assert "50%" in result

    def test_full_utilization_with_reset_utc(self):
        result = _format_utilization("5-hour", {"utilization": 100, "resets_at": "2026-02-11T15:00:00+00:00"})
        assert "100%" in result
        assert "resets Feb 11 15:00" in result

    def test_reset_in_user_timezone(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        result = _format_utilization("5-hour", {"utilization": 50, "resets_at": "2026-02-11T23:00:00+00:00"}, tz=tz)
        # 23:00 UTC = 18:00 EST
        assert "resets Feb 11 18:00" in result

    def test_bar_rendering(self):
        result = _format_utilization("test", {"utilization": 25, "resets_at": None})
        # 25% of 20 chars = 5 filled
        assert "#####---------------" in result

    def test_bar_capped_at_100(self):
        """Utilization above 100 should not overflow the bar."""
        result = _format_utilization("test", {"utilization": 150, "resets_at": None})
        assert "####################" in result
        assert len(result.split("[")[1].split("]")[0]) == 20


class TestCmdUsage:
    def _write_creds(self, tmp_path, token="sk-ant-oat01-test"):
        import json as json_mod
        fake_home = tmp_path / "fake_home"
        creds_dir = fake_home / ".claude"
        creds_dir.mkdir(parents=True)
        creds = {"claudeAiOauth": {"accessToken": token}}
        (creds_dir / ".credentials.json").write_text(json_mod.dumps(creds))
        return fake_home

    @pytest.mark.asyncio
    async def test_no_token_returns_error(self, make_config, tmp_path):
        config = make_config()
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            with patch("istota.commands.Path.home", return_value=fake_home):
                result = await cmd_usage(config, conn, "alice", "room1", "", client)
        assert "Error" in result
        assert ".credentials.json" in result

    @pytest.mark.asyncio
    async def test_successful_usage(self, make_config, tmp_path):
        config = make_config()
        fake_home = self._write_creds(tmp_path)

        api_response = {
            "five_hour": {"utilization": 33, "resets_at": "2026-02-11T23:00:00+00:00"},
            "seven_day": {"utilization": 97, "resets_at": "2026-02-12T21:00:00+00:00"},
            "seven_day_oauth_apps": None,
            "seven_day_opus": None,
            "seven_day_sonnet": {"utilization": 9, "resets_at": "2026-02-13T12:00:00+00:00"},
            "seven_day_cowork": None,
            "iguana_necktie": None,
            "extra_usage": {
                "is_enabled": True,
                "monthly_limit": 2000,
                "used_credits": 792.0,
                "utilization": 39.6,
            },
        }

        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            with (
                patch("istota.commands.Path.home", return_value=fake_home),
                patch("istota.commands.httpx.AsyncClient") as MockClient,
            ):
                mock_http = MockClient.return_value.__aenter__.return_value
                mock_http.get = AsyncMock(return_value=MagicMock(
                    json=lambda: api_response, raise_for_status=lambda: None,
                ))
                result = await cmd_usage(config, conn, "alice", "room1", "", client)

        assert "Claude Code Usage" in result
        assert "5-hour" in result
        assert "33%" in result
        assert "7-day" in result
        assert "97%" in result
        assert "sonnet" in result
        assert "9%" in result
        assert "$7.92 / $20.00 (40%)" in result

    @pytest.mark.asyncio
    async def test_http_error(self, make_config, tmp_path):
        import httpx

        config = make_config()
        fake_home = self._write_creds(tmp_path)

        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            with (
                patch("istota.commands.Path.home", return_value=fake_home),
                patch("istota.commands.httpx.AsyncClient") as MockClient,
            ):
                mock_http = MockClient.return_value.__aenter__.return_value
                error_response = MagicMock(status_code=401, text="Unauthorized")
                mock_http.get = AsyncMock(side_effect=httpx.HTTPStatusError(
                    "401", request=MagicMock(), response=error_response,
                ))
                result = await cmd_usage(config, conn, "alice", "room1", "", client)

        assert "Error" in result
        assert "401" in result

    @pytest.mark.asyncio
    async def test_empty_response(self, make_config, tmp_path):
        """All buckets are null — should show fallback message."""
        config = make_config()
        fake_home = self._write_creds(tmp_path)

        api_response = {
            "five_hour": None,
            "seven_day": None,
            "seven_day_opus": None,
            "seven_day_oauth_apps": None,
            "iguana_necktie": None,
        }

        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            with (
                patch("istota.commands.Path.home", return_value=fake_home),
                patch("istota.commands.httpx.AsyncClient") as MockClient,
            ):
                mock_http = MockClient.return_value.__aenter__.return_value
                mock_http.get = AsyncMock(return_value=MagicMock(
                    json=lambda: api_response, raise_for_status=lambda: None,
                ))
                result = await cmd_usage(config, conn, "alice", "room1", "", client)

        assert "No usage data" in result

    @pytest.mark.asyncio
    async def test_help_includes_usage(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_help(config, conn, "alice", "room1", "", client)
        assert "!usage" in result


# =============================================================================
# TestExportHelpers
# =============================================================================


class TestParseExportMetadata:
    def test_markdown_format(self):
        line = "<!-- export:token=abc123,last_id=42,updated=2026-02-25T14:45:00Z -->"
        result = _parse_export_metadata(line)
        assert result == {"token": "abc123", "last_id": 42, "updated": "2026-02-25T14:45:00Z"}

    def test_text_format(self):
        line = "# export:token=room1,last_id=100,updated=2026-02-25T14:45:00Z"
        result = _parse_export_metadata(line)
        assert result == {"token": "room1", "last_id": 100, "updated": "2026-02-25T14:45:00Z"}

    def test_invalid_line(self):
        assert _parse_export_metadata("# Just a heading") is None
        assert _parse_export_metadata("") is None

    def test_with_leading_whitespace(self):
        line = "  <!-- export:token=t,last_id=1,updated=2026-01-01T00:00:00Z -->"
        result = _parse_export_metadata(line)
        assert result is not None
        assert result["token"] == "t"


class TestBuildExportMetadata:
    def test_markdown_format(self):
        result = _build_export_metadata("room1", 42, "markdown")
        assert result.startswith("<!-- export:token=room1,last_id=42,updated=")
        assert result.endswith(" -->")

    def test_text_format(self):
        result = _build_export_metadata("room1", 42, "text")
        assert result.startswith("# export:token=room1,last_id=42,updated=")
        assert "-->" not in result


class TestFilterUserMessages:
    def test_filters_system_messages(self):
        messages = [
            {"id": 1, "actorType": "users", "messageType": "comment", "message": "hello"},
            {"id": 2, "actorType": "guests", "messageType": "system", "message": "joined"},
            {"id": 3, "actorType": "users", "messageType": "comment", "message": "bye"},
            {"id": 4, "actorType": "users", "messageType": "comment_deleted", "message": ""},
        ]
        result = _filter_user_messages(messages)
        assert [m["id"] for m in result] == [1, 3]

    def test_empty_list(self):
        assert _filter_user_messages([]) == []


class TestFormatMessagesMarkdown:
    def test_basic_messages(self):
        messages = [
            {"actorDisplayName": "Alice", "actorId": "alice", "timestamp": 1740000000, "message": "Hello", "messageParameters": {}},
            {"actorDisplayName": "Bob", "actorId": "bob", "timestamp": 1740000060, "message": "Hi there", "messageParameters": {}},
        ]
        result = _format_messages_markdown(messages)
        assert "**Alice**" in result
        assert "Hello" in result
        assert "**Bob**" in result
        assert "Hi there" in result
        assert "---" in result

    def test_coalescing(self):
        messages = [
            {"actorDisplayName": "Alice", "actorId": "alice", "timestamp": 1740000000, "message": "Line 1", "messageParameters": {}},
            {"actorDisplayName": "Alice", "actorId": "alice", "timestamp": 1740000010, "message": "Line 2", "messageParameters": {}},
            {"actorDisplayName": "Bob", "actorId": "bob", "timestamp": 1740000060, "message": "Reply", "messageParameters": {}},
        ]
        result = _format_messages_markdown(messages)
        # Alice should appear only once as a header
        assert result.count("**Alice**") == 1
        assert "Line 1" in result
        assert "Line 2" in result
        assert "**Bob**" in result

    def test_empty_messages(self):
        assert _format_messages_markdown([]) == ""


class TestFormatMessagesText:
    def test_basic_messages(self):
        messages = [
            {"actorDisplayName": "Alice", "actorId": "alice", "timestamp": 1740000000, "message": "Hello", "messageParameters": {}},
            {"actorDisplayName": "Bob", "actorId": "bob", "timestamp": 1740000060, "message": "Hi", "messageParameters": {}},
        ]
        result = _format_messages_text(messages)
        assert "Alice" in result
        assert "Hello" in result
        assert "Bob" in result
        assert "---" not in result  # plaintext doesn't use HR

    def test_coalescing(self):
        messages = [
            {"actorDisplayName": "Alice", "actorId": "alice", "timestamp": 1740000000, "message": "Line 1", "messageParameters": {}},
            {"actorDisplayName": "Alice", "actorId": "alice", "timestamp": 1740000010, "message": "Line 2", "messageParameters": {}},
        ]
        result = _format_messages_text(messages)
        # Alice header only once
        lines = result.split("\n")
        alice_headers = [l for l in lines if l.startswith("Alice")]
        assert len(alice_headers) == 1


# =============================================================================
# TestCmdExport
# =============================================================================


class TestCmdExport:
    def _make_messages(self, count=3, start_id=1):
        """Generate test messages."""
        messages = []
        for i in range(count):
            messages.append({
                "id": start_id + i,
                "actorType": "users",
                "actorId": f"user{i % 2}",
                "actorDisplayName": f"User{i % 2}",
                "messageType": "comment",
                "message": f"Message {start_id + i}",
                "messageParameters": {},
                "timestamp": 1740000000 + i * 60,
            })
        return messages

    @pytest.mark.asyncio
    async def test_no_mount_configured(self, make_config):
        config = make_config()
        config.nextcloud_mount_path = None
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_export(config, conn, "alice", "room1", "", client)
        assert "mount not configured" in result

    @pytest.mark.asyncio
    async def test_full_export_markdown(self, make_config):
        config = make_config()
        messages = self._make_messages(3)
        client = AsyncMock()
        client.fetch_full_history = AsyncMock(return_value=messages)
        client.get_conversation_info = AsyncMock(return_value={"displayName": "Test Room"})
        client.get_participants = AsyncMock(return_value=[
            {"actorType": "users", "actorId": "user0", "displayName": "User0"},
            {"actorType": "users", "actorId": "user1", "displayName": "User1"},
        ])

        with db.get_db(config.db_path) as conn:
            result = await cmd_export(config, conn, "alice", "room1", "", client)

        assert "Exported 3 messages" in result
        assert "room1.md" in result

        export_path = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "exports" / "conversations" / "room1.md"
        assert export_path.exists()
        content = export_path.read_text()
        assert "<!-- export:token=room1" in content
        assert "# Test Room" in content
        assert "**Participants:**" in content
        assert "User0" in content
        assert "Message 1" in content
        assert "Message 3" in content

    @pytest.mark.asyncio
    async def test_full_export_text(self, make_config):
        config = make_config()
        messages = self._make_messages(2)
        client = AsyncMock()
        client.fetch_full_history = AsyncMock(return_value=messages)
        client.get_conversation_info = AsyncMock(return_value={"displayName": "Test Room"})
        client.get_participants = AsyncMock(return_value=[])

        with db.get_db(config.db_path) as conn:
            result = await cmd_export(config, conn, "alice", "room1", "text", client)

        assert "Exported 2 messages" in result
        export_path = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "exports" / "conversations" / "room1.txt"
        assert export_path.exists()
        content = export_path.read_text()
        assert "# export:token=room1" in content
        assert "====" in content
        assert "---" not in content

    @pytest.mark.asyncio
    async def test_empty_channel(self, make_config):
        config = make_config()
        client = AsyncMock()
        client.fetch_full_history = AsyncMock(return_value=[])

        with db.get_db(config.db_path) as conn:
            result = await cmd_export(config, conn, "alice", "room1", "", client)

        assert "No messages to export" in result

    @pytest.mark.asyncio
    async def test_filters_system_messages(self, make_config):
        config = make_config()
        messages = [
            {"id": 1, "actorType": "users", "actorId": "alice", "actorDisplayName": "Alice",
             "messageType": "comment", "message": "Hello", "messageParameters": {}, "timestamp": 1740000000},
            {"id": 2, "actorType": "guests", "actorId": "system", "actorDisplayName": "System",
             "messageType": "system", "message": "joined", "messageParameters": {}, "timestamp": 1740000010},
        ]
        client = AsyncMock()
        client.fetch_full_history = AsyncMock(return_value=messages)
        client.get_conversation_info = AsyncMock(return_value={"displayName": "Room"})
        client.get_participants = AsyncMock(return_value=[])

        with db.get_db(config.db_path) as conn:
            result = await cmd_export(config, conn, "alice", "room1", "", client)

        assert "Exported 1 messages" in result

    @pytest.mark.asyncio
    async def test_incremental_export(self, make_config):
        config = make_config()

        # First do a full export
        messages = self._make_messages(2, start_id=1)
        client = AsyncMock()
        client.fetch_full_history = AsyncMock(return_value=messages)
        client.get_conversation_info = AsyncMock(return_value={"displayName": "Room"})
        client.get_participants = AsyncMock(return_value=[])

        with db.get_db(config.db_path) as conn:
            await cmd_export(config, conn, "alice", "room1", "", client)

        export_path = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "exports" / "conversations" / "room1.md"
        original_content = export_path.read_text()

        # Now do incremental export
        new_messages = self._make_messages(2, start_id=3)
        client.fetch_messages_since = AsyncMock(return_value=new_messages)

        with db.get_db(config.db_path) as conn:
            result = await cmd_export(config, conn, "alice", "room1", "", client)

        assert "Appended 2 new messages" in result
        updated_content = export_path.read_text()
        assert "Message 3" in updated_content
        assert "Message 4" in updated_content
        # Original messages should still be there
        assert "Message 1" in updated_content
        # Metadata should be updated with new last_id
        meta = _parse_export_metadata(updated_content.split("\n")[0])
        assert meta["last_id"] == 4

    @pytest.mark.asyncio
    async def test_incremental_no_new_messages(self, make_config):
        config = make_config()

        # Set up existing export file
        export_dir = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "exports" / "conversations"
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / "room1.md"
        export_path.write_text("<!-- export:token=room1,last_id=10,updated=2026-02-25T00:00:00Z -->\n\n# Room\n")

        client = AsyncMock()
        client.fetch_messages_since = AsyncMock(return_value=[])

        with db.get_db(config.db_path) as conn:
            result = await cmd_export(config, conn, "alice", "room1", "", client)

        assert "No new messages" in result

    @pytest.mark.asyncio
    async def test_format_aliases(self, make_config):
        """txt and plaintext should also work as format arguments."""
        config = make_config()
        messages = self._make_messages(1)
        client = AsyncMock()
        client.fetch_full_history = AsyncMock(return_value=messages)
        client.get_conversation_info = AsyncMock(return_value={"displayName": "R"})
        client.get_participants = AsyncMock(return_value=[])

        export_path = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "exports" / "conversations" / "room1.txt"
        for fmt_arg in ("txt", "plaintext", "text"):
            # Remove existing file to avoid incremental path
            if export_path.exists():
                export_path.unlink()
            with db.get_db(config.db_path) as conn:
                result = await cmd_export(config, conn, "alice", "room1", fmt_arg, client)
            assert "room1.txt" in result

    @pytest.mark.asyncio
    async def test_help_includes_export(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_help(config, conn, "alice", "room1", "", client)
        assert "!export" in result

    @pytest.mark.asyncio
    async def test_different_format_creates_separate_file(self, make_config):
        """If existing export is .md but user asks for text, it creates .txt (new export)."""
        config = make_config()

        # Create existing markdown export
        export_dir = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "exports" / "conversations"
        export_dir.mkdir(parents=True, exist_ok=True)
        (export_dir / "room1.md").write_text("<!-- export:token=room1,last_id=10,updated=2026-02-25T00:00:00Z -->\n")

        messages = self._make_messages(1)
        client = AsyncMock()
        client.fetch_full_history = AsyncMock(return_value=messages)
        client.get_conversation_info = AsyncMock(return_value={"displayName": "R"})
        client.get_participants = AsyncMock(return_value=[])

        with db.get_db(config.db_path) as conn:
            result = await cmd_export(config, conn, "alice", "room1", "text", client)

        # Should create a new .txt file, not append to .md
        assert "room1.txt" in result
        assert "Exported 1 messages" in result
        assert (export_dir / "room1.txt").exists()
        assert (export_dir / "room1.md").exists()  # original still there
