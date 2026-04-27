"""Brain abstraction — model invocation behind a single protocol.

The executor builds the prompt, env, and sandbox configuration, then hands
a BrainRequest to a Brain implementation. Brains own everything from
"compose the model call" through "produce a result + trace".

Phase 1 ships a single brain (ClaudeCodeBrain) that wraps the `claude` CLI.
Future phases add direct-HTTP brains (OpenRouter, Anthropic) without any
change to the executor's per-task orchestration.
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
    "make_brain",
    "make_stream_parser",
    "parse_stream_line",
]
