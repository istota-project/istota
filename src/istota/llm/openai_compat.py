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

from .provider import StreamDone, StreamError, StreamEvent, TextDelta, ToolCallDelta
from .types import (
    AssistantMessage,
    ImageContent,
    Message,
    TextContent,
    ToolCallContent,
    ToolSchema,
    Usage,
)

logger = logging.getLogger("istota.llm.openai_compat")

# OpenAI finish_reason → istota stop_reason
_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
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
    ) -> AsyncIterator[StreamEvent]:
        body = self._build_chat_completion_request(
            system_prompt, messages, tools, model, max_tokens
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
    ) -> dict:
        """Convert istota Message types to an OpenAI chat completions body."""
        wire: list[dict] = []
        if system_prompt:
            wire.append({"role": "system", "content": system_prompt})
        for msg in messages:
            wire.append(self._message_to_wire(msg))

        if self._prompt_caching:
            self._apply_cache_breakpoints(wire)

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
        if tools:
            body["tools"] = [self._tool_to_wire(t) for t in tools]
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
    def _apply_cache_breakpoints(wire: list[dict]) -> None:
        """Mark the stable prefix with cache_control (Anthropic/OpenRouter).

        Two breakpoints on the blocks that stay constant across a task's turns:
        the system message and the first user message (the native brain's large
        composed prompt). Mutates ``wire`` in place; converts string content to
        a single text block so the cache_control field has somewhere to live.
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

        if wire and wire[0].get("role") == "system":
            _mark(wire[0])
        for msg in wire:
            if msg.get("role") == "user":
                _mark(msg)
                break

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
        # index -> {"id", "name", "args"}
        tool_acc: dict[int, dict] = {}
        finish_reason: str | None = None
        usage_raw: dict | None = None

        async for raw in lines:
            line = raw.strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                break
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                logger.debug("Skipping non-JSON SSE payload: %s", payload[:100])
                continue

            if data.get("usage"):
                usage_raw = data["usage"]

            choices = data.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}

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

        yield StreamDone(
            message=self._assemble_message(text_parts, tool_acc, finish_reason, usage_raw, model)
        )

    @staticmethod
    def _assemble_message(
        text_parts: list[str],
        tool_acc: dict[int, dict],
        finish_reason: str | None,
        usage_raw: dict | None,
        model: str,
    ) -> AssistantMessage:
        content: list = []
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
            cached = (usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            usage = Usage(
                input_tokens=usage_raw.get("prompt_tokens", 0),
                output_tokens=usage_raw.get("completion_tokens", 0),
                cache_read_tokens=cached,
            )

        stop_reason = _FINISH_REASON_MAP.get(finish_reason or "", "end_turn")
        return AssistantMessage(
            content=content, usage=usage, stop_reason=stop_reason, model=model
        )
