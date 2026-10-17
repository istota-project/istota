"""Push registration uses the existing session and origin guards."""

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
