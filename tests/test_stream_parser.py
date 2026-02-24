"""Tests for stream_parser module."""

import json

from istota.stream_parser import (
    ResultEvent,
    TextEvent,
    ToolUseEvent,
    _describe_tool_use,
    parse_stream_line,
)


# --- _describe_tool_use tests ---


class TestDescribeToolUse:
    def test_bash_with_description(self):
        assert _describe_tool_use("Bash", {"command": "ls -la", "description": "List files"}) == "⚙️ List files"

    def test_bash_without_description_short_command(self):
        assert _describe_tool_use("Bash", {"command": "echo hello"}) == "⚙️ echo hello"

    def test_bash_without_description_long_command(self):
        long_cmd = "x" * 100
        result = _describe_tool_use("Bash", {"command": long_cmd})
        assert result.startswith("⚙️ ")
        assert result.endswith("...")

    def test_bash_empty_input(self):
        assert _describe_tool_use("Bash", {}) == "⚙️ Running command"

    def test_read(self):
        assert _describe_tool_use("Read", {"file_path": "/srv/mount/nextcloud/content/alice/TODO.txt"}) == "📄 Reading TODO.txt"

    def test_read_empty(self):
        assert _describe_tool_use("Read", {}) == "📄 Reading file"

    def test_edit(self):
        assert _describe_tool_use("Edit", {"file_path": "/tmp/script.py"}) == "✏️ Editing script.py"

    def test_multi_edit(self):
        assert _describe_tool_use("MultiEdit", {"file_path": "/tmp/config.toml"}) == "✏️ Editing config.toml"

    def test_write(self):
        assert _describe_tool_use("Write", {"file_path": "/tmp/output.json"}) == "📝 Writing output.json"

    def test_grep(self):
        assert _describe_tool_use("Grep", {"pattern": "TODO"}) == "🔍 Searching for 'TODO'"

    def test_glob(self):
        assert _describe_tool_use("Glob", {"pattern": "**/*.py"}) == "🔍 Searching for '**/*.py'"

    def test_task_with_description(self):
        assert _describe_tool_use("Task", {"description": "find errors"}) == "🐙 Delegating: find errors"

    def test_task_without_description(self):
        assert _describe_tool_use("Task", {}) == "🐙 Using Task"

    def test_unknown_tool(self):
        assert _describe_tool_use("WebSearch", {}) == "🌐 Using WebSearch"


# --- parse_stream_line tests ---


class TestParseStreamLine:
    def _make_line(self, data: dict) -> str:
        return json.dumps(data)

    def test_empty_line(self):
        assert parse_stream_line("") is None
        assert parse_stream_line("   ") is None

    def test_invalid_json(self):
        assert parse_stream_line("not json at all") is None

    def test_system_init_event(self):
        line = self._make_line({"type": "system", "subtype": "init", "cwd": "/tmp"})
        assert parse_stream_line(line) is None

    def test_user_event(self):
        line = self._make_line({"type": "user", "message": {"role": "user"}})
        assert parse_stream_line(line) is None

    def test_result_success(self):
        line = self._make_line({
            "type": "result",
            "subtype": "success",
            "result": "Here are your events for today.",
        })
        event = parse_stream_line(line)
        assert isinstance(event, ResultEvent)
        assert event.success is True
        assert event.text == "Here are your events for today."

    def test_result_error(self):
        line = self._make_line({
            "type": "result",
            "subtype": "error",
            "result": "Task execution failed",
        })
        event = parse_stream_line(line)
        assert isinstance(event, ResultEvent)
        assert event.success is False
        assert event.text == "Task execution failed"

    def test_result_missing_result_field(self):
        line = self._make_line({"type": "result", "subtype": "success"})
        event = parse_stream_line(line)
        assert isinstance(event, ResultEvent)
        assert event.text == ""

    def test_assistant_tool_use(self):
        line = self._make_line({
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_123",
                        "name": "Read",
                        "input": {"file_path": "/tmp/data.csv"},
                    }
                ]
            },
        })
        event = parse_stream_line(line)
        assert isinstance(event, ToolUseEvent)
        assert event.tool_name == "Read"
        assert event.description == "📄 Reading data.csv"

    def test_assistant_text(self):
        line = self._make_line({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Let me check your calendar."},
                ]
            },
        })
        event = parse_stream_line(line)
        assert isinstance(event, TextEvent)
        assert event.text == "Let me check your calendar."

    def test_assistant_tool_use_preferred_over_text(self):
        """When both tool_use and text blocks exist, tool_use takes priority."""
        line = self._make_line({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I'll read the file now."},
                    {
                        "type": "tool_use",
                        "id": "toolu_456",
                        "name": "Bash",
                        "input": {"command": "cat /tmp/test.txt", "description": "Read test file"},
                    },
                ]
            },
        })
        event = parse_stream_line(line)
        assert isinstance(event, ToolUseEvent)
        assert event.description == "⚙️ Read test file"

    def test_assistant_empty_content(self):
        line = self._make_line({
            "type": "assistant",
            "message": {"content": []},
        })
        assert parse_stream_line(line) is None

    def test_assistant_whitespace_only_text(self):
        line = self._make_line({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "   \n  "}]
            },
        })
        assert parse_stream_line(line) is None

    def test_assistant_multiple_text_blocks(self):
        line = self._make_line({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "First part."},
                    {"type": "text", "text": "Second part."},
                ]
            },
        })
        event = parse_stream_line(line)
        assert isinstance(event, TextEvent)
        assert event.text == "First part.\nSecond part."

    def test_assistant_first_tool_use_returned(self):
        """When multiple tool_use blocks, only first is returned."""
        line = self._make_line({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "/tmp/a.txt"}},
                    {"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "/tmp/b.txt"}},
                ]
            },
        })
        event = parse_stream_line(line)
        assert isinstance(event, ToolUseEvent)
        assert event.description == "📄 Reading a.txt"

    def test_unknown_type_ignored(self):
        line = self._make_line({"type": "unknown_future_type", "data": "stuff"})
        assert parse_stream_line(line) is None

    def test_missing_message_key(self):
        line = self._make_line({"type": "assistant"})
        assert parse_stream_line(line) is None


# --- Integration-style: simulate a full stream ---


class TestFullStream:
    def test_multi_turn_stream(self):
        """Simulate parsing a full multi-turn stream-json output."""
        lines = [
            # System init
            json.dumps({"type": "system", "subtype": "init", "cwd": "/tmp"}),
            # Assistant uses a tool
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "Bash",
                         "input": {"command": "ls /tmp", "description": "List temp files"}},
                    ]
                },
            }),
            # User (tool result) - should be skipped
            json.dumps({"type": "user", "message": {"role": "user"}, "tool_use_result": True}),
            # Assistant responds with text + another tool
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Found some files. Let me read one."},
                        {"type": "tool_use", "id": "t2", "name": "Read",
                         "input": {"file_path": "/tmp/notes.txt"}},
                    ]
                },
            }),
            # Another user tool result
            json.dumps({"type": "user", "message": {"role": "user"}}),
            # Final assistant text
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Here is the summary."}]
                },
            }),
            # Result
            json.dumps({
                "type": "result",
                "subtype": "success",
                "result": "Here is the summary of your files.",
            }),
        ]

        events = [parse_stream_line(line) for line in lines]
        events = [e for e in events if e is not None]

        assert len(events) == 4
        assert isinstance(events[0], ToolUseEvent)
        assert events[0].description == "⚙️ List temp files"
        assert isinstance(events[1], ToolUseEvent)  # tool preferred over text
        assert events[1].description == "📄 Reading notes.txt"
        assert isinstance(events[2], TextEvent)
        assert events[2].text == "Here is the summary."
        assert isinstance(events[3], ResultEvent)
        assert events[3].success is True
        assert events[3].text == "Here is the summary of your files."
