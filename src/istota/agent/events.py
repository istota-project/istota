"""Agent lifecycle events and tool-use rendering.

Phase 0 of the agent-loop migration extracts the brain-agnostic tool-use
description renderer here. It renders a human-readable, emoji-prefixed summary
of a tool call (``📄 Reading TODO.txt``) for progress surfaces — Talk message
edits, the log channel, SSE. The Claude Code stream parser imports it; the
native agent loop will reuse it for the same progress strings.

Phases 2+ expand this module with the full ``AgentEvent`` lifecycle dataclass
and ``AgentEventSink`` callback type that the native loop emits.
"""

from pathlib import Path

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
