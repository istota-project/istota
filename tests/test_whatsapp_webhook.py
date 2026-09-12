"""The signed WhatsApp wire contract and the inbound transaction.

Three layers, and each one has a job the others cannot do.

`TestTheVerificationHandshake` and `TestTheSignedPostRoute` drive the real
FastAPI routes with real bytes, because the authentication boundary is bytes
and a header — a test that calls the normalizer with a dict has authenticated
nothing. `TestPayloadNormalization` drives the normalizer directly, because
walking every entry, change, message and status is a claim about payload
*shapes* and building forty signed requests to make it would say less. The rest
drive `handle_whatsapp_batch` on an open connection, the way the SMS suite
drives `handle_provider_event`, because the disposition ladder is database
behaviour rather than HTTP behaviour.

Two things every case here holds, and they are the reason the surface exists in
this shape at all: no room, binding, membership or canonical `messages` row is
ever written, and no authenticated identifier — phone number, BSUID, send id,
message body — reaches a log line.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from istota import db
from istota.config import Config, UserConfig, WhatsAppConfig
from istota.transport.whatsapp import whatsapp_conversation_token
from istota.transport.whatsapp.webhook import (
    MAX_WEBHOOK_BODY,
    WhatsAppWebhookError,
    handle_whatsapp_batch,
    normalize_payload,
    parse_webhook,
    verify_subscription,
)

WABA_ID = "123456789012345"
PHONE_NUMBER_ID = "223456789012345"
APP_SECRET = "wa-app-secret"
VERIFY_TOKEN = "wa-verify-token"
USER_WA_ID = "15551234567"
USER_NUMBER = "+15551234567"
USER_BSUID = "US.9876543210"
OTHER_BSUID = "US.1111111111"


# ---------------------------------------------------------------------------
# Fixtures and payload builders
# ---------------------------------------------------------------------------


def _config(tmp_path, *, enabled: bool = True, **overrides) -> Config:
    path = tmp_path / "istota.db"
    db.init_db(path)
    fields = dict(
        enabled=enabled,
        waba_id=WABA_ID,
        phone_number_id=PHONE_NUMBER_ID,
        business_phone_number="+15551230000",
        access_token="wa-access-token",
        app_secret=APP_SECRET,
        verify_token=VERIFY_TOKEN,
        business_timezone="UTC",
    )
    fields.update(overrides)
    config = Config(
        db_path=path,
        temp_dir=tmp_path / "tmp",
        whatsapp=WhatsAppConfig(**fields),
        users={"alice": UserConfig()},
    )
    config.site.hostname = "assistant.example.com"
    return config


def _bind(config, user_id="alice", **kwargs):
    with db.get_db(config.db_path) as conn:
        return db.set_whatsapp_binding(conn, user_id, **kwargs)


def _contact(*, bsuid=USER_BSUID, wa_id=USER_WA_ID, username=None, name="Alice"):
    contact: dict = {"user_id": bsuid, "profile": {"name": name}}
    if wa_id is not None:
        contact["wa_id"] = wa_id
    if username is not None:
        contact["profile"]["username"] = username
    return contact


def _text_message(*, message_id="wamid.001", text="check the backup", sender=None,
                  timestamp=None, **extra):
    message = {
        "id": message_id,
        "from": sender if sender is not None else USER_BSUID,
        "timestamp": str(int((timestamp or datetime.now(timezone.utc)).timestamp())),
        "type": "text",
        "text": {"body": text},
    }
    message.update(extra)
    return message


def _button_message(*, message_id="wamid.btn", payload="confirm:7:yes", sender=None):
    return {
        "id": message_id,
        "from": sender if sender is not None else USER_BSUID,
        "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": payload, "title": "Yes"},
        },
    }


def _value(*, contacts=None, messages=None, statuses=None,
           phone_number_id=PHONE_NUMBER_ID, messaging_product="whatsapp"):
    value: dict = {
        "messaging_product": messaging_product,
        "metadata": {
            "display_phone_number": "15551230000",
            "phone_number_id": phone_number_id,
        },
    }
    if contacts is not None:
        value["contacts"] = contacts
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return value


def _payload(*values, waba_id=WABA_ID, obj="whatsapp_business_account", field="messages"):
    return {
        "object": obj,
        "entry": [
            {
                "id": waba_id,
                "changes": [{"field": field, "value": value} for value in values],
            }
        ],
    }


def _text_payload(**message_kwargs):
    contact = message_kwargs.pop("contact", None) or _contact()
    return _payload(_value(contacts=[contact], messages=[_text_message(**message_kwargs)]))


def _body(payload) -> bytes:
    return json.dumps(payload).encode()


def _sign(raw: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _post_headers(raw: bytes, *, secret: str = APP_SECRET, signature=None):
    return {
        "content-type": "application/json",
        "X-Hub-Signature-256": _sign(raw, secret) if signature is None else signature,
    }


def _receiver(config, monkeypatch):
    """The real FastAPI app with this test's config bound into it."""
    from istota import webhook_receiver

    monkeypatch.setattr(webhook_receiver, "_config", config)
    return TestClient(webhook_receiver.app)


def _handle(config, payload, *, conn=None):
    events = normalize_payload(config, payload)
    if conn is not None:
        return handle_whatsapp_batch(conn, config, events)
    with db.get_db(config.db_path) as connection:
        return handle_whatsapp_batch(connection, config, events)


def _dispositions(results):
    return [result.disposition for result in results]


def _istota_log(caplog) -> str:
    """Only records istota's own loggers emitted.

    `caplog.text` also carries the httpx client inside `TestClient` logging the
    request line it sent — a fact about the harness, not about the product.
    """
    return "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("istota")
    )


def _counts(config, *tables):
    with db.get_db(config.db_path) as conn:
        return [
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in tables
        ]


# ---------------------------------------------------------------------------
# GET: Meta's subscription handshake
# ---------------------------------------------------------------------------


class TestTheVerificationHandshake:
    def test_the_matching_token_gets_the_challenge_back_as_plain_text(
        self, tmp_path, monkeypatch,
    ):
        client = _receiver(_config(tmp_path), monkeypatch)

        response = client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "1158201444",
            },
        )

        assert response.status_code == 200
        assert response.text == "1158201444"
        assert response.headers["content-type"].startswith("text/plain")

    @pytest.mark.parametrize(
        "params, status",
        [
            ({"hub.mode": "subscribe", "hub.verify_token": "wrong",
              "hub.challenge": "1"}, 403),
            ({"hub.mode": "subscribe", "hub.verify_token": "",
              "hub.challenge": "1"}, 403),
            ({"hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1"}, 400),
            ({"hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN,
              "hub.challenge": "1"}, 400),
            ({"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
              "hub.challenge": "<script>x</script>"}, 400),
            ({"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
              "hub.challenge": ""}, 400),
            ({"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
              "hub.challenge": "1" * 200}, 400),
        ],
    )
    def test_every_other_handshake_is_refused(
        self, tmp_path, monkeypatch, params, status,
    ):
        client = _receiver(_config(tmp_path), monkeypatch)

        assert client.get("/webhooks/whatsapp", params=params).status_code == status

    def test_a_prefix_of_the_token_is_not_accepted(self, tmp_path):
        config = _config(tmp_path)

        with pytest.raises(WhatsAppWebhookError) as excinfo:
            verify_subscription(config, {
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN[:-1],
                "hub.challenge": "1",
            })
        assert excinfo.value.status_code == 403

    def test_a_disabled_transport_has_no_endpoint(self, tmp_path, monkeypatch):
        client = _receiver(_config(tmp_path, enabled=False), monkeypatch)

        assert client.get("/webhooks/whatsapp", params={
            "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1",
        }).status_code == 404

    def test_an_enabled_transport_with_no_verify_token_refuses_rather_than_matching_empty(
        self, tmp_path, monkeypatch,
    ):
        client = _receiver(_config(tmp_path, verify_token=""), monkeypatch)

        response = client.get("/webhooks/whatsapp", params={
            "hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1",
        })

        assert response.status_code == 503

    def test_the_verify_token_never_reaches_a_log_line(
        self, tmp_path, monkeypatch, caplog,
    ):
        """istota's own records only.

        The httpx client inside `TestClient` logs the request line it sent,
        query string and all, which is a fact about the harness. The
        deployment's equivalent is nginx's access log, and the answer to that
        one is nginx configuration rather than anything istota can assert.
        """
        caplog.set_level("DEBUG")
        client = _receiver(_config(tmp_path), monkeypatch)

        client.get("/webhooks/whatsapp", params={
            "hub.mode": "subscribe", "hub.verify_token": "guessed-token",
            "hub.challenge": "1",
        })

        ours = _istota_log(caplog)
        assert "whatsapp.verify.rejected" in ours
        assert VERIFY_TOKEN not in ours
        assert "guessed-token" not in ours


# ---------------------------------------------------------------------------
# POST: bounds, signature, and the acknowledgement contract
# ---------------------------------------------------------------------------


class TestTheSignedPostRoute:
    def test_a_correctly_signed_batch_is_acknowledged_and_creates_the_task(
        self, tmp_path, monkeypatch,
    ):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER)
        client = _receiver(config, monkeypatch)
        raw = _body(_text_payload())

        response = client.post(
            "/webhooks/whatsapp", content=raw, headers=_post_headers(raw),
        )

        assert response.status_code == 200
        assert response.content == b""
        assert _counts(config, "processed_whatsapp") == [1]

    @pytest.mark.parametrize(
        "signature",
        [
            None,                       # header absent entirely
            "",
            "sha256=" + "0" * 64,
            "not-a-signature",
        ],
    )
    def test_a_bad_signature_is_403_with_no_database_write(
        self, tmp_path, monkeypatch, signature,
    ):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER)
        client = _receiver(config, monkeypatch)
        raw = _body(_text_payload())
        headers = {"content-type": "application/json"}
        if signature is not None:
            headers["X-Hub-Signature-256"] = signature

        response = client.post("/webhooks/whatsapp", content=raw, headers=headers)

        assert response.status_code == 403
        assert _counts(config, "processed_whatsapp", "tasks") == [0, 0]

    def test_a_signature_from_another_app_secret_is_refused(
        self, tmp_path, monkeypatch,
    ):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER)
        client = _receiver(config, monkeypatch)
        raw = _body(_text_payload())

        response = client.post(
            "/webhooks/whatsapp", content=raw,
            headers=_post_headers(raw, secret="someone-elses-secret"),
        )

        assert response.status_code == 403
        assert _counts(config, "processed_whatsapp") == [0]

    def test_one_edited_byte_after_signing_is_refused(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER)
        client = _receiver(config, monkeypatch)
        raw = _body(_text_payload())
        signature = _sign(raw)
        tampered = raw.replace(b"check the backup", b"check the backuq")
        assert len(tampered) == len(raw)

        response = client.post(
            "/webhooks/whatsapp", content=tampered,
            headers={"content-type": "application/json",
                     "X-Hub-Signature-256": signature},
        )

        assert response.status_code == 403
        assert _counts(config, "processed_whatsapp") == [0]

    def test_the_signature_is_checked_before_the_json_is_decoded(self, tmp_path):
        """Unparseable bytes with a bad signature answer 403, never 400.

        A 400 here would say the body was read, which is the thing a signature
        check exists to happen first. It is also the difference between a
        rejected request and a JSON parser fed attacker bytes.
        """
        config = _config(tmp_path)
        raw = b"{not json at all"

        with pytest.raises(WhatsAppWebhookError) as excinfo:
            parse_webhook(
                config, raw,
                {"content-type": "application/json",
                 "X-Hub-Signature-256": "sha256=" + "0" * 64},
            )
        assert excinfo.value.status_code == 403

    def test_an_authenticated_but_unparseable_body_is_400(self, tmp_path):
        config = _config(tmp_path)
        raw = b"{not json at all"

        with pytest.raises(WhatsAppWebhookError) as excinfo:
            parse_webhook(config, raw, _post_headers(raw))
        assert excinfo.value.status_code == 400

    def test_an_oversize_body_is_refused_before_the_signature(
        self, tmp_path, monkeypatch,
    ):
        config = _config(tmp_path)
        client = _receiver(config, monkeypatch)
        filler = "x" * (MAX_WEBHOOK_BODY + 1024)
        raw = _body(_text_payload(text=filler))
        assert len(raw) > MAX_WEBHOOK_BODY

        response = client.post(
            "/webhooks/whatsapp", content=raw, headers=_post_headers(raw),
        )

        assert response.status_code == 413
        assert _counts(config, "processed_whatsapp") == [0]

    def test_the_cap_is_the_256_kib_the_spec_names(self):
        assert MAX_WEBHOOK_BODY == 256 * 1024

    @pytest.mark.parametrize(
        "content_type", ["application/x-www-form-urlencoded", "text/plain", ""],
    )
    def test_a_non_json_content_type_is_refused(self, tmp_path, content_type):
        config = _config(tmp_path)
        raw = _body(_text_payload())
        headers = _post_headers(raw)
        headers["content-type"] = content_type

        with pytest.raises(WhatsAppWebhookError) as excinfo:
            parse_webhook(config, raw, headers)
        assert excinfo.value.status_code == 415

    def test_a_json_content_type_with_a_charset_parameter_is_accepted(self, tmp_path):
        config = _config(tmp_path)
        raw = _body(_text_payload())
        headers = _post_headers(raw)
        headers["content-type"] = "application/json; charset=utf-8"

        assert len(parse_webhook(config, raw, headers)) == 1

    def test_an_enabled_transport_with_no_app_secret_refuses_every_post(self, tmp_path):
        """The `pywa` configuration the spec calls silently disabled validation.

        With no secret the validator would verify an HMAC under an empty key,
        which any caller can compute. The route must refuse to serve at all.
        """
        config = _config(tmp_path, app_secret="")
        raw = _body(_text_payload())

        with pytest.raises(WhatsAppWebhookError) as excinfo:
            parse_webhook(config, raw, _post_headers(raw, secret=""))
        assert excinfo.value.status_code == 503

    def test_a_database_failure_rolls_back_and_answers_503(self, tmp_path, monkeypatch):
        """The retryable path. Nothing may be half-applied and acknowledged."""
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER)
        client = _receiver(config, monkeypatch)
        raw = _body(_body_two_messages())

        real_ingest = None
        from istota.transport.whatsapp import webhook as webhook_module

        real_ingest = webhook_module.ingest_message
        calls = {"n": 0}

        def fail_on_second(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real_ingest(*args, **kwargs)

        monkeypatch.setattr(webhook_module, "ingest_message", fail_on_second)

        response = client.post(
            "/webhooks/whatsapp", content=raw, headers=_post_headers(raw),
        )

        assert response.status_code == 503
        # The first message's claim, its task and the binding touch all went
        # with the rollback: a partially applied batch that answered 200 would
        # never be retried and the second message would be lost for good.
        assert _counts(config, "processed_whatsapp", "tasks") == [0, 0]

    def test_the_route_never_logs_the_body_the_number_or_the_bsuid(
        self, tmp_path, monkeypatch, caplog,
    ):
        caplog.set_level("DEBUG")
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER)
        client = _receiver(config, monkeypatch)
        raw = _body(_text_payload(text="my bank code is 4242"))

        client.post("/webhooks/whatsapp", content=raw, headers=_post_headers(raw))

        for secret in ("my bank code is 4242", USER_NUMBER, USER_WA_ID,
                       USER_BSUID, APP_SECRET, "wa-access-token"):
            assert secret not in caplog.text


def _body_two_messages():
    return _payload(_value(
        contacts=[_contact()],
        messages=[
            _text_message(message_id="wamid.first", text="first"),
            _text_message(message_id="wamid.second", text="second"),
        ],
    ))


# ---------------------------------------------------------------------------
# Normalization: every entry, change, message and status
# ---------------------------------------------------------------------------


class TestPayloadNormalization:
    def test_every_entry_change_message_and_status_is_walked(self, tmp_path):
        """PyWa reads `entry[0]["changes"][0]["value"]["messages"][0]`.

        That is the whole reason the normalizer is local. A batch Meta is
        entitled to send would lose everything but its first element.
        """
        config = _config(tmp_path)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": WABA_ID,
                    "changes": [
                        {"field": "messages", "value": _value(
                            contacts=[_contact()],
                            messages=[_text_message(message_id="wamid.a", text="a")],
                        )},
                        {"field": "messages", "value": _value(
                            contacts=[_contact()],
                            messages=[_text_message(message_id="wamid.b", text="b")],
                            statuses=[_status(message_id="wamid.out1")],
                        )},
                    ],
                },
                {
                    "id": WABA_ID,
                    "changes": [
                        {"field": "messages", "value": _value(
                            contacts=[_contact()],
                            messages=[_text_message(message_id="wamid.c", text="c")],
                            statuses=[_status(message_id="wamid.out2",
                                              status="delivered")],
                        )},
                    ],
                },
            ],
        }

        events = normalize_payload(config, payload)

        assert [getattr(e, "message_id") for e in events] == [
            "wamid.a", "wamid.b", "wamid.out1", "wamid.c", "wamid.out2",
        ]
        assert [e.text for e in events if hasattr(e, "text")] == ["a", "b", "c"]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"waba_id": "999999999999999"},
            {"obj": "page"},
        ],
    )
    def test_another_waba_or_object_is_refused_with_403(self, tmp_path, kwargs):
        config = _config(tmp_path)

        with pytest.raises(WhatsAppWebhookError) as excinfo:
            normalize_payload(config, _payload(
                _value(contacts=[_contact()], messages=[_text_message()]), **kwargs,
            ))
        assert excinfo.value.status_code == 403

    @pytest.mark.parametrize(
        "value_kwargs",
        [
            {"phone_number_id": "999999999999999"},
            {"messaging_product": "instagram"},
        ],
    )
    def test_another_phone_number_or_product_is_refused_with_403(
        self, tmp_path, value_kwargs,
    ):
        config = _config(tmp_path)

        with pytest.raises(WhatsAppWebhookError) as excinfo:
            normalize_payload(config, _payload(_value(
                contacts=[_contact()], messages=[_text_message()], **value_kwargs,
            )))
        assert excinfo.value.status_code == 403

    def test_an_unknown_field_is_acknowledged_and_logged_by_name_only(
        self, tmp_path, caplog,
    ):
        caplog.set_level("INFO")
        config = _config(tmp_path)

        events = normalize_payload(config, _payload(
            _value(contacts=[_contact()], messages=[_text_message()]),
            field="smb_message_echoes",
        ))

        assert events == []
        assert "smb_message_echoes" in caplog.text
        assert USER_BSUID not in caplog.text
        assert "check the backup" not in caplog.text

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            {"object": "whatsapp_business_account"},
            {"object": "whatsapp_business_account", "entry": {}},
            {"object": "whatsapp_business_account", "entry": [{"id": WABA_ID}]},
            {"object": "whatsapp_business_account",
             "entry": [{"id": WABA_ID, "changes": [{"field": "messages"}]}]},
        ],
    )
    def test_a_malformed_envelope_is_400_and_yields_no_partial_batch(
        self, tmp_path, payload,
    ):
        config = _config(tmp_path)

        with pytest.raises(WhatsAppWebhookError) as excinfo:
            normalize_payload(config, payload)
        assert excinfo.value.status_code == 400

    def test_a_malformed_item_beside_a_good_one_yields_nothing_at_all(self, tmp_path):
        """No partial acknowledgement of an uncommitted batch.

        Normalization runs to completion before the transaction opens, so a
        broken element cannot leave the first message applied and the rest
        dropped behind a 200.
        """
        config = _config(tmp_path)

        with pytest.raises(WhatsAppWebhookError):
            normalize_payload(config, _payload(_value(
                contacts=[_contact()],
                messages=[
                    _text_message(message_id="wamid.good"),
                    {"id": "wamid.broken", "type": "text"},   # no timestamp
                ],
            )))

    def test_a_message_is_paired_with_its_own_sender(self, tmp_path):
        """Two senders in one change value must not be attributed positionally.

        Getting this wrong is a principal takeover, not a mislabelled row: the
        identity decides which Istota user the message may act as.
        """
        config = _config(tmp_path)
        events = normalize_payload(config, _payload(_value(
            contacts=[_contact(bsuid=OTHER_BSUID, wa_id="15559998888"), _contact()],
            messages=[
                _text_message(message_id="wamid.mine", sender=USER_BSUID),
                _text_message(message_id="wamid.theirs", sender=OTHER_BSUID),
            ],
        )))

        assert [(e.message_id, e.from_user.bsuid) for e in events] == [
            ("wamid.mine", USER_BSUID),
            ("wamid.theirs", OTHER_BSUID),
        ]

    def test_a_sender_matching_no_contact_yields_no_identity_rather_than_a_guess(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        events = normalize_payload(config, _payload(_value(
            contacts=[_contact()],
            messages=[
                _text_message(message_id="wamid.x", sender="US.0000000000"),
                _text_message(message_id="wamid.y", sender=USER_BSUID),
            ],
        )))

        assert events[0].from_user.bsuid == ""
        assert events[1].from_user.bsuid == USER_BSUID

    def test_a_username_only_contact_carries_no_wa_id(self, tmp_path):
        config = _config(tmp_path)
        events = normalize_payload(config, _payload(_value(
            contacts=[_contact(wa_id=None, username="alice.w")],
            messages=[_text_message()],
        )))

        assert events[0].from_user.wa_id is None
        assert events[0].from_user.username == "alice.w"
        assert events[0].from_user.bsuid == USER_BSUID

    def test_a_button_reply_becomes_callback_data_and_not_text(self, tmp_path):
        config = _config(tmp_path)
        events = normalize_payload(config, _payload(_value(
            contacts=[_contact()], messages=[_button_message()],
        )))

        assert events[0].message_type == "interactive"
        assert events[0].callback_data == "confirm:7:yes"
        assert events[0].text is None

    def test_a_template_quick_reply_becomes_callback_data(self, tmp_path):
        config = _config(tmp_path)
        events = normalize_payload(config, _payload(_value(
            contacts=[_contact()],
            messages=[{
                "id": "wamid.qr", "from": USER_BSUID, "type": "button",
                "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                "button": {"payload": "confirm:9:no", "text": "No"},
            }],
        )))

        assert events[0].callback_data == "confirm:9:no"

    def test_a_reply_context_carries_the_parent_message_id(self, tmp_path):
        config = _config(tmp_path)
        events = normalize_payload(config, _payload(_value(
            contacts=[_contact()],
            messages=[_text_message(context={"id": "wamid.parent",
                                             "from": "15551230000"})],
        )))

        assert events[0].reply_to_message_id == "wamid.parent"

    def test_the_event_time_is_parsed_as_utc(self, tmp_path):
        config = _config(tmp_path)
        events = normalize_payload(config, _payload(_value(
            contacts=[_contact()],
            messages=[dict(_text_message(), timestamp="1757620000")],
        )))

        assert events[0].sent_at == datetime.fromtimestamp(1757620000, tz=timezone.utc)

    def test_a_status_carries_pricing_verbatim_and_infers_nothing(self, tmp_path):
        """PyWa's own `Pricing.from_dict` infers `billable` from the type.

        The spec forbids that: absent pricing data stays NULL, because a guess
        is what opens or fails to open the billing circuit.
        """
        config = _config(tmp_path)
        events = normalize_payload(config, _payload(_value(statuses=[
            _status(message_id="wamid.priced", status="sent", pricing={
                "billable": True, "pricing_model": "PMP",
                "category": "service", "type": "regular",
            }),
            _status(message_id="wamid.typed", status="sent", pricing={
                "pricing_model": "PMP", "category": "service", "type": "regular",
            }),
            _status(message_id="wamid.bare", status="sent"),
        ])))

        assert [(e.billable, e.pricing_category, e.pricing_type) for e in events] == [
            (True, "service", "regular"),
            (None, "service", "regular"),
            (None, None, None),
        ]

    def test_a_status_error_keeps_only_the_numeric_code(self, tmp_path):
        config = _config(tmp_path)
        events = normalize_payload(config, _payload(_value(statuses=[
            _status(message_id="wamid.failed", status="failed", errors=[{
                "code": 131047,
                "title": "Re-engagement message",
                "message": "Message failed to send because more than 24 hours "
                           "have passed since the customer last replied",
                "error_data": {"details": "the recipient +15551234567 is stale"},
            }]),
        ])))

        assert events[0].status == "failed"
        assert events[0].error_code == "131047"
        # Meta's prose and its error_data can carry a phone number; neither is
        # a field on the record at all.
        assert not any(
            "24 hours" in str(value) or USER_NUMBER in str(value)
            for value in vars(events[0]).values()
        )

    def test_an_unmapped_status_is_dropped_rather_than_invented(self, tmp_path):
        config = _config(tmp_path)
        events = normalize_payload(config, _payload(_value(statuses=[
            _status(message_id="wamid.played", status="played"),
            _status(message_id="wamid.sent", status="sent"),
        ])))

        assert [e.message_id for e in events] == ["wamid.sent"]


def _status(*, message_id="wamid.out", status="sent", recipient_id=USER_WA_ID,
            timestamp=None, pricing=None, errors=None):
    payload = {
        "id": message_id,
        "status": status,
        "timestamp": str(int((timestamp or datetime.now(timezone.utc)).timestamp())),
        "recipient_id": recipient_id,
    }
    if pricing is not None:
        payload["pricing"] = pricing
    if errors is not None:
        payload["errors"] = errors
    return payload


# ---------------------------------------------------------------------------
# Identity resolution and enrollment
# ---------------------------------------------------------------------------


class TestIdentityAndEnrollment:
    def test_a_first_message_latches_the_bsuid_onto_the_bootstrap_number(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER)

        results = _handle(config, _text_payload())

        assert _dispositions(results) == ["task"]
        with db.get_db(config.db_path) as conn:
            binding = db.get_whatsapp_binding(conn, "alice")
        assert binding.bsuid == USER_BSUID
        assert binding.send_id == USER_BSUID
        assert binding.enrolled_at
        assert binding.bootstrap_phone_number == USER_NUMBER

    def test_a_later_message_matches_the_bsuid_and_not_the_number(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        results = _handle(config, _payload(_value(
            contacts=[_contact(wa_id=None, username="alice.w")],
            messages=[_text_message(message_id="wamid.later")],
        )))

        assert _dispositions(results) == ["task"]

    def test_a_username_only_sender_with_no_binding_creates_nothing(
        self, tmp_path, caplog,
    ):
        caplog.set_level("INFO")
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER)

        results = _handle(config, _payload(_value(
            contacts=[_contact(bsuid=OTHER_BSUID, wa_id=None, username="mallory")],
            messages=[_text_message(sender=OTHER_BSUID)],
        )))

        assert _dispositions(results) == ["unknown_sender"]
        assert _counts(config, "processed_whatsapp", "tasks") == [0, 0]
        assert OTHER_BSUID not in caplog.text
        assert "mallory" not in caplog.text
        assert "whatsapp.inbound.rejected" in caplog.text

    def test_an_unknown_bsuid_with_an_unknown_number_creates_nothing(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER)

        results = _handle(config, _payload(_value(
            contacts=[_contact(bsuid=OTHER_BSUID, wa_id="15559998888")],
            messages=[_text_message(sender=OTHER_BSUID)],
        )))

        assert _dispositions(results) == ["unknown_sender"]
        assert _counts(config, "processed_whatsapp", "tasks") == [0, 0]

    def test_a_message_with_no_bsuid_is_never_resolved_by_number_alone(self, tmp_path):
        """Rule 1: a phone number is never sufficient identity.

        The `wa_id` here matches a configured bootstrap number exactly, and
        without a BSUID it still creates nothing — because the BSUID is the
        thing that survives a recycled line.
        """
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER)

        results = _handle(config, _payload(_value(
            contacts=[{"wa_id": USER_WA_ID, "profile": {"name": "Alice"}}],
            messages=[_text_message(sender=USER_WA_ID)],
        )))

        assert _dispositions(results) == ["unknown_sender"]
        assert _counts(config, "tasks") == [0]

    def test_a_recycled_number_never_takes_over_the_principal(self, tmp_path):
        """Rule 5. The bootstrap number matches; the stored BSUID does not.

        Either the line was reassigned by the carrier or the identity changed
        under us. Both are reasons to stop, not to rebind: latching the new
        BSUID would hand a stranger the Istota user's tasks, memory and
        confirmations.
        """
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        results = _handle(config, _payload(_value(
            contacts=[_contact(bsuid=OTHER_BSUID, wa_id=USER_WA_ID)],
            messages=[_text_message(sender=OTHER_BSUID)],
        )))

        assert _dispositions(results) == ["identity_mismatch"]
        assert _counts(config, "processed_whatsapp", "tasks") == [0, 0]
        with db.get_db(config.db_path) as conn:
            assert db.get_whatsapp_binding(conn, "alice").bsuid == USER_BSUID

    def test_the_recycled_number_alert_is_written_once_and_never_over_whatsapp(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        payload = _payload(_value(
            contacts=[_contact(bsuid=OTHER_BSUID, wa_id=USER_WA_ID)],
            messages=[_text_message(sender=OTHER_BSUID)],
        ))

        first = _handle(config, payload)
        second = _handle(config, _payload(_value(
            contacts=[_contact(bsuid=OTHER_BSUID, wa_id=USER_WA_ID)],
            messages=[_text_message(message_id="wamid.again", sender=OTHER_BSUID)],
        )))

        assert first[0].pending_alert is not None
        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT source, dedup_key, title, body FROM notifications"
            ).fetchall()
        assert len(rows) == 1, "the second sighting must bump, never duplicate"
        assert second[0].disposition == "identity_mismatch"
        for row in rows:
            for field in ("dedup_key", "title", "body"):
                assert OTHER_BSUID not in row[field]
                assert USER_NUMBER not in row[field]
                assert USER_WA_ID not in row[field]

    def test_one_identity_on_two_bootstrap_numbers_enrolls_only_the_first(
        self, tmp_path,
    ):
        """Two operators bootstrapped the same person on two numbers.

        The BSUID lookup runs before the phone lookup, so the second number
        never enrolls anything: the same identity resolves to the user it is
        already bound to, and bob's row stays empty. That ordering is the whole
        of rule 1 — once a BSUID binding exists, a number cannot make a second.
        """
        config = _config(tmp_path)
        config.users["bob"] = UserConfig()
        _bind(config, "alice", bootstrap_phone_number=USER_NUMBER)
        _bind(config, "bob", bootstrap_phone_number="+15557654321")

        first = _handle(config, _text_payload())
        second = _handle(config, _payload(_value(
            contacts=[_contact(wa_id="15557654321")],
            messages=[_text_message(message_id="wamid.second")],
        )))

        assert _dispositions(first) == ["task"]
        assert _dispositions(second) == ["task"]
        with db.get_db(config.db_path) as conn:
            alice = db.get_whatsapp_binding(conn, "alice")
            bob = db.get_whatsapp_binding(conn, "bob")
        assert alice.bsuid == USER_BSUID
        assert bob.bsuid == ""
        assert second[0].user_id == "alice"

    def test_a_latch_the_unique_indexes_refuse_fails_closed(self, tmp_path):
        """The `send_id` index catches what the BSUID lookup cannot.

        Alice's row carries a stale `send_id` — the shape a reset or an
        operator edit leaves behind — that happens to equal the BSUID now
        arriving on bob's bootstrap number. The BSUID lookup misses, the phone
        lookup finds bob, and the latch collides on `idx_whatsapp_binding_send_id`.
        The partial unique indexes are the arbiter for exactly this, and the
        loser must fail closed rather than take a destination the database says
        belongs to somebody else.
        """
        config = _config(tmp_path)
        config.users["bob"] = UserConfig()
        _bind(config, "alice", bootstrap_phone_number=USER_NUMBER,
              bsuid=OTHER_BSUID, send_id=USER_BSUID)
        _bind(config, "bob", bootstrap_phone_number="+15557654321")

        results = _handle(config, _payload(_value(
            contacts=[_contact(wa_id="15557654321")],
            messages=[_text_message(message_id="wamid.collide")],
        )))

        assert _dispositions(results) == ["identity_conflict"]
        assert _counts(config, "processed_whatsapp", "tasks") == [0, 0]
        with db.get_db(config.db_path) as conn:
            assert db.get_whatsapp_binding(conn, "bob").bsuid == ""
            assert db.get_whatsapp_binding(conn, "alice").send_id == USER_BSUID

    def test_a_row_that_gained_an_identity_mid_transaction_is_not_overwritten(
        self, tmp_path,
    ):
        """The conditional latch, driven directly.

        `handle_whatsapp_batch` takes `BEGIN IMMEDIATE`, so two batches for one
        user serialize and the second sees the committed BSUID rather than
        racing it — which means the `WHERE bsuid = ''` arm is unreachable
        through the webhook and has to be driven here. It is the guard that
        makes the read-then-write safe if that ordering ever changes.
        """
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        with db.get_db(config.db_path) as conn:
            assert db.latch_whatsapp_bsuid(
                conn, "alice", bsuid=OTHER_BSUID, send_id=OTHER_BSUID,
            ) is False
            assert db.get_whatsapp_binding(conn, "alice").bsuid == USER_BSUID

    def test_the_send_id_and_username_follow_an_authenticated_message(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        _handle(config, _payload(_value(
            contacts=[_contact(username="alice.w")],
            messages=[_text_message()],
        )))

        with db.get_db(config.db_path) as conn:
            binding = db.get_whatsapp_binding(conn, "alice")
        assert binding.send_id == USER_BSUID
        assert binding.username == "alice.w"
        assert binding.last_seen_at

    def test_a_disabled_transport_processes_nothing(self, tmp_path):
        config = _config(tmp_path, enabled=False)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        results = _handle(config, _text_payload())

        assert _dispositions(results) == ["unconfigured"]
        assert _counts(config, "processed_whatsapp", "tasks") == [0, 0]


# ---------------------------------------------------------------------------
# The service window
# ---------------------------------------------------------------------------


class TestTheServiceWindowClock:
    def test_the_window_advances_to_the_authenticated_event_time(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        sent = datetime.now(timezone.utc) - timedelta(minutes=3)

        _handle(config, _text_payload(timestamp=sent))

        with db.get_db(config.db_path) as conn:
            stored = db.get_whatsapp_binding(conn, "alice").last_user_message_at
        assert stored == sent.strftime("%Y-%m-%d %H:%M:%S")

    def test_a_replayed_older_event_never_moves_the_window_backwards(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        recent = datetime.now(timezone.utc) - timedelta(minutes=1)
        old = datetime.now(timezone.utc) - timedelta(hours=6)

        _handle(config, _text_payload(message_id="wamid.recent", timestamp=recent))
        _handle(config, _text_payload(message_id="wamid.old", timestamp=old))

        with db.get_db(config.db_path) as conn:
            stored = db.get_whatsapp_binding(conn, "alice").last_user_message_at
        assert stored == recent.strftime("%Y-%m-%d %H:%M:%S")

    def test_a_far_future_timestamp_cannot_extend_the_window(self, tmp_path):
        """Meta's timestamp is authenticated, not trustworthy as a clock.

        A value hours ahead would hold a free-form send authorised long after
        the user stopped writing, so anything past a five-minute margin falls
        back to the receiving instant.
        """
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        future = datetime.now(timezone.utc) + timedelta(hours=9)

        _handle(config, _text_payload(timestamp=future))

        with db.get_db(config.db_path) as conn:
            stored = db.get_whatsapp_binding(conn, "alice").last_user_message_at
        assert stored < (
            datetime.now(timezone.utc) + timedelta(minutes=6)
        ).strftime("%Y-%m-%d %H:%M:%S")

    def test_a_slightly_future_timestamp_inside_the_margin_is_taken_as_given(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        skewed = datetime.now(timezone.utc) + timedelta(minutes=2)

        _handle(config, _text_payload(timestamp=skewed))

        with db.get_db(config.db_path) as conn:
            stored = db.get_whatsapp_binding(conn, "alice").last_user_message_at
        assert stored == skewed.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Dispositions: dedup, opt-out, unsupported types, commands, confirmations
# ---------------------------------------------------------------------------


class TestInboundDispositions:
    def test_an_ordinary_message_creates_one_task_and_no_room_rows(self, tmp_path):
        """The whole non-room claim, held on rows rather than on a flag."""
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        results = _handle(config, _text_payload())

        assert _dispositions(results) == ["task"]
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, results[0].task_id)
            assert task.source_type == "whatsapp"
            assert task.conversation_token == whatsapp_conversation_token("alice")
            assert task.output_target == "whatsapp"
            assert task.prompt == "check the backup"
            for table in ("rooms", "room_bindings", "room_members", "messages"):
                assert conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0] == 0, f"{table} must stay empty for WhatsApp"
            row = conn.execute("SELECT * FROM processed_whatsapp").fetchone()
        assert row["disposition"] == "task"
        assert row["task_id"] == results[0].task_id
        assert row["message_type"] == "text"

    def test_the_dedup_row_holds_no_body_identifier_or_callback_data(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        _handle(config, _text_payload(text="the private thing"))

        with db.get_db(config.db_path) as conn:
            row = conn.execute("SELECT * FROM processed_whatsapp").fetchone()
        stored = " ".join(str(value) for value in tuple(row))
        for secret in ("the private thing", USER_BSUID, USER_NUMBER, USER_WA_ID):
            assert secret not in stored

    def test_a_duplicate_message_id_creates_no_second_task(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        first = _handle(config, _text_payload())
        second = _handle(config, _text_payload())

        assert _dispositions(first) == ["task"]
        assert _dispositions(second) == ["duplicate"]
        assert _counts(config, "processed_whatsapp", "tasks") == [1, 1]

    def test_a_batch_holding_one_duplicate_and_one_new_message_processes_one(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        _handle(config, _text_payload(message_id="wamid.seen"))

        results = _handle(config, _payload(_value(
            contacts=[_contact()],
            messages=[
                _text_message(message_id="wamid.seen", text="seen"),
                _text_message(message_id="wamid.fresh", text="fresh"),
            ],
        )))

        assert _dispositions(results) == ["duplicate", "task"]
        assert _counts(config, "processed_whatsapp", "tasks") == [2, 2]

    def test_an_empty_body_records_empty_and_creates_no_task(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        results = _handle(config, _text_payload(text="   \n  "))

        assert _dispositions(results) == ["empty"]
        assert _counts(config, "tasks") == [0]
        with db.get_db(config.db_path) as conn:
            assert conn.execute(
                "SELECT disposition FROM processed_whatsapp"
            ).fetchone()[0] == "empty"

    @pytest.mark.parametrize("keyword", ["STOP", "stop", "  Stop  "])
    def test_stop_opts_the_binding_out(self, tmp_path, keyword):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        results = _handle(config, _text_payload(text=keyword))

        assert _dispositions(results) == ["stop"]
        assert results[0].response_text
        with db.get_db(config.db_path) as conn:
            assert db.get_whatsapp_binding(conn, "alice").opted_out_at
        assert _counts(config, "tasks") == [0]

    def test_start_clears_the_opt_out_and_help_answers_without_one(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        _handle(config, _text_payload(message_id="wamid.stop", text="STOP"))
        started = _handle(config, _text_payload(message_id="wamid.start", text="START"))
        helped = _handle(config, _text_payload(message_id="wamid.help", text="HELP"))

        assert _dispositions(started) == ["start"]
        assert _dispositions(helped) == ["help"]
        with db.get_db(config.db_path) as conn:
            assert db.get_whatsapp_binding(conn, "alice").opted_out_at is None
        assert helped[0].response_text
        assert _counts(config, "tasks") == [0]

    def test_a_stopped_binding_still_records_but_creates_no_task(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        _handle(config, _text_payload(message_id="wamid.stop", text="STOP"))

        results = _handle(config, _text_payload(message_id="wamid.after",
                                                text="are you there"))

        assert _dispositions(results) == ["opted_out"]
        assert _counts(config, "tasks") == [0]

    def test_an_opted_out_binding_still_answers_a_question_it_was_asked(self, tmp_path):
        """One rule for both answer routes, and the reason it is not the gate.

        STOP means no outbound WhatsApp. It does not mean a question already in
        front of the user stops being answerable — the answer is a state change
        they deliberately made, and only the acknowledgement is a send, which
        the ledger refuses on its own. Below the gate the bare `YES` would be
        refused while the Yes *button* carrying the same answer was accepted.
        """
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        token = whatsapp_conversation_token("alice")
        with db.get_db(config.db_path) as conn:
            held = db.create_task(
                conn, prompt="delete it", user_id="alice", source_type="whatsapp",
                conversation_token=token, output_target="whatsapp",
            )
            db.set_task_confirmation(conn, held, "May I delete it?")
        _handle(config, _text_payload(message_id="wamid.stop", text="STOP"))

        typed = _handle(config, _text_payload(message_id="wamid.yes", text="YES"))

        assert _dispositions(typed) == ["confirmation_answer"]
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, held).status == "pending"

    def test_an_opted_out_binding_answers_a_button_the_same_way(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        token = whatsapp_conversation_token("alice")
        with db.get_db(config.db_path) as conn:
            held = db.create_task(
                conn, prompt="delete it", user_id="alice", source_type="whatsapp",
                conversation_token=token, output_target="whatsapp",
            )
            db.set_task_confirmation(conn, held, "May I delete it?")
        _handle(config, _text_payload(message_id="wamid.stop", text="STOP"))

        tapped = _handle(config, _payload(_value(
            contacts=[_contact()],
            messages=[_button_message(payload=f"confirm:{held}:no")],
        )))

        assert _dispositions(tapped) == ["confirmation_answer"]
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, held).status == "cancelled"

    def test_an_opted_out_binding_runs_no_command(self, tmp_path):
        """A command's whole value is its response, and there is no route for one."""
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        _handle(config, _text_payload(message_id="wamid.stop", text="STOP"))

        results = _handle(config, _text_payload(message_id="wamid.cmd", text="!help"))

        assert _dispositions(results) == ["opted_out"]
        assert results[0].command_text is None

    def test_the_keywords_are_exact_and_a_sentence_is_an_ordinary_request(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        results = _handle(config, _text_payload(text="stop the nightly backup job"))

        assert _dispositions(results) == ["task"]
        with db.get_db(config.db_path) as conn:
            assert db.get_whatsapp_binding(conn, "alice").opted_out_at is None

    @pytest.mark.parametrize(
        "message_type, body",
        [
            ("image", {"image": {"id": "media-1", "caption": "do this"}}),
            ("document", {"document": {"id": "media-2", "caption": "and this"}}),
            ("audio", {"audio": {"id": "media-3"}}),
            ("location", {"location": {"latitude": 1.0, "longitude": 2.0}}),
            ("order", {"order": {"catalog_id": "c1"}}),
            ("unknown", {}),
        ],
    )
    def test_an_unsupported_type_gets_one_fixed_reply_and_no_task(
        self, tmp_path, message_type, body,
    ):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        results = _handle(config, _payload(_value(
            contacts=[_contact()],
            messages=[{
                "id": f"wamid.{message_type}", "from": USER_BSUID,
                "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                "type": message_type, **body,
            }],
        )))

        assert _dispositions(results) == ["unsupported_type"]
        assert results[0].response_text == (
            "That WhatsApp message type is not supported yet. "
            "Please resend the request as text."
        )
        assert results[0].response_logical_key == f"unsupported:wamid.{message_type}"
        assert _counts(config, "tasks") == [0]

    def test_a_caption_is_never_processed_as_a_request(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        results = _handle(config, _payload(_value(
            contacts=[_contact()],
            messages=[{
                "id": "wamid.img", "from": USER_BSUID,
                "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                "type": "image",
                "image": {"id": "media-1", "caption": "delete every backup"},
            }],
        )))

        assert results[0].disposition == "unsupported_type"
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0

    @pytest.mark.parametrize(
        "message, disposition",
        [
            ({"type": "reaction", "reaction": {"message_id": "wamid.1", "emoji": "x"}},
             "reaction"),
            ({"type": "text", "text": {"body": "fixed"},
              "edit": {"original_message_id": "wamid.1"}}, "edited"),
            ({"type": "text", "revoke": {"original_message_id": "wamid.1"}}, "revoked"),
        ],
    )
    def test_reactions_edits_and_deletions_get_no_reply(
        self, tmp_path, message, disposition,
    ):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        results = _handle(config, _payload(_value(
            contacts=[_contact()],
            messages=[{
                "id": "wamid.meta", "from": USER_BSUID,
                "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                **message,
            }],
        )))

        assert _dispositions(results) == [disposition]
        assert results[0].response_text is None
        assert _counts(config, "tasks") == [0]

    def test_a_group_message_is_rejected_before_identity_lookup(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        results = _handle(config, _payload(_value(
            contacts=[_contact()],
            messages=[_text_message(group_id="120363000000000000")],
        )))

        assert _dispositions(results) == ["group"]
        assert _counts(config, "processed_whatsapp", "tasks") == [0, 0]

    def test_a_command_dispatches_on_the_whatsapp_surface(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)

        results = _handle(config, _text_payload(text="!help"))

        assert _dispositions(results) == ["command"]
        assert results[0].command_text == "!help"
        assert results[0].response_logical_key == "command:wamid.001"
        assert _counts(config, "tasks") == [0]

    def test_a_command_response_is_produced_off_the_whatsapp_surface_name(
        self, tmp_path,
    ):
        import asyncio

        from istota.transport.whatsapp.webhook import resolve_event_response

        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        results = _handle(config, _text_payload(text="!help"))

        text = asyncio.run(resolve_event_response(config, results[0]))

        assert text and "Available commands" in text

    def test_a_button_callback_answers_this_conversations_confirmation(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        token = whatsapp_conversation_token("alice")
        with db.get_db(config.db_path) as conn:
            held = db.create_task(
                conn, prompt="delete it", user_id="alice", source_type="whatsapp",
                conversation_token=token, output_target="whatsapp",
            )
            db.set_task_confirmation(conn, held, "May I delete it?")

        results = _handle(config, _payload(_value(
            contacts=[_contact()],
            messages=[_button_message(payload=f"confirm:{held}:yes")],
        )))

        assert _dispositions(results) == ["confirmation_answer"]
        assert results[0].response_text
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, held).status == "pending"
            assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0

    def test_a_button_callback_naming_another_users_confirmation_is_refused(
        self, tmp_path,
    ):
        """Ownership is checked, not assumed from the signed sender.

        The callback data is a value the payload carries. It says which
        question, never whose.
        """
        config = _config(tmp_path)
        config.users["bob"] = UserConfig()
        _bind(config, "alice", bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        _bind(config, "bob", bootstrap_phone_number="+15557654321",
              bsuid=OTHER_BSUID)
        with db.get_db(config.db_path) as conn:
            theirs = db.create_task(
                conn, prompt="delete bob's backup", user_id="bob",
                source_type="whatsapp",
                conversation_token=whatsapp_conversation_token("bob"),
                output_target="whatsapp",
            )
            db.set_task_confirmation(conn, theirs, "May I delete it?")

        results = _handle(config, _payload(_value(
            contacts=[_contact()],
            messages=[_button_message(payload=f"confirm:{theirs}:yes")],
        )))

        assert _dispositions(results) == ["callback_unmatched"]
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, theirs).status == "pending_confirmation"

    def test_a_bare_yes_answers_only_this_conversations_question(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        token = whatsapp_conversation_token("alice")
        with db.get_db(config.db_path) as conn:
            held = db.create_task(
                conn, prompt="delete it", user_id="alice", source_type="whatsapp",
                conversation_token=token, output_target="whatsapp",
            )
            db.set_task_confirmation(conn, held, "May I delete it?")

        results = _handle(config, _text_payload(text="YES"))

        assert _dispositions(results) == ["confirmation_answer"]
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, held).status == "pending"

    def test_a_bare_yes_with_no_question_here_is_an_ordinary_request(self, tmp_path):
        """`confirmations.resolve` Path C would answer another surface's question.

        A WhatsApp identity is exactly as strong as the binding, and an email
        confirmation gate answered from here would write a permanent trust row
        for somebody else's correspondent. Only a question parked in *this*
        conversation is answerable.
        """
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        with db.get_db(config.db_path) as conn:
            elsewhere = db.create_task(
                conn, prompt="approve the mail", user_id="alice",
                source_type="email", conversation_token="thread-hash",
                output_target="email",
            )
            db.set_task_confirmation(conn, elsewhere, "Trust this sender?")

        results = _handle(config, _text_payload(text="yes"))

        assert _dispositions(results) == ["task"]
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, elsewhere).status == "pending_confirmation"

    def test_a_new_request_cancels_the_pending_confirmation_first(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        token = whatsapp_conversation_token("alice")
        with db.get_db(config.db_path) as conn:
            held = db.create_task(
                conn, prompt="delete it", user_id="alice", source_type="whatsapp",
                conversation_token=token, output_target="whatsapp",
            )
            db.set_task_confirmation(conn, held, "May I delete it?")

        results = _handle(config, _text_payload(text="never mind, list the backups"))

        assert _dispositions(results) == ["task"]
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, held).status == "cancelled"


# ---------------------------------------------------------------------------
# Isolation guards
# ---------------------------------------------------------------------------


class TestTheSurfaceStaysOutsideTheRoomModel:
    def test_the_conversation_token_carries_no_meta_identifier(self):
        token = whatsapp_conversation_token("alice")

        assert token.startswith("whatsapp-")
        assert token == whatsapp_conversation_token("alice")
        assert token != whatsapp_conversation_token("bob")
        for identifier in ("alice", USER_NUMBER, USER_WA_ID, USER_BSUID, WABA_ID):
            assert identifier not in token

    def test_the_token_survives_a_number_username_and_send_id_change(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        before = _handle(config, _text_payload())

        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number="+15550001111",
                bsuid=USER_BSUID, send_id="new-send-id", username="renamed",
            )
        after = _handle(config, _text_payload(message_id="wamid.after"))

        with db.get_db(config.db_path) as conn:
            assert (
                db.get_task(conn, before[0].task_id).conversation_token
                == db.get_task(conn, after[0].task_id).conversation_token
            )

    def test_the_webhook_module_never_names_a_room_writer(self):
        from tests.support.drift import source_of

        from istota.transport.whatsapp import webhook as webhook_module

        source = source_of(webhook_module)
        for forbidden in (
            "register_room", "add_room_binding", "add_room_member",
            "store_message", "mirror_to_room=True",
        ):
            assert forbidden not in source

    def test_ingest_is_told_not_to_mirror(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, bootstrap_phone_number=USER_NUMBER, bsuid=USER_BSUID)
        seen: list = []

        from istota.transport.whatsapp import webhook as webhook_module

        real = webhook_module.ingest_message

        def spy(conn, cfg, msg):
            seen.append(msg)
            return real(conn, cfg, msg)

        original = webhook_module.ingest_message
        webhook_module.ingest_message = spy
        try:
            _handle(config, _text_payload())
        finally:
            webhook_module.ingest_message = original

        assert len(seen) == 1
        assert seen[0].mirror_to_room is False
        assert seen[0].surface == "whatsapp"
        assert seen[0].source_type == "whatsapp"
        assert seen[0].queue == "foreground"
        assert seen[0].is_group_chat is False
