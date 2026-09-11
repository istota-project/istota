"""Provider-neutral SMS transport."""

from __future__ import annotations

import hashlib
import uuid
from typing import TYPE_CHECKING

from .._types import DeliveryOptions, IncomingMessage, TransportCapabilities
from .outbound import deliver_sms
from .providers.registry import SmsProviderRegistry, make_provider_registry

if TYPE_CHECKING:
    from ... import db
    from ...config import Config


def sms_conversation_token(user_id: str) -> str:
    digest = hashlib.sha256(f"istota-sms-v1\0{user_id}".encode()).hexdigest()[:24]
    return "sms-" + digest


class SmsTransport:
    name = "sms"
    capabilities = TransportCapabilities(
        supports_edit=False,
        supports_threading=False,
        supports_progress_ack=False,
        supports_typing=False,
        max_message_length=None,
        surface_class="push",
        user_routable=True,
        room_view=None,
        inbound_room_role=None,
        user_turn_mirror=None,
    )

    def __init__(
        self, config: "Config", *, providers: SmsProviderRegistry | None = None,
    ) -> None:
        self._config = config
        self._providers = providers

    def _provider_registry(self) -> SmsProviderRegistry:
        if self._providers is None:
            self._providers = make_provider_registry(self._config)
        return self._providers

    async def poll(self) -> list[IncomingMessage]:
        return []

    async def deliver(
        self, target: str, text: str, *, task: "db.Task | None" = None,
        reply_to: int | None = None, reference_id: str | None = None,
        threaded: bool = False, options: DeliveryOptions | None = None,
    ) -> int | None:
        user_id = (
            task.user_id
            if task is not None
            else self._config.find_user_by_sms_number(target)
        )
        if not user_id:
            return None
        logical_key = reference_id or (
            f"task-result:{task.id}"
            if task is not None
            else f"notification:{uuid.uuid4()}"
        )
        await deliver_sms(
            self._config, self._provider_registry(), logical_key=logical_key,
            user_id=user_id, text=text,
            task_id=task.id if task is not None else None,
        )
        return None

    async def edit(self, target: str, message_id: int, text: str) -> None:
        return None

    async def download_attachment(self, remote_ref: str, local_path: str) -> None:
        return None

    def resolve_target(self, task: "db.Task") -> str | None:
        return self._config.sms_phone_number_for(task.user_id)


__all__ = ["SmsTransport", "sms_conversation_token"]
