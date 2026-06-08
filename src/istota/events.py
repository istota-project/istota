"""Task event streaming — a single event infrastructure for every output surface.

The executor produces a typed, persisted stream of ``TaskEvent``s per task.
Consumers (Talk, log channel, push, web SSE, admin) each read that one stream
and format it for their surface — no callback composition, no per-consumer
state threaded through function attributes.

Three layers live here:

1. ``TaskEvent`` — the executor's view of task lifecycle (higher-level than a
   brain's internal events, lower-level than consumer-formatted output).
2. ``EventWriter`` — writes events to the ``task_events`` table (WAL, shared by
   the scheduler and web processes) and notifies in-process subscribers.
3. ``EventSubscriber`` — the consumer protocol. The writer calls ``on_event``
   synchronously in the execution thread; a consumer that needs async delivery
   uses ``asyncio.run`` (the brain guarantees notification runs off any event
   loop — see ``brain/native.py`` ``_emit_progress`` and the spec's Layer 3
   invariant).

The SSE and admin consumers are NOT in-process subscribers: they poll
``task_events`` from the web process. The table is the bus — no IPC, no pubsub.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

logger = logging.getLogger("istota.events")

PAYLOAD_MAX_BYTES = 8192

# Generic "working on it" progress verbs, surface-agnostic (no markup) so every
# output surface draws from one list. The executor stamps one onto the
# ``task_started`` event so a stream surface (web chat) shows a real verb instead
# of a hardcoded placeholder; the Talk ack picks one too (and italicizes it
# itself). Tool-specific progress comes from ``agent.events._describe_tool_use``,
# not here.
PROGRESS_MESSAGES = [
    "On it...",
    "Hmm...",
    "Heard, chef...",
    "Investigating...",
    "One sec...",
    "Copy that...",
    "Roger...",
    "Considering...",
    "Thinkifying...",
    "Braining...",
    "Improvising...",
    "Jamming...",
    "Riffing...",
    "Grooving...",
    "Beboppin'...",
    "Noodling...",
    "Syncopating...",
    "Comping...",
    "Soloing...",
    # Cephalopod
    "Inking...",
    "Tentacling...",
    "Suckering...",
    "Jetting...",
    "Unfurling...",
    "Chromatophoring...",
    "Squidding...",
    "Grasping...",
    "Probing...",
    "Siphoning...",
    # Cheeky
    "Instigating...",
    "Scheming...",
    "Concocting...",
    "Percolating...",
    "Marinating...",
    "Hatching...",
    "Sleuthing...",
    "Finagling...",
    "Wrangling...",
    "Tinkering...",
    "Rummaging...",
    "Conjuring...",
    "Fermenting...",
    "Machinating...",
    "Gallivanting...",
]


def random_progress_message() -> str:
    """A random surface-agnostic progress verb (plain text, no markup)."""
    return random.choice(PROGRESS_MESSAGES)

EventKind = Literal[
    "task_started",        # executor picked up the task
    "tool_start",          # brain started a tool call
    "tool_end",            # tool call completed (success or failure) — NativeBrain only
    "tool_progress",       # incremental tool output mid-execution — NativeBrain only
    "progress_text",       # intermediate text output from the brain
    "text_delta",          # incremental answer text — stream surfaces only, pruned after result
    "context_management",  # brain compacted context
    "confirmation",        # task paused for user confirmation
    "result",              # final output text
    "error",               # task failed
    "cancelled",           # task was cancelled
    "done",                # terminal event (always last)
]


def _utc_now() -> str:
    """ISO 8601 UTC with millisecond precision and a Z suffix.

    Matches the sortable shape the ``task_events.created_at`` DB default
    produces (``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')``).
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass(frozen=True)
class TaskEvent:
    """One event in a task's lifecycle.

    Persisted to the ``task_events`` table. Consumers receive these via the
    event writer and format them for their output surface.

    Payload schemas per kind:

      task_started:        {}
      tool_start:          {"tool_name", "description", "tool_call_id"}
      tool_end:            {"tool_name", "tool_call_id", "success", "duration_ms"}
      tool_progress:       {"tool_name", "tool_call_id", "text"}
      progress_text:       {"text"}
      text_delta:          {"text"}
      context_management:  {}
      confirmation:        {"prompt"}
      result:              {"text", "truncated"}
      error:               {"message", "stop_reason"}
      cancelled:           {}
      done:                {"stop_reason", "duration_seconds"}
    """

    task_id: int
    seq: int                  # monotonic per task_id, assigned by the writer
    kind: EventKind
    payload: dict[str, Any]   # kind-specific structured data
    created_at: str           # ISO 8601 UTC


class EventSubscriber(Protocol):
    """Consumer interface for task events."""

    def on_event(self, event: TaskEvent) -> None:
        """Handle a single event. Must not block for long."""
        ...

    def on_finish(self) -> None:
        """Called after the terminal event. Clean up resources."""
        ...


class EventWriter:
    """Writes TaskEvents to the event log and notifies in-process consumers.

    One instance per task execution. ``emit()`` is synchronous: it inserts one
    row, then calls each subscriber's ``on_event`` in line. For ``NativeBrain``
    (events originate on an event loop) the brain dispatches the executor's
    callback — and therefore this whole ``emit`` chain — off the loop via
    ``run_in_executor``, so the synchronous Talk/log subscribers can call
    ``asyncio.run`` without colliding with a running loop (ISSUE-111).
    """

    def __init__(self, task_id: int, db_path: str, *, enabled: bool = True):
        self._task_id = task_id
        self._db_path = db_path
        # Operational kill-switch (config.scheduler.event_log_enabled). When
        # False the DB write is skipped but subscribers are still notified, so
        # Talk progress keeps working with the table offline.
        self._enabled = enabled
        self._seq = 0
        self._start_time = time.monotonic()
        self._subscribers: list[EventSubscriber] = []

    def subscribe(self, subscriber: EventSubscriber) -> None:
        self._subscribers.append(subscriber)

    @property
    def subscribers(self) -> list[EventSubscriber]:
        return self._subscribers

    def emit(self, kind: EventKind, payload: dict | None = None) -> TaskEvent:
        self._seq += 1
        payload = dict(payload) if payload else {}

        # Enforce payload size cap.
        payload_json = json.dumps(payload, default=str)
        if len(payload_json.encode()) > PAYLOAD_MAX_BYTES:
            payload["_truncated"] = True
            for key in ("text", "summary", "prompt", "message", "description"):
                if key in payload and isinstance(payload[key], str):
                    payload[key] = payload[key][:2000] + "… [truncated]"
            payload_json = json.dumps(payload, default=str)

        event = TaskEvent(
            task_id=self._task_id,
            seq=self._seq,
            kind=kind,
            payload=payload,
            created_at=_utc_now(),
        )

        # Persist (best-effort — never let a DB hiccup break execution).
        if self._enabled:
            try:
                self._write_to_db(event, payload_json)
            except Exception:
                logger.warning(
                    "Failed to persist event %s seq=%d", kind, self._seq, exc_info=True
                )

        # Notify subscribers (best-effort, never block execution on a failure).
        for sub in self._subscribers:
            try:
                sub.on_event(event)
            except Exception:
                logger.debug(
                    "Subscriber %s failed on event %s",
                    type(sub).__name__, kind, exc_info=True,
                )

        return event

    def finish(self) -> None:
        """Notify every subscriber that the terminal event has been emitted."""
        for sub in self._subscribers:
            try:
                sub.on_finish()
            except Exception:
                logger.debug(
                    "Subscriber %s failed on finish", type(sub).__name__, exc_info=True
                )

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start_time

    def _write_to_db(self, event: TaskEvent, payload_json: str) -> None:
        from . import db
        with db.get_db(self._db_path) as conn:
            conn.execute(
                "INSERT INTO task_events (task_id, seq, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (event.task_id, event.seq, event.kind, payload_json, event.created_at),
            )
