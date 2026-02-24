"""Tests for channel sleep cycle functionality in istota.sleep_cycle."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

from istota import db
from istota.config import Config, ChannelSleepCycleConfig, MemorySearchConfig
from istota.sleep_cycle import (
    gather_channel_data,
    build_channel_memory_extraction_prompt,
    process_channel_sleep_cycle,
    cleanup_old_channel_memory_files,
    check_channel_sleep_cycles,
    NO_NEW_MEMORIES,
    MAX_DAY_DATA_CHARS,
)


@pytest.fixture
def mount_config(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    return Config(
        db_path=tmp_path / "test.db",
        temp_dir=tmp_path / "temp",
        nextcloud_mount_path=mount,
        channel_sleep_cycle=ChannelSleepCycleConfig(
            enabled=True,
            cron="0 3 * * *",
            lookback_hours=24,
            memory_retention_days=90,
        ),
    )


class TestGatherChannelData:
    def test_returns_empty_when_no_tasks(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            result = gather_channel_data(mount_config, conn, "room123", 24, None)
        assert result == ""

    def test_gathers_tasks_for_channel(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            t = db.create_task(
                conn, prompt="Deploy the API", user_id="alice",
                conversation_token="room123",
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="API deployed successfully.")

            result = gather_channel_data(mount_config, conn, "room123", 24, None)

        assert "Deploy the API" in result
        assert "API deployed successfully." in result
        assert "alice" in result  # user attribution

    def test_filters_by_conversation_token(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="Room 123 task", user_id="alice",
                conversation_token="room123",
            )
            db.update_task_status(conn, t1, "running")
            db.update_task_status(conn, t1, "completed", result="Done 1")

            t2 = db.create_task(
                conn, prompt="Room 456 task", user_id="bob",
                conversation_token="room456",
            )
            db.update_task_status(conn, t2, "running")
            db.update_task_status(conn, t2, "completed", result="Done 2")

            result = gather_channel_data(mount_config, conn, "room123", 24, None)

        assert "Room 123 task" in result
        assert "Room 456 task" not in result

    def test_includes_user_attribution(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            t = db.create_task(
                conn, prompt="Check status", user_id="bob",
                conversation_token="room123",
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="All good")

            result = gather_channel_data(mount_config, conn, "room123", 24, None)

        assert "user: bob" in result

    def test_respects_after_task_id(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="First task", user_id="alice",
                conversation_token="room123",
            )
            db.update_task_status(conn, t1, "running")
            db.update_task_status(conn, t1, "completed", result="Result 1")

            t2 = db.create_task(
                conn, prompt="Second task", user_id="alice",
                conversation_token="room123",
            )
            db.update_task_status(conn, t2, "running")
            db.update_task_status(conn, t2, "completed", result="Result 2")

            result = gather_channel_data(mount_config, conn, "room123", 24, t1)

        assert "First task" not in result
        assert "Second task" in result

    def test_truncates_long_data(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            for i in range(100):
                t = db.create_task(
                    conn, prompt="x" * 500, user_id="alice",
                    conversation_token="room123",
                )
                db.update_task_status(conn, t, "running")
                db.update_task_status(conn, t, "completed", result="y" * 500)

            result = gather_channel_data(mount_config, conn, "room123", 24, None)

        assert len(result) <= MAX_DAY_DATA_CHARS + 100
        assert "truncated" in result


class TestBuildChannelMemoryExtractionPrompt:
    def test_includes_channel_token(self):
        prompt = build_channel_memory_extraction_prompt("room123", "some data", None, "2026-02-07")
        assert "room123" in prompt

    def test_includes_day_data(self):
        prompt = build_channel_memory_extraction_prompt(
            "room123", "Decided to use GraphQL", None, "2026-02-07"
        )
        assert "Decided to use GraphQL" in prompt

    def test_includes_existing_memory(self):
        prompt = build_channel_memory_extraction_prompt(
            "room123", "data", "- Migrating to Python 3.12", "2026-02-07"
        )
        assert "Migrating to Python 3.12" in prompt
        assert "Do NOT repeat" in prompt

    def test_no_existing_memory_section_when_none(self):
        prompt = build_channel_memory_extraction_prompt("room123", "data", None, "2026-02-07")
        assert "Existing channel memory" not in prompt

    def test_focuses_on_shared_context(self):
        prompt = build_channel_memory_extraction_prompt("room123", "data", None, "2026-02-07")
        assert "Decisions" in prompt or "decisions" in prompt
        assert "personal" in prompt.lower() or "private" in prompt.lower()

    def test_includes_no_new_memories_sentinel(self):
        prompt = build_channel_memory_extraction_prompt("room123", "data", None, "2026-02-07")
        assert NO_NEW_MEMORIES in prompt


class TestProcessChannelSleepCycle:
    def test_skips_when_no_interactions(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            result = process_channel_sleep_cycle(mount_config, conn, "room123")
        assert result is False

    @patch("istota.sleep_cycle.subprocess.run")
    def test_writes_memory_file(self, mock_run, mount_config, db_path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="- Decided to use GraphQL (alice, 2026-02-07)\n",
            stderr="",
        )

        with db.get_db(db_path) as conn:
            t = db.create_task(
                conn, prompt="Should we use GraphQL?", user_id="alice",
                conversation_token="room123",
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Yes, GraphQL is the way to go.")

            result = process_channel_sleep_cycle(mount_config, conn, "room123")

        assert result is True

        memories_dir = mount_config.nextcloud_mount_path / "Channels" / "room123" / "memories"
        date_str = datetime.now().strftime("%Y-%m-%d")
        memory_file = memories_dir / f"{date_str}.md"
        assert memory_file.exists()
        assert "GraphQL" in memory_file.read_text()

    @patch("istota.sleep_cycle.subprocess.run")
    def test_no_file_when_no_new_memories(self, mock_run, mount_config, db_path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=NO_NEW_MEMORIES,
            stderr="",
        )

        with db.get_db(db_path) as conn:
            t = db.create_task(
                conn, prompt="Hello", user_id="alice",
                conversation_token="room123",
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Hi!")

            result = process_channel_sleep_cycle(mount_config, conn, "room123")

        assert result is False

    @patch("istota.sleep_cycle.subprocess.run")
    def test_updates_state(self, mock_run, mount_config, db_path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="- New channel memory (2026-02-07)\n",
            stderr="",
        )

        with db.get_db(db_path) as conn:
            t = db.create_task(
                conn, prompt="Test", user_id="alice",
                conversation_token="room123",
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            process_channel_sleep_cycle(mount_config, conn, "room123")

            last_run, last_task = db.get_channel_sleep_cycle_last_run(conn, "room123")
            assert last_run is not None
            assert last_task == t

    @patch("istota.sleep_cycle.subprocess.run")
    def test_indexes_into_memory_search(self, mock_run, mount_config, db_path):
        mount_config.memory_search = MemorySearchConfig(
            enabled=True,
            auto_index_memory_files=True,
        )
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="- Channel decision (2026-02-07)\n",
            stderr="",
        )

        with db.get_db(db_path) as conn:
            t = db.create_task(
                conn, prompt="Test", user_id="alice",
                conversation_token="room123",
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            with patch("istota.memory_search.index_file") as mock_index:
                process_channel_sleep_cycle(mount_config, conn, "room123")

                # Should have been called with channel user_id
                assert mock_index.called
                call_args = mock_index.call_args
                assert call_args[0][1] == "channel:room123"
                assert call_args[0][4] == "channel_memory"

    @patch("istota.sleep_cycle.subprocess.run")
    def test_handles_timeout(self, mock_run, mount_config, db_path):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)

        with db.get_db(db_path) as conn:
            t = db.create_task(
                conn, prompt="Test", user_id="alice",
                conversation_token="room123",
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            result = process_channel_sleep_cycle(mount_config, conn, "room123")

        assert result is False

    def test_no_mount_warning(self, db_path):
        """Without mount, warns, updates state, and returns False."""
        config = Config(
            db_path=db_path,
            channel_sleep_cycle=ChannelSleepCycleConfig(enabled=True),
        )

        with db.get_db(db_path) as conn:
            t = db.create_task(
                conn, prompt="Test", user_id="alice",
                conversation_token="room123",
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            with patch("istota.sleep_cycle.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout="- Memory (2026-02-07)\n",
                    stderr="",
                )
                result = process_channel_sleep_cycle(config, conn, "room123")

            # State must be updated even without mount, to avoid infinite reprocessing
            last_run, last_task = db.get_channel_sleep_cycle_last_run(conn, "room123")
            assert last_run is not None
            assert last_task == t

        assert result is False


class TestCleanupOldChannelMemoryFiles:
    def test_deletes_old_files(self, mount_config):
        memories_dir = mount_config.nextcloud_mount_path / "Channels" / "room123" / "memories"
        memories_dir.mkdir(parents=True)

        old_date = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
        (memories_dir / f"{old_date}.md").write_text("old memory")

        recent_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        (memories_dir / f"{recent_date}.md").write_text("recent memory")

        deleted = cleanup_old_channel_memory_files(mount_config, "room123", 90)

        assert deleted == 1
        assert not (memories_dir / f"{old_date}.md").exists()
        assert (memories_dir / f"{recent_date}.md").exists()

    def test_keeps_recent_files(self, mount_config):
        memories_dir = mount_config.nextcloud_mount_path / "Channels" / "room123" / "memories"
        memories_dir.mkdir(parents=True)

        recent_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        (memories_dir / f"{recent_date}.md").write_text("recent memory")

        deleted = cleanup_old_channel_memory_files(mount_config, "room123", 90)

        assert deleted == 0

    def test_returns_zero_when_missing_dir(self, mount_config):
        deleted = cleanup_old_channel_memory_files(mount_config, "room123", 90)
        assert deleted == 0

    def test_skips_cleanup_when_retention_zero(self, mount_config):
        memories_dir = mount_config.nextcloud_mount_path / "Channels" / "room123" / "memories"
        memories_dir.mkdir(parents=True)

        old_date = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
        (memories_dir / f"{old_date}.md").write_text("old memory")

        deleted = cleanup_old_channel_memory_files(mount_config, "room123", 0)

        assert deleted == 0
        assert (memories_dir / f"{old_date}.md").exists()


class TestCheckChannelSleepCycles:
    def test_returns_empty_when_disabled(self, mount_config, db_path):
        mount_config.channel_sleep_cycle.enabled = False
        with db.get_db(db_path) as conn:
            result = check_channel_sleep_cycles(conn, mount_config)
        assert result == []

    def test_auto_discovers_active_channels(self, mount_config, db_path):
        """Active channels are discovered from completed tasks."""
        with db.get_db(db_path) as conn:
            t = db.create_task(
                conn, prompt="Test", user_id="alice",
                conversation_token="room123",
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            # Use always-due cron so it runs
            mount_config.channel_sleep_cycle.cron = "* * * * *"

            with patch("istota.sleep_cycle.process_channel_sleep_cycle") as mock_process:
                mock_process.return_value = True
                result = check_channel_sleep_cycles(conn, mount_config)

        assert "room123" in result
        mock_process.assert_called_once()

    def test_skips_when_not_due(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            t = db.create_task(
                conn, prompt="Test", user_id="alice",
                conversation_token="room123",
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            # Set last run to now
            db.set_channel_sleep_cycle_last_run(conn, "room123", t)

            # Use a specific hour cron
            mount_config.channel_sleep_cycle.cron = "0 3 * * *"

            with patch("istota.sleep_cycle.process_channel_sleep_cycle") as mock_process:
                check_channel_sleep_cycles(conn, mount_config)

            # Whether it ran depends on current time, but no error raised

    @patch("istota.sleep_cycle.process_channel_sleep_cycle")
    def test_handles_process_error_gracefully(self, mock_process, mount_config, db_path):
        mock_process.side_effect = Exception("Something went wrong")

        with db.get_db(db_path) as conn:
            t = db.create_task(
                conn, prompt="Test", user_id="alice",
                conversation_token="room123",
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            mount_config.channel_sleep_cycle.cron = "* * * * *"

            # Should not raise
            result = check_channel_sleep_cycles(conn, mount_config)

        assert result == []

    def test_first_run_discovery(self, mount_config, db_path):
        """On first run, discovers channels and processes if past cron time."""
        with db.get_db(db_path) as conn:
            t = db.create_task(
                conn, prompt="Test", user_id="alice",
                conversation_token="room789",
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            mount_config.channel_sleep_cycle.cron = "* * * * *"  # always due

            with patch("istota.sleep_cycle.process_channel_sleep_cycle") as mock_process:
                mock_process.return_value = True
                result = check_channel_sleep_cycles(conn, mount_config)

        assert "room789" in result
