"""Tests for !command dispatch system."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from istota import db
from istota.commands import (
    _build_export_metadata, _filter_user_messages, _format_messages_markdown,
    _format_messages_text, _parse_export_metadata, _parse_search_args,
    cmd_check, cmd_cron, cmd_export, cmd_help, cmd_memory, cmd_more, cmd_search,
    cmd_skills, cmd_status, cmd_stop,
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
    async def test_system_stats_hidden_for_non_admin(self, make_config):
        config = make_config()
        config.users["bob"] = UserConfig()
        # Non-empty admin_users with alice excluded → alice is non-admin
        config.admin_users = {"someone_else"}

        with db.get_db(config.db_path) as conn:
            t1 = db.create_task(
                conn, prompt="Running", user_id="bob", source_type="talk"
            )
            db.update_task_status(conn, t1, "running")
            db.create_task(
                conn, prompt="Pending", user_id="bob", source_type="talk"
            )

            client = AsyncMock()
            result = await cmd_status(config, conn, "alice", "room1", "", client)

        assert "System:" not in result
        assert "running" not in result
        assert "queued" not in result

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
        assert "!memory facts" in result

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

    @pytest.mark.asyncio
    async def test_facts_empty(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_memory(config, conn, "alice", "room1", "facts", client)
        assert "no facts" in result

    @pytest.mark.asyncio
    async def test_facts_with_few(self, make_config):
        """Small fact sets show all facts inline."""
        config = make_config()
        from istota.knowledge_graph import ensure_table, add_fact
        with db.get_db(config.db_path) as conn:
            ensure_table(conn)
            add_fact(conn, "alice", "alice", "works_at", "acme")
            add_fact(conn, "alice", "alice", "knows", "python")
            conn.commit()
            client = AsyncMock()
            result = await cmd_memory(config, conn, "alice", "room1", "facts", client)
        assert "Knowledge graph" in result
        assert "2 facts" in result
        assert "works_at" in result
        assert "python" in result

    @pytest.mark.asyncio
    async def test_facts_large_set_summarizes(self, make_config):
        """Large fact sets show entity summary instead of all facts."""
        config = make_config()
        from istota.knowledge_graph import ensure_table, add_fact
        with db.get_db(config.db_path) as conn:
            ensure_table(conn)
            for i in range(25):
                add_fact(conn, "alice", "alice", "knows", f"tech_{i}")
            conn.commit()
            client = AsyncMock()
            result = await cmd_memory(config, conn, "alice", "room1", "facts", client)
        assert "25 facts" in result
        assert "Entities:" in result
        assert "alice (25)" in result
        # Should not dump all individual facts
        assert "tech_0" not in result

    @pytest.mark.asyncio
    async def test_facts_entity_filter(self, make_config):
        """!memory facts <entity> shows facts for that entity only."""
        config = make_config()
        from istota.knowledge_graph import ensure_table, add_fact
        with db.get_db(config.db_path) as conn:
            ensure_table(conn)
            add_fact(conn, "alice", "alice", "works_at", "acme")
            add_fact(conn, "alice", "bob", "works_at", "globex")
            conn.commit()
            client = AsyncMock()
            result = await cmd_memory(config, conn, "alice", "room1", "facts alice", client)
        assert "Facts about alice" in result
        assert "works_at" in result
        assert "globex" not in result

    @pytest.mark.asyncio
    async def test_facts_entity_not_found(self, make_config):
        config = make_config()
        from istota.knowledge_graph import ensure_table
        with db.get_db(config.db_path) as conn:
            ensure_table(conn)
            client = AsyncMock()
            result = await cmd_memory(config, conn, "alice", "room1", "facts nobody", client)
        assert "none found" in result

    @pytest.mark.asyncio
    async def test_facts_no_mount_required(self, make_config):
        """Facts come from DB, not filesystem — works without mount."""
        config = make_config()
        config.nextcloud_mount_path = None
        from istota.knowledge_graph import ensure_table, add_fact
        with db.get_db(config.db_path) as conn:
            ensure_table(conn)
            add_fact(conn, "alice", "alice", "speaks", "polish")
            conn.commit()
            client = AsyncMock()
            result = await cmd_memory(config, conn, "alice", "room1", "facts", client)
        assert "Knowledge graph" in result
        assert "speaks" in result


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
# TestCmdSkills
# =============================================================================


class TestCmdSkills:
    @pytest.mark.asyncio
    async def test_lists_bundled_skills(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_skills(config, conn, "alice", "room1", "", client)

        assert "Skills" in result
        assert "total" in result
        # Some well-known bundled skills should appear
        assert "files" in result
        assert "calendar" in result

    @pytest.mark.asyncio
    async def test_hides_admin_skills_from_non_admin(self, make_config):
        config = make_config()
        config.admin_users = {"bob"}  # alice is not admin
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_skills(config, conn, "alice", "room1", "", client)

        # tasks skill is admin_only, should not appear for non-admin
        assert "**tasks**" not in result

    @pytest.mark.asyncio
    async def test_shows_admin_skills_to_admin(self, make_config):
        config = make_config()
        config.admin_users = set()  # empty = all admin
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_skills(config, conn, "alice", "room1", "", client)

        # With all users as admin, admin-only skills should be visible
        assert "tasks" in result

    @pytest.mark.asyncio
    async def test_shows_unavailable_skills(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            with patch("istota.skills._loader.get_skill_availability") as mock_avail:
                # Make one skill unavailable
                def side_effect(meta):
                    if meta.name == "whisper":
                        return ("unavailable", "faster-whisper")
                    return ("available", None)
                mock_avail.side_effect = side_effect
                result = await cmd_skills(config, conn, "alice", "room1", "", client)

        assert "Unavailable" in result
        assert "faster-whisper" in result

    @pytest.mark.asyncio
    async def test_shows_disabled_skills(self, make_config):
        config = make_config()
        config.disabled_skills = ["browse"]
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_skills(config, conn, "alice", "room1", "", client)

        assert "Disabled" in result
        assert "browse" in result

    @pytest.mark.asyncio
    async def test_skill_detail_view(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = AsyncMock()
            result = await cmd_skills(config, conn, "alice", "room1", "calendar", client)

        assert "**calendar**" in result
        assert "Status:" in result
        assert "CalDAV" in result


# =============================================================================
# TestCmdCheck
# =============================================================================


class TestCmdCheck:
    @pytest.mark.asyncio
    async def test_all_pass_no_sandbox(self, make_config):
        """All fast checks pass, sandbox disabled, Claude execution passes."""
        config = make_config(security=SecurityConfig(sandbox_enabled=False))
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
        config = make_config(security=SecurityConfig(sandbox_enabled=False))
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


# ---------------------------------------------------------------------------
# TestCmdMore
# ---------------------------------------------------------------------------


class TestCmdMore:
    """Test !more command for viewing execution traces."""

    @pytest.mark.asyncio
    async def test_shows_execution_trace(self, make_config, db_path):
        config = make_config()
        trace = json.dumps([
            {"type": "text", "text": "Let me look into that."},
            {"type": "tool", "text": "Read config.py"},
            {"type": "text", "text": "I see the issue. Let me fix it."},
            {"type": "tool", "text": "Edit config.py"},
        ])
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Fix the config", user_id="alice")
            db.update_task_status(conn, task_id, "completed", result="Fixed it.", execution_trace=trace)

        client = MagicMock()
        with db.get_db(db_path) as conn:
            result = await cmd_more(config, conn, "alice", "room1", str(task_id), client)

        assert f"Task #{task_id}" in result
        assert "Let me look into that." in result
        assert "Read config.py" in result
        assert "Edit config.py" in result
        assert "Fixed it." in result

    @pytest.mark.asyncio
    async def test_accepts_hash_prefix(self, make_config, db_path):
        config = make_config()
        trace = json.dumps([{"type": "tool", "text": "Read file"}])
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Test", user_id="alice")
            db.update_task_status(conn, task_id, "completed", result="Done", execution_trace=trace)

        client = MagicMock()
        with db.get_db(db_path) as conn:
            result = await cmd_more(config, conn, "alice", "room1", f"#{task_id}", client)

        assert f"Task #{task_id}" in result

    @pytest.mark.asyncio
    async def test_no_trace_available(self, make_config, db_path):
        config = make_config()
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Old task", user_id="alice")
            db.update_task_status(conn, task_id, "completed", result="Done")

        client = MagicMock()
        with db.get_db(db_path) as conn:
            result = await cmd_more(config, conn, "alice", "room1", str(task_id), client)

        assert "no execution trace" in result

    @pytest.mark.asyncio
    async def test_task_not_found(self, make_config, db_path):
        config = make_config()
        client = MagicMock()
        with db.get_db(db_path) as conn:
            result = await cmd_more(config, conn, "alice", "room1", "99999", client)

        assert "not found" in result

    @pytest.mark.asyncio
    async def test_other_users_task_blocked(self, make_config, db_path):
        config = make_config()
        config.admin_users = {"bob"}  # alice is NOT admin
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="Secret", user_id="bob")
            db.update_task_status(conn, task_id, "completed", result="Done")

        client = MagicMock()
        with db.get_db(db_path) as conn:
            result = await cmd_more(config, conn, "alice", "room1", str(task_id), client)

        assert "another user" in result

    @pytest.mark.asyncio
    async def test_invalid_task_id(self, make_config, db_path):
        config = make_config()
        client = MagicMock()
        with db.get_db(db_path) as conn:
            result = await cmd_more(config, conn, "alice", "room1", "notanumber", client)

        assert "Usage" in result

    @pytest.mark.asyncio
    async def test_running_task_shows_status(self, make_config, db_path):
        config = make_config()
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="In progress", user_id="alice")
            db.update_task_status(conn, task_id, "running")

        client = MagicMock()
        with db.get_db(db_path) as conn:
            result = await cmd_more(config, conn, "alice", "room1", str(task_id), client)

        assert "still running" in result


# =============================================================================
# TestCmdSearch
# =============================================================================


class TestCmdSearch:
    """Test !search command for conversation history search."""

    def _mock_resolve(self):
        """Patch _resolve_room_names to return token as name (identity map)."""
        async def _identity_resolve(client, tokens):
            return {t: t for t in tokens}
        return patch("istota.commands._resolve_room_names", side_effect=_identity_resolve)

    @pytest.mark.asyncio
    async def test_empty_query_returns_usage(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "", client)
        assert "Usage" in result
        assert "!search" in result

    @pytest.mark.asyncio
    async def test_whitespace_only_query_returns_usage(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "   ", client)
        assert "Usage" in result

    @pytest.mark.asyncio
    async def test_search_current_room_no_results(self, make_config, db_path):
        config = make_config()
        with db.get_db(db_path) as conn:
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "nonexistent query xyz", client)
        assert "No results" in result

    @pytest.mark.asyncio
    async def test_search_current_room_filters_by_token(self, make_config, db_path):
        """Results from other rooms should be excluded when searching current room."""
        config = make_config()
        with db.get_db(db_path) as conn:
            # Create tasks in different rooms
            t1 = db.create_task(conn, prompt="parser bug discussion", user_id="alice",
                                conversation_token="room1", source_type="talk")
            db.update_task_status(conn, t1, "completed", result="Fixed the parser bug")
            t2 = db.create_task(conn, prompt="parser bug in other room", user_id="alice",
                                conversation_token="room2", source_type="talk")
            db.update_task_status(conn, t2, "completed", result="Also about parser")

        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            self._mock_resolve(),
        ):
            # Memory search returns results from both rooms
            mock_mem.return_value = [
                {"date": "Apr 1", "room": "room1", "summary": "Parser bug in room1",
                 "task_id": t1, "conversation_token": "room1"},
                {"date": "Mar 28", "room": "room2", "summary": "Parser bug in room2",
                 "task_id": t2, "conversation_token": "room2"},
            ]
            mock_talk.return_value = []
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "parser bug", client)

        # Only room1 result should appear
        assert "Parser bug in room1" in result
        assert "room2" not in result

    @pytest.mark.asyncio
    async def test_search_all_rooms(self, make_config, db_path):
        """--all flag should return results from all rooms."""
        config = make_config()
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            self._mock_resolve(),
        ):
            mock_mem.return_value = [
                {"date": "Apr 1", "room": "room1", "summary": "Bug in room1",
                 "task_id": 100, "conversation_token": "room1"},
                {"date": "Mar 28", "room": "room2", "summary": "Bug in room2",
                 "task_id": 200, "conversation_token": "room2"},
            ]
            mock_talk.return_value = []
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "--all parser bug", client)

        assert "room1" in result
        assert "room2" in result

    @pytest.mark.asyncio
    async def test_search_specific_room(self, make_config, db_path):
        """--room flag should filter to a specific room."""
        config = make_config()
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            self._mock_resolve(),
        ):
            mock_mem.return_value = [
                {"date": "Apr 1", "room": "room1", "summary": "Result in room1",
                 "task_id": 100, "conversation_token": "room1"},
                {"date": "Mar 28", "room": "otherroom", "summary": "Result in other",
                 "task_id": 200, "conversation_token": "otherroom"},
            ]
            mock_talk.return_value = []
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "--room otherroom some query", client)

        assert "otherroom" in result
        assert "Result in room1" not in result

    @pytest.mark.asyncio
    async def test_output_format_includes_task_reference(self, make_config, db_path):
        """Results without talk_message_id should fall back to task ID reference."""
        config = make_config()
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            self._mock_resolve(),
        ):
            mock_mem.return_value = [
                {"date": "Apr 1", "room": "room1", "summary": "Fixed parser bug",
                 "task_id": 46945, "conversation_token": "room1"},
            ]
            mock_talk.return_value = []
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "--all parser bug", client)

        assert "#46945" in result

    @pytest.mark.asyncio
    async def test_output_format_with_message_link(self, make_config, db_path):
        """Results with talk_message_id should show a deep link instead of task ref."""
        config = make_config()
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            self._mock_resolve(),
        ):
            mock_mem.return_value = [
                {"date": "Apr 1", "room": "room1", "summary": "Fixed parser bug",
                 "task_id": 100, "talk_message_id": 38939,
                 "conversation_token": "room1"},
            ]
            mock_talk.return_value = []
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "--all parser bug", client)

        assert "https://nc.test/call/room1#message_38939" in result

    @pytest.mark.asyncio
    async def test_output_format_header(self, make_config, db_path):
        """Output should start with result count and query."""
        config = make_config()
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            self._mock_resolve(),
        ):
            mock_mem.return_value = [
                {"date": "Apr 1", "room": "room1", "summary": "Found it",
                 "task_id": 100, "conversation_token": "room1"},
            ]
            mock_talk.return_value = []
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "--all test query", client)

        assert "1 result" in result
        assert "test query" in result

    @pytest.mark.asyncio
    async def test_talk_api_results_included(self, make_config, db_path):
        """Talk API results should be merged when memory search has no hits."""
        config = make_config()
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            self._mock_resolve(),
        ):
            mock_mem.return_value = []
            mock_talk.return_value = [
                {"date": "Apr 1", "room": "room1", "summary": "Recent chat message",
                 "talk_link": "https://nc.test/call/room1#message_123",
                 "conversation_token": "room1"},
            ]
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "--all recent chat", client)

        assert "Recent chat message" in result
        assert "https://nc.test/call/room1#message_123" in result

    @pytest.mark.asyncio
    async def test_talk_api_results_filtered_by_room(self, make_config, db_path):
        """Talk API results should respect room scoping."""
        config = make_config()
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            self._mock_resolve(),
        ):
            mock_mem.return_value = []
            mock_talk.return_value = [
                {"date": "Apr 1", "room": "room1", "summary": "In room1",
                 "talk_link": "https://nc.test/call/room1#message_1",
                 "conversation_token": "room1"},
                {"date": "Apr 1", "room": "room2", "summary": "In room2",
                 "talk_link": "https://nc.test/call/room2#message_2",
                 "conversation_token": "room2"},
            ]
            client = MagicMock()
            # Default: current room only
            result = await cmd_search(config, conn, "alice", "room1", "some query", client)

        assert "In room1" in result
        assert "In room2" not in result

    @pytest.mark.asyncio
    async def test_max_results_capped(self, make_config, db_path):
        """Should cap results at 8."""
        config = make_config()
        many_results = [
            {"date": f"Apr {i}", "room": "room1", "summary": f"Result {i}",
             "task_id": i, "conversation_token": "room1"}
            for i in range(15)
        ]
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            self._mock_resolve(),
        ):
            mock_mem.return_value = many_results
            mock_talk.return_value = []
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "--all lots of results", client)

        # Count numbered results (lines starting with "N. ")
        import re
        numbered = re.findall(r"^\d+\.", result, re.MULTILINE)
        assert len(numbered) <= 8

    @pytest.mark.asyncio
    async def test_deduplication_between_sources(self, make_config, db_path):
        """Same task_id from memory and Talk should not appear twice."""
        config = make_config()
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            self._mock_resolve(),
        ):
            mock_mem.return_value = [
                {"date": "Apr 1", "room": "room1", "summary": "From memory",
                 "task_id": 100, "conversation_token": "room1"},
            ]
            # Talk result with same task_id
            mock_talk.return_value = [
                {"date": "Apr 1", "room": "room1", "summary": "From talk",
                 "task_id": 100, "conversation_token": "room1"},
            ]
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "--all test", client)

        assert "1 result" in result

    @pytest.mark.asyncio
    async def test_room_names_resolved(self, make_config, db_path):
        """Room tokens should be resolved to display names."""
        config = make_config()

        async def _named_resolve(client, tokens):
            return {t: {"room1": "General", "room2": "Dev Chat"}.get(t, t) for t in tokens}

        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            patch("istota.commands._resolve_room_names", side_effect=_named_resolve),
        ):
            mock_mem.return_value = [
                {"date": "Apr 1", "room": "room1", "summary": "Some discussion",
                 "task_id": 100, "conversation_token": "room1"},
            ]
            mock_talk.return_value = []
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "--all discussion", client)

        assert "in General" in result
        assert "room1" not in result  # token should not appear

    @pytest.mark.asyncio
    async def test_help_registered(self, make_config):
        """!search should appear in !help output."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            client = MagicMock()
            result = await cmd_help(config, conn, "alice", "room1", "", client)
        assert "!search" in result


class TestSearchMemory:
    """Test the _search_memory helper that wraps memory_search."""

    def test_maps_search_results_to_dicts(self, make_config, db_path):
        from istota.commands import _search_memory
        from istota.memory_search import SearchResult

        config = make_config()
        mock_results = [
            SearchResult(
                chunk_id=1, content="User: How do I fix the parser?\n\nBot: Check stream_parser.py",
                score=0.8, source_type="conversation", source_id="500",
                metadata={"task_id": "500"},
            ),
        ]
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands.memory_search_mod.search", return_value=mock_results),
        ):
            # Create the task so we can resolve its conversation_token
            task_id = db.create_task(conn, prompt="Fix parser", user_id="alice",
                                     conversation_token="room1", source_type="talk",
                                     talk_message_id=12345)
            # Override source_id to match
            mock_results[0].source_id = str(task_id)
            mock_results[0].metadata["task_id"] = str(task_id)

            results = _search_memory(config, conn, "alice", "fix parser")

        assert len(results) == 1
        assert results[0]["task_id"] == task_id
        assert results[0]["conversation_token"] == "room1"
        assert results[0]["talk_message_id"] == 12345
        assert len(results[0]["summary"]) > 0

    def test_returns_empty_when_no_results(self, make_config, db_path):
        from istota.commands import _search_memory

        config = make_config()
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands.memory_search_mod.search", return_value=[]),
        ):
            results = _search_memory(config, conn, "alice", "nothing here")

        assert results == []

    def test_skips_results_without_task(self, make_config, db_path):
        """Memory results whose source_id doesn't map to a task should still work."""
        from istota.commands import _search_memory
        from istota.memory_search import SearchResult

        config = make_config()
        mock_results = [
            SearchResult(
                chunk_id=1, content="Some memory file content",
                score=0.5, source_type="memory_file", source_id="memories/2026-03-28.md",
                metadata={},
            ),
        ]
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands.memory_search_mod.search", return_value=mock_results),
        ):
            results = _search_memory(config, conn, "alice", "memory content")

        # memory_file results have no task_id — should still appear with no task ref
        assert len(results) == 1
        assert results[0].get("task_id") is None


class TestSearchTalkApi:
    """Test the _search_talk_api helper."""

    @pytest.mark.asyncio
    async def test_returns_formatted_results(self, make_config):
        from istota.commands import _search_talk_api

        config = make_config()
        # Mock the OCS response from Nextcloud unified search
        mock_ocs_data = {
            "entries": [
                {
                    "title": "Recent message about deployment",
                    "subline": "Let me check the deploy status",
                    "resourceUrl": "https://nc.test/call/room1#message_456",
                    "attributes": {
                        "conversation": "room1",
                        "messageId": "456",
                    },
                },
            ],
        }
        with patch("istota.commands.ocs_get", return_value=mock_ocs_data):
            results = await _search_talk_api(config, "deploy")

        assert len(results) == 1
        # subline is preferred over title (title is "username in room")
        assert "deploy status" in results[0]["summary"]
        assert results[0]["conversation_token"] == "room1"
        assert "message_456" in results[0]["talk_link"]

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_failure(self, make_config):
        from istota.commands import _search_talk_api

        config = make_config()
        with patch("istota.commands.ocs_get", return_value=None):
            results = await _search_talk_api(config, "test")

        assert results == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_entries(self, make_config):
        from istota.commands import _search_talk_api

        config = make_config()
        with patch("istota.commands.ocs_get", return_value={"entries": []}):
            results = await _search_talk_api(config, "test")

        assert results == []


class TestParseSearchArgs:
    """Test _parse_search_args with new --since, --week, --memories flags."""

    def test_basic_query(self):
        result = _parse_search_args("hello world")
        assert result.scope is None
        assert result.query == "hello world"
        assert result.since is None
        assert result.memories_only is False

    def test_all_flag(self):
        result = _parse_search_args("--all some query")
        assert result.scope == "all"
        assert result.query == "some query"

    def test_room_flag(self):
        result = _parse_search_args("--room abc123 some query")
        assert result.scope == "abc123"
        assert result.query == "some query"

    def test_since_flag(self):
        result = _parse_search_args("--since 2026-03-25 deployment")
        assert result.since == "2026-03-25"
        assert result.query == "deployment"

    def test_week_flag(self):
        from datetime import date, timedelta
        result = _parse_search_args("--week deployment")
        expected = (date.today() - timedelta(days=7)).isoformat()
        assert result.since == expected
        assert result.query == "deployment"

    def test_memories_flag(self):
        result = _parse_search_args("--memories something")
        assert result.memories_only is True
        assert result.query == "something"

    def test_combined_flags(self):
        result = _parse_search_args("--all --week --memories deployment")
        assert result.scope == "all"
        assert result.since is not None
        assert result.memories_only is True
        assert result.query == "deployment"

    def test_since_and_all(self):
        result = _parse_search_args("--all --since 2026-01-01 query here")
        assert result.scope == "all"
        assert result.since == "2026-01-01"
        assert result.query == "query here"

    def test_empty_returns_empty_query(self):
        result = _parse_search_args("")
        assert result.query == ""

    def test_since_without_date_treats_as_query(self):
        """--since at end with no date should be treated as query text."""
        result = _parse_search_args("--since")
        assert result.query == "--since"
        assert result.since is None

    def test_flags_order_independent(self):
        r1 = _parse_search_args("--memories --all query")
        r2 = _parse_search_args("--all --memories query")
        assert r1.scope == r2.scope == "all"
        assert r1.memories_only == r2.memories_only is True
        assert r1.query == r2.query == "query"


class TestCmdSearchFiltering:
    """Test !search with --since, --week, --memories filtering."""

    def _mock_resolve(self):
        async def _identity_resolve(client, tokens):
            return {t: t for t in tokens}
        return patch("istota.commands._resolve_room_names", side_effect=_identity_resolve)

    @pytest.mark.asyncio
    async def test_memories_only_skips_talk_api(self, make_config, db_path):
        """--memories should skip Talk API search entirely."""
        config = make_config()
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            self._mock_resolve(),
        ):
            mock_mem.return_value = [
                {"date": "Mar 28", "room": "room1", "summary": "Memory result",
                 "task_id": None, "conversation_token": "room1", "source_type": "memory_file"},
            ]
            mock_talk.return_value = []
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "--all --memories test", client)

        mock_talk.assert_not_called()
        assert "Memory result" in result

    @pytest.mark.asyncio
    async def test_memories_only_passes_source_types(self, make_config, db_path):
        """--memories should pass source_types=["memory_file"] to _search_memory."""
        config = make_config()
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            self._mock_resolve(),
        ):
            mock_mem.return_value = []
            mock_talk.return_value = []
            client = MagicMock()
            await cmd_search(config, conn, "alice", "room1", "--all --memories test", client)

        call_kwargs = mock_mem.call_args
        assert call_kwargs.kwargs.get("source_types") == ["memory_file"]

    @pytest.mark.asyncio
    async def test_since_passed_to_search_memory(self, make_config, db_path):
        """--since should be forwarded to _search_memory."""
        config = make_config()
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            self._mock_resolve(),
        ):
            mock_mem.return_value = []
            mock_talk.return_value = []
            client = MagicMock()
            await cmd_search(config, conn, "alice", "room1", "--all --since 2026-03-01 test", client)

        call_kwargs = mock_mem.call_args
        assert call_kwargs.kwargs.get("since") == "2026-03-01"

    @pytest.mark.asyncio
    async def test_since_filters_talk_results(self, make_config, db_path):
        """--since should filter out Talk API results older than the date."""
        config = make_config()
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands._search_memory") as mock_mem,
            patch("istota.commands._search_talk_api") as mock_talk,
            self._mock_resolve(),
        ):
            mock_mem.return_value = []
            mock_talk.return_value = [
                {"date": "2026-03-15", "room": "room1", "summary": "Old result",
                 "conversation_token": "room1"},
                {"date": "2026-03-25", "room": "room1", "summary": "Recent result",
                 "conversation_token": "room1"},
            ]
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "--all --since 2026-03-20 test", client)

        assert "Recent result" in result
        assert "Old result" not in result

    @pytest.mark.asyncio
    async def test_updated_usage_string(self, make_config, db_path):
        """Usage string should mention new flags."""
        config = make_config()
        with db.get_db(db_path) as conn:
            client = MagicMock()
            result = await cmd_search(config, conn, "alice", "room1", "", client)
        assert "--since" in result
        assert "--memories" in result


# =============================================================================
# TestTrustCommand
# =============================================================================


class TestTrustCommand:
    @pytest.mark.asyncio
    async def test_trust_adds_sender(self, make_config, db_path):
        from istota.commands import cmd_trust
        config = make_config()
        with db.get_db(db_path) as conn:
            client = MagicMock()
            result = await cmd_trust(config, conn, "alice", "room1", "joe@example.com", client)
        assert "Trusted" in result
        assert "joe@example.com" in result
        with db.get_db(db_path) as conn:
            assert db.is_sender_trusted_in_db(conn, "alice", "joe@example.com") is True

    @pytest.mark.asyncio
    async def test_trust_duplicate(self, make_config, db_path):
        from istota.commands import cmd_trust
        config = make_config()
        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "alice", "joe@example.com")
            client = MagicMock()
            result = await cmd_trust(config, conn, "alice", "room1", "joe@example.com", client)
        assert "already trusted" in result

    @pytest.mark.asyncio
    async def test_trust_no_args_lists_senders(self, make_config, db_path):
        from istota.commands import cmd_trust
        config = make_config()
        config.users["alice"] = UserConfig(trusted_email_senders=["*@corp.com"])
        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "alice", "joe@example.com")
            client = MagicMock()
            result = await cmd_trust(config, conn, "alice", "room1", "", client)
        assert "*@corp.com" in result
        assert "(config)" in result
        assert "joe@example.com" in result

    @pytest.mark.asyncio
    async def test_trust_invalid_email(self, make_config, db_path):
        from istota.commands import cmd_trust
        config = make_config()
        with db.get_db(db_path) as conn:
            client = MagicMock()
            result = await cmd_trust(config, conn, "alice", "room1", "notanemail", client)
        assert "Usage" in result

    @pytest.mark.asyncio
    async def test_untrust_removes_sender(self, make_config, db_path):
        from istota.commands import cmd_untrust
        config = make_config()
        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "alice", "joe@example.com")
            client = MagicMock()
            result = await cmd_untrust(config, conn, "alice", "room1", "joe@example.com", client)
        assert "Removed" in result
        with db.get_db(db_path) as conn:
            assert db.is_sender_trusted_in_db(conn, "alice", "joe@example.com") is False

    @pytest.mark.asyncio
    async def test_untrust_nonexistent(self, make_config, db_path):
        from istota.commands import cmd_untrust
        config = make_config()
        with db.get_db(db_path) as conn:
            client = MagicMock()
            result = await cmd_untrust(config, conn, "alice", "room1", "nobody@example.com", client)
        assert "not in your trusted" in result

    @pytest.mark.asyncio
    async def test_trust_list_empty(self, make_config, db_path):
        from istota.commands import cmd_trust
        config = make_config()
        with db.get_db(db_path) as conn:
            client = MagicMock()
            result = await cmd_trust(config, conn, "alice", "room1", "", client)
        assert "No trusted senders" in result
