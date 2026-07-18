"""OpenAI-compatible chat completions provider.

The standard provider. Works with any endpoint implementing the OpenAI chat
completions API: Anthropic's OpenAI-compatible endpoint, OpenRouter, and local
models (Ollama, vLLM, llama.cpp). ``base_url`` + ``api_key`` select the backend.

Per the provider contract, this never raises for API/runtime failures — HTTP
errors and connection errors are encoded as a single ``StreamError`` event.
"""

import json
import logging
from collections.abc import AsyncIterator

import httpx

from .provider import (
    StreamDone,
    StreamError,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
)
from .types import (
    AssistantMessage,
    ImageContent,
    Message,
    TextContent,
    ThinkingContent,
    ToolCallContent,
    ToolSchema,
    Usage,
)

logger = logging.getLogger("istota.llm.openai_compat")

# OpenAI finish_reason → istota stop_reason. ``content_filter`` is preserved
# (not laundered into ``end_turn``) so a filtered/blocked answer is visible
# downstream rather than delivered as a clean completion (NB-15).
_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "content_filter",
}

# The OpenAI-compatible ``reasoning_effort`` field accepts only low/medium/high.
# Istota's finer Opus tiers fold down — the compat endpoint exposes no knob
# below "high", so xhigh/max both map there. The original tier stays on the
# task row unchanged.
_REASONING_EFFORT_WIRE = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


class OpenAICompatibleProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        timeout: float = 300.0,
        extra_headers: dict[str, str] | None = None,
        prompt_caching: bool = False,
    ):
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, headers=headers)
        # Opt-in: add Anthropic/OpenRouter cache_control breakpoints. Off by
        # default so a plain-OpenAI / local endpoint never sees the extension.
        self._prompt_caching = prompt_caching

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
        body = self._build_chat_completion_request(
            system_prompt,
            messages,
            tools,
            model,
            max_tokens,
            reasoning_effort=reasoning_effort,
            render_tool_images=render_tool_images,
        )
        try:
            async with self._client.stream("POST", "/chat/completions", json=body) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    text = error_body.decode() if isinstance(error_body, bytes) else str(error_body)
                    yield StreamError(
                        message=AssistantMessage(
                            stop_reason="error",
                            error_message=f"HTTP {resp.status_code}: {text[:500]}",
                            model=model,
                        )
                    )
                    return
                async for event in self._parse_sse_lines(resp.aiter_lines(), model):
                    yield event
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError, OSError) as e:
            yield StreamError(
                message=AssistantMessage(
                    stop_reason="error",
                    error_message=f"Connection error: {e}",
                    model=model,
                )
            )

    def _build_chat_completion_request(
        self,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSchema],
        model: str,
        max_tokens: int,
        reasoning_effort: str | None = None,
        render_tool_images: bool = False,
    ) -> dict:
        """Convert istota Message types to an OpenAI chat completions body."""
        wire: list[dict] = []
        if system_prompt:
            wire.append({"role": "system", "content": system_prompt})
        for msg in messages:
            wire.append(self._message_to_wire(msg))
            # A tool result carrying image content can't ride in the
            # ``role:"tool"`` message on most endpoints; Anthropic's compat layer
            # honors a follow-up ``role:"user"`` block instead. Inject it right
            # after the tool message so the image lands in order.
            extra = self._tool_image_followup(msg, render_tool_images)
            if extra is not None:
                wire.append(extra)

        tools_wire = [self._tool_to_wire(t) for t in tools] if tools else None

        if self._prompt_caching:
            self._apply_cache_breakpoints(wire, tools_wire)

        body: dict = {
            "model": model,
            "messages": wire,
            "max_tokens": max_tokens,
            "stream": True,
            # Ask the server for a trailing usage chunk; without this OpenAI-
            # compatible endpoints (OpenAI, LM Studio, vLLM, OpenRouter) report
            # no token usage in streaming mode and cost telemetry stays zero.
            "stream_options": {"include_usage": True},
        }
        if tools_wire is not None:
            body["tools"] = tools_wire

        effort = _REASONING_EFFORT_WIRE.get((reasoning_effort or "").lower())
        if effort:
            body["reasoning_effort"] = effort
        return body

    @staticmethod
    def _message_to_wire(msg: Message) -> dict:
        role = getattr(msg, "role", "user")
        if role == "tool_result":
            return {
                "role": "tool",
                "tool_call_id": msg.tool_call_id,
                "content": "".join(
                    c.text for c in msg.content if isinstance(c, TextContent)
                ),
            }
        if role == "assistant":
            wire: dict = {"role": "assistant"}
            text = "".join(c.text for c in msg.content if isinstance(c, TextContent))
            wire["content"] = text or None
            tool_calls = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in msg.content
                if isinstance(c, ToolCallContent)
            ]
            if tool_calls:
                wire["tool_calls"] = tool_calls
            return wire

        # user message
        parts: list[dict] = []
        for c in msg.content:
            if isinstance(c, TextContent):
                parts.append({"type": "text", "text": c.text})
            elif isinstance(c, ImageContent):
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{c.media_type};base64,{c.data}"},
                    }
                )
        return {"role": "user", "content": parts}

    @staticmethod
    def _tool_image_followup(msg: Message, render_tool_images: bool) -> dict | None:
        """A follow-up ``role:"user"`` message carrying a tool result's images.

        The ``role:"tool"`` message renders text only; image parts ride in a
        follow-up user message injected right after it (the portable pattern
        Anthropic's compat layer honors). On a no-vision model the images are
        dropped and replaced with a text note so the request still validates.
        Returns ``None`` when the message isn't an image-bearing tool result.
        """
        if getattr(msg, "role", "") != "tool_result":
            return None
        images = [c for c in msg.content if isinstance(c, ImageContent)]
        if not images:
            return None
        if not render_tool_images:
            return {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "[image output omitted: model has no vision support]",
                    }
                ],
            }
        tool_name = getattr(msg, "tool_name", "") or "tool"
        parts: list[dict] = [
            {"type": "text", "text": f"Image output of tool {tool_name}:"}
        ]
        for img in images:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{img.media_type};base64,{img.data}"},
                }
            )
        return {"role": "user", "content": parts}

    @staticmethod
    def _apply_cache_breakpoints(
        wire: list[dict], tools_wire: list[dict] | None = None
    ) -> None:
        """Mark the cacheable prefix with cache_control (Anthropic/OpenRouter).

        Anthropic caps cache_control breakpoints at 4; we place up to four, in
        priority order:

        1. **Tool definitions** — the last tool's ``function`` (tool schemas are
           constant across a task).
        2. **System message** — the stable system prompt.
        3. **First user message** — the large composed prompt.
        4. **Rolling breakpoint** — the last content block of the *last* message,
           so turn N reuses the cached prefix through turn N−1. This is what
           yields cross-turn cache hits.

        Because a cached breakpoint also reads everything before it, the rolling
        breakpoint plus the system breakpoint together cover the whole prefix.
        Marking the same block twice (single-turn: first user == last message) is
        idempotent — one breakpoint, not two. Mutates ``wire`` / ``tools_wire``
        in place; converts string content to a single text block so cache_control
        has somewhere to live.
        """
        cc = {"type": "ephemeral"}

        def _mark(msg: dict) -> None:
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = [
                    {"type": "text", "text": content, "cache_control": cc}
                ]
            elif isinstance(content, list) and content:
                for block in reversed(content):
                    if isinstance(block, dict) and block.get("type") == "text":
                        block["cache_control"] = cc
                        return
                if isinstance(content[-1], dict):
                    content[-1]["cache_control"] = cc

        # 1. Tool definitions — mark the last tool's function object.
        if tools_wire:
            last_fn = tools_wire[-1].get("function")
            if isinstance(last_fn, dict):
                last_fn["cache_control"] = cc

        # 2. System message.
        if wire and wire[0].get("role") == "system":
            _mark(wire[0])

        # 3. First user message.
        for msg in wire:
            if msg.get("role") == "user":
                _mark(msg)
                break

        # 4. Rolling breakpoint — the last message in the wire.
        if wire:
            _mark(wire[-1])

    @staticmethod
    def _tool_to_wire(tool: ToolSchema) -> dict:
        properties: dict = {}
        required: list[str] = []
        for p in tool.parameters:
            prop: dict = {"type": p.type}
            if p.description:
                prop["description"] = p.description
            if p.enum:
                prop["enum"] = p.enum
            properties[p.name] = prop
            if p.required:
                required.append(p.name)
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    async def _parse_sse_lines(
        self, lines: AsyncIterator[str], model: str = ""
    ) -> AsyncIterator[StreamEvent]:
        """Parse OpenAI SSE chunks into stream events, assembling a final message."""
        text_parts: list[str] = []
        # Extended-thinking deltas (Anthropic compat: ``reasoning_content``; some
        # endpoints: ``reasoning``). Captured for message fidelity but never
        # emitted as a TextDelta — it must not land in result_text or progress.
        thinking_parts: list[str] = []
        # index -> {"id", "name", "args"}
        tool_acc: dict[int, dict] = {}
        finish_reason: str | None = None
        usage_raw: dict | None = None
        saw_done = False

        async for raw in lines:
            line = raw.strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                saw_done = True
                break
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                logger.debug("Skipping non-JSON SSE payload: %s", payload[:100])
                continue

            # Mid-stream error frame (HTTP 200 body, no choices): OpenRouter and
            # some gateways report an upstream failure as a ``data: {"error":…}``
            # SSE frame rather than a non-200 status. Surface it as a StreamError
            # instead of discarding it and EOF-ing into a false clean completion
            # (NB-2). Anthropic's compat layer nests it under ``error``; a few
            # gateways nest it inside the choice — handle both.
            err = data.get("error")
            if err is None:
                ch = (data.get("choices") or [{}])[0]
                err = ch.get("error") if isinstance(ch, dict) else None
            if err is not None:
                msg = err.get("message") if isinstance(err, dict) else str(err)
                code = err.get("code") if isinstance(err, dict) else None
                detail = f"HTTP {code}: {msg}" if code else (msg or "stream error")
                yield StreamError(
                    message=AssistantMessage(
                        content=[TextContent(text="".join(text_parts))] if text_parts else [],
                        stop_reason="error",
                        error_message=f"Provider error frame: {detail}",
                        model=model,
                    )
                )
                return

            if data.get("usage"):
                usage_raw = data["usage"]

            choices = data.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}

            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                # Accumulate for StreamDone message fidelity (assembled into a
                # ThinkingContent block, excluded from result_text) AND stream it
                # live so the executor can surface reasoning on stream surfaces.
                thinking_parts.append(reasoning)
                yield ThinkingDelta(thinking=reasoning)

            content = delta.get("content")
            if content:
                text_parts.append(content)
                yield TextDelta(text=content)

            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                args_delta = fn.get("arguments") or ""
                if args_delta:
                    slot["args"] += args_delta
                yield ToolCallDelta(
                    id=slot["id"], name=slot["name"], arguments_delta=args_delta
                )

            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

        # A stream that ends without ever signalling completion — no
        # ``finish_reason`` and no ``[DONE]`` sentinel — is a truncated response
        # (dropped connection, gateway cut-off). Delivering the partial text as a
        # clean ``end_turn`` would report a wrong/short answer as success (NB-2).
        # Surface it as an error so the task retries instead.
        if finish_reason is None and not saw_done:
            yield StreamError(
                message=AssistantMessage(
                    content=[TextContent(text="".join(text_parts))] if text_parts else [],
                    stop_reason="error",
                    error_message=(
                        "Stream ended without completion (no finish_reason or "
                        "[DONE]); response likely truncated."
                    ),
                    model=model,
                )
            )
            return

        yield StreamDone(
            message=self._assemble_message(
                text_parts, thinking_parts, tool_acc, finish_reason, usage_raw, model
            )
        )

    @staticmethod
    def _assemble_message(
        text_parts: list[str],
        thinking_parts: list[str],
        tool_acc: dict[int, dict],
        finish_reason: str | None,
        usage_raw: dict | None,
        model: str,
    ) -> AssistantMessage:
        content: list = []
        thinking = "".join(thinking_parts)
        if thinking:
            # Thinking precedes the visible answer in the assembled message so a
            # future surface can render it in order. AssistantMessage.text
            # filters to TextContent, so this never leaks into the result.
            content.append(ThinkingContent(thinking=thinking))
        joined = "".join(text_parts)
        if joined:
            content.append(TextContent(text=joined))
        for idx in sorted(tool_acc):
            slot = tool_acc[idx]
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                args = {}
            content.append(ToolCallContent(id=slot["id"], name=slot["name"], arguments=args))

        usage = Usage()
        if usage_raw:
            details = usage_raw.get("prompt_tokens_details") or {}
            cached = details.get("cached_tokens", 0)
            # Anthropic's OpenAI-compat usage reports cache writes either at the
            # top level (``cache_creation_input_tokens``) or nested in
            # prompt_tokens_details. Best-effort: absent → 0.
            cache_write = (
                usage_raw.get("cache_creation_input_tokens")
                or details.get("cache_creation_input_tokens")
                or 0
            )
            usage = Usage(
                input_tokens=usage_raw.get("prompt_tokens", 0),
                output_tokens=usage_raw.get("completion_tokens", 0),
                cache_read_tokens=cached,
                cache_write_tokens=cache_write,
            )

        stop_reason = _FINISH_REASON_MAP.get(finish_reason or "", "end_turn")
        return AssistantMessage(
            content=content, usage=usage, stop_reason=stop_reason, model=model
        )
