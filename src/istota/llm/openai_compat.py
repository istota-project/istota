"""OpenAI-compatible chat completions provider.

The standard provider. Works with any endpoint implementing the OpenAI chat
completions API: Anthropic's OpenAI-compatible endpoint, OpenRouter, and local
models (Ollama, vLLM, llama.cpp). ``base_url`` + ``api_key`` select the backend.

Per the provider contract, this never raises for API/runtime failures — HTTP
errors and connection errors are encoded as a single ``StreamError`` event.
"""

import json
import logging
import math
import re
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
    PROMPT_CACHE_TTLS,
    REASONING_REPLAY_MODES,
    AssistantMessage,
    ImageContent,
    Message,
    TextContent,
    ThinkingContent,
    ToolCallContent,
    ToolParameter,
    ToolSchema,
    Usage,
)

logger = logging.getLogger("istota.llm.openai_compat")

# OpenAI finish_reason → istota stop_reason. ``content_filter`` is preserved
# (not laundered into ``end_turn``) so a filtered/blocked answer is visible
# downstream rather than delivered as a clean completion (NB-15). ``error`` is
# preserved too: OpenRouter reports an upstream generation failure (notably
# Gemini's ``MALFORMED_FUNCTION_CALL`` — the model emitted a tool call whose
# arguments are invalid JSON) as ``finish_reason: "error"`` in a normal choices
# chunk with NO ``error`` object, so the mid-stream error-frame guard never
# fires. Without this entry ``.get("error", "end_turn")`` would launder a
# provider error into a clean ``end_turn`` and the half-built (often empty)
# response would be delivered as a silent success (seen in production).
_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "content_filter",
    "error": "error",
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

# Model families on OpenAI's own endpoint that require ``max_completion_tokens``
# instead of ``max_tokens`` (NB-12). Matched as a lowercase prefix of the id.
_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def _uses_max_completion_tokens(base_url: str, model: str) -> bool:
    if "api.openai.com" not in (base_url or ""):
        return False
    return model.lower().startswith(_OPENAI_REASONING_PREFIXES)


def _supports_cost_accounting(base_url: str) -> bool:
    """Whether the endpoint returns real per-request cost in its usage object.

    OpenRouter does when the request carries ``"usage": {"include": true}``;
    other OpenAI-compatible endpoints (OpenAI, vLLM, LM Studio, local servers)
    don't, and some 400 on the unknown body field — so the request param is
    scoped to OpenRouter. Parsing of ``usage.cost`` stays unconditional and
    degrades gracefully (absent → catalog pricing)."""
    return "openrouter.ai" in (base_url or "")


def _parse_reported_cost(usage_raw: dict) -> float | None:
    """Extract a provider-reported per-request cost (USD) from a usage object.

    OpenRouter reports the real charged cost (markup included) at the top-level
    ``cost`` field. Returns a finite, non-negative float, or ``None`` when the
    field is absent or unusable — in which case cost telemetry falls back to
    catalog pricing. Rejections that matter:
    - ``bool`` (an ``int`` subclass, so ``True`` would otherwise become ``1.0``);
    - non-numeric (a string like ``"0.0004"`` from a re-serializing proxy);
    - non-finite / negative: ``json.loads`` accepts bare ``NaN``/``Infinity``, and
      a single ``nan`` would propagate through every subsequent ``+=`` and poison
      the whole task's running total (and serialize as invalid JSON in task_logs).
    A genuine ``0.0`` (free turn) is kept — distinct from ``None`` (unreported)."""
    raw = usage_raw.get("cost")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        return None
    return value


# The plain-string reasoning delta fields, in the order they are read.
_PLAIN_REASONING_FIELDS = ("reasoning_content", "reasoning")

# String fields of a ``reasoning_details`` item that stream in pieces.
_DETAIL_STRING_FIELDS = ("text", "summary", "data")

# Readable reasoning in a ``reasoning_details`` item, by item type. Encrypted
# items carry ``data``, which is not.
_DETAIL_READABLE_FIELD = {"reasoning.text": "text", "reasoning.summary": "summary"}


def coalesce_reasoning_details(items: list) -> list[dict]:
    """Merge streamed ``reasoning_details`` fragments into whole items.

    Consecutive items sharing ``type`` and ``index`` are one item streamed in
    pieces: their ``text``, ``summary`` and ``data`` are concatenated, the
    other fields come from the first piece, and ``signature`` is the last
    non-empty one (it arrives with the final piece). Checked against the
    recorded OpenRouter stream, which sends one ``reasoning.text`` item per
    chunk at index 0.
    """
    merged: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        last = merged[-1] if merged else None
        if (
            last is not None
            and last.get("type") == item.get("type")
            and last.get("index") == item.get("index")
        ):
            for key in _DETAIL_STRING_FIELDS:
                piece = item.get(key)
                if isinstance(piece, str) and piece:
                    last[key] = (last.get(key) or "") + piece
            signature = item.get("signature")
            if signature:
                last["signature"] = signature
            continue
        merged.append(dict(item))
    return merged


def _readable_detail_text(item: dict) -> str:
    field = _DETAIL_READABLE_FIELD.get(item.get("type"))
    value = item.get(field) if field else None
    return value if isinstance(value, str) else ""


def _reasoning_wire_fields(msg: Message, mode: str) -> dict:
    """What an assistant turn sends back of its reasoning, under ``mode``.

    ``auto`` sends structured ``reasoning_details`` verbatim and nothing else:
    plain-string replay is model-specific. ``on`` also sends plain reasoning
    under the field it arrived in. Details win over the plain text where both
    exist, since the text is a copy of what the details hold.
    """
    if mode not in ("auto", "on"):
        return {}
    blocks = [c for c in msg.content if isinstance(c, ThinkingContent)]
    details = [item for block in blocks for item in (block.details or [])]
    if details:
        return {"reasoning_details": details}
    if mode == "on":
        for block in blocks:
            if block.thinking and block.source_field in _PLAIN_REASONING_FIELDS:
                text = "".join(
                    b.thinking for b in blocks if b.source_field == block.source_field
                )
                return {block.source_field: text}
    return {}


def replays_reasoning(messages: list[Message], mode: str) -> bool:
    """Whether a request built from ``messages`` carries reasoning back."""
    for msg in messages:
        if getattr(msg, "role", "") == "assistant" and _reasoning_wire_fields(msg, mode):
            return True
    return False


_REASONING_REJECTION_RE = re.compile(r"reasoning|thinking|signature", re.IGNORECASE)
_REASONING_EFFORT_RE = re.compile(r"reasoning[_ ]effort", re.IGNORECASE)


def is_reasoning_rejection(error_message: str) -> bool:
    """A 400 that names reasoning, thinking or a signature.

    What an endpoint answers when it refuses replayed reasoning. A message
    naming only ``reasoning_effort`` is about a different field and does not
    count. Read off the ``HTTP 400`` this provider stamps into every error.
    """
    text = error_message or ""
    if not re.search(r"\bHTTP 400\b", text):
        return False
    return bool(_REASONING_REJECTION_RE.search(_REASONING_EFFORT_RE.sub("", text)))


def _tool_delta_index(tc: dict, tool_acc: dict, id_to_index: dict) -> int:
    """Resolve the accumulator slot for a streamed tool_call delta (NB-16).

    Prefers the wire ``index``. When absent: a new ``id`` opens a new slot; a
    known ``id`` maps back to its slot; a delta with neither (a bare argument
    continuation) appends to the most recently opened slot.
    """
    idx = tc.get("index")
    if idx is not None:
        tc_id = tc.get("id")
        if tc_id:
            id_to_index[tc_id] = idx
        return idx
    tc_id = tc.get("id")
    if tc_id:
        if tc_id not in id_to_index:
            id_to_index[tc_id] = len(tool_acc)
        return id_to_index[tc_id]
    # No index, no id: continuation of the open (highest) slot, or slot 0.
    return max(tool_acc) if tool_acc else 0


class OpenAICompatibleProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        timeout: float = 300.0,
        extra_headers: dict[str, str] | None = None,
        prompt_caching: bool = False,
        reasoning_replay: str = "auto",
        prompt_cache_key: bool = False,
        prompt_cache_ttl: str = "5m",
    ):
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        self._api_key = api_key or ""
        self._base_url = base_url or ""
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, headers=headers)
        # Opt-in: add Anthropic/OpenRouter cache_control breakpoints. Off by
        # default so a plain-OpenAI / local endpoint never sees the extension.
        self._prompt_caching = prompt_caching
        if reasoning_replay not in REASONING_REPLAY_MODES:
            logger.warning(
                "reasoning_replay=%r is not one of %s; using 'auto'",
                reasoning_replay,
                "/".join(REASONING_REPLAY_MODES),
            )
            reasoning_replay = "auto"
        self._reasoning_replay = reasoning_replay
        # Whether a request carries the caller's ``cache_affinity_key`` as the
        # ``prompt_cache_key`` body field. Resolved from the host by make_provider.
        self._prompt_cache_key = bool(prompt_cache_key)
        if prompt_cache_ttl not in PROMPT_CACHE_TTLS:
            logger.warning(
                "prompt_cache_ttl=%r is not one of %s; using '5m'",
                prompt_cache_ttl,
                "/".join(PROMPT_CACHE_TTLS),
            )
            prompt_cache_ttl = "5m"
        self._prompt_cache_ttl = prompt_cache_ttl

    @property
    def prompt_cache_key(self) -> bool:
        """Whether this provider sends ``prompt_cache_key``, resolved.

        ``NativeBrain`` mints an attempt's key only when this is on, and reads
        it for the session log's header.
        """
        return self._prompt_cache_key

    @property
    def prompt_cache_ttl(self) -> str:
        """The breakpoint TTL (``5m``/``1h``), which also prices cache writes."""
        return self._prompt_cache_ttl

    @property
    def reasoning_replay(self) -> str:
        """Which reasoning an assistant turn sends back (``auto``/``on``/``off``).

        Read by ``NativeBrain``'s retrying wrapper to tell whether a refused
        request carried reasoning, before it re-issues one without.
        """
        return self._reasoning_replay

    @property
    def prompt_caching(self) -> bool:
        """Whether this provider adds ``cache_control`` breakpoints.

        The *resolved* answer, which is not the same thing as the config field:
        ``make_provider`` reads a tri-state where ``None`` derives the value
        from ``base_url``, so on the default deployment the config says ``None``
        and the wire says on. Read by ``NativeBrain`` for the session log's
        header, which records what the run actually did rather than what was
        configured.
        """
        return self._prompt_caching

    def scrub(self, text: str) -> str:
        """``text`` without this provider's key in it.

        An error body can quote the key a request carried, and the error
        message it becomes is logged and shown. A key under eight characters
        is a placeholder, and replacing it would mangle the message.
        """
        if len(self._api_key) < 8:
            return text
        return text.replace(self._api_key, "[redacted]")

    async def aclose(self) -> None:
        """Close the underlying HTTP client (frees pooled sockets/fds). NB-17."""
        await self._client.aclose()

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
        prompt_caching: bool | None = None,
        reasoning_replay: str | None = None,
        cache_affinity_key: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        body = self._build_chat_completion_request(
            system_prompt,
            messages,
            tools,
            model,
            max_tokens,
            reasoning_effort=reasoning_effort,
            render_tool_images=render_tool_images,
            prompt_caching=prompt_caching,
            reasoning_replay=reasoning_replay,
            cache_affinity_key=cache_affinity_key,
        )
        try:
            async with self._client.stream("POST", "/chat/completions", json=body) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    text = error_body.decode() if isinstance(error_body, bytes) else str(error_body)
                    yield StreamError(
                        message=AssistantMessage(
                            stop_reason="error",
                            error_message=f"HTTP {resp.status_code}: {self.scrub(text)[:500]}",
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
                    error_message=self.scrub(f"Connection error: {e}"),
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
        prompt_caching: bool | None = None,
        reasoning_replay: str | None = None,
        cache_affinity_key: str | None = None,
    ) -> dict:
        """Convert istota Message types to an OpenAI chat completions body.

        ``prompt_caching`` overrides the provider's own setting for this one
        request; ``None`` keeps it. A compaction summary passes ``False``.
        ``reasoning_replay`` is the same, and a refused replay is re-sent with
        ``"off"``. ``cache_affinity_key`` becomes ``prompt_cache_key`` only
        when this provider has the field on.
        """
        replay = self._reasoning_replay if reasoning_replay is None else reasoning_replay
        wire: list[dict] = []
        if system_prompt:
            wire.append({"role": "system", "content": system_prompt})
        for msg in messages:
            wire.append(self._message_to_wire(msg, replay))
            # A tool result carrying image content can't ride in the
            # ``role:"tool"`` message on most endpoints; Anthropic's compat layer
            # honors a follow-up ``role:"user"`` block instead. Inject it right
            # after the tool message so the image lands in order.
            extra = self._tool_image_followup(msg, render_tool_images)
            if extra is not None:
                wire.append(extra)

        tools_wire = [self._tool_to_wire(t) for t in tools] if tools else None

        caching = self._prompt_caching if prompt_caching is None else prompt_caching
        if caching:
            self._apply_cache_breakpoints(wire, tools_wire, self._prompt_cache_ttl)

        body: dict = {
            "model": model,
            "messages": wire,
            "stream": True,
            # Ask the server for a trailing usage chunk; without this OpenAI-
            # compatible endpoints (OpenAI, LM Studio, vLLM, OpenRouter) report
            # no token usage in streaming mode and cost telemetry stays zero.
            "stream_options": {"include_usage": True},
        }
        # OpenRouter returns the actual charged cost (markup included) in the
        # trailing usage chunk's ``cost`` field, but only when asked. Scoped to
        # OpenRouter because other endpoints may 400 on the unknown field.
        if _supports_cost_accounting(self._base_url):
            body["usage"] = {"include": True}
        # NB-12: OpenAI's own o-series / gpt-5 reasoning models 400 on
        # ``max_tokens`` and require ``max_completion_tokens``. Only the direct
        # OpenAI endpoint is affected — OpenRouter and others normalize — so the
        # gate is scoped to ``api.openai.com`` to avoid breaking a local server
        # that only knows ``max_tokens``.
        if _uses_max_completion_tokens(self._base_url, model):
            body["max_completion_tokens"] = max_tokens
        else:
            body["max_tokens"] = max_tokens
        if tools_wire is not None:
            body["tools"] = tools_wire

        effort = _REASONING_EFFORT_WIRE.get((reasoning_effort or "").lower())
        if effort:
            body["reasoning_effort"] = effort
        if self._prompt_cache_key and cache_affinity_key:
            body["prompt_cache_key"] = cache_affinity_key
        return body

    @staticmethod
    def _message_to_wire(msg: Message, reasoning_replay: str = "off") -> dict:
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
            wire.update(_reasoning_wire_fields(msg, reasoning_replay))
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
        wire: list[dict], tools_wire: list[dict] | None = None, ttl: str = "5m"
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

        ``ttl="1h"`` adds ``"ttl": "1h"`` to every breakpoint; ``5m`` is the
        endpoint default, so it writes the bare marker the wire always had.
        """
        cc = {"type": "ephemeral"}
        if ttl == "1h":
            cc["ttl"] = "1h"

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
            properties[p.name] = OpenAICompatibleProvider._param_to_schema(p)
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

    @staticmethod
    def _param_to_schema(p: ToolParameter) -> dict:
        """Render a ``ToolParameter`` as a JSON-Schema property.

        Recurses into ``properties`` (objects) and ``items`` (arrays). Strict
        providers — Google Gemini via OpenRouter in particular — reject an
        array that omits ``items`` (``...items: missing field``), and an
        object whose nested ``properties`` are absent, so both are emitted
        whenever they are declared.
        """
        schema: dict = {"type": p.type}
        if p.description:
            schema["description"] = p.description
        if p.enum:
            schema["enum"] = p.enum
        if p.properties:
            schema["properties"] = {
                sub.name: OpenAICompatibleProvider._param_to_schema(sub)
                for sub in p.properties.values()
            }
        if p.items is not None:
            schema["items"] = OpenAICompatibleProvider._param_to_schema(p.items)
        return schema

    async def _parse_sse_lines(
        self, lines: AsyncIterator[str], model: str = ""
    ) -> AsyncIterator[StreamEvent]:
        """Parse OpenAI SSE chunks into stream events, assembling a final message."""
        text_parts: list[str] = []
        # Extended-thinking deltas (Anthropic compat: ``reasoning_content``; some
        # endpoints: ``reasoning``). Captured for message fidelity but never
        # emitted as a TextDelta — it must not land in result_text or progress.
        thinking_parts: list[str] = []
        # Structured reasoning (`reasoning_details`), kept for replay, and the
        # plain field the text came from, if any.
        detail_items: list = []
        plain_field = ""
        # index -> {"id", "name", "args"}
        tool_acc: dict[int, dict] = {}
        # NB-16: some endpoints omit ``index``. Track id→slot and a running
        # counter so index-less parallel calls don't all merge into slot 0, and
        # an id-less argument continuation appends to the open call.
        id_to_index: dict[str, int] = {}
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
                        error_message=self.scrub(f"Provider error frame: {detail}"),
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

            # OpenRouter sends each chunk's reasoning twice, as `reasoning` and
            # inside `reasoning_details`, so the text comes from one of them:
            # the plain field when the chunk has one, else the details.
            details = delta.get("reasoning_details")
            if isinstance(details, list):
                detail_items.extend(details)
            reasoning = None
            for field_name in _PLAIN_REASONING_FIELDS:
                value = delta.get(field_name)
                if isinstance(value, str) and value:
                    reasoning = value
                    plain_field = plain_field or field_name
                    break
            if reasoning is None and isinstance(details, list):
                reasoning = "".join(
                    _readable_detail_text(item) for item in details if isinstance(item, dict)
                )
            if reasoning:
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
                idx = _tool_delta_index(tc, tool_acc, id_to_index)
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

        details = coalesce_reasoning_details(detail_items)
        source_field = plain_field or ("reasoning_details" if details else "")
        yield StreamDone(
            message=self._assemble_message(
                text_parts,
                thinking_parts,
                tool_acc,
                finish_reason,
                usage_raw,
                model,
                details=details or None,
                source_field=source_field,
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
        details: list[dict] | None = None,
        source_field: str = "",
    ) -> AssistantMessage:
        content: list = []
        thinking = "".join(thinking_parts)
        if thinking or details:
            # Thinking precedes the visible answer in the assembled message so a
            # future surface can render it in order. AssistantMessage.text
            # filters to TextContent, so this never leaks into the result.
            content.append(
                ThinkingContent(thinking=thinking, details=details, source_field=source_field)
            )
        joined = "".join(text_parts)
        if joined:
            content.append(TextContent(text=joined))
        for idx in sorted(tool_acc):
            slot = tool_acc[idx]
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                args = {}
            # NB-16: synthesize a stable id when the endpoint sent none — an
            # empty id would collide with any other id-less call in the pair
            # sanitizer (which keys on id) and some endpoints 400 on "".
            call_id = slot["id"] or f"call_{idx}"
            content.append(ToolCallContent(id=call_id, name=slot["name"], arguments=args))

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
                # OpenRouter's real charged cost (top-level ``cost``, USD, markup
                # included); None on every other endpoint → catalog pricing.
                cost_usd=_parse_reported_cost(usage_raw),
            )

        stop_reason = _FINISH_REASON_MAP.get(finish_reason or "", "end_turn")
        # ``finish_reason: "error"`` (OpenRouter's signal for an upstream
        # generation failure — e.g. Gemini ``MALFORMED_FUNCTION_CALL``, where
        # the model emitted a tool call with invalid JSON arguments) arrives in
        # a choices chunk with no ``error`` object, so there is no error text to
        # capture. Stamp a descriptive message so the downstream error result /
        # retry log / scheduler row explains the failure instead of reading as a
        # bare "Native brain execution error".
        error_message = ""
        if finish_reason == "error":
            error_message = (
                "Provider returned finish_reason='error' (upstream generation "
                "failure; e.g. MALFORMED_FUNCTION_CALL — the model emitted a "
                "tool call with invalid JSON arguments)"
            )
        return AssistantMessage(
            content=content,
            usage=usage,
            stop_reason=stop_reason,
            error_message=error_message or None,
            model=model,
        )
