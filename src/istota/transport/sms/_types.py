"""Plain provider-neutral records for SMS rendering and delivery."""

from __future__ import annotations

from dataclasses import dataclass

from .providers._types import SmsEncoding


# The delivery statuses that mean a provider took the message. Read by the
# scheduler's owed-confirmation arm and by the notification dispatcher, which
# each had their own copy of the same four names.
#
# Deliberately not `delivered` alone: provider acceptance is the strongest
# thing a send call can report, and a handset receipt arrives later on a
# callback nothing is still waiting for.
REACHED_PROVIDER = frozenset({"accepted", "queued", "sent", "delivered"})


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
    # A notification raised inside the caller's transaction and owed a push
    # after it commits. `notification_store` documents why the write and the
    # send are two calls: a second connection opened under an open write
    # transaction waits out the whole busy timeout and is then swallowed.
    pending_alert: object | None = None
