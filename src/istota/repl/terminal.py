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

    def _c(self, code: str, text: str) -> str:
        return f"{code}{text}{_RESET}" if self._color else text

    def _print(self, text: str = "") -> None:
        print(text, file=self._stream, flush=True)

    def on_event(self, event: "TaskEvent") -> None:
        kind = event.kind
        p = event.payload or {}
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
            self._print()
            self._print(self._c(_GREEN, p.get("text") or ""))
            if p.get("truncated"):
                self._print(self._c(_DIM, "[result truncated]"))
        elif kind == "error":
            self._print()
            self._print(self._c(_RED, f"error: {p.get('message') or 'task failed'}"))
        elif kind == "cancelled":
            self._print(self._c(_YELLOW, "cancelled"))
        elif kind == "confirmation":
            self._print()
            self._print(self._c(_YELLOW, p.get("prompt") or "(confirmation requested)"))
