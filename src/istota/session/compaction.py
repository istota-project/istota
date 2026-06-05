"""Context compaction for long native-brain sessions.

Compaction runs inside the loop's ``prepare_next_turn`` hook, at a clean turn
boundary — not wrapped around the loop. The session layer checks token usage
after each turn, and when the context approaches the model's window it replaces a
prefix of old messages with a single structured summary, keeping the recent tail
intact.

Key decisions:
- Incremental summaries: an existing summary is *updated* (preserve, add, move
  In Progress → Done) rather than regenerated, so context survives many cycles.
- Two-stage token estimation: real ``Usage`` from the last good assistant message
  plus a chars/4 heuristic for anything after it.
- Never cut at a tool_result — it must follow its tool_call.
- File-operation tracking across cycles: ``CompactionDetails`` (read / modified
  files) is carried forward so the model keeps awareness of files touched early.
- Compaction failure never crashes the loop — it returns the previous summary.

Prior art: Pi's compact() (compaction.ts) — cut-point detection, incremental
summaries, file-operation tracking, structured prompt format.
"""

from __future__ import annotations

import logging

from istota.llm.tokens import estimate_tokens
from istota.llm.types import (
    AssistantMessage,
    TextContent,
    ToolResultMessage,
    UserMessage,
)

from .messages import CompactionDetails

logger = logging.getLogger("istota.session.compaction")


def should_compact(
    context_tokens: int,
    context_window: int,
    reserve_tokens: int = 16384,
) -> bool:
    """True when the context is within ``reserve_tokens`` of the window."""
    return context_tokens > context_window - reserve_tokens


def estimate_context_tokens(messages: list) -> tuple[int, int | None]:
    """Two-stage estimate: real usage from the last good assistant turn + tail.

    Returns ``(total_estimate, last_usage_index)``. Walks back to the most recent
    assistant message whose stop_reason is not error/aborted, takes its reported
    ``total_tokens``, and adds a chars/4 estimate for everything after it. Falls
    back to a pure heuristic when no usable usage is present.
    """
    last_usage_idx: int | None = None
    last_usage_tokens = 0

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if hasattr(msg, "usage") and hasattr(msg, "stop_reason"):
            if msg.stop_reason not in ("error", "aborted") and msg.usage.total_tokens > 0:
                last_usage_idx = i
                last_usage_tokens = msg.usage.total_tokens
                break

    if last_usage_idx is None:
        total = sum(estimate_tokens(m) for m in messages if hasattr(m, "content"))
        return total, None

    trailing = sum(
        estimate_tokens(m)
        for m in messages[last_usage_idx + 1 :]
        if hasattr(m, "content")
    )
    return last_usage_tokens + trailing, last_usage_idx


def find_cut_point(messages: list, keep_recent_tokens: int = 20000) -> int:
    """Index splitting compacted prefix (``[:cut]``) from kept tail (``[cut:]``).

    Walks back from the newest, accumulating token estimates until
    ``keep_recent_tokens`` is reached; the boundary message and everything older
    are compacted. The cut never lands on a tool_result (it would strand the
    result from its call) — it advances forward past any leading tool_results.
    The newest message is always kept, even if it alone exceeds the budget
    (we don't cut mid-turn).

    Returns 0 when everything fits (nothing to compact).
    """
    if not messages:
        return 0

    accumulated = 0
    cut_idx = 0
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if hasattr(msg, "content"):
            accumulated += estimate_tokens(msg)
        if accumulated >= keep_recent_tokens:
            # Keep message ``i`` and everything newer; compact the rest.
            cut_idx = i
            break

    if cut_idx == 0:
        return 0

    # Never start the kept tail on a tool_result — advance past leading ones.
    while cut_idx < len(messages) and isinstance(messages[cut_idx], ToolResultMessage):
        cut_idx += 1

    # Always keep at least the newest message.
    cut_idx = min(cut_idx, len(messages) - 1)
    return cut_idx


def _extract_file_operations(messages: list) -> dict[str, list[str]]:
    """Pull read / modified file paths from tool calls in the compacted prefix."""
    read: list[str] = []
    modified: list[str] = []
    for msg in messages:
        if not isinstance(msg, AssistantMessage):
            continue
        for call in msg.tool_calls:
            path = call.arguments.get("file_path") or call.arguments.get("path")
            if not path:
                continue
            if call.name in ("Write", "Edit", "MultiEdit"):
                modified.append(path)
            elif call.name in ("Read", "Grep", "Glob"):
                read.append(path)
    return {"read": read, "modified": modified}


def _format_file_operations(details: CompactionDetails) -> str:
    parts = []
    if details.read_files:
        parts.append("Files read: " + ", ".join(details.read_files))
    if details.modified_files:
        parts.append("Files modified: " + ", ".join(details.modified_files))
    if not parts:
        return ""
    return "Files touched this session:\n" + "\n".join(parts) + "\n\n"


def _serialize_for_summary(llm_messages: list) -> str:
    """Render LLM messages to plain text for the summary prompt."""
    lines: list[str] = []
    for msg in llm_messages:
        role = getattr(msg, "role", "?")
        if isinstance(msg, ToolResultMessage):
            text = "\n".join(getattr(c, "text", "") for c in msg.content)
            lines.append(f"[tool_result {msg.tool_name}]\n{text}")
            continue
        text_parts = []
        for c in getattr(msg, "content", []):
            if isinstance(c, TextContent):
                text_parts.append(c.text)
            elif getattr(c, "type", "") == "tool_call":
                text_parts.append(f"[tool_call {c.name} {c.arguments}]")
        lines.append(f"{role}: {' '.join(p for p in text_parts if p)}")
    return "\n\n".join(lines)


_STRUCTURE = (
    "## Goal\nWhat the task is trying to accomplish.\n\n"
    "## Constraints\nRules, limits, or requirements that bound the work.\n\n"
    "## Progress\n- Done: completed items\n- In Progress: current items\n\n"
    "## Key Decisions\nDecisions made and alternatives rejected (with reasons).\n\n"
    "## Next Steps\nWhat remains to be done.\n\n"
    "## Critical Context\nAnything the model must remember to continue correctly "
    "(variable names, file paths, error messages, user preferences stated during "
    "the session).\n\n"
)


async def _complete_simple(provider, model: str, prompt: str) -> str:
    """Run a single text completion through ``provider`` and return its text."""
    from istota.llm.provider import StreamDone, StreamError, TextDelta

    text_parts: list[str] = []
    final_text = ""
    async for event in provider.stream(
        "", [UserMessage(content=[TextContent(text=prompt)])], [], model=model
    ):
        if isinstance(event, TextDelta):
            text_parts.append(event.text)
        elif isinstance(event, StreamDone):
            final_text = event.message.text
            break
        elif isinstance(event, StreamError):
            raise RuntimeError(event.message.error_message or "compaction stream error")
    return final_text or "".join(text_parts)


async def compact_messages(
    messages_to_compact: list,
    previous_summary: str | None,
    previous_details: CompactionDetails | None,
    provider,
    model: str,
    convert_to_llm,
) -> tuple[str, CompactionDetails]:
    """Generate (or update) a structured summary, tracking file operations.

    Returns ``(summary_text, merged_details)``. File-operation details are merged
    with the previous cycle's and carried forward. On any provider failure the
    previous summary is returned unchanged — compaction never crashes the loop.
    """
    new_ops = _extract_file_operations(messages_to_compact)
    merged_details = CompactionDetails(
        read_files=list(
            dict.fromkeys(
                (previous_details.read_files if previous_details else []) + new_ops["read"]
            )
        ),
        modified_files=list(
            dict.fromkeys(
                (previous_details.modified_files if previous_details else [])
                + new_ops["modified"]
            )
        ),
    )

    try:
        llm_messages = convert_to_llm(messages_to_compact)
        conversation_text = _serialize_for_summary(llm_messages)
    except Exception as e:  # noqa: BLE001 — conversion must not crash compaction
        logger.warning("Compaction serialization failed: %s", e)
        return previous_summary or "", merged_details

    file_ops_text = (
        _format_file_operations(merged_details)
        if (merged_details.read_files or merged_details.modified_files)
        else ""
    )

    if previous_summary:
        prompt = (
            "You are updating a conversation summary. Below is the previous "
            "summary followed by new conversation content.\n\n"
            "Update the summary using this structure. Preserve existing entries, "
            "add new information, and move In Progress items to Done where "
            "applicable:\n\n"
            f"{_STRUCTURE}"
            f"Previous summary:\n{previous_summary}\n\n"
            f"New conversation:\n{conversation_text}\n\n"
            f"{file_ops_text}"
            "Updated summary:"
        )
    else:
        prompt = (
            "Summarize this conversation using the following structure:\n\n"
            f"{_STRUCTURE}"
            f"{conversation_text}\n\n"
            f"{file_ops_text}"
            "Summary:"
        )

    try:
        summary = await _complete_simple(provider, model, prompt)
        return summary or (previous_summary or ""), merged_details
    except Exception as e:  # noqa: BLE001 — failed summary must not crash the loop
        logger.warning("Compaction summary failed: %s", e)
        return previous_summary or "", merged_details
