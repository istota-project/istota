"""Tests for sleep-cycle playbook generation (Part B, Stage 5)."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, PlaybooksConfig, SleepCycleConfig, MemorySearchConfig
from istota.memory.sleep_cycle import (
    _parse_structured_extraction,
    _playbook_slug,
    build_memory_extraction_prompt,
    cleanup_old_playbooks,
    gather_day_data,
    process_user_sleep_cycle,
    tool_summary,
)


@pytest.fixture
def pb_config(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    db.init_db(tmp_path / "test.db")
    return Config(
        db_path=tmp_path / "test.db",
        temp_dir=tmp_path / "temp",
        nextcloud_mount_path=mount,
        sleep_cycle=SleepCycleConfig(enabled=True, lookback_hours=24),
        playbooks=PlaybooksConfig(enabled=True, min_tool_calls=4),
        memory_search=MemorySearchConfig(enabled=True, auto_index_memory_files=True),
    )


def _trace(*tool_descs):
    entries = []
    for d in tool_descs:
        entries.append({"type": "tool", "text": d})
        entries.append({"type": "text", "text": "step done"})
    return json.dumps(entries)


# --- tool_summary -----------------------------------------------------------


class TestToolSummary:
    def test_counts_and_strips_emoji(self):
        tj = _trace("⚙️ gh pr create", "📄 Reading config.toml")
        count, labels = tool_summary(tj)
        assert count == 2
        assert labels[0] == "gh pr create"
        assert labels[1] == "Reading config.toml"

    def test_empty_or_malformed(self):
        assert tool_summary(None) == (0, [])
        assert tool_summary("not json") == (0, [])
        assert tool_summary("{}") == (0, [])

    def test_truncates_long_label(self):
        long = "⚙️ " + "x" * 200
        count, labels = tool_summary(json.dumps([{"type": "tool", "text": long}]))
        assert count == 1
        assert len(labels[0]) <= 80


# --- _parse_structured_extraction (PLAYBOOKS section) -----------------------


class TestParsePlaybooks:
    def test_parses_playbooks_section(self):
        output = (
            "MEMORIES:\n- did a thing (2026-06-10)\n\n"
            "FACTS:\n[]\n\n"
            "TOPICS:\n{}\n\n"
            "PLAYBOOKS:\n"
            '[{"title": "Open a GitHub PR", "triggers": ["pr", "pull request"], '
            '"steps": "1. clone\\n2. gh pr create"}]'
        )
        memories, facts, topics, playbooks = _parse_structured_extraction(output)
        assert "did a thing" in memories
        assert len(playbooks) == 1
        assert playbooks[0]["title"] == "Open a GitHub PR"

    def test_missing_playbooks_section_yields_empty(self):
        output = "MEMORIES:\n- thing\n\nFACTS:\n[]\n\nTOPICS:\n{}"
        _m, _f, _t, playbooks = _parse_structured_extraction(output)
        assert playbooks == []

    def test_malformed_playbooks_yields_empty(self):
        output = "MEMORIES:\n- thing\n\nPLAYBOOKS:\nnot valid json"
        _m, _f, _t, playbooks = _parse_structured_extraction(output)
        assert playbooks == []

    def test_invalid_playbook_entries_dropped(self):
        output = (
            "MEMORIES:\n- thing\n\nPLAYBOOKS:\n"
            '[{"title": "ok", "steps": "do it"}, {"title": "", "steps": "x"}, {"title": "no steps"}]'
        )
        _m, _f, _t, playbooks = _parse_structured_extraction(output)
        assert len(playbooks) == 1
        assert playbooks[0]["title"] == "ok"


# --- prompt content ---------------------------------------------------------


class TestExtractionPrompt:
    def test_playbooks_section_present_when_enabled(self):
        prompt = build_memory_extraction_prompt(
            "alice", "data", None, "2026-06-10",
            playbooks_enabled=True, min_tool_calls=4,
        )
        assert "PLAYBOOKS:" in prompt
        assert "at least 4 tool calls" in prompt
        assert "never ships code to run" in prompt

    def test_playbooks_section_absent_when_disabled(self):
        prompt = build_memory_extraction_prompt("alice", "data", None, "2026-06-10")
        assert "PLAYBOOKS:" not in prompt

    def test_gather_day_data_includes_tool_summary(self, pb_config):
        with db.get_db(pb_config.db_path) as conn:
            t = db.create_task(conn, prompt="deploy", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(
                conn, t, "completed", result="done",
                execution_trace=_trace("⚙️ a", "⚙️ b", "📄 c", "📝 d", "🔍 e"),
            )
            data = gather_day_data(pb_config, conn, "alice", 24, None)
        assert "Tools (5):" in data


# --- slug -------------------------------------------------------------------


class TestPlaybookSlug:
    def test_slugify(self):
        assert _playbook_slug("Open a GitHub PR!") == "open-a-github-pr"
        assert _playbook_slug("   ") == "playbook"


# --- process_user_sleep_cycle ----------------------------------------------


_PB_OUTPUT = (
    "MEMORIES:\n- Opened a PR for the auth fix (2026-06-10, ref:1)\n\n"
    "FACTS:\n[]\n\nTOPICS:\n{}\n\n"
    "PLAYBOOKS:\n"
    '[{"title": "Open a GitHub PR via developer skill", '
    '"triggers": ["pr", "pull request", "merge"], '
    '"steps": "1. Use the developer skill\\n2. gh pr create\\n3. paste the URL back"}]'
)


class TestProcessWritesPlaybooks:
    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_writes_and_indexes_playbook(self, mock_run, pb_config):
        mock_run.return_value = (True, _PB_OUTPUT)
        with db.get_db(pb_config.db_path) as conn:
            t = db.create_task(conn, prompt="open a PR", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(
                conn, t, "completed", result="PR opened",
                execution_trace=_trace("⚙️ a", "⚙️ b", "📄 c", "📝 d", "🔍 e"),
            )
            assert process_user_sleep_cycle(pb_config, conn, "alice") is True

            pb_dir = pb_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "playbooks"
            files = list(pb_dir.glob("*.md"))
            assert len(files) == 1
            text = files[0].read_text()
            assert "Open a GitHub PR via developer skill" in text
            assert "source: sleep_cycle" in text
            assert "gh pr create" in text

            # Indexed as source_type=playbook.
            rows = conn.execute(
                "SELECT COUNT(*) FROM memory_chunks WHERE user_id=? AND source_type='playbook'",
                ("alice",),
            ).fetchone()
            assert rows[0] >= 1

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_disabled_writes_nothing(self, mock_run, pb_config):
        pb_config.playbooks.enabled = False
        mock_run.return_value = (True, _PB_OUTPUT)
        with db.get_db(pb_config.db_path) as conn:
            t = db.create_task(conn, prompt="open a PR", user_id="alice")
            db.update_task_status(conn, t, "running")
            db.update_task_status(conn, t, "completed", result="PR opened", execution_trace=_trace("⚙️ a"))
            process_user_sleep_cycle(pb_config, conn, "alice")
        pb_dir = pb_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "playbooks"
        assert not pb_dir.exists()

    @patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
    def test_duplicate_updates_in_place(self, mock_run, pb_config):
        mock_run.return_value = (True, _PB_OUTPUT)
        for _ in range(2):
            with db.get_db(pb_config.db_path) as conn:
                t = db.create_task(conn, prompt="open a PR", user_id="alice")
                db.update_task_status(conn, t, "running")
                db.update_task_status(
                    conn, t, "completed", result="PR opened",
                    execution_trace=_trace("⚙️ a", "⚙️ b", "📄 c", "📝 d"),
                )
                process_user_sleep_cycle(pb_config, conn, "alice")
        pb_dir = pb_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "playbooks"
        assert len(list(pb_dir.glob("*.md"))) == 1


# --- cleanup ----------------------------------------------------------------


class TestCleanupPlaybooks:
    def test_retention_zero_keeps_all(self, pb_config):
        pb_dir = pb_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "playbooks"
        pb_dir.mkdir(parents=True)
        (pb_dir / "old.md").write_text("x")
        assert cleanup_old_playbooks(pb_config, "alice", 0) == 0
        assert (pb_dir / "old.md").exists()

    def test_prunes_old_files(self, pb_config):
        pb_dir = pb_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "playbooks"
        pb_dir.mkdir(parents=True)
        old = pb_dir / "old.md"
        old.write_text("x")
        # Backdate mtime well past the retention window.
        past = time.time() - 40 * 86400
        import os
        os.utime(old, (past, past))
        fresh = pb_dir / "fresh.md"
        fresh.write_text("y")

        assert cleanup_old_playbooks(pb_config, "alice", 30) == 1
        assert not old.exists()
        assert fresh.exists()
