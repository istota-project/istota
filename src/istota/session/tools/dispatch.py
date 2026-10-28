"""The per-call policy for tools a model calls over MCP.

``ToolDispatcher`` stands between whatever receives a tool call and the
``AgentTool`` that runs it, and applies, in order: the subset (a name not
offered never reaches ``execute``), argument preparation (the native loop's
own steps, ``agent.coercion.prepare_tool_arguments``), a readers-writer lock by
``execution_mode``, ``execute``, then the text cap.

The tool server builds one over its real tools for every call it runs, from
either brain (#862). This module imports only from
``istota.agent`` and ``istota.llm``'s leaves, never the brain or the daemon:
``tests/test_tool_dispatch.py`` holds that.

**Why a lock and not native's batch rule.** The native loop sees a whole batch
and serialises it when any call mutates. The CLI sends calls one at a time as
each block finishes streaming, so only a per-call rule can apply the same
policy: ``sequential`` tools (Write, Edit, Bash) run alone, ``parallel`` ones
(Read, Grep, Glob) run together. Serialising every mutation against
everything also covers native's path-overlap check, since an overlap only
matters when one side mutates.

**Why the text cap.** The CLI replaces an MCP result over 50,000 characters
with a preview pointing at a file in its own config directory, which the tool
server cannot read (Stage 1). Cutting below that keeps the model on a result
it can recover from, with a line saying how.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Iterable, Mapping

from istota.agent.coercion import prepare_tool_arguments
from istota.agent.tools import AgentTool, ToolResult, ToolUpdateCallback
from istota.llm.tool_json_schema import tool_parameters
from istota.llm.types import TextContent

logger = logging.getLogger("istota.session.tools.dispatch")

HARNESS_TEXT_CAP_CHARS = 45_000

# Parameters the MCP tool list does not show. Bash's `exclude_from_context`
# answers with a stub saying the output was shown to the user, which is false
# where nothing forwards `update` frames.
_HIDDEN_FROM_MCP: dict[str, frozenset[str]] = {
    "Bash": frozenset({"exclude_from_context"}),
}

_RECOVERY_HINTS: dict[str, str] = {
    "Read": "Read the file again with offset= past the last line shown, and a limit=, to see the rest.",
    "Bash": (
        "If the output named a file holding the full output, Read that file with "
        "offset= and limit=; otherwise run the command again with its output narrowed."
    ),
    "Grep": "Narrow the pattern or the path to see the rest.",
    "Glob": "Narrow the pattern or the path to see the rest.",
}


def _error(text: str) -> ToolResult:
    return ToolResult(content=[TextContent(text=text)], is_error=True)


def recovery_line(tool_name: str, cap: int, total: int) -> str:
    """The line appended to a cut result, naming how to get the rest."""
    hint = _RECOVERY_HINTS.get(tool_name, "")
    line = f"[Output cut at {cap:,} of {total:,} characters."
    if hint:
        line += " " + hint
    return line + "]"


def cap_text(result: ToolResult, cap: int, tool_name: str) -> ToolResult:
    """``result`` with its text cut to ``cap`` characters in all, plus the
    recovery line. Images are kept as they are: the CLI re-encodes them and
    they trip none of its limits (Stage 1)."""
    total = 0
    for block in result.content:
        if isinstance(block, TextContent):
            total += len(block.text)
    if total <= cap:
        return result

    budget = cap
    content: list = []
    for block in result.content:
        if not isinstance(block, TextContent):
            content.append(block)
            continue
        if budget <= 0:
            continue
        if len(block.text) > budget:
            content.append(TextContent(text=block.text[:budget]))
            budget = 0
        else:
            content.append(block)
            budget -= len(block.text)
    content.append(TextContent(text=recovery_line(tool_name, cap, total)))
    return ToolResult(
        content=content,
        details=result.details,
        terminate=result.terminate,
        is_error=result.is_error,
    )


class _ToolLock:
    """An async readers-writer lock, granted in arrival order.

    A waiting exclusive holder blocks shared requests that arrive after it, so
    a stream of Reads cannot starve a Write. Release is synchronous, so a call
    cancelled while running or while waiting can never leave the lock held.
    """

    def __init__(self) -> None:
        self._readers = 0
        self._writer = False
        self._queue: deque[tuple[bool, asyncio.Future]] = deque()

    def _can_grant(self, exclusive: bool) -> bool:
        if self._writer:
            return False
        if exclusive:
            return self._readers == 0
        return True

    def _take(self, exclusive: bool) -> None:
        if exclusive:
            self._writer = True
        else:
            self._readers += 1

    def _wake(self) -> None:
        while self._queue:
            exclusive, fut = self._queue[0]
            if fut.done():
                self._queue.popleft()
                continue
            if not self._can_grant(exclusive):
                return
            self._queue.popleft()
            self._take(exclusive)
            fut.set_result(None)
            if exclusive:
                return

    async def acquire(self, exclusive: bool) -> None:
        if not self._queue and self._can_grant(exclusive):
            self._take(exclusive)
            return
        fut = asyncio.get_running_loop().create_future()
        self._queue.append((exclusive, fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # Granted in the same tick the cancel arrived: give it back.
                self.release(exclusive)
            else:
                with contextlib.suppress(ValueError):
                    self._queue.remove((exclusive, fut))
                self._wake()
            raise

    def release(self, exclusive: bool) -> None:
        if exclusive:
            self._writer = False
        else:
            self._readers -= 1
        self._wake()


class ToolDispatcher:
    """Runs one tool call under the harness's per-call policy."""

    def __init__(
        self,
        tools: Mapping[str, AgentTool],
        *,
        offered: Iterable[str],
        text_cap: int | None,
    ) -> None:
        self._tools = dict(tools)
        names: list[str] = []
        for name in offered:
            if name in self._tools and name not in names:
                names.append(name)
        self.offered: tuple[str, ...] = tuple(names)
        self._text_cap = text_cap
        self._lock = _ToolLock()

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Every tool bound, offered or not."""
        return tuple(self._tools)

    def mcp_tool_list(self) -> list[dict]:
        """The offered tools as ``{name, description, input_schema, read_only}``,
        in offered order: what ``sandbox.mcp_stdio`` lists as ``tools/list``.
        ``read_only`` is ``parallel`` execution, the one fact both sides read."""
        entries: list[dict] = []
        for name in self.offered:
            tool = self._tools[name]
            schema = tool_parameters(tool.schema)
            hidden = _HIDDEN_FROM_MCP.get(name, frozenset())
            if hidden:
                schema["properties"] = {
                    key: value for key, value in schema["properties"].items() if key not in hidden
                }
                schema["required"] = [key for key in schema["required"] if key not in hidden]
            entries.append({
                "name": name,
                "description": tool.schema.description,
                "input_schema": schema,
                "read_only": tool.execution_mode == "parallel",
            })
        return entries

    async def call(
        self,
        call_id: str,
        name: str,
        raw_args: dict,
        on_update: ToolUpdateCallback | None,
        abort: asyncio.Event | None,
    ) -> ToolResult:
        """Run ``name``; never raises except for cancellation."""
        if name not in self.offered:
            return _error(f"Unknown tool: {name}")
        if not isinstance(raw_args, dict):
            return _error(f"{name}: arguments must be an object")
        tool = self._tools[name]

        args, error = prepare_tool_arguments(tool, raw_args)
        if error is not None:
            return _error(error)

        exclusive = tool.execution_mode != "parallel"
        await self._lock.acquire(exclusive)
        try:
            try:
                result = await tool.execute(call_id, args, on_update, abort)
            except Exception as exc:  # noqa: BLE001 — a tool failure is a result
                logger.warning("tool_execute_error tool=%s id=%s: %s", name, call_id, exc)
                return _error(f"Tool error: {exc}")
        finally:
            self._lock.release(exclusive)

        if self._text_cap is not None:
            result = cap_text(result, self._text_cap, name)
        return result
