"""Transport registry — built once at scheduler startup, keyed by surface name.

The registry centralizes the ``source_type → surface`` mapping that today is
scattered through ``process_one_task`` and resolves the delivery transport for
a task. Construction does no I/O (``TalkClient.__init__`` only stores
credentials), so callers without a registry in scope — notably
``notifications.send_notification`` — can build one on demand cheaply.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._types import Transport

if TYPE_CHECKING:
    from .. import db
    from ..config import Config


def _surface_for_source_type(source_type: str) -> str:
    """Map a task's ``source_type`` to the delivery surface name.

    ``email`` → ``"email"``; ``repl`` → ``"repl"``; ``web`` → ``"web"`` (a
    stream surface — ``for_task`` resolves it to ``WebTransport``, but an
    interactive web task's *result* still streams over the task_events log, not
    a push: ``resolve_delivery_plan`` short-circuits ``web`` to a stream
    destination, and ``WebTransport`` advertises no progress-ack. The transport's
    ``deliver`` is used only by the routing paths — alerts / log / notifications
    routed to ``web``). Everything else (talk, briefing, scheduled, subtask,
    heartbeat, cli, istota_file, unknown) → ``"talk"``, the existing default.
    """
    if source_type == "email":
        return "email"
    if source_type == "repl":
        return "repl"
    if source_type == "web":
        return "web"
    return "talk"


def task_is_stream_surface(config: "Config", task: "db.Task") -> bool:
    """True when the task's primary surface is stream-class (``web``, ``repl``,
    and any future stream transport).

    The single predicate the executor's delta coalescer and the scheduler's
    delta-prune both gate on, so "which surfaces stream" lives in one place
    (the transport's ``surface_class``) rather than a hardcoded set. Building a
    registry here is cheap — ``make_registry`` does no I/O."""
    transport = make_registry(config).for_task(task)
    return (
        transport is not None
        and getattr(transport.capabilities, "surface_class", "push") == "stream"
    )


class TransportRegistry:
    """Holds the enabled transports and resolves one for a task."""

    def __init__(self, transports: dict[str, Transport]):
        self._by_name = transports

    def get(self, name: str) -> Transport | None:
        return self._by_name.get(name)

    def for_task(self, task: "db.Task") -> Transport | None:
        """Resolve the primary delivery transport for a task by surface name
        derived from ``source_type``. Returns None if that surface is disabled."""
        return self._by_name.get(_surface_for_source_type(task.source_type))

    def pollers(self) -> list[Transport]:
        """Transports that participate in inbound polling (all registered
        transports — each owns its own cadence)."""
        return list(self._by_name.values())

    def names(self) -> list[str]:
        return list(self._by_name.keys())

    def routable_names(self) -> list[str]:
        """Names of surfaces a user can deliberately route to (a briefing
        output, a default destination, an alert route). Excludes self-routing
        surfaces (``istota_file``, ``repl``) whose ``user_routable`` is False —
        they still validate on the wire, they're just never UI-offered."""
        return [
            name for name, t in self._by_name.items()
            if getattr(t.capabilities, "user_routable", True)
        ]


def make_registry(config: "Config") -> TransportRegistry:
    """Build the registry from config. No network on construction.

    Talk is registered when ``talk.enabled`` and email when ``email.enabled``;
    ntfy and istota_file are registered unconditionally (per-user gating happens
    in their ``resolve_target`` / ``deliver``, not at construction). Adding
    Matrix or web chat is one more ``if`` here plus the transport class.
    """
    from .email import EmailTransport
    from .istota_file import IstotaFileTransport
    from .ntfy import NtfyTransport
    from .repl import ReplTransport
    from .talk import TalkTransport
    from .web import WebTransport

    transports: dict[str, Transport] = {}
    if config.talk.enabled:
        transports["talk"] = TalkTransport(config)
    if config.email.enabled:
        transports["email"] = EmailTransport(config)
    transports["ntfy"] = NtfyTransport(config)
    transports["istota_file"] = IstotaFileTransport(config)
    transports["repl"] = ReplTransport(config)
    transports["web"] = WebTransport(config)
    return TransportRegistry(transports)
