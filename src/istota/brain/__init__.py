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
    TextEvent,
    ToolUseEvent,
    make_stream_parser,
    parse_stream_line,
)
from ._roles import get_role_override, get_role_overrides, set_role_overrides
from ._types import Brain, BrainConfig, BrainRequest, BrainResult
from .claude_code import ClaudeCodeBrain


def make_brain(brain_config: BrainConfig) -> Brain:
    """Construct a brain instance from config.

    Raises ValueError on unknown brain.kind so misconfiguration fails loud
    at startup rather than silently picking the wrong implementation.
    """
    kind = brain_config.kind
    if kind == "claude_code":
        return ClaudeCodeBrain()
    raise ValueError(f"Unknown brain kind: {kind!r}")


__all__ = [
    "Brain",
    "BrainConfig",
    "BrainRequest",
    "BrainResult",
    "ClaudeCodeBrain",
    "ContextManagementEvent",
    "ResultEvent",
    "StreamEvent",
    "TextEvent",
    "ToolUseEvent",
    "get_role_override",
    "get_role_overrides",
    "make_brain",
    "make_stream_parser",
    "parse_stream_line",
    "set_role_overrides",
]
