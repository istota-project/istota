"""Configuration loading for istota.sleep_cycle module."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

from istota import db
from istota.config import Config, SleepCycleConfig, UserConfig
from istota.memory.sleep_cycle import (
    _excerpt,
    _parse_structured_extraction,
    _topics_per_chunk,
    _validate_fact,
    gather_day_data,
    build_memory_extraction_prompt,
    process_user_sleep_cycle,
    cleanup_old_memory_files,
    check_sleep_cycles,
    curate_user_memory,
    NO_NEW_MEMORIES,
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

    def test_prompt_includes_existing_facts_when_provided(self):
        """When existing_facts is given, the prompt should include them and tell the LLM not to re-emit."""
        existing = "- stefan works_at acme\n- felix allergic_to eggs"
        prompt = build_memory_extraction_prompt(
            "alice", "data", None, "2026-01-28", existing_facts=existing
        )
        assert "stefan works_at acme" in prompt
        assert "felix allergic_to eggs" in prompt
        # Some form of "do not re-emit" guidance should appear near the facts section
        assert "knowledge graph" in prompt.lower()
        assert "do not re-emit" in prompt.lower() or "do not repeat" in prompt.lower()

    def test_prompt_omits_kg_section_when_no_existing_facts(self):
        """No existing_facts → no KG section in the prompt."""
        prompt = build_memory_extraction_prompt(
            "alice", "data", None, "2026-01-28", existing_facts=None
        )
        assert "knowledge graph" not in prompt.lower() or "Existing knowledge graph" not in prompt

    def test_prompt_includes_object_length_constraint(self):
        """Prompt should constrain object value length explicitly."""
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert "10 words" in prompt or "under 10" in prompt or "max" in prompt.lower()

    def test_prompt_includes_decided_expiry_guidance(self):
        """Prompt should tell LLM to set valid_until on one-time `decided` facts."""
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert "decided" in prompt
        # Some hint about one-time / cancellation / aging out via valid_until
        assert "one-time" in prompt.lower() or "age out" in prompt.lower() or "cancellation" in prompt.lower()

    def test_prompt_includes_source_ref_field(self):
        """FACTS schema should mention source_ref so the LLM attaches a task id."""
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        assert "source_ref" in prompt

    def test_prompt_includes_new_predicates(self):
        """Expanded predicate vocabulary (item 5)."""
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-01-28")
        for pred in ("has_family_member", "traveled_to", "completed", "has_appointment", "interested_in"):
            assert pred in prompt, f"Predicate '{pred}' missing from prompt"


class TestProcessUserSleepCycle:
    def test_skips_when_no_interactions(self, mount_config, db_path):
        with db.get_db(db_path) as conn:
            result = process_user_sleep_cycle(mount_config, conn, "alice")
        assert result is False

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_writes_memory_file(self, mock_run, mount_config, db_path):
        mock_run.return_value = (True, "- Discussed project Alpha (2026-01-28)\n")

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Tell me about project Alpha", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Project Alpha is going well.")

            result = process_user_sleep_cycle(mount_config, conn, "alice")

        assert result is True

        # Verify file was written. Filename uses user-local date; this
        # fixture has no configured user, so the sleep cycle falls back to UTC.
        context_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "memories"
        date_str = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")
        memory_file = context_dir / f"{date_str}.md"
        assert memory_file.exists()
        assert "project Alpha" in memory_file.read_text()

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_no_file_when_no_new_memories(self, mock_run, mount_config, db_path):
        mock_run.return_value = (True, NO_NEW_MEMORIES)

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

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_updates_state(self, mock_run, mount_config, db_path):
        mock_run.return_value = (True, "- New memory (2026-01-28)\n")

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Test", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            process_user_sleep_cycle(mount_config, conn, "alice")

            last_run, last_task = db.get_sleep_cycle_last_run(conn, "alice")
            assert last_run is not None
            assert last_task == t

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_handles_cli_failure(self, mock_run, mount_config, db_path):
        mock_run.return_value = (False, "")

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Test", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            result = process_user_sleep_cycle(mount_config, conn, "alice")

        assert result is False

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_handles_timeout(self, mock_run, mount_config, db_path):
        # Brain helper returns (False, "") for any failure incl. timeout.
        mock_run.return_value = (False, "")

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Test", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            result = process_user_sleep_cycle(mount_config, conn, "alice")

        assert result is False

    def test_extraction_routes_through_make_brain(self, mount_config, db_path):
        """The user sleep cycle must invoke `make_brain(config.brain).execute(req)`,
        not bypass it with a direct subprocess call."""
        from istota.brain import BrainResult
        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Test", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            with patch("istota.memory.sleep_cycle.make_brain") as mock_make_brain:
                mock_make_brain.return_value.execute.return_value = BrainResult(
                    success=True,
                    result_text="- A memory\n",
                )
                process_user_sleep_cycle(mount_config, conn, "alice")
                mock_make_brain.assert_called()
                # The BrainRequest should have empty allowed_tools (no tools, no
                # streaming) to keep the call privileged + text-only.
                req = mock_make_brain.return_value.execute.call_args[0][0]
                assert req.allowed_tools == []
                assert req.streaming is False
                assert req.sandbox_wrap is None
                assert req.on_progress is None
                assert req.cancel_check is None

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_skips_write_when_no_bullets_in_memories(self, mock_run, mount_config, db_path):
        """If MEMORIES section has no bullet points, treat as malformed and skip write (item 2)."""
        mock_run.return_value = (True, "Memory extraction complete. Saved to disk.")

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Test", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")
            result = process_user_sleep_cycle(mount_config, conn, "alice")

        assert result is False
        memories_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "memories"
        date_str = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")
        assert not (memories_dir / f"{date_str}.md").exists() if memories_dir.exists() else True

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_passes_source_task_id_from_source_ref(self, mock_run, mount_config, db_path):
        """source_ref in FACTS JSON should propagate to add_fact(source_task_id=...) (item 3)."""
        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="I'm allergic to eggs", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Noted.")

            output = (
                "MEMORIES:\n- A note (2026-01-28, ref:%d)\n\n"
                "FACTS:\n"
                '[{"subject": "alice", "predicate": "allergic_to", "object": "eggs", "source_ref": %d}]\n\n'
                "TOPICS:\n{}"
            ) % (t, t)
            mock_run.return_value = (True, output)

            process_user_sleep_cycle(mount_config, conn, "alice")

            # The extracted fact should have source_task_id set to the task id
            row = conn.execute(
                "SELECT source_task_id FROM knowledge_facts WHERE user_id=? AND predicate='allergic_to'",
                ("alice",),
            ).fetchone()
            assert row is not None
            assert row[0] == t

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_extraction_prompt_receives_existing_kg_facts(self, mock_run, mount_config, db_path):
        """process_user_sleep_cycle should load current facts and pass them into the prompt (item 1)."""
        from istota.memory.knowledge_graph import add_fact, ensure_table

        mock_run.return_value = (True, "- A new memory (2026-01-28, ref:1)\n")

        with db.get_db(db_path) as conn:
            ensure_table(conn)
            add_fact(conn, "alice", subject="alice", predicate="works_at", object_val="acme")

            t = db.create_task(conn, prompt="Test", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            process_user_sleep_cycle(mount_config, conn, "alice")

        # The brain was called once for extraction; check that the prompt
        # included the existing KG fact.
        call_prompt = mock_run.call_args.args[1]
        assert "alice works_at acme" in call_prompt

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_dated_filename_uses_user_timezone(self, mock_run, mount_config, db_path):
        """Filename should reflect the user's calendar day, not the server's."""
        mock_run.return_value = (True, "- A memory\n")
        # Configure a user in a far-east tz so server-local UTC and user-local
        # date differ for some hours of the day. This test asserts that the
        # written file name matches the user-local date, regardless of when
        # the test happens to run.
        mount_config.users["alice"] = UserConfig(timezone="Pacific/Kiritimati")  # UTC+14

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Test", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")

            process_user_sleep_cycle(mount_config, conn, "alice")

        context_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "memories"
        expected_date = datetime.now(ZoneInfo("Pacific/Kiritimati")).strftime("%Y-%m-%d")
        assert (context_dir / f"{expected_date}.md").exists()


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

    def test_cutoff_uses_user_timezone(self, mount_config):
        # Sleep cycle writes filenames in the user's tz; cleanup must use
        # the same tz when computing the cutoff or the boundary day can
        # be evicted ~1 day too early/late.
        mount_config.users["alice"] = UserConfig(timezone="Pacific/Kiritimati")
        context_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "memories"
        context_dir.mkdir(parents=True)

        user_tz = ZoneInfo("Pacific/Kiritimati")
        # `today` in the user's tz must NOT be deleted with retention=1.
        today_user = datetime.now(user_tz).strftime("%Y-%m-%d")
        (context_dir / f"{today_user}.md").write_text("today")
        # A clearly-old file should still be deleted.
        old_date = (datetime.now(user_tz) - timedelta(days=10)).strftime("%Y-%m-%d")
        (context_dir / f"{old_date}.md").write_text("old")

        deleted = cleanup_old_memory_files(mount_config, "alice", 1)

        assert deleted == 1
        assert (context_dir / f"{today_user}.md").exists()
        assert not (context_dir / f"{old_date}.md").exists()


class TestSleepCycleChunkCleanup:
    """Item 4: nightly user sleep cycle prunes old ephemeral memory_chunks."""

    @patch("istota.memory.search.cleanup_old_chunks")
    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_chunk_cleanup_called_when_retention_set(
        self, mock_run, mock_cleanup, mount_config, db_path
    ):
        mount_config.sleep_cycle.memory_retention_days = 90
        mock_run.return_value = (True, "- mem\n")
        mock_cleanup.return_value = 0

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="x", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="r")
            process_user_sleep_cycle(mount_config, conn, "alice")

        assert mock_cleanup.called
        # Args: (conn, user_id, retention_days)
        args = mock_cleanup.call_args.args
        assert args[1] == "alice"
        assert args[2] == 90

    @patch("istota.memory.search.cleanup_old_chunks")
    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_chunk_cleanup_skipped_when_retention_zero(
        self, mock_run, mock_cleanup, mount_config, db_path
    ):
        mount_config.sleep_cycle.memory_retention_days = 0
        mock_run.return_value = (True, "- mem\n")

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="x", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="r")
            process_user_sleep_cycle(mount_config, conn, "alice")

        mock_cleanup.assert_not_called()


class TestSleepCycleKGAuditCleanup:
    """KG audit pruning runs on its own knob, not memory_retention_days."""

    @patch("istota.memory.knowledge_graph.cleanup_old_audit_rows")
    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_audit_cleanup_runs_when_memory_retention_zero(
        self, mock_run, mock_cleanup, mount_config, db_path
    ):
        # Default deployments have memory_retention_days=0 (unlimited),
        # but the KG audit table must still be pruned.
        mount_config.sleep_cycle.memory_retention_days = 0
        mount_config.sleep_cycle.knowledge_graph_audit_retention_days = 365
        mock_run.return_value = (True, "- mem\n")
        mock_cleanup.return_value = 0

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="x", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="r")
            process_user_sleep_cycle(mount_config, conn, "alice")

        assert mock_cleanup.called
        args = mock_cleanup.call_args.args
        assert args[1] == "alice"
        assert args[2] == 365

    @patch("istota.memory.knowledge_graph.cleanup_old_audit_rows")
    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_audit_cleanup_skipped_when_audit_retention_zero(
        self, mock_run, mock_cleanup, mount_config, db_path
    ):
        mount_config.sleep_cycle.memory_retention_days = 90
        mount_config.sleep_cycle.knowledge_graph_audit_retention_days = 0
        mock_run.return_value = (True, "- mem\n")

        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="x", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="r")
            process_user_sleep_cycle(mount_config, conn, "alice")

        mock_cleanup.assert_not_called()


class TestCheckSleepCycles:
    def test_skips_when_disabled(self, mount_config, db_path):
        mount_config.sleep_cycle = SleepCycleConfig(enabled=False)
        mount_config.users = {"alice": UserConfig(display_name="Alice")}

        with db.get_db(db_path) as conn:
            result = check_sleep_cycles(conn, mount_config)

        assert result == []

    @patch("istota.memory.sleep_cycle.process_user_sleep_cycle")
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

    @patch("istota.memory.sleep_cycle.process_user_sleep_cycle")
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

    @patch("istota.memory.sleep_cycle.process_user_sleep_cycle")
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
# TestCurateUserMemory (op-based curation)
# ---------------------------------------------------------------------------


def _setup_curation_fixture(mount_config, *, existing_user_md: str | None = None) -> tuple:
    """Create dated memories + (optionally) an existing USER.md. Returns (config_dir, memories_dir)."""
    memories_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "memories"
    memories_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    (memories_dir / f"{today}.md").write_text("- Prefers Python over JS\n")
    config_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    if existing_user_md is not None:
        (config_dir / "USER.md").write_text(existing_user_md)
    return config_dir, memories_dir


class TestCurateUserMemory:
    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_writes_updated_user_md_when_ops_applied(self, mock_run, mount_config):
        mount_config.sleep_cycle.curate_user_memory = True
        config_dir, _ = _setup_curation_fixture(
            mount_config, existing_user_md="## Preferences\n- Likes vim\n"
        )
        mock_run.return_value = (True, '{"ops": [{"op": "append", "heading": "Preferences", "line": "- Prefers Python over JS"}]}')

        result = curate_user_memory(mount_config, "alice")
        assert result is True
        text = (config_dir / "USER.md").read_text()
        assert "- Likes vim" in text
        assert "- Prefers Python over JS" in text

    @patch("istota.memory.search.index_file")
    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_all_noop_ops_does_not_rewrite_even_with_drift(
        self, mock_run, mock_index, mount_config, db_path
    ):
        """USER.md with formatting drift (trailing whitespace on heading) +
        an op that dedups to a no-op must NOT trigger a write — otherwise
        the parse/serialize round-trip would silently rewrite the file
        every night until it matches the canonical form.
        """
        mount_config.sleep_cycle.curate_user_memory = True
        # Note the trailing whitespace on the heading and the missing trailing
        # newline — both formatting quirks the serializer normalizes away.
        drift_md = "## Preferences  \n- Likes vim"
        config_dir, _ = _setup_curation_fixture(
            mount_config, existing_user_md=drift_md
        )
        # The op deduplicates against the existing bullet → noop_dup outcome.
        mock_run.return_value = (True, '{"ops": [{"op": "append", "heading": "Preferences", "line": "- Likes vim"}]}')
        with db.get_db(db_path) as conn:
            result = curate_user_memory(mount_config, "alice", conn=conn)

        assert result is False
        # File untouched (still has the drift)
        assert (config_dir / "USER.md").read_text() == drift_md
        # No re-index either
        mock_index.assert_not_called()

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_empty_ops_response_does_not_touch_file(self, mock_run, mount_config):
        mount_config.sleep_cycle.curate_user_memory = True
        config_dir, _ = _setup_curation_fixture(
            mount_config, existing_user_md="## Preferences\n- Likes vim\n"
        )
        original = (config_dir / "USER.md").read_text()
        mock_run.return_value = (True, '{"ops": []}')

        result = curate_user_memory(mount_config, "alice")
        assert result is False
        assert (config_dir / "USER.md").read_text() == original

    @patch("istota.memory.search.index_file")
    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_empty_ops_response_does_not_reindex(
        self, mock_run, mock_index, mount_config, db_path
    ):
        mount_config.sleep_cycle.curate_user_memory = True
        _setup_curation_fixture(mount_config, existing_user_md="## A\n- a\n")
        mock_run.return_value = (True, '{"ops": []}')

        with db.get_db(db_path) as conn:
            curate_user_memory(mount_config, "alice", conn=conn)
        mock_index.assert_not_called()

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_invalid_json_returns_false(self, mock_run, mount_config):
        mount_config.sleep_cycle.curate_user_memory = True
        config_dir, _ = _setup_curation_fixture(
            mount_config, existing_user_md="## A\n- a\n"
        )
        mock_run.return_value = (True, "garbage not json")
        result = curate_user_memory(mount_config, "alice")
        assert result is False

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_json_with_fences_is_unwrapped(self, mock_run, mount_config):
        mount_config.sleep_cycle.curate_user_memory = True
        config_dir, _ = _setup_curation_fixture(
            mount_config, existing_user_md="## Preferences\n- Likes vim\n"
        )
        mock_run.return_value = (True, '```json\n{"ops": [{"op": "append", "heading": "Preferences", "line": "- New thing"}]}\n```')
        result = curate_user_memory(mount_config, "alice")
        assert result is True
        assert "- New thing" in (config_dir / "USER.md").read_text()

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_subprocess_timeout_returns_false(self, mock_run, mount_config):
        mount_config.sleep_cycle.curate_user_memory = True
        _setup_curation_fixture(mount_config, existing_user_md="## A\n- a\n")
        # Brain helper collapses timeouts (and other failures) into (False, "").
        mock_run.return_value = (False, "")
        result = curate_user_memory(mount_config, "alice")
        assert result is False

    @patch("istota.memory.search.index_file")
    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_reindexes_user_md_when_ops_applied(
        self, mock_run, mock_index, mount_config, db_path
    ):
        mount_config.sleep_cycle.curate_user_memory = True
        _setup_curation_fixture(mount_config, existing_user_md="## Preferences\n- old\n")
        mock_run.return_value = (True, '{"ops": [{"op": "append", "heading": "Preferences", "line": "- New durable fact"}]}')
        with db.get_db(db_path) as conn:
            result = curate_user_memory(mount_config, "alice", conn=conn)
        assert result is True
        assert mock_index.called
        call = mock_index.call_args
        args = call.args
        kwargs = call.kwargs
        # signature: index_file(conn, user_id, file_path, content, source_type)
        assert (args[1] if len(args) > 1 else kwargs.get("user_id")) == "alice"
        passed_source_type = args[4] if len(args) > 4 else kwargs.get("source_type")
        assert passed_source_type == "user_memory"
        passed_content = args[3] if len(args) > 3 else kwargs.get("content")
        assert "New durable fact" in passed_content

    @patch("istota.memory.search.index_file")
    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_reindex_failure_does_not_break_curation(
        self, mock_run, mock_index, mount_config, db_path
    ):
        mount_config.sleep_cycle.curate_user_memory = True
        config_dir, _ = _setup_curation_fixture(
            mount_config, existing_user_md="## A\n- a\n"
        )
        mock_run.return_value = (True, '{"ops": [{"op": "append", "heading": "A", "line": "- new"}]}')
        mock_index.side_effect = Exception("vec extension borked")
        with db.get_db(db_path) as conn:
            result = curate_user_memory(mount_config, "alice", conn=conn)
        assert result is True
        assert "- new" in (config_dir / "USER.md").read_text()

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_audit_log_written_for_applied_ops(self, mock_run, mount_config):
        mount_config.sleep_cycle.curate_user_memory = True
        config_dir, _ = _setup_curation_fixture(
            mount_config, existing_user_md="## A\n- a\n"
        )
        mock_run.return_value = (True, '{"ops": [{"op": "append", "heading": "A", "line": "- new"}]}')
        curate_user_memory(mount_config, "alice")
        audit_path = config_dir / "USER.md.audit.jsonl"
        assert audit_path.exists()
        import json as _json
        line = audit_path.read_text().strip().splitlines()[0]
        entry = _json.loads(line)
        assert entry["user_id"] == "alice"
        assert any(a["op"]["op"] == "append" for a in entry["applied"])

    @patch("istota.memory.sleep_cycle._post_curation_summary")
    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_log_channel_summary_posted_when_ops_applied_and_configured(
        self, mock_run, mock_post, mount_config
    ):
        mount_config.sleep_cycle.curate_user_memory = True
        mount_config.sleep_cycle.curation_log_summary = True
        _setup_curation_fixture(mount_config, existing_user_md="## A\n- a\n")
        mock_run.return_value = (True, '{"ops": [{"op": "append", "heading": "A", "line": "- new"}]}')
        result = curate_user_memory(mount_config, "alice")
        assert result is True
        assert mock_post.called

    @patch("istota.memory.sleep_cycle._post_curation_summary")
    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_log_channel_summary_skipped_when_disabled(
        self, mock_run, mock_post, mount_config
    ):
        mount_config.sleep_cycle.curate_user_memory = True
        mount_config.sleep_cycle.curation_log_summary = False
        _setup_curation_fixture(mount_config, existing_user_md="## A\n- a\n")
        mock_run.return_value = (True, '{"ops": [{"op": "append", "heading": "A", "line": "- new"}]}')
        curate_user_memory(mount_config, "alice")
        mock_post.assert_not_called()

    def test_returns_false_when_no_dated_memories(self, mount_config):
        result = curate_user_memory(mount_config, "alice")
        assert result is False

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_curation_called_from_sleep_cycle(self, mock_run, mount_config, db_path):
        """Verify curate_user_memory is called when enabled in process_user_sleep_cycle."""
        mount_config.sleep_cycle.curate_user_memory = True
        # First call: extraction, second call: curation
        mock_run.side_effect = [
            (True, "- Memory from today\n"),
            (True, '{"ops": []}'),
        ]
        with db.get_db(db_path) as conn:
            t = db.create_task(conn, prompt="Test", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="Done")
            process_user_sleep_cycle(mount_config, conn, "alice")
        # Should have been called twice: once for extraction, once for curation
        assert mock_run.call_count == 2

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_curation_not_called_when_disabled(self, mock_run, mount_config, db_path):
        """Verify curate_user_memory is NOT called when disabled."""
        mount_config.sleep_cycle.curate_user_memory = False
        mock_run.return_value = (True, "- Memory\n")
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
    def test_rejects_overly_long_object(self):
        """Objects longer than 100 chars are rejected (item 4)."""
        long_obj = "x" * 101
        assert not _validate_fact({"subject": "stefan", "predicate": "decided", "object": long_obj})

    def test_accepts_object_at_length_limit(self):
        """Objects up to 100 chars are still accepted."""
        obj_at_limit = "x" * 100
        assert _validate_fact({"subject": "stefan", "predicate": "decided", "object": obj_at_limit})

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


class TestTopicsPerChunk:
    def test_first_ref_wins_per_chunk(self):
        chunks = [
            "- Project Alpha is going great (2026-01-28, ref:1234)\n"
            "- Felix prefers vim (ref:1235)",
            "- Stefan likes hiking (ref:1236)",
        ]
        topics = {"ref:1234": "tech", "ref:1235": "personal", "ref:1236": "personal"}
        assert _topics_per_chunk(chunks, topics) == ["tech", "personal"]

    def test_chunk_without_ref_gets_none(self):
        chunks = ["- Some bullet without a ref"]
        assert _topics_per_chunk(chunks, {"ref:1234": "tech"}) == [None]

    def test_unknown_ref_gets_none(self):
        chunks = ["- A bullet (ref:9999)"]
        assert _topics_per_chunk(chunks, {"ref:1234": "tech"}) == [None]

    def test_empty_topics_dict(self):
        chunks = ["- A bullet (ref:1234)"]
        assert _topics_per_chunk(chunks, {}) == [None]


class TestUserMemoryObservability:
    def test_warn_below_threshold_is_silent(self, mount_config):
        """Below 8 KB, no notification fires."""
        from istota.memory.sleep_cycle import _maybe_warn_usermd_size, USER_MEMORY_SOFT_WARN_BYTES
        mount_config.users["alice"] = UserConfig(log_channel="logroom")
        with patch("istota.notifications.send_notification") as mock_send:
            _maybe_warn_usermd_size(mount_config, "alice", USER_MEMORY_SOFT_WARN_BYTES - 1)
            mock_send.assert_not_called()

    def test_warn_at_or_above_threshold_posts_to_log_channel(self, mount_config):
        from istota.memory.sleep_cycle import _maybe_warn_usermd_size, USER_MEMORY_SOFT_WARN_BYTES
        mount_config.users["alice"] = UserConfig(log_channel="logroom")
        with patch("istota.notifications.send_notification") as mock_send:
            _maybe_warn_usermd_size(mount_config, "alice", USER_MEMORY_SOFT_WARN_BYTES + 1)
            mock_send.assert_called_once()

    def test_warn_silent_without_log_channel(self, mount_config):
        """No log_channel → no notification, but logger still records."""
        from istota.memory.sleep_cycle import _maybe_warn_usermd_size, USER_MEMORY_SOFT_WARN_BYTES
        mount_config.users["alice"] = UserConfig()  # no log_channel
        with patch("istota.notifications.send_notification") as mock_send:
            _maybe_warn_usermd_size(mount_config, "alice", USER_MEMORY_SOFT_WARN_BYTES + 1)
            mock_send.assert_not_called()

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_audit_log_records_user_md_size(self, mock_run, mount_config):
        """The curation audit JSONL records USER.md size for growth tracking."""
        mount_config.sleep_cycle.curate_user_memory = True
        config_dir, _ = _setup_curation_fixture(
            mount_config, existing_user_md="## Preferences\n- Likes vim\n",
        )
        mock_run.return_value = (
            True,
            '{"ops": [{"op": "append", "heading": "Preferences", "line": "- Prefers Python over JS"}]}',
        )
        curate_user_memory(mount_config, "alice")

        audit_path = config_dir / "USER.md.audit.jsonl"
        assert audit_path.exists()
        entries = [
            __import__("json").loads(line)
            for line in audit_path.read_text().splitlines()
            if line.strip()
        ]
        assert entries
        last = entries[-1]
        assert "user_md_size_bytes" in last
        assert isinstance(last["user_md_size_bytes"], int)
        assert last["user_md_size_bytes"] > 0
