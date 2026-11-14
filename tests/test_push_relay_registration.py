"""Relay registration: the route on the row, the send capability in a 0600 file and nowhere else."""

import logging
import stat
from pathlib import Path

import pytest

from istota import db
from istota.config import PushConfig
from tests.test_push_routes import (  # noqa: F401
    BOUND, DEVICE_KEY, GENERATION, URL, _b64, _device_count, _encrypted, _kid, private_push,
)
from tests.test_settings_snapshot import install, invoke  # noqa: F401
from tests.test_web_notifications import (  # noqa: F401
    ORIGIN, _db_path, _login, client,
)

RELAY_URL = "https://relay.example.com/istota"
ROUTE_ID = _b64(b"\x11" * 16)
CAPABILITY = _b64(b"\x5a" * 32)
LEASE = 1_900_000_000
RELAY = {"route_id": ROUTE_ID, "send_capability": CAPABILITY, "lease_expires_at": LEASE}


@pytest.fixture
def relay_push(private_push):  # noqa: F811
    import istota.webui.app as mod
    from istota.push import envelope

    # The signing key in a config directory of its own, as on the stack, so a
    # file written beside it rather than beside the database is told apart.
    key_path = mod._config.db_path.parent / "config" / "push" / "signing.key"
    raw = envelope.write_signing_key(key_path).public_key().public_bytes_raw()
    mod._config.push.signing_key_path = str(key_path)
    mod._config.push.enabled = True
    mod._config.push.delivery = "relay"
    mod._config.push.relay_url = RELAY_URL
    return {"path": key_path, "raw": raw, "kid": _kid(raw),
            "dir": mod._config.db_path.parent / "push" / "relay"}


def _relay_body(kid, **relay_patch):
    body = _encrypted(kid)
    body["private"]["relay"] = {**RELAY, **relay_patch}
    return body


def _relay_columns():
    with db.get_db(_db_path()) as conn:
        return tuple(conn.execute(
            "SELECT relay_route_id, relay_credential_ref, relay_lease_expires_at, enrollment_state FROM push_devices"
        ).fetchone())


def _database_text() -> str:
    with db.get_db(_db_path()) as conn:
        return "\n".join(conn.iterdump())


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


async def test_relay_registration_stores_the_route_and_a_private_capability_file(client, relay_push, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_relay_body(relay_push["kid"]))
    assert response.status_code == 200
    device_id = response.json()["id"]
    ref = f"{device_id}.cap"
    assert _relay_columns() == (ROUTE_ID, ref, LEASE, "active")
    cap = relay_push["dir"] / ref
    assert cap.read_text("ascii") == CAPABILITY
    assert not (relay_push["path"].parent / "relay").exists()
    assert _mode(cap) == 0o600
    assert _mode(relay_push["dir"]) == 0o700
    listed = (await client.get(URL, cookies=cookies)).json()["devices"]
    assert listed[0]["content"] == "encrypted"
    assert listed[0]["enrollment_state"] == "active"
    for secret in (CAPABILITY, ROUTE_ID, ref):
        assert secret not in str(listed)
    assert CAPABILITY not in _database_text()
    assert CAPABILITY not in caplog.text


async def test_re_registration_replaces_the_capability_in_place(client, relay_push):  # noqa: F811
    cookies = await _login(client, "alice")
    first = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_relay_body(relay_push["kid"]))
    newer = _b64(b"\x77" * 32)
    second = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_relay_body(
        relay_push["kid"], send_capability=newer, route_id=_b64(b"\x22" * 16), lease_expires_at=LEASE + 1))
    assert second.json() == first.json()
    ref = f"{first.json()['id']}.cap"
    assert _relay_columns() == (_b64(b"\x22" * 16), ref, LEASE + 1, "active")
    # The replaced route's capability moves aside for its revocation (test_push_relay_revocation.py).
    revoke = f"revoke-{ROUTE_ID}.cap"
    assert sorted(p.name for p in relay_push["dir"].iterdir()) == sorted([ref, revoke])
    assert (relay_push["dir"] / ref).read_text("ascii") == newer
    assert (relay_push["dir"] / revoke).read_text("ascii") == CAPABILITY


async def test_a_registration_without_a_route_clears_it_and_queues_its_revocation(client, relay_push):  # noqa: F811
    import istota.webui.app as mod

    cookies = await _login(client, "alice")
    first = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_relay_body(relay_push["kid"]))
    assert first.status_code == 200
    # Under direct delivery the relay URL may be empty: the row's own is the one queued.
    mod._config.push.delivery = "direct"
    mod._config.push.relay_url = ""
    second = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_encrypted(relay_push["kid"]))
    assert second.json() == first.json()
    assert _relay_columns() == (None, None, None, "active")
    assert [p.name for p in relay_push["dir"].iterdir()] == [f"revoke-{ROUTE_ID}.cap"]
    with db.get_db(_db_path()) as conn:
        queued = conn.execute("SELECT route_id, relay_url FROM push_relay_revocations").fetchall()
    assert [tuple(r) for r in queued] == [(ROUTE_ID, RELAY_URL)]


async def test_an_explicit_credentials_dir_is_used(client, relay_push, tmp_path):  # noqa: F811
    import istota.webui.app as mod

    explicit = tmp_path / "elsewhere" / "caps"
    mod._config.push.push_credentials_dir = str(explicit)
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_relay_body(relay_push["kid"]))
    assert response.status_code == 200
    assert (explicit / f"{response.json()['id']}.cap").read_text("ascii") == CAPABILITY
    assert _mode(explicit) == 0o700
    assert not relay_push["dir"].exists()


async def test_a_capability_that_cannot_be_written_registers_nothing(client, relay_push, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    relay_push["dir"].write_text("not a directory")
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_relay_body(relay_push["kid"]))
    assert response.status_code == 503
    assert _device_count() == 0
    assert CAPABILITY not in caplog.text


async def test_encrypted_without_a_route_is_400_under_relay(client, relay_push):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_encrypted(relay_push["kid"]))
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid private enrollment"
    assert _device_count() == 0


async def test_a_route_under_direct_delivery_is_400(client, private_push):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_relay_body(private_push["kid"]))
    assert response.status_code == 400
    assert _device_count() == 0
    assert not (_db_path().parent / "push" / "relay").exists()


async def test_generic_is_not_offered_under_relay(client, relay_push):  # noqa: F811
    cookies = await _login(client, "alice")
    body = {**BOUND, "private": {"version": 1, "content": "generic", "generation": GENERATION}}
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=body)
    assert response.status_code == 409
    assert response.json()["detail"] == "private push not offered"
    assert _device_count() == 0


@pytest.mark.parametrize("body", [BOUND, {k: v for k, v in BOUND.items() if not k.startswith("connection_")}])
async def test_a_plain_registration_is_refused_under_relay(client, relay_push, body):  # noqa: F811
    # The relay forwards only sealed envelopes to a route; a plain row would
    # never receive anything, so an older shell is told rather than enrolled.
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=body)
    assert response.status_code == 409
    assert response.json()["detail"] == "private push not offered"
    assert _device_count() == 0


@pytest.mark.parametrize("patch", [
    {"route_id": _b64(b"\x11" * 15)}, {"route_id": _b64(b"\x11" * 17)}, {"route_id": ROUTE_ID + "="},
    {"route_id": "A" * 21 + "B"}, {"route_id": 5}, {"route_id": None},
    {"send_capability": _b64(b"\x5a" * 31)}, {"send_capability": _b64(b"\x5a" * 33)},
    {"send_capability": CAPABILITY[:-1] + "/"}, {"send_capability": ["x"]},
    {"lease_expires_at": True}, {"lease_expires_at": "1900000000"}, {"lease_expires_at": 1.9e9},
    {"lease_expires_at": 0}, {"lease_expires_at": -1}, {"lease_expires_at": 2**53},
    {"extra": 1},
])
async def test_invalid_relay_fields_are_400(client, relay_push, patch):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_relay_body(relay_push["kid"], **patch))
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid private enrollment"
    assert _device_count() == 0
    assert not relay_push["dir"].exists()


@pytest.mark.parametrize("relay", [None, [], "route", {}, {"route_id": ROUTE_ID, "send_capability": CAPABILITY}])
async def test_invalid_relay_shapes_are_400(client, relay_push, relay):  # noqa: F811
    cookies = await _login(client, "alice")
    body = _encrypted(relay_push["kid"])
    body["private"]["relay"] = relay
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=body)
    assert response.status_code == 400
    assert _device_count() == 0


async def test_largest_relay_registration_fits_the_body_bound(client, relay_push):  # noqa: F811
    import json

    cookies = await _login(client, "alice")
    body = {**_relay_body(relay_push["kid"], lease_expires_at=2**53 - 1), "token": "ab" * 100,
            "app_version": "x" * 100, "connection_generation": 2**53 - 1}
    assert len(json.dumps(body).encode()) < 4096
    assert (await client.post(URL, cookies=cookies, headers=ORIGIN, json=body)).status_code == 200


# --- The credentials directory ---------------------------------------------------


def test_credentials_dir_defaults_under_the_state_root_not_the_config_dir():
    # The stack mounts /data/config read-only for every running service, and the
    # web process writes the capability at registration, so the default is
    # beside the framework database, which the sandbox already masks.
    from istota.config import Config
    from istota.push import devices

    config = Config(db_path=Path("/data/istota.db"),
                    push=PushConfig(signing_key_path="/data/config/push/signing.key"))
    directory = devices.relay_credentials_dir(config)
    assert directory == Path("/data/push/relay")
    assert directory.is_relative_to(config.db_path.parent)
    assert not directory.is_relative_to(Path(config.push.signing_key_path).parent)
    assert not directory.is_relative_to(Path("/data/config"))
    config.push.push_credentials_dir = "/srv/caps"
    assert devices.relay_credentials_dir(config) == Path("/srv/caps")


def test_push_credentials_dir_loads_from_toml(tmp_path):
    from istota.config import load_config

    path = tmp_path / "config.toml"
    path.write_text('[push]\npush_credentials_dir = "/srv/caps"\n')
    assert load_config(path, apply_db_overlays=False).push.push_credentials_dir == "/srv/caps"


# --- Nothing else carries the capability -----------------------------------------


def test_snapshot_and_config_view_never_carry_the_capability(install, tmp_path, monkeypatch, capsys):  # noqa: F811
    from istota.push import devices, envelope
    from istota.webui import admin_config_view

    path, config = install
    key = tmp_path / "push" / "signing.key"
    envelope.write_signing_key(key)
    config.push = PushConfig(enabled=True, content="private", delivery="relay", relay_url=RELAY_URL,
                             signing_key_path=str(key))
    with db.get_db(config.db_path) as conn:
        device_id = devices.register_device(
            conn, "alice", "ab" * 32, "sandbox", private_version=1, private_content="encrypted",
            encryption_public_key=_b64(DEVICE_KEY), encryption_key_id=_kid(DEVICE_KEY),
            sender_key_id=_kid(DEVICE_KEY), push_generation=GENERATION,
            relay=RELAY, credentials_dir=devices.relay_credentials_dir(config),
        )
    assert device_id is not None
    assert (config.db_path.parent / "push" / "relay" / f"{device_id}.cap").read_text("ascii") == CAPABILITY
    code, stdout, stderr = invoke(monkeypatch, capsys, path, "snapshot")
    assert code == 0, stderr
    assert CAPABILITY not in stdout and CAPABILITY not in stderr
    assert CAPABILITY not in str(admin_config_view.build_config_view(config, secrets_dir=tmp_path / "none"))


def _queued_routes():
    with db.get_db(_db_path()) as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT route_id, credential_ref, relay_url FROM push_relay_revocations")]


async def test_removing_a_relay_device_queues_its_route_and_keeps_the_file(client, relay_push):  # noqa: F811
    cookies = await _login(client, "alice")
    device_id = (await client.post(URL, cookies=cookies, headers=ORIGIN, json=_relay_body(relay_push["kid"]))).json()["id"]
    removed = await client.delete(f"{URL}/{device_id}", cookies=cookies, headers=ORIGIN)
    assert removed.status_code == 200
    assert _device_count() == 0
    assert _queued_routes() == [(ROUTE_ID, f"{device_id}.cap", RELAY_URL)]
    assert (relay_push["dir"] / f"{device_id}.cap").read_text("ascii") == CAPABILITY


async def test_logout_queues_the_session_relay_route(client, relay_push):  # noqa: F811
    cookies = await _login(client, "alice")
    first = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_relay_body(relay_push["kid"]))
    response = await client.get("/istota/logout", cookies=first.cookies, follow_redirects=False)
    assert response.status_code == 302
    assert _device_count() == 0
    assert _queued_routes() == [(ROUTE_ID, f"{first.json()['id']}.cap", RELAY_URL)]


async def test_removal_after_a_switch_to_direct_queues_the_issuing_relay(client, relay_push):  # noqa: F811
    import istota.webui.app as mod

    cookies = await _login(client, "alice")
    device_id = (await client.post(URL, cookies=cookies, headers=ORIGIN, json=_relay_body(relay_push["kid"]))).json()["id"]
    mod._config.push.delivery = "direct"
    mod._config.push.relay_url = ""
    removed = await client.delete(f"{URL}/{device_id}", cookies=cookies, headers=ORIGIN)
    assert removed.status_code == 200
    assert _queued_routes() == [(ROUTE_ID, f"{device_id}.cap", RELAY_URL)]


async def test_a_token_replacement_through_the_route_keeps_one_row(client, relay_push):  # noqa: F811
    cookies = await _login(client, "alice")
    first = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_relay_body(relay_push["kid"]))
    body = _relay_body(relay_push["kid"])
    body["token"] = "ef" * 32
    second = await client.post(URL, cookies=cookies, headers=ORIGIN, json=body)
    assert second.status_code == 200 and second.json() != first.json()
    assert _device_count() == 1
    assert _queued_routes() == []
    assert [p.name for p in relay_push["dir"].iterdir()] == [f"{second.json()['id']}.cap"]


async def test_a_stale_sender_key_lists_as_repair_under_relay_delivery(client, relay_push):  # noqa: F811
    cookies = await _login(client, "alice")
    await client.post(URL, cookies=cookies, headers=ORIGIN, json=_relay_body(relay_push["kid"]))
    with db.get_db(_db_path()) as conn:
        conn.execute("UPDATE push_devices SET sender_key_id = ?", ("A" * 43,))
    listed, = (await client.get(URL, cookies=cookies)).json()["devices"]
    assert (listed["stale_key"], listed["enrollment_state"]) == (True, "repair")
    # An unreadable key is an outage, not a stale enrollment: nothing to repair.
    Path(str(relay_push["path"]) + ".pub").unlink()
    listed, = (await client.get(URL, cookies=cookies)).json()["devices"]
    assert (listed["stale_key"], listed["enrollment_state"]) == (False, "active")


async def test_a_device_left_in_repair_lists_as_active_under_direct_delivery(client, relay_push):  # noqa: F811
    import istota.webui.app as mod

    cookies = await _login(client, "alice")
    await client.post(URL, cookies=cookies, headers=ORIGIN, json=_relay_body(relay_push["kid"]))
    with db.get_db(_db_path()) as conn:
        conn.execute("UPDATE push_devices SET enrollment_state = 'repair'")
    assert (await client.get(URL, cookies=cookies)).json()["devices"][0]["enrollment_state"] == "repair"
    mod._config.push.delivery = "direct"
    assert (await client.get(URL, cookies=cookies)).json()["devices"][0]["enrollment_state"] == "active"


async def test_a_registration_with_push_disabled_on_a_relay_installation_says_so(client, relay_push):  # noqa: F811
    import istota.webui.app as mod

    mod._config.push.enabled = False
    cookies = await _login(client, "alice")
    for body in (_relay_body(relay_push["kid"]), {"token": "ab" * 32, "environment": "production"}):
        response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=body)
        assert response.status_code == 409
        assert response.json()["detail"] == "push not enabled"
    assert _device_count() == 0
