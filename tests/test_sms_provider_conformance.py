from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from requests.exceptions import ReadTimeout
from telnyx import APITimeoutError, BadRequestError
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from istota import db
from istota.config import (
    Config,
    SmsConfig,
    TelnyxSmsConfig,
    TwilioSmsConfig,
    UserConfig,
)
from istota.transport.sms.providers._types import (
    InboundSmsEvent,
    SmsDeliveryEvent,
    SmsSendFailure,
    SmsSendRequest,
    SmsWebhookRequest,
)
from tests.support.drift import source_of


SERVICE_NUMBER = "+15551230000"
USER_NUMBER = "+15551234567"
PROFILE_ID = "10000000-0000-4000-8000-000000000001"
TELNYX_MESSAGE_ID = "20000000-0000-4000-8000-000000000002"
TELNYX_EVENT_ID = "30000000-0000-4000-8000-000000000003"
TWILIO_MESSAGE_ID = "SM-contract"
_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
PUBLIC_KEY = base64.b64encode(
    _PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
).decode()


def _config(tmp_path, provider: str) -> Config:
    path = tmp_path / f"{provider}.db"
    db.init_db(path)
    return Config(
        db_path=path,
        site=SimpleNamespace(hostname="assistant.example.test"),
        sms=SmsConfig(
            enabled=True,
            provider=provider,
            service_numbers=[SERVICE_NUMBER],
            default_sender_number=SERVICE_NUMBER,
            twilio=TwilioSmsConfig(
                account_sid="AC-account",
                auth_token="twilio-webhook-secret",
                api_key_sid="SK-api-key",
                api_key_secret="twilio-api-secret",
                messaging_service_sid="MG-service",
            ),
            telnyx=TelnyxSmsConfig(
                api_key="telnyx-api-secret",
                public_key=PUBLIC_KEY,
                messaging_profile_id=PROFILE_ID,
            ),
        ),
        users={"alice": UserConfig(sms_phone_number=USER_NUMBER)},
    )


def _twilio_request(fields) -> SmsWebhookRequest:
    url = "https://assistant.example.test/webhooks/sms/twilio"
    signature = RequestValidator("twilio-webhook-secret").compute_signature(
        url, fields,
    )
    return SmsWebhookRequest(
        raw_body=urlencode(fields).encode(),
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "x-twilio-signature": signature,
        },
        public_url=url,
    )


def _telnyx_request(payload) -> SmsWebhookRequest:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    stamp = str(int(time.time()))
    signature = base64.b64encode(
        _PRIVATE_KEY.sign(stamp.encode() + b"|" + body)
    ).decode()
    return SmsWebhookRequest(
        raw_body=body,
        headers={
            "content-type": "application/json",
            "telnyx-signature-ed25519": signature,
            "telnyx-timestamp": stamp,
        },
        public_url="https://assistant.example.test/webhooks/sms/telnyx",
    )


def _inbound_request(provider: str) -> SmsWebhookRequest:
    if provider == "twilio":
        return _twilio_request({
            "AccountSid": "AC-account",
            "MessagingServiceSid": "MG-service",
            "MessageSid": TWILIO_MESSAGE_ID,
            "From": USER_NUMBER,
            "To": SERVICE_NUMBER,
            "Body": "STOP",
            "NumMedia": "2",
            "OptOutType": "STOP",
        })
    return _telnyx_request({
        "data": {
            "id": TELNYX_EVENT_ID,
            "event_type": "message.received",
            "record_type": "event",
            "payload": {
                "id": TELNYX_MESSAGE_ID,
                "record_type": "message",
                "direction": "inbound",
                "type": "SMS",
                "messaging_profile_id": PROFILE_ID,
                "from": {"phone_number": USER_NUMBER},
                "to": [{"phone_number": SERVICE_NUMBER}],
                "text": "STOP",
                "media": [{"url": "one"}, {"url": "two"}],
                "autoresponse_type": "STOP",
            },
        },
    })


def _delivery_request(provider: str) -> SmsWebhookRequest:
    if provider == "twilio":
        return _twilio_request({
            "AccountSid": "AC-account",
            "MessagingServiceSid": "MG-service",
            "MessageSid": TWILIO_MESSAGE_ID,
            "From": SERVICE_NUMBER,
            "To": USER_NUMBER,
            "MessageStatus": "delivered",
            "NumSegments": "2",
        })
    return _telnyx_request({
        "data": {
            "id": TELNYX_EVENT_ID,
            "event_type": "message.finalized",
            "record_type": "event",
            "payload": {
                "id": TELNYX_MESSAGE_ID,
                "record_type": "message",
                "direction": "outbound",
                "type": "SMS",
                "messaging_profile_id": PROFILE_ID,
                "from": {"phone_number": SERVICE_NUMBER},
                "to": [{"phone_number": USER_NUMBER, "status": "delivered"}],
                "parts": 2,
                "errors": [],
            },
        },
    })


def _build_adapter(provider: str, config: Config):
    if provider == "twilio":
        from istota.transport.sms.providers.twilio import build_adapter
    else:
        from istota.transport.sms.providers.telnyx import build_adapter
    return build_adapter(config)


@pytest.mark.parametrize("provider", ["twilio", "telnyx"])
def test_sms_adapter_conformance_normalizes_signed_inbound(provider, tmp_path):
    event = _build_adapter(provider, _config(tmp_path, provider)).parse_webhook(
        _inbound_request(provider)
    ).event

    assert isinstance(event, InboundSmsEvent)
    assert event.provider == provider
    assert event.provider_message_id in {TWILIO_MESSAGE_ID, TELNYX_MESSAGE_ID}
    assert event.from_number == USER_NUMBER
    assert event.to_number == SERVICE_NUMBER
    assert event.text == "STOP"
    assert event.media_count == 2
    assert event.opt_out_action == "stop"


@pytest.mark.parametrize("provider", ["twilio", "telnyx"])
def test_sms_adapter_conformance_normalizes_delivery(provider, tmp_path):
    event = _build_adapter(provider, _config(tmp_path, provider)).parse_webhook(
        _delivery_request(provider)
    ).event

    assert isinstance(event, SmsDeliveryEvent)
    assert event.provider == provider
    assert event.status == "delivered"
    assert event.reported_segments == 2
    assert event.error_code is None
    assert event.opted_out is False


@pytest.mark.parametrize("provider", ["twilio", "telnyx"])
def test_callback_only_http_route_updates_delivery_but_rejects_inbound_work(
    provider, tmp_path, monkeypatch,
):
    from fastapi.testclient import TestClient

    from istota import webhook_receiver as receiver
    from istota.transport.sms.providers.registry import make_provider_registry

    config = _config(tmp_path, provider)
    message_id = TWILIO_MESSAGE_ID if provider == "twilio" else TELNYX_MESSAGE_ID
    with db.get_db(config.db_path) as conn:
        conn.execute(
            "INSERT INTO sent_sms (logical_key, provider, provider_message_id, "
            "user_id, to_number, from_number, status, estimated_segments, "
            "body_chars, body_sha256, created_at, updated_at) "
            "VALUES (?, ?, ?, 'alice', ?, ?, 'sent', 1, 4, 'hash', "
            "datetime('now'), datetime('now'))",
            (f"callback-only:{provider}", provider, message_id, USER_NUMBER, SERVICE_NUMBER),
        )
    config.sms.enabled = False
    monkeypatch.setattr(receiver, "_config", config)
    monkeypatch.setattr(receiver, "_sms_providers", make_provider_registry(config))
    monkeypatch.setattr(receiver, "reload_config", lambda: None)
    monkeypatch.setattr(receiver.signal, "signal", lambda *_args: None)

    delivery = _delivery_request(provider)
    inbound = _inbound_request(provider)
    with TestClient(receiver.app) as client:
        delivery_response = client.post(
            f"/webhooks/sms/{provider}",
            content=delivery.raw_body,
            headers=delivery.headers,
        )
        inbound_response = client.post(
            f"/webhooks/sms/{provider}",
            content=inbound.raw_body,
            headers=inbound.headers,
        )

    assert delivery_response.status_code == 204
    assert inbound_response.status_code == (200 if provider == "twilio" else 204)
    with db.get_db(config.db_path) as conn:
        row = conn.execute(
            "SELECT status, reported_segments FROM sent_sms WHERE provider = ?",
            (provider,),
        ).fetchone()
        task_count = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    assert tuple(row) == ("delivered", 2)
    assert task_count == 0


def test_webhook_receiver_depends_only_on_common_provider_contract():
    from istota import webhook_receiver

    source = (
        source_of(webhook_receiver.receive_twilio_sms)
        + source_of(webhook_receiver.receive_telnyx_sms)
    )

    assert ".providers.twilio" not in source
    assert ".providers.telnyx" not in source
    assert ".providers._types" in source


def _provider_failure(provider: str, kind: str):
    if provider == "twilio":
        if kind == "ambiguous":
            return ReadTimeout("twilio-api-secret")
        code = 21610 if kind == "opt_out" else 21211
        return TwilioRestException(
            400, "/Messages", f"twilio-api-secret {USER_NUMBER}", code,
        )
    if kind == "ambiguous":
        return APITimeoutError(httpx.Request("POST", "https://api.example.test"))
    code = "40300" if kind == "opt_out" else "40001"
    return BadRequestError(
        f"telnyx-api-secret {USER_NUMBER}",
        response=httpx.Response(
            400, request=httpx.Request("POST", "https://api.example.test")
        ),
        body={"errors": [{"code": code, "detail": "telnyx-api-secret"}]},
    )


def _adapter_with_send_failure(provider: str, config: Config, monkeypatch, failure):
    if provider == "twilio":
        from istota.transport.sms.providers import twilio as module

        class FakeMessages:
            def create(self, **_kwargs):
                raise failure

        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                self.messages = FakeMessages()

        monkeypatch.setattr(module, "Client", FakeClient)
    else:
        from istota.transport.sms.providers import telnyx as module

        class FakeMessages:
            def send(self, **_kwargs):
                raise failure

        class FakeClient:
            def __init__(self, **_kwargs):
                self.messages = FakeMessages()

        monkeypatch.setattr(module, "Telnyx", FakeClient)
    return module.build_adapter(config)


@pytest.mark.parametrize("provider", ["twilio", "telnyx"])
@pytest.mark.parametrize("kind", ["definite", "ambiguous", "opt_out"])
def test_sms_adapter_conformance_classifies_and_scrubs_failures(
    provider, kind, tmp_path, monkeypatch,
):
    config = _config(tmp_path, provider)
    adapter = _adapter_with_send_failure(
        provider, config, monkeypatch, _provider_failure(provider, kind),
    )

    outcome = adapter.send(SmsSendRequest(
        USER_NUMBER,
        SERVICE_NUMBER,
        "done",
        "gsm7",
        f"https://assistant.example.test/webhooks/sms/{provider}",
    ))

    assert isinstance(outcome, SmsSendFailure)
    assert outcome.definite is (kind != "ambiguous")
    assert outcome.opted_out is (kind == "opt_out")
    assert outcome.error_code == {
        "definite": "21211" if provider == "twilio" else "40001",
        "ambiguous": None,
        "opt_out": "21610" if provider == "twilio" else "40300",
    }[kind]
    assert "api-secret" not in outcome.safe_reason
    assert USER_NUMBER not in outcome.safe_reason
