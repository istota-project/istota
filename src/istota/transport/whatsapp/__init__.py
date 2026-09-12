"""WhatsApp through Meta's hosted Cloud API.

A direct external conversation, never a view of a room: nothing here creates or
joins a room, writes a canonical `messages` row, or mirrors a turn into Talk or
web chat. The room facts on `WhatsAppTransport.capabilities` and in
`surfaces.SURFACES` are all `None`, which is what keeps the surface out of the
room model with no special case anywhere in `ingest` or the planner.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import TYPE_CHECKING

from .._types import DeliveryOptions, IncomingMessage, TransportCapabilities
from ._types import (
    LOCAL_TERMINAL_STATES,
    REACHED_META,
    InboundWhatsAppEvent,
    WhatsAppDeliveryEvent,
    WhatsAppDeliveryStatus,
    WhatsAppEvent,
    WhatsAppMessageKind,
    WhatsAppSendFailure,
    WhatsAppSendOutcome,
    WhatsAppSendRequest,
    WhatsAppSendResult,
    WhatsAppUserIdentity,
)

if TYPE_CHECKING:
    from ... import db
    from ...config import Config
    from ._types import WhatsAppDeliveryRecord


def whatsapp_conversation_token(user_id: str) -> str:
    """One stable conversation token per Istota user.

    Derived from the Istota user id alone, so it survives a number change, a
    username change, a re-enrollment and a new send id — the four things that
    move underneath a WhatsApp identity — and the task-history fallback keeps
    working across all of them. It carries no Meta identifier and no phone
    number, because a conversation token reaches task rows, log lines and the
    admin task views, none of which are private operator surfaces.

    Not a room token: nothing registers it, and `transcript_room` finds no
    registered room for it, which is what keeps the whole surface out of the
    room model without a special case anywhere in `ingest`.
    """
    digest = hashlib.sha256(f"istota-whatsapp-v1\0{user_id}".encode()).hexdigest()
    return "whatsapp-" + digest[:24]


class WhatsAppTransport:
    """The `whatsapp` surface: an interactive, user-routable push transport.

    Every room fact is `None`, and that is the declaration rather than an
    omission — a WhatsApp exchange is its own external conversation, not a
    second view of a Talk or web room, so it never creates or joins one, writes
    no canonical `messages` row and is never mirrored into a transcript.

    `max_message_length` is `None` too, and for the opposite kind of reason: the
    limit exists (4,096 characters) but the generic splitter must not apply it.
    Splitting one answer into three messages multiplies the per-message cost
    and gives the recipient three chances to read a third of an answer, so
    `render_whatsapp` caps one message instead.
    """

    name = "whatsapp"
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

    def __init__(self, config: "Config") -> None:
        self._config = config

    async def poll(self) -> list[IncomingMessage]:
        """Nothing. Inbound arrives as a signed webhook, not by polling."""
        return []

    async def deliver(
        self, target: str, text: str, *, task: "db.Task | None" = None,
        reply_to: int | None = None, reference_id: str | None = None,
        threaded: bool = False, options: DeliveryOptions | None = None,
    ) -> int | None:
        await self.send_record(
            target, text, task=task, reference_id=reference_id,
        )
        # No surface-native message id of our own: Meta's belongs to the
        # ledger row, and returning it here would invite a caller to edit or
        # reply to it on a surface that supports neither.
        return None

    async def send_record(
        self, target: str, text: str, *, task: "db.Task | None" = None,
        user_id: str | None = None, reference_id: str | None = None,
        buttons: tuple[tuple[str, str], ...] = (),
        ignore_opt_out: bool = False,
    ) -> "WhatsAppDeliveryRecord | None":
        """`deliver`, returning the ledger record instead of a message id.

        The scheduler's owed-confirmation arm has to know whether the question
        reached anybody, and `Transport.deliver`'s contract has nowhere to put
        that. `None` means no user could be resolved, which is the one case
        that writes no ledger row at all.

        `target` is accepted and ignored, deliberately. It is the planner's
        advisory channel — the stable conversation token, never a Meta
        identifier — and the destination is resolved from the user's *current*
        binding inside `deliver_whatsapp`, immediately before the call.

        `reference_id` is the caller's stable logical id and is what makes a
        repeated send cost nothing. The random fallback is honest — with no key
        there is nothing to deduplicate against — but every caller without one
        mints a fresh ledger row and, on a metered surface, a fresh message.
        """
        from .outbound import deliver_whatsapp

        if not user_id:
            user_id = task.user_id if task is not None else None
        if not user_id:
            return None
        logical_key = reference_id or (
            f"task-result:{task.id}"
            if task is not None
            else f"notification:{uuid.uuid4()}"
        )
        return await deliver_whatsapp(
            self._config, logical_key=logical_key, user_id=user_id, text=text,
            task_id=task.id if task is not None else None,
            buttons=buttons, ignore_opt_out=ignore_opt_out,
        )

    async def edit(self, target: str, message_id: int, text: str) -> None:
        return None

    async def download_attachment(self, remote_ref: str, local_path: str) -> None:
        return None

    def resolve_target(self, task: "db.Task") -> str | None:
        """The advisory channel a resolved destination carries.

        The **stable conversation token**, not the send id and not the phone
        number. A resolved `Destination` reaches log lines, stored origin
        descriptors and the admin task views, none of which is a private
        operator surface, and this value has to survive being written down. It
        also answers the only question the planner asks of it — whether the
        user is enrolled at all — without the planner learning anything about
        who they are.
        """
        from .outbound import current_destination

        if not task.user_id or not current_destination(self._config, task.user_id):
            return None
        return whatsapp_conversation_token(task.user_id)


__all__ = [
    "LOCAL_TERMINAL_STATES",
    "REACHED_META",
    "InboundWhatsAppEvent",
    "WhatsAppDeliveryEvent",
    "WhatsAppDeliveryStatus",
    "WhatsAppEvent",
    "WhatsAppMessageKind",
    "WhatsAppSendFailure",
    "WhatsAppSendOutcome",
    "WhatsAppSendRequest",
    "WhatsAppSendResult",
    "WhatsAppTransport",
    "WhatsAppUserIdentity",
    "whatsapp_conversation_token",
]
