"""Before/after tool-call hooks.

Hooks let the application layer veto a tool call (``before_tool_call`` →
``block``) or rewrite its result (``after_tool_call`` → partial override).
Both are partial: omitted fields keep their original values.

Prior art: Pi's BeforeToolCallContext / AfterToolCallResult (agent/src/types.ts).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from istota.llm.types import ImageContent, TextContent

if TYPE_CHECKING:
    from istota.llm.types import AssistantMessage, ToolCallContent

    from .tools import ToolResult
    from .types import AgentContext


@dataclass
class BeforeToolCallContext:
    """Prior art: Pi's BeforeToolCallContext (agent/src/types.ts:76)."""

    assistant_message: "AssistantMessage"
    tool_call: "ToolCallContent"
    args: dict
    context: "AgentContext"


@dataclass
class BeforeToolCallResult:
    block: bool = False
    reason: str = ""


@dataclass
class AfterToolCallContext:
    """Prior art: Pi's AfterToolCallContext (agent/src/types.ts:88)."""

    assistant_message: "AssistantMessage"
    tool_call: "ToolCallContent"
    args: dict
    result: "ToolResult"
    is_error: bool
    context: "AgentContext"


@dataclass
class AfterToolCallResult:
    """Partial override — omitted fields keep their original values.

    Prior art: Pi's AfterToolCallResult (agent/src/types.ts:52).
    """

    content: list[TextContent | ImageContent] | None = None
    details: Any = None
    is_error: bool | None = None
    terminate: bool | None = None


# Hooks may be sync or async; the loop awaits the result either way.
BeforeToolCallHook = Callable[
    [BeforeToolCallContext], "BeforeToolCallResult | None | Awaitable[BeforeToolCallResult | None]"
]
AfterToolCallHook = Callable[
    [AfterToolCallContext], "AfterToolCallResult | None | Awaitable[AfterToolCallResult | None]"
]
