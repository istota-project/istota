"""WhatsApp through Meta's hosted Cloud API.

A direct external conversation, never a view of a room: nothing here creates or
joins a room, writes a canonical `messages` row, or mirrors a turn into Talk or
web chat. The transport, the webhook and the outbound ledger arrive in later
stages; this package currently holds the local records those speak in.
"""

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
]
