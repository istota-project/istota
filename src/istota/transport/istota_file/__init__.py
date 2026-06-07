"""TASKS.md (istota_file) write-back surface.

A TASKS.md task's "delivery" is writing the result/status back into the source
markdown file (and an optional email notice). Folding it into the transport
abstraction removes the scheduler's direct ``handle_tasks_file_completion``
call from the delivery path. Inbound stays in ``tasks_file_poller`` — this
transport's ``poll`` returns ``[]``.

The success flag is derived from the task's terminal status at delivery time
(re-read from the DB), so the ``Transport.deliver`` signature needs no
``success`` parameter.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .._types import DeliveryOptions, IncomingMessage, TransportCapabilities

if TYPE_CHECKING:
    from ... import db
    from ...config import Config

logger = logging.getLogger("istota.transport.istota_file")

__all__ = ["IstotaFileTransport"]


class IstotaFileTransport:
    """Write-back adapter over TASKS.md files."""

    name = "istota_file"
    capabilities = TransportCapabilities(
        supports_edit=False,
        supports_threading=False,
        supports_progress_ack=False,
        supports_typing=False,
        max_message_length=None,
        surface_class="push",
    )

    def __init__(self, config: "Config"):
        self._config = config

    async def poll(self) -> list[IncomingMessage]:
        # Inbound stays in tasks_file_poller.poll_all_tasks_files.
        return []

    async def deliver(
        self, target: str, text: str, *,
        task: "db.Task | None" = None,
        reply_to: int | None = None,
        reference_id: str | None = None,
        threaded: bool = False,
        options: DeliveryOptions | None = None,
    ) -> int | None:
        """Write the result/status back to the source TASKS.md file. The success
        flag is derived from the task's current (terminal) status in the DB, so
        no success kwarg is needed. Returns None (no message-id concept)."""
        if task is None:
            return None
        from ... import db
        from ...tasks_file_poller import handle_tasks_file_completion

        with db.get_db(self._config.db_path) as conn:
            refreshed = db.get_task(conn, task.id)
        status = refreshed.status if refreshed else task.status
        success = status == "completed"
        handle_tasks_file_completion(self._config, task, success, text)
        return None

    async def edit(self, target: str, message_id: int, text: str) -> None:
        return None

    async def download_attachment(self, remote_ref: str, local_path: str) -> None:
        return None

    def resolve_target(self, task: "db.Task") -> str | None:
        """The source TASKS.md file path for this task, or None if no record."""
        from ... import db
        with db.get_db(self._config.db_path) as conn:
            record = db.get_istota_file_task_by_task_id(conn, task.id)
        return record.file_path if record else None
