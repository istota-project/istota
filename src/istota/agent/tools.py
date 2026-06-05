"""Tool protocol for the agent runtime.

An ``AgentTool`` binds a JSON ``ToolSchema`` to an async ``execute`` function.
The loop dispatches tool calls against these. Per-tool ``execution_mode``
decides whether a tool may run concurrently with others in the same batch
(Pi's design — replaces a global allowlist); ``prepare_arguments`` is an
optional per-tool coercion hook that overrides the generic coercion layer.

Prior art: Pi's AgentTool / AgentToolResult (agent/src/types.ts).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from istota.llm.types import ImageContent, TextContent, ToolSchema

ToolExecutionMode = Literal["sequential", "parallel"]


@dataclass
class ToolResult:
    """Result of a single tool execution.

    Prior art: Pi's AgentToolResult (agent/src/types.ts:292).
    """

    content: list[TextContent | ImageContent] = field(default_factory=list)
    details: Any = None
    terminate: bool = False  # hint to stop after this batch


# Streaming partial results during tool execution. The tool calls this with
# intermediate output as it becomes available; the loop turns each invocation
# into a ``tool_execution_update`` event so subscribers see incremental
# progress instead of silence. Prior art: Pi's AgentToolUpdateCallback.
ToolUpdateCallback = Callable[[str], Awaitable[None]]

# The signature every tool's execute function satisfies:
#   (tool_call_id, args, on_update | None, abort | None) -> Awaitable[ToolResult]
ToolExecuteFn = Callable[
    [str, dict, "ToolUpdateCallback | None", "asyncio.Event | None"],
    Awaitable[ToolResult],
]


@dataclass
class AgentTool:
    """Tool definition for the agent runtime.

    Prior art: Pi's AgentTool (agent/src/types.ts:308). Per-tool
    ``execution_mode`` replaces a global allowlist; ``prepare_arguments``
    overrides the generic coercion layer; the ``on_update`` arg to ``execute``
    streams partial results mid-run.
    """

    schema: ToolSchema
    execute: ToolExecuteFn
    execution_mode: ToolExecutionMode = "sequential"
    prepare_arguments: Callable[[Any], dict] | None = None
