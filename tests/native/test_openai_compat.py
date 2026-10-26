"""Phase 1 — OpenAI-compatible chat completions provider.

The SSE parser is exercised directly via an injected async line iterator
(Tier 1: real parser, no network). The error paths use a fake client to prove
the provider never raises.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from istota.llm import make_provider
from istota.llm.openai_compat import (
    OpenAICompatibleProvider,
    coalesce_reasoning_details,
    is_reasoning_rejection,
    replays_reasoning,
)
from istota.llm.replay import ReplayProvider, read_fixture
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
    ThinkingContent,
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

    def test_openai_reasoning_model_uses_max_completion_tokens(self):
        # NB-12: o-series / gpt-5 on api.openai.com 400 on max_tokens (require
        # max_completion_tokens). Only the direct OpenAI endpoint is affected.
        p = OpenAICompatibleProvider(api_key="k", base_url="https://api.openai.com/v1")
        for model in ["o1", "o3-mini", "gpt-5", "o4-mini"]:
            body = p._build_chat_completion_request("", [], [], model, 100)
            assert body.get("max_completion_tokens") == 100, model
            assert "max_tokens" not in body, model

    def test_openai_non_reasoning_model_keeps_max_tokens(self):
        p = OpenAICompatibleProvider(api_key="k", base_url="https://api.openai.com/v1")
        body = p._build_chat_completion_request("", [], [], "gpt-4o", 100)
        assert body["max_tokens"] == 100
        assert "max_completion_tokens" not in body

    def test_non_openai_endpoint_keeps_max_tokens_for_reasoning(self):
        # OpenRouter / others normalize; only the direct OpenAI endpoint 400s.
        p = OpenAICompatibleProvider(api_key="k", base_url="https://openrouter.ai/api/v1")
        body = p._build_chat_completion_request("", [], [], "o3", 100)
        assert body["max_tokens"] == 100
        assert "max_completion_tokens" not in body

    def test_openrouter_requests_cost_accounting(self):
        # OpenRouter returns real charged cost only when asked via usage.include.
        p = OpenAICompatibleProvider(api_key="k", base_url="https://openrouter.ai/api/v1")
        body = p._build_chat_completion_request("", [], [], "m", 100)
        assert body["usage"] == {"include": True}

    def test_non_openrouter_endpoint_omits_cost_accounting(self):
        # Other endpoints may 400 on the unknown body field — scope it out.
        p = OpenAICompatibleProvider(api_key="k", base_url="https://api.openai.com/v1")
        body = p._build_chat_completion_request("", [], [], "gpt-4o", 100)
        assert "usage" not in body

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

    def test_array_parameter_emits_items_for_strict_providers(self):
        # Google Gemini (via OpenRouter) rejects `{"type": "array"}` with no
        # `items`: "...properties[edits].items: missing field". The serializer
        # must emit the declared `items` sub-schema (recursively).
        schema = ToolSchema(
            name="Edit",
            description="Edit a file",
            parameters=[
                ToolParameter(
                    name="file_path",
                    type="string",
                    description="path",
                ),
                ToolParameter(
                    name="edits",
                    type="array",
                    description="batch of edits",
                    required=False,
                    items=ToolParameter(
                        name="edit",
                        type="object",
                        description="one edit",
                        required=False,
                        properties={
                            "old_string": ToolParameter(
                                name="old_string",
                                type="string",
                                description="find",
                                required=False,
                            ),
                            "new_string": ToolParameter(
                                name="new_string",
                                type="string",
                                description="replace",
                                required=False,
                            ),
                        },
                    ),
                ),
            ],
        )
        body = _provider()._build_chat_completion_request("", [], [schema], "m", 100)
        props = body["tools"][0]["function"]["parameters"]["properties"]
        edits = props["edits"]
        assert edits["type"] == "array"
        # The bit Gemini was complaining about — `items` must be present and
        # itself a valid schema with a `type`.
        assert edits["items"]["type"] == "object"
        item_props = edits["items"]["properties"]
        assert item_props["old_string"]["type"] == "string"
        assert item_props["new_string"]["type"] == "string"

    def test_object_parameter_emits_nested_properties(self):
        # Nested object `properties` were silently dropped before recursion
        # was added — guard the latent sibling of the Gemini array bug.
        schema = ToolSchema(
            name="Thing",
            description="d",
            parameters=[
                ToolParameter(
                    name="opts",
                    type="object",
                    description="opts",
                    required=False,
                    properties={
                        "name": ToolParameter(
                            name="name", type="string", description="n", required=False
                        ),
                        "count": ToolParameter(
                            name="count", type="integer", description="c", required=False
                        ),
                    },
                ),
            ],
        )
        body = _provider()._build_chat_completion_request("", [], [schema], "m", 100)
        opts = body["tools"][0]["function"]["parameters"]["properties"]["opts"]
        assert opts["type"] == "object"
        assert set(opts["properties"]) == {"name", "count"}
        assert opts["properties"]["count"]["type"] == "integer"


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

    async def _cost_from(self, usage_json):
        lines = [
            'data: {"choices":[{"delta":{"content":"x"}}]}',
            f'data: {{"choices":[{{"delta":{{}},"finish_reason":"stop"}}],"usage":{usage_json}}}',
            "data: [DONE]",
        ]
        done = (await self._collect(lines))[-1]
        return done.message.usage.cost_usd

    async def test_openrouter_reported_cost_captured(self):
        cost = await self._cost_from(
            '{"prompt_tokens":100,"completion_tokens":2,"cost":0.00042}'
        )
        assert cost == 0.00042

    async def test_reported_zero_cost_kept_distinct_from_none(self):
        # A genuine free turn: 0.0 must survive as 0.0 (respected), not None.
        cost = await self._cost_from(
            '{"prompt_tokens":100,"completion_tokens":2,"cost":0.0}'
        )
        assert cost == 0.0

    async def test_absent_cost_leaves_cost_usd_none(self):
        # Every non-OpenRouter endpoint: no cost field → None, so telemetry
        # falls back to catalog pricing rather than reporting a wrong 0.0.
        cost = await self._cost_from('{"prompt_tokens":100,"completion_tokens":2}')
        assert cost is None

    async def test_non_finite_cost_rejected(self):
        # json.loads accepts bare NaN/Infinity; a nan would poison the whole
        # task's running cost total and serialize as invalid JSON. Drop it.
        for token in ("NaN", "Infinity", "-Infinity"):
            cost = await self._cost_from(
                f'{{"prompt_tokens":100,"completion_tokens":2,"cost":{token}}}'
            )
            assert cost is None, token

    async def test_negative_and_bool_and_string_cost_rejected(self):
        for literal in ("-0.5", "true", '"0.0004"'):
            cost = await self._cost_from(
                f'{{"prompt_tokens":100,"completion_tokens":2,"cost":{literal}}}'
            )
            assert cost is None, literal


class TestWireIntegrity:
    """NB-2 / NB-15: silent-truncation and finish-reason holes must not be
    laundered into a clean StreamDone(end_turn)."""

    async def _collect(self, lines, model="m"):
        return [e async for e in _provider()._parse_sse_lines(_aiter(lines), model)]

    async def test_midstream_error_frame_becomes_stream_error(self):
        # OpenRouter's documented upstream-failure pattern: HTTP 200, then a
        # data frame carrying an {"error": ...} object and no choices.
        lines = [
            'data: {"choices":[{"delta":{"content":"partial"}}]}',
            'data: {"error":{"message":"upstream model timed out","code":502}}',
        ]
        events = await self._collect(lines)
        assert isinstance(events[-1], StreamError)
        assert events[-1].message.stop_reason == "error"
        assert "upstream model timed out" in events[-1].message.error_message
        # It must NOT emit a clean StreamDone.
        assert not any(isinstance(e, StreamDone) for e in events)

    async def test_eof_without_done_or_finish_reason_is_error(self):
        # Connection drops mid-stream: content deltas, then nothing — no
        # finish_reason, no [DONE]. Truncated answer, must be an error.
        lines = [
            'data: {"choices":[{"delta":{"content":"half an ans"}}]}',
        ]
        events = await self._collect(lines)
        assert isinstance(events[-1], StreamError)
        assert events[-1].message.stop_reason == "error"
        assert "truncat" in events[-1].message.error_message.lower()
        assert not any(isinstance(e, StreamDone) for e in events)

    async def test_finish_reason_without_done_is_clean(self):
        # A finish_reason with no trailing [DONE] is a legitimate completion
        # (some endpoints omit [DONE]).
        lines = [
            'data: {"choices":[{"delta":{"content":"done"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        ]
        events = await self._collect(lines)
        assert isinstance(events[-1], StreamDone)
        assert events[-1].message.stop_reason == "end_turn"

    async def test_done_without_finish_reason_is_clean(self):
        # [DONE] with no per-choice finish_reason still counts as a clean end.
        lines = [
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            "data: [DONE]",
        ]
        events = await self._collect(lines)
        assert isinstance(events[-1], StreamDone)

    async def test_length_finish_reason_maps_to_max_tokens(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"cut off"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
            "data: [DONE]",
        ]
        done = (await self._collect(lines))[-1]
        assert isinstance(done, StreamDone)
        assert done.message.stop_reason == "max_tokens"

    async def test_content_filter_finish_reason_preserved(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"blocked"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"content_filter"}]}',
            "data: [DONE]",
        ]
        done = (await self._collect(lines))[-1]
        assert isinstance(done, StreamDone)
        assert done.message.stop_reason == "content_filter"

    async def test_error_finish_reason_not_laundered_to_end_turn(self):
        # OpenRouter reports an upstream generation failure — notably Gemini's
        # MALFORMED_FUNCTION_CALL (the model emitted a tool call whose arguments
        # are invalid JSON) — as ``finish_reason: "error"`` in a normal choices
        # chunk, with NO ``error`` object (so the mid-stream error-frame guard
        # never fires). Before the fix, ``_FINISH_REASON_MAP.get("error",
        # "end_turn")`` laundered this into a clean ``end_turn`` and the empty /
        # half-built response was delivered as a silent success (a production task
        # #270956). It must surface as ``stop_reason="error"`` with a
        # descriptive message so the task fails + retries instead.
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_1","function":{"name":"Bash","arguments":"{not valid"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"error"}]}',
            "data: [DONE]",
        ]
        events = await self._collect(lines)
        done = events[-1]
        assert isinstance(done, StreamDone)
        assert done.message.stop_reason == "error"
        # A descriptive error_message is stamped (no error object arrived in the
        # stream, so the provider layer synthesises one).
        assert done.message.error_message
        assert "finish_reason='error'" in done.message.error_message
        assert "MALFORMED" in done.message.error_message

    async def test_error_finish_reason_with_partial_text_keeps_content(self):
        # Some text streamed before the error must still be carried on the
        # assembled message (fidelity for logs / a stream surface), but the
        # stop_reason stays ``error`` so the turn is treated as a failure.
        lines = [
            'data: {"choices":[{"delta":{"content":"partial answer"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"error"}]}',
            "data: [DONE]",
        ]
        done = (await self._collect(lines))[-1]
        assert isinstance(done, StreamDone)
        assert done.message.stop_reason == "error"
        assert done.message.error_message
        assert any(
            isinstance(c, TextContent) and c.text == "partial answer"
            for c in done.message.content
        )

    async def test_parallel_tool_calls_without_index_do_not_merge(self):
        # NB-16: some endpoints omit `index`. Two calls with distinct ids must
        # not both collapse into slot 0.
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"id":"a","function":{"name":"Read","arguments":"{}"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"id":"b","function":{"name":"Grep","arguments":"{}"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        done = (await self._collect(lines))[-1]
        calls = done.message.tool_calls
        assert len(calls) == 2
        assert {c.name for c in calls} == {"Read", "Grep"}

    async def test_index_less_argument_continuation_appends(self):
        # A follow-up delta with neither index nor id continues the open call.
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"id":"a","function":{"name":"Read","arguments":"{\\"p\\":"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"\\"/x\\"}"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        done = (await self._collect(lines))[-1]
        calls = done.message.tool_calls
        assert len(calls) == 1
        assert calls[0].arguments == {"p": "/x"}

    async def test_empty_tool_call_id_synthesized(self):
        # NB-16: a server that never sends an id must not produce ToolCallContent
        # with id="" (empty ids collide in the pair sanitizer).
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"Read","arguments":"{}"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        done = (await self._collect(lines))[-1]
        calls = done.message.tool_calls
        assert len(calls) == 1
        assert calls[0].id  # non-empty synthesized id


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

    async def test_an_error_body_quoting_the_key_does_not_carry_it(self):
        # The error message is logged and shown; a provider that echoes the
        # key it was sent must not put it there.
        key = "provider-key-under-test-3c9d"
        p = OpenAICompatibleProvider(api_key=key, base_url="https://x/v1")
        p._client = FakeClient(response=FakeStreamResponse(401, body=f"Incorrect API key: {key}".encode()))
        events = [e async for e in p.stream("", [], [])]
        assert "401" in events[0].message.error_message
        assert key not in events[0].message.error_message

        frame = f'data: {{"error":{{"message":"bad key {key}","code":401}}}}'
        events = [e async for e in p._parse_sse_lines(_aiter([frame]), "m")]
        assert key not in events[-1].message.error_message

    async def test_a_key_straddling_the_cut_leaves_no_prefix(self):
        # The body is cut at 500 characters; scrubbing after the cut would
        # leave the part of the key before it.
        key = "provider-key-straddling-the-cut-" + "x" * 40
        p = OpenAICompatibleProvider(api_key=key, base_url="https://x/v1")
        body = "e" * 480 + key
        p._client = FakeClient(response=FakeStreamResponse(401, body=body.encode()))
        events = [e async for e in p.stream("", [], [])]
        assert key[:12] not in events[0].message.error_message

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


# --------------------------------------------------------------------------- #
# Reasoning continuity (native-brain pi 1.x round, stage 4)
# --------------------------------------------------------------------------- #

_FIXTURES = Path(__file__).parent / "fixtures"
_RECORDED = _FIXTURES / "sse_reasoning_details.jsonl"


def _sse(delta: dict, finish: str | None = None) -> str:
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]})


_FINISH = [_sse({}, "stop"), "data: [DONE]"]


async def _events(lines) -> list:
    return [e async for e in _provider()._parse_sse_lines(_aiter(lines), "m")]


async def _done(lines) -> StreamDone:
    events = await _events(lines)
    assert isinstance(events[-1], StreamDone), events[-1]
    return events[-1]


def _thinking(message) -> ThinkingContent:
    blocks = [c for c in message.content if isinstance(c, ThinkingContent)]
    assert len(blocks) == 1, message.content
    return blocks[0]


def _recorded_reasoning() -> tuple[str, str]:
    """The fixture's reasoning, read from each of the two fields alone."""
    plain = ""
    detailed = ""
    for line in read_fixture(_RECORDED):
        if not line.startswith("data:") or line.endswith("[DONE]"):
            continue
        for choice in json.loads(line[5:]).get("choices") or []:
            delta = choice.get("delta") or {}
            plain += delta.get("reasoning") or ""
            for item in delta.get("reasoning_details") or []:
                detailed += item.get("text") or ""
    return plain, detailed


class TestReasoningDetailsFixture:
    """The recorded OpenRouter stream says each chunk's reasoning twice."""

    async def test_it_coalesces_into_one_item_and_the_text_is_not_doubled(self):
        plain, detailed = _recorded_reasoning()
        assert plain and plain == detailed  # the premise the parser is built on

        events = [e async for e in ReplayProvider(_RECORDED).stream("", [], [])]
        done = events[-1]
        assert isinstance(done, StreamDone)
        block = _thinking(done.message)
        assert block.thinking == plain
        assert "".join(e.thinking for e in events if isinstance(e, ThinkingDelta)) == plain
        assert block.details == [
            {"type": "reasoning.text", "text": plain, "format": "unknown", "index": 0}
        ]
        assert block.source_field == "reasoning"
        assert done.message.tool_calls


class TestReasoningDetailsParse:
    async def test_a_reasoning_only_stream_parses_as_before(self):
        done = await _done(
            [
                _sse({"reasoning_content": "Let me "}),
                _sse({"reasoning_content": "think."}),
                _sse({"content": "A."}),
            ]
            + _FINISH
        )
        block = _thinking(done.message)
        assert block.thinking == "Let me think."
        assert block.details is None
        assert block.source_field == "reasoning_content"
        assert done.message.text == "A."

    async def test_details_alone_supply_the_text_and_the_deltas(self):
        events = await _events(
            [
                _sse({"reasoning_details": [{"type": "reasoning.text", "text": "one ", "index": 0}]}),
                _sse({"reasoning_details": [{"type": "reasoning.text", "text": "two", "index": 0}]}),
            ]
            + _FINISH
        )
        assert [e.thinking for e in events if isinstance(e, ThinkingDelta)] == ["one ", "two"]
        block = _thinking(events[-1].message)
        assert block.thinking == "one two"
        assert block.source_field == "reasoning_details"
        assert block.details == [{"type": "reasoning.text", "text": "one two", "index": 0}]

    async def test_coalescing_keeps_order_type_index_and_the_last_signature(self):
        def item(**kw):
            return _sse({"reasoning_details": [kw]})

        lines = [
            item(type="reasoning.text", text="a", index=0, format="anthropic-claude-v1"),
            item(type="reasoning.text", text="b", index=0, signature="sig-1"),
            item(type="reasoning.text", text="", index=0, signature="sig-2"),
            item(type="reasoning.text", text="", index=0, signature=""),
            item(type="reasoning.summary", summary="sum", index=1),
            item(type="reasoning.summary", summary="mary", index=1),
            item(type="reasoning.encrypted", data="QUJD", index=2),
            item(type="reasoning.encrypted", data="REVG", index=2),
            item(type="reasoning.text", text="c", index=3),
        ] + _FINISH
        block = _thinking((await _done(lines)).message)
        assert block.details == [
            {
                "type": "reasoning.text",
                "text": "ab",
                "index": 0,
                "format": "anthropic-claude-v1",
                "signature": "sig-2",
            },
            {"type": "reasoning.summary", "summary": "summary", "index": 1},
            {"type": "reasoning.encrypted", "data": "QUJDREVG", "index": 2},
            {"type": "reasoning.text", "text": "c", "index": 3},
        ]
        # Encrypted data is not readable reasoning.
        assert block.thinking == "absummaryc"

    async def test_an_encrypted_only_stream_still_keeps_its_details(self):
        lines = [
            _sse({"reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque", "index": 0}]}),
            _sse({"content": "ok"}),
        ] + _FINISH
        block = _thinking((await _done(lines)).message)
        assert block.thinking == ""
        assert block.details == [{"type": "reasoning.encrypted", "data": "opaque", "index": 0}]

    async def test_a_stream_with_no_reasoning_has_no_thinking_block(self):
        done = await _done([_sse({"content": "plain"})] + _FINISH)
        assert not [c for c in done.message.content if isinstance(c, ThinkingContent)]

    def test_coalesce_skips_items_that_are_not_objects(self):
        assert coalesce_reasoning_details(
            ["junk", {"type": "reasoning.text", "text": "x", "index": 0}, None]
        ) == [{"type": "reasoning.text", "text": "x", "index": 0}]


_DETAILS = [{"type": "reasoning.encrypted", "data": "QUJD", "index": 0}]
_CALL = ToolCallContent(id="c1", name="Read", arguments={"path": "a"})


def _turn(*blocks) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), stop_reason="tool_use")


def _wire(block: ThinkingContent, mode: str) -> dict:
    return OpenAICompatibleProvider._message_to_wire(_turn(block, _CALL), mode)


class TestReasoningReplayWire:
    def test_auto_sends_details_verbatim(self):
        wire = _wire(ThinkingContent(details=_DETAILS, source_field="reasoning_details"), "auto")
        assert wire["reasoning_details"] == _DETAILS
        assert wire["tool_calls"][0]["id"] == "c1"

    def test_auto_does_not_send_plain_reasoning(self):
        wire = _wire(ThinkingContent(thinking="hm", source_field="reasoning_content"), "auto")
        assert set(wire) == {"role", "content", "tool_calls"}

    def test_on_sends_plain_reasoning_under_its_own_field(self):
        wire = _wire(ThinkingContent(thinking="hm", source_field="reasoning_content"), "on")
        assert wire["reasoning_content"] == "hm"
        assert "reasoning_details" not in wire

    def test_on_sends_details_rather_than_the_text_they_repeat(self):
        wire = _wire(ThinkingContent(thinking="hm", details=_DETAILS, source_field="reasoning"), "on")
        assert wire["reasoning_details"] == _DETAILS
        assert "reasoning" not in wire

    def test_off_sends_nothing(self):
        wire = _wire(ThinkingContent(thinking="hm", details=_DETAILS, source_field="reasoning"), "off")
        assert set(wire) == {"role", "content", "tool_calls"}

    def test_the_provider_setting_and_the_per_call_override(self):
        msgs = [
            UserMessage(content=[TextContent(text="hi")]),
            _turn(ThinkingContent(details=_DETAILS, source_field="reasoning_details"), _CALL),
        ]
        p = OpenAICompatibleProvider(api_key="k", base_url="https://x/v1", reasoning_replay="auto")
        assert p.reasoning_replay == "auto"
        body = p._build_chat_completion_request("", msgs, [], "m", 10)
        assert body["messages"][-1]["reasoning_details"] == _DETAILS
        off = p._build_chat_completion_request("", msgs, [], "m", 10, reasoning_replay="off")
        assert "reasoning_details" not in off["messages"][-1]

    def test_replays_reasoning_reads_the_same_rule(self):
        msgs = [_turn(ThinkingContent(thinking="hm", source_field="reasoning"), _CALL)]
        assert replays_reasoning(msgs, "auto") is False
        assert replays_reasoning(msgs, "on") is True
        assert replays_reasoning(msgs, "off") is False

    def test_the_default_endpoint_body_is_byte_identical_to_before(self):
        """The body api.anthropic.com was sent before reasoning replay existed.

        The golden was written by the pre-stage-4 request builder. That
        endpoint streams `reasoning_content` and no `reasoning_details`, so
        under the default `auto` nothing about its wire may change.
        """
        provider = make_provider(SimpleNamespace(provider="openai_compat", api_key="k"))
        assert provider.reasoning_replay == "auto"
        messages = [
            UserMessage(content=[TextContent(text="Fix the failing test.")]),
            AssistantMessage(
                content=[
                    ThinkingContent(
                        thinking="The test reads a file first.", source_field="reasoning_content"
                    ),
                    TextContent(text="Reading it."),
                    ToolCallContent(id="call_1", name="Read", arguments={"path": "a.py"}),
                ],
                stop_reason="tool_use",
            ),
            ToolResultMessage(
                tool_call_id="call_1", tool_name="Read", content=[TextContent(text="x = 1\n")]
            ),
            AssistantMessage(
                content=[
                    ThinkingContent(thinking="Done.", source_field="reasoning_content"),
                    TextContent(text="It passes now."),
                ],
            ),
            UserMessage(content=[TextContent(text="Thanks.")]),
        ]
        tools = [
            ToolSchema(
                name="Read",
                description="Read a file.",
                parameters=[
                    ToolParameter(name="path", type="string", description="The file."),
                    ToolParameter(name="offset", type="integer", required=False),
                ],
            )
        ]
        body = provider._build_chat_completion_request(
            "You are a coding agent.",
            messages,
            tools,
            "claude-sonnet-5-5",
            16384,
            reasoning_effort="high",
        )
        golden = (_FIXTURES / "default_endpoint_request_body.json").read_text()
        assert json.dumps(body) + "\n" == golden


class TestReasoningRejection:
    def test_a_400_naming_reasoning_thinking_or_a_signature(self):
        assert is_reasoning_rejection('HTTP 400: {"error":{"message":"invalid reasoning_details"}}')
        assert is_reasoning_rejection("HTTP 400: thinking blocks cannot be modified")
        assert is_reasoning_rejection("Provider error frame: HTTP 400: Invalid signature in block")

    def test_other_errors_are_not(self):
        assert not is_reasoning_rejection("HTTP 400: messages: field required")
        assert not is_reasoning_rejection("HTTP 500: reasoning service down")
        assert not is_reasoning_rejection("HTTP 429: thinking too fast")
        assert not is_reasoning_rejection("")
        # The effort knob is a different field with a failure of its own.
        assert not is_reasoning_rejection("HTTP 400: reasoning_effort is not supported")

    def test_make_provider_falls_back_on_an_unknown_mode(self):
        def build(mode):
            return make_provider(
                SimpleNamespace(provider="openai_compat", api_key="k", reasoning_replay=mode)
            )

        assert build("sometimes").reasoning_replay == "auto"
        assert build("off").reasoning_replay == "off"
        assert build("on").reasoning_replay == "on"
