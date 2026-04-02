"""Parse Claude Code --output-format stream-json events."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("istota.stream_parser")


@dataclass
class ToolUseEvent:
    tool_name: str
    description: str


@dataclass
class TextEvent:
    text: str


@dataclass
class ResultEvent:
    success: bool
    text: str


@dataclass
class ContextManagementEvent:
    """Marker: context management fired, conversation was compacted."""
    pass


StreamEvent = ToolUseEvent | TextEvent | ResultEvent | ContextManagementEvent


_TOOL_EMOJI = {
    "Bash": "⚙️",
    "Read": "📄",
    "Edit": "✏️",
    "MultiEdit": "✏️",
    "Write": "📝",
    "Grep": "🔍",
    "Glob": "🔍",
    "Task": "🐙",
    "WebFetch": "🌐",
    "WebSearch": "🌐",
}


def _describe_tool_use(name: str, input_data: dict) -> str:
    """Extract a human-readable description from a tool_use block."""
    emoji = _TOOL_EMOJI.get(name, "🔧")

    if name == "Bash":
        desc = input_data.get("description")
        if desc:
            return f"{emoji} {desc}"
        cmd = input_data.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"{emoji} {cmd}" if cmd else f"{emoji} Running command"

    if name == "Read":
        path = input_data.get("file_path", "")
        filename = Path(path).name if path else "file"
        return f"{emoji} Reading {filename}"

    if name in ("Edit", "MultiEdit"):
        path = input_data.get("file_path", "")
        filename = Path(path).name if path else "file"
        return f"{emoji} Editing {filename}"

    if name == "Write":
        path = input_data.get("file_path", "")
        filename = Path(path).name if path else "file"
        return f"{emoji} Writing {filename}"

    if name == "Grep":
        pattern = input_data.get("pattern", "")
        return f"{emoji} Searching for '{pattern}'"

    if name == "Glob":
        pattern = input_data.get("pattern", "")
        return f"{emoji} Searching for '{pattern}'"

    if name == "Task":
        desc = input_data.get("description", "")
        return f"{emoji} Delegating: {desc}" if desc else f"{emoji} Using {name}"

    return f"{emoji} Using {name}"


def make_stream_parser() -> "Callable[[str], StreamEvent | None]":
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
                tool_events.append(ToolUseEvent(tool_name=name, description=desc))

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

        # Prefer tool events (more informative for progress)
        if tool_events:
            return tool_events[0]

        if text_parts:
            return TextEvent(text="\n".join(text_parts))

    return None
