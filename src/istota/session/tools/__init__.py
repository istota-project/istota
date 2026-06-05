"""Native-brain tool implementations.

The six core tools (Read, Write, Edit, Grep, Glob, Bash) the native agent loop
dispatches against, built as ``AgentTool`` instances. They mirror Claude Code's
tool schemas closely enough that prompts written for one work on the other.

Sandbox model (per the migration spec): the agent loop runs in istota's main
process, *outside* any sandbox. Each tool sandboxes its own subprocess work
per-execution via ``ToolEnv.sandbox_wrap`` — more granular than wrapping the
whole task, and the loop itself never runs user-controlled code. On macOS (no
bubblewrap) ``sandbox_wrap`` is absent and the wrap is a no-op; isolation
correctness is validated only on Linux / in Docker.

``build_default_tools(env)`` returns all six bound to one ``ToolEnv``; that's
what ``NativeBrain`` calls in Phase 3.
"""

from .bash import make_bash_tool
from .env import ToolEnv
from .files import (
    make_edit_tool,
    make_glob_tool,
    make_grep_tool,
    make_read_tool,
    make_write_tool,
)
from istota.agent.tools import AgentTool


def build_default_tools(env: ToolEnv) -> list[AgentTool]:
    """All six core tools bound to one ``ToolEnv``.

    Execution modes: Read / Grep / Glob are read-only and parallel-safe;
    Write / Edit / Bash mutate state and run sequentially.
    """
    return [
        make_read_tool(env),
        make_write_tool(env),
        make_edit_tool(env),
        make_grep_tool(env),
        make_glob_tool(env),
        make_bash_tool(env),
    ]


__all__ = [
    "ToolEnv",
    "build_default_tools",
    "make_read_tool",
    "make_write_tool",
    "make_edit_tool",
    "make_grep_tool",
    "make_glob_tool",
    "make_bash_tool",
]
