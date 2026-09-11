"""Telnyx webhook verification and outbound SMS adapter."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from telnyx import APIStatusError, Telnyx
from telnyx.lib.webhook_verification import WebhookVerificationError
from telnyx.lib.webhooks_ed25519 import verify_ed25519

from ....config import Config
from ._types import (
    MAX_WEBHOOK_BODY,
    InboundSmsEvent,
    SmsDeliveryEvent,
    SmsDeliveryStatus,
    SmsOptOutAction,
    SmsProviderAdapter,
    SmsSendFailure,
    SmsSendRequest,
    SmsSendResult,
    SmsWebhookError,
    SmsWebhookRequest,
    SmsWebhookResult,
    header_value,
)

_STATUS_MAP: dict[str, SmsDeliveryStatus] = {
    "accepted": "accepted",
    "queued": "queued",
    "sending": "sent",
    "sent": "sent",
    "delivered": "delivered",
    "delivery_unconfirmed": "delivery_unconfirmed",
    "sending_failed": "failed",
    "delivery_failed": "failed",
    "expired": "failed",
}
_OPT_OUT_TYPES: dict[str, SmsOptOutAction] = {
    "STOP": "stop",
    "START": "start",
    "HELP": "help",
}
_STOP_KEYWORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"})
_START_KEYWORDS = frozenset({"START", "UNSTOP"})
_HELP_KEYWORDS = frozenset({"HELP", "INFO"})
_PUBLIC_ERROR_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")


TelnyxWebhookError = SmsWebhookError


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TelnyxWebhookError(f"invalid {name}", 400)
    return value


def _required_text(values: Mapping[str, object], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value:
        raise TelnyxWebhookError("missing required event field", 400)
    return value


def _objects(value: object, name: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise TelnyxWebhookError(f"invalid {name}", 400)
    result: list[Mapping[str, object]] = []
    for item in value:
        result.append(_mapping(item, name))
    return result


def _phone(values: Mapping[str, object], name: str) -> str:
    return _required_text(_mapping(values.get(name), name), "phone_number")


def _recipient(payload: Mapping[str, object]) -> Mapping[str, object]:
    recipients = _objects(payload.get("to"), "recipient list")
    if len(recipients) != 1:
        raise TelnyxWebhookError("invalid recipient list", 400)
    return recipients[0]


def _opt_out_action(payload: Mapping[str, object]) -> SmsOptOutAction | None:
    explicit = payload.get("autoresponse_type")
    if explicit not in (None, ""):
        if not isinstance(explicit, str):
            raise TelnyxWebhookError("invalid opt-out type", 400)
        action = _OPT_OUT_TYPES.get(explicit.strip().upper())
        if action is None:
            raise TelnyxWebhookError("invalid opt-out type", 400)
        return action
    text = payload.get("text", "")
    keyword = text.strip().upper() if isinstance(text, str) else ""
    if keyword in _STOP_KEYWORDS:
        return "stop"
    if keyword in _START_KEYWORDS:
        return "start"
    if keyword in _HELP_KEYWORDS:
        return "help"
    return None


def _delivery_status(value: object) -> SmsDeliveryStatus:
    if not isinstance(value, str):
        raise TelnyxWebhookError("missing delivery status", 400)
    status = _STATUS_MAP.get(value.strip().lower())
    if status is None:
        raise TelnyxWebhookError("unsupported message status", 400)
    return status


def _public_error(value: object) -> str | None:
    if value in (None, ""):
        return None
    code = str(value)
    return code if _PUBLIC_ERROR_CODE.fullmatch(code) else None


def _first_error_code(payload: Mapping[str, object]) -> str | None:
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return None
    for item in errors:
        if isinstance(item, Mapping):
            code = _public_error(item.get("code"))
            if code is not None:
                return code
    return None


def _nonnegative_integer(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _parse_event(
    body: Mapping[str, object],
    *,
    messaging_profile_id: str,
    service_numbers: frozenset[str],
) -> SmsWebhookResult:
    data = _mapping(body.get("data"), "event data")
    event_id = _required_text(data, "id")
    event_type = _required_text(data, "event_type")
    payload = _mapping(data.get("payload"), "event payload")
    if _required_text(payload, "messaging_profile_id") != messaging_profile_id:
        raise TelnyxWebhookError("unexpected messaging profile")
    message_id = _required_text(payload, "id")
    recipient = _recipient(payload)

    if event_type == "message.received":
        to_number = _required_text(recipient, "phone_number")
        if to_number not in service_numbers:
            raise TelnyxWebhookError("unexpected receiving number")
        from_number = _phone(payload, "from")
        text = payload.get("text", "")
        if not isinstance(text, str):
            raise TelnyxWebhookError("invalid message text", 400)
        media = _objects(payload.get("media", []), "media list")
        event = InboundSmsEvent(
            provider="telnyx",
            provider_event_id=event_id,
            provider_message_id=message_id,
            from_number=from_number,
            to_number=to_number,
            text=text,
            media_count=len(media),
            opt_out_action=_opt_out_action(payload),
        )
    elif event_type in {"message.sent", "message.finalized"}:
        from_number = _phone(payload, "from")
        if from_number not in service_numbers:
            raise TelnyxWebhookError("unexpected sending number")
        error_code = _first_error_code(payload)
        event = SmsDeliveryEvent(
            provider="telnyx",
            provider_event_id=event_id,
            provider_message_id=message_id,
            status=_delivery_status(recipient.get("status")),
            error_code=error_code,
            reported_segments=_nonnegative_integer(payload.get("parts")),
            opted_out=error_code == "40300",
        )
    else:
        raise TelnyxWebhookError("unsupported event type", 400)
    return SmsWebhookResult(event, 204, None, b"")


def _parse_webhook(
    request: SmsWebhookRequest,
    *,
    client: Telnyx,
    messaging_profile_id: str,
    service_numbers: frozenset[str],
) -> SmsWebhookResult:
    content_type = header_value(request.headers, "content-type").partition(";")[0].strip()
    if content_type.casefold() != "application/json":
        raise TelnyxWebhookError("unsupported content type", 415)
    if len(request.raw_body) > MAX_WEBHOOK_BODY:
        raise TelnyxWebhookError("webhook body too large", 413)
    try:
        verify_ed25519(client, request.raw_body, request.headers)
    except (WebhookVerificationError, UnicodeDecodeError, ValueError):
        raise TelnyxWebhookError("invalid signature") from None
    try:
        body = json.loads(request.raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TelnyxWebhookError("invalid json body", 400) from None
    return _parse_event(
        _mapping(body, "event body"),
        messaging_profile_id=messaging_profile_id,
        service_numbers=service_numbers,
    )


def _response_status(data: object) -> SmsDeliveryStatus | None:
    recipients = getattr(data, "to", None)
    if not isinstance(recipients, list) or len(recipients) != 1:
        return None
    value = getattr(recipients[0], "status", None)
    if not isinstance(value, str):
        return None
    return _STATUS_MAP.get(value.strip().lower())


def _exception_error_code(exc: APIStatusError) -> str | None:
    body = exc.body
    if not isinstance(body, Mapping):
        return None
    errors = body.get("errors")
    if not isinstance(errors, list):
        return None
    for item in errors:
        if isinstance(item, Mapping):
            code = _public_error(item.get("code"))
            if code is not None:
                return code
    return None


def _send(client: Telnyx, config: Config, request: SmsSendRequest):
    try:
        response = client.messages.send(
            to=request.to_number,
            from_=request.preferred_from_number,
            text=request.text,
            type="SMS",
            encoding=request.encoding,
            messaging_profile_id=config.sms.telnyx.messaging_profile_id,
            webhook_url=request.status_callback_url,
            use_profile_webhooks=False,
        )
        data = getattr(response, "data", None)
        message_id = str(getattr(data, "id", "") or "")
        status = _response_status(data)
        if not message_id or status is None:
            return SmsSendFailure(False, None, False, "delivery outcome unknown")
        return SmsSendResult(
            message_id,
            status,
            _nonnegative_integer(getattr(data, "parts", None)),
        )
    except APIStatusError as exc:
        code = _exception_error_code(exc)
        return SmsSendFailure(
            True, code, code == "40300", "provider rejected message",
        )
    except Exception:
        return SmsSendFailure(False, None, False, "delivery outcome unknown")


def build_adapter(config: Config) -> SmsProviderAdapter:
    """Build a Telnyx adapter whose exceptions never cross its boundary."""
    telnyx = config.sms.telnyx
    client = Telnyx(
        api_key=telnyx.api_key,
        public_key=telnyx.public_key,
        timeout=config.sms.request_timeout_seconds,
        max_retries=0,
    )

    def parse(request: SmsWebhookRequest) -> SmsWebhookResult:
        return _parse_webhook(
            request,
            client=client,
            messaging_profile_id=telnyx.messaging_profile_id,
            service_numbers=frozenset(config.sms.service_numbers),
        )

    def send(request: SmsSendRequest):
        return _send(client, config, request)

    return SmsProviderAdapter(name="telnyx", parse_webhook=parse, send=send)


__all__ = ["TelnyxWebhookError", "build_adapter"]
