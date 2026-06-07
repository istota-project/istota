"""Core types for the generic agent runtime.

The loop operates on ``AgentMessage`` — the union of LLM messages and any
app-level ``CustomMessage`` it carries but doesn't understand (compaction
summaries, bash-execution records). Translation to the provider's wire format
happens once, at the ``convert_to_llm`` boundary.

Prior art: Pi's AgentMessage / AgentLoopConfig / AgentContext (agent/src/types.ts).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from istota.llm.types import Message

from .tools import AgentTool, ToolExecutionMode

if TYPE_CHECKING:
    from istota.llm.provider import LLMProvider

    from .hooks import AfterToolCallHook, BeforeToolCallHook


@runtime_checkable
class CustomMessage(Protocol):
    """Any app-level message the loop carries but doesn't understand.

    Prior art: Pi's CustomAgentMessages interface + declaration merging
    (agent/src/types.ts:247). Any dataclass with ``role`` and ``timestamp``
    can ride along in the context; ``convert_to_llm`` decides how (or whether)
    to render it for the provider.
    """

    role: str
    timestamp: float


# LLM messages plus anything satisfying CustomMessage.
AgentMessage = Message | CustomMessage

# The single translation point from app-level messages to provider wire format.
ConvertToLlm = Callable[[list[AgentMessage]], list[Message]]
# Optional in-place reshaping of the context (filtering, reordering) applied
# before conversion.
TransformContext = Callable[[list[AgentMessage]], list[AgentMessage]]


@dataclass
class AgentContext:
    system_prompt: str
    messages: list[AgentMessage]
    tools: list[AgentTool] | None = None


QueueMode = Literal["all", "one_at_a_time"]
"""How steering / follow-up queues are drained.

- ``"all"`` — drain every pending message before the next LLM call.
- ``"one_at_a_time"`` — inject one message per turn, leaving the rest for
  subsequent turns. Prevents queue flooding (multi-user, external triggers)
  from stomping the agent's flow.
"""


@dataclass
class PrepareNextTurnResult:
    """Return value from the ``prepare_next_turn`` hook.

    Omitted (``None``) fields keep their current values. Lets the session layer
    swap the context snapshot (compaction), the model (planning → execution),
    or the system prompt between turns without special-casing in the loop.

    Prior art: Pi's prepareNextTurn() hook.
    """

    messages: list[AgentMessage] | None = None  # replace context snapshot
    model: str | None = None  # switch model mid-task
    system_prompt: str | None = None  # swap system prompt


PrepareNextTurnHook = Callable[
    ["AgentContext", list[AgentMessage]],
    Awaitable["PrepareNextTurnResult | None"],
]
"""Called after ``turn_end``, before the next provider request.

This is where compaction lives: the session layer checks token usage, runs
compaction if needed, and returns a new context snapshot. The loop applies it
without knowing what happened.
"""

ShouldStopAfterTurnHook = Callable[
    ["AgentContext", list[AgentMessage]],
    Awaitable[bool],
]
"""Called after every ``turn_end``. Returns True to stop the loop gracefully.

The degenerate single-condition form of ``stop_conditions``. Kept for
back-compat; the loop wraps it into the condition list.
"""


@dataclass
class StopDecision:
    stop: bool
    reason: str = ""  # 'token_budget', 'loop_detected', 'max_turns', ...


StopCondition = Callable[
    ["AgentContext", list[AgentMessage]],
    Awaitable[StopDecision],
]
"""A named graceful-stop predicate evaluated after each ``turn_end``.

Naming each condition makes the stop reason self-documenting in telemetry —
``decision.reason`` flows into ``agent_end`` and onward to
``BrainResult.stop_reason``.
"""


@dataclass
class AgentLoopConfig:
    """Configuration for one agent loop run.

    Prior art: Pi's AgentLoopConfig (agent/src/types.ts:103).
    """

    provider: "LLMProvider"
    model: str
    convert_to_llm: ConvertToLlm
    transform_context: TransformContext | None = None
    before_tool_call: "BeforeToolCallHook | None" = None
    after_tool_call: "AfterToolCallHook | None" = None
    prepare_next_turn: PrepareNextTurnHook | None = None
    should_stop_after_turn: ShouldStopAfterTurnHook | None = None
    # Composable stop gate (Crush refinement). ``should_stop_after_turn`` is the
    # degenerate single-condition case; the loop adapts it into this list.
    stop_conditions: list[StopCondition] = field(default_factory=list)
    get_steering_messages: Callable[[], list[AgentMessage]] | None = None
    get_follow_up_messages: Callable[[], list[AgentMessage]] | None = None
    steering_queue_mode: QueueMode = "one_at_a_time"
    follow_up_queue_mode: QueueMode = "all"
    tool_execution: ToolExecutionMode = "sequential"
    max_tokens: int = 16384
    # Forwarded to ``provider.stream`` each turn. ``reasoning_effort`` is the
    # capability-gated effort tier (None = omit); ``render_tool_images`` lets the
    # provider render image-bearing tool results for vision models.
    reasoning_effort: str | None = None
    render_tool_images: bool = False
    abort: asyncio.Event | None = None
