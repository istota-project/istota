"""Tests for stream_parser module."""

import json

from istota.stream_parser import (
    ContextManagementEvent,
    ResultEvent,
    TextEvent,
    ToolUseEvent,
    _describe_tool_use,
    make_stream_parser,
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
                "stop_reason": "tool_use",
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
                "stop_reason": "end_turn",
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
                "stop_reason": "tool_use",
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
            "message": {"stop_reason": "end_turn", "content": []},
        })
        assert parse_stream_line(line) is None

    def test_assistant_whitespace_only_text(self):
        line = self._make_line({
            "type": "assistant",
            "message": {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "   \n  "}]
            },
        })
        assert parse_stream_line(line) is None

    def test_assistant_multiple_text_blocks(self):
        line = self._make_line({
            "type": "assistant",
            "message": {
                "stop_reason": "end_turn",
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
                "stop_reason": "tool_use",
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

    def test_partial_event_thinking_only_skipped(self):
        """Partial events with only thinking content should be skipped."""
        line = self._make_line({
            "type": "assistant",
            "message": {
                "stop_reason": None,
                "content": [
                    {"type": "thinking", "thinking": "Let me think about this..."},
                ],
            },
        })
        assert parse_stream_line(line) is None

    def test_partial_event_text_captured(self):
        """Text from interrupted turns (stop_reason=null) must be captured (ISSUE-025)."""
        line = self._make_line({
            "type": "assistant",
            "message": {
                "stop_reason": None,
                "content": [
                    {"type": "text", "text": "Here are the top 2 apartments I found."},
                ],
            },
        })
        event = parse_stream_line(line)
        assert isinstance(event, TextEvent)
        assert event.text == "Here are the top 2 apartments I found."

    def test_partial_event_tool_use_captured(self):
        """Tool use from partial events must be captured — dedup handles duplicates."""
        line = self._make_line({
            "type": "assistant",
            "message": {
                "stop_reason": None,
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/tmp/data.txt"}},
                ],
            },
        })
        event = parse_stream_line(line)
        assert isinstance(event, ToolUseEvent)
        assert event.description == "📄 Reading data.txt"

    def test_partial_event_mixed_content_prefers_tool_use(self):
        """Partial with both text and tool_use: tool_use takes priority (same as completed)."""
        line = self._make_line({
            "type": "assistant",
            "message": {
                "stop_reason": None,
                "content": [
                    {"type": "text", "text": "Let me check the file."},
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/tmp/data.txt"}},
                ],
            },
        })
        event = parse_stream_line(line)
        assert isinstance(event, ToolUseEvent)
        assert event.description == "📄 Reading data.txt"

    def test_assistant_context_management_emits_marker(self):
        """Context management events emit a ContextManagementEvent marker."""
        line = self._make_line({
            "type": "assistant",
            "message": {
                "stop_reason": "end_turn",
                "context_management": {"applied_edits": ["truncate_conversation"]},
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/tmp/test.txt"}},
                ],
            },
        })
        event = parse_stream_line(line)
        assert isinstance(event, ContextManagementEvent)

    def test_assistant_completed_event_still_parsed(self):
        """Completed events with stop_reason should still be parsed."""
        line = self._make_line({
            "type": "assistant",
            "message": {
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": "Here is the answer."},
                ],
            },
        })
        event = parse_stream_line(line)
        assert isinstance(event, TextEvent)
        assert event.text == "Here is the answer."

    def test_assistant_tool_use_with_stop_reason(self):
        """Tool use events with stop_reason=tool_use should still be parsed."""
        line = self._make_line({
            "type": "assistant",
            "message": {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash",
                     "input": {"command": "ls", "description": "List files"}},
                ],
            },
        })
        event = parse_stream_line(line)
        assert isinstance(event, ToolUseEvent)
        assert event.description == "⚙️ List files"


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
                    "stop_reason": "tool_use",
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
                    "stop_reason": "tool_use",
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
                    "stop_reason": "end_turn",
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


class TestMessageDedup:
    """Test stateful deduplication via make_stream_parser (ISSUE-024)."""

    def _make_line(self, data: dict) -> str:
        return json.dumps(data)

    def test_duplicate_tool_use_block_skipped(self):
        """Same tool_use block ID emitted twice (partial then completed) is deduplicated."""
        parse = make_stream_parser()

        # Partial (stop_reason=null) — first emission
        partial = self._make_line({
            "type": "assistant",
            "message": {
                "id": "msg_001",
                "stop_reason": None,
                "content": [
                    {"type": "tool_use", "id": "toolu_001", "name": "Read",
                     "input": {"file_path": "/tmp/data.txt"}},
                ],
            },
        })
        # Completed (stop_reason=tool_use) — same tool_use block ID
        completed = self._make_line({
            "type": "assistant",
            "message": {
                "id": "msg_001",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "tool_use", "id": "toolu_001", "name": "Read",
                     "input": {"file_path": "/tmp/data.txt"}},
                ],
            },
        })

        event1 = parse(partial)
        event2 = parse(completed)
        assert isinstance(event1, ToolUseEvent)
        assert event2 is None  # deduplicated by block ID

    def test_same_message_id_different_tool_blocks_both_parsed(self):
        """Claude Code reuses message ID across tool calls in a turn — both must be captured."""
        parse = make_stream_parser()

        line1 = self._make_line({
            "type": "assistant",
            "message": {
                "id": "msg_001",
                "stop_reason": None,
                "content": [
                    {"type": "tool_use", "id": "toolu_001", "name": "Read",
                     "input": {"file_path": "/tmp/a.txt"}},
                ],
            },
        })
        line2 = self._make_line({
            "type": "assistant",
            "message": {
                "id": "msg_001",
                "stop_reason": None,
                "content": [
                    {"type": "tool_use", "id": "toolu_002", "name": "Bash",
                     "input": {"command": "ls", "description": "List files"}},
                ],
            },
        })

        event1 = parse(line1)
        event2 = parse(line2)
        assert isinstance(event1, ToolUseEvent)
        assert isinstance(event2, ToolUseEvent)
        assert "a.txt" in event1.description
        assert "List files" in event2.description

    def test_duplicate_text_block_skipped(self):
        """Same text emitted twice under the same message ID is deduplicated."""
        parse = make_stream_parser()

        line1 = self._make_line({
            "type": "assistant",
            "message": {
                "id": "msg_002",
                "stop_reason": None,
                "content": [
                    {"type": "text", "text": "Here is the answer."},
                ],
            },
        })
        line2 = self._make_line({
            "type": "assistant",
            "message": {
                "id": "msg_002",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": "Here is the answer."},
                ],
            },
        })

        event1 = parse(line1)
        event2 = parse(line2)
        assert isinstance(event1, TextEvent)
        assert event2 is None  # deduplicated

    def test_context_management_replay_emits_marker(self):
        """Context management replay emits ContextManagementEvent, not content."""
        parse = make_stream_parser()

        original = self._make_line({
            "type": "assistant",
            "message": {
                "id": "msg_001",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "tool_use", "id": "toolu_001", "name": "Bash",
                     "input": {"command": "ls", "description": "List files"}},
                ],
            },
        })
        replay = self._make_line({
            "type": "assistant",
            "message": {
                "id": "msg_001_replay",
                "stop_reason": "tool_use",
                "context_management": {"applied_edits": ["truncate"]},
                "content": [
                    {"type": "tool_use", "id": "toolu_001", "name": "Bash",
                     "input": {"command": "ls", "description": "List files"}},
                ],
            },
        })

        event1 = parse(original)
        event2 = parse(replay)
        assert isinstance(event1, ToolUseEvent)
        assert isinstance(event2, ContextManagementEvent)

    def test_result_events_not_affected_by_dedup(self):
        """ResultEvents don't have message IDs, should always pass through."""
        parse = make_stream_parser()

        line = self._make_line({
            "type": "result",
            "subtype": "success",
            "result": "Done.",
        })
        assert isinstance(parse(line), ResultEvent)


class TestRealCLIOutput:
    """Integration test using real Claude CLI stream-json output.

    Captured from: claude -p --output-format stream-json --allowedTools Read Bash
    Key observations from real output:
    - All tool_use events come with stop_reason: null (never "tool_use")
    - Same message ID is reused across different tool calls in a turn
    - Final text response also has stop_reason: null
    - Only the ResultEvent carries the final answer
    """

    # Real stream-json lines from a multi-tool task (message IDs and content
    # simplified for clarity, structure preserved exactly as emitted).
    STREAM_LINES = [
        # 1. System init
        json.dumps({
            "type": "system", "subtype": "init",
            "cwd": "/tmp", "model": "claude-opus-4-6[1m]",
        }),
        # 2. First tool call (Read) — stop_reason: null, message reused
        json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg_01Abc", "stop_reason": None,
                "content": [{
                    "type": "tool_use", "id": "toolu_01Read",
                    "name": "Read",
                    "input": {"file_path": "/tmp/pyproject.toml", "limit": 20},
                }],
            },
        }),
        # 3. Tool result (user event — skipped)
        json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [{
                "tool_use_id": "toolu_01Read", "type": "tool_result",
                "content": "name = \"istota\"\nversion = \"0.5.0\"",
            }]},
        }),
        # 4. Second tool call (Bash) — same message ID, different tool block ID
        json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg_01Abc", "stop_reason": None,
                "content": [{
                    "type": "tool_use", "id": "toolu_02Bash",
                    "name": "Bash",
                    "input": {
                        "command": "test -f tests/test_stream_parser.py && echo EXISTS",
                        "description": "Check if test file exists",
                    },
                }],
            },
        }),
        # 5. Tool result
        json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [{
                "tool_use_id": "toolu_02Bash", "type": "tool_result",
                "content": "EXISTS",
            }]},
        }),
        # 6. Final text — new message ID, still stop_reason: null
        json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg_02Xyz", "stop_reason": None,
                "content": [{
                    "type": "text",
                    "text": "- **Project name:** istota\n- **Version:** 0.5.0\n- **test file:** exists",
                }],
            },
        }),
        # 7. Result event
        json.dumps({
            "type": "result", "subtype": "success",
            "result": "- **Project name:** istota\n- **Version:** 0.5.0\n- **test file:** exists",
        }),
    ]

    def test_all_tool_calls_captured(self):
        """Both tool calls are captured despite same message ID and stop_reason=null."""
        parse = make_stream_parser()
        events = [parse(line) for line in self.STREAM_LINES]
        events = [e for e in events if e is not None]

        assert len(events) == 4  # Read + Bash + Text + Result
        assert isinstance(events[0], ToolUseEvent)
        assert "Reading pyproject.toml" in events[0].description
        assert isinstance(events[1], ToolUseEvent)
        assert "Check if test file exists" in events[1].description
        assert isinstance(events[2], TextEvent)
        assert "istota" in events[2].text
        assert isinstance(events[3], ResultEvent)
        assert events[3].success is True

    def test_actions_taken_populated(self):
        """Simulates the executor's actions_taken collection from real output."""
        parse = make_stream_parser()
        actions_descriptions = []
        execution_trace = []

        for line in self.STREAM_LINES:
            event = parse(line)
            if event is None:
                continue
            if isinstance(event, ToolUseEvent):
                actions_descriptions.append(event.description)
                execution_trace.append({"type": "tool", "text": event.description})
            elif isinstance(event, TextEvent):
                execution_trace.append({"type": "text", "text": event.text})

        assert len(actions_descriptions) == 2
        assert any("Reading" in d for d in actions_descriptions)
        assert any("Check if test file exists" in d for d in actions_descriptions)
        assert len(execution_trace) == 3  # 2 tools + 1 text

    def test_context_management_replay_in_real_stream(self):
        """Context management replays are filtered even with real stream patterns."""
        parse = make_stream_parser()

        # Process normal stream first
        for line in self.STREAM_LINES:
            parse(line)

        # Then a context management replay arrives (as observed in real logs)
        replay = json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg_03Replay", "stop_reason": None,
                "context_management": {"applied_edits": ["truncate_conversation"]},
                "content": [{
                    "type": "tool_use", "id": "toolu_02Bash",
                    "name": "Bash",
                    "input": {"command": "ls", "description": "List files"},
                }],
            },
        })
        assert isinstance(parse(replay), ContextManagementEvent)
