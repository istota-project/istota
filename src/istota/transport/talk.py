"""TalkTransport — Nextcloud Talk surface adapter.

Stage 1 is a thin seam: ``deliver`` / ``edit`` / ``resolve_target`` delegate to
the existing scheduler functions so no behaviour moves yet, while the new types
and registry are wired and tested. ``poll`` gains its real body (the
``poll_talk_conversations`` logic, producing ``IncomingMessage`` instead of
creating tasks inline) in a later stage; until then the scheduler's Talk
poll driver still calls ``poll_talk_conversations`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._types import IncomingMessage, TransportCapabilities

if TYPE_CHECKING:
    from .. import db
    from ..config import Config


class TalkTransport:
    """Bidirectional adapter over Nextcloud Talk."""

    name = "talk"
    capabilities = TransportCapabilities(
        supports_edit=True,
        supports_threading=True,
        supports_progress_ack=True,
        supports_typing=True,
        max_message_length=32000,
    )

    def __init__(self, config: "Config"):
        self._config = config

    async def poll(self) -> list[IncomingMessage]:
        # Real body lands in Stage 3 (moved from poll_talk_conversations).
        # Until the driver is switched, this is unused.
        return []

    async def deliver(
        self, target: str, text: str, *,
        task: "db.Task | None" = None,
        reply_to: int | None = None,
        reference_id: str | None = None,
        threaded: bool = False,
    ) -> int | None:
        from ..scheduler import post_result_to_talk
        if task is not None:
            return await post_result_to_talk(
                self._config, task, text,
                use_reply_threading=threaded,
                reference_id=reference_id,
                target_token=target,
            )
        # No task in scope (notification path): direct split + send.
        from ..talk import TalkClient, split_message
        if not self._config.nextcloud.url or not target:
            return None
        try:
            client = TalkClient(self._config)
            msg_id = None
            for part in split_message(text):
                response = await client.send_message(
                    target, part, reply_to=reply_to, reference_id=reference_id,
                )
                msg_id = response.get("ocs", {}).get("data", {}).get("id")
            return msg_id
        except Exception:
            return None

    async def edit(self, target: str, message_id: int, text: str) -> None:
        from ..scheduler import edit_talk_message
        from .. import db
        # edit_talk_message keys off task.conversation_token; build a minimal
        # task carrying the target room.
        shim = db.Task(
            id=0, status="running", source_type="talk",
            user_id="", prompt="", conversation_token=target,
        )
        await edit_talk_message(self._config, shim, message_id, text)

    async def download_attachment(self, remote_ref: str, local_path: str) -> None:
        from ..talk import TalkClient
        client = TalkClient(self._config)
        await client.download_attachment(remote_ref, local_path)

    def resolve_target(self, task: "db.Task") -> str | None:
        from ..scheduler import _talk_target_for_delivery
        return _talk_target_for_delivery(self._config, task)
