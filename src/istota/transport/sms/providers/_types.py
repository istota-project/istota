"""Plain values shared by common SMS code and provider adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias

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


SmsProviderEvent: TypeAlias = InboundSmsEvent | SmsDeliveryEvent


@dataclass(frozen=True)
class SmsWebhookRequest:
    raw_body: bytes
    headers: Mapping[str, str]
    public_url: str


@dataclass(frozen=True)
class SmsWebhookResult:
    event: SmsProviderEvent
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


@dataclass(frozen=True)
class SmsProviderAdapter:
    name: SmsProviderName
    parse_webhook: Callable[[SmsWebhookRequest], SmsWebhookResult]
    send: Callable[[SmsSendRequest], SmsSendOutcome]
