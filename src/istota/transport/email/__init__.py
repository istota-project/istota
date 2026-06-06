"""Email surface (IMAP/SMTP).

This package is the home for everything email-specific that sits above the
low-level IMAP/SMTP client (``istota.skills.email`` — email's equivalent of
``istota.talk.TalkClient``):

- ``EmailTransport`` (here) — the bidirectional seam: ``poll`` (inbound) /
  ``deliver`` (outbound) / ``resolve_target``.
- ``inbound`` — the inbound body (``poll_emails`` + the routing precedence /
  attachment handling / untrusted-sender confirmation gate).
- ``outbound`` — the send body (``deliver_email_result`` + structured-output
  parsing + sent-email recording).

``deliver`` replicates the previous ``post_result_to_email`` body; the
scheduler's ``post_result_to_email`` is a thin shim over it, mirroring
``post_result_to_talk`` / ``TalkTransport.deliver``.

Email "threading" is RFC 5322 In-Reply-To / References headers, not Talk reply
ids, and email has no edit / progress-ack concept — the capabilities reflect
that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._types import IncomingMessage, TransportCapabilities
from .inbound import poll_emails
from .outbound import deliver_email_result

if TYPE_CHECKING:
    from ... import db
    from ...config import Config

__all__ = ["EmailTransport", "poll_emails", "deliver_email_result"]


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

        Like Talk, email self-creates its tasks inside ``poll_emails`` rather
        than handing un-ingested ``IncomingMessage``s back to a driver: the
        confirmation gate (``set_task_confirmation`` + the gate message) and the
        ``processed_emails`` linkage both need the freshly created task id
        mid-loop, and the create must share the inbound ``db.get_db``
        transaction so a failure rolls the batch back rather than losing mail.
        So this returns an empty ``IncomingMessage`` list — there is nothing
        left for a driver to ingest.
        """
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
        email-output file / ``ProcessedEmail`` lookup (the recipient is resolved
        from the task's email thread, so ``target`` is advisory); returns None
        (email has no platform message-id concept the core consumes)."""
        if task is None:
            return None
        await deliver_email_result(self._config, task, text)
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
        from ... import db
        with db.get_db(self._config.db_path) as conn:
            processed = db.get_email_for_task(conn, task.id)
        if processed and processed.sender_email:
            return processed.sender_email
        user_config = self._config.users.get(task.user_id)
        if user_config and user_config.email_addresses:
            return user_config.email_addresses[0]
        return None
