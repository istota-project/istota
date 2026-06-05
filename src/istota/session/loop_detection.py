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

    Pairs each assistant ``tool_call`` to its ``tool_result`` by id, builds a
    signature per completed pair, and counts signatures over the trailing
    ``window`` pairs. Unpaired (dangling) calls are skipped.
    """
    # Map every result to its tool_call_id for pairing.
    results: dict[str, ToolResultMessage] = {}
    for msg in messages:
        if isinstance(msg, ToolResultMessage) and msg.tool_call_id:
            results[msg.tool_call_id] = msg

    signatures: list[str] = []
    for msg in messages:
        if not isinstance(msg, AssistantMessage):
            continue
        for call in msg.tool_calls:
            result = results.get(call.id)
            if result is None:
                continue  # dangling call — not a completed execution
            signatures.append(_signature(call.name, call.arguments, _result_text(result)))

    recent = signatures[-window:]
    counts: dict[str, int] = {}
    for sig in recent:
        counts[sig] = counts.get(sig, 0) + 1
        if counts[sig] > max_repeats:
            return sig
    return None
