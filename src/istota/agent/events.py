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
import re
import shlex
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


# How a `Read` call renders in the execution trace, named rather than spelled
# out twice. The executor's post-run image audit reads it back: a Claude Code
# task's vision claim rests on the model complying with a prompt directive, so
# the trace is checked for one `Read` per prepared image. Producer and reader in
# one file, because a copied literal is how the audit would silently start
# reporting every image unread.
READ_DESCRIPTION_PREFIX = f"{_TOOL_EMOJI['Read']} Reading "
PRIVATE_RELAY_TOOL_DESCRIPTION = "Private relay request"


def _private_relay_tool(name: str, input_data: dict) -> bool:
    """Keep relay arguments and model labels out of progress and execution traces."""
    if name != "Bash" or not isinstance(input_data, dict):
        return False
    command = str(input_data.get("command", ""))
    try:
        # Shell quoting can split a word into adjacent quoted fragments.
        command = " ".join(shlex.split(command))
    except ValueError:
        pass
    # `whatsapp ask` is retired, but a task holding the old instructions can
    # still type it, and the argv would carry the question all the same. A
    # `room whisper` is for the principal alone, and a shared room's progress
    # trace is read by the whole room; a `room post` is not yet approved.
    return bool(re.search(r"\b(?:(?:relay|whatsapp)\s+ask|room\s+(?:whisper|post))\b", command))


# Outside quotes the shell would treat any of these as more than one plain
# word: a separator, a pipe, a redirect, a substitution, an expansion or a glob.
_UNQUOTED_SHELL_SYNTAX = frozenset(";&|<>()$`*?[]{}~")


def _lone_relay_ask(name: str, input_data: dict) -> bool:
    """Whether a tool call is exactly one `istota-skill relay ask` and nothing else.

    The clean-turn rule (ISSUE-565) authorizes on this flag, so it is strict
    where `_private_relay_tool` is loose: that one hides output, and matching
    too much there costs nothing. Here a false positive lets a question shaped
    by something the task read skip approval. One simple command, argv[0]
    literally `istota-skill`, no separators, pipes, redirects, substitutions,
    expansions, globs, newlines or assignment prefixes. Single-quoted text is
    literal and may hold anything but a newline; double-quoted text may not
    hold `$` or a backtick, which the shell would still expand there.
    """
    if name != "Bash" or not isinstance(input_data, dict):
        return False
    command = input_data.get("command")
    if not isinstance(command, str) or "\n" in command or "\r" in command:
        return False
    quote = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = None
        elif quote == '"':
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = None
            elif ch in "$`":
                return False
        elif ch == "\\":
            i += 1
        elif ch in "'\"":
            quote = ch
        elif ch in _UNQUOTED_SHELL_SYNTAX:
            return False
        i += 1
    if quote is not None:
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    # `room post` takes the same clean-turn rule (multiplayer D4).
    return argv[:3] in (["istota-skill", "relay", "ask"], ["istota-skill", "room", "post"])


def _describe_tool_use(name: str, input_data: dict) -> str:
    """Extract a human-readable description from a tool_use block."""
    emoji = _TOOL_EMOJI.get(name, "🔧")

    if _private_relay_tool(name, input_data):
        return PRIVATE_RELAY_TOOL_DESCRIPTION
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
        return f"{READ_DESCRIPTION_PREFIX}{filename}"

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


def _tool_invocation(name: str, input_data: dict) -> str | None:
    """The literal, untruncated command a tool call ran, or None.

    Distinct from ``_describe_tool_use`` (a human-readable label — for Bash it
    returns the model's *description* paraphrase, not the command). This returns
    the verbatim command string so the sleep cycle can distil playbooks that
    quote the actual verified invocation instead of a reconstruction
    (ISSUE-174 fix 1). Only Bash carries a meaningful command; other tools
    return None and callers fall back to the description label.
    """
    if not isinstance(input_data, dict) or _private_relay_tool(name, input_data):
        return None
    if name == "Bash":
        cmd = str(input_data.get("command", "")).strip()
        return cmd or None
    return None


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
    duration_ms: int = 0  # for tool_execution_end: per-tool wall time (loop-measured)


# The loop pushes every lifecycle event through this sink. The application
# layer subscribes to translate events into Talk edits, SSE, log-channel posts.
AgentEventSink = Callable[[AgentEvent], Awaitable[None]]
