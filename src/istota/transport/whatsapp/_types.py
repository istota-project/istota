"""Plain local records for the WhatsApp Cloud API surface.

Common istota code works with these rather than with PyWa classes or raw Meta
dictionaries. The reason is the same one that keeps PyWa's handler loop out of
the webhook: a PyWa update object carries the raw payload, and a raw payload
carries the message body, the sender's phone number and their BSUID. Bounding
that at one module means the rest of the transport cannot leak it by accident.

`client.py` is the only module that may hold a PyWa object; everything here is
data, so this module imports nothing from the package and nothing from PyWa.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias

WhatsAppMessageKind = Literal["service", "template"]
WhatsAppDeliveryStatus = Literal[
    "accepted", "sent", "delivered", "read", "failed",
]

REACHED_META = frozenset({"accepted", "sent", "delivered", "read"})
"""Ledger states meaning Meta took the message.

`accepted` is in it, deliberately: an immediate Cloud API response means Meta
accepted the request, never that a handset received anything. A caller asking
"did this reach the provider" is asking a different question from "did it
arrive", and conflating the two is how a delivered answer gets sent twice.
"""

LOCAL_TERMINAL_STATES = frozenset({
    "window_closed", "budget_exhausted", "billing_blocked",
    "opted_out", "unconfigured", "unknown",
})
"""Ledger states no automatic path may leave.

Five are decisions istota made without calling Meta. The sixth, `unknown`, is
the one that matters: a timeout or a lost response means the request may have
reached Meta, and the Cloud API has no application idempotency token that
could settle it — so a resend risks a duplicate private answer, which is worse
than a visible unknown.
"""


@dataclass(frozen=True)
class WhatsAppUserIdentity:
    """Who sent a message, as WhatsApp 4.x models identity.

    `bsuid` is the durable one and the only one authorization may rest on.
    `wa_id` is absent for a user with a username, so it is an enrollment hint
    rather than an identity, and `username` is display-only — never an
    authentication fallback.
    """
    bsuid: str
    wa_id: str | None
    username: str | None


@dataclass(frozen=True)
class InboundWhatsAppEvent:
    message_id: str
    waba_id: str
    phone_number_id: str
    from_user: WhatsAppUserIdentity
    message_type: str
    text: str | None
    callback_data: str | None
    reply_to_message_id: str | None
    sent_at: datetime


@dataclass(frozen=True)
class WhatsAppDeliveryEvent:
    """One status callback.

    The pricing fields are opaque observed values, stored and never
    interpreted: a status payload is evidence *after* a send, not
    authorization before one, and old conversation-pricing names do not
    describe current cost. `billable = None` means Meta said nothing, which is
    neither free nor paid.
    """
    message_id: str
    waba_id: str
    phone_number_id: str
    recipient_id: str
    status: WhatsAppDeliveryStatus
    occurred_at: datetime
    error_code: str | None
    billable: bool | None
    pricing_model: str | None
    pricing_category: str | None
    pricing_type: str | None


@dataclass(frozen=True)
class WhatsAppParkedStatus:
    """A status callback held because the id it names had not landed yet.

    The seven fields `apply_delivery_event` reads, and no others. It is not a
    `WhatsAppDeliveryEvent` with blanks in it: that record carries the
    recipient id, the WABA id and the phone number id, none of which is stored
    when a status is parked, and filling them with empty strings on the way
    back out would put three values in a dataclass that never observed them.
    """
    message_id: str
    status: WhatsAppDeliveryStatus
    error_code: str | None
    billable: bool | None
    pricing_model: str | None
    pricing_category: str | None
    pricing_type: str | None


WhatsAppEvent: TypeAlias = InboundWhatsAppEvent | WhatsAppDeliveryEvent


@dataclass(frozen=True)
class WhatsAppSendRequest:
    """One Cloud API call, described without a PyWa object in sight.

    `buttons` is a tuple of ``(callback_data, title)`` pairs rather than PyWa
    `Button` instances, for the reason this whole module exists: `outbound.py`
    decides that a confirmation prompt carries Yes and No, and `client.py` is
    the only place allowed to turn that into a PyWa type. The callback data is
    the confirmation id and the choice and nothing else — never an
    authorization secret, since it travels in a WhatsApp message on the
    recipient's own device.
    """
    to: str
    text: str
    kind: WhatsAppMessageKind
    reply_to_message_id: str | None = None
    template_name: str | None = None
    template_language: str | None = None
    buttons: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class WhatsAppSendResult:
    """Meta accepted the request. Not a delivery, and never read as one."""
    message_id: str
    status: Literal["accepted"] = "accepted"


@dataclass(frozen=True)
class WhatsAppSendFailure:
    """`definite` is the whole point of this record.

    A documented API rejection is definite and the row fails. Anything else —
    a timeout, a lost connection, an unreadable success response, any failure
    after the request may have reached Meta — is ambiguous, and the row goes
    to `unknown` rather than being retried. `safe_reason` is scrubbed local
    text; provider exception text never reaches it.
    """
    definite: bool
    error_code: str | None
    safe_reason: str


WhatsAppSendOutcome: TypeAlias = WhatsAppSendResult | WhatsAppSendFailure


@dataclass(frozen=True)
class WhatsAppDeliveryRecord:
    """One `sent_whatsapp` row, as every caller outside the ledger sees it.

    Deliberately carries neither the rendered body nor the destination: the
    task, notification source or command record already owns the content, and
    the binding is resolved immediately before the send rather than stored.
    `body_chars` and the row's `body_sha256` are what diagnostics get instead.
    """
    logical_key: str
    status: str
    send_kind: str = "service"
    meta_message_id: str | None = None
    error_code: str | None = None
    body_chars: int = 0
