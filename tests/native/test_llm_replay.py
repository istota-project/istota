"""Tier-1 SSE replay/record providers — offline, real-parser, no credits.

ReplayProvider feeds recorded SSE lines through the *real*
OpenAICompatibleProvider parser. RecordingProvider tees a live provider's raw
SSE lines to a fixture while passing events through unchanged. Round-tripping
record→replay must reproduce the same StreamEvents.
"""

import json

import pytest

from istota.llm.openai_compat import OpenAICompatibleProvider
from istota.llm.provider import StreamDone, StreamError
from istota.llm.replay import ReplayProvider, RecordingProvider, write_fixture

pytestmark = pytest.mark.asyncio

_TEXT_SSE = [
    'data: {"choices":[{"delta":{"content":"Hello"}}]}',
    'data: {"choices":[{"delta":{"content":" world"}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":7,"completion_tokens":3}}',
    "data: [DONE]",
]

_TOOL_SSE = [
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"Read","arguments":"{\\"file_path\\":"}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":" \\"/x\\"}"}}]}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    "data: [DONE]",
]


async def _collect(provider, model="m"):
    return [e async for e in provider.stream("sys", [], [], model=model)]


class TestReplayProvider:
    async def test_text_fixture_replays_through_real_parser(self, tmp_path):
        fx = tmp_path / "text.jsonl"
        write_fixture(fx, _TEXT_SSE)
        events = await _collect(ReplayProvider(fx))
        done = events[-1]
        assert isinstance(done, StreamDone)
        assert done.message.text == "Hello world"
        assert done.message.usage.input_tokens == 7
        assert done.message.usage.output_tokens == 3

    async def test_tool_call_fixture_assembles(self, tmp_path):
        fx = tmp_path / "tool.jsonl"
        write_fixture(fx, _TOOL_SSE)
        events = await _collect(ReplayProvider(fx))
        done = events[-1]
        assert isinstance(done, StreamDone)
        calls = done.message.tool_calls
        assert len(calls) == 1
        assert calls[0].name == "Read"
        assert calls[0].arguments == {"file_path": "/x"}

    async def test_model_defaults_to_constructor(self, tmp_path):
        fx = tmp_path / "text.jsonl"
        write_fixture(fx, _TEXT_SSE)
        events = await _collect(ReplayProvider(fx, model="canned"), model="")
        assert events[-1].message.model == "canned"


class _FakeResp:
    def __init__(self, lines, status=200):
        self._lines = lines
        self.status_code = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aread(self):
        return b"boom"

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


class _FakeClient:
    def __init__(self, lines, status=200):
        self._lines = lines
        self._status = status

    def stream(self, *a, **k):
        return _FakeResp(self._lines, self._status)


class TestRecordingProvider:
    async def test_records_raw_lines_and_passes_events(self, tmp_path):
        inner = OpenAICompatibleProvider(api_key="sk-test", base_url="https://x/v1")
        inner._client = _FakeClient(_TEXT_SSE)
        fx = tmp_path / "rec.jsonl"

        events = await _collect(RecordingProvider(inner, fx))
        assert isinstance(events[-1], StreamDone)
        assert events[-1].message.text == "Hello world"

        # Fixture holds the raw SSE lines, and replaying it reproduces events.
        recorded = [json.loads(l) for l in fx.read_text().splitlines() if l]
        assert recorded == _TEXT_SSE
        replayed = await _collect(ReplayProvider(fx))
        assert replayed[-1].message.text == "Hello world"

    async def test_non_200_encoded_as_error(self, tmp_path):
        inner = OpenAICompatibleProvider(api_key="sk-test", base_url="https://x/v1")
        inner._client = _FakeClient([], status=503)
        fx = tmp_path / "err.jsonl"
        events = await _collect(RecordingProvider(inner, fx))
        assert len(events) == 1
        assert isinstance(events[0], StreamError)
