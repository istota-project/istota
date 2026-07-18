"""Native-brain tool implementations.

The six core tools (Read, Write, Edit, Grep, Glob, Bash) the native agent loop
dispatches against, built as ``AgentTool`` instances. They mirror Claude Code's
tool schemas closely enough that prompts written for one work on the other.

Sandbox model: the agent loop runs in istota's main process, *outside* any
sandbox. The two isolation mechanisms are asymmetric, because only one tool
spawns a subprocess:

- ``Bash`` runs its subprocess through ``ToolEnv.sandbox_wrap`` (bwrap on Linux,
  a no-op on macOS / when the sandbox is disabled) — the same per-execution
  bwrap the claude_code path uses.
- ``Read`` / ``Write`` / ``Edit`` / ``Grep`` / ``Glob`` do their file I/O in the
  daemon process (``asyncio.to_thread``), so bwrap can't confine them. Instead
  they honor ``ToolEnv.read_roots`` / ``write_roots`` — a symlink-resolved path
  allowlist the executor populates with the same user-data roots bwrap would
  bind for the claude_code path (NB-1). When the roots are ``None`` (dev /
  bwrap unavailable) the file tools are unconfined, matching the claude_code
  path's own posture where bwrap can't run.

Filesystem-isolation correctness under bwrap is validated only on Linux / in
Docker; the path-allowlist confinement is exercised in unit tests everywhere.

``build_default_tools(env)`` returns all six bound to one ``ToolEnv``; that's
what ``NativeBrain`` calls in Phase 3.
"""

from .bash import make_bash_tool
from .env import ToolEnv, ToolPathError
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
    "ToolPathError",
    "build_default_tools",
    "make_read_tool",
    "make_write_tool",
    "make_edit_tool",
    "make_grep_tool",
    "make_glob_tool",
    "make_bash_tool",
]
