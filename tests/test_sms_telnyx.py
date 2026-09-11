from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from telnyx import BadRequestError, APITimeoutError

from istota import db
from istota.config import Config, SmsConfig, TelnyxSmsConfig, UserConfig
from istota.transport.sms.providers._types import (
    InboundSmsEvent,
    SmsDeliveryEvent,
    SmsSendFailure,
    SmsSendRequest,
    SmsSendResult,
    SmsWebhookRequest,
)


PUBLIC_URL = "https://assistant.example.test/webhooks/sms/telnyx"
SERVICE_NUMBER = "+15551230000"
USER_NUMBER = "+15551234567"
PROFILE_ID = "10000000-0000-4000-8000-000000000001"
MESSAGE_ID = "20000000-0000-4000-8000-000000000002"
EVENT_ID = "30000000-0000-4000-8000-000000000003"
_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
PUBLIC_KEY = base64.b64encode(
    _PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
).decode()


def _config(tmp_path) -> Config:
    path = tmp_path / "istota.db"
    db.init_db(path)
    return Config(
        db_path=path,
        site=SimpleNamespace(hostname="assistant.example.test"),
        sms=SmsConfig(
            enabled=True,
            provider="telnyx",
            service_numbers=[SERVICE_NUMBER],
            default_sender_number=SERVICE_NUMBER,
            request_timeout_seconds=7,
            telnyx=TelnyxSmsConfig(
                api_key="telnyx-api-secret",
                public_key=PUBLIC_KEY,
                messaging_profile_id=PROFILE_ID,
            ),
        ),
        users={"alice": UserConfig(sms_phone_number=USER_NUMBER)},
    )


def _inbound_payload(**changes):
    payload = {
        "id": MESSAGE_ID,
        "record_type": "message",
        "direction": "inbound",
        "type": "SMS",
        "messaging_profile_id": PROFILE_ID,
        "from": {"phone_number": USER_NUMBER},
        "to": [{"phone_number": SERVICE_NUMBER}],
        "text": "check the backup",
        "media": [
            {"url": "https://media.example.test/one"},
            {"url": "https://media.example.test/two"},
        ],
    }
    payload.update(changes)
    return {
        "data": {
            "id": EVENT_ID,
            "event_type": "message.received",
            "record_type": "event",
            "payload": payload,
        },
    }


def _delivery_payload(status, **changes):
    payload = {
        "id": MESSAGE_ID,
        "record_type": "message",
        "direction": "outbound",
        "type": "SMS",
        "messaging_profile_id": PROFILE_ID,
        "from": {"phone_number": SERVICE_NUMBER},
        "to": [{"phone_number": USER_NUMBER, "status": status}],
        "parts": 3,
        "errors": [{"code": "30007", "title": "Rejected"}],
    }
    payload.update(changes)
    return {
        "data": {
            "id": EVENT_ID,
            "event_type": "message.finalized",
            "record_type": "event",
            "payload": payload,
        },
    }


def _signed_request(payload, *, timestamp=None, signature=None, raw_body=None):
    body = raw_body if raw_body is not None else json.dumps(
        payload, separators=(",", ":"), sort_keys=True,
    ).encode()
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    signed = stamp.encode() + b"|" + body
    supplied = signature or base64.b64encode(_PRIVATE_KEY.sign(signed)).decode()
    return SmsWebhookRequest(
        raw_body=body,
        headers={
            "content-type": "application/json; charset=utf-8",
            "telnyx-signature-ed25519": supplied,
            "telnyx-timestamp": stamp,
        },
        public_url=PUBLIC_URL,
    )


def test_telnyx_signed_inbound_maps_all_fields_and_returns_204(tmp_path):
    from istota.transport.sms.providers.telnyx import build_adapter

    result = build_adapter(_config(tmp_path)).parse_webhook(
        _signed_request(_inbound_payload())
    )

    assert result.event == InboundSmsEvent(
        provider="telnyx",
        provider_event_id=EVENT_ID,
        provider_message_id=MESSAGE_ID,
        from_number=USER_NUMBER,
        to_number=SERVICE_NUMBER,
        text="check the backup",
        media_count=2,
        opt_out_action=None,
    )
    assert result.response_status == 204
    assert result.response_content_type is None
    assert result.response_body == b""


@pytest.mark.parametrize(
    "change",
    [
        "body",
        "signature",
        "profile",
        "receiving_number",
    ],
)
def test_telnyx_rejects_signature_and_config_mismatches(tmp_path, change):
    from istota.transport.sms.providers.telnyx import TelnyxWebhookError, build_adapter

    payload = _inbound_payload()
    request = _signed_request(payload)
    if change == "body":
        changed = _inbound_payload(text="tampered")
        request = SmsWebhookRequest(
            raw_body=json.dumps(changed, separators=(",", ":"), sort_keys=True).encode(),
            headers=request.headers,
            public_url=request.public_url,
        )
    elif change == "signature":
        request = _signed_request(payload, signature=base64.b64encode(b"x" * 64).decode())
    elif change == "profile":
        request = _signed_request(_inbound_payload(messaging_profile_id="other-profile"))
    elif change == "receiving_number":
        request = _signed_request(
            _inbound_payload(to=[{"phone_number": "+15559999999"}])
        )

    with pytest.raises(TelnyxWebhookError):
        build_adapter(_config(tmp_path)).parse_webhook(request)


def test_telnyx_verifies_before_json_decoding(tmp_path, monkeypatch):
    from istota.transport.sms.providers import telnyx as provider

    decoded = False
    original_loads = json.loads

    def tracked_loads(value, *args, **kwargs):
        nonlocal decoded
        decoded = True
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(provider.json, "loads", tracked_loads)
    request = _signed_request(
        {}, raw_body=b'{"data":{"invalid":true}}',
        signature=base64.b64encode(b"x" * 64).decode(),
    )

    with pytest.raises(provider.TelnyxWebhookError):
        provider.build_adapter(_config(tmp_path)).parse_webhook(request)
    assert decoded is False


@pytest.mark.parametrize(
    "age, accepted",
    [
        (-300, True),
        (300, True),
        (-301, False),
        (301, False),
    ],
)
def test_telnyx_enforces_five_minute_replay_window(
    tmp_path, monkeypatch, age, accepted,
):
    from telnyx.lib import webhook_verification
    from istota.transport.sms.providers.telnyx import TelnyxWebhookError, build_adapter

    now = 2_000_000_000
    monkeypatch.setattr(webhook_verification.time, "time", lambda: now)
    request = _signed_request(_inbound_payload(), timestamp=now + age)

    if accepted:
        assert build_adapter(_config(tmp_path)).parse_webhook(request).response_status == 204
    else:
        with pytest.raises(TelnyxWebhookError):
            build_adapter(_config(tmp_path)).parse_webhook(request)


@pytest.mark.parametrize(
    "body, autoresponse_type, expected",
    [
        ("anything", "STOP", "stop"),
        ("anything", "START", "start"),
        ("anything", "HELP", "help"),
        (" unsubscribe ", None, "stop"),
        ("UNSTOP", None, "start"),
        ("info", None, "help"),
    ],
)
def test_telnyx_maps_advanced_opt_out_and_standard_keywords(
    tmp_path, body, autoresponse_type, expected,
):
    from istota.transport.sms.providers.telnyx import build_adapter

    changes = {"text": body, "media": []}
    if autoresponse_type is not None:
        changes["autoresponse_type"] = autoresponse_type
    event = build_adapter(_config(tmp_path)).parse_webhook(
        _signed_request(_inbound_payload(**changes))
    ).event

    assert isinstance(event, InboundSmsEvent)
    assert event.opt_out_action == expected


@pytest.mark.parametrize(
    "event_type, provider_status, expected",
    [
        ("message.sent", "sent", "sent"),
        ("message.finalized", "queued", "queued"),
        ("message.finalized", "sending", "sent"),
        ("message.finalized", "delivered", "delivered"),
        ("message.finalized", "delivery_unconfirmed", "delivery_unconfirmed"),
        ("message.finalized", "sending_failed", "failed"),
        ("message.finalized", "delivery_failed", "failed"),
        ("message.finalized", "expired", "failed"),
    ],
)
def test_telnyx_delivery_events_map_canonical_state(
    tmp_path, event_type, provider_status, expected,
):
    from istota.transport.sms.providers.telnyx import build_adapter

    payload = _delivery_payload(provider_status)
    payload["data"]["event_type"] = event_type
    result = build_adapter(_config(tmp_path)).parse_webhook(_signed_request(payload))

    assert result.event == SmsDeliveryEvent(
        provider="telnyx",
        provider_event_id=EVENT_ID,
        provider_message_id=MESSAGE_ID,
        status=expected,
        error_code="30007",
        reported_segments=3,
        opted_out=False,
    )
    assert result.response_status == 204


def test_telnyx_delivery_event_maps_opt_out_error(tmp_path):
    from istota.transport.sms.providers.telnyx import build_adapter

    result = build_adapter(_config(tmp_path)).parse_webhook(_signed_request(
        _delivery_payload(
            "delivery_failed",
            errors=[{"code": "40300", "title": "Blocked recipient"}],
        )
    ))

    assert isinstance(result.event, SmsDeliveryEvent)
    assert result.event.error_code == "40300"
    assert result.event.opted_out is True


def test_telnyx_send_uses_api_key_profile_callback_and_explicit_encoding(
    tmp_path, monkeypatch,
):
    from istota.transport.sms.providers import telnyx as provider

    captured = {}

    class FakeMessages:
        def send(self, **kwargs):
            captured["message"] = kwargs
            return SimpleNamespace(data=SimpleNamespace(
                id=MESSAGE_ID,
                to=[SimpleNamespace(status="queued")],
                parts=2,
            ))

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.messages = FakeMessages()

    monkeypatch.setattr(provider, "Telnyx", FakeClient)
    outcome = provider.build_adapter(_config(tmp_path)).send(SmsSendRequest(
        to_number=USER_NUMBER,
        preferred_from_number=SERVICE_NUMBER,
        text="done",
        encoding="ucs2",
        status_callback_url=PUBLIC_URL,
    ))

    assert outcome == SmsSendResult(MESSAGE_ID, "queued", 2)
    assert captured["client"] == {
        "api_key": "telnyx-api-secret",
        "public_key": PUBLIC_KEY,
        "timeout": 7,
        "max_retries": 0,
    }
    assert captured["message"] == {
        "to": USER_NUMBER,
        "from_": SERVICE_NUMBER,
        "text": "done",
        "type": "SMS",
        "encoding": "ucs2",
        "messaging_profile_id": PROFILE_ID,
        "webhook_url": PUBLIC_URL,
        "use_profile_webhooks": False,
    }


@pytest.mark.parametrize(
    "failure, definite, opted_out, code",
    [
        (
            BadRequestError(
                "private +15551234567",
                response=httpx.Response(
                    400, request=httpx.Request("POST", "https://api.example.test")
                ),
                body={"errors": [{"code": "40300", "detail": "telnyx-api-secret"}]},
            ),
            True,
            True,
            "40300",
        ),
        (
            BadRequestError(
                "token=telnyx-api-secret",
                response=httpx.Response(
                    400, request=httpx.Request("POST", "https://api.example.test")
                ),
                body={"errors": [{"code": "40001", "detail": USER_NUMBER}]},
            ),
            True,
            False,
            "40001",
        ),
        (
            APITimeoutError(httpx.Request("POST", "https://api.example.test")),
            False,
            False,
            None,
        ),
        (RuntimeError("telnyx-api-secret"), False, False, None),
    ],
)
def test_telnyx_send_scrubs_and_classifies_failures(
    tmp_path, monkeypatch, failure, definite, opted_out, code,
):
    from istota.transport.sms.providers import telnyx as provider

    class FakeMessages:
        def send(self, **_kwargs):
            raise failure

    class FakeClient:
        def __init__(self, **_kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(provider, "Telnyx", FakeClient)
    outcome = provider.build_adapter(_config(tmp_path)).send(SmsSendRequest(
        USER_NUMBER, SERVICE_NUMBER, "done", "gsm7", PUBLIC_URL,
    ))

    assert outcome == SmsSendFailure(
        definite=definite,
        error_code=code,
        opted_out=opted_out,
        safe_reason=(
            "provider rejected message" if definite else "delivery outcome unknown"
        ),
    )
    assert "telnyx-api-secret" not in outcome.safe_reason
    assert USER_NUMBER not in outcome.safe_reason


def test_telnyx_route_uses_fixed_public_url_and_returns_without_provider_send(
    tmp_path, monkeypatch,
):
    from fastapi.testclient import TestClient

    from istota import webhook_receiver as receiver
    from istota.transport.sms.providers.registry import make_provider_registry

    config = _config(tmp_path)
    monkeypatch.setattr(receiver, "_config", config)
    monkeypatch.setattr(receiver, "_sms_providers", make_provider_registry(config))
    monkeypatch.setattr(receiver, "reload_config", lambda: None)
    monkeypatch.setattr(receiver.signal, "signal", lambda *_args: None)
    started = time.monotonic()

    with TestClient(receiver.app) as client:
        signed = _signed_request(_inbound_payload(media=[]))
        response = client.post(
            "/webhooks/sms/telnyx",
            content=signed.raw_body,
            headers={
                **signed.headers,
                "host": "internal.proxy.invalid",
                "x-forwarded-host": "attacker.invalid",
            },
        )

    assert response.status_code == 204
    assert time.monotonic() - started < 2
    with db.get_db(config.db_path) as conn:
        row = conn.execute("SELECT * FROM processed_sms").fetchone()
        task = db.get_task(conn, row["task_id"])
    assert row["provider_message_id"] == MESSAGE_ID
    assert task.prompt == "check the backup"
    assert task.source_type == "sms"


def test_telnyx_route_rejects_oversize_and_bad_signatures(tmp_path, monkeypatch):
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
            "/webhooks/sms/telnyx",
            content=b"x" * 65537,
            headers={"content-type": "application/json"},
        )
        bad = _signed_request(
            _inbound_payload(), signature=base64.b64encode(b"x" * 64).decode()
        )
        bad_signature = client.post(
            "/webhooks/sms/telnyx",
            content=bad.raw_body,
            headers=bad.headers,
        )

    assert oversized.status_code == 413
    assert bad_signature.status_code == 403
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM processed_sms").fetchone()[0] == 0
