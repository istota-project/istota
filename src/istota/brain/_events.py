"""Stream event types and the Claude Code stream-json parser.

The event types are the brain-agnostic surface that the executor and
progress callbacks consume. Different brain implementations are responsible
for adapting their underlying transport (subprocess stdout, SSE, etc.)
into these events.

The parser here is specific to Claude Code's --output-format stream-json;
other brains will produce the same events from different sources.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

# Tool-use rendering moved to istota.agent.events in Phase 0 of the agent-loop
# migration (brain-agnostic; reused by the native loop). Re-exported here so
# the stream_parser shim and existing imports keep working.
from ..agent.events import _TOOL_EMOJI, _describe_tool_use  # noqa: F401

logger = logging.getLogger("istota.brain.events")


@dataclass
class ToolUseEvent:
    tool_name: str
    description: str
    # The brain's own id for this tool call. ClaudeCodeBrain threads the
    # stream-json tool_use block id; NativeBrain threads the provider tool_call
    # id. An empty string means "this brain doesn't expose one" — it is NOT a
    # shared value to correlate against (every call would collide on ""), so
    # consumers that key chips by id must treat "" as "no correlation".
    tool_call_id: str = ""


@dataclass
class TextEvent:
    text: str


@dataclass
class TextDeltaEvent:
    """An *incremental* fragment of the assistant's answer text.

    Unlike ``TextEvent`` (a complete text block), this carries one streaming
    delta as it is produced. NativeBrain emits these per provider ``TextDelta``;
    the executor coalesces them into ``text_delta`` task events for stream
    surfaces only (push surfaces never see them). ClaudeCodeBrain does not emit
    these — the executor routes its block-level ``TextEvent``s through the same
    coalescer instead (coarse streaming)."""
    text: str


@dataclass
class ResultEvent:
    success: bool
    text: str


@dataclass
class ContextManagementEvent:
    """Marker: context management fired, conversation was compacted."""
    pass


@dataclass
class ToolEndEvent:
    """A tool call finished. NativeBrain only — ClaudeCodeBrain's stream-json
    has no tool-completion frame, so it never emits this."""
    tool_name: str
    tool_call_id: str
    success: bool       # from AgentEvent.is_error (inverted)
    duration_ms: int    # measured in the agent loop


@dataclass
class ToolProgressEvent:
    """Incremental output during a single tool call. NativeBrain only."""
    tool_name: str
    tool_call_id: str
    text: str


@dataclass
class ThinkingEvent:
    """A complete extended-thinking / reasoning block. ClaudeCodeBrain only —
    the ``claude`` CLI emits ``thinking`` content blocks as whole snapshots (one
    per block, deduped by block id), not token deltas. The executor maps this to
    a ``thinking`` task event on stream surfaces only (web/repl); push surfaces
    drop it."""
    text: str


@dataclass
class ThinkingDeltaEvent:
    """An *incremental* fragment of the assistant's extended thinking. NativeBrain
    only — the OpenAI-compat provider streams ``reasoning_content`` / ``reasoning``
    deltas, which the loop forwards token-by-token. The executor coalesces these
    into ``thinking`` task events on stream surfaces only."""
    thinking: str


StreamEvent = (
    ToolUseEvent
    | TextEvent
    | TextDeltaEvent
    | ResultEvent
    | ContextManagementEvent
    | ToolEndEvent
    | ToolProgressEvent
    | ThinkingEvent
    | ThinkingDeltaEvent
)


def make_stream_parser() -> Callable[[str], StreamEvent | None]:
    """Create a stateful stream parser that deduplicates assistant events.

    Claude Code's stream-json reuses the same message ID across multiple
    emissions within a turn (each tool call gets its own line with the same
    message.id). Dedup therefore tracks individual content block IDs
    (tool_use id) and text content hashes to avoid duplicates from
    context-management replays (ISSUE-024) while preserving distinct
    tool calls within the same message.
    """
    seen_block_ids: set[str] = set()

    def parse(line: str) -> StreamEvent | None:
        return parse_stream_line(line, _seen=seen_block_ids)

    return parse


def parse_stream_line(
    line: str, *, _seen: set[str] | None = None,
) -> StreamEvent | None:
    """
    Parse a single line of stream-json output into a StreamEvent.

    Returns None for lines that don't map to a user-visible event
    (system init, user tool results, etc.).

    Pass ``_seen`` (a set of block/content IDs) for deduplication across
    calls.  Use ``make_stream_parser()`` for a convenient stateful wrapper.
    """
    line = line.strip()
    if not line:
        return None

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.debug("Skipping non-JSON stream line: %s", line[:100])
        return None

    event_type = data.get("type")

    if event_type == "result":
        success = data.get("subtype") == "success"
        text = data.get("result", "")
        return ResultEvent(success=success, text=text)

    if event_type == "assistant":
        message = data.get("message", {})

        # Context-management events re-emit the most recent assistant
        # response with a new message ID (ISSUE-024).  Emit a marker so
        # downstream code can segment by CM boundaries (ISSUE-026).
        if message.get("context_management") is not None:
            return ContextManagementEvent()

        content_blocks = message.get("content", [])

        tool_events = []
        text_parts = []
        thinking_parts = []

        for block in content_blocks:
            block_type = block.get("type")

            if block_type == "tool_use":
                # Dedup by tool_use block ID (unique per invocation)
                block_id = block.get("id", "")
                if _seen is not None and block_id:
                    if block_id in _seen:
                        continue
                    _seen.add(block_id)
                name = block.get("name", "")
                input_data = block.get("input", {})
                desc = _describe_tool_use(name, input_data)
                tool_events.append(
                    ToolUseEvent(tool_name=name, description=desc, tool_call_id=block_id)
                )

            elif block_type == "text":
                text = block.get("text", "").strip()
                if not text:
                    continue
                # Dedup text blocks by message_id + content hash
                if _seen is not None:
                    msg_id = message.get("id", "")
                    text_key = f"text:{msg_id}:{hash(text)}"
                    if text_key in _seen:
                        continue
                    _seen.add(text_key)
                text_parts.append(text)

            elif block_type == "thinking":
                # Extended-thinking block. Arrives as a whole snapshot per block;
                # dedup by message_id + content hash to drop CM replays, mirroring
                # the text-dedup above (ISSUE-024).
                think = block.get("thinking", "").strip()
                if not think:
                    continue
                if _seen is not None:
                    msg_id = message.get("id", "")
                    think_key = f"think:{msg_id}:{hash(think)}"
                    if think_key in _seen:
                        continue
                    _seen.add(think_key)
                thinking_parts.append(think)

        # Single event per line, in priority order: a tool call is the most
        # informative, then answer/narration text, then (only when a line carries
        # thinking alone) the reasoning. A rare combined thinking+text line still
        # prefers text — acceptable for v1, since the CLI emits thinking on its
        # own block/line in practice.
        if tool_events:
            return tool_events[0]

        if text_parts:
            return TextEvent(text="\n".join(text_parts))

        if thinking_parts:
            return ThinkingEvent(text="\n".join(thinking_parts))

    return None
