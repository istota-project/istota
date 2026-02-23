"""Tests for Talk API-based conversation context pipeline."""

import json

import pytest
from unittest.mock import patch, MagicMock

from istota.context import (
    build_talk_context,
    select_relevant_talk_context,
    format_talk_context_for_prompt,
    _parse_reference_id,
)
from istota.config import Config, NextcloudConfig, ConversationConfig, TalkConfig
from istota.db import TalkMessage


def _make_config(**conv_overrides):
    defaults = {
        "enabled": True,
        "lookback_count": 25,
        "selection_model": "haiku",
        "selection_timeout": 30.0,
        "skip_selection_threshold": 3,
        "use_selection": True,
        "always_include_recent": 5,
        "context_truncation": 0,
        "previous_tasks_count": 3,
        "talk_context_limit": 100,
    }
    defaults.update(conv_overrides)
    return Config(
        nextcloud=NextcloudConfig(url="https://nc.test", username="istota", app_password="pass"),
        talk=TalkConfig(bot_username="istota"),
        conversation=ConversationConfig(**defaults),
    )


def _raw_msg(
    msg_id, actor_id, message, timestamp=1000,
    message_type="comment", deleted=False, reference_id=None,
    message_params=None, actor_display_name=None,
):
    """Build a raw Talk API message dict."""
    m = {
        "id": msg_id,
        "actorId": actor_id,
        "actorDisplayName": actor_display_name or actor_id,
        "actorType": "users",
        "message": message,
        "messageType": message_type,
        "timestamp": timestamp,
        "messageParameters": message_params or {},
    }
    if deleted:
        m["deleted"] = True
    if reference_id:
        m["referenceId"] = reference_id
    return m


class TestParseReferenceId:
    def test_result_tag(self):
        task_id, tag = _parse_reference_id("istota:task:42:result")
        assert task_id == 42
        assert tag == "result"

    def test_ack_tag(self):
        task_id, tag = _parse_reference_id("istota:task:7:ack")
        assert task_id == 7
        assert tag == "ack"

    def test_progress_tag(self):
        task_id, tag = _parse_reference_id("istota:task:99:progress")
        assert task_id == 99
        assert tag == "progress"

    def test_none_input(self):
        assert _parse_reference_id(None) == (None, None)

    def test_empty_string(self):
        assert _parse_reference_id("") == (None, None)

    def test_non_matching(self):
        assert _parse_reference_id("some:other:format") == (None, None)

    def test_wrong_prefix(self):
        assert _parse_reference_id("other:task:42:result") == (None, None)


class TestBuildTalkContext:
    def test_basic_user_and_bot_messages(self):
        raw = [
            _raw_msg(1, "alice", "What's up?", timestamp=100),
            _raw_msg(2, "istota", "Not much!", timestamp=101, reference_id="istota:task:10:result"),
        ]
        result = build_talk_context(raw, "istota", {10: {"actions_taken": None, "source_type": "talk"}})
        assert len(result) == 2
        assert result[0].actor_id == "alice"
        assert result[0].is_bot is False
        assert result[0].message_role == "user"
        assert result[1].actor_id == "istota"
        assert result[1].is_bot is True
        assert result[1].message_role == "bot_result"

    def test_filters_system_messages(self):
        raw = [
            _raw_msg(1, "system", "User joined", message_type="system"),
            _raw_msg(2, "alice", "Hello"),
        ]
        result = build_talk_context(raw, "istota", {})
        assert len(result) == 1
        assert result[0].actor_id == "alice"

    def test_filters_deleted_messages(self):
        raw = [
            _raw_msg(1, "alice", "Oops", deleted=True),
            _raw_msg(2, "alice", "Fixed"),
        ]
        result = build_talk_context(raw, "istota", {})
        assert len(result) == 1
        assert result[0].content == "Fixed"

    def test_filters_ack_messages(self):
        raw = [
            _raw_msg(1, "alice", "Do something"),
            _raw_msg(2, "istota", "On it!", reference_id="istota:task:5:ack"),
            _raw_msg(3, "istota", "Done!", reference_id="istota:task:5:result"),
        ]
        result = build_talk_context(raw, "istota", {5: {"actions_taken": None, "source_type": "talk"}})
        assert len(result) == 2
        assert result[0].content == "Do something"
        assert result[1].content == "Done!"

    def test_filters_progress_messages(self):
        raw = [
            _raw_msg(1, "istota", "*Reading file...*", reference_id="istota:task:5:progress"),
            _raw_msg(2, "istota", "Here's what I found", reference_id="istota:task:5:result"),
        ]
        result = build_talk_context(raw, "istota", {5: {"actions_taken": None, "source_type": "talk"}})
        assert len(result) == 1
        assert result[0].content == "Here's what I found"

    def test_actions_taken_enrichment(self):
        raw = [
            _raw_msg(1, "istota", "Done", reference_id="istota:task:10:result"),
        ]
        metadata = {10: {"actions_taken": '["Read file.txt"]', "source_type": "talk"}}
        result = build_talk_context(raw, "istota", metadata)
        assert result[0].actions_taken == '["Read file.txt"]'
        assert result[0].task_id == 10

    def test_scheduled_source_type_role(self):
        raw = [
            _raw_msg(1, "istota", "Morning briefing", reference_id="istota:task:20:result"),
        ]
        metadata = {20: {"actions_taken": None, "source_type": "briefing"}}
        result = build_talk_context(raw, "istota", metadata)
        assert result[0].message_role == "scheduled"

    def test_cron_source_type_role(self):
        raw = [
            _raw_msg(1, "istota", "Scheduled check", reference_id="istota:task:30:result"),
        ]
        metadata = {30: {"actions_taken": None, "source_type": "scheduled"}}
        result = build_talk_context(raw, "istota", metadata)
        assert result[0].message_role == "scheduled"

    def test_legacy_bot_message_no_reference_id(self):
        """Pre-migration bot messages without referenceId treated as bot_result."""
        raw = [
            _raw_msg(1, "istota", "Old response"),
        ]
        result = build_talk_context(raw, "istota", {})
        assert result[0].is_bot is True
        assert result[0].message_role == "bot_result"
        assert result[0].task_id is None
        assert result[0].actions_taken is None

    def test_multi_participant_messages(self):
        raw = [
            _raw_msg(1, "alice", "Hey everyone", timestamp=100),
            _raw_msg(2, "bob", "Hi Alice!", timestamp=101),
            _raw_msg(3, "istota", "Hello!", timestamp=102, reference_id="istota:task:1:result"),
            _raw_msg(4, "carol", "Nice to meet you", timestamp=103),
        ]
        result = build_talk_context(raw, "istota", {1: {"actions_taken": None, "source_type": "talk"}})
        assert len(result) == 4
        assert [m.actor_id for m in result] == ["alice", "bob", "istota", "carol"]

    def test_preserves_order(self):
        raw = [
            _raw_msg(1, "alice", "First", timestamp=100),
            _raw_msg(2, "bob", "Second", timestamp=101),
            _raw_msg(3, "alice", "Third", timestamp=102),
        ]
        result = build_talk_context(raw, "istota", {})
        assert [m.message_id for m in result] == [1, 2, 3]

    def test_file_placeholders_resolved(self):
        raw = [
            _raw_msg(
                1, "alice", "Check {file0}",
                message_params={"file0": {"name": "report.pdf"}},
            ),
        ]
        result = build_talk_context(raw, "istota", {})
        assert result[0].content == "Check [report.pdf]"

    def test_bot_mention_stripped_from_user_messages(self):
        """Bot mentions in user messages should be stripped."""
        raw = [
            _raw_msg(
                1, "alice", "{mention-user0} what time is it?",
                message_params={"mention-user0": {"type": "user", "id": "istota", "name": "Istota"}},
            ),
        ]
        result = build_talk_context(raw, "istota", {})
        assert result[0].content == "what time is it?"

    def test_bot_mention_not_stripped_from_bot_messages(self):
        """Bot's own messages should not have bot_username passed to clean_message_content."""
        raw = [
            _raw_msg(1, "istota", "I am {mention-user0}", reference_id="istota:task:1:result",
                      message_params={"mention-user0": {"type": "user", "id": "alice", "name": "Alice"}}),
        ]
        result = build_talk_context(raw, "istota", {1: {"actions_taken": None, "source_type": "talk"}})
        # Bot messages don't pass bot_username, so mentions stay as-is
        assert "Alice" not in result[0].content or "{mention-user0}" not in result[0].content


class TestSelectRelevantTalkContext:
    def test_empty_messages(self):
        config = _make_config()
        assert select_relevant_talk_context("hello", [], config) == []

    def test_short_history_no_selection(self):
        config = _make_config(skip_selection_threshold=5)
        messages = [
            TalkMessage(i, f"user{i}", f"User {i}", False, f"msg {i}", 100 + i, None, "user", None)
            for i in range(3)
        ]
        result = select_relevant_talk_context("hello", messages, config)
        assert result == messages

    def test_within_always_include_recent(self):
        config = _make_config(skip_selection_threshold=3, always_include_recent=10)
        messages = [
            TalkMessage(i, "alice", "Alice", False, f"msg {i}", 100 + i, None, "user", None)
            for i in range(8)
        ]
        result = select_relevant_talk_context("hello", messages, config)
        assert result == messages

    def test_selection_disabled(self):
        config = _make_config(use_selection=False)
        messages = [
            TalkMessage(i, "alice", "Alice", False, f"msg {i}", 100 + i, None, "user", None)
            for i in range(20)
        ]
        result = select_relevant_talk_context("hello", messages, config)
        assert result == messages

    def test_triage_with_mock_subprocess(self):
        config = _make_config(skip_selection_threshold=3, always_include_recent=2)
        messages = [
            TalkMessage(i, "alice", "Alice", False, f"msg {i}", 100 + i, None, "user", None)
            for i in range(10)
        ]

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"relevant_ids": [0, 3, 5]})

        with patch("istota.context.subprocess.run", return_value=mock_result):
            result = select_relevant_talk_context("hello", messages, config)

        # 3 triaged + 2 guaranteed recent = 5
        assert len(result) == 5
        # First 3 are the triaged older messages
        assert result[0].message_id == 0
        assert result[1].message_id == 3
        assert result[2].message_id == 5
        # Last 2 are guaranteed recent
        assert result[3].message_id == 8
        assert result[4].message_id == 9

    def test_triage_error_returns_recent_only(self):
        config = _make_config(skip_selection_threshold=3, always_include_recent=2)
        messages = [
            TalkMessage(i, "alice", "Alice", False, f"msg {i}", 100 + i, None, "user", None)
            for i in range(10)
        ]

        with patch("istota.context.subprocess.run", side_effect=Exception("boom")):
            result = select_relevant_talk_context("hello", messages, config)

        # Only guaranteed recent
        assert len(result) == 2
        assert result[0].message_id == 8
        assert result[1].message_id == 9

    def test_lookback_count_limits_triage_input(self):
        """Messages exceeding lookback_count are trimmed before triage."""
        config = _make_config(
            skip_selection_threshold=3, always_include_recent=2, lookback_count=10,
        )
        # 50 messages, but lookback_count=10 means only last 10 go to triage
        messages = [
            TalkMessage(i, "alice", "Alice", False, f"msg {i}", 100 + i, None, "user", None)
            for i in range(50)
        ]

        mock_result = MagicMock()
        mock_result.returncode = 0
        # Select index 0 from the trimmed set (which is message_id=40)
        mock_result.stdout = json.dumps({"relevant_ids": [0]})

        with patch("istota.context.subprocess.run", return_value=mock_result) as mock_run:
            result = select_relevant_talk_context("hello", messages, config)

        # Should have been called with 8 older messages (10 lookback - 2 recent)
        call_input = mock_run.call_args.kwargs.get("input") or mock_run.call_args[1].get("input")
        # The trimmed older messages start from message_id=40
        assert "msg 40" in call_input
        assert "msg 39" not in call_input
        # Result: 1 triaged + 2 recent = 3
        assert len(result) == 3
        assert result[0].message_id == 40  # triaged from trimmed set
        assert result[1].message_id == 48  # recent
        assert result[2].message_id == 49  # recent


class TestFormatTalkContextForPrompt:
    def test_empty(self):
        assert format_talk_context_for_prompt([]) == ""

    def test_user_message(self):
        msg = TalkMessage(1, "alice", "Alice", False, "Hello!", 1000000, None, "user", None)
        result = format_talk_context_for_prompt([msg])
        assert "[" in result  # timestamp
        assert "alice: Hello!" in result

    def test_bot_message(self):
        msg = TalkMessage(1, "istota", "Istota", True, "Hi there!", 1000000, None, "bot_result", 10)
        result = format_talk_context_for_prompt([msg])
        assert "Bot: Hi there!" in result

    def test_scheduled_message(self):
        msg = TalkMessage(1, "istota", "Istota", True, "Daily report", 1000000, None, "scheduled", 20)
        result = format_talk_context_for_prompt([msg])
        # Scheduled messages from bot should still show as Bot
        assert "Bot: Daily report" in result

    def test_truncation(self):
        long_content = "x" * 5000
        msg = TalkMessage(1, "istota", "Istota", True, long_content, 1000000, None, "bot_result", None)
        result = format_talk_context_for_prompt([msg], truncation=100)
        assert "...[truncated]" in result
        # Total line length should be reasonable
        lines = result.split("\n")
        bot_line = [l for l in lines if "Bot:" in l][0]
        # Content portion should be truncated
        assert len(long_content) > 100

    def test_no_truncation_when_zero(self):
        long_content = "x" * 5000
        msg = TalkMessage(1, "istota", "Istota", True, long_content, 1000000, None, "bot_result", None)
        result = format_talk_context_for_prompt([msg], truncation=0)
        assert "...[truncated]" not in result
        assert long_content in result

    def test_actions_appended(self):
        msg = TalkMessage(
            1, "istota", "Istota", True, "Done",
            1000000, '["Read file.txt", "Write output.txt"]', "bot_result", 10,
        )
        result = format_talk_context_for_prompt([msg])
        assert "[Actions: Read file.txt | Write output.txt]" in result

    def test_actions_none(self):
        msg = TalkMessage(1, "istota", "Istota", True, "Done", 1000000, None, "bot_result", None)
        result = format_talk_context_for_prompt([msg])
        assert "[Actions:" not in result

    def test_multi_participant(self):
        messages = [
            TalkMessage(1, "alice", "Alice", False, "Hey everyone", 100, None, "user", None),
            TalkMessage(2, "bob", "Bob", False, "Hi Alice!", 101, None, "user", None),
            TalkMessage(3, "istota", "Istota", True, "Hello!", 102, None, "bot_result", 1),
            TalkMessage(4, "carol", "Carol", False, "Nice", 103, None, "user", None),
        ]
        result = format_talk_context_for_prompt(messages)
        lines = result.split("\n")
        assert any("alice: Hey everyone" in l for l in lines)
        assert any("bob: Hi Alice!" in l for l in lines)
        assert any("Bot: Hello!" in l for l in lines)
        assert any("carol: Nice" in l for l in lines)

    def test_user_with_no_actor_id(self):
        msg = TalkMessage(1, "", "Unknown", False, "Hello", 1000000, None, "user", None)
        result = format_talk_context_for_prompt([msg])
        assert "User: Hello" in result

    def test_actions_capped_at_15(self):
        actions = [f"Action {i}" for i in range(20)]
        msg = TalkMessage(
            1, "istota", "Istota", True, "Done",
            1000000, json.dumps(actions), "bot_result", 10,
        )
        result = format_talk_context_for_prompt([msg])
        assert "+5 more" in result
