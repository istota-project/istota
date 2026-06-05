"""A scripted mock LLMProvider for agent-loop tests.

Feed it a list of ``AssistantMessage`` turns. Each call to ``stream`` pops the
next turn, emits text / tool-call deltas reconstructed from its content, then a
``StreamDone`` (or ``StreamError`` if the turn's stop_reason is "error").

The mock records every ``stream`` invocation's ``messages`` and ``model`` so
tests can assert on what the loop fed back across turns.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from istota.llm.provider import (
    StreamDone,
    StreamError,
    StreamEvent,
    StreamStart,
    TextDelta,
    ToolCallDelta,
)
from istota.llm.types import (
    AssistantMessage,
    Message,
    TextContent,
    ToolCallContent,
    ToolSchema,
)


class MockProvider:
    def __init__(self, turns: list[AssistantMessage]):
        self._turns = list(turns)
        self.calls: list[dict] = []

    async def stream(
        self,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSchema],
        *,
        model: str = "",
        max_tokens: int = 16384,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "messages": list(messages),
                "tools": list(tools),
                "model": model,
                "max_tokens": max_tokens,
            }
        )
        if not self._turns:
            raise AssertionError("MockProvider ran out of scripted turns")
        turn = self._turns.pop(0)

        yield StreamStart()
        for block in turn.content:
            if isinstance(block, TextContent):
                yield TextDelta(text=block.text)
            elif isinstance(block, ToolCallContent):
                yield ToolCallDelta(
                    id=block.id,
                    name=block.name,
                    arguments_delta=json.dumps(block.arguments),
                )

        if turn.stop_reason == "error":
            yield StreamError(message=turn)
        else:
            yield StreamDone(message=turn)
