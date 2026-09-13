"""A paired WhatsApp Web session, bound to the provider seam.

Thin, and deliberately so. Everything this adapter needs a running process for
— the Unix socket, the sidecar's lifecycle, the correlated send round trip, the
inbound receiver and the paired session directory — is `baileys_bridge.py`, for
the volume of lifecycle code that accrued there. What is left here is the seam
itself: which capabilities this provider has, that it has no HTTP callback, and
how a send finds the bridge.

**`build_adapter` finds a bridge; it never makes one**, and that is a
constraint the seam imposes rather than a preference. `make_provider_registry`
may do no I/O on construction — the rule `outbound.active_adapter` rests on
when it builds a fresh registry per send — and a bridge opens a socket, makes a
directory and may spawn a subprocess. So the owner that started the bridge
publishes it (`baileys_bridge.set_active_bridge`) and this looks it up per
call. With none published the send is refused **definitely**: nothing was
written to any socket, so the ledger's `failed` is the honest answer and
`unknown`, the one state an operator can never resolve, is not spent on it.

`parse_webhook` and `verify_signature` are both `None`. Baileys receives over
its own socket, so there is no HTTP callback to mount and no signature to
check; the registry's contract test requires the two to be `None` together,
which is what stops an adapter declaring a webhook with no authentication for
it. The trust boundary is the socket — local, owned by the daemon, 0600, no
network peer — and faking a signature check over it would be theatre.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .. import baileys_protocol as proto
from .._types import WhatsAppSendOutcome, WhatsAppSendRequest
from ..outbound import WHATSAPP_TEXT_LIMIT
from ._types import WhatsAppProviderAdapter, WhatsAppProviderCaps

if TYPE_CHECKING:
    from ....config import Config

logger = logging.getLogger(__name__)

#: No sidecar has been published in this process, so the line was never
#: written. Distinct from `proto.REASON_NO_SIDECAR`, which is the bridge's
#: answer when a bridge exists and its peer is not connected: that one is about
#: the link, this one is about the deployment — nothing is running the
#: WhatsApp surface here at all, and the remedy is different.
REASON_NO_BRIDGE = "no WhatsApp sidecar is running in this process"

BAILEYS_CAPS = WhatsAppProviderCaps(
    metered=False,
    has_service_window=False,
    supports_templates=False,
    delivery_receipts=True,
    address_field="jid",
    service_body_limit=WHATSAPP_TEXT_LIMIT,
    interactive_body_limit=WHATSAPP_TEXT_LIMIT,
)
"""What a paired session is and is not, stated once.

Almost the inverse of `CLOUD_CAPS`, and each answer is the absence of a Meta
rule rather than a WhatsApp one. Nothing is **metered**: there is no per-message
price, so the billing circuit and the monthly attempt cap have nothing to
guard and `outbound._gate` skips both. There is no **service window**: the
24-hour rule is a Cloud API billing boundary, not a protocol one, so a reply
goes out whenever the task finishes. And there are no **templates**, which
follows — a template exists only to buy a way past a closed window.

`delivery_receipts` is True and is the one answer that is a presence rather
than an absence: WhatsApp reports sent, delivered and read back over the
socket, and the sidecar forwards each as a `WhatsAppDeliveryEvent` through the
same monotonic ladder Meta's callbacks use.

The two body budgets are **equal**, which is the whole point of their being
fields. Meta caps an interactive message — one carrying quick-reply buttons —
at a quarter of a plain one; there is no interactive object here, so the
sidecar sends a confirmation question as ordinary text and the answer travels
in the sentence the scheduler appends (`Task #N. Reply YES or NO.`), which is
the route `_request`'s docstring already documents for a Cloud *template*, and
which a typed `YES` or `!confirm <id>` satisfies from any surface. Inheriting
Meta's 1,024 here would silently discard three quarters of every question.
"""


def build_adapter(config: "Config") -> WhatsAppProviderAdapter:
    """The Baileys adapter. No socket, no subprocess, no session read.

    `config` is accepted and deliberately unused: the bridge that holds this
    deployment's configuration was built by its owner, and re-reading the
    session directory or the socket path here would be a second place for
    either to be decided. The parameter stays because the registry's builder
    signature is one shape for every provider.
    """
    async def send(request: WhatsAppSendRequest) -> WhatsAppSendOutcome:
        return await _send(request)

    return WhatsAppProviderAdapter(
        name="baileys",
        caps=BAILEYS_CAPS,
        parse_webhook=None,
        send=send,
        verify_signature=None,
    )


async def _send(request: WhatsAppSendRequest) -> WhatsAppSendOutcome:
    """Hand one send to the running bridge, or refuse it definitely.

    Never raises, which is the adapter contract `outbound._send_claimed` rests
    on: an escape there lands inside the claim-to-settle region and settles the
    row `unknown`. `BaileysBridge.send` carries the same contract and its own
    `definite` bookkeeping, so the only thing left to answer here is the case
    where there is no bridge — and that one is unambiguous.

    A `template` request cannot arrive: `supports_templates` is False, so
    `_gate` never chooses that kind. It is refused rather than sent as text on
    the ledger's own principle — a request this adapter cannot express is a
    message that must not be silently turned into a different one — and the
    refusal is definite for the same reason the missing bridge is.
    """
    from ..baileys_bridge import active_bridge  # noqa: PLC0415

    bridge = active_bridge()
    if bridge is None:
        logger.warning(
            "whatsapp.baileys.no_bridge: the WhatsApp surface is set to "
            "baileys and no sidecar bridge is running in this process",
        )
        return proto.local_failure(REASON_NO_BRIDGE, definite=True)
    if request.kind != "service":
        logger.warning(
            "whatsapp.baileys.unsupported_kind kind=%s", request.kind,
        )
        return proto.local_failure(proto.REASON_ENCODE_FAILED, definite=True)
    return await bridge.send(request)


__all__ = ["BAILEYS_CAPS", "REASON_NO_BRIDGE", "build_adapter"]
