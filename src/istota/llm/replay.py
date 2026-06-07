"""Tier-1 SSE replay/record providers for offline native-brain testing.

The mock provider (``tests/native/_mock_provider.py``) exercises the agent loop
but bypasses real SSE parsing. These two providers close that gap without
spending credits:

- ``ReplayProvider`` reads recorded SSE lines from a JSONL fixture and feeds
  them through the *real* :class:`OpenAICompatibleProvider` parser, so CI
  exercises streaming-delta reassembly and tool-arg JSON repair offline. This
  is the CI default.
- ``RecordingProvider`` wraps a live provider, tees every raw SSE line to a
  fixture, and passes events through unchanged. Run it once with a real key
  (``ISTOTA_NATIVE_RECORD=1``) to (re)generate fixtures, commit them, then
  replay for free.

Fixture format: JSONL, one ``json.dumps(raw_sse_line)`` per line. JSON-encoding
each line keeps embedded quotes/escapes intact and survives round-tripping.
"""

from collections.abc import AsyncIterator
from pathlib import Path
import json

from .openai_compat import OpenAICompatibleProvider
from .provider import StreamError, StreamEvent
from .types import AssistantMessage, Message, ToolSchema


def write_fixture(path, lines) -> None:
    """Write raw SSE lines to a JSONL fixture (one json-encoded line each)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(line) + "\n" for line in lines)
    p.write_text(body)


def read_fixture(path) -> list[str]:
    """Read raw SSE lines back from a JSONL fixture."""
    text = Path(path).read_text()
    return [json.loads(line) for line in text.splitlines() if line]


class ReplayProvider:
    """Replays recorded SSE lines through the real OpenAI-compatible parser.

    Construction never opens a socket — the embedded parser-only provider is
    used solely for its ``_parse_sse_lines`` method.
    """

    def __init__(self, fixture_path, model: str = ""):
        self._path = Path(fixture_path)
        self._model = model
        self._parser = OpenAICompatibleProvider(
            api_key="", base_url="http://replay.invalid"
        )

    async def stream(
        self,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSchema],
        *,
        model: str = "",
        max_tokens: int = 16384,
        reasoning_effort: str | None = None,
        render_tool_images: bool = False,
    ) -> AsyncIterator[StreamEvent]:
        lines = read_fixture(self._path)

        async def _aiter():
            for line in lines:
                yield line

        async for event in self._parser._parse_sse_lines(
            _aiter(), model or self._model
        ):
            yield event


class RecordingProvider:
    """Wraps a live OpenAICompatibleProvider, teeing raw SSE lines to a fixture.

    Reuses the inner provider's HTTP client, request builder, and parser — only
    the ~10-line stream scaffold is re-expressed so the raw lines can be
    captured before parsing. The fixture is written on stream completion (even
    on early error/abort, via ``finally``).
    """

    def __init__(self, inner: OpenAICompatibleProvider, fixture_path):
        self._inner = inner
        self._path = Path(fixture_path)

    async def stream(
        self,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSchema],
        *,
        model: str = "",
        max_tokens: int = 16384,
        reasoning_effort: str | None = None,
        render_tool_images: bool = False,
    ) -> AsyncIterator[StreamEvent]:
        body = self._inner._build_chat_completion_request(
            system_prompt,
            messages,
            tools,
            model,
            max_tokens,
            reasoning_effort=reasoning_effort,
            render_tool_images=render_tool_images,
        )
        recorded: list[str] = []
        try:
            async with self._inner._client.stream(
                "POST", "/chat/completions", json=body
            ) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    text = (
                        error_body.decode()
                        if isinstance(error_body, bytes)
                        else str(error_body)
                    )
                    yield StreamError(
                        message=AssistantMessage(
                            stop_reason="error",
                            error_message=f"HTTP {resp.status_code}: {text[:500]}",
                            model=model,
                        )
                    )
                    return

                async def _tee():
                    async for line in resp.aiter_lines():
                        recorded.append(line)
                        yield line

                async for event in self._inner._parse_sse_lines(_tee(), model):
                    yield event
        finally:
            write_fixture(self._path, recorded)
