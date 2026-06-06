"""Log channel consumer — verbose per-user execution log.

Accumulates tool descriptions and edits a single log-channel message in place
on each ``tool_start`` (no rate limiting — existing behavior). The scheduler's
``_finalize_log_channel`` reads ``all_descriptions`` / ``log_msg_id`` off this
subscriber after delivery to post the final status/skills/model footer, so
those attributes are part of the public surface.
"""

from __future__ import annotations

import logging

from .. import db
from ..async_runtime import run_coro
from ..events import TaskEvent

logger = logging.getLogger("istota.consumers.log_channel")


class LogChannelSubscriber:
    """Streams every tool call to the log channel via message editing."""

    def __init__(self, config, task, log_channel: str, prefix):
        self._config = config
        self._task = task
        self._channel = log_channel
        self._prefix = prefix
        self.all_descriptions: list[str] = []
        self.log_msg_id: list[int | None] = [None]

    def on_event(self, event: TaskEvent) -> None:
        if event.kind != "tool_start":
            return
        desc = event.payload.get("description", "")
        if not desc:
            return
        # Collapse multi-line tool output (e.g. inline scripts) to one line.
        self.all_descriptions.append(desc.replace("\n", " ").strip())

        from ..scheduler import _format_log_channel_body, edit_talk_message
        from ..transport.talk import TalkTransport

        body = _format_log_channel_body(self._prefix, self.all_descriptions)
        try:
            if self.log_msg_id[0] is None:
                # The log channel is always a Talk room today; deliver through
                # the Talk transport rather than constructing TalkClient here.
                self.log_msg_id[0] = run_coro(TalkTransport(self._config).deliver(
                    self._channel, body,
                    reference_id=f"istota:log:{self._task.id}",
                ))
            else:
                run_coro(edit_talk_message(
                    self._config,
                    db.Task(
                        id=self._task.id, status="running",
                        source_type=self._task.source_type,
                        user_id=self._task.user_id, prompt="",
                        conversation_token=self._channel,
                    ),
                    self.log_msg_id[0], body,
                ))
        except Exception:
            logger.debug(
                "Log channel update failed for task %d", self._task.id, exc_info=True
            )

    def on_finish(self) -> None:
        # Final summary is posted by the scheduler's _finalize_log_channel,
        # which needs success/skills/model context this subscriber lacks.
        pass
