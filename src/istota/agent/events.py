"""Agent lifecycle events and tool-use rendering.

Phase 0 of the agent-loop migration extracts the brain-agnostic tool-use
description renderer here. It renders a human-readable, emoji-prefixed summary
of a tool call (``📄 Reading TODO.txt``) for progress surfaces — Talk message
edits, the log channel, SSE. The Claude Code stream parser imports it; the
native agent loop will reuse it for the same progress strings.

Phase 2 expands this module with the full ``AgentEvent`` lifecycle dataclass
and ``AgentEventSink`` callback type that the native loop emits.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


@dataclass
class AgentEvent:
    """Lifecycle events emitted by the agent loop.

    Prior art: Pi's AgentEvent (agent/src/types.ts:350).

    Event types:
    - ``agent_start`` / ``agent_end`` — bookend the entire agent run.
      ``agent_end`` carries ``messages`` (all new messages) and ``stop_reason``
      (the named stop condition that fired, or "" for a natural stop).
    - ``turn_start`` / ``turn_end`` — bookend one LLM call + tool-execution
      cycle. ``turn_end`` carries the assistant ``message`` and ``tool_results``.
    - ``message_start`` / ``message_update`` / ``message_end`` — individual
      message lifecycle. ``message_update`` carries an ``assistant_event``
      (a provider ``StreamEvent``) during streaming.
    - ``tool_execution_start`` — dispatch begins (``tool_call_id``, ``tool_name``,
      ``args``).
    - ``tool_execution_update`` — partial output during execution
      (``update_text``). Prior art: Pi's onUpdate callback. Bridges a tool's
      incremental output to the event stream so subscribers see progress.
    - ``tool_execution_end`` — dispatch complete (``result``, ``is_error``).
    """

    type: str
    message: Any = None
    messages: list | None = None
    tool_results: list | None = None
    tool_call_id: str = ""
    tool_name: str = ""
    args: dict = field(default_factory=dict)
    result: Any = None
    is_error: bool = False
    update_text: str = ""  # for tool_execution_update events
    assistant_event: Any = None  # for streaming message_update events
    stop_reason: str = ""  # for agent_end: the named stop condition that fired


# The loop pushes every lifecycle event through this sink. The application
# layer subscribes to translate events into Talk edits, SSE, log-channel posts.
AgentEventSink = Callable[[AgentEvent], Awaitable[None]]
