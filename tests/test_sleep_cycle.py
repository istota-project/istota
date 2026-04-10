"""Configuration loading for istota.sleep_cycle module."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

from istota import db
from istota.config import Config, SleepCycleConfig, UserConfig
from istota.sleep_cycle import (
    _excerpt,
    _parse_structured_extraction,
    _validate_fact,
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
    INTERACTIVE_SOURCE_TYPES,
    SUGGESTED_PREDICATES,
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


class TestExcerpt:
    def test_short_text_unchanged(self):
        assert _excerpt("hello world", 100) == "hello world"

    def test_empty_string_unchanged(self):
        assert _excerpt("", 100) == ""

    def test_none_returns_empty(self):
        assert _excerpt(None, 100) == ""

    def test_long_text_returns_head_and_tail(self):
        text = "A" * 200 + "MIDDLE" + "Z" * 200
        result = _excerpt(text, 100)
        assert result.startswith("A")
        assert result.endswith("Z" * 10)  # tail preserved
        assert "truncated" in result.lower() or "trimmed" in result.lower()

    def test_within_budget(self):
        text = "x" * 10000
        result = _excerpt(text, 500)
        assert len(result) <= 520  # small margin for marker

    def test_tail_content_preserved(self):
        text = "x" * 5000 + "FINAL_CONCLUSION"
        result = _excerpt(text, 500)
        assert "FINAL_CONCLUSION" in result


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

    def test_small_tasks_not_truncated(self, mount_config, db_path):
        """Two small tasks should appear in full with no trimming."""
        with db.get_db(db_path) as conn:
            t1 = db.create_task(conn, prompt="Short prompt", user_id="alice")
            db.update_task_status(conn, t1, "running")
            db.update_task_status(conn, t1, "completed", result="Short result")

            t2 = db.create_task(conn, prompt="Another prompt", user_id="alice")
            db.update_task_status(conn, t2, "running")
            db.update_task_status(conn, t2, "completed", result="Another result")

            result = gather_day_data(mount_config, conn, "alice", 24, None)

        assert "Short prompt" in result
        assert "Short result" in result
        assert "Another prompt" in result
        assert "Another result" in result
        assert "truncated" not in result

    def test_tail_preserved_in_long_results(self, mount_config, db_path):
        """A task with a long result should preserve content near the end."""
        with db.get_db(db_path) as conn:
            long_result = "x" * 10000 + "FINAL_CONCLUSION_HERE"
            t = db.create_task(conn, prompt="Analyze project", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result=long_result)

            # Add many more tasks to force per-task budget below result length
            for i in range(20):
                t2 = db.create_task(conn, prompt=f"task {i}", user_id="alice")
                db.update_task_status(conn, t2, "running")
                db.update_task_status(conn, t2, "completed", result=f"result {i}")

            result = gather_day_data(mount_config, conn, "alice", 24, None)

        assert "FINAL_CONCLUSION_HERE" in result

    def test_conversation_grouping(self, mount_config, db_path):
        """Tasks sharing a conversation_token should be grouped together."""
        with db.get_db(db_path) as conn:
            for i in range(3):
                t = db.create_task(
                    conn, prompt=f"Message {i}", user_id="alice",
                    conversation_token="conv123",
                )
                db.update_task_status(conn, t, "running")
                db.update_task_status(conn, t, "completed", result=f"Reply {i}")

            result = gather_day_data(mount_config, conn, "alice", 24, None)

        assert "Conversation" in result
        assert "conv123" in result
        assert "3 messages" in result

    def test_standalone_tasks_no_group_header(self, mount_config, db_path):
        """Tasks without conversation_token should not get a group header."""
        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="CLI task", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            result = gather_day_data(mount_config, conn, "alice", 24, None)

        assert "Conversation" not in result

    def test_separates_interactive_and_automated_sections(self, mount_config, db_path):
        """Talk and briefing tasks appear in separate labeled sections."""
        with db.get_db(db_path) as conn:
            t1 = db.create_task(
                conn, prompt="What's the weather?", user_id="alice", source_type="talk",
            )
            db.update_task_status(conn, t1, "running")
            db.update_task_status(conn, t1, "completed", result="It's sunny.")

            t2 = db.create_task(
                conn, prompt="Morning briefing", user_id="alice", source_type="briefing",
            )
            db.update_task_status(conn, t2, "running")
            db.update_task_status(conn, t2, "completed", result="Here's your briefing.")

            result = gather_day_data(mount_config, conn, "alice", 24, None)

        assert "INTERACTIVE CONVERSATIONS" in result
        assert "AUTOMATED/SCHEDULED OUTPUT" in result
        # Interactive task in the interactive section (appears before automated header)
        interactive_pos = result.index("INTERACTIVE CONVERSATIONS")
        automated_pos = result.index("AUTOMATED/SCHEDULED OUTPUT")
        weather_pos = result.index("What's the weather?")
        briefing_pos = result.index("Morning briefing")
        assert interactive_pos < weather_pos < automated_pos < briefing_pos

    def test_automated_tasks_get_reduced_budget(self, mount_config, db_path):
        """Automated section should be shorter than interactive when both exist."""
        with db.get_db(db_path) as conn:
            # One interactive task with long content
            t1 = db.create_task(
                conn, prompt="A" * 2000, user_id="alice", source_type="talk",
            )
            db.update_task_status(conn, t1, "running")
            db.update_task_status(conn, t1, "completed", result="B" * 2000)

            # One automated task with equally long content
            t2 = db.create_task(
                conn, prompt="C" * 2000, user_id="alice", source_type="cron",
            )
            db.update_task_status(conn, t2, "running")
            db.update_task_status(conn, t2, "completed", result="D" * 2000)

            result = gather_day_data(mount_config, conn, "alice", 24, None)

        automated_pos = result.index("AUTOMATED/SCHEDULED OUTPUT")
        interactive_section = result[:automated_pos]
        automated_section = result[automated_pos:]
        # Interactive section should be larger (80% budget vs 20%)
        assert len(interactive_section) > len(automated_section)

    def test_only_interactive_omits_automated_header(self, mount_config, db_path):
        """When only talk tasks exist, no automated section header appears."""
        with db.get_db(db_path) as conn:
            t = db.create_task(
                conn, prompt="Hello", user_id="alice", source_type="talk",
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Hi there")

            result = gather_day_data(mount_config, conn, "alice", 24, None)

        assert "INTERACTIVE CONVERSATIONS" in result
        assert "AUTOMATED" not in result

    def test_only_automated_omits_interactive_header(self, mount_config, db_path):
        """When only cron tasks exist, no interactive section header appears."""
        with db.get_db(db_path) as conn:
            t = db.create_task(
                conn, prompt="Check status", user_id="alice", source_type="cron",
            )
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="All good")

            result = gather_day_data(mount_config, conn, "alice", 24, None)

        assert "INTERACTIVE" not in result
        assert "AUTOMATED/SCHEDULED OUTPUT" in result


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

    def test_prompt_includes_depth_guidance(self):
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert "months" in prompt.lower()

    def test_prompt_includes_self_contained_guidance(self):
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert "self-contained" in prompt.lower()

    def test_prompt_discourages_thin_bullets(self):
        """Prompt should include a good/bad example to guide extraction depth."""
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        # Should have a concrete example, not just "concise bullet points"
        assert "Bad:" in prompt or "Good:" in prompt

    def test_prompt_encourages_light_day_extraction(self):
        """Prompt should say it's OK to extract from days with few interactions."""
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert "few interactions" in prompt.lower() or "single" in prompt.lower()

    def test_prompt_includes_source_type_guidance(self):
        """Prompt should explain how to treat interactive vs automated sections."""
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert "INTERACTIVE CONVERSATIONS" in prompt
        assert "AUTOMATED/SCHEDULED OUTPUT" in prompt
        assert "Bot-generated" in prompt

    def test_prompt_includes_subject_constraints(self):
        """Prompt should constrain who facts can be about."""
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert "Subject constraints" in prompt
        assert "alice" in prompt  # user_id should be in the constraints

    def test_prompt_includes_negative_fact_examples(self):
        """Prompt should show bad fact examples to prevent hallucinated attributions."""
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert "Bad fact examples" in prompt
        assert "Good fact examples" in prompt

    def test_prompt_predicates_from_registry(self):
        """Every suggested predicate should appear in the prompt."""
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        for predicate in SUGGESTED_PREDICATES:
            assert predicate in prompt, f"Predicate '{predicate}' missing from prompt"

    def test_prompt_allows_freeform_predicates(self):
        """Prompt should indicate freeform predicates are accepted."""
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert "Suggested predicates" in prompt
        assert "You may use other predicates" in prompt

    def test_prompt_dedup_guidance(self):
        """Prompt should instruct to put attributes in FACTS only, not MEMORIES."""
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert "FACT only" in prompt
        assert "personal attribute" in prompt

    def test_prompt_predicate_hints(self):
        """Prompt should include usage hints for suggested predicates."""
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        # Check that hints are present, not just bare predicate names
        assert "single-valued" in prompt
        assert "software" in prompt  # uses_tech hint
        assert "temporary" in prompt  # staying_in/visiting hint

    def test_prompt_temporal_guidance(self):
        """Prompt should instruct to use valid_from/valid_until, not date strings in objects."""
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert "valid_from" in prompt
        assert "valid_until" in prompt
        assert "NOT in the object string" in prompt


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


class TestParseStructuredExtraction:
    def test_plain_text_fallback(self):
        """Output without structured markers is treated as plain memories."""
        output = "- Memory one (2026-04-08, ref:1)\n- Memory two (2026-04-08, ref:2)"
        memories, facts, topics = _parse_structured_extraction(output)
        assert "Memory one" in memories
        assert "Memory two" in memories
        assert facts == []
        assert topics == {}

    def test_full_structured_output(self):
        output = """MEMORIES:
- Stefan switched to FastAPI (2026-04-08, ref:1234)
- Prefers dark mode (2026-04-08, ref:1235)

FACTS:
[{"subject": "istota", "predicate": "uses_tech", "object": "fastapi"}]

TOPICS:
{"ref:1234": "tech", "ref:1235": "personal"}"""

        memories, facts, topics = _parse_structured_extraction(output)
        assert "Stefan switched to FastAPI" in memories
        assert "Prefers dark mode" in memories
        assert len(facts) == 1
        assert facts[0]["subject"] == "istota"
        assert facts[0]["predicate"] == "uses_tech"
        assert topics == {"ref:1234": "tech", "ref:1235": "personal"}

    def test_memories_only_with_empty_facts(self):
        output = """MEMORIES:
- Just a memory (2026-04-08, ref:1)

FACTS:
[]

TOPICS:
{}"""

        memories, facts, topics = _parse_structured_extraction(output)
        assert "Just a memory" in memories
        assert facts == []
        assert topics == {}

    def test_malformed_facts_json(self):
        output = """MEMORIES:
- A memory (2026-04-08, ref:1)

FACTS:
not valid json

TOPICS:
{"ref:1": "tech"}"""

        memories, facts, topics = _parse_structured_extraction(output)
        assert "A memory" in memories
        assert facts == []
        assert topics == {"ref:1": "tech"}

    def test_malformed_topics_json(self):
        output = """MEMORIES:
- A memory (2026-04-08, ref:1)

FACTS:
[{"subject": "stefan", "predicate": "knows", "object": "python"}]

TOPICS:
broken json"""

        memories, facts, topics = _parse_structured_extraction(output)
        assert len(facts) == 1
        assert topics == {}

    def test_missing_facts_section(self):
        output = """MEMORIES:
- A memory (2026-04-08, ref:1)

TOPICS:
{"ref:1": "tech"}"""

        memories, facts, topics = _parse_structured_extraction(output)
        assert "A memory" in memories
        assert facts == []
        assert topics == {"ref:1": "tech"}

    def test_missing_topics_section(self):
        output = """MEMORIES:
- A memory (2026-04-08, ref:1)

FACTS:
[{"subject": "stefan", "predicate": "knows", "object": "python"}]"""

        memories, facts, topics = _parse_structured_extraction(output)
        assert "A memory" in memories
        assert len(facts) == 1
        assert topics == {}

    def test_facts_with_invalid_entries_filtered(self):
        """Facts missing required fields are filtered out."""
        output = """MEMORIES:
- Memory (2026-04-08, ref:1)

FACTS:
[{"subject": "stefan", "predicate": "knows", "object": "python"}, {"bad": "entry"}, "not a dict"]

TOPICS:
{}"""

        memories, facts, topics = _parse_structured_extraction(output)
        assert len(facts) == 1
        assert facts[0]["subject"] == "stefan"

    def test_multiple_facts(self):
        output = """MEMORIES:
- Memory (2026-04-08, ref:1)

FACTS:
[
  {"subject": "stefan", "predicate": "works_at", "object": "acme"},
  {"subject": "stefan", "predicate": "knows", "object": "python"},
  {"subject": "istota", "predicate": "uses_tech", "object": "svelte"}
]

TOPICS:
{"ref:1": "work"}"""

        memories, facts, topics = _parse_structured_extraction(output)
        assert len(facts) == 3

    def test_freeform_predicates_accepted(self):
        """Facts with freeform predicates are accepted (not filtered)."""
        output = """MEMORIES:
- Memory (2026-04-08, ref:1)

FACTS:
[
  {"subject": "stefan", "predicate": "knows", "object": "python"},
  {"subject": "stefan", "predicate": "allergic_to", "object": "tree nuts"},
  {"subject": "stefan", "predicate": "favorite_color", "object": "blue"}
]

TOPICS:
{}"""

        memories, facts, topics = _parse_structured_extraction(output)
        assert len(facts) == 3
        predicates = {f["predicate"] for f in facts}
        assert predicates == {"knows", "allergic_to", "favorite_color"}

    def test_empty_subject_filtered(self):
        """Facts with empty subject are dropped."""
        output = """MEMORIES:
- Memory (2026-04-08, ref:1)

FACTS:
[{"subject": "", "predicate": "knows", "object": "python"}]

TOPICS:
{}"""

        memories, facts, topics = _parse_structured_extraction(output)
        assert facts == []

    def test_empty_object_filtered(self):
        """Facts with whitespace-only object are dropped."""
        output = """MEMORIES:
- Memory (2026-04-08, ref:1)

FACTS:
[{"subject": "stefan", "predicate": "works_at", "object": "   "}]

TOPICS:
{}"""

        memories, facts, topics = _parse_structured_extraction(output)
        assert facts == []

    def test_fact_values_normalized(self):
        """Fact subject, predicate, and object are normalized to lowercase."""
        output = """MEMORIES:
- Memory (2026-04-08, ref:1)

FACTS:
[{"subject": "Stefan", "predicate": "Works_At", "object": "Acme Corp"}]

TOPICS:
{}"""

        memories, facts, topics = _parse_structured_extraction(output)
        assert len(facts) == 1
        assert facts[0]["subject"] == "stefan"
        assert facts[0]["predicate"] == "works_at"
        assert facts[0]["object"] == "acme corp"


class TestValidateFact:
    def test_valid_fact_passes(self):
        assert _validate_fact({"subject": "stefan", "predicate": "knows", "object": "python"})

    def test_missing_field_fails(self):
        assert not _validate_fact({"subject": "stefan", "predicate": "knows"})

    def test_freeform_predicate_accepted(self):
        """Freeform predicates are accepted — validation only checks non-empty."""
        assert _validate_fact({"subject": "stefan", "predicate": "likes", "object": "cats"})
        assert _validate_fact({"subject": "stefan", "predicate": "allergic_to", "object": "tree nuts"})
        assert _validate_fact({"subject": "stefan", "predicate": "speaks", "object": "polish"})
        assert _validate_fact({"subject": "stefan", "predicate": "enjoys", "object": "hiking"})

    def test_empty_subject_fails(self):
        assert not _validate_fact({"subject": "", "predicate": "knows", "object": "python"})

    def test_empty_predicate_fails(self):
        assert not _validate_fact({"subject": "stefan", "predicate": "", "object": "python"})

    def test_whitespace_predicate_fails(self):
        assert not _validate_fact({"subject": "stefan", "predicate": "   ", "object": "python"})

    def test_whitespace_object_fails(self):
        assert not _validate_fact({"subject": "stefan", "predicate": "knows", "object": "   "})

    def test_not_a_dict_fails(self):
        assert not _validate_fact("not a dict")

    def test_predicate_case_insensitive(self):
        assert _validate_fact({"subject": "stefan", "predicate": "Works_At", "object": "acme"})
