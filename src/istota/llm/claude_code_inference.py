"""Claude Code CLI as a dumb inference endpoint.

Invokes ``claude -p - --output-format stream-json`` with no allowed tools,
using Claude Code as a completion API. The CLI handles authentication, prompt
caching, rate limiting, and model resolution (e.g. ``opus`` → latest Opus).

This is not an agent — it's a single completion call per ``stream()``. The
agent loop (``istota.agent``) drives tool-call iteration; this provider just
generates the next assistant message. Use it over ``ClaudeCodeBrain`` when you
want istota to own the loop but borrow Claude Code's auth/caching; use it over
``OpenAICompatibleProvider`` when you don't want to manage API keys directly.

It reuses the existing Claude Code stream-json parser (``brain._events``); the
difference from ``ClaudeCodeBrain`` is that there is no tool execution and no
loop — just one assembled ``AssistantMessage``.
"""

import asyncio
import logging
import subprocess
from collections.abc import AsyncIterator

from ..brain._events import ResultEvent, TextEvent, make_stream_parser
from .provider import StreamDone, StreamError, StreamEvent, TextDelta
from .types import AssistantMessage, Message, TextContent, ToolSchema

logger = logging.getLogger("istota.llm.claude_code_inference")


class ClaudeCodeInferenceProvider:
    def __init__(self, model: str = "", claude_binary: str = "claude"):
        self._model = model
        self._binary = claude_binary
        self._warned_tools = False

    async def stream(
        self,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSchema],
        *,
        model: str = "",
        max_tokens: int = 16384,
    ) -> AsyncIterator[StreamEvent]:
        effective_model = model or self._model
        if tools and not self._warned_tools:
            # This provider is inference-only — it runs the CLI with no allowed
            # tools and cannot emit tool-call deltas, so the agent loop will
            # never see a tool invocation. Tools are only described inline. Warn
            # once if it's wired as a brain provider with a non-empty tool set.
            self._warned_tools = True
            logger.warning(
                "ClaudeCodeInferenceProvider received %d tool(s) but cannot emit "
                "tool calls; the model will not be able to invoke them. Use it for "
                "inference only (compaction, classification).",
                len(tools),
            )
        prompt_text = self._compose_prompt(system_prompt, messages, tools)

        cmd = [self._binary, "-p", "-", "--output-format", "stream-json", "--verbose"]
        if effective_model:
            cmd.extend(["--model", effective_model])
        cmd.extend(["--allowedTools", ""])  # no tools — inference only

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as e:
            yield StreamError(
                message=AssistantMessage(
                    stop_reason="error",
                    error_message=f"Claude CLI not available: {e}",
                    model=effective_model,
                )
            )
            return

        if proc.stdin is not None:
            proc.stdin.write(prompt_text.encode())
            await proc.stdin.drain()
            proc.stdin.close()

        async for event in self._parse_claude_lines(
            _decode_lines(proc.stdout), effective_model
        ):
            yield event

        await proc.wait()

    def _compose_prompt(
        self, system_prompt: str, messages: list[Message], tools: list[ToolSchema]
    ) -> str:
        """Flatten system prompt + conversation into a single prompt string.

        The inference-only CLI takes one prompt over stdin; there is no separate
        system parameter. Tool schemas are described inline so the model can
        emit tool-call-shaped responses for the agent loop to parse (a future
        enhancement); for plain completions this is a no-op when ``tools`` is
        empty.
        """
        parts: list[str] = []
        if system_prompt:
            parts.append(system_prompt)
        for msg in messages:
            role = getattr(msg, "role", "user")
            text = "".join(
                c.text for c in getattr(msg, "content", []) if isinstance(c, TextContent)
            )
            if text:
                parts.append(f"{role}: {text}")
        if tools:
            names = ", ".join(t.name for t in tools)
            parts.append(f"Available tools: {names}")
        return "\n\n".join(parts)

    async def _parse_claude_lines(
        self, lines: AsyncIterator[str], model: str
    ) -> AsyncIterator[StreamEvent]:
        """Parse Claude Code stream-json lines into stream events.

        Reuses the Claude Code parser. ``TextEvent``s become ``TextDelta``s; the
        terminal ``ResultEvent`` supplies the final text and success flag. If no
        ResultEvent arrives, falls back to the concatenated streamed text.
        """
        parse = make_stream_parser()
        text_parts: list[str] = []
        final_text: str | None = None
        success = True

        async for raw in lines:
            event = parse(raw)
            if isinstance(event, TextEvent):
                text_parts.append(event.text)
                yield TextDelta(text=event.text)
            elif isinstance(event, ResultEvent):
                final_text = event.text
                success = event.success

        text = final_text if final_text is not None else "\n".join(text_parts)
        yield StreamDone(
            message=AssistantMessage(
                content=[TextContent(text=text)],
                stop_reason="end_turn" if success else "error",
                error_message=None if success else (text or "inference failed"),
                model=model,
            )
        )


async def _decode_lines(stream) -> AsyncIterator[str]:
    """Yield decoded text lines from an asyncio subprocess stdout stream."""
    if stream is None:
        return
    async for raw in stream:
        yield raw.decode(errors="replace")
