"""Backward-compatibility shim — stream events moved to ``istota.brain._events``.

New code should import from ``istota.brain`` directly. This module is kept
because tests and a few internal references still import from here.
"""

from .brain._events import (
    ContextManagementEvent,
    ResultEvent,
    StreamEvent,
    TextDeltaEvent,
    TextEvent,
    ThinkingDeltaEvent,
    ThinkingEvent,
    ToolUseEvent,
    _describe_tool_use,
    make_stream_parser,
    parse_stream_line,
)

__all__ = [
    "ContextManagementEvent",
    "ResultEvent",
    "StreamEvent",
    "TextDeltaEvent",
    "TextEvent",
    "ThinkingDeltaEvent",
    "ThinkingEvent",
    "ToolUseEvent",
    "make_stream_parser",
    "parse_stream_line",
]
