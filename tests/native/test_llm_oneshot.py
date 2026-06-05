"""One-shot text completion helper — lightweight inference, no agent loop.

Used by Pass-2 skill routing (and any caller wanting a quick prompt->text
completion) so the native brain doesn't have to shell out to the `claude` CLI.
"""

import pytest

from istota.llm.oneshot import complete_text
from istota.llm.provider import StreamError
from istota.llm.types import AssistantMessage, TextContent

from ._mock_provider import MockProvider


def test_collects_final_text():
    provider = MockProvider([AssistantMessage(content=[TextContent(text="hello there")])])
    out = complete_text(provider, "sys", "hi", model="m")
    assert out == "hello there"


def test_passes_model_and_prompt():
    provider = MockProvider([AssistantMessage(content=[TextContent(text="ok")])])
    complete_text(provider, "be terse", "classify this", model="qwen-x", max_tokens=256)
    call = provider.calls[0]
    assert call["model"] == "qwen-x"
    assert call["max_tokens"] == 256
    assert call["system_prompt"] == "be terse"
    assert call["messages"][0].content[0].text == "classify this"


def test_stream_error_returns_none():
    provider = MockProvider(
        [AssistantMessage(content=[TextContent(text="boom")], stop_reason="error")]
    )
    assert complete_text(provider, "", "x", model="m") is None


def test_timeout_returns_none():
    class _SlowProvider:
        async def stream(self, *a, **k):
            import asyncio

            await asyncio.sleep(5)
            yield  # never reached

    assert complete_text(_SlowProvider(), "", "x", model="m", timeout=0.1) is None
