"""Twilio webhook verification and outbound SMS adapter."""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import parse_qsl

from requests.exceptions import RequestException
from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from ....config import Config
from ._types import (
    InboundSmsEvent,
    SmsDeliveryEvent,
    SmsDeliveryStatus,
    SmsOptOutAction,
    SmsProviderAdapter,
    SmsSendFailure,
    SmsSendRequest,
    SmsSendResult,
    SmsWebhookRequest,
    SmsWebhookResult,
)

_MAX_WEBHOOK_BODY = 64 * 1024
_TWIML_RESPONSE = b'<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
_STATUS_MAP: dict[str, SmsDeliveryStatus] = {
    "accepted": "accepted",
    "scheduled": "accepted",
    "queued": "queued",
    "sending": "sent",
    "sent": "sent",
    "delivered": "delivered",
    "read": "delivered",
    "undelivered": "failed",
    "failed": "failed",
    "canceled": "failed",
}
_OPT_OUT_TYPES: dict[str, SmsOptOutAction] = {
    "STOP": "stop",
    "START": "start",
    "HELP": "help",
}
_STOP_KEYWORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"})
_START_KEYWORDS = frozenset({"START", "UNSTOP"})
_HELP_KEYWORDS = frozenset({"HELP", "INFO"})


class TwilioWebhookError(ValueError):
    """A safe HTTP-facing rejection of a Twilio webhook."""

    def __init__(self, reason: str, status_code: int = 403) -> None:
        super().__init__(reason)
        self.status_code = status_code


class _FormFields:
    """Minimal multi-dict preserving every signed form field and value."""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs

    def __iter__(self) -> Iterator[str]:
        return iter({name for name, _value in self._pairs})

    def __getitem__(self, name: str) -> str:
        values = self.getlist(name)
        if not values:
            raise KeyError(name)
        return values[-1]

    def get(self, name: str, default: str = "") -> str:
        values = self.getlist(name)
        return values[-1] if values else default

    def getlist(self, name: str) -> list[str]:
        return [value for key, value in self._pairs if key == name]


def _header(headers, name: str) -> str:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return ""


def _parse_fields(request: SmsWebhookRequest) -> _FormFields:
    content_type = _header(request.headers, "content-type").partition(";")[0].strip()
    if content_type.casefold() != "application/x-www-form-urlencoded":
        raise TwilioWebhookError("unsupported content type", 415)
    if len(request.raw_body) > _MAX_WEBHOOK_BODY:
        raise TwilioWebhookError("webhook body too large", 413)
    try:
        body = request.raw_body.decode("utf-8", errors="strict")
        pairs = parse_qsl(
            body, keep_blank_values=True, strict_parsing=True, max_num_fields=1024,
        )
    except (UnicodeDecodeError, ValueError):
        raise TwilioWebhookError("invalid form body", 400) from None
    return _FormFields(pairs)


def _required(fields: _FormFields, name: str) -> str:
    value = fields.get(name)
    if not value:
        raise TwilioWebhookError("missing required form field")
    return value


def _integer(fields: _FormFields, name: str, *, default: int | None) -> int | None:
    raw = fields.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise TwilioWebhookError("invalid numeric form field", 400) from None
    if value < 0:
        raise TwilioWebhookError("invalid numeric form field", 400)
    return value


def _opt_out_action(fields: _FormFields) -> SmsOptOutAction | None:
    explicit = fields.get("OptOutType").strip().upper()
    if explicit:
        action = _OPT_OUT_TYPES.get(explicit)
        if action is None:
            raise TwilioWebhookError("invalid opt-out type", 400)
        return action
    keyword = fields.get("Body").strip().upper()
    if keyword in _STOP_KEYWORDS:
        return "stop"
    if keyword in _START_KEYWORDS:
        return "start"
    if keyword in _HELP_KEYWORDS:
        return "help"
    return None


def _delivery_status(value: str) -> SmsDeliveryStatus:
    status = _STATUS_MAP.get(value.strip().lower())
    if status is None:
        raise TwilioWebhookError("unsupported message status", 400)
    return status


def _parse_webhook(
    request: SmsWebhookRequest,
    *,
    validator: RequestValidator,
    account_sid: str,
    messaging_service_sid: str,
    service_numbers: frozenset[str],
) -> SmsWebhookResult:
    fields = _parse_fields(request)
    signature = _header(request.headers, "x-twilio-signature")
    if not signature or not validator.validate(request.public_url, fields, signature):
        raise TwilioWebhookError("invalid signature")
    if fields.get("AccountSid") != account_sid:
        raise TwilioWebhookError("unexpected account")
    if fields.get("MessagingServiceSid") != messaging_service_sid:
        raise TwilioWebhookError("unexpected messaging service")
    if fields.get("To") not in service_numbers:
        raise TwilioWebhookError("unexpected receiving number")

    message_sid = _required(fields, "MessageSid")
    callback_status = fields.get("MessageStatus")
    if callback_status:
        error_code = fields.get("ErrorCode") or None
        event = SmsDeliveryEvent(
            provider="twilio",
            provider_event_id=None,
            provider_message_id=message_sid,
            status=_delivery_status(callback_status),
            error_code=error_code,
            reported_segments=_integer(fields, "NumSegments", default=None),
            opted_out=error_code == "21610",
        )
        return SmsWebhookResult(event, 204, None, b"")

    event = InboundSmsEvent(
        provider="twilio",
        provider_event_id=None,
        provider_message_id=message_sid,
        from_number=_required(fields, "From"),
        to_number=_required(fields, "To"),
        text=fields.get("Body"),
        media_count=_integer(fields, "NumMedia", default=0) or 0,
        opt_out_action=_opt_out_action(fields),
    )
    return SmsWebhookResult(event, 200, "application/xml", _TWIML_RESPONSE)


def _send(client: Client, config: Config, request: SmsSendRequest):
    try:
        message = client.messages.create(
            to=request.to_number,
            from_=request.preferred_from_number,
            body=request.text,
            messaging_service_sid=config.sms.twilio.messaging_service_sid,
            status_callback=request.status_callback_url,
        )
        message_sid = str(getattr(message, "sid", "") or "")
        if not message_sid:
            return SmsSendFailure(False, None, False, "delivery outcome unknown")
        status = _STATUS_MAP.get(str(getattr(message, "status", "") or "").lower())
        if status is None:
            return SmsSendFailure(False, None, False, "delivery outcome unknown")
        raw_segments = getattr(message, "num_segments", None)
        try:
            segments = int(raw_segments) if raw_segments not in (None, "") else None
        except (TypeError, ValueError):
            segments = None
        return SmsSendResult(message_sid, status, segments)
    except TwilioRestException as exc:
        code = str(exc.code) if exc.code is not None else None
        return SmsSendFailure(True, code, code == "21610", "provider rejected message")
    except RequestException:
        return SmsSendFailure(False, None, False, "delivery outcome unknown")
    except Exception:
        return SmsSendFailure(False, None, False, "delivery outcome unknown")


def build_adapter(config: Config) -> SmsProviderAdapter:
    """Build a Twilio adapter whose exceptions never cross its boundary."""
    twilio = config.sms.twilio
    validator = RequestValidator(twilio.auth_token)
    http_client = TwilioHttpClient(
        timeout=config.sms.request_timeout_seconds,
        max_retries=0,
    )
    client = Client(
        twilio.api_key_sid,
        twilio.api_key_secret,
        twilio.account_sid,
        http_client=http_client,
    )

    def parse(request: SmsWebhookRequest) -> SmsWebhookResult:
        return _parse_webhook(
            request,
            validator=validator,
            account_sid=twilio.account_sid,
            messaging_service_sid=twilio.messaging_service_sid,
            service_numbers=frozenset(config.sms.service_numbers),
        )

    def send(request: SmsSendRequest):
        return _send(client, config, request)

    return SmsProviderAdapter(name="twilio", parse_webhook=parse, send=send)


__all__ = ["TwilioWebhookError", "build_adapter"]
