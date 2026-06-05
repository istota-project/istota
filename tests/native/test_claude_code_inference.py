"""Phase 1 — Claude Code CLI as an inference-only provider."""

import json
from unittest import mock

from istota.llm.claude_code_inference import ClaudeCodeInferenceProvider
from istota.llm.provider import StreamDone, StreamError, TextDelta
from istota.llm.types import (
    TextContent,
    ToolResultMessage,
    UserMessage,
)


async def _aiter(lines):
    for line in lines:
        yield line


def _provider():
    return ClaudeCodeInferenceProvider(model="opus")


class TestComposePrompt:
    def test_includes_system_and_user_text(self):
        prompt = _provider()._compose_prompt(
            "be terse", [UserMessage(content=[TextContent(text="hello there")])], []
        )
        assert "be terse" in prompt
        assert "hello there" in prompt

    def test_includes_tool_result_text(self):
        prompt = _provider()._compose_prompt(
            "",
            [ToolResultMessage(tool_call_id="c1", tool_name="Read", content=[TextContent(text="file body")])],
            [],
        )
        assert "file body" in prompt


class TestParseClaudeLines:
    async def _collect(self, lines):
        return [e async for e in _provider()._parse_claude_lines(_aiter(lines), "opus")]

    async def test_text_events_then_result(self):
        lines = [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"id": "m1", "content": [{"type": "text", "text": "partial answer"}]},
                }
            ),
            json.dumps({"type": "result", "subtype": "success", "result": "final answer"}),
        ]
        events = await self._collect(lines)
        assert any(isinstance(e, TextDelta) and "partial" in e.text for e in events)
        done = events[-1]
        assert isinstance(done, StreamDone)
        assert done.message.text == "final answer"
        assert done.message.stop_reason == "end_turn"

    async def test_result_failure_marks_error_stop(self):
        lines = [json.dumps({"type": "result", "subtype": "error", "result": "nope"})]
        done = (await self._collect(lines))[-1]
        assert isinstance(done, StreamDone)
        assert done.message.stop_reason == "error"

    async def test_no_result_falls_back_to_streamed_text(self):
        lines = [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"id": "m1", "content": [{"type": "text", "text": "only text"}]},
                }
            ),
        ]
        done = (await self._collect(lines))[-1]
        assert isinstance(done, StreamDone)
        assert done.message.text == "only text"


class TestStreamErrorPaths:
    async def test_binary_not_found_encoded_not_raised(self):
        async def _raise(*a, **k):
            raise FileNotFoundError("no claude binary")

        with mock.patch(
            "istota.llm.claude_code_inference.asyncio.create_subprocess_exec",
            new=_raise,
        ):
            p = ClaudeCodeInferenceProvider(model="opus", claude_binary="claude")
            events = [
                e
                async for e in p.stream(
                    "", [UserMessage(content=[TextContent(text="hi")])], []
                )
            ]
        assert len(events) == 1
        assert isinstance(events[0], StreamError)
        assert events[0].message.stop_reason == "error"
        assert "no claude binary" in events[0].message.error_message
