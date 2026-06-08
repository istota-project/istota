"""Terminal renderer for REPL task events.

``TerminalSubscriber`` is an ``EventSubscriber`` that renders a task's
``TaskEvent`` stream to stdout: tool starts/ends, progress text, and the final
result / error / cancelled notice. The dispatch is over ``TaskEvent`` kinds
(the persisted event log), distinct from ``scripts/native_repl.py`` whose
dispatch is over the brain's raw ``StreamEvent`` types — only the ANSI palette
is shared in spirit.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..events import TaskEvent

# ANSI palette (matches scripts/native_repl.py's vocabulary).
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[90m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"


class TerminalSubscriber:
    """Render a task's event stream to a terminal."""

    def __init__(self, *, color: bool | None = None, stream=None):
        self._stream = stream if stream is not None else sys.stdout
        if color is None:
            color = bool(getattr(self._stream, "isatty", lambda: False)())
        self._color = color
        # Live answer text streamed via text_delta events (stream surface). We
        # write deltas inline (no newline) so the answer appears progressively;
        # _streamed accumulates them so the terminal `result` can skip a
        # redundant re-print when it matches what already streamed.
        self._streamed = ""
        self._mid_line = False  # an un-terminated delta line is open

    def _c(self, code: str, text: str) -> str:
        return f"{code}{text}{_RESET}" if self._color else text

    def _print(self, text: str = "") -> None:
        # Terminate any open in-flight delta line before a block print so the
        # two don't run together on one line.
        if self._mid_line:
            print(file=self._stream, flush=True)
            self._mid_line = False
        print(text, file=self._stream, flush=True)

    def _write_inline(self, text: str) -> None:
        self._stream.write(text)
        self._stream.flush()
        self._mid_line = True

    def on_event(self, event: "TaskEvent") -> None:
        kind = event.kind
        p = event.payload or {}
        if kind == "text_delta":
            text = p.get("text") or ""
            if text:
                self._streamed += text
                self._write_inline(text)
            return
        if kind == "tool_start":
            desc = p.get("description") or p.get("tool_name") or "tool"
            self._print(self._c(_CYAN, f"  ▸ {desc}"))
        elif kind == "tool_end":
            if p.get("success") is False:
                name = p.get("tool_name") or "tool"
                self._print(self._c(_RED, f"  ✗ {name} failed"))
        elif kind == "tool_progress":
            text = (p.get("text") or "").rstrip()
            if text:
                self._print(self._c(_DIM, f"    {text}"))
        elif kind == "progress_text":
            text = (p.get("text") or "").rstrip()
            if text:
                self._print(text)
        elif kind == "result":
            final = p.get("text") or ""
            if self._streamed and final.strip() == self._streamed.strip():
                # Already shown live via deltas — just terminate the line rather
                # than re-printing the whole answer in green.
                self._print()
            else:
                # No deltas, or CM-aware composition rewrote the answer: print
                # the canonical (corrected) result.
                self._print()
                self._print(self._c(_GREEN, final))
            if p.get("truncated"):
                self._print(self._c(_DIM, "[result truncated]"))
            self._streamed = ""
        elif kind == "error":
            self._print()
            self._print(self._c(_RED, f"error: {p.get('message') or 'task failed'}"))
        elif kind == "cancelled":
            self._print(self._c(_YELLOW, "cancelled"))
        elif kind == "confirmation":
            self._print()
            self._print(self._c(_YELLOW, p.get("prompt") or "(confirmation requested)"))

    def on_finish(self) -> None:
        """No-op: the terminal renders everything live via ``on_event``.

        Part of the ``EventSubscriber`` contract — ``EventWriter.finish()`` calls
        it on every subscriber. Defined explicitly so the call doesn't raise an
        ``AttributeError`` that ``finish()`` would silently swallow.
        """
        return None
