"""Log channel consumer — verbose per-user execution log.

Accumulates tool descriptions and streams them to the user's resolved log
destinations. On **edit-capable** surfaces (Talk) a single message is edited in
place on each ``tool_start`` (no rate limiting — existing behavior). **Non-edit**
surfaces (email, ntfy) get nothing during the run; the scheduler's
``_finalize_log_channel`` delivers them a single final-summary message instead of
per-tool spam.

The scheduler's ``_finalize_log_channel`` reads ``all_descriptions`` /
``delivery_state`` off this subscriber after the run to edit the in-flight
edit-capable messages into their final state and to deliver the footer to every
remaining destination, so those attributes are part of the public surface.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..async_runtime import run_coro
from ..events import TaskEvent
from ..transport import make_registry

if TYPE_CHECKING:
    from ..transport import Destination

logger = logging.getLogger("istota.consumers.log_channel")


class LogChannelSubscriber:
    """Streams every tool call to the log channel's edit-capable destinations."""

    def __init__(self, config, task, log_dests: "list[Destination]", prefix, registry=None):
        self._config = config
        self._task = task
        self._dests = log_dests
        self._prefix = prefix
        self._registry = registry or make_registry(config)
        self.all_descriptions: list[str] = []
        # Per-destination in-flight message id for edit-capable surfaces, keyed
        # by (surface, channel). None / absent → not yet posted. Read back by
        # _finalize_log_channel to edit each streamed message into final state.
        self.delivery_state: dict[tuple[str, str | None], int | None] = {}

    def on_event(self, event: TaskEvent) -> None:
        if event.kind != "tool_start":
            return
        desc = event.payload.get("description", "")
        if not desc:
            return
        # Collapse multi-line tool output (e.g. inline scripts) to one line.
        self.all_descriptions.append(desc.replace("\n", " ").strip())

        from ..scheduler import _format_log_channel_body

        body = _format_log_channel_body(self._prefix, self.all_descriptions)

        for dest in self._dests:
            transport = self._registry.get(dest.surface)
            if transport is None:
                continue
            # Only edit-capable surfaces stream live; non-edit surfaces receive
            # a single final summary from _finalize_log_channel.
            if not transport.capabilities.supports_edit:
                continue
            key = (dest.surface, dest.channel)
            try:
                if self.delivery_state.get(key) is None:
                    self.delivery_state[key] = run_coro(transport.deliver(
                        dest.channel, body, task=self._task,
                        reference_id=f"istota:log:{self._task.id}",
                    ))
                else:
                    run_coro(transport.edit(
                        dest.channel, self.delivery_state[key], body,
                    ))
            except Exception:
                logger.debug(
                    "Log channel update failed for task %d dest %s",
                    self._task.id, dest.surface, exc_info=True,
                )

    def on_finish(self) -> None:
        # Final summary is posted by the scheduler's _finalize_log_channel,
        # which needs success/skills/model context this subscriber lacks.
        pass
