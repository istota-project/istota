"""APNs protocol and operator seams, with generated keys and no network."""

from istota.config import config_from_document


def test_push_config_maps_through_the_real_loader():
    config = config_from_document({"push": {"enabled": True, "chat_delay_seconds": 21}})
    assert getattr(config, "push", None) is not None
    assert config.push.enabled is True
    assert config.push.chat_delay_seconds == 21
    assert config.push.dispatch_interval_seconds == 5


import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from istota import db, doctor
from istota.config import Config, PushConfig, UserConfig, load_config
from istota.push import apns, devices

TOKEN = "ab" * 32
PAYLOAD = {"aps": {"alert": {"title": "Test"}, "badge": 3}}


@pytest.fixture
def signing(tmp_path, monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "signing.p8"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    path.chmod(0o600)
    monkeypatch.setattr(apns, "_cached_token", None)
    monkeypatch.setattr(apns, "_cached_credentials", None)
    monkeypatch.setattr(apns, "_cached_at", 0)
    return PushConfig(enabled=True, apns_key_path=str(path), apns_key_id="KEYEXAMPLE", apns_team_id="TEAMEXAMPL", apns_topic="com.example.app"), key


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        devices.register_device(conn, "alice", TOKEN, "sandbox", auth_method="nextcloud", auth_epoch=0)
        conn.execute("UPDATE push_devices SET created_at='2026-01-01T00:00:00.000Z', last_error='old'")
    return path


def registered(path):
    with db.get_db(path) as conn:
        return devices.devices_for_user(conn, "alice")[0]


def unbase64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_jwt_raw_signature_verifies_and_cache_respects_age(signing, monkeypatch):
    config, key = signing
    now = [1800000000]
    monkeypatch.setattr(apns.time, "time", lambda: now[0])
    token = apns.provider_token(config)
    header, claims, signature = token.split(".")
    assert json.loads(unbase64(header)) == {"alg": "ES256", "kid": config.apns_key_id}
    assert json.loads(unbase64(claims)) == {"iss": config.apns_team_id, "iat": now[0]}
    raw = unbase64(signature)
    assert len(raw) == 64
    key.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")), f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    now[0] += 1199
    assert apns.provider_token(config, force_refresh=True) == token
    now[0] += 1800
    assert apns.provider_token(config) == token
    now[0] += 1
    assert apns.provider_token(config) != token


@pytest.mark.parametrize("environment,host", [("sandbox", "api.sandbox.push.apple.com"), ("production", "api.push.apple.com")])
def test_wire_headers_hosts_payload_and_success_recording(signing, registry, environment, host):
    config, _ = signing
    device = registered(registry)
    device["environment"] = environment
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200)
    result = apns.send(config, device, PAYLOAD, priority=5, collapse_id="badge", expiration=123, transport=httpx.MockTransport(respond))
    assert result.success
    request, = requests
    assert request.method == "POST"
    assert request.url.host == host
    assert request.url.path == f"/3/device/{TOKEN}"
    assert request.headers["apns-topic"] == "com.example.app"
    assert request.headers["apns-push-type"] == "alert"
    assert request.headers["apns-priority"] == "5"
    assert request.headers["apns-expiration"] == "123"
    assert request.headers["apns-collapse-id"] == "badge"
    assert request.headers["authorization"].startswith("bearer ")
    assert json.loads(request.content) == PAYLOAD
    with db.get_db(registry) as conn:
        devices.record_result(conn, device["id"], result)
        row, = devices.devices_for_user(conn, "alice")
    assert row["last_seen_at"] and row["last_error"] is None
    assert row["last_badge"] == 3


@pytest.mark.parametrize("status,reason,deleted,unchanged", [
    (400, "BadDeviceToken", True, False), (400, "DeviceTokenNotForTopic", True, False),
    (400, "BadTopic", False, True), (403, "InvalidProviderToken", False, True),
    (429, "TooManyRequests", False, False), (503, "ServiceUnavailable", False, False),
])
def test_response_actions_through_http_and_database(signing, registry, status, reason, deleted, unchanged):
    config, _ = signing
    device = registered(registry)
    result = apns.send(config, device, PAYLOAD, transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"reason": reason})))
    with db.get_db(registry) as conn:
        devices.record_result(conn, device["id"], result)
        rows = devices.devices_for_user(conn, "alice")
    if deleted:
        assert rows == []
    else:
        assert rows[0]["last_error"] == ("old" if unchanged else f"{status} {reason}")
        assert rows[0]["last_badge"] == -1


@pytest.mark.parametrize("delta,deleted", [(-1, False), (0, True), (1, True)])
def test_unregistered_timestamp_is_milliseconds_and_preserves_new_registration(signing, registry, delta, deleted):
    config, _ = signing
    device = registered(registry)
    epoch_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    result = apns.send(config, device, PAYLOAD, transport=httpx.MockTransport(lambda request: httpx.Response(410, json={"reason": "Unregistered", "timestamp": epoch_ms + delta})))
    with db.get_db(registry) as conn:
        devices.record_result(conn, device["id"], result)
        assert bool(devices.devices_for_user(conn, "alice")) is not deleted


@pytest.mark.parametrize("status,reason", [(200, ""), (400, "BadDeviceToken"), (410, "Unregistered"), (429, "TooManyRequests")])
def test_result_cannot_change_re_registered_device(signing, registry, status, reason):
    config, _ = signing
    device = registered(registry)
    def respond(request):
        with db.get_db(registry) as conn:
            devices.register_device(conn, "bob", TOKEN, "sandbox")
        return httpx.Response(status, json={"reason": reason, "timestamp": 9999999999999})
    result = apns.send(config, device, PAYLOAD, transport=httpx.MockTransport(respond))
    with db.get_db(registry) as conn:
        devices.record_result(conn, device["id"], result)
        row, = devices.devices_for_user(conn, "bob")
        assert row["last_badge"] == -1 and row["last_error"] is None and row["last_seen_at"] is None


@pytest.mark.parametrize("second_status", [200, 403])
def test_expired_provider_refreshes_and_retries_once(signing, registry, monkeypatch, second_status):
    config, _ = signing
    now = [1800000000]
    monkeypatch.setattr(apns.time, "time", lambda: now[0])
    original = apns.provider_token(config)
    now[0] += 1200
    calls = []
    def respond(request):
        calls.append(request.headers["authorization"])
        status = 403 if len(calls) == 1 else second_status
        return httpx.Response(status, json={"reason": "ExpiredProviderToken"})
    result = apns.send(config, registered(registry), PAYLOAD, transport=httpx.MockTransport(respond))
    assert len(calls) == 2
    assert calls[0] == f"bearer {original}" and calls[0] != calls[1]
    assert result.status == second_status
    assert apns.provider_token(config) == calls[1].removeprefix("bearer ")


def test_fresh_expired_provider_does_not_churn_across_devices(signing, registry):
    config, _ = signing
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(403, json={"reason": "ExpiredProviderToken"})
    for _ in range(2):
        result = apns.send(config, registered(registry), PAYLOAD, transport=httpx.MockTransport(respond))
        assert result.status == 403
    assert len(requests) == 2
    assert requests[0].headers["authorization"] == requests[1].headers["authorization"]


def test_transport_error_is_recorded_without_token_or_key(signing, registry, caplog):
    config, _ = signing
    def fail(request):
        raise httpx.ConnectError(f"Cannot connect {request.url}")
    device = registered(registry)
    result = apns.send(config, device, PAYLOAD, transport=httpx.MockTransport(fail))
    with db.get_db(registry) as conn:
        devices.record_result(conn, device["id"], result)
        assert devices.devices_for_user(conn, "alice")[0]["last_error"] == "0 ConnectError"
    assert TOKEN not in caplog.text
    assert "PRIVATE KEY" not in caplog.text


def test_disabled_missing_key_invalid_token_and_oversize_never_send(signing, registry):
    config, _ = signing
    device = registered(registry)
    def unexpected(request):
        pytest.fail("request should not reach the transport")
    transport = httpx.MockTransport(unexpected)
    result = apns.send(config, device, {"aps": {"alert": "x" * 4096}}, transport=transport)
    assert result.reason == "PayloadTooLarge"
    assert apns.send(config, {**device, "token": "not/hex"}, PAYLOAD, transport=transport).reason == "ValueError"
    config.enabled = False
    assert apns.send(config, device, PAYLOAD, transport=transport).reason == "PushDisabled"
    config.enabled = True
    config.apns_key_path += ".missing"
    assert apns.send(config, device, PAYLOAD, transport=transport).reason == "ProviderTokenUnavailable"


def test_doctor_checks_metadata_without_reading_key(signing, monkeypatch):
    push, _ = signing
    config = Config(push=push)
    def forbidden(*args, **kwargs):
        pytest.fail("doctor must not read the key")
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    results = doctor.run_checks(config, only=("push.credentials",), probe=False)
    assert len(results) == 1 and results[0].status == doctor.OK
    Path(push.apns_key_path).chmod(0o644)
    assert doctor.check_push_credentials(config, False).status == doctor.FAIL
    push.apns_key_path += ".missing"
    assert doctor.check_push_credentials(config, False).status == doctor.FAIL
    push.enabled = False
    assert doctor.check_push_credentials(config, False).status == doctor.SKIP


def test_key_path_environment_override_uses_shared_loader(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[push]\napns_key_path = "/example/from-file.p8"\n')
    monkeypatch.setenv("ISTOTA_APNS_KEY_PATH", "/example/from-env.p8")
    assert load_config(path, apply_db_overlays=False).push.apns_key_path == "/example/from-env.p8"


def test_cli_parses_and_sends_each_device_through_real_http_seam(signing, registry, monkeypatch, capsys):
    from istota import cli

    push, _ = signing
    config = Config(push=push, db_path=registry, users={"alice": UserConfig()})
    with db.get_db(registry) as conn:
        devices.register_device(conn, "alice", "cd" * 32, "production", auth_method="nextcloud", auth_epoch=0)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200 if len(calls) == 1 else 429, json={"reason": "TooManyRequests"})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(apns, "_client", lambda: client)
        monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: config)
        monkeypatch.setattr(cli, "setup_logging", lambda *args, **kwargs: None)
        monkeypatch.setattr("sys.argv", ["istota", "push", "test", "--user", "alice", "--message", "Hello phone"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 1
    assert len(calls) == 2 and json.loads(calls[0].content)["aps"]["alert"]["body"] == "Hello phone"
    assert json.loads(calls[0].content)["aps"]["badge"] == 0
    output = capsys.readouterr().out
    assert "accepted by APNs" in output and "429 TooManyRequests" in output
    assert TOKEN not in output and "cd" * 32 not in output
    with db.get_db(registry) as conn:
        rows = devices.devices_for_user(conn, "alice")
        assert rows[0]["last_seen_at"] and rows[1]["last_error"] == "429 TooManyRequests"


def test_cli_refuses_disabled_and_unregistered(signing, registry, monkeypatch, capsys):
    from istota import cli

    push, _ = signing
    config = Config(push=push, db_path=registry)
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: config)
    args = SimpleNamespace(config=None, user="nobody", message="Test")
    assert cli.cmd_push_test(args) == 1
    assert "No push devices" in capsys.readouterr().err
    push.enabled = False
    assert cli.cmd_push_test(args) == 1
    assert "disabled" in capsys.readouterr().err


def test_missing_http2_is_reported_without_a_network_request(signing, registry, monkeypatch):
    push, _ = signing
    def missing():
        raise ImportError("h2 is not installed")
    monkeypatch.setattr(apns, "_client", missing)
    result = apns.send(push, registered(registry), PAYLOAD)
    assert result.reason == "ImportError"
    original = doctor.importlib.util.find_spec
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: None if name == "h2" else original(name))
    result = doctor.check_push_credentials(Config(push=push), False)
    assert result.status == doctor.FAIL and "push extra" in result.remedy


def test_unknown_response_cannot_log_or_store_arbitrary_body(signing, registry, caplog):
    config, _ = signing
    result = apns.send(config, registered(registry), PAYLOAD, transport=httpx.MockTransport(lambda request: httpx.Response(500, json={"reason": TOKEN})))
    assert result.reason == "UnknownResponse"
    assert TOKEN not in caplog.text


def test_client_is_lazy_reused_and_requests_http2(monkeypatch):
    calls = []
    sentinel = SimpleNamespace(is_closed=False)
    def create(**kwargs):
        calls.append(kwargs)
        return sentinel
    monkeypatch.setattr(apns, "_http_client", None)
    monkeypatch.setattr(apns.httpx, "Client", create)
    assert apns._client() is sentinel
    assert apns._client() is sentinel
    assert calls == [{"http2": True, "timeout": 10.0}]
