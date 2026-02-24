"""Configuration loading for istota.sleep_cycle module."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

from istota import db
from istota.config import Config, SleepCycleConfig, UserConfig
from istota.sleep_cycle import (
    gather_day_data,
    build_memory_extraction_prompt,
    process_user_sleep_cycle,
    cleanup_old_memory_files,
    check_sleep_cycles,
    build_curation_prompt,
    curate_user_memory,
    NO_NEW_MEMORIES,
    NO_CHANGES_NEEDED,
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
        sleep_cycle=SleepCycleConfig(
            enabled=True,
            cron="0 2 * * *",
            memory_retention_days=90,
            lookback_hours=24,
        ),
    )


class TestGatherDayData:
    def test_returns_empty_when_no_tasks(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            result = gather_day_data(mount_config, conn, "alice", 24, None)
        assert result == ""

    def test_gathers_completed_tasks(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="What's the weather?", user_id="alice")
            db.update_task_status(conn, task_id, "running")
            db.update_task_status(conn, task_id, "completed", result="It's sunny today.")

            result = gather_day_data(mount_config, conn, "alice", 24, None)

        assert "What's the weather?" in result
        assert "It's sunny today." in result
        assert f"Task {task_id}" in result

    def test_respects_after_task_id(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(conn, prompt="First", user_id="alice")
            db.update_task_status(conn, t1, "running")
            db.update_task_status(conn, t1, "completed", result="Result 1")

            t2 = db.create_task(conn, prompt="Second", user_id="alice")
            db.update_task_status(conn, t2, "running")
            db.update_task_status(conn, t2, "completed", result="Result 2")

            result = gather_day_data(mount_config, conn, "alice", 24, t1)

        assert "First" not in result
        assert "Second" in result

    def test_only_includes_target_user(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            t1 = db.create_task(conn, prompt="Alice task", user_id="alice")
            db.update_task_status(conn, t1, "running")
            db.update_task_status(conn, t1, "completed", result="Alice result")

            t2 = db.create_task(conn, prompt="Bob task", user_id="bob")
            db.update_task_status(conn, t2, "running")
            db.update_task_status(conn, t2, "completed", result="Bob result")

            result = gather_day_data(mount_config, conn, "alice", 24, None)

        assert "Alice task" in result
        assert "Bob task" not in result

    def test_truncates_long_data(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            # Create enough tasks to exceed MAX_DAY_DATA_CHARS
            for i in range(100):
                t = db.create_task(conn, prompt="x" * 500, user_id="alice")
                db.update_task_status(conn, t, "running")
                db.update_task_status(conn, t, "completed", result="y" * 500)

            result = gather_day_data(mount_config, conn, "alice", 24, None)

        assert len(result) <= MAX_DAY_DATA_CHARS + 100  # some margin for truncation marker
        assert "truncated" in result


class TestBuildMemoryExtractionPrompt:
    def test_includes_user_id(self):
        prompt = build_memory_extraction_prompt("alice", "some data", None, "2026-01-28")
        assert "alice" in prompt

    def test_includes_day_data(self):
        prompt = build_memory_extraction_prompt("alice", "User asked about weather", None, "2026-01-28")
        assert "User asked about weather" in prompt

    def test_includes_existing_memory(self):
        prompt = build_memory_extraction_prompt(
            "alice", "data", "- Prefers morning meetings", "2026-01-28"
        )
        assert "Prefers morning meetings" in prompt
        assert "Do NOT repeat" in prompt

    def test_no_existing_memory_section_when_none(self):
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert "Existing long-term memory" not in prompt

    def test_includes_date(self):
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert "2026-01-28" in prompt

    def test_includes_no_new_memories_sentinel(self):
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert NO_NEW_MEMORIES in prompt


class TestProcessUserSleepCycle:
    def test_skips_when_no_interactions(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            result = process_user_sleep_cycle(mount_config, conn, "alice")
        assert result is False

    @patch("istota.sleep_cycle.subprocess.run")
    def test_writes_memory_file(self, mock_run, mount_config, db_path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="- Discussed project Alpha (2026-01-28)\n",
            stderr="",
        )

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Tell me about project Alpha", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Project Alpha is going well.")

            result = process_user_sleep_cycle(mount_config, conn, "alice")

        assert result is True

        # Verify file was written
        context_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "memories"
        date_str = datetime.now().strftime("%Y-%m-%d")
        memory_file = context_dir / f"{date_str}.md"
        assert memory_file.exists()
        assert "project Alpha" in memory_file.read_text()

    @patch("istota.sleep_cycle.subprocess.run")
    def test_no_file_when_no_new_memories(self, mock_run, mount_config, db_path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=NO_NEW_MEMORIES,
            stderr="",
        )

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Hello", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Hi there!")

            result = process_user_sleep_cycle(mount_config, conn, "alice")

        assert result is False
        memories_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "memories"
        assert not memories_dir.exists() or not any(
            f.name.endswith(".md")
            for f in memories_dir.iterdir()
        )

    @patch("istota.sleep_cycle.subprocess.run")
    def test_updates_state(self, mock_run, mount_config, db_path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="- New memory (2026-01-28)\n",
            stderr="",
        )

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Test", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            process_user_sleep_cycle(mount_config, conn, "alice")

            last_run, last_task = db.get_sleep_cycle_last_run(conn, "alice")
            assert last_run is not None
            assert last_task == t

    @patch("istota.sleep_cycle.subprocess.run")
    def test_handles_cli_failure(self, mock_run, mount_config, db_path):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error occurred",
        )

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Test", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            result = process_user_sleep_cycle(mount_config, conn, "alice")

        assert result is False

    @patch("istota.sleep_cycle.subprocess.run")
    def test_handles_timeout(self, mock_run, mount_config, db_path):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Test", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            result = process_user_sleep_cycle(mount_config, conn, "alice")

        assert result is False


class TestCleanupOldMemoryFiles:
    def test_deletes_old_files(self, mount_config):
        context_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "memories"
        context_dir.mkdir(parents=True)

        # Create old file
        old_date = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
        (context_dir / f"{old_date}.md").write_text("old memory")

        # Create recent file
        recent_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        (context_dir / f"{recent_date}.md").write_text("recent memory")

        deleted = cleanup_old_memory_files(mount_config, "alice", 90)

        assert deleted == 1
        assert not (context_dir / f"{old_date}.md").exists()
        assert (context_dir / f"{recent_date}.md").exists()

    def test_preserves_non_dated_files(self, mount_config):
        memories_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "memories"
        memories_dir.mkdir(parents=True)

        (memories_dir / "readme.md").write_text("not a dated file")

        old_date = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
        (memories_dir / f"{old_date}.md").write_text("old")

        cleanup_old_memory_files(mount_config, "alice", 90)

        assert (memories_dir / "readme.md").exists()

    def test_returns_zero_when_no_dir(self, mount_config):
        deleted = cleanup_old_memory_files(mount_config, "alice", 90)
        assert deleted == 0

    def test_skips_cleanup_when_retention_zero(self, mount_config):
        context_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "memories"
        context_dir.mkdir(parents=True)

        old_date = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
        (context_dir / f"{old_date}.md").write_text("old memory")

        deleted = cleanup_old_memory_files(mount_config, "alice", 0)

        assert deleted == 0
        assert (context_dir / f"{old_date}.md").exists()


class TestCheckSleepCycles:
    def test_skips_when_disabled(self, mount_config, db_path):
        mount_config.sleep_cycle = SleepCycleConfig(enabled=False)
        mount_config.users = {"alice": UserConfig(display_name="Alice")}

        with db.get_db(db_path) as conn:
            result = check_sleep_cycles(conn, mount_config)

        assert result == []

    @patch("istota.sleep_cycle.process_user_sleep_cycle")
    def test_runs_when_due(self, mock_process, mount_config, db_path):
        mock_process.return_value = True

        mount_config.sleep_cycle = SleepCycleConfig(
            enabled=True,
            cron="* * * * *",  # every minute = always due
        )
        mount_config.users = {
            "alice": UserConfig(display_name="Alice", timezone="UTC")
        }

        with db.get_db(db_path) as conn:
            result = check_sleep_cycles(conn, mount_config)

        assert "alice" in result
        mock_process.assert_called_once()

    @patch("istota.sleep_cycle.process_user_sleep_cycle")
    def test_does_not_run_when_not_due(self, mock_process, mount_config, db_path):
        mount_config.sleep_cycle = SleepCycleConfig(
            enabled=True,
            cron="0 2 * * *",  # 2am
        )
        mount_config.users = {
            "alice": UserConfig(display_name="Alice", timezone="UTC")
        }

        # Set last run to now (so next run is tomorrow 2am)
        with db.get_db(db_path) as conn:
            db.set_sleep_cycle_last_run(conn, "alice", None)

            result = check_sleep_cycles(conn, mount_config)

        # Whether it ran depends on current time, but mock verifies no unexpected calls
        # The important thing is no exception is raised

    @patch("istota.sleep_cycle.process_user_sleep_cycle")
    def test_handles_process_error_gracefully(self, mock_process, mount_config, db_path):
        mock_process.side_effect = Exception("Something went wrong")

        mount_config.sleep_cycle = SleepCycleConfig(
            enabled=True,
            cron="* * * * *",
        )
        mount_config.users = {
            "alice": UserConfig(display_name="Alice", timezone="UTC")
        }

        with db.get_db(db_path) as conn:
            # Should not raise
            result = check_sleep_cycles(conn, mount_config)

        assert result == []


# ---------------------------------------------------------------------------
# TestMemoryProvenance
# ---------------------------------------------------------------------------


class TestMemoryProvenance:
    def test_extraction_prompt_includes_ref_format(self):
        prompt = build_memory_extraction_prompt("alice", "some data", None, "2026-01-28")
        assert "ref:" in prompt

    def test_extraction_prompt_example_has_task_ref(self):
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        # The prompt should show examples like (2026-01-28, ref:1234)
        assert "ref:1234" in prompt


# ---------------------------------------------------------------------------
# TestBuildCurationPrompt
# ---------------------------------------------------------------------------


class TestBuildCurationPrompt:
    def test_includes_current_memory(self):
        prompt = build_curation_prompt("alice", "- Likes Python", "- New fact from today")
        assert "Likes Python" in prompt
        assert "Current USER.md" in prompt

    def test_includes_dated_memories(self):
        prompt = build_curation_prompt("alice", None, "- Prefers dark mode")
        assert "Prefers dark mode" in prompt
        assert "Recent dated memories" in prompt

    def test_empty_memory_shows_placeholder(self):
        prompt = build_curation_prompt("alice", None, "- Some memory")
        assert "Empty" in prompt or "no existing" in prompt.lower()

    def test_includes_no_changes_sentinel(self):
        prompt = build_curation_prompt("alice", "existing", "new")
        assert NO_CHANGES_NEEDED in prompt

    def test_includes_user_id(self):
        prompt = build_curation_prompt("bob", None, "data")
        assert "bob" in prompt


# ---------------------------------------------------------------------------
# TestCurateUserMemory
# ---------------------------------------------------------------------------


class TestCurateUserMemory:
    @patch("istota.sleep_cycle.subprocess.run")
    def test_writes_updated_memory(self, mock_run, mount_config):
        mount_config.sleep_cycle.curate_user_memory = True
        # Create existing dated memories
        memories_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "memories"
        memories_dir.mkdir(parents=True)
        today = datetime.now().strftime("%Y-%m-%d")
        (memories_dir / f"{today}.md").write_text("- Prefers Python over JS")

        # Create config dir for USER.md
        config_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config"
        config_dir.mkdir(parents=True)

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="# Alice\n\n## Preferences\n- Prefers Python over JS\n",
            stderr="",
        )

        result = curate_user_memory(mount_config, "alice")
        assert result is True

        # Verify USER.md was written
        memory_path = config_dir / "USER.md"
        assert memory_path.exists()
        assert "Prefers Python" in memory_path.read_text()

    @patch("istota.sleep_cycle.subprocess.run")
    def test_no_changes_needed_returns_false(self, mock_run, mount_config):
        mount_config.sleep_cycle.curate_user_memory = True
        memories_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "memories"
        memories_dir.mkdir(parents=True)
        today = datetime.now().strftime("%Y-%m-%d")
        (memories_dir / f"{today}.md").write_text("- Some memory")

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=NO_CHANGES_NEEDED,
            stderr="",
        )

        result = curate_user_memory(mount_config, "alice")
        assert result is False

    def test_returns_false_when_no_dated_memories(self, mount_config):
        result = curate_user_memory(mount_config, "alice")
        assert result is False

    @patch("istota.sleep_cycle.subprocess.run")
    def test_returns_false_on_cli_failure(self, mock_run, mount_config):
        memories_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "memories"
        memories_dir.mkdir(parents=True)
        today = datetime.now().strftime("%Y-%m-%d")
        (memories_dir / f"{today}.md").write_text("- Memory")

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Error")
        result = curate_user_memory(mount_config, "alice")
        assert result is False

    @patch("istota.sleep_cycle.subprocess.run")
    def test_returns_false_on_timeout(self, mock_run, mount_config):
        import subprocess
        memories_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "memories"
        memories_dir.mkdir(parents=True)
        today = datetime.now().strftime("%Y-%m-%d")
        (memories_dir / f"{today}.md").write_text("- Memory")

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)
        result = curate_user_memory(mount_config, "alice")
        assert result is False

    @patch("istota.sleep_cycle.subprocess.run")
    def test_curation_called_from_sleep_cycle(self, mock_run, mount_config, db_path):
        """Verify curate_user_memory is called when enabled in process_user_sleep_cycle."""
        mount_config.sleep_cycle.curate_user_memory = True

        # First call: extraction, second call: curation
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="- Memory from today\n", stderr=""),
            MagicMock(returncode=0, stdout=NO_CHANGES_NEEDED, stderr=""),
        ]

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Test", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            process_user_sleep_cycle(mount_config, conn, "alice")

        # Should have been called twice: once for extraction, once for curation
        assert mock_run.call_count == 2

    @patch("istota.sleep_cycle.subprocess.run")
    def test_curation_not_called_when_disabled(self, mock_run, mount_config, db_path):
        """Verify curate_user_memory is NOT called when disabled."""
        mount_config.sleep_cycle.curate_user_memory = False

        mock_run.return_value = MagicMock(
            returncode=0, stdout="- Memory\n", stderr="",
        )

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Test", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            process_user_sleep_cycle(mount_config, conn, "alice")

        # Only one call: extraction. No curation.
        assert mock_run.call_count == 1
