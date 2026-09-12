"""The contract one WhatsApp provider adapter fills in.

Mirrors `transport/sms/providers/_types.py` in shape and deliberately not in
code: the two surfaces have different identities, different ledger keys and
different delivery vocabularies, so what is shared is the arrangement rather
than a runtime. The event types themselves already live one directory up in
`transport/whatsapp/_types.py`, which is why this module defines only the
adapter record and its capability tuple.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from .._types import (
    WhatsAppSendOutcome,
    WhatsAppSendRequest,
    WhatsAppWebhookRequest,
    WhatsAppWebhookResult,
)

WhatsAppProviderName = Literal["baileys", "whatsapp_cloud"]


@dataclass(frozen=True)
class WhatsAppProviderCaps:
    """What the send path may assume about one provider.

    This record exists so the gate order in `outbound.py` stays
    provider-agnostic. Almost every hard constraint the WhatsApp surface
    carries — the 24-hour service window, approved templates, the monthly
    attempt cap and the billing circuit breaker — is a fact about Meta's
    hosted Cloud API rather than about WhatsApp, and a second provider that
    has none of them must not be reached by scattering `if provider == …`
    through common code. A gate reads the field that names its own
    precondition and skips itself where the precondition cannot arise.

    No defaults, on purpose: a third adapter answers all four questions
    rather than inheriting whichever answers happened to suit the first two.

    `delivery_receipts` is the one that is easy to guess wrong in the
    optimistic direction — it says the provider reports a *handset* state
    back, not that a send was accepted. Every provider reports the latter.
    """
    metered: bool
    has_service_window: bool
    supports_templates: bool
    delivery_receipts: bool


@dataclass(frozen=True)
class WhatsAppProviderAdapter:
    """One provider's whole surface to the rest of the transport.

    `parse_webhook` and `verify_signature` are `None` together for a provider
    that receives over a transport of its own rather than over an HTTP
    callback. They are two fields rather than one because the route needs both
    answers separately — whether to mount at all, and whether an arriving
    request authenticates — and because an adapter with a webhook and no
    signature scheme would be a thing to refuse loudly rather than to express.

    `send` is not optional. A provider that cannot send is not a provider.

    **`send` is awaitable and the other two are not**, which is where this
    seam departs from the SMS one it copies. There both halves are synchronous
    because the Twilio and Telnyx SDKs are; here the whole send path from
    `deliver_whatsapp` down is `async`, the Cloud API is reached over an
    `httpx.AsyncClient`, and a second provider receiving over a socket will
    await a round trip for its own reasons. `parse_webhook` and
    `verify_signature` stay synchronous: both are pure work over bytes already
    in hand, and the route awaits the body before either is called. Stage 1
    declared all three alike, before there was an adapter to check it against.
    """
    name: WhatsAppProviderName
    caps: WhatsAppProviderCaps
    parse_webhook: Callable[[WhatsAppWebhookRequest], WhatsAppWebhookResult] | None
    send: Callable[[WhatsAppSendRequest], Awaitable[WhatsAppSendOutcome]]
    verify_signature: Callable[[WhatsAppWebhookRequest], bool] | None
