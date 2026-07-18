"""Output-aware loop detection (Crush refinement item 2).

A wedged model can call the same tool with the same arguments and keep getting
the same result, burning tokens forever. We hash ``tool_name + args + result``
over a sliding window of the most recent tool executions and fire when one
signature recurs more than ``max_repeats`` times.

Including the *result* in the hash is what makes this output-aware: a model that
re-runs ``tail log`` and gets new lines each time is making progress and must not
trip the detector, while one that re-runs it and gets the identical output is
stuck and should.

Registered by ``NativeBrain`` as a ``loop_detected`` stop condition — a hard
stop, not a steering nudge. For a headless multi-user service a wedged model
should stop, not be coaxed.

Prior art: Crush's hasRepeatedToolCalls (loop_detection.go). Improves on
opencode's strict-consecutive identical check: windowed, count-based, and
output-aware.
"""

from __future__ import annotations

import hashlib
import json

from istota.llm.types import AssistantMessage, ToolResultMessage


def _result_text(msg: ToolResultMessage) -> str:
    return "\n".join(getattr(c, "text", "") for c in msg.content)


def _signature(tool_name: str, args: dict, result_text: str) -> str:
    payload = "\0".join([tool_name, json.dumps(args, sort_keys=True), result_text])
    return hashlib.sha256(payload.encode()).hexdigest()


def detect_repeated_tool_calls(
    messages: list,
    *,
    window: int = 10,
    max_repeats: int = 5,
) -> str | None:
    """Return the repeating signature if a tool call+result recurs more than
    ``max_repeats`` times within the last ``window`` tool executions, else None.

    Pairs each assistant ``tool_call`` to its ``tool_result`` by **adjacency** —
    a result matches a call in the *same local group* (the results immediately
    following that assistant message), not via a transcript-wide id map. Some
    endpoints (llama.cpp / vLLM) emit deterministic per-response ids like
    ``call_0`` reused every turn; a global map would pair every historical call
    with the newest result and hash six progressing calls identically, tripping
    a false ``loop_detected`` hard stop (NB-5). Within one assistant message,
    parallel-call ids are unique, so local id matching (with a positional
    fallback for id-less endpoints) is safe. Unpaired (dangling) calls are
    skipped.
    """
    signatures: list[str] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if not (isinstance(msg, AssistantMessage) and msg.tool_calls):
            i += 1
            continue
        # Collect the run of tool results immediately following this message —
        # they belong to this message's calls.
        j = i + 1
        local_by_id: dict[str, ToolResultMessage] = {}
        local_in_order: list[ToolResultMessage] = []
        while j < n and isinstance(messages[j], ToolResultMessage):
            r = messages[j]
            if r.tool_call_id:
                local_by_id[r.tool_call_id] = r
            local_in_order.append(r)
            j += 1
        for idx, call in enumerate(msg.tool_calls):
            result = local_by_id.get(call.id)
            if result is None and idx < len(local_in_order):
                result = local_in_order[idx]  # positional fallback (id-less)
            if result is None:
                continue  # dangling call — not a completed execution
            signatures.append(_signature(call.name, call.arguments, _result_text(result)))
        i = j if j > i + 1 else i + 1

    recent = signatures[-window:]
    counts: dict[str, int] = {}
    for sig in recent:
        counts[sig] = counts.get(sig, 0) + 1
        if counts[sig] > max_repeats:
            return sig
    return None
