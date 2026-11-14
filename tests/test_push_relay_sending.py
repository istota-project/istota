"""Relay delivery: `send_prepared`'s second branch, against a mock relay speaking istota-relay's schema.

`MockRelay` follows istota-relay's `src/relay/send.py` and `web.py`: `POST
{base}/v1/routes/{route_id}/send` with `Authorization: Bearer <capability>`
and a strict JSON body (`version` 1, a 16-byte base64url `request_id`, `kind`,
then `envelope` for an alert or `badge` for a badge), answered with one of the
finite results at its HTTP status.
"""

import dataclasses
import json
import logging
import time
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from istota import db
from istota.config import Config, PushConfig
from istota.notifications import store
from istota.push import apns, delivery, devices, dispatch, envelope, relay_client

RELAY_URL = "https://relay.example.com/istota"
BINDING = "0f8e4b0c-2a6d-4c4e-9a43-6d2f1b8e7c51"
GENERATION = "AAECAwQFBgcICQoLDA0ODw"
ACCOUNT = "alice"
ROOM_TOKEN = "rm_testroomtoken01"
CAPABILITY = envelope.b64url(b"\x5a" * 32)
OTHER_CAPABILITY = envelope.b64url(b"\x3c" * 32)
LEASE = 1_900_000_000
NOW = 2_000_000_000.0


def route_id(n: int) -> str:
    return envelope.b64url(bytes([n]) * 16)


def is_id(text) -> bool:
    try:
        return isinstance(text, str) and len(envelope.b64url_decode(text)) == 16
    except ValueError:
        return False


class MockRelay:
    """The relay's send endpoint. Answers are queued as (status, body, headers); default `accepted`."""

    def __init__(self, signing_key, capabilities):
        self.signing_key = signing_key
        self.capabilities = capabilities  # route_id -> capability
        self.requests = []
        self.problems = []
        self.answers = []

    def _refuse(self, problem):
        self.problems.append(problem)
        return httpx.Response(400, json={"result": "invalid_payload"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        prefix = "/istota/v1/routes/"
        path = request.url.path
        if request.method != "POST" or request.url.host != "relay.example.com" or not path.startswith(prefix):
            return self._refuse(f"wrong endpoint {request.method} {request.url}")
        route, _, tail = path[len(prefix):].partition("/")
        if tail != "send" or not is_id(route) or request.url.query:
            return self._refuse("wrong path")
        if request.headers.get("content-type") != "application/json":
            return self._refuse("content type")
        if request.headers.get_list("authorization") != [f"Bearer {self.capabilities.get(route)}"]:
            return httpx.Response(401, json={"result": "unauthorized_route"})
        body = json.loads(request.content)
        if not isinstance(body, dict) or body.get("version") != 1 or not is_id(body.get("request_id")):
            return self._refuse("version or request_id")
        if body.get("kind") == "alert":
            if set(body) - {"version", "request_id", "kind", "envelope", "badge", "collapse"} or "envelope" not in body:
                return self._refuse("alert keys")
            if not isinstance(body["envelope"], dict) or set(body["envelope"]) != {"h", "c", "s"}:
                return self._refuse("envelope shape")
        elif body.get("kind") == "badge":
            if set(body) != {"version", "request_id", "kind", "badge"} or not 0 <= body["badge"] <= 9999:
                return self._refuse("badge keys")
        else:
            return self._refuse("kind")
        if self.answers:
            status, answer, headers = self.answers.pop(0)
        else:
            status, answer, headers = 200, {"result": "accepted", "apns_status": 200}, {}
        if isinstance(answer, (dict, list)):
            return httpx.Response(status, json=answer, headers=headers)
        return httpx.Response(status, content=answer, headers=headers)

    def answer(self, status, body, headers=None):
        self.answers.append((status, body, headers or {}))


@pytest.fixture
def relay(tmp_path, monkeypatch):
    """A relay installation: signing key, no APNs settings, one encrypted relay device, a mock relay."""
    key_path = tmp_path / "config" / "push" / "signing.pem"
    signing_key = envelope.write_signing_key(key_path)
    push = PushConfig(enabled=True, content="private", signing_key_path=str(key_path), delivery="relay",
                      relay_url=RELAY_URL, relay_timeout_seconds=7)
    config = Config(db_path=tmp_path / "istota.db", push=push, bot_name="Istota")
    db.init_db(config.db_path)
    device_key = X25519PrivateKey.generate()
    mock = MockRelay(signing_key, {})
    client = httpx.Client(transport=httpx.MockTransport(mock))
    monkeypatch.setattr(relay_client, "_client", lambda: client)
    monkeypatch.setattr(dispatch.time, "time", lambda: NOW)
    monkeypatch.setattr(dispatch, "_warned_unavailable", False)
    monkeypatch.setattr(dispatch, "_warned_signing_key", False)

    def no_apns(*args, **kwargs):
        pytest.fail("relay delivery must not call APNs")

    monkeypatch.setattr(apns, "send", no_apns)
    state = SimpleNamespace(config=config, signing_key=signing_key, device_key=device_key, mock=mock,
                            client=client, directory=devices.relay_credentials_dir(config))
    state.enroll = lambda token="ab" * 32, route=1, **kw: _enroll(state, token, route, **kw)
    yield state
    client.close()


def _enroll(state, token, route, *, capability=CAPABILITY, user=ACCOUNT):
    raw = state.device_key.public_key().public_bytes_raw()
    sender = envelope.key_id(state.signing_key.public_key().public_bytes_raw())
    relay = {"route_id": route_id(route), "send_capability": capability, "lease_expires_at": LEASE}
    with db.get_db(state.config.db_path) as conn:
        device_id = devices.register_device(
            conn, user, token, "production", "ios", "0.15.0", auth_method="nextcloud", auth_epoch=0,
            connection_binding=BINDING, connection_generation=3, private_version=1, private_content="encrypted",
            encryption_public_key=envelope.b64url(raw), encryption_key_id=envelope.key_id(raw),
            sender_key_id=sender, push_generation=GENERATION, relay=relay, credentials_dir=state.directory,
            relay_url=state.config.push.relay_url)
    assert device_id is not None
    state.mock.capabilities[route_id(route)] = capability
    return device_id


def row(state, device_id):
    with db.get_db(state.config.db_path) as conn:
        found = conn.execute("SELECT * FROM push_devices WHERE id = ?", (device_id,)).fetchone()
    return dict(found) if found else None


def chat_alert(badge=3):
    group = "room:" + ROOM_TOKEN
    return delivery.alert("Quarterly planning", "The draft budget", badge, thread=group, group=group,
                          target={"kind": "chat", "room_token": ROOM_TOKEN, "room_id": 7})


def send_one(state, device_id, logical=None):
    device = row(state, device_id)
    prepared = delivery.build_delivery(state.config, device, logical or chat_alert(), now=time.time(),
                                       signing_key=state.signing_key)
    result = delivery.send_prepared(state.config, device, prepared)
    with db.get_db(state.config.db_path) as conn:
        devices.record_result(conn, device_id, result,
                              credentials_dir=devices.relay_credentials_dir(state.config))
    return prepared, result


# --- The request ---------------------------------------------------------------


def test_an_alert_goes_to_the_route_under_the_base_path_with_its_capability(relay):
    device_id = relay.enroll()
    prepared, result = send_one(relay, device_id)
    assert result.success and not relay.mock.problems
    request, = relay.mock.requests
    assert str(request.url) == f"{RELAY_URL}/v1/routes/{route_id(1)}/send"
    assert request.headers["authorization"] == f"Bearer {CAPABILITY}"
    body = json.loads(request.content)
    assert set(body) == {"version", "request_id", "kind", "envelope"}
    assert (body["version"], body["kind"], body["request_id"]) == (1, "alert", prepared.event_id)
    assert body["envelope"] == json.loads(prepared.payload)["istota_private"]
    opened = envelope.open_for_tests(
        json.dumps({"aps": {}, "istota_private": body["envelope"]}).encode(),
        device_private_key=relay.device_key, sender_public_key=relay.signing_key.public_key(),
        now=time.time(), binding=BINDING, generation=GENERATION)
    assert opened.plaintext["title"] == "Quarterly planning"
    assert request.extensions["timeout"] == {"connect": 7, "read": 7, "write": 7, "pool": 7}


def test_a_badge_goes_as_a_badge_with_a_fresh_request_id(relay):
    device_id = relay.enroll()
    send_one(relay, device_id, delivery.badge_only(4))
    send_one(relay, device_id, delivery.badge_only(4))
    bodies = [json.loads(r.content) for r in relay.mock.requests]
    assert [set(b) for b in bodies] == [{"version", "request_id", "kind", "badge"}] * 2
    assert [(b["kind"], b["badge"]) for b in bodies] == [("badge", 4)] * 2
    assert all(is_id(b["request_id"]) for b in bodies)
    assert bodies[0]["request_id"] != bodies[1]["request_id"]
    assert row(relay, device_id)["last_badge"] == 4


def test_a_badge_past_the_relay_range_is_clamped(relay):
    device_id = relay.enroll()
    send_one(relay, device_id, delivery.badge_only(123456))
    assert json.loads(relay.mock.requests[0].content)["badge"] == 9999
    assert not relay.mock.problems


def test_one_logical_push_gives_the_same_envelope_through_direct_and_relay(relay, monkeypatch):
    device_id = relay.enroll()
    device = row(relay, device_id)
    direct_config = dataclasses.replace(relay.config, push=dataclasses.replace(relay.config.push, delivery="direct"))
    # Pin the event ID so the two builds' signed headers can be compared byte for byte.
    monkeypatch.setattr(envelope, "new_event_id", lambda: route_id(9))
    built = {name: delivery.build_delivery(cfg, device, chat_alert(), now=NOW, signing_key=relay.signing_key)
             for name, cfg in (("direct", direct_config), ("relay", relay.config))}
    direct, relayed = (json.loads(built[name].payload) for name in ("direct", "relay"))
    assert direct["aps"] == relayed["aps"]
    assert direct["istota_private"]["h"] == relayed["istota_private"]["h"]
    assert dataclasses.replace(built["direct"], payload=b"") == dataclasses.replace(built["relay"], payload=b"")

    # One prepared push through each transport: the same envelope crosses both wires.
    apns_calls = []
    monkeypatch.setattr(apns, "send", lambda push, dev, payload, **kw: apns_calls.append((payload, kw))
                        or apns.ApnsResult(status=200))
    prepared = built["relay"]
    delivery.send_prepared(direct_config, device, prepared)
    delivery.send_prepared(relay.config, device, prepared)
    (apns_payload, apns_kwargs), = apns_calls
    sent = json.loads(relay.mock.requests[0].content)
    assert apns_payload == prepared.payload
    assert sent["envelope"] == json.loads(apns_payload)["istota_private"]
    assert sent["request_id"] == prepared.event_id == route_id(9)
    header = json.loads(envelope.b64url_decode(sent["envelope"]["h"]))
    assert header["collapse"] == apns_kwargs["collapse_id"] == prepared.collapse_id


# --- Results -----------------------------------------------------------------------


@pytest.mark.parametrize("status,answer,error", [
    (429, {"result": "rate_limited", "retry_after": 5}, "429 rate_limited"),
    (429, {"result": "rate_limited", "apns_status": 429, "apns_reason": "TooManyRequests", "retry_after": 60},
     "429 TooManyRequests"),
    (429, {"result": "rate_limited", "apns_status": 429, "apns_reason": "Made up", "retry_after": 60},
     "429 rate_limited"),
    (502, {"result": "provider_unavailable", "apns_status": 500, "apns_reason": "InternalServerError"},
     "502 provider_unavailable"),
    (502, {"result": "provider_unavailable", "apns_status": 403, "apns_reason": "InvalidProviderToken"},
     "502 provider_unavailable"),
    (503, {"result": "provider_unavailable", "retry_after": 1}, "503 provider_unavailable"),
    (504, {"result": "delivery_unknown"}, "504 delivery_unknown"),
    (400, {"result": "invalid_payload"}, "400 invalid_payload"),
    (409, {"result": "invalid_payload"}, "409 invalid_payload"),
    (413, {"result": "invalid_payload"}, "413 invalid_payload"),
])
def test_a_transient_or_refused_send_keeps_the_device_active_and_is_not_resent(relay, status, answer, error):
    device_id = relay.enroll()
    relay.mock.answer(status, answer)
    _, result = send_one(relay, device_id)
    assert not result.success
    assert len(relay.mock.requests) == 1
    kept = row(relay, device_id)
    assert (kept["enrollment_state"], kept["last_error"]) == ("active", error)
    assert (relay.directory / kept["relay_credential_ref"]).is_file()


@pytest.mark.parametrize("status,answer", [
    (401, {"result": "unauthorized_route"}),
    (403, {"result": "expired_route"}),
])
def test_an_expired_or_revoked_route_marks_the_device_for_repair(relay, status, answer):
    device_id = relay.enroll()
    relay.mock.answer(status, answer)
    send_one(relay, device_id)
    marked = row(relay, device_id)
    assert (marked["enrollment_state"], marked["last_error"]) == ("repair", f"{status} {answer['result']}")
    assert marked["relay_route_id"] == route_id(1)


@pytest.mark.parametrize("status,answer", [
    (410, {"result": "invalid_device", "apns_status": 410, "apns_reason": "Unregistered",
           "invalidated_at": int(NOW * 1000) + 10_000_000_000}),
    (410, {"result": "invalid_device", "apns_status": 410, "apns_reason": "Unregistered"}),
    (410, {"result": "invalid_device", "apns_status": 400, "apns_reason": "BadDeviceToken"}),
])
def test_an_invalid_device_deletes_the_row_and_its_credential_file(relay, status, answer):
    device_id = relay.enroll()
    other = relay.enroll("cd" * 32, route=2, capability=OTHER_CAPABILITY)
    cap = relay.directory / f"{device_id}.cap"
    assert cap.is_file()
    relay.mock.answer(status, answer)
    send_one(relay, device_id)
    assert row(relay, device_id) is None
    assert not cap.exists()
    assert row(relay, other) is not None and (relay.directory / f"{other}.cap").is_file()


def test_a_late_result_does_not_touch_a_newer_registration(relay):
    device_id = relay.enroll()
    stale = row(relay, device_id)
    relay.enroll()  # the same token registers again during the send
    for result in (apns.ApnsResult(410, "Unregistered", int(NOW * 1000) * 10, None, stale["created_at"]),
                   apns.ApnsResult(403, "expired_route", None, None, stale["created_at"])):
        with db.get_db(relay.config.db_path) as conn:
            devices.record_result(conn, device_id, result, credentials_dir=relay.directory)
    current = row(relay, device_id)
    assert current["enrollment_state"] == "active"
    assert (relay.directory / current["relay_credential_ref"]).is_file()


@pytest.mark.parametrize("status,body", [
    (200, {"result": "expired_route"}),
    (200, {"result": "accepted", "apns_status": 200, "extra": 1}),
    (200, {"result": "accepted", "apns_status": True}),
    (200, {"result": "accepted", "apns_status": "200"}),
    (200, {"result": "teapot"}),
    (200, ["accepted"]),
    (200, b"accepted"),
    (200, b'{"result":"accepted","result":"accepted"}'),
    pytest.param(200, b'{"result": "accepted", "pad": "' + b"x" * 5000 + b'"}', id="oversized-extra-key"),
    pytest.param(200, b'{"result":"accepted"}' + b" " * 5000, id="oversized-valid-answer"),
    (410, {"result": "invalid_device", "invalidated_at": "soon"}),
    (429, {"result": "rate_limited", "retry_after": -1}),
    (500, {"error": "internal"}),
    (503, {"error": "unavailable"}),
    (418, {"result": "accepted"}),
])
def test_an_answer_outside_the_schema_is_delivery_unknown(relay, status, body):
    device_id = relay.enroll()
    relay.mock.answer(status, body)
    _, result = send_one(relay, device_id)
    assert (result.success, result.reason) == (False, "delivery_unknown")
    kept = row(relay, device_id)
    assert (kept["enrollment_state"], kept["last_error"]) == ("active", "0 delivery_unknown")


@pytest.mark.parametrize("follow", [False, True])
def test_a_redirect_is_not_followed(relay, monkeypatch, follow):
    device_id = relay.enroll()
    client = httpx.Client(transport=httpx.MockTransport(relay.mock), follow_redirects=follow)
    monkeypatch.setattr(relay_client, "_client", lambda: client)
    relay.mock.answer(307, b"", {"location": "https://elsewhere.example.com/v1/routes/x/send"})
    _, result = send_one(relay, device_id)
    client.close()
    assert len(relay.mock.requests) == 1
    assert result.reason == "delivery_unknown"
    assert row(relay, device_id)["enrollment_state"] == "active"


def test_the_production_client_does_not_follow_redirects():
    client = relay_client._client()
    assert client.follow_redirects is False


@pytest.mark.parametrize("error,reason", [
    (httpx.ConnectError("private detail"), "relay_unreachable"),
    (httpx.ConnectTimeout("private detail"), "relay_unreachable"),
    (httpx.ReadTimeout("private detail"), "delivery_unknown"),
    (httpx.RemoteProtocolError("private detail"), "delivery_unknown"),
])
def test_a_transport_failure_keeps_the_device_and_logs_no_detail(relay, monkeypatch, caplog, error, reason):
    caplog.set_level(logging.DEBUG)
    device_id = relay.enroll()

    def fail(request):
        relay.mock.requests.append(request)
        raise error

    client = httpx.Client(transport=httpx.MockTransport(fail))
    monkeypatch.setattr(relay_client, "_client", lambda: client)
    _, result = send_one(relay, device_id)
    client.close()
    assert result.reason == reason
    assert len(relay.mock.requests) == 1
    assert row(relay, device_id)["enrollment_state"] == "active"
    assert "private detail" not in caplog.text
    assert CAPABILITY not in caplog.text and route_id(1) not in caplog.text


def test_the_capability_never_reaches_a_log_line_or_the_row(relay, caplog):
    caplog.set_level(logging.DEBUG)
    device_id = relay.enroll()
    for status, answer in ((401, {"result": "unauthorized_route"}), (504, {"result": "delivery_unknown"})):
        relay.mock.answer(status, answer)
        send_one(relay, device_id)
    assert CAPABILITY not in caplog.text
    assert CAPABILITY not in json.dumps(row(relay, device_id))


# --- Missing credentials and devices that cannot use the relay ------------------------


def test_a_missing_credential_file_marks_repair_without_a_request(relay):
    device_id = relay.enroll()
    (relay.directory / f"{device_id}.cap").unlink()
    _, result = send_one(relay, device_id)
    assert not relay.mock.requests
    marked = row(relay, device_id)
    assert marked["enrollment_state"] == "repair"
    assert marked["last_error"] == "0 relay_credential_missing"


@pytest.mark.parametrize("content", ["", "not a capability", CAPABILITY + "\n" + CAPABILITY])
def test_an_unusable_credential_file_marks_repair_without_a_request(relay, content):
    device_id = relay.enroll()
    (relay.directory / f"{device_id}.cap").write_text(content)
    send_one(relay, device_id)
    assert not relay.mock.requests
    assert row(relay, device_id)["enrollment_state"] == "repair"


def test_a_credential_file_that_is_a_link_is_not_followed(relay, tmp_path):
    device_id = relay.enroll()
    cap = relay.directory / f"{device_id}.cap"
    target = tmp_path / "elsewhere.cap"
    target.write_text(CAPABILITY)
    cap.unlink()
    cap.symlink_to(target)
    send_one(relay, device_id)
    assert not relay.mock.requests
    assert row(relay, device_id)["enrollment_state"] == "repair"


def test_a_device_left_from_direct_delivery_marks_repair_without_a_request(relay):
    with db.get_db(relay.config.db_path) as conn:
        device_id = devices.register_device(conn, ACCOUNT, "ef" * 32, "production", auth_method="nextcloud",
                                            auth_epoch=0)
    send_one(relay, device_id, delivery.badge_only(2))
    assert not relay.mock.requests
    assert row(relay, device_id)["enrollment_state"] == "repair"


def test_a_generic_alert_is_never_sent_through_the_relay(relay):
    device_id = relay.enroll()
    with db.get_db(relay.config.db_path) as conn:
        conn.execute("UPDATE push_devices SET sender_key_id = ? WHERE id = ?", ("A" * 43, device_id))
    _, result = send_one(relay, device_id)
    assert not result.success
    assert not relay.mock.requests
    kept = row(relay, device_id)
    assert kept["enrollment_state"] == "active"
    assert kept["last_error"] == "0 relay_needs_envelope"
    send_one(relay, device_id, delivery.badge_only(5))
    assert [json.loads(r.content)["kind"] for r in relay.mock.requests] == ["badge"]


# --- The dispatcher ---------------------------------------------------------------------


def _pending_alert(state, key="task:1"):
    with db.get_db(state.config.db_path) as conn:
        result = store.write_notification(conn, ACCOUNT, source="confirmation", dedup_key=key,
                                          title="Approval needed", body="Review it", link="/chat")
        conn.execute("UPDATE notifications SET updated_at = ? WHERE id = ?",
                     ("2033-05-18T03:33:20+00:00", result.notification_id))


def test_the_dispatcher_sends_through_the_relay_with_no_apns_key_or_h2(relay, monkeypatch):
    monkeypatch.setattr(dispatch.importlib.util, "find_spec", lambda name: None)
    device_id = relay.enroll()
    _pending_alert(relay)
    dispatch.run_push_dispatch(relay.config)
    assert not relay.mock.problems
    assert [json.loads(r.content)["kind"] for r in relay.mock.requests] == ["alert"]
    assert row(relay, device_id)["last_error"] is None
    with db.get_db(relay.config.db_path) as conn:
        assert store.take_pending_pushes(conn) == []


def test_relay_delivery_with_push_disabled_warns_once_and_leaves_pending_work(relay, caplog):
    relay.enroll()
    _pending_alert(relay)
    relay.config.push.enabled = False
    dispatch.run_push_dispatch(relay.config)
    dispatch.run_push_dispatch(relay.config)
    assert not relay.mock.requests
    assert caplog.text.count("Native push unavailable") == 1
    with db.get_db(relay.config.db_path) as conn:
        assert len(store.take_pending_pushes(conn)) == 1


def test_the_dispatcher_skips_a_device_in_repair(relay):
    repaired = relay.enroll()
    healthy = relay.enroll("cd" * 32, route=2, capability=OTHER_CAPABILITY)
    with db.get_db(relay.config.db_path) as conn:
        conn.execute("UPDATE push_devices SET enrollment_state = 'repair' WHERE id = ?", (repaired,))
    _pending_alert(relay)
    dispatch.run_push_dispatch(relay.config)
    assert [r.url.path.split("/")[-2] for r in relay.mock.requests] == [route_id(2)]
    assert row(relay, healthy)["last_error"] is None


def test_a_device_marked_for_repair_mid_sweep_gets_no_further_alerts(relay):
    device_id = relay.enroll()
    _pending_alert(relay, "task:1")
    _pending_alert(relay, "task:2")
    relay.mock.answer(403, {"result": "expired_route"})
    dispatch.run_push_dispatch(relay.config)
    assert len(relay.mock.requests) == 1
    assert row(relay, device_id)["enrollment_state"] == "repair"
    dispatch.run_push_dispatch(relay.config)
    assert len(relay.mock.requests) == 1


def test_registering_again_brings_a_repaired_device_back(relay):
    device_id = relay.enroll()
    with db.get_db(relay.config.db_path) as conn:
        conn.execute("UPDATE push_devices SET enrollment_state = 'repair' WHERE id = ?", (device_id,))
    assert relay.enroll() == device_id
    _pending_alert(relay)
    dispatch.run_push_dispatch(relay.config)
    assert len(relay.mock.requests) == 1


def _direct_config(tmp_path, monkeypatch):
    key = tmp_path / "apns.p8"
    key.write_text("unused")
    key.chmod(0o600)
    config = Config(db_path=tmp_path / "test.db", push=PushConfig(
        enabled=True, apns_key_path=str(key), apns_key_id="KEY", apns_team_id="TEAM"))
    db.init_db(config.db_path)
    monkeypatch.setattr(dispatch, "_warned_unavailable", False)
    sent = []
    monkeypatch.setattr(apns, "send", lambda push, device, payload, **kw: sent.append(device["id"])
                        or apns.ApnsResult(status=200, device_created_at=device["created_at"]))
    with db.get_db(config.db_path) as conn:
        repaired = devices.register_device(conn, ACCOUNT, "ab" * 32, "sandbox", auth_method="nextcloud",
                                           auth_epoch=0)
        healthy = devices.register_device(conn, ACCOUNT, "cd" * 32, "sandbox", auth_method="nextcloud",
                                          auth_epoch=0)
        conn.execute("UPDATE push_devices SET enrollment_state = 'repair' WHERE id = ?", (repaired,))
    return config, sent, repaired, healthy


def test_the_direct_dispatcher_sends_to_a_device_left_in_repair(tmp_path, monkeypatch):
    # Repair is a relay state: after a switch to direct, APNs reaches the token whatever a relay said.
    config, sent, repaired, healthy = _direct_config(tmp_path, monkeypatch)
    with db.get_db(config.db_path) as conn:
        store.write_notification(conn, ACCOUNT, source="confirmation", dedup_key="k", title="T", body="B",
                                 link="/chat")
    dispatch.run_push_dispatch(config)
    assert sent == [repaired, healthy]


def test_cli_push_test_sends_to_a_device_left_in_repair_under_direct_delivery(tmp_path, monkeypatch, capsys):
    from istota import cli
    config, sent, repaired, healthy = _direct_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: config)
    assert cli.cmd_push_test(SimpleNamespace(config=None, user=ACCOUNT, message="Hello")) == 0
    assert sent == [repaired, healthy]
    assert "enroll again" not in capsys.readouterr().out


def test_the_push_credentials_check_reports_devices_left_in_repair(tmp_path, monkeypatch):
    from istota import doctor
    config, _, _, _ = _direct_config(tmp_path, monkeypatch)
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())
    result = doctor.check_push_credentials(config, False)
    assert result.status == doctor.OK
    assert "1 device" in result.detail and "relay" in result.detail
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE push_devices SET enrollment_state = 'active'")
    assert "relay" not in doctor.check_push_credentials(config, False).detail


# --- The CLI -----------------------------------------------------------------------------


def test_cli_push_test_goes_through_the_relay(relay, monkeypatch, capsys):
    from istota import cli
    device_id = relay.enroll()
    repaired = relay.enroll("cd" * 32, route=2, capability=OTHER_CAPABILITY)
    with db.get_db(relay.config.db_path) as conn:
        conn.execute("UPDATE push_devices SET enrollment_state = 'repair' WHERE id = ?", (repaired,))
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: relay.config)
    assert cli.cmd_push_test(SimpleNamespace(config=None, user=ACCOUNT, message="Hello")) == 1
    request, = relay.mock.requests
    assert request.url.path.endswith(f"/{route_id(1)}/send")
    out = capsys.readouterr().out
    assert f"Device {device_id}" in out and "accepted by the relay" in out
    assert f"Device {repaired}" in out and "enroll again" in out
    assert CAPABILITY not in out and OTHER_CAPABILITY not in out


def test_cli_push_test_reports_a_relay_refusal(relay, monkeypatch, capsys):
    from istota import cli
    device_id = relay.enroll()
    relay.mock.answer(403, {"result": "expired_route"})
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: relay.config)
    assert cli.cmd_push_test(SimpleNamespace(config=None, user=ACCOUNT, message="Hello")) == 1
    assert "403 expired_route" in capsys.readouterr().out
    assert row(relay, device_id)["enrollment_state"] == "repair"


# --- Doctor ------------------------------------------------------------------------------


def test_doctor_counts_devices_in_repair(relay):
    from istota import doctor
    first = relay.enroll()
    relay.enroll("cd" * 32, route=2, capability=OTHER_CAPABILITY)
    assert doctor.check_push_relay(relay.config, False).status == doctor.OK
    with db.get_db(relay.config.db_path) as conn:
        conn.execute("UPDATE push_devices SET enrollment_state = 'repair' WHERE id = ?", (first,))
    result = doctor.check_push_relay(relay.config, False)
    assert result.status == doctor.WARN
    assert "1 device" in result.detail and "repair" in result.detail
    assert result.remedy


def test_doctor_counts_a_device_enrolled_under_an_older_key_as_needing_repair(relay):
    from istota import doctor
    device_id = relay.enroll()
    with db.get_db(relay.config.db_path) as conn:
        conn.execute("UPDATE push_devices SET sender_key_id = ? WHERE id = ?", ("A" * 43, device_id))
    result = doctor.check_push_relay(relay.config, False)
    assert result.status == doctor.WARN
    assert "1 device" in result.detail and "older" in result.detail


def test_doctor_does_not_count_a_stale_key_while_the_public_key_is_unreadable(relay):
    from istota import doctor
    from pathlib import Path
    device_id = relay.enroll()
    with db.get_db(relay.config.db_path) as conn:
        conn.execute("UPDATE push_devices SET sender_key_id = ? WHERE id = ?", ("A" * 43, device_id))
    Path(relay.config.push.signing_key_path + ".pub").unlink()
    assert doctor.check_push_relay(relay.config, False).status == doctor.OK
