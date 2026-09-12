"""Meta's hosted Cloud API, bound to the provider seam.

**What this module is, and what it deliberately is not.** The spec's stage line
says "move the Meta-specifics into `providers/whatsapp_cloud.py`", written from
a survey that lists `transport/whatsapp/` as `__init__.py`, `_types.py`,
`outbound.py` and `webhook.py`. The tree has a fifth module the survey missed:
`client.py`, whose own docstring calls it "the PyWa boundary … the one module
in the package allowed to import PyWa". So the separation the stage asks for
already exists, under two filenames rather than one — `client.py` is the Meta
send boundary and `webhook.py` is the Meta callback boundary, both Cloud-only
(the spec's own Affected-files section keeps `webhook.py` as a file and calls
it "Cloud-only"). Relocating either one buys a large diff, no boundary that is
not already there, and an import-path change in every test that names them,
which is the one thing the stage's equivalence instrument asks not to spend.

What genuinely did not exist is the binding: common code reached Meta by
importing those modules directly. This module is that binding, and the property
it buys is checkable — `outbound.py`, which is the common send path, now imports
no Meta module at all and reaches the provider only through the adapter record.
`tests/test_whatsapp_providers.py` holds that as a drift guard.

The webhook fields are the other half and are a **declaration** at this stage
rather than a caller. `webhook_receiver`'s two routes still call `webhook.py`
directly, and deliberately: routing them through `parse_webhook` here would put
the registry's own "is this adapter fully configured" answer in front of a
request, and a Cloud deployment missing `verify_token` — which the ISSUE-058
rule says must load, be reported by doctor and be refused at *use* — would
start answering 404 where it answers 403 or 503 today. Mounting is what became
provider-aware (`config.whatsapp_webhooks_enabled`), and these two fields are
what Baileys sets to `None` when it arrives with no HTTP callback at all. They
are the same code path the route takes, so the two cannot drift, and both are
driven by tests.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .._types import (
    WhatsAppSendFailure,
    WhatsAppSendOutcome,
    WhatsAppSendRequest,
    WhatsAppWebhookRequest,
    WhatsAppWebhookResult,
)
from ._types import WhatsAppProviderAdapter, WhatsAppProviderCaps

if TYPE_CHECKING:
    from ....config import Config

logger = logging.getLogger(__name__)

CLOUD_CAPS = WhatsAppProviderCaps(
    metered=True,
    has_service_window=True,
    supports_templates=True,
    delivery_receipts=True,
)
"""Every constraint `.claude/rules/whatsapp.md` records, declared as this
provider's own.

All four are true of Meta's hosted API and none of them is true of WhatsApp:
the 24-hour service window, approved templates, the monthly attempt cap and
the billing circuit are the Cloud API's rules. Declaring them here is what lets
`outbound.py`'s gate order stay provider-agnostic while behaving, for this
adapter, exactly as the hard-coded version did.
"""

#: A client that could not be built is a message that provably never left, so
#: the failure is **definite** and the ledger records `failed`. `unknown` is
#: the one state an operator can never resolve and is not spent on a case whose
#: answer is known. The reason is local text from a fixed table, never built
#: from the exception — `client.py`'s rule, for its reason: Meta's prose, PyWa's
#: exception text and an httpx repr all carry the request URL.
_CLIENT_UNAVAILABLE_REASON = "the whatsapp cloud client could not be built"


def build_adapter(config: "Config") -> WhatsAppProviderAdapter:
    """The Cloud adapter. No I/O, no PyWa import, no session.

    `make_registry` may not do I/O on construction and this registry follows
    the same rule, so nothing here builds a client: `send` constructs one per
    call and closes it, which is the lifetime `client.py` already argues for —
    the surface is capped at a few hundred messages a month and an
    `httpx.AsyncClient` is bound to the loop it was made on. That rule is what
    lets `outbound.active_adapter` build a registry per send without the
    caching the SMS side needs, and it is pinned rather than stated.

    **No client-injection parameter**, deliberately. One existed and was
    removed: `deliver_whatsapp` injects by replacing the resolved adapter's
    `send`, so a second seam here would be a second mechanism for one job,
    exercised only by tests and diverging from the path production takes. A
    test that wants a double replaces `send` the way production does.
    """
    def parse(request: WhatsAppWebhookRequest) -> WhatsAppWebhookResult:
        from ..webhook import parse_webhook  # noqa: PLC0415

        # `parse_webhook` is the whole authenticate-then-read chain — size,
        # content type, configured secret, HMAC over the exact raw bytes,
        # decode, normalize — and it stays one call for that reason. Splitting
        # the signature check out as a precondition the caller has to remember
        # is how a parser gets fed to an unauthenticated caller. `verify` below
        # is the same predicate exposed for the seam, never a step this skips.
        events = parse_webhook(config, request.raw_body, request.headers)
        return WhatsAppWebhookResult(
            events=tuple(events),
            response_status=200,
            response_content_type=None,
            response_body=b"",
        )

    def verify(request: WhatsAppWebhookRequest) -> bool:
        from ....http_headers import header_value  # noqa: PLC0415
        from ..client import SIGNATURE_HEADER, verify_signature  # noqa: PLC0415

        return verify_signature(
            config.whatsapp.app_secret,
            request.raw_body,
            header_value(request.headers, SIGNATURE_HEADER),
        )

    async def send(request: WhatsAppSendRequest) -> WhatsAppSendOutcome:
        return await _send(config, request)

    return WhatsAppProviderAdapter(
        name="whatsapp_cloud",
        caps=CLOUD_CAPS,
        parse_webhook=parse,
        send=send,
        verify_signature=verify,
    )


async def _send(config: "Config", request: WhatsAppSendRequest):
    """One Cloud API call, with the client's whole lifetime inside it.

    The construction is guarded separately from the call, and the split is the
    same line `_send_claimed` draws one level up: everything before the first
    byte is provably a message that never left. `WhatsAppClient.__init__`
    imports PyWa before it makes a session, so a missing or renamed dependency
    raises here — which used to reach `_send_claimed`'s pre-send arm and settle
    `failed`. Returning a *definite* failure keeps that ledger outcome exactly.

    Nothing between the construction and the first byte can have sent
    anything, which is what makes `definite` honest rather than optimistic:
    `client.py` records that `WhatsApp(**kwargs)` issues no request, and the
    `httpx.AsyncClient` it wraps opens no socket until one is made.
    """
    try:
        # Inside the guard, not above it. `client.py` is the module that
        # imports PyWa, so a missing or renamed dependency raises at *this*
        # line as readily as inside `make_client` — and an import left outside
        # would escape to `_send_claimed`'s backstop and settle `unknown`,
        # which is precisely the outcome this function exists to keep as
        # `failed`.
        from ..client import make_client  # noqa: PLC0415

        client = make_client(config)
    except Exception:
        # `exc_info` kept: the cause is a dependency or configuration fault an
        # operator has to see, and a traceback prints frames rather than the
        # message body.
        logger.warning(
            "whatsapp.outbound.failed reason=client_unavailable", exc_info=True,
        )
        return WhatsAppSendFailure(True, None, _CLIENT_UNAVAILABLE_REASON)
    try:
        return await client.send(request)
    finally:
        await client.aclose()


__all__ = ["CLOUD_CAPS", "build_adapter"]
