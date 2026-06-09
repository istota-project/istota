"""TmuxClaudeBrain — drives the *interactive* `claude` TUI via a detached tmux
session, instead of the headless `claude -p` subprocess `ClaudeCodeBrain` uses.

Why this exists: starting 2026-06-15, Anthropic meters headless/SDK usage
(`claude -p`, the Agent SDK, GitHub Actions) against a separate monthly Agent
SDK credit, while *interactive* terminal use keeps drawing from the normal
subscription limits. This brain is a feasibility prototype testing whether
driving the interactive TUI programmatically (prompt injection + a `Stop` hook
sentinel + transcript parsing) keeps Istota on subscription billing. See
``Specs/Active/tmux-subscription-brain-feasibility.md``.

Stage 0 (this file's current state) is scaffold only: it satisfies the Brain
protocol by delegating the four model-resolution methods wholesale to a composed
``ClaudeCodeBrain`` (same CLI, same Anthropic model namespace), and stubs
``execute`` with NotImplementedError. The real tmux flow lands in Stage 2.
"""

import json
import logging
from pathlib import Path

from ..agent.events import _describe_tool_use
from ._events import (
    ResultEvent,
    StreamEvent,
    TextEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from ._types import BrainRequest, BrainResult
from .claude_code import ClaudeCodeBrain

logger = logging.getLogger("istota.brain.tmux_claude")


def parse_transcript(path: Path) -> list[StreamEvent]:
    """Reconstruct StreamEvents from an interactive `claude` transcript JSONL.

    The interactive transcript is an append-only session log — one JSON record
    per line, each with a top-level ``type`` (``assistant`` / ``user`` /
    ``system`` / ``attachment`` / ``mode`` / …). Unlike ``-p``'s stream-json it
    carries **no** terminal ``result`` record, so the final answer is
    synthesized from the last ``end_turn`` assistant turn's text blocks.

    Returns the ordered events with a terminal ``ResultEvent`` appended:
    ``ToolUseEvent`` per ``tool_use`` block, ``TextEvent`` per ``text`` block,
    ``ThinkingEvent`` per ``thinking`` block (document order within each
    assistant record), then ``ResultEvent(success, text)``. Non-assistant
    records (the user prompt, tool results, system notices) contribute no
    events — they exist only as turn structure.

    Malformed / blank lines are skipped (defensive: a partially-flushed
    transcript at sentinel time must not crash the parse).
    """
    events: list[StreamEvent] = []
    # Track the text of the last assistant turn that ended the conversation
    # (stop_reason == "end_turn"); that is the final answer. Fall back to the
    # last assistant text seen if no end_turn turn exists (degenerate session).
    final_answer: str | None = None
    last_text_any: str | None = None
    saw_assistant = False
    seen_tool_ids: set[str] = set()

    for raw in _iter_records(path):
        if raw.get("type") != "assistant":
            continue
        message = raw.get("message")
        if not isinstance(message, dict):
            continue
        saw_assistant = True
        content = message.get("content")
        if not isinstance(content, list):
            continue

        turn_text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                block_id = block.get("id", "")
                if block_id and block_id in seen_tool_ids:
                    continue
                if block_id:
                    seen_tool_ids.add(block_id)
                name = block.get("name", "")
                desc = _describe_tool_use(name, block.get("input", {}))
                events.append(
                    ToolUseEvent(tool_name=name, description=desc, tool_call_id=block_id)
                )
            elif btype == "text":
                text = (block.get("text") or "").strip()
                if not text:
                    continue
                events.append(TextEvent(text=text))
                turn_text_parts.append(text)
            elif btype == "thinking":
                think = (block.get("thinking") or "").strip()
                if not think:
                    continue
                events.append(ThinkingEvent(text=think))

        if turn_text_parts:
            joined = "\n".join(turn_text_parts)
            last_text_any = joined
            if message.get("stop_reason") == "end_turn":
                final_answer = joined

    if final_answer is not None:
        result_text = final_answer
    elif last_text_any is not None:
        result_text = last_text_any
    else:
        result_text = ""

    # Success = we saw at least one assistant turn. An empty transcript (no
    # assistant records at all) means the turn never produced output.
    events.append(ResultEvent(success=saw_assistant, text=result_text))
    return events


def _iter_records(path: Path):
    """Yield parsed JSON records from a JSONL transcript, skipping blanks and
    malformed lines."""
    try:
        text = Path(path).read_text()
    except OSError as e:
        logger.warning("parse_transcript: cannot read %s: %s", path, e)
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            logger.debug("parse_transcript: skipping non-JSON line: %s", line[:100])
            continue


class TmuxClaudeBrain:
    """Brain that drives the interactive `claude` TUI inside a tmux session.

    Model resolution is delegated to an internal ``ClaudeCodeBrain``: this brain
    runs the same `claude` CLI binary against the same Anthropic model namespace,
    so duplicating ``MODEL_ALIASES`` / ``DEFAULT_ROLE_TARGETS`` would only invite
    drift. Only ``execute`` is genuinely new.
    """

    def __init__(self) -> None:
        # Composed, not inherited: we forward the four resolution methods and
        # own execute. The CLI brain holds no per-instance state, so a fresh
        # one here is free.
        self._cli = ClaudeCodeBrain()

    # --- Model resolution (delegated to ClaudeCodeBrain) -------------------

    def resolve_alias(self, alias):
        return self._cli.resolve_alias(alias)

    def resolve_model_name(self, name):
        return self._cli.resolve_model_name(name)

    def list_aliases(self):
        return self._cli.list_aliases()

    def validate_role_override(self, role, target):
        return self._cli.validate_role_override(role, target)

    # --- Execution --------------------------------------------------------

    def execute(self, req: BrainRequest) -> BrainResult:
        raise NotImplementedError(
            "TmuxClaudeBrain.execute lands in Stage 2 of the "
            "tmux-subscription-brain feasibility study"
        )
