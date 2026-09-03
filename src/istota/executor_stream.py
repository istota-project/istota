"""Adapting a brain's ``StreamEvent`` stream to the task's ``TaskEvent`` log.

Extracted verbatim from ``execute_task``, where it was one callback and three
flush helpers over six mutable locals. The dict-wrapped flags it used
(``_delta_state``, ``_delta_seen``, ``_thinking_seen``) were dicts only because
a closure cannot rebind an enclosing name; as attributes they are plain.

The module owns nothing else: the coalescing cadence, the narration gate and
the delta-vs-whole-turn dedupe are all here, and the executor holds one
instance per task.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from .brain._events import (
    ContextManagementEvent,
    StreamEvent,
    TextDeltaEvent,
    TextEvent,
    ThinkingDeltaEvent,
    ThinkingEvent,
    ToolEndEvent,
    ToolProgressEvent,
    ToolUseEvent,
)

if TYPE_CHECKING:
    from . import db
    from .config import Config
    from .events import EventWriter

logger = logging.getLogger(__name__)

_DELTA_FLUSH_MS = 250
_DELTA_FLUSH_CHARS = 120


class TaskStreamAdapter:
    """Adapts a brain's StreamEvent stream to TaskEvents for one task.

    Owns the coalescing buffers for streamed answer text and reasoning, the
    narration gate, and the delta-vs-whole-turn dedupe. Stateful and
    single-threaded: events arrive serialized (NativeBrain awaits each
    run_in_executor hop; ClaudeCodeBrain's parse loop is sequential), so
    nothing here takes a lock.

    ``on_event`` is what goes on ``BrainRequest.on_progress``. For loop-based
    brains (NativeBrain) it fires on a worker thread, not the brain's event
    loop (Layer 3 invariant) — the body stays plain-synchronous either way.
    ``progress_show_tool_use`` / ``progress_show_text`` gate whether ``tool_*``
    and ``progress_text`` events are emitted at all.
    """

    def __init__(
        self,
        config: "Config",
        task: "db.Task",
        event_writer: "EventWriter | None",
    ) -> None:
        self.config = config
        self.task = task
        self.event_writer = event_writer
        self.show_tool_use = config.scheduler.progress_show_tool_use
        self.show_text = config.scheduler.progress_show_text

        # Stream surfaces (web chat, repl) get the answer text streamed live as
        # ``text_delta`` events; push surfaces (Talk/email/ntfy/istota_file) are
        # completely untouched — no text_delta rows. Computed once per task.
        # Imported here, not at module scope. The reason is a *test* constraint
        # rather than a cycle — ``transport.registry`` imports only ``._types``,
        # so a module-scope import would resolve cleanly — but two suites patch
        # this name through ``istota.transport.registry``, and a name bound at
        # import time would make both patches inert while the tests still
        # passed. Moving it up means re-pointing those patch targets.
        from .transport.registry import task_is_stream_surface

        self.is_stream_surface = task_is_stream_surface(config, task)

        # Per-task coalescing buffer for streamed answer text. Incoming deltas
        # (NativeBrain's TextDeltaEvent, or ClaudeCodeBrain's block TextEvent)
        # are buffered and flushed as one ``text_delta`` event every ~250 ms or
        # ~120 chars, plus a forced flush on each tool/CM boundary and a final
        # flush after the brain finishes. This bounds row volume to tens per
        # answer (not thousands of token rows); the scheduler prunes them once
        # the canonical ``result`` lands, so steady state retains zero.
        #
        # Narration gate: a text run emits NOTHING until it crosses the gate
        # without an intervening tool call. This splits a text-then-tool block
        # into two cases at the boundary (see ``settle_at_tool_boundary``): a
        # short lead-in ("Let me check…") stays under the ceiling, never
        # streams, and is dropped; a SUBSTANTIAL block crosses the ceiling,
        # "unlocks" (the held buffer flushes and subsequent deltas stream live
        # at the cadence below), and is KEPT — flushed at the tool boundary so
        # the full block reaches the stream surface, where the web client
        # renders it as its own prose block rather than throwaway narration.
        # The gate is thus a substance classifier, not an answer-vs-narration
        # one: the final answer (after the last tool) always streams, and a
        # short *final* answer that never crosses the gate still arrives via the
        # canonical ``result`` event (and ``finish`` releases the held buffer),
        # so gating costs only token-by-token animation on text too short to
        # benefit. Threshold is the ``[scheduler]`` knob
        # ``stream_text_gate_chars`` (0 disables — deltas stream immediately,
        # legacy behaviour); the ``stream_gate:`` telemetry below records every
        # flush / discard so the value can be tuned against production.
        self._delta_gate_chars = config.scheduler.stream_text_gate_chars
        self._delta_buf: list[str] = []
        self._delta_chars = 0
        self._delta_last_flush = time.monotonic()
        # ``unlocked``: this text run has crossed the narration gate; deltas now
        # stream live. Reset to False at every tool boundary (new run re-gates).
        self._delta_unlocked = False
        # True once any TextDeltaEvent has streamed this task. Used to dedupe a
        # NativeBrain whole-turn TextEvent against the deltas that already
        # carried the same text: the brain stays surface-agnostic (it always
        # emits both per-token deltas and intermediate-turn TextEvents); the
        # executor — which alone knows the surface — drops the redundant
        # TextEvent on a stream surface once deltas have flowed, and forwards it
        # as progress_text on a push surface (where deltas were dropped).
        self._delta_seen = False
        # Symmetric flag for reasoning: True once any ThinkingDeltaEvent has
        # streamed. A brain that streams thinking deltas (NativeBrain, or
        # ClaudeCodeBrain with --include-partial-messages) may *also* emit the
        # whole-block ThinkingEvent afterward; on a stream surface that whole
        # block is then a redundant re-render, so it is dropped here. Thinking is
        # stream-surface-only either way (push drops both), so no push fallback.
        self._thinking_seen = False

        # A SEPARATE coalescing buffer for streamed *thinking*
        # (extended-reasoning) text. It must be independent of the answer-text
        # buffer above because the two render to different places on a stream
        # surface: thinking folds into the activity chip, the answer streams
        # prominent. Same flush cadence / boundaries; emits ``thinking`` task
        # events instead of ``text_delta``.
        self._thinking_buf: list[str] = []
        self._thinking_chars = 0
        self._thinking_last_flush = time.monotonic()

    # --- answer text ---

    def flush_deltas(self) -> None:
        if self.event_writer is None or not self._delta_buf:
            return
        text = "".join(self._delta_buf)
        self._delta_buf.clear()
        self._delta_chars = 0
        self._delta_last_flush = time.monotonic()
        # Best-effort: a flush failure means slightly less live text, never
        # a failed task (matches EventWriter.emit's own swallow).
        try:
            self.event_writer.emit("text_delta", {"text": text})
        except Exception:
            logger.debug("text_delta flush failed", exc_info=True)

    def _buffer_delta(self, text: str) -> None:
        if not text:
            return
        self._delta_buf.append(text)
        self._delta_chars += len(text)
        if not self._delta_unlocked:
            # Gated: hold everything (emit nothing) until the run crosses
            # the narration ceiling. Crucially NO time-based flush here —
            # that was the race that leaked narration. A tool boundary
            # before the ceiling discards the buffer; crossing it unlocks.
            if self._delta_chars >= self._delta_gate_chars:
                self._delta_unlocked = True
                logger.debug(
                    "stream_gate: unlocked at %d chars (task %s, gate=%d)",
                    self._delta_chars, self.task.id, self._delta_gate_chars,
                )
                self.flush_deltas()
            return
        now = time.monotonic()
        if (
            self._delta_chars >= _DELTA_FLUSH_CHARS
            or (now - self._delta_last_flush) * 1000 >= _DELTA_FLUSH_MS
        ):
            self.flush_deltas()

    def settle_at_tool_boundary(self) -> None:
        # Resolve the buffered answer text at a tool boundary. Text before a
        # tool is one of two things, and the narration gate already told them
        # apart:
        #   (a) a SUBSTANTIAL block (the run crossed the gate and unlocked —
        #       analysis the model wrote, then acted on). It has been streaming;
        #       FLUSH its unflushed tail so the full block reaches the stream
        #       surface and renders as its own prose block (the web client keeps
        #       substantial intermediate blocks — they are not narration). A
        #       token-streaming brain (NativeBrain) leaves up to one flush-window
        #       buffered here; a whole-block brain already flushed everything on
        #       unlock, so this is a no-op for it.
        #   (b) a short LEAD-IN ("Let me search…", under the gate). It was
        #       held and never emitted; DROP it intact so it doesn't flash in
        #       the prominent answer area. Only reasoning + tool actions land
        #       in the activity chip.
        held = self._delta_chars
        if self._delta_unlocked:
            if held:
                logger.debug(
                    "stream_gate: flushed %d-char tail of a substantial "
                    "block at a tool boundary (task %s)", held, self.task.id,
                )
            self.flush_deltas()  # clears buf + resets chars/last_flush
        else:
            if held:
                logger.debug(
                    "stream_gate: discarded %d chars of held narration at a "
                    "tool boundary (task %s, gate=%d)",
                    held, self.task.id, self._delta_gate_chars,
                )
            self._delta_buf.clear()
            self._delta_chars = 0
            self._delta_last_flush = time.monotonic()
        self._delta_unlocked = False  # next text run re-gates

    # --- reasoning ---

    def flush_thinking(self) -> None:
        if self.event_writer is None or not self._thinking_buf:
            return
        text = "".join(self._thinking_buf)
        self._thinking_buf.clear()
        self._thinking_chars = 0
        self._thinking_last_flush = time.monotonic()
        try:
            self.event_writer.emit("thinking", {"text": text})
        except Exception:
            logger.debug("thinking flush failed", exc_info=True)

    def _buffer_thinking(self, text: str) -> None:
        if not text:
            return
        self._thinking_buf.append(text)
        self._thinking_chars += len(text)
        now = time.monotonic()
        if (
            self._thinking_chars >= _DELTA_FLUSH_CHARS
            or (now - self._thinking_last_flush) * 1000 >= _DELTA_FLUSH_MS
        ):
            self.flush_thinking()

    # --- the callback ---

    def on_event(self, event: StreamEvent) -> None:
        event_writer = self.event_writer
        if event_writer is None:
            return
        if isinstance(event, ToolUseEvent):
            # A tool boundary settles the reasoning chip and drops any
            # pre-tool narration. This is a property of the STREAM SURFACE,
            # not of whether the tool row is shown — so it must run even when
            # progress_show_tool_use is off, or pre-tool narration would
            # flush and flash in the answer area with no tool chip to explain
            # it.
            if self.is_stream_surface:
                self.flush_thinking()  # tool boundary: settle the reasoning chip
                self.settle_at_tool_boundary()  # keep substantial, drop lead-ins
            if self.show_tool_use:
                event_writer.emit("tool_start", {
                    "tool_name": event.tool_name,
                    "description": event.description,
                    "tool_call_id": event.tool_call_id,  # "" under ClaudeCodeBrain
                })
        elif isinstance(event, ToolEndEvent) and self.show_tool_use:
            event_writer.emit("tool_end", {
                "tool_name": event.tool_name,
                "tool_call_id": event.tool_call_id,
                "success": event.success,
                "duration_ms": event.duration_ms,
            })
        elif isinstance(event, ToolProgressEvent):
            # Web SSE only; Talk/log subscribers ignore this kind.
            event_writer.emit("tool_progress", {
                "tool_name": event.tool_name,
                "tool_call_id": event.tool_call_id,
                "text": event.text,
            })
        elif isinstance(event, ThinkingDeltaEvent):
            # Incremental reasoning (NativeBrain, or ClaudeCodeBrain with
            # --include-partial-messages). Stream surfaces only; a push task
            # drops it (thinking is web/repl-only — no progress_text
            # fallback).
            if self.is_stream_surface:
                self._thinking_seen = True
                self._buffer_thinking(event.thinking)
        elif isinstance(event, ThinkingEvent):
            # Whole reasoning block. Stream surfaces only. Dropped when
            # thinking deltas already carried this turn's reasoning live
            # (mirrors the TextEvent-vs-deltas dedup above).
            if self.is_stream_surface:
                if self._thinking_seen:
                    return
                self._buffer_thinking(event.text)
        elif isinstance(event, TextDeltaEvent):
            # NativeBrain incremental answer text. Stream surfaces only; a
            # push task drops it (the final result is delivered once).
            if self.is_stream_surface:
                self.flush_thinking()  # thinking → answer boundary: keep order
                self._delta_seen = True
                self._buffer_delta(event.text)
        elif isinstance(event, TextEvent):
            if self.is_stream_surface:
                if self._delta_seen:
                    # NativeBrain: the per-token deltas already carried this
                    # intermediate turn's text live, so the whole-turn
                    # TextEvent is a redundant re-render — drop it.
                    return
                # ClaudeCodeBrain (no deltas): coarse streaming, one
                # TextEvent per completed block — route through the same
                # delta channel rather than progress_text so it renders live.
                self.flush_thinking()  # thinking → answer boundary: keep order
                self._buffer_delta(event.text)
            elif self.show_text:
                # Push surface: deltas are dropped, so intermediate-turn
                # TextEvents are how NativeBrain narration reaches Talk. The
                # brain holds back the final turn's text (it becomes the
                # result); ClaudeCodeBrain's ResultEvent is a distinct frame.
                # Neither double-renders against the result.
                event_writer.emit("progress_text", {"text": event.text})
        elif isinstance(event, ContextManagementEvent):
            if self.is_stream_surface:
                self.flush_thinking()  # turn/CM boundary
                self.flush_deltas()  # turn/CM boundary
            event_writer.emit("context_management")

    def finish(self) -> None:
        """Both flushes, for the end of the run.

        Thinking first so its rows keep a lower seq than any trailing answer
        text.
        """
        self.flush_thinking()
        self.flush_deltas()
