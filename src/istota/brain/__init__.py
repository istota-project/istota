"""Brain abstraction — model invocation behind a single protocol.

The executor builds the prompt, env, and sandbox configuration, then hands
a BrainRequest to a Brain implementation. Brains own everything from
"compose the model call" through "produce a result + trace", *and* own
their own model namespace — canonical IDs, provider aliases, and how role
aliases like ``smart`` map to a real model. Consumers never reach into a
brain module's tables; they go through ``make_brain(config.brain)`` and
call ``.resolve_alias`` / ``.resolve_model_name`` / ``.list_aliases``.

Operator role overrides (``[models.roles]`` TOML) are provider-agnostic
and live globally in ``_roles.py`` — each brain consults the override
table at resolution time and routes the override target through its own
provider alias map.

Phase 1 ships a single brain (ClaudeCodeBrain) that wraps the `claude`
CLI. Future phases add direct-HTTP brains (OpenRouter, Anthropic) without
any change to the executor's per-task orchestration.
"""

from ._events import (
    ContextManagementEvent,
    ResultEvent,
    StreamEvent,
    TextDeltaEvent,
    TextEvent,
    ThinkingDeltaEvent,
    ThinkingEvent,
    ToolEndEvent,
    ToolProgressEvent,
    ToolUseEvent,
    make_stream_parser,
    parse_stream_line,
)
import dataclasses
import logging

from ._roles import get_role_override, get_role_overrides, set_role_overrides
from ._types import Brain, BrainConfig, BrainRequest, BrainResult
from .claude_code import ClaudeCodeBrain
from .native import NativeBrain

logger = logging.getLogger(__name__)

# Every brain kind ``make_brain`` knows how to build. The routing resolver
# validates override targets against this set so a typo in
# ``[brain.source_type_overrides]`` falls back to the base kind instead of
# raising and wedging the task.
KNOWN_BRAIN_KINDS = frozenset({"claude_code", "native", "tmux_claude"})


def make_brain(brain_config: BrainConfig) -> Brain:
    """Construct a brain instance from config.

    Raises ValueError on unknown brain.kind so misconfiguration fails loud
    at startup rather than silently picking the wrong implementation.
    """
    kind = brain_config.kind
    if kind == "claude_code":
        return ClaudeCodeBrain()
    if kind == "native":
        from .native import NativeBrain

        return NativeBrain(brain_config.native)
    if kind == "tmux_claude":
        from .tmux_claude import TmuxClaudeBrain

        return TmuxClaudeBrain()
    raise ValueError(f"Unknown brain kind: {kind!r}")


def resolve_brain_kind(source_type, brain_config):
    """Return the BrainConfig to use for a task with the given source_type.

    The instance default (``brain_config.kind``) applies unless an operator
    has mapped this ``source_type`` to a different kind via
    ``[brain.source_type_overrides]``. This is the gradual-rollout knob:
    route cron/heartbeat tasks to the native brain while interactive tasks
    stay on ``claude_code``, with no executor or DB change.

    Returns ``brain_config`` unchanged (same object) when no override applies,
    so callers can cheaply detect the common no-routing case. Unknown override
    targets are logged and ignored — a routing typo must never break a task.
    """
    overrides = getattr(brain_config, "source_type_overrides", None) or {}
    target = overrides.get((source_type or "").strip())
    if not target or target == brain_config.kind:
        return brain_config
    if target not in KNOWN_BRAIN_KINDS:
        logger.warning(
            "brain routing: unknown kind %r mapped for source_type %r; "
            "falling back to %r",
            target, source_type, brain_config.kind,
        )
        return brain_config
    return dataclasses.replace(brain_config, kind=target)


__all__ = [
    "Brain",
    "BrainConfig",
    "BrainRequest",
    "BrainResult",
    "ClaudeCodeBrain",
    "ContextManagementEvent",
    "KNOWN_BRAIN_KINDS",
    "NativeBrain",
    "ResultEvent",
    "StreamEvent",
    "TextDeltaEvent",
    "TextEvent",
    "ThinkingDeltaEvent",
    "ThinkingEvent",
    "ToolEndEvent",
    "ToolProgressEvent",
    "ToolUseEvent",
    "get_role_override",
    "get_role_overrides",
    "make_brain",
    "make_stream_parser",
    "parse_stream_line",
    "resolve_brain_kind",
    "set_role_overrides",
]
