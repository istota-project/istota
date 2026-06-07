"""Stage 1 — reasoning/effort budgets for the native brain.

Three sub-changes:
- ``reasoning_effort`` plumbed into the OpenAI-compatible request body, with the
  ``xhigh`` / ``max`` tiers folded to ``high`` (the only knob the compat field
  exposes).
- A capability gate in ``NativeBrain``: effort is forwarded only when the target
  model ``supports_thinking``; otherwise it's dropped (no ``reasoning_effort``
  reaches the provider) so a non-reasoning endpoint never 400s.
- Extended-thinking deltas (``reasoning_content`` / ``reasoning``) parsed into a
  ``ThinkingContent`` block that never leaks into ``result_text``.
"""

from pathlib import Path

from istota.brain.native import NativeBrain
from istota.config import NativeBrainConfig
from istota.llm.openai_compat import OpenAICompatibleProvider
from istota.llm.provider import StreamDone, TextDelta
from istota.llm.types import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    Usage,
)

from ._mock_provider import MockProvider


async def _aiter(lines):
    for line in lines:
        yield line


def _provider():
    return OpenAICompatibleProvider(api_key="sk-test", base_url="https://x/v1")


def _req(prompt, cwd, model="claude-sonnet-4-6", effort=""):
    from istota.brain import BrainRequest

    return BrainRequest(
        prompt=prompt,
        allowed_tools=[],
        cwd=cwd,
        env={},
        timeout_seconds=30,
        model=model,
        effort=effort,
    )


class TestRequestBodyEffort:
    def test_reasoning_effort_added_when_passed(self):
        body = _provider()._build_chat_completion_request(
            "", [], [], "m", 100, reasoning_effort="high"
        )
        assert body["reasoning_effort"] == "high"

    def test_reasoning_effort_omitted_when_none(self):
        body = _provider()._build_chat_completion_request("", [], [], "m", 100)
        assert "reasoning_effort" not in body

    def test_reasoning_effort_omitted_when_empty(self):
        body = _provider()._build_chat_completion_request(
            "", [], [], "m", 100, reasoning_effort=""
        )
        assert "reasoning_effort" not in body

    def test_low_medium_high_pass_through(self):
        for tier in ("low", "medium", "high"):
            body = _provider()._build_chat_completion_request(
                "", [], [], "m", 100, reasoning_effort=tier
            )
            assert body["reasoning_effort"] == tier

    def test_xhigh_maps_to_high(self):
        body = _provider()._build_chat_completion_request(
            "", [], [], "m", 100, reasoning_effort="xhigh"
        )
        assert body["reasoning_effort"] == "high"

    def test_max_maps_to_high(self):
        body = _provider()._build_chat_completion_request(
            "", [], [], "m", 100, reasoning_effort="max"
        )
        assert body["reasoning_effort"] == "high"


class TestThinkingParse:
    async def _collect(self, lines, model="m"):
        return [e async for e in _provider()._parse_sse_lines(_aiter(lines), model)]

    async def test_reasoning_content_folds_into_thinking_block(self):
        lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"Let me think. "}}]}',
            'data: {"choices":[{"delta":{"reasoning_content":"Step two."}}]}',
            'data: {"choices":[{"delta":{"content":"The answer is 42."}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        done = (await self._collect(lines))[-1]
        assert isinstance(done, StreamDone)
        thinking = [c for c in done.message.content if isinstance(c, ThinkingContent)]
        assert len(thinking) == 1
        assert thinking[0].thinking == "Let me think. Step two."
        # the visible answer is unchanged, and excludes the thinking
        assert done.message.text == "The answer is 42."

    async def test_reasoning_alias_field_also_parsed(self):
        lines = [
            'data: {"choices":[{"delta":{"reasoning":"hmm"}}]}',
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        done = (await self._collect(lines))[-1]
        thinking = [c for c in done.message.content if isinstance(c, ThinkingContent)]
        assert thinking and thinking[0].thinking == "hmm"

    async def test_thinking_not_emitted_as_text_delta(self):
        lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"secret reasoning"}}]}',
            'data: {"choices":[{"delta":{"content":"visible"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        events = await self._collect(lines)
        text_deltas = [e.text for e in events if isinstance(e, TextDelta)]
        assert "visible" in text_deltas
        assert "secret reasoning" not in text_deltas

    async def test_no_thinking_block_when_absent(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"plain"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        done = (await self._collect(lines))[-1]
        assert not any(isinstance(c, ThinkingContent) for c in done.message.content)

    async def test_thinking_block_precedes_text(self):
        lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"r"}}]}',
            'data: {"choices":[{"delta":{"content":"t"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        done = (await self._collect(lines))[-1]
        assert isinstance(done.message.content[0], ThinkingContent)


class TestCapabilityGate:
    def _brain(self, provider, model, effort, tmp_path):
        config = NativeBrainConfig(model=model)
        brain = NativeBrain(config, provider=provider)
        return brain.execute(_req("hi", tmp_path, model=model, effort=effort))

    def test_effort_forwarded_on_thinking_model(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        self._brain(provider, "claude-sonnet-4-6", "high", tmp_path)
        assert provider.calls[0]["reasoning_effort"] == "high"

    def test_raw_tier_forwarded_provider_maps_it(self, tmp_path):
        # The brain forwards the raw tier; the OpenAI-compatible provider folds
        # xhigh→high at the wire boundary, not the brain.
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        self._brain(provider, "claude-sonnet-4-6", "xhigh", tmp_path)
        assert provider.calls[0]["reasoning_effort"] == "xhigh"

    def test_effort_dropped_on_non_thinking_model(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        # haiku-4-5 has supports_thinking=False in the bundled catalog
        self._brain(provider, "claude-haiku-4-5", "high", tmp_path)
        assert provider.calls[0]["reasoning_effort"] is None

    def test_effort_dropped_on_unknown_model(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        self._brain(provider, "qwen-local", "high", tmp_path)
        assert provider.calls[0]["reasoning_effort"] is None

    def test_no_effort_means_none_forwarded(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        self._brain(provider, "claude-sonnet-4-6", "", tmp_path)
        assert provider.calls[0]["reasoning_effort"] is None

    def test_config_effort_used_when_request_effort_empty(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        config = NativeBrainConfig(model="claude-sonnet-4-6", effort="medium")
        brain = NativeBrain(config, provider=provider)
        brain.execute(_req("hi", tmp_path, model="claude-sonnet-4-6", effort=""))
        assert provider.calls[0]["reasoning_effort"] == "medium"


class TestThinkingExcludedFromResult:
    def test_result_text_excludes_thinking_end_to_end(self, tmp_path):
        # An assistant turn carrying both a ThinkingContent and TextContent must
        # surface only the text as the result.
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[
                        ThinkingContent(thinking="internal monologue"),
                        TextContent(text="The visible answer."),
                    ],
                    usage=Usage(input_tokens=10, output_tokens=5),
                    stop_reason="end_turn",
                )
            ]
        )
        config = NativeBrainConfig(model="claude-sonnet-4-6")
        result = NativeBrain(config, provider=provider).execute(
            _req("q", tmp_path, model="claude-sonnet-4-6")
        )
        assert result.result_text == "The visible answer."
        assert "internal monologue" not in result.result_text
