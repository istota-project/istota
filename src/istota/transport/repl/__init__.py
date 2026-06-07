"""REPL surface — a ``stream``-class transport.

The terminal REPL can't be a *push* target: the daemon in one process can't
write to a terminal owned by another. Its outbound is the ``task_events``
stream, which the REPL's own ``TerminalSubscriber`` renders. So ``deliver`` is a
no-op and ``resolve_target`` returns the sentinel ``"stream"`` — the
``result``/``error``/``done`` events satisfy the destination. Web chat works the
same way.

Registered in ``make_registry`` so ``output_target="stream"`` /
``source_type="repl"`` route here; the REPL itself runs tasks inline via
``scheduler.run_task_inline`` (no daemon needed).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._types import DeliveryOptions, IncomingMessage, TransportCapabilities

if TYPE_CHECKING:
    from ... import db
    from ...config import Config

__all__ = ["ReplTransport"]


class ReplTransport:
    """Events-only adapter for the terminal REPL (and future web chat)."""

    name = "repl"
    capabilities = TransportCapabilities(
        supports_edit=True,
        supports_threading=False,
        supports_progress_ack=False,
        supports_typing=False,
        max_message_length=None,
        surface_class="stream",
    )

    def __init__(self, config: "Config"):
        self._config = config

    async def poll(self) -> list[IncomingMessage]:
        return []

    async def deliver(
        self, target: str, text: str, *,
        task: "db.Task | None" = None,
        reply_to: int | None = None,
        reference_id: str | None = None,
        threaded: bool = False,
        options: DeliveryOptions | None = None,
    ) -> int | None:
        # Stream surface: the client tails task_events; nothing to push.
        return None

    async def edit(self, target: str, message_id: int, text: str) -> None:
        return None

    async def download_attachment(self, remote_ref: str, local_path: str) -> None:
        return None

    def resolve_target(self, task: "db.Task") -> str | None:
        return "stream"
