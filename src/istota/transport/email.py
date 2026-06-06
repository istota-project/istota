"""EmailTransport — IMAP/SMTP surface adapter.

Stage 1 is a thin seam: ``deliver`` delegates to the existing
``post_result_to_email`` and ``resolve_target`` resolves the email reply
recipient, while ``poll`` gains its real body (moved from ``poll_emails``,
producing ``IncomingMessage``) in a later stage. Until then the scheduler's
email tick still calls ``poll_emails`` directly.

Email "threading" is RFC 5322 In-Reply-To / References headers, not Talk reply
ids, and email has no edit / progress-ack concept — the capabilities reflect
that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._types import IncomingMessage, TransportCapabilities

if TYPE_CHECKING:
    from .. import db
    from ..config import Config


class EmailTransport:
    """Bidirectional adapter over IMAP/SMTP email."""

    name = "email"
    capabilities = TransportCapabilities(
        supports_edit=False,
        supports_threading=True,
        supports_progress_ack=False,
        supports_typing=False,
        max_message_length=None,
    )

    def __init__(self, config: "Config"):
        self._config = config

    async def poll(self) -> list[IncomingMessage]:
        """Poll IMAP and create email tasks.

        Unlike Talk, email cannot use the ``collect → ingest_message`` split:
        ``poll_emails`` needs the freshly-created task id mid-loop for the
        untrusted-sender confirmation gate (``set_task_confirmation`` + posting
        the gate message) and for linking the task into ``processed_emails``. So
        email self-creates its tasks here and returns an empty
        ``IncomingMessage`` list — there is nothing left for a driver to ingest.
        The scheduler's email tick may call this or ``poll_emails`` directly;
        both create the same tasks.
        """
        from ..email_poller import poll_emails
        poll_emails(self._config)
        return []

    async def deliver(
        self, target: str, text: str, *,
        task: "db.Task | None" = None,
        reply_to: int | None = None,
        reference_id: str | None = None,
        threaded: bool = False,
    ) -> int | None:
        """Send a task result via email. Requires ``task`` for the deferred
        email-output file / ``ProcessedEmail`` lookup; returns None (email has
        no platform message-id concept the core consumes)."""
        if task is None:
            return None
        from ..scheduler import post_result_to_email
        await post_result_to_email(self._config, task, text)
        return None

    async def edit(self, target: str, message_id: int, text: str) -> None:
        # Email has no edit concept.
        return None

    async def download_attachment(self, remote_ref: str, local_path: str) -> None:
        # Inbound attachments are downloaded during poll(), not on demand.
        return None

    def resolve_target(self, task: "db.Task") -> str | None:
        """Resolve the email reply recipient for a task. Returns the sender of
        the original email when this is a reply, else the user's own address."""
        from .. import db
        with db.get_db(self._config.db_path) as conn:
            processed = db.get_email_for_task(conn, task.id)
        if processed and processed.sender_email:
            return processed.sender_email
        user_config = self._config.users.get(task.user_id)
        if user_config and user_config.email_addresses:
            return user_config.email_addresses[0]
        return None
