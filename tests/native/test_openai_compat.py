"""Phase 1 — OpenAI-compatible chat completions provider.

The SSE parser is exercised directly via an injected async line iterator
(Tier 1: real parser, no network). The error paths use a fake client to prove
the provider never raises.
"""

import httpx

from istota.llm.openai_compat import OpenAICompatibleProvider
from istota.llm.provider import (
    StreamDone,
    StreamError,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
)
from istota.llm.types import (
    AssistantMessage,
    TextContent,
    ToolCallContent,
    ToolParameter,
    ToolResultMessage,
    ToolSchema,
    UserMessage,
)


async def _aiter(lines):
    for line in lines:
        yield line


def _provider():
    return OpenAICompatibleProvider(api_key="sk-test", base_url="https://x/v1")


class TestRequestBuilding:
    def test_system_prompt_becomes_system_message(self):
        body = _provider()._build_chat_completion_request(
            "you are helpful", [UserMessage(content=[TextContent(text="hi")])], [], "m", 100
        )
        assert body["messages"][0] == {"role": "system", "content": "you are helpful"}
        assert body["model"] == "m"
        assert body["max_tokens"] == 100
        assert body["stream"] is True
        # Without this, streaming responses carry no usage chunk and the native
        # brain's cost telemetry stays zero (verified against LM Studio).
        assert body["stream_options"] == {"include_usage": True}

    def test_user_message_text_converted(self):
        body = _provider()._build_chat_completion_request(
            "", [UserMessage(content=[TextContent(text="hello")])], [], "m", 100
        )
        user = body["messages"][-1]
        assert user["role"] == "user"
        assert "hello" in str(user["content"])

    def test_assistant_tool_call_converted(self):
        msg = AssistantMessage(
            content=[
                TextContent(text="calling"),
                ToolCallContent(id="c1", name="Read", arguments={"file_path": "/x"}),
            ]
        )
        body = _provider()._build_chat_completion_request("", [msg], [], "m", 100)
        asst = [m for m in body["messages"] if m["role"] == "assistant"][0]
        assert asst["tool_calls"][0]["id"] == "c1"
        assert asst["tool_calls"][0]["function"]["name"] == "Read"
        # arguments serialized as a JSON string per the OpenAI schema
        assert isinstance(asst["tool_calls"][0]["function"]["arguments"], str)

    def test_tool_result_becomes_tool_role(self):
        msg = ToolResultMessage(
            tool_call_id="c1", tool_name="Read", content=[TextContent(text="data")]
        )
        body = _provider()._build_chat_completion_request("", [msg], [], "m", 100)
        tool_msg = body["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "c1"
        assert "data" in tool_msg["content"]

    def test_tools_use_openai_function_schema(self):
        schema = ToolSchema(
            name="Read",
            description="Read a file",
            parameters=[ToolParameter(name="file_path", type="string", description="path")],
        )
        body = _provider()._build_chat_completion_request("", [], [schema], "m", 100)
        fn = body["tools"][0]
        assert fn["type"] == "function"
        assert fn["function"]["name"] == "Read"
        params = fn["function"]["parameters"]
        assert params["type"] == "object"
        assert "file_path" in params["properties"]
        assert params["required"] == ["file_path"]

    def test_no_tools_key_when_empty(self):
        body = _provider()._build_chat_completion_request("", [], [], "m", 100)
        assert "tools" not in body


class TestSSEParsing:
    async def _collect(self, lines, model="m"):
        return [e async for e in _provider()._parse_sse_lines(_aiter(lines), model)]

    async def test_text_deltas_assemble_into_done(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"Hello"}}]}',
            'data: {"choices":[{"delta":{"content":" world"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":7,"completion_tokens":3}}',
            "data: [DONE]",
        ]
        events = await self._collect(lines)
        assert any(isinstance(e, TextDelta) and e.text == "Hello" for e in events)
        done = events[-1]
        assert isinstance(done, StreamDone)
        assert done.message.text == "Hello world"
        assert done.message.stop_reason == "end_turn"
        assert done.message.usage.input_tokens == 7
        assert done.message.usage.output_tokens == 3

    async def test_tool_call_deltas_assemble(self):
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"Read","arguments":"{\\"file_path\\":"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":" \\"/x\\"}"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        events = await self._collect(lines)
        assert any(isinstance(e, ToolCallDelta) for e in events)
        done = events[-1]
        assert isinstance(done, StreamDone)
        calls = done.message.tool_calls
        assert len(calls) == 1
        assert calls[0].id == "call_1"
        assert calls[0].name == "Read"
        assert calls[0].arguments == {"file_path": "/x"}
        assert done.message.stop_reason == "tool_use"

    async def test_blank_and_non_data_lines_ignored(self):
        lines = [
            "",
            ": comment",
            'data: {"choices":[{"delta":{"content":"x"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        events = await self._collect(lines)
        assert isinstance(events[-1], StreamDone)
        assert events[-1].message.text == "x"

    async def test_reasoning_deltas_yield_thinking_and_stay_out_of_content(self):
        """reasoning_content deltas stream as ThinkingDelta AND assemble into the
        message's ThinkingContent — never into the text content / result."""
        lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"Let me "}}]}',
            'data: {"choices":[{"delta":{"reasoning_content":"think."}}]}',
            'data: {"choices":[{"delta":{"content":"Answer."}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        events = await self._collect(lines)
        thinking = [e.thinking for e in events if isinstance(e, ThinkingDelta)]
        assert thinking == ["Let me ", "think."]
        # Streamed thinking precedes the text delta (reasoning comes first).
        idx_think = next(i for i, e in enumerate(events) if isinstance(e, ThinkingDelta))
        idx_text = next(i for i, e in enumerate(events) if isinstance(e, TextDelta))
        assert idx_think < idx_text
        done = events[-1]
        assert isinstance(done, StreamDone)
        # The answer content excludes the reasoning.
        assert done.message.text == "Answer."

    async def test_alternate_reasoning_field_yields_thinking(self):
        """Some endpoints use `reasoning` (not `reasoning_content`)."""
        lines = [
            'data: {"choices":[{"delta":{"reasoning":"hmm"}}]}',
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        events = await self._collect(lines)
        assert [e.thinking for e in events if isinstance(e, ThinkingDelta)] == ["hmm"]

    async def test_cached_tokens_mapped(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"x"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":2,"prompt_tokens_details":{"cached_tokens":40}}}',
            "data: [DONE]",
        ]
        done = (await self._collect(lines))[-1]
        assert done.message.usage.cache_read_tokens == 40


class FakeStreamResponse:
    def __init__(self, status_code, body=b"", lines=None):
        self.status_code = status_code
        self._body = body
        self._lines = lines or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeClient:
    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise = raise_exc

    def stream(self, *a, **k):
        if self._raise:
            raise self._raise
        return self._response


class TestErrorEncoding:
    async def test_non_200_yields_single_stream_error(self):
        p = OpenAICompatibleProvider(api_key="k", base_url="https://x/v1")
        p._client = FakeClient(response=FakeStreamResponse(429, body=b"rate limited"))
        events = [
            e
            async for e in p.stream("", [UserMessage(content=[TextContent(text="hi")])], [])
        ]
        assert len(events) == 1
        assert isinstance(events[0], StreamError)
        assert events[0].message.stop_reason == "error"
        assert "429" in events[0].message.error_message

    async def test_connection_error_encoded_not_raised(self):
        p = OpenAICompatibleProvider(api_key="k", base_url="https://x/v1")
        p._client = FakeClient(raise_exc=httpx.ConnectError("down"))
        events = [e async for e in p.stream("", [], [])]
        assert len(events) == 1
        assert isinstance(events[0], StreamError)
        assert "down" in events[0].message.error_message

    async def test_success_path_through_client(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"hi"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        p = OpenAICompatibleProvider(api_key="k", base_url="https://x/v1")
        p._client = FakeClient(response=FakeStreamResponse(200, lines=lines))
        events = [e async for e in p.stream("", [], [])]
        assert isinstance(events[-1], StreamDone)
        assert events[-1].message.text == "hi"
