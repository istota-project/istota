"""Talk consumer — discrete tool visibility via message editing.

Edits a single ack message in place on each tool event (the existing "replace"
progress style, now driven by structured events instead of a string callback).
Final result delivery stays with the scheduler's ``post_result_to_talk`` as a
separate message; this subscriber only owns the in-flight progress surface and
the error/cancelled status edit.

The writer calls ``on_event`` synchronously in the execution thread. For
``NativeBrain`` that thread is an off-loop worker (the brain routes the
executor callback through ``run_in_executor`` — see ``brain/native.py``
``_emit_progress``). The Talk sends below go through ``run_coro``, which
submits to the scheduler's persistent loop on its own dedicated thread via
``run_coroutine_threadsafe`` — so they never collide with a running event loop
on the execution thread (ISSUE-111), and they reuse the shared persistent
``TalkClient`` connection pool.
"""

from __future__ import annotations

import logging
import time

from ..async_runtime import run_coro
from ..events import TaskEvent

logger = logging.getLogger("istota.consumers.talk")


class TalkEventSubscriber:
    """Delivers discrete tool events to Talk via message editing."""

    def __init__(self, config, task, ack_msg_id: int | None):
        self._config = config
        self._task = task
        self._ack_msg_id = ack_msg_id
        self._start_time = time.monotonic()
        self._descriptions: list[str] = []
        # Live text message (progress_show_text only).
        self._text_msg_id: int | None = None
        self._accumulated_texts: list[str] = []

    @property
    def descriptions(self) -> list[str]:
        """All tool descriptions seen (in arrival order)."""
        return self._descriptions

    def on_event(self, event: TaskEvent) -> None:
        kind = event.kind
        if kind == "tool_start":
            desc = event.payload.get("description", "")
            self._descriptions.append(desc)
            if self._ack_msg_id is not None and desc:
                elapsed = int(time.monotonic() - self._start_time)
                self._edit_ack(f"`{desc} ({elapsed}s)`")

        elif kind == "tool_end":
            # NativeBrain only — annotate the ack with outcome + duration.
            if self._ack_msg_id is not None and self._descriptions:
                mark = "✓" if event.payload.get("success") else "✗"
                ms = event.payload.get("duration_ms", 0)
                self._edit_ack(f"`{self._descriptions[-1]} {mark} ({ms}ms)`")

        elif kind == "progress_text":
            text = event.payload.get("text", "").strip()
            if not text:
                return
            self._accumulated_texts.append(text)
            body = "\n\n".join(self._accumulated_texts)
            if len(body) > 3900:
                body = body[:3900] + "…"
            self._post_or_edit_text(body)

        # Terminal events replace the per-tool line with a completion summary
        # (the "✅ Done — N actions (Xs)" the replace style has always shown).
        elif kind == "result":
            self._edit_summary("✅ Done")

        elif kind == "confirmation":
            self._edit_summary("⏸️ Awaiting confirmation")

        elif kind == "error":
            self._edit_summary("❌ Failed")

        elif kind == "cancelled":
            self._edit_summary("🛑 Cancelled")

    def on_finish(self) -> None:
        # Result delivery is handled by the scheduler's post_result_to_talk;
        # the ack carries the completion summary edited on the terminal event.
        pass

    def _summary_body(self, status: str) -> str:
        total = len(self._descriptions)
        elapsed = int(time.monotonic() - self._start_time)
        if total == 0:
            return f"`#{self._task.id}` {status} ({elapsed}s)"
        plural = "s" if total != 1 else ""
        return f"`#{self._task.id}` {status} — {total} action{plural} ({elapsed}s)"

    def _edit_summary(self, status: str) -> None:
        if self._ack_msg_id is not None:
            self._edit_ack(self._summary_body(status))

    # --- helpers -----------------------------------------------------------

    def _edit_ack(self, body: str) -> None:
        from ..scheduler import edit_talk_message
        try:
            run_coro(edit_talk_message(self._config, self._task, self._ack_msg_id, body))
        except Exception:
            logger.debug("Talk ack edit failed", exc_info=True)

    def _post_or_edit_text(self, body: str) -> None:
        from ..scheduler import edit_talk_message, post_result_to_talk
        try:
            if self._text_msg_id is None:
                self._text_msg_id = run_coro(post_result_to_talk(
                    self._config, self._task, body,
                    reference_id=f"istota:task:{self._task.id}:text",
                ))
            else:
                run_coro(edit_talk_message(
                    self._config, self._task, self._text_msg_id, body,
                ))
        except Exception:
            logger.debug("Talk text progress update failed", exc_info=True)
