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

# ``_describe_tool_use`` is private but re-exported deliberately: it was part of
# this module before the move and ``tests/test_stream_parser.py`` still imports
# it from here. Listing it makes the shim's surface declared rather than
# incidental — an unlisted name reads as an unused import and gets pruned.
__all__ = [
    "ContextManagementEvent",
    "ResultEvent",
    "StreamEvent",
    "TextDeltaEvent",
    "TextEvent",
    "ThinkingDeltaEvent",
    "ThinkingEvent",
    "ToolUseEvent",
    "_describe_tool_use",
    "make_stream_parser",
    "parse_stream_line",
]
