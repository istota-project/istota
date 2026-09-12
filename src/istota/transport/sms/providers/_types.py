"""Plain values shared by common SMS code and provider adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias

from ....http_headers import header_value  # noqa: F401  (re-exported; see below)

SmsProviderName = Literal["twilio", "telnyx"]
SmsEncoding = Literal["gsm7", "ucs2"]
SmsDeliveryStatus = Literal[
    "accepted",
    "queued",
    "sent",
    "delivered",
    "delivery_unconfirmed",
    "failed",
]
SmsOptOutAction = Literal["stop", "start", "help"]


@dataclass(frozen=True)
class InboundSmsEvent:
    provider: SmsProviderName
    provider_event_id: str | None
    provider_message_id: str
    from_number: str
    to_number: str
    text: str
    media_count: int
    opt_out_action: SmsOptOutAction | None


@dataclass(frozen=True)
class SmsDeliveryEvent:
    provider: SmsProviderName
    provider_event_id: str | None
    provider_message_id: str
    status: SmsDeliveryStatus
    error_code: str | None
    reported_segments: int | None
    opted_out: bool = False


SmsProviderEvent: TypeAlias = InboundSmsEvent | SmsDeliveryEvent


@dataclass(frozen=True)
class SmsWebhookRequest:
    raw_body: bytes
    headers: Mapping[str, str]
    public_url: str


@dataclass(frozen=True)
class SmsWebhookResult:
    # `None` is an authenticated event this adapter deliberately does not
    # model — an unknown-but-valid provider status. It is acknowledged so the
    # provider stops retrying, and it changes nothing.
    event: SmsProviderEvent | None
    response_status: int
    response_content_type: str | None
    response_body: bytes


@dataclass(frozen=True)
class SmsSendRequest:
    to_number: str
    preferred_from_number: str
    text: str
    encoding: SmsEncoding
    status_callback_url: str


@dataclass(frozen=True)
class SmsSendResult:
    provider_message_id: str
    status: SmsDeliveryStatus
    reported_segments: int | None


@dataclass(frozen=True)
class SmsSendFailure:
    definite: bool
    error_code: str | None
    opted_out: bool
    safe_reason: str


SmsSendOutcome: TypeAlias = SmsSendResult | SmsSendFailure


class SmsWebhookError(ValueError):
    """A safe HTTP-facing provider webhook rejection."""

    def __init__(self, reason: str, status_code: int = 403) -> None:
        super().__init__(reason)
        self.status_code = status_code


# One cap for both adapters and for the ASGI reader in `webhook_receiver`, which
# has to refuse an oversize body *before* either adapter sees it. Three copies
# of this number meant a receiver that accepted more than an adapter would.
MAX_WEBHOOK_BODY = 64 * 1024


# Re-exported, not defined: the WhatsApp webhook needs the same lookup and
# importing it from an SMS provider package would be the wrong direction, so
# the implementation moved to the stdlib-only `istota.http_headers` leaf. Both
# adapters' callers keep the name they already import from here.


@dataclass(frozen=True)
class SmsProviderAdapter:
    name: SmsProviderName
    parse_webhook: Callable[[SmsWebhookRequest], SmsWebhookResult]
    send: Callable[[SmsSendRequest], SmsSendOutcome]
