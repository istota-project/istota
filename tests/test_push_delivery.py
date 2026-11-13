"""The delivery seam: one logical alert, three content modes, one sender.

`golden/push/legacy_wire.json` is the wire output of a full legacy sweep, taken
before the seam existed. Regenerate it only for an intended change to legacy
delivery: `uv run env ISTOTA_UPDATE_GOLDEN=1 pytest tests/test_push_delivery.py -n0`.
"""

import json
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from istota import db
from istota.config import Config, PushConfig
from istota.push import apns, delivery, devices, dispatch, envelope
from tests.test_prompt_golden import updating

SNAPSHOT = Path(__file__).parent / "golden" / "push" / "legacy_wire.json"
UPDATE_CMD = "uv run env ISTOTA_UPDATE_GOLDEN=1 pytest tests/test_push_delivery.py -n0"
NOW = 2_000_000_000.0
BINDING = "0f8e4b0c-2a6d-4c4e-9a43-6d2f1b8e7c51"
_WIRE_HEADERS = ("apns-topic", "apns-push-type", "apns-priority", "apns-expiration", "apns-collapse-id")


def _stamp(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _message(conn, room, when, body):
    db.add_message(conn, room.token, role="assistant", body=body, origin_surface="web")
    message_id = db.room_max_message_id(conn, room.token)
    conn.execute("UPDATE messages SET created_at=? WHERE id=?", (_stamp(when), message_id))
    return message_id


def _notification(conn, user, key, *, title, body, link):
    from istota.notifications import store
    result = store.write_notification(conn, user, source="confirmation", dedup_key=key,
                                     title=title, body=body, link=link)
    conn.execute("UPDATE notifications SET updated_at=? WHERE id=?", (_stamp(NOW), result.notification_id))


@pytest.fixture
def wire(tmp_path, monkeypatch):
    """A push deployment whose APNs is an in-memory transport recording each request."""
    key = tmp_path / "apns.p8"
    key.write_text("unused: the provider token is stubbed")
    push = PushConfig(enabled=True, apns_key_path=str(key), apns_key_id="KEYEXAMPLE",
                      apns_team_id="TEAMEXAMPL", apns_topic="com.example.app")
    config = Config(db_path=tmp_path / "test.db", push=push)
    db.init_db(config.db_path)
    monkeypatch.setattr(dispatch.time, "time", lambda: NOW)
    monkeypatch.setattr(dispatch, "_warned_unavailable", False)
    monkeypatch.setattr(apns, "provider_token", lambda config, force_refresh=False: "jwt")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(apns, "_client", lambda: client)
    yield config, requests
    client.close()


def _recorded(requests, replacements):
    def scrub(text):
        for value, placeholder in replacements.items():
            text = text.replace(value, placeholder)
        return text

    return [{
        "path": request.url.path,
        "headers": {name: scrub(request.headers[name]) for name in _WIRE_HEADERS if name in request.headers},
        "body": scrub(request.content.decode("utf-8")),
    } for request in requests]


def _legacy_sweeps(config, requests):
    """Two users, a bound and an unbound device, previews on and off, then a badge repair."""
    with db.get_db(config.db_path) as conn:
        devices.register_device(conn, "alice", "ab" * 32, "sandbox", auth_method="nextcloud", auth_epoch=0)
        devices.register_device(conn, "alice", "ef" * 32, "production", "ios", "0.12.0",
                                auth_method="nextcloud", auth_epoch=0,
                                connection_binding=BINDING, connection_generation=7)
        devices.register_device(conn, "bob", "cd" * 32, "sandbox", auth_method="nextcloud", auth_epoch=0)
        conn.execute("INSERT INTO user_profiles (user_id, push_preview) VALUES ('bob', 0)")
        work = db.create_web_chat_room(conn, "alice", "Work")
        cafe = db.create_web_chat_room(conn, "alice", "Café ☕ " + "ü" * 120)
        shared = db.create_web_chat_room(conn, "bob", "Shared")
        db.add_web_room_member(conn, shared.token, "alice")
        _notification(conn, "alice", "task:1", title="Approval needed", body="Review the held task", link="/chat")
        _notification(conn, "alice", "task:2", title="🚀" * 100 + "\x00‮", body="z\n\t" * 3000,
                      link="https://example.com/elsewhere")
        _notification(conn, "bob", "task:3", title="Secret subject", body="Secret text", link="/chat")
        _message(conn, work, NOW - 30, "First")
        _message(conn, work, NOW - 29, "## Done\n\n**Deployed** the [fix](https://example.com/run_42).")
        last = _message(conn, cafe, NOW - 28, "Grüße, " + "é" * 400)
        _message(conn, shared, NOW - 27, "Shared reply")
    dispatch.run_push_dispatch(config)
    with db.get_db(config.db_path) as conn:
        db.set_room_read_state(conn, cafe.token, "web", last, "alice")
    dispatch.run_push_dispatch(config)
    return {work.token: "<work>", cafe.token: "<cafe>", shared.token: "<shared>"}


def test_legacy_wire_output_matches_the_snapshot(wire):
    config, requests = wire
    replacements = _legacy_sweeps(config, requests)
    recorded = _recorded(requests, replacements)
    if updating(command=UPDATE_CMD):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(recorded, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    assert recorded == json.loads(SNAPSHOT.read_text(encoding="utf-8"))


# --- The seam: one logical alert, three content modes ---------------------------

GENERATION = "AAECAwQFBgcICQoLDA0ODw"
ROOM_NAME = "Quarterly planning"
ROOM_TOKEN = "rm_testroomtoken01"
BODY = "The draft budget is attached for review"
ACCOUNT = "alice"
ROOM_ID = 4242


@pytest.fixture
def private(tmp_path):
    """A private deployment: a signing key on disk and one device key."""
    key_path = tmp_path / "push" / "signing.pem"
    signing_key = envelope.write_signing_key(key_path)
    push = PushConfig(enabled=True, apns_key_path=str(tmp_path / "apns.p8"), apns_key_id="KEYEXAMPLE",
                      apns_team_id="TEAMEXAMPL", apns_topic="com.example.app",
                      content="private", signing_key_path=str(key_path))
    config = Config(db_path=tmp_path / "test.db", push=push, bot_name="Istota\u0000 Test")
    db.init_db(config.db_path)
    return config, signing_key, X25519PrivateKey.generate()


def sender_id(signing_key):
    return envelope.key_id(signing_key.public_key().public_bytes_raw())


def encrypted_device(signing_key, device_key, **row):
    raw = device_key.public_key().public_bytes_raw()
    return {"id": 1, "user_id": ACCOUNT, "token": "ab" * 32, "environment": "sandbox",
            "created_at": "2033-01-01T00:00:00.000Z", "connection_binding": BINDING,
            "connection_generation": 3, "private_version": 1, "private_content": "encrypted",
            "encryption_public_key": envelope.b64url(raw), "encryption_key_id": envelope.key_id(raw),
            "sender_key_id": sender_id(signing_key), "push_generation": GENERATION, **row}


def plain_device(**row):
    return {"id": 2, "user_id": ACCOUNT, "token": "cd" * 32, "environment": "sandbox",
            "created_at": "2033-01-01T00:00:00.000Z", "connection_binding": None,
            "connection_generation": None, "private_content": None, "encryption_public_key": None,
            "sender_key_id": None, "push_generation": None, **row}


def chat_alert(**overrides):
    fields = {"title": ROOM_NAME, "body": BODY, "badge": 3, "thread": "room:" + ROOM_TOKEN,
              "group": "room:" + ROOM_TOKEN,
              "target": {"kind": "chat", "room_token": ROOM_TOKEN, "room_id": ROOM_ID}}
    fields.update(overrides)
    return delivery.alert(**fields)


def plaintext_free(payload: bytes, collapse_id, *extra):
    text = payload.decode("utf-8") + (collapse_id or "")
    for secret in (ROOM_NAME, ROOM_TOKEN, BODY, "draft budget", str(ROOM_ID), ACCOUNT, "room:", *extra):
        assert secret not in text


def test_legacy_build_is_the_old_helpers_serialized_as_apns_sent_them():
    logical = chat_alert()
    config = Config(push=PushConfig(enabled=True))
    bound = plain_device(connection_binding=BINDING, connection_generation=3)
    for device in (plain_device(), bound):
        prepared = delivery.build_delivery(config, device, logical, now=NOW)
        expected = delivery.legacy_payload(logical, device)
        assert prepared.payload == json.dumps(expected, ensure_ascii=False, separators=(",", ":"),
                                              allow_nan=False).encode("utf-8")
        assert (prepared.collapse_id, prepared.priority, prepared.kind, prepared.badge, prepared.event_id) == (
            "room:" + ROOM_TOKEN, 10, "alert", 3, None)
    routed = json.loads(delivery.build_delivery(config, bound, logical, now=NOW).payload)["istota"]
    assert routed["connection_binding"] == BINDING and routed["connection_generation"] == 3


def test_effective_content_follows_the_enrollment_rule(private):
    config, signing_key, device_key = private
    sender = sender_id(signing_key)
    assert delivery.effective_content(config, encrypted_device(signing_key, device_key), sender) == "encrypted"
    assert delivery.effective_content(config, plain_device(), sender) == "generic"
    stale = encrypted_device(signing_key, device_key, sender_key_id="A" * 43)
    assert delivery.effective_content(config, stale, sender) == "generic"
    legacy = Config(push=PushConfig(enabled=True))
    assert delivery.effective_content(legacy, encrypted_device(signing_key, device_key), sender) == "legacy"


def test_generic_build_is_the_fixed_alert_and_nothing_else(private):
    config, signing_key, _ = private
    prepared = delivery.build_delivery(config, plain_device(), chat_alert(), now=NOW, signing_key=signing_key)
    assert json.loads(prepared.payload) == {
        "aps": {"alert": {"title": "Istota Test", "body": "You have new activity."}, "sound": "default", "badge": 3}}
    assert (prepared.collapse_id, prepared.priority, prepared.event_id) == (None, 10, None)
    plaintext_free(prepared.payload, prepared.collapse_id)


def test_stale_sender_key_gets_generic_content_and_the_row_is_untouched(private):
    config, signing_key, device_key = private
    device = encrypted_device(signing_key, device_key, sender_key_id="A" * 43)
    before = dict(device)
    prepared = delivery.build_delivery(config, device, chat_alert(), now=NOW, signing_key=signing_key)
    assert set(json.loads(prepared.payload)) == {"aps"}
    plaintext_free(prepared.payload, prepared.collapse_id)
    assert device == before


def test_encrypted_build_opens_to_the_routed_alert(private):
    config, signing_key, device_key = private
    device = encrypted_device(signing_key, device_key)
    prepared = delivery.build_delivery(config, device, chat_alert(), now=NOW, signing_key=signing_key)
    outer = json.loads(prepared.payload)
    assert outer["aps"] == {"alert": {"title": "Istota Test", "body": "You have new activity."},
                            "mutable-content": 1, "sound": "default", "badge": 3}
    opened = envelope.open_for_tests(prepared.payload, device_private_key=device_key,
                                     sender_public_key=signing_key.public_key(), now=NOW,
                                     binding=BINDING, generation=GENERATION)
    assert (opened.plaintext["title"], opened.plaintext["body"]) == (ROOM_NAME, BODY)
    assert opened.plaintext["target"] == {"kind": "chat", "room_token": ROOM_TOKEN, "room_id": ROOM_ID,
                                          "connection_binding": BINDING, "connection_generation": 3}
    assert (opened.header["iat"], opened.header["exp"]) == (int(NOW), int(NOW) + 300)
    assert prepared.collapse_id == opened.header["collapse"] == envelope.collapse_id(
        signing_key, BINDING, "room:" + ROOM_TOKEN)
    assert prepared.event_id == opened.header["mid"]
    plaintext_free(prepared.payload, prepared.collapse_id)


def test_each_build_gets_its_own_event_id(private):
    config, signing_key, device_key = private
    device = encrypted_device(signing_key, device_key)
    ids = {delivery.build_delivery(config, device, chat_alert(), now=NOW, signing_key=signing_key).event_id
           for _ in range(3)}
    assert len(ids) == 3


def test_encrypted_text_is_sanitized_and_capped_before_sealing(private):
    config, signing_key, device_key = private
    logical = chat_alert(title="Ü‮" * 200, body="ß\n" * 400 + "€" * 180)
    prepared = delivery.build_delivery(config, encrypted_device(signing_key, device_key), logical,
                                       now=NOW, signing_key=signing_key)
    assert len(prepared.payload) <= 4096
    opened = envelope.open_for_tests(prepared.payload, device_private_key=device_key,
                                     sender_public_key=signing_key.public_key(), now=NOW)
    assert opened.plaintext["title"] == "Ü" * 80
    assert opened.plaintext["body"] == delivery._sanitize(logical.body, 180)
    assert len(opened.plaintext["body"]) == 180


def test_preview_off_text_reaches_the_envelope_with_its_target(private):
    config, signing_key, device_key = private
    row = {"room_token": ROOM_TOKEN, "room_id": 5, "room_name": ROOM_NAME, "body": BODY,
           "created_at": "2033-05-18T03:33:20Z"}
    logical = next(dispatch._chat_pushes([row], 4, False, 0))
    prepared = delivery.build_delivery(config, encrypted_device(signing_key, device_key), logical,
                                       now=NOW, signing_key=signing_key)
    opened = envelope.open_for_tests(prepared.payload, device_private_key=device_key,
                                     sender_public_key=signing_key.public_key(), now=NOW)
    assert (opened.plaintext["title"], opened.plaintext["body"]) == ("New message in " + ROOM_NAME, "")
    assert opened.plaintext["target"]["room_token"] == ROOM_TOKEN


def test_a_missing_target_becomes_the_inbox_target(private):
    config, signing_key, device_key = private
    prepared = delivery.build_delivery(config, encrypted_device(signing_key, device_key),
                                       chat_alert(target=None), now=NOW, signing_key=signing_key)
    opened = envelope.open_for_tests(prepared.payload, device_private_key=device_key,
                                     sender_public_key=signing_key.public_key(), now=NOW)
    assert opened.plaintext["target"] == {"kind": "inbox"}


@pytest.mark.parametrize("mode", ["legacy", "generic", "encrypted"])
def test_badge_only_is_the_same_in_every_mode(private, mode):
    config, signing_key, device_key = private
    if mode == "legacy":
        config = Config(push=PushConfig(enabled=True))
    device = encrypted_device(signing_key, device_key) if mode == "encrypted" else plain_device()
    prepared = delivery.build_delivery(config, device, delivery.badge_only(7), now=NOW, signing_key=signing_key)
    assert (prepared.payload, prepared.collapse_id, prepared.priority, prepared.kind, prepared.badge) == (
        b'{"aps":{"badge":7}}', "badge", 5, "badge", 7)


def test_a_bad_stored_key_is_a_private_payload_refusal(private):
    config, signing_key, device_key = private
    device = encrypted_device(signing_key, device_key, encryption_public_key="not-a-key")
    with pytest.raises(delivery.DeliveryError) as refused:
        delivery.build_delivery(config, device, chat_alert(), now=NOW, signing_key=signing_key)
    assert refused.value.reason == "private_payload"


def test_an_envelope_over_the_apns_bound_is_payload_too_large(private, monkeypatch):
    config, signing_key, device_key = private

    def oversize(**kwargs):
        raise envelope.PayloadTooLarge(5000)

    monkeypatch.setattr(delivery.envelope, "seal", oversize)
    with pytest.raises(delivery.DeliveryError) as refused:
        delivery.build_delivery(config, encrypted_device(signing_key, device_key), chat_alert(),
                                now=NOW, signing_key=signing_key)
    assert refused.value.reason == "payload_too_large"


def test_without_its_key_a_private_deployment_sends_every_device_the_generic_alert(private):
    config, signing_key, device_key = private
    generic = {"aps": {"alert": {"title": "Istota Test", "body": "You have new activity."},
                       "sound": "default", "badge": 3}}
    for device in (encrypted_device(signing_key, device_key), plain_device()):
        prepared = delivery.build_delivery(config, device, chat_alert(), now=NOW, signing_key=None)
        assert json.loads(prepared.payload) == generic
        assert prepared.collapse_id is None
        plaintext_free(prepared.payload, prepared.collapse_id)


def test_send_prepared_hands_apns_the_measured_bytes(private, monkeypatch):
    config, signing_key, device_key = private
    calls = []

    def send(push_config, device, payload, **kwargs):
        calls.append((payload, kwargs))
        return apns.ApnsResult(status=200)

    monkeypatch.setattr(apns, "send", send)
    device = encrypted_device(signing_key, device_key)
    prepared = delivery.build_delivery(config, device, chat_alert(), now=NOW, signing_key=signing_key)
    delivery.send_prepared(config, device, prepared)
    assert calls == [(prepared.payload, {"collapse_id": prepared.collapse_id, "priority": 10, "expiration": 0})]


# --- The dispatcher on a private deployment ---------------------------------------


def _enroll_encrypted(conn, signing_key, device_key, token):
    raw = device_key.public_key().public_bytes_raw()
    return devices.register_device(
        conn, ACCOUNT, token, "sandbox", "ios", "0.15.0", auth_method="nextcloud", auth_epoch=0,
        connection_binding=BINDING, connection_generation=3, private_version=1, private_content="encrypted",
        encryption_public_key=envelope.b64url(raw), encryption_key_id=envelope.key_id(raw),
        sender_key_id=sender_id(signing_key), push_generation=GENERATION)


@pytest.fixture
def private_sweep(private, monkeypatch):
    config, signing_key, device_key = private
    Path(config.push.apns_key_path).write_text("unused: the provider token is stubbed")
    monkeypatch.setattr(dispatch.time, "time", lambda: NOW)
    monkeypatch.setattr(dispatch, "_warned_unavailable", False)
    monkeypatch.setattr(dispatch, "_warned_signing_key", False, raising=False)
    monkeypatch.setattr(apns, "provider_token", lambda config, force_refresh=False: "jwt")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(apns, "_client", lambda: client)
    with db.get_db(config.db_path) as conn:
        legacy = devices.register_device(conn, ACCOUNT, "cd" * 32, "sandbox", auth_method="nextcloud",
                                         auth_epoch=0)
        generic = devices.register_device(conn, ACCOUNT, "ef" * 32, "sandbox", "ios", "0.15.0",
                                          auth_method="nextcloud", auth_epoch=0, connection_binding=BINDING,
                                          connection_generation=2, private_version=1,
                                          private_content="generic", push_generation=GENERATION)
        encrypted = _enroll_encrypted(conn, signing_key, device_key, "ab" * 32)
        room = db.create_web_chat_room(conn, ACCOUNT, ROOM_NAME)
        _notification(conn, ACCOUNT, "task:1", title="Approval needed", body=BODY, link="/chat")
        _message(conn, room, NOW - 30, BODY)
    yield config, signing_key, device_key, requests, {"legacy": legacy, "generic": generic,
                                                      "encrypted": encrypted}, room
    client.close()


def test_private_sweep_sends_each_device_a_plaintext_free_payload(private_sweep):
    config, signing_key, device_key, requests, ids, room = private_sweep
    dispatch.run_push_dispatch(config)
    by_token = {}
    for request in requests:
        by_token.setdefault(request.url.path.rsplit("/", 1)[1], []).append(request)
    assert {token: len(sent) for token, sent in by_token.items()} == {"cd" * 32: 2, "ef" * 32: 2, "ab" * 32: 2}
    for request in requests:
        plaintext_free(request.content, request.headers.get("apns-collapse-id"), room.token,
                       "Approval needed", "notification_id")
    opened = [envelope.open_for_tests(request.content, device_private_key=device_key,
                                      sender_public_key=signing_key.public_key(), now=NOW)
              for request in by_token["ab" * 32]]
    assert [o.plaintext["target"]["kind"] for o in opened] == ["notification", "chat"]
    assert opened[1].plaintext["target"]["room_token"] == room.token
    assert all(json.loads(r.content)["aps"]["badge"] == 2 for r in requests)
    with db.get_db(config.db_path) as conn:
        assert all(d["last_error"] is None and d["last_badge"] == 2 for d in devices.devices_for_user(conn, ACCOUNT))


def test_missing_signing_key_warns_once_and_sends_only_the_generic_alert(private_sweep, caplog):
    from istota.notifications import store
    config, _, _, requests, _, room = private_sweep
    Path(config.push.signing_key_path).unlink()
    dispatch.run_push_dispatch(config)
    dispatch.run_push_dispatch(config)
    assert sum("signing key unreadable" in r.getMessage() for r in caplog.records) == 1
    tokens = {r.url.path.rsplit("/", 1)[1] for r in requests}
    assert tokens == {"cd" * 32, "ef" * 32, "ab" * 32}
    for request in requests:
        body = json.loads(request.content)
        assert "istota_private" not in body and "mutable-content" not in body["aps"]
        plaintext_free(request.content, request.headers.get("apns-collapse-id"), room.token,
                       "Approval needed", "notification_id")
    with db.get_db(config.db_path) as conn:
        assert store.take_pending_pushes(conn) == []


def test_a_refused_seal_is_recorded_and_the_next_device_still_gets_its_alerts(private_sweep, caplog):
    from istota.notifications import store
    config, _, _, requests, ids, _ = private_sweep
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE push_devices SET encryption_public_key='not-a-key' WHERE id=?", (ids["encrypted"],))
    dispatch.run_push_dispatch(config)
    assert {r.url.path.rsplit("/", 1)[1] for r in requests} == {"cd" * 32, "ef" * 32}
    with db.get_db(config.db_path) as conn:
        rows = {d["id"]: d for d in devices.devices_for_user(conn, ACCOUNT)}
        assert rows[ids["encrypted"]]["last_error"] == "private_payload"
        assert rows[ids["encrypted"]]["encryption_public_key"] == "not-a-key"
        assert not store.take_pending_pushes(conn)
    assert f"device={ids['encrypted']}" in caplog.text and "not-a-key" not in caplog.text


def test_cli_push_test_goes_through_the_seam(private_sweep, monkeypatch, capsys):
    from types import SimpleNamespace
    from istota import cli
    config, signing_key, device_key, requests, _, _ = private_sweep
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: config)
    assert cli.cmd_push_test(SimpleNamespace(config=None, user=ACCOUNT, message=BODY)) == 0
    assert len(requests) == 3
    for request in requests:
        plaintext_free(request.content, request.headers.get("apns-collapse-id"))
    encrypted, = [r for r in requests if r.url.path.endswith("ab" * 32)]
    opened = envelope.open_for_tests(encrypted.content, device_private_key=device_key,
                                     sender_public_key=signing_key.public_key(), now=time.time())
    assert opened.plaintext["body"] == BODY
    assert "accepted by APNs" in capsys.readouterr().out


def test_cli_push_test_refuses_a_private_deployment_without_its_key(private_sweep, monkeypatch, capsys):
    from types import SimpleNamespace
    from istota import cli
    config, _, _, requests, _, _ = private_sweep
    Path(config.push.signing_key_path).unlink()
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: config)
    assert cli.cmd_push_test(SimpleNamespace(config=None, user=ACCOUNT, message=BODY)) == 1
    assert not requests
    assert "signing key" in capsys.readouterr().err


def test_a_badge_past_the_envelope_range_is_clamped_not_refused(private):
    config, signing_key, device_key = private
    device = encrypted_device(signing_key, device_key)
    logical = chat_alert(badge=12345)
    prepared = delivery.build_delivery(config, device, logical, now=NOW, signing_key=signing_key)
    opened = envelope.open_for_tests(prepared.payload, device_private_key=device_key,
                                     sender_public_key=signing_key.public_key(), now=NOW,
                                     binding=BINDING, generation=GENERATION)
    assert json.loads(prepared.payload)["aps"]["badge"] == opened.header["badge"] == 9999
    assert opened.plaintext["badge"] == 9999


def test_envelopes_are_stamped_when_the_user_is_built_not_when_the_sweep_began(private_sweep, monkeypatch):
    config, signing_key, device_key, requests, ids, room = private_sweep
    calls = []

    def clock():
        calls.append(None)
        return NOW if len(calls) == 1 else NOW + 200

    monkeypatch.setattr(dispatch.time, "time", clock)
    dispatch.run_push_dispatch(config)
    sealed = [r for r in requests if r.url.path.endswith("ab" * 32)]
    assert sealed
    for request in sealed:
        opened = envelope.open_for_tests(request.content, device_private_key=device_key,
                                         sender_public_key=signing_key.public_key(), now=NOW + 200)
        assert opened.header["iat"] == int(NOW + 200)
