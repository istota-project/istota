"""WhatsApp through Meta's hosted Cloud API.

A direct external conversation, never a view of a room: nothing here creates or
joins a room, writes a canonical `messages` row, or mirrors a turn into Talk or
web chat. The transport, the webhook and the outbound ledger arrive in later
stages; this package currently holds the local records those speak in.
"""

import hashlib

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
    "WhatsAppUserIdentity",
    "whatsapp_conversation_token",
]
