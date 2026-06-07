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

    ``email`` → ``"email"``; everything else (talk, briefing, scheduled,
    subtask, heartbeat, cli, istota_file, unknown) → ``"talk"``, the existing
    default. ntfy / istota_file remain fan-out side channels handled outside
    the registry, so they are not surfaces here.
    """
    if source_type == "email":
        return "email"
    return "talk"


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
    from .talk import TalkTransport

    transports: dict[str, Transport] = {}
    if config.talk.enabled:
        transports["talk"] = TalkTransport(config)
    if config.email.enabled:
        transports["email"] = EmailTransport(config)
    transports["ntfy"] = NtfyTransport(config)
    transports["istota_file"] = IstotaFileTransport(config)
    return TransportRegistry(transports)
