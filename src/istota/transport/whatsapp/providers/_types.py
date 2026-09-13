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

    No defaults, on purpose: a third adapter answers every question rather
    than inheriting whichever answers happened to suit the first two. That is
    also why widening this record is a deliberate seam change — the three
    fields below the original four were added at Stage 6, and both shipped
    adapters had to answer them in the same diff.

    `delivery_receipts` is the one that is easy to guess wrong in the
    optimistic direction — it says the provider reports a *handset* state
    back, not that a send was accepted. Every provider reports the latter.

    **`delivery_receipts` is declared ahead of its reader and nothing reads it
    yet.** `outbound._gate` reads the cost and window fields; the parked-status
    table, the monotonic status ladder and the failure alert are
    unconditional, so declaring this `False` disables none of them today. It is
    here rather than added later because a capability record answered by two
    adapters and then widened is a record whose existing answers were never
    considered — but an adapter author must not read the field's presence as a
    switch.

    `address_field` names the `whatsapp_user_bindings` column holding this
    provider's own destination for a user, and it is the field that turned a
    deferral into a defect: `outbound._destination` returned
    ``send_id or bootstrap_phone_number`` for every provider, and a
    JID-latched row has no `send_id` by design, so a Baileys send resolved to
    a bare E.164 number its socket cannot address. Declared rather than
    branched on the provider name, for this record's own reason; the two
    spellings themselves live in `identity.address_for_binding`, beside the
    JID parser they have to agree with.

    `service_body_limit` and `interactive_body_limit` are the two body
    budgets, and the second is why they are here rather than being constants.
    Meta caps an **interactive** message — one carrying quick-reply buttons —
    at a quarter of a plain one, and `.claude/rules/whatsapp.md` records what
    rendering a confirmation prompt at the wrong one cost: Meta refused every
    question past 1,024 characters and the task parked until it expired. A
    provider that has no interactive object has no such cliff and must not
    inherit Meta's number, so the budget is the adapter's answer and the
    scheduler asks for it (`outbound.confirmation_body_budget`).
    """
    metered: bool
    has_service_window: bool
    supports_templates: bool
    delivery_receipts: bool
    address_field: str
    service_body_limit: int
    interactive_body_limit: int


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

    **`send` carries two obligations the ledger's correctness now rests on,
    and neither is expressible in the type.** They used to be structural:
    `outbound._send_claimed` built the Cloud client itself, inside the arm that
    settles `failed`. With construction behind this field, an adapter owns the
    line.

    1. **It never raises.** `_send_claimed`'s `except Exception` is a backstop
       for a double that does not, and it settles `unknown` — which
       `.claude/rules/whatsapp.md` calls the one state an operator can never
       resolve. An adapter that raises for a message that never left has
       spent it for nothing.
    2. **A provably-unsent failure is `definite=True`.** That single bit is the
       whole ledger decision: `definite` settles `failed`, anything else
       settles `unknown`. Erring towards ambiguous is safe and erring towards
       definite is not, so an adapter reports `definite` only where it knows
       nothing was queued — the provider refused under a documented rule, or
       no request was ever built.

    `parse_webhook` keeps a **different** error convention, and it is a
    convention rather than a type: `WhatsAppWebhookResult` carries a
    `response_status` for the answer, and a *rejection* is raised as the
    provider's own error carrying its status (`WhatsAppWebhookError` for
    Cloud) rather than returned. That is what keeps authenticate-then-read one
    call — a result object for a 403 is a value a caller can forget to check,
    where a raise is not. A second adapter with a webhook either follows it or
    the two are unified onto `response_status` before it lands; today Cloud is
    the only one and the route already catches that error.

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
