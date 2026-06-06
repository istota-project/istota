"""Push notification consumer — fires when a long-running task finishes.

Short tasks don't need a ping (the user is likely still watching the screen),
so the notification only fires when the task ran longer than
``push_notification_threshold_seconds``. Gated by source type
(``push_notification_sources``) at construction time by the scheduler.
"""

from __future__ import annotations

import logging
import time

from ..events import TaskEvent

logger = logging.getLogger("istota.consumers.push")


class PushNotificationSubscriber:
    """Sends an ntfy push notification when a long task finishes."""

    def __init__(self, config, task, threshold_seconds: float = 30.0):
        self._config = config
        self._task = task
        self._threshold = threshold_seconds
        self._started_at = time.monotonic()
        self._final_event: TaskEvent | None = None

    def on_event(self, event: TaskEvent) -> None:
        if event.kind in ("result", "error", "cancelled"):
            self._final_event = event

    def on_finish(self) -> None:
        elapsed = time.monotonic() - self._started_at
        if elapsed < self._threshold:
            return
        if self._final_event is None:
            return

        kind = self._final_event.kind
        if kind == "result":
            title = f"Task #{self._task.id} completed"
            body = self._final_event.payload.get("text", "")[:100]
        elif kind == "error":
            title = f"Task #{self._task.id} failed"
            body = self._final_event.payload.get("message", "")[:100]
        else:
            title = f"Task #{self._task.id} cancelled"
            body = ""

        try:
            from ..notifications import send_notification
            send_notification(
                self._config, self._task.user_id, body or title,
                surface="ntfy", title=title,
            )
        except Exception:
            logger.debug("Push notification failed", exc_info=True)
