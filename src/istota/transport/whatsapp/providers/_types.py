"""The contract one WhatsApp provider adapter fills in.

Mirrors `transport/sms/providers/_types.py` in shape and deliberately not in
code: the two surfaces have different identities, different ledger keys and
different delivery vocabularies, so what is shared is the arrangement rather
than a runtime. The event types themselves already live one directory up in
`transport/whatsapp/_types.py`, which is why this module defines only the
adapter record and its capability tuple.
"""

from __future__ import annotations

from collections.abc import Callable
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
    """
    name: WhatsAppProviderName
    caps: WhatsAppProviderCaps
    parse_webhook: Callable[[WhatsAppWebhookRequest], WhatsAppWebhookResult] | None
    send: Callable[[WhatsAppSendRequest], WhatsAppSendOutcome]
    verify_signature: Callable[[WhatsAppWebhookRequest], bool] | None
