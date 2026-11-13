"""Push registration uses the existing session and origin guards."""

import base64
import hashlib
import json

import pytest

from istota import db
from tests.test_web_notifications import (  # noqa: F401
    ORIGIN, _db_path, _login, client,
)


async def test_count_includes_chat_unread(client):  # noqa: F811
    cookies = await _login(client, "alice")
    with db.get_db(_db_path()) as conn:
        room = db.register_room(conn, None, "alice", origin="web", name="Work")
        db.set_room_read_state(conn, room.token, "web", 0, "alice")
        db.add_message(conn, room.token, role="assistant", body="Reply", origin_surface="web")
    response = await client.get("/istota/api/notifications/count", cookies=cookies)
    assert response.status_code == 200
    assert response.json()["chat_unread"] == 1


TOKEN = "ab" * 32
BODY = {"token": TOKEN, "environment": "sandbox", "platform": "ios", "app_version": "0.11.0"}
URL = "/istota/api/push/devices"


@pytest.mark.parametrize("method,path", [("get", URL), ("post", URL), ("delete", URL + "/1")])
async def test_device_routes_require_auth(client, method, path):  # noqa: F811
    response = await getattr(client, method)(path)
    assert response.status_code == 401


@pytest.mark.parametrize("origin", [None, "https://other.example.com"])
async def test_mutations_require_origin(client, origin):  # noqa: F811
    cookies = await _login(client, "alice")
    headers = {"origin": origin} if origin else {}
    assert (await client.post(URL, cookies=cookies, headers=headers, json=BODY)).status_code == 403
    assert (await client.delete(URL + "/1", cookies=cookies, headers=headers)).status_code == 403


async def test_register_list_move_and_remove(client):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=BODY)
    assert response.status_code == 200
    device_id = response.json()["id"]
    listed = (await client.get(URL, cookies=cookies)).json()["devices"]
    assert len(listed) == 1
    assert listed[0]["token_tail"] == TOKEN[-6:]
    assert TOKEN not in str(listed)
    other = await _login(client, "bob")
    assert (await client.get(URL, cookies=other)).json() == {"devices": []}
    assert (await client.delete(f"{URL}/{device_id}", cookies=other, headers=ORIGIN)).status_code == 404
    moved = await client.post(URL, cookies=other, headers=ORIGIN, json=BODY)
    assert moved.json() == {"id": device_id}
    assert (await client.get(URL, cookies=cookies)).json() == {"devices": []}
    removed = await client.delete(f"{URL}/{device_id}", cookies=other, headers=ORIGIN)
    assert removed.json() == {"ok": True}


@pytest.mark.parametrize("patch", [
    {"token": "aa"}, {"token": "AB" * 32}, {"token": "ab" * 101},
    {"token": None}, {"environment": "development"}, {"environment": []},
    {"platform": "android"}, {"app_version": []}, {"user_id": "bob"},
])
async def test_invalid_registration_is_400(client, patch):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json={**BODY, **patch})
    assert response.status_code == 400


async def test_chat_off_preserves_ordinary_chat_count(client):  # noqa: F811
    from istota import user_profiles

    cookies = await _login(client, "alice")
    user_profiles.ensure_profile(_db_path(), "alice")
    user_profiles.update_profile(_db_path(), "alice", push_chat_alerts="off")
    with db.get_db(_db_path()) as conn:
        room = db.register_room(conn, None, "alice", origin="web", name="Work")
        db.add_message(conn, room.token, role="assistant", body="Reply", origin_surface="web")
    result = (await client.get("/istota/api/notifications/count", cookies=cookies)).json()
    assert result["chat_unread"] == 1
    assert result["push_badge"] == 0


async def test_bad_and_oversized_bodies(client):  # noqa: F811
    cookies = await _login(client, "alice")
    assert (await client.post(URL, cookies=cookies, headers=ORIGIN, content=b"not JSON")).status_code == 400
    assert (await client.post(URL, cookies=cookies, headers=ORIGIN, json=[])).status_code == 400
    assert (await client.post(URL, cookies=cookies, headers=ORIGIN, json={**BODY, "app_version": "x" * 5000})).status_code == 413


async def test_failed_registration_is_not_reported_as_success(client, monkeypatch):  # noqa: F811
    from istota.push import devices

    cookies = await _login(client, "alice")
    monkeypatch.setattr(devices, "register_device", lambda *args, **kwargs: None)
    assert (await client.post(URL, cookies=cookies, headers=ORIGIN, json=BODY)).status_code == 503


async def test_logout_removes_only_session_registration(client):  # noqa: F811
    cookies = await _login(client, "alice")
    first = await client.post(URL, cookies=cookies, headers=ORIGIN, json=BODY)
    session = first.cookies
    other_login = await _login(client, "alice")
    second = await client.post(URL, cookies=other_login, headers=ORIGIN, json={**BODY, "token": "cd" * 32})
    response = await client.get("/istota/logout", cookies=session, follow_redirects=False)
    assert response.status_code == 302
    with db.get_db(_db_path()) as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM push_devices")] == [second.json()["id"]]


@pytest.mark.parametrize("new_user", ["alice", "bob"])
async def test_old_session_logout_cannot_remove_new_registration(client, new_user):  # noqa: F811
    cookies = await _login(client, "alice")
    first = await client.post(URL, cookies=cookies, headers=ORIGIN, json=BODY)
    session = first.cookies
    cookies = await _login(client, new_user)
    second = await client.post(URL, cookies=cookies, headers=ORIGIN, json=BODY)
    assert second.json() == first.json()
    await client.get("/istota/logout", cookies=session, follow_redirects=False)
    with db.get_db(_db_path()) as conn:
        assert conn.execute("SELECT user_id FROM push_devices").fetchone()[0] == new_user
    await client.get("/istota/logout", cookies=second.cookies, follow_redirects=False)
    with db.get_db(_db_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM push_devices").fetchone()[0] == 0


async def test_logout_after_older_poll_cookie_removes_enrollment(client, monkeypatch):  # noqa: F811
    import asyncio
    import threading
    from istota.webui import app as web

    cookies = await _login(client, "alice")
    entered, release = threading.Event(), threading.Event()
    original = web._notification_counts

    def held_count(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release count request"
        return original(*args, **kwargs)

    monkeypatch.setattr(web, "_notification_counts", held_count)
    poll = asyncio.create_task(client.get("/istota/api/notifications/count", cookies=cookies))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        registered = await client.post(URL, cookies=cookies, headers=ORIGIN, json=BODY)
        assert registered.status_code == 200
    finally:
        release.set()
    older_response = await poll
    assert older_response.status_code == 200
    response = await client.get("/istota/logout", cookies=older_response.cookies, follow_redirects=False)
    assert response.status_code == 302
    with db.get_db(_db_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM push_devices").fetchone()[0] == 0


async def test_legacy_login_requires_fresh_sign_in_before_enrollment(client):  # noqa: F811
    from istota.webui import app as web
    from tests.test_web_auth_routes import set_session

    set_session(client, web, {"user": {"username": "alice"}, "auth": {"method": "nextcloud", "epoch": 0}})
    response = await client.post(URL, headers=ORIGIN, json=BODY)
    assert response.status_code == 409
    assert "Sign out and sign in again" in response.json()["detail"]
    with db.get_db(_db_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM push_devices").fetchone()[0] == 0


BINDING = "0f8e4b0c-2a6d-4c4e-9a43-6d2f1b8e7c51"
BOUND = {**BODY, "app_version": "0.12.0", "connection_binding": BINDING, "connection_generation": 3}


def _binding_row():
    with db.get_db(_db_path()) as conn:
        return tuple(conn.execute("SELECT connection_binding, connection_generation FROM push_devices").fetchone())


async def test_generic_shell_binding_is_stored_and_legacy_reregistration_clears_it(client):  # noqa: F811
    cookies = await _login(client, "alice")
    bound = await client.post(URL, cookies=cookies, headers=ORIGIN, json=BOUND)
    assert bound.status_code == 200
    assert _binding_row() == (BINDING, 3)
    # The same token from an older binary is a legacy enrollment again, not a bound one.
    legacy = await client.post(URL, cookies=cookies, headers=ORIGIN, json=BODY)
    assert legacy.json() == bound.json()
    assert _binding_row() == (None, None)
    again = await client.post(URL, cookies=cookies, headers=ORIGIN, json={**BOUND, "connection_generation": 4})
    assert again.json() == bound.json()
    assert _binding_row() == (BINDING, 4)
    listed, = (await client.get(URL, cookies=cookies)).json()["devices"]
    assert "connection_binding" not in listed


@pytest.mark.parametrize("patch", [
    {"connection_generation": None}, {"connection_binding": None},
    {"connection_binding": BINDING.upper()}, {"connection_binding": "not-a-uuid"},
    {"connection_binding": "{" + BINDING + "}"}, {"connection_binding": 7},
    {"connection_generation": 0}, {"connection_generation": -1}, {"connection_generation": True},
    {"connection_generation": 1.5}, {"connection_generation": "3"}, {"connection_generation": 2**53},
])
async def test_invalid_binding_is_400(client, patch):  # noqa: F811
    cookies = await _login(client, "alice")
    body = {key: value for key, value in {**BOUND, **patch}.items() if value is not None}
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=body)
    assert response.status_code == 400
    with db.get_db(_db_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM push_devices").fetchone()[0] == 0


# --- Private enrollment ---------------------------------------------------------

SENDER_URL = "/istota/api/push/sender-key"
DEVICE_KEY = bytes(range(32))
_OTHER_KEY = bytes(range(1, 33))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _kid(raw: bytes) -> str:
    return _b64(hashlib.sha256(raw).digest())


GENERATION = _b64(b"\x01" * 16)


@pytest.fixture
def private_push(tmp_path):
    from istota.push import envelope
    import istota.webui.app as mod

    key_path = tmp_path / "push" / "signing.key"
    signing_key = envelope.write_signing_key(key_path)
    raw = signing_key.public_key().public_bytes_raw()
    mod._config.push.content = "private"
    mod._config.push.signing_key_path = str(key_path)
    return {"path": key_path, "raw": raw, "kid": _kid(raw)}


def _encrypted(sender_kid, **patch):
    private = {"version": 1, "content": "encrypted", "encryption_public_key": _b64(DEVICE_KEY),
               "encryption_key_id": _kid(DEVICE_KEY), "sender_key_id": sender_kid, "generation": GENERATION}
    return {**BOUND, "private": {**private, **patch}}


def _private_columns():
    with db.get_db(_db_path()) as conn:
        return tuple(conn.execute(
            "SELECT private_version, private_content, encryption_public_key, encryption_key_id, sender_key_id,"
            " push_generation FROM push_devices").fetchone())


def _device_count():
    with db.get_db(_db_path()) as conn:
        return conn.execute("SELECT COUNT(*) FROM push_devices").fetchone()[0]


async def test_sender_key_requires_auth(client):  # noqa: F811
    assert (await client.get(SENDER_URL)).status_code == 401


async def test_sender_key_is_not_offered_on_a_legacy_install(client):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.get(SENDER_URL, cookies=cookies)
    assert response.status_code == 404
    assert response.json()["detail"] == "private push not offered"
    assert response.headers["cache-control"] == "no-store"


async def test_sender_key_serves_the_public_half(client, private_push):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.get(SENDER_URL, cookies=cookies)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"version": 1, "key_id": private_push["kid"], "public_key": _b64(private_push["raw"])}


@pytest.mark.parametrize("damage", ["missing", "malformed"])
async def test_sender_key_file_problems_are_503(client, private_push, damage):  # noqa: F811
    from istota.push import envelope

    pub = envelope.public_key_path(private_push["path"])
    if damage == "missing":
        pub.unlink()
    else:
        pub.write_text("not a key\n")
    cookies = await _login(client, "alice")
    response = await client.get(SENDER_URL, cookies=cookies)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


async def test_encrypted_enrollment_is_stored_listed_and_cleared(client, private_push):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_encrypted(private_push["kid"]))
    assert response.status_code == 200
    assert _private_columns() == (1, "encrypted", _b64(DEVICE_KEY), _kid(DEVICE_KEY), private_push["kid"], GENERATION)
    listed, = (await client.get(URL, cookies=cookies)).json()["devices"]
    assert (listed["content"], listed["stale_key"]) == ("encrypted", False)
    assert _b64(DEVICE_KEY) not in str(listed) and GENERATION not in str(listed)
    plain = await client.post(URL, cookies=cookies, headers=ORIGIN, json=BOUND)
    assert plain.json() == response.json()
    assert _private_columns() == (None,) * 6
    listed, = (await client.get(URL, cookies=cookies)).json()["devices"]
    assert (listed["content"], listed["stale_key"]) == ("generic", False)


async def test_generic_enrollment_is_stored(client, private_push):  # noqa: F811
    cookies = await _login(client, "alice")
    body = {**BOUND, "private": {"version": 1, "content": "generic", "generation": GENERATION}}
    assert (await client.post(URL, cookies=cookies, headers=ORIGIN, json=body)).status_code == 200
    assert _private_columns() == (1, "generic", None, None, None, GENERATION)
    listed, = (await client.get(URL, cookies=cookies)).json()["devices"]
    assert (listed["content"], listed["stale_key"]) == ("generic", False)


async def test_rotated_key_marks_the_device_stale(client, private_push):  # noqa: F811
    from istota.push import envelope

    cookies = await _login(client, "alice")
    assert (await client.post(URL, cookies=cookies, headers=ORIGIN,
                              json=_encrypted(private_push["kid"]))).status_code == 200
    envelope.public_key_path(private_push["path"]).unlink()
    private_push["path"].unlink()
    envelope.write_signing_key(private_push["path"])
    listed, = (await client.get(URL, cookies=cookies)).json()["devices"]
    assert (listed["content"], listed["stale_key"]) == ("generic", True)


async def test_legacy_list_reports_legacy_content(client):  # noqa: F811
    cookies = await _login(client, "alice")
    await client.post(URL, cookies=cookies, headers=ORIGIN, json=BODY)
    listed, = (await client.get(URL, cookies=cookies)).json()["devices"]
    assert (listed["content"], listed["stale_key"]) == ("legacy", False)


async def test_changed_sender_key_is_409(client, private_push):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_encrypted(_kid(_OTHER_KEY)))
    assert response.status_code == 409
    assert response.json()["detail"] == "sender key changed"
    assert _device_count() == 0


async def test_private_field_on_a_legacy_install_is_409(client):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_encrypted(_kid(_OTHER_KEY)))
    assert response.status_code == 409
    assert response.json()["detail"] == "private push not offered"
    assert _device_count() == 0


async def test_private_enrollment_with_unreadable_sender_key_is_503(client, private_push):  # noqa: F811
    from istota.push import envelope

    envelope.public_key_path(private_push["path"]).unlink()
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_encrypted(private_push["kid"]))
    assert response.status_code == 503
    assert _device_count() == 0


async def test_private_enrollment_requires_a_binding(client, private_push):  # noqa: F811
    cookies = await _login(client, "alice")
    body = {**BODY, "private": _encrypted(private_push["kid"])["private"]}
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=body)
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid private enrollment"


@pytest.mark.parametrize("patch", [
    {"version": 2}, {"version": True}, {"version": "1"},
    {"content": "plain"}, {"content": None},
    {"extra": 1},
    {"encryption_public_key": _b64(DEVICE_KEY[:31])}, {"encryption_public_key": _b64(DEVICE_KEY) + "A"},
    {"encryption_public_key": _b64(DEVICE_KEY) + "="}, {"encryption_public_key": 7},
    {"encryption_key_id": _kid(_OTHER_KEY)}, {"encryption_key_id": "x"},
    {"sender_key_id": 5}, {"sender_key_id": "short"},
    {"generation": "a" * 21}, {"generation": "a" * 23}, {"generation": "a" * 21 + "+"},
    {"generation": 12},
])
async def test_invalid_encrypted_enrollment_is_400(client, private_push, patch):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=_encrypted(private_push["kid"], **patch))
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid private enrollment"
    assert _device_count() == 0


@pytest.mark.parametrize("private", [
    None, [], "encrypted", {},
    {"version": 1, "content": "generic"},
    {"version": 1, "content": "generic", "generation": GENERATION, "encryption_public_key": _b64(DEVICE_KEY)},
    {"version": 1, "content": "encrypted", "generation": GENERATION},
])
async def test_invalid_private_shapes_are_400(client, private_push, private):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json={**BOUND, "private": private})
    assert response.status_code == 400
    assert _device_count() == 0


async def test_largest_valid_registration_fits_the_body_bound(client, private_push):  # noqa: F811
    cookies = await _login(client, "alice")
    body = {**_encrypted(private_push["kid"]), "token": "ab" * 100, "app_version": "x" * 100,
            "connection_generation": 2**53 - 1}
    assert len(json.dumps(body).encode()) < 4096
    assert (await client.post(URL, cookies=cookies, headers=ORIGIN, json=body)).status_code == 200


async def test_an_unusable_device_key_is_refused_at_enrollment(client, private_push):  # noqa: F811
    zero = bytes(32)
    cookies = await _login(client, "alice")
    body = _encrypted(private_push["kid"], encryption_public_key=_b64(zero), encryption_key_id=_kid(zero))
    response = await client.post(URL, cookies=cookies, headers=ORIGIN, json=body)
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid private enrollment"
    assert _device_count() == 0
