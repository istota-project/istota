"""Native-brain tool implementations.

The six core tools (Read, Write, Edit, Grep, Glob, Bash) the native agent loop
dispatches against, built as ``AgentTool`` instances. They mirror Claude Code's
tool schemas closely enough that prompts written for one work on the other.

Sandbox model: these six run in ``istota.tool_server``, one process per task
attempt, wrapped once by ``build_bwrap_cmd(..., profile=NATIVE)`` and placed in
the task cgroup before it can fork. ``NativeBrain`` holds proxies onto it
(``session/tools/remote.py``); the agent loop, the provider client and
``WebFetch`` stay in the daemon.

**The asymmetry this replaced is worth writing down, because the shape of these
modules still carries it.** ``Bash`` used to rebuild a bwrap namespace per call
while ``Read`` / ``Write`` / ``Edit`` / ``Grep`` / ``Glob`` did their file I/O
on daemon worker threads, confined only by ``ToolEnv``'s symlink-resolved root
allowlist. That was a second filesystem policy written in Python: every tool
had to remember to call it, every bwrap bind change had to be copied across,
the check and the open were separate syscalls so an ancestor could be swapped
between them, and ``Grep``/``Glob`` walked the daemon's own filesystem view and
filtered afterwards (NB-1, ISSUE-389).

The roots are still passed and still enforced — the server builds its
``ToolEnv`` from them — but they are the **error-message layer** now rather
than the boundary: "outside the allowed workspace" beats ENOENT, and a path
outside the namespace is not there to reach in the first place. Where bwrap is
unavailable (macOS, the standalone install, a Docker stack without the two
container settings) ``build_bwrap_cmd`` returns the command unchanged, the
server is an ordinary child, and the roots are the confinement exactly as
before — which is also what lets the default suite exercise this seam on a
developer machine.

``build_default_tools(env)`` returns all six bound to one ``ToolEnv``, plus
``WebFetch`` when ``env.web_fetch`` is set. Inside the server it never is, so
the server is exactly six; the daemon builds the seventh itself.
"""

from .bash import make_bash_tool
from .env import ToolEnv, ToolPathError, WebFetchPolicy
from .files import (
    make_edit_tool,
    make_glob_tool,
    make_grep_tool,
    make_read_tool,
    make_write_tool,
)
from .web_fetch import make_web_fetch_tool
# Last, and after `.bash` / `.files`: it imports schema constants from both.
from .remote import (
    RemoteToolServer,
    ToolServerError,
    build_remote_tools,
    hello_payload,
    start_tool_server,
)
from istota.agent.tools import AgentTool


def build_default_tools(env: ToolEnv) -> list[AgentTool]:
    """The core tools bound to one ``ToolEnv``.

    Execution modes: Read / Grep / Glob / WebFetch are read-only and
    parallel-safe; Write / Edit / Bash mutate state and run sequentially.

    ``WebFetch`` is appended only when ``env.web_fetch`` is set and enabled; when
    omitted the model never sees it (and ``NativeBrain._build_tools``'
    allowed-tools filter is a no-op on an absent tool).
    """
    tools = [
        make_read_tool(env),
        make_write_tool(env),
        make_edit_tool(env),
        make_grep_tool(env),
        make_glob_tool(env),
        make_bash_tool(env),
    ]
    if env.web_fetch is not None and env.web_fetch.enabled:
        tools.append(make_web_fetch_tool(env))
    return tools


__all__ = [
    "RemoteToolServer",
    "ToolEnv",
    "ToolPathError",
    "ToolServerError",
    "WebFetchPolicy",
    "build_default_tools",
    "build_remote_tools",
    "hello_payload",
    "start_tool_server",
    "make_read_tool",
    "make_write_tool",
    "make_edit_tool",
    "make_grep_tool",
    "make_glob_tool",
    "make_bash_tool",
    "make_web_fetch_tool",
]
