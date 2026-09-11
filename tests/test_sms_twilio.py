from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from requests.exceptions import ReadTimeout
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from istota import db
from istota.config import Config, SmsConfig, TwilioSmsConfig, UserConfig
from istota.transport.sms.providers._types import (
    InboundSmsEvent,
    SmsDeliveryEvent,
    SmsSendFailure,
    SmsSendRequest,
    SmsSendResult,
    SmsWebhookRequest,
)


PUBLIC_URL = "https://assistant.example.test/webhooks/sms/twilio"
SERVICE_NUMBER = "+15551230000"
USER_NUMBER = "+15551234567"


def _config(tmp_path) -> Config:
    path = tmp_path / "istota.db"
    db.init_db(path)
    return Config(
        db_path=path,
        site=SimpleNamespace(hostname="assistant.example.test"),
        sms=SmsConfig(
            enabled=True,
            provider="twilio",
            service_numbers=[SERVICE_NUMBER],
            default_sender_number=SERVICE_NUMBER,
            request_timeout_seconds=7,
            twilio=TwilioSmsConfig(
                account_sid="AC-account",
                auth_token="webhook-secret",
                api_key_sid="SK-api-key",
                api_key_secret="api-secret",
                messaging_service_sid="MG-service",
            ),
        ),
        users={"alice": UserConfig(sms_phone_number=USER_NUMBER)},
    )


def _signed_request(params, *, url=PUBLIC_URL, body=None, signature=None):
    raw = body if body is not None else urlencode(params).encode()
    supplied = signature or RequestValidator("webhook-secret").compute_signature(
        url, params,
    )
    return SmsWebhookRequest(
        raw_body=raw,
        headers={
            "content-type": "application/x-www-form-urlencoded; charset=utf-8",
            "x-twilio-signature": supplied,
        },
        public_url=url,
    )


def _inbound_params(**changes):
    params = {
        "AccountSid": "AC-account",
        "MessagingServiceSid": "MG-service",
        "MessageSid": "SM-inbound",
        "From": USER_NUMBER,
        "To": SERVICE_NUMBER,
        "Body": "check the backup",
        "NumMedia": "2",
    }
    params.update(changes)
    return params


def test_twilio_signed_inbound_maps_all_fields_and_empty_twiml(tmp_path):
    from istota.transport.sms.providers.twilio import build_adapter

    adapter = build_adapter(_config(tmp_path))
    result = adapter.parse_webhook(_signed_request(_inbound_params()))

    assert result.event == InboundSmsEvent(
        provider="twilio",
        provider_event_id=None,
        provider_message_id="SM-inbound",
        from_number=USER_NUMBER,
        to_number=SERVICE_NUMBER,
        text="check the backup",
        media_count=2,
        opt_out_action=None,
    )
    assert result.response_status == 200
    assert result.response_content_type == "application/xml"
    assert b"<Response" in result.response_body
    assert b"<Message" not in result.response_body


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://internal.invalid/webhooks/sms/twilio"},
        {"field": ("Body", "tampered")},
        {"field": ("AccountSid", "AC-other")},
        {"field": ("MessagingServiceSid", "MG-other")},
        {"field": ("To", "+15559999999")},
    ],
)
def test_twilio_rejects_signature_and_config_mismatches(tmp_path, change):
    from istota.transport.sms.providers.twilio import TwilioWebhookError, build_adapter

    adapter = build_adapter(_config(tmp_path))
    signed_params = _inbound_params()
    request = _signed_request(signed_params)
    if "url" in change:
        request = SmsWebhookRequest(
            raw_body=request.raw_body,
            headers=request.headers,
            public_url=change["url"],
        )
    if "field" in change:
        name, value = change["field"]
        changed_params = dict(signed_params)
        changed_params[name] = value
        headers = request.headers
        if name != "Body":
            headers = _signed_request(changed_params).headers
        request = SmsWebhookRequest(
            raw_body=urlencode(changed_params).encode(),
            headers=headers,
            public_url=PUBLIC_URL,
        )

    with pytest.raises(TwilioWebhookError):
        adapter.parse_webhook(request)


@pytest.mark.parametrize(
    "body, opt_out_type, expected",
    [
        ("anything", "STOP", "stop"),
        ("anything", "START", "start"),
        ("anything", "HELP", "help"),
        (" unsubscribe ", "", "stop"),
        ("UNSTOP", "", "start"),
        ("info", "", "help"),
    ],
)
def test_twilio_maps_advanced_opt_out_and_standard_keywords(
    tmp_path, body, opt_out_type, expected,
):
    from istota.transport.sms.providers.twilio import build_adapter

    params = _inbound_params(Body=body, NumMedia="0", OptOutType=opt_out_type)
    event = build_adapter(_config(tmp_path)).parse_webhook(
        _signed_request(params)
    ).event

    assert isinstance(event, InboundSmsEvent)
    assert event.opt_out_action == expected


@pytest.mark.parametrize(
    "provider_status, expected",
    [
        ("accepted", "accepted"),
        ("queued", "queued"),
        ("sending", "sent"),
        ("sent", "sent"),
        ("delivered", "delivered"),
        ("undelivered", "failed"),
        ("failed", "failed"),
        ("canceled", "failed"),
    ],
)
def test_twilio_status_callback_maps_delivery_state(
    tmp_path, provider_status, expected,
):
    from istota.transport.sms.providers.twilio import build_adapter

    params = _inbound_params(
        MessageSid="SM-outbound",
        MessageStatus=provider_status,
        From=SERVICE_NUMBER,
        To=USER_NUMBER,
        ErrorCode="30007",
        NumSegments="3",
    )
    result = build_adapter(_config(tmp_path)).parse_webhook(_signed_request(params))

    assert result.event == SmsDeliveryEvent(
        provider="twilio",
        provider_event_id=None,
        provider_message_id="SM-outbound",
        status=expected,
        error_code="30007",
        reported_segments=3,
        opted_out=False,
    )
    assert result.response_status == 204
    assert result.response_body == b""


def test_twilio_send_uses_api_key_service_sender_and_callback(tmp_path, monkeypatch):
    from istota.transport.sms.providers import twilio as provider

    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured["message"] = kwargs
            return SimpleNamespace(sid="SM-created", status="queued", num_segments="2")

    class FakeClient:
        def __init__(self, username, password, account_sid, http_client):
            captured["auth"] = (username, password, account_sid)
            captured["timeout"] = http_client.timeout
            self.messages = FakeMessages()

    monkeypatch.setattr(provider, "Client", FakeClient)
    adapter = provider.build_adapter(_config(tmp_path))
    outcome = adapter.send(SmsSendRequest(
        to_number=USER_NUMBER,
        preferred_from_number=SERVICE_NUMBER,
        text="done",
        encoding="gsm7",
        status_callback_url=PUBLIC_URL,
    ))

    assert outcome == SmsSendResult("SM-created", "queued", 2)
    assert captured["auth"] == ("SK-api-key", "api-secret", "AC-account")
    assert captured["timeout"] == 7
    assert captured["message"] == {
        "to": USER_NUMBER,
        "from_": SERVICE_NUMBER,
        "body": "done",
        "messaging_service_sid": "MG-service",
        "status_callback": PUBLIC_URL,
    }


@pytest.mark.parametrize(
    "failure, definite, opted_out, code",
    [
        (TwilioRestException(400, "/Messages", "private +15551234567", 21610), True, True, "21610"),
        (TwilioRestException(400, "/Messages", "token=api-secret", 21211), True, False, "21211"),
        (ReadTimeout("request contained api-secret"), False, False, None),
        (RuntimeError("api-secret"), False, False, None),
    ],
)
def test_twilio_send_scrubs_and_classifies_failures(
    tmp_path, monkeypatch, failure, definite, opted_out, code,
):
    from istota.transport.sms.providers import twilio as provider

    class FakeMessages:
        def create(self, **_kwargs):
            raise failure

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(provider, "Client", FakeClient)
    outcome = provider.build_adapter(_config(tmp_path)).send(SmsSendRequest(
        USER_NUMBER, SERVICE_NUMBER, "done", "gsm7", PUBLIC_URL,
    ))

    assert outcome == SmsSendFailure(
        definite=definite,
        error_code=code,
        opted_out=opted_out,
        safe_reason="provider rejected message" if definite else "delivery outcome unknown",
    )
    assert "api-secret" not in outcome.safe_reason
    assert USER_NUMBER not in outcome.safe_reason


def test_twilio_route_uses_configured_public_url_and_persists_event(
    tmp_path, monkeypatch,
):
    from fastapi.testclient import TestClient

    from istota import webhook_receiver as receiver
    from istota.transport.sms.providers.registry import make_provider_registry

    config = _config(tmp_path)
    params = _inbound_params(NumMedia="0")
    body = urlencode(params).encode()
    signature = RequestValidator("webhook-secret").compute_signature(PUBLIC_URL, params)
    monkeypatch.setattr(receiver, "_config", config)
    monkeypatch.setattr(receiver, "_sms_providers", make_provider_registry(config))
    monkeypatch.setattr(receiver, "reload_config", lambda: None)
    monkeypatch.setattr(receiver.signal, "signal", lambda *_args: None)

    with TestClient(receiver.app) as client:
        response = client.post(
            "/webhooks/sms/twilio",
            content=body,
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "x-twilio-signature": signature,
                "host": "internal.proxy.invalid",
                "x-forwarded-host": "attacker.invalid",
            },
        )

    assert response.status_code == 200
    with db.get_db(config.db_path) as conn:
        row = conn.execute("SELECT * FROM processed_sms").fetchone()
        task = db.get_task(conn, row["task_id"])
    assert row["provider_message_id"] == "SM-inbound"
    assert task.prompt == "check the backup"
    assert task.source_type == "sms"


def test_twilio_route_rejects_oversize_and_bad_signatures(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from istota import webhook_receiver as receiver
    from istota.transport.sms.providers.registry import make_provider_registry

    config = _config(tmp_path)
    monkeypatch.setattr(receiver, "_config", config)
    monkeypatch.setattr(receiver, "_sms_providers", make_provider_registry(config))
    monkeypatch.setattr(receiver, "reload_config", lambda: None)
    monkeypatch.setattr(receiver.signal, "signal", lambda *_args: None)

    with TestClient(receiver.app) as client:
        oversized = client.post(
            "/webhooks/sms/twilio",
            content=b"x" * 65537,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        bad_signature = client.post(
            "/webhooks/sms/twilio",
            content=urlencode(_inbound_params()).encode(),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "x-twilio-signature": "invalid",
            },
        )

    assert oversized.status_code == 413
    assert bad_signature.status_code == 403
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM processed_sms").fetchone()[0] == 0
