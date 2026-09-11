"""Plain provider-neutral records for SMS rendering and delivery."""

from __future__ import annotations

from dataclasses import dataclass

from .providers._types import SmsEncoding


@dataclass(frozen=True)
class RenderedSms:
    text: str
    encoding: SmsEncoding
    estimated_segments: int


@dataclass(frozen=True)
class SmsDeliveryRecord:
    logical_key: str
    provider: str
    status: str
    provider_message_id: str | None = None
    error_code: str | None = None
    estimated_segments: int = 0
    reported_segments: int | None = None


@dataclass(frozen=True)
class SmsEventResult:
    disposition: str
    user_id: str | None = None
    task_id: int | None = None
    delivery: SmsDeliveryRecord | None = None
    response_text: str | None = None
    command_text: str | None = None
    response_logical_key: str | None = None
    preferred_from_number: str | None = None
