"""The mock harness must be accepted by the real adapter, or it proves nothing.

A harness that drifts from `providers/telnyx.py` is worse than no harness: it
answers a question about itself and reads as an answer about the product. So
every assertion here drives a harness-built, harness-signed payload through
`build_adapter(...).parse_webhook`, the same call the receiver makes, rather
than through a second copy of the parsing.
"""
from __future__ import annotations

import base64
import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota import db
from istota.config import Config, SmsConfig, TelnyxSmsConfig, UserConfig
from istota.transport.sms.providers._types import SmsWebhookError, SmsWebhookRequest
from istota.transport.sms.providers.telnyx import build_adapter

_HARNESS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sms_mock_webhook.py"

SERVICE_NUMBER = "+15551230000"
USER_NUMBER = "+15551234567"
PROFILE_ID = "10000000-0000-4000-8000-000000000001"
PUBLIC_URL = "https://assistant.example.test/webhooks/sms/telnyx"


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location("sms_mock_webhook", _HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def keypair(harness):
    private_b64, public_b64 = harness.generate_keypair()
    return harness.load_private_key(private_b64), public_b64


def _config(tmp_path, public_key: str) -> Config:
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
            telnyx=TelnyxSmsConfig(
                api_key="telnyx-api-secret",
                public_key=public_key,
                messaging_profile_id=PROFILE_ID,
            ),
        ),
        users={"alice": UserConfig(sms_phone_number=USER_NUMBER)},
    )


def _request(harness, payload, private_key, **kwargs) -> SmsWebhookRequest:
    body, headers = harness.sign_payload(payload, private_key, **kwargs)
    return SmsWebhookRequest(raw_body=body, headers=headers, public_url=PUBLIC_URL)


class TestTheHarnessSpeaksTheAdaptersLanguage:
    def test_a_signed_inbound_event_parses_into_the_message(
        self, tmp_path, harness, keypair
    ):
        private_key, public_key = keypair
        payload = harness.inbound_payload(
            from_number=USER_NUMBER,
            to_number=SERVICE_NUMBER,
            profile_id=PROFILE_ID,
            text="check the backup",
        )

        result = build_adapter(_config(tmp_path, public_key)).parse_webhook(
            _request(harness, payload, private_key)
        )

        assert result.event is not None
        assert result.event.from_number == USER_NUMBER
        assert result.event.to_number == SERVICE_NUMBER
        assert result.event.text == "check the backup"
        assert result.event.media_count == 0

    def test_media_reaches_the_adapter_as_a_count(self, tmp_path, harness, keypair):
        private_key, public_key = keypair
        payload = harness.inbound_payload(
            from_number=USER_NUMBER,
            to_number=SERVICE_NUMBER,
            profile_id=PROFILE_ID,
            text="",
            media=[{"url": "https://media.example.test/one"}],
        )

        result = build_adapter(_config(tmp_path, public_key)).parse_webhook(
            _request(harness, payload, private_key)
        )

        assert result.event.media_count == 1

    def test_a_signed_delivery_callback_parses_with_its_status(
        self, tmp_path, harness, keypair
    ):
        private_key, public_key = keypair
        payload = harness.delivery_payload(
            from_number=SERVICE_NUMBER,
            to_number=USER_NUMBER,
            profile_id=PROFILE_ID,
            status="delivered",
            message_id="20000000-0000-4000-8000-000000000002",
            parts=2,
        )

        result = build_adapter(_config(tmp_path, public_key)).parse_webhook(
            _request(harness, payload, private_key)
        )

        assert result.event is not None
        assert result.event.provider_message_id == "20000000-0000-4000-8000-000000000002"
        assert result.event.reported_segments == 2


class TestTheHarnessCannotForgeWhatItShouldNot:
    """Controls. Without these the acceptance tests above are equally true of
    an adapter that verifies nothing, which is the failure this file exists to
    rule out."""

    def test_a_foreign_key_is_refused(self, tmp_path, harness, keypair):
        _, public_key = keypair
        other_private, _ = harness.generate_keypair()
        payload = harness.inbound_payload(
            from_number=USER_NUMBER,
            to_number=SERVICE_NUMBER,
            profile_id=PROFILE_ID,
            text="forged",
        )

        with pytest.raises(SmsWebhookError):
            build_adapter(_config(tmp_path, public_key)).parse_webhook(
                _request(harness, payload, harness.load_private_key(other_private))
            )

    def test_a_body_edited_after_signing_is_refused(self, tmp_path, harness, keypair):
        private_key, public_key = keypair
        payload = harness.inbound_payload(
            from_number=USER_NUMBER,
            to_number=SERVICE_NUMBER,
            profile_id=PROFILE_ID,
            text="original",
        )
        body, headers = harness.sign_payload(payload, private_key)
        tampered = body.replace(b"original", b"tampered")
        assert len(tampered) == len(body)

        with pytest.raises(SmsWebhookError):
            build_adapter(_config(tmp_path, public_key)).parse_webhook(
                SmsWebhookRequest(
                    raw_body=tampered, headers=headers, public_url=PUBLIC_URL
                )
            )

    def test_a_stale_timestamp_is_refused(self, tmp_path, harness, keypair):
        private_key, public_key = keypair
        payload = harness.inbound_payload(
            from_number=USER_NUMBER,
            to_number=SERVICE_NUMBER,
            profile_id=PROFILE_ID,
            text="replayed",
        )

        with pytest.raises(SmsWebhookError):
            build_adapter(_config(tmp_path, public_key)).parse_webhook(
                _request(
                    harness, payload, private_key, timestamp=int(time.time()) - 86400
                )
            )


class TestTheKeygenOutputIsWhatTheConfigWants:
    def test_the_public_half_is_a_32_byte_base64_key(self, harness):
        _, public_b64 = harness.generate_keypair()

        assert len(base64.b64decode(public_b64)) == 32

    def test_a_malformed_private_key_is_rejected_with_its_length(self, harness):
        with pytest.raises(ValueError, match="32 bytes"):
            harness.load_private_key(base64.b64encode(b"short").decode())
