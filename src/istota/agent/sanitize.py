"""Repair orphaned tool_call / tool_result pairs at the converter boundary.

An interrupted run can leave a dangling assistant ``tool_call`` with no
following ``tool_result`` (the tool never finished), or a ``tool_result`` whose
``tool_call`` was dropped by compaction. Either one makes the provider reject
the request with a 400. Because istota *retries* failed tasks, a dangling pair
is a silent, repeatable failure the retry loop can't escape.

``sanitize_tool_pairs`` runs inside ``convert_to_llm`` so it fires on every
provider request regardless of how the context was assembled:

- assistant ``tool_call`` with no matching ``tool_result`` → synthesize an
  error ``tool_result`` ("[interrupted: no result recorded]").
- ``tool_result`` with no preceding ``tool_call`` → drop it.

Prior art: Crush's preparePrompt() orphan synthesis; hermes's tool-pair
sanitization.
"""

from __future__ import annotations

from istota.llm.types import (
    AssistantMessage,
    Message,
    TextContent,
    ToolResultMessage,
)

_INTERRUPTED_TEXT = "[interrupted: no result recorded]"


def sanitize_tool_pairs(messages: list[Message]) -> list[Message]:
    """Return a message list with orphaned tool_call/tool_result pairs repaired.

    Pure: the input list and its messages are not mutated. Synthesized error
    results are inserted immediately after the assistant message that owns the
    orphaned call, preserving the call→result adjacency the providers expect.
    """
    # First pass: collect the set of tool_call ids that have a result.
    result_ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, ToolResultMessage) and msg.tool_call_id:
            result_ids.add(msg.tool_call_id)

    sanitized: list[Message] = []
    for msg in messages:
        if isinstance(msg, ToolResultMessage):
            # Drop results with no preceding call we've seen emitted.
            if msg.tool_call_id in _seen_call_ids(sanitized):
                sanitized.append(msg)
            # else: orphaned result → drop silently.
            continue

        sanitized.append(msg)

        if isinstance(msg, AssistantMessage):
            # Synthesize a result for every call this message makes that has
            # no real result anywhere downstream.
            for call in msg.tool_calls:
                if call.id not in result_ids:
                    sanitized.append(
                        ToolResultMessage(
                            tool_call_id=call.id,
                            tool_name=call.name,
                            content=[TextContent(text=_INTERRUPTED_TEXT)],
                            is_error=True,
                        )
                    )
                    # Treat the synthetic result as satisfying the call so a
                    # later real result (shouldn't exist) doesn't double up.
                    result_ids.add(call.id)

    return sanitized


def _seen_call_ids(messages: list[Message]) -> set[str]:
    """Tool-call ids issued by assistant messages seen so far."""
    ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, AssistantMessage):
            for call in msg.tool_calls:
                ids.add(call.id)
    return ids
