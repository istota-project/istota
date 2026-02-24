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


StreamEvent = ToolUseEvent | TextEvent | ResultEvent


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


def parse_stream_line(line: str) -> StreamEvent | None:
    """
    Parse a single line of stream-json output into a StreamEvent.

    Returns None for lines that don't map to a user-visible event
    (system init, user tool results, etc.).
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
        content_blocks = message.get("content", [])

        tool_events = []
        text_parts = []

        for block in content_blocks:
            block_type = block.get("type")
            if block_type == "tool_use":
                name = block.get("name", "")
                input_data = block.get("input", {})
                desc = _describe_tool_use(name, input_data)
                tool_events.append(ToolUseEvent(tool_name=name, description=desc))
            elif block_type == "text":
                text = block.get("text", "").strip()
                if text:
                    text_parts.append(text)

        # Prefer tool events (more informative for progress)
        if tool_events:
            return tool_events[0]

        if text_parts:
            return TextEvent(text="\n".join(text_parts))

    return None
