"""Provider-neutral SMS transport."""

from __future__ import annotations

import hashlib
import uuid
from typing import TYPE_CHECKING

from .._types import DeliveryOptions, IncomingMessage, TransportCapabilities
from .outbound import deliver_sms
from .providers.registry import SmsProviderRegistry

if TYPE_CHECKING:
    from ... import db
    from ...config import Config
    from ._types import SmsDeliveryRecord


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
        """Build the adapter registry once per transport, and cache it.

        Resolved through the `providers.registry` module rather than a name
        bound at import time, so there is exactly one place to patch and one
        place the factory can be reached from. Binding it here as well gave the
        suite two targets for one factory, and a test patching the other one
        went inert without failing.
        """
        if self._providers is None:
            from .providers import registry as provider_registry
            self._providers = provider_registry.make_provider_registry(self._config)
        return self._providers

    async def poll(self) -> list[IncomingMessage]:
        return []

    async def deliver(
        self, target: str, text: str, *, task: "db.Task | None" = None,
        reply_to: int | None = None, reference_id: str | None = None,
        threaded: bool = False, options: DeliveryOptions | None = None,
    ) -> int | None:
        await self.send_record(
            target, text, task=task, reference_id=reference_id,
        )
        return None

    async def send_record(
        self, target: str, text: str, *, task: "db.Task | None" = None,
        user_id: str | None = None, reference_id: str | None = None,
        preferred_from_number: str | None = None,
    ) -> "SmsDeliveryRecord | None":
        """`deliver`, returning the ledger record instead of a message id.

        The `Transport.deliver` contract returns the surface's own message id,
        and SMS has none — so the `SmsDeliveryRecord` had nowhere to go and was
        dropped. The scheduler's owed-confirmation arm has to read it to know
        whether the question reached anybody, so it calls this instead. `None`
        means no user could be resolved, which is the one case that writes no
        ledger row at all.

        `reference_id` is the caller's stable logical id, and supplying one is
        what makes a repeated send cost nothing. The random fallback is honest
        — with no key there is nothing to deduplicate against — but it is not
        free: every caller without one mints a fresh ledger row and a fresh
        paid message.
        """
        # Explicit first, then the task, and only then the number. A caller
        # that already knows the user must not be made to reverse-lookup one
        # from a number it does not have — `find_user_by_sms_number("")` is a
        # question with no good answer.
        if not user_id:
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
        return await deliver_sms(
            self._config, self._provider_registry(), logical_key=logical_key,
            user_id=user_id, text=text,
            task_id=task.id if task is not None else None,
            preferred_from_number=preferred_from_number,
        )

    async def edit(self, target: str, message_id: int, text: str) -> None:
        return None

    async def download_attachment(self, remote_ref: str, local_path: str) -> None:
        return None

    def resolve_target(self, task: "db.Task") -> str | None:
        return self._config.sms_phone_number_for(task.user_id)


__all__ = ["SmsTransport", "sms_conversation_token"]
