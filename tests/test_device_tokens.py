"""Device tokens: the `/istota/api/devices` routes and the bearer on `POST /notes`.

A device token is a per-phone bearer scoped by capability. These tests drive
the real app, so the session, the origin check and the bearer dependency are
the ones a request meets in production. The route-enumeration test is the
guard against the token quietly gaining reach: exactly two routes accept it.
"""

from unittest.mock import AsyncMock

import pytest

from tests.test_web_app import _make_config, _needs_web_deps, _patch_app

pytestmark = _needs_web_deps

try:
    from httpx import ASGITransport, AsyncClient
except ImportError:  # pragma: no cover - covered by _needs_web_deps
    pass

DEVICES = "/istota/api/devices"
NOTES = "/istota/api/workspace/notes"
ORIGIN = {"origin": "https://example.com"}
DEVICE = "6f1c2b3a-4d5e-4f60-8a7b-9c0d1e2f3a4b"
FOLDER = "Notes/Inbox"


@pytest.fixture
def config(tmp_path):
    from istota import user_profiles
    from istota.config import UserConfig

    cfg = _make_config(tmp_path, users={
        "alice": UserConfig(display_name="Alice"),
        "bob": UserConfig(display_name="Bob"),
    })
    for user_id in ("alice", "bob"):
        cfg.workspace_root(user_id).mkdir(parents=True)
        user_profiles.ensure_profile(cfg.db_path, user_id)
    user_profiles.update_profile(cfg.db_path, "alice", quick_notes_folder=FOLDER)
    return cfg


@pytest.fixture
def app(config):
    return _patch_app(config)


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://example.com") as c:
        yield c


async def _login(client, user_id="alice"):
    import istota.webui.app as mod

    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": user_id})
    response = await client.get("/istota/callback", follow_redirects=False)
    client.cookies.clear()
    return response.cookies


def _bearer(token):
    return {"authorization": f"Bearer {token}"}


def _rows(config, user_id="alice"):
    from istota import db

    with db.get_db(config.db_path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM device_tokens WHERE user_id = ? ORDER BY id", (user_id,),
        )]


def _mint_direct(config, user_id="alice", device_id=DEVICE, scopes=("notes",)):
    from istota import db
    from istota.webui import device_tokens

    with db.get_db(config.db_path) as conn:
        return device_tokens.mint(
            conn, user_id, device_id, "iPhone", set(scopes), auth_method="nextcloud", auth_epoch=0,
        )


def _notes_dir(config, user_id="alice"):
    return config.workspace_root(user_id) / FOLDER


async def _mint(client, cookies, **body):
    payload = {"device_id": DEVICE, "label": "iPhone", "scopes": ["notes"], **body}
    return await client.post(DEVICES, cookies=cookies, headers=ORIGIN, json=payload)


# --- minting -----------------------------------------------------------------


async def test_mint_returns_the_token_once_and_a_remint_kills_the_old_one(client, config):
    from istota.lib.tokens import hash_token

    cookies = await _login(client)
    first = await _mint(client, cookies)
    assert first.status_code == 201
    body = first.json()
    assert set(body) == {"id", "token", "scopes"}
    assert body["scopes"] == ["notes"]
    assert len(body["token"]) == 43
    [row] = _rows(config)
    assert row["token_hash"] == hash_token(body["token"])
    assert row["device_id"] == DEVICE.upper()
    assert row["scopes"] == "notes"

    listed = await client.get(DEVICES, cookies=cookies)
    assert listed.status_code == 200
    [device] = listed.json()["devices"]
    assert set(device) == {"id", "device_id", "label", "scopes", "token_tail", "created_at", "last_used_at"}
    assert device["token_tail"] == body["token"][-4:]
    assert device["scopes"] == ["notes"]
    assert body["token"] not in listed.text
    assert row["token_hash"] not in listed.text

    second = await _mint(client, cookies, device_id=DEVICE.lower())
    assert second.status_code == 201
    assert second.json()["id"] == body["id"]
    assert len(_rows(config)) == 1

    old = await client.post(NOTES, headers=_bearer(body["token"]), json={"content": "Old token"})
    assert old.status_code == 401
    new = await client.post(NOTES, headers=_bearer(second.json()["token"]), json={"content": "New token"})
    assert new.status_code == 201


@pytest.mark.parametrize("scopes,code", [
    (["notes", "voice"], "unknown_scope"),
    ([], "invalid_scopes"),
    ("notes", "invalid_scopes"),
    ([5], "invalid_scopes"),
])
async def test_a_bad_scope_list_is_a_400(client, config, scopes, code):
    cookies = await _login(client)
    response = await _mint(client, cookies, scopes=scopes)
    assert response.status_code == 400
    assert response.json()["code"] == code
    assert _rows(config) == []


@pytest.mark.parametrize("body", [
    {"device_id": "not-a-uuid"},
    {"device_id": None},
    {"label": 7},
    {"label": "x" * 65},
    {"extra": True},
])
async def test_a_malformed_mint_body_is_a_400(client, config, body):
    cookies = await _login(client)
    response = await _mint(client, cookies, **body)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_body"
    assert _rows(config) == []


async def test_granting_notes_without_a_folder_is_a_409(client, config):
    cookies = await _login(client, "bob")
    response = await _mint(client, cookies)
    assert response.status_code == 409
    assert response.json()["code"] == "notes_folder_unset"
    assert _rows(config, "bob") == []


async def test_the_session_routes_need_a_session_and_an_origin(client, config):
    for method, path in (("get", DEVICES), ("post", DEVICES), ("delete", DEVICES + "/1")):
        response = await getattr(client, method)(path, headers=ORIGIN)
        assert response.status_code == 401, (method, path)
    cookies = await _login(client)
    assert (await client.post(DEVICES, cookies=cookies, json={})).status_code == 403
    assert (await client.delete(DEVICES + "/1", cookies=cookies)).status_code == 403
    assert _rows(config) == []


async def test_the_cap_of_twenty_holds_and_a_replace_does_not_count(client, config):
    from istota.webui import device_tokens

    for n in range(device_tokens.MAX_TOKENS_PER_USER):
        _mint_direct(config, device_id=f"00000000-0000-4000-8000-{n:012d}")
    cookies = await _login(client)
    refused = await _mint(client, cookies)
    assert refused.status_code == 409
    assert refused.json()["code"] == "too_many_devices"
    replaced = await _mint(client, cookies, device_id="00000000-0000-4000-8000-000000000003")
    assert replaced.status_code == 201
    assert len(_rows(config)) == device_tokens.MAX_TOKENS_PER_USER


# --- revoking ----------------------------------------------------------------


async def test_revoking_from_settings_makes_the_token_a_401(client, config):
    cookies = await _login(client)
    minted = (await _mint(client, cookies)).json()
    deleted = await client.delete(f"{DEVICES}/{minted['id']}", cookies=cookies, headers=ORIGIN)
    assert deleted.status_code == 200
    assert _rows(config) == []
    response = await client.post(NOTES, headers=_bearer(minted["token"]), json={"content": "Buy milk"})
    assert response.status_code == 401
    assert not _notes_dir(config).exists()


async def test_revoking_another_users_row_is_a_404(client, config):
    _, _token = _mint_direct(config)
    [row] = _rows(config)
    cookies = await _login(client, "bob")
    response = await client.delete(f"{DEVICES}/{row['id']}", cookies=cookies, headers=ORIGIN)
    assert response.status_code == 404
    assert len(_rows(config)) == 1


async def test_a_device_revokes_itself_with_the_bearer_alone(client, config):
    _, token = _mint_direct(config)
    _, other = _mint_direct(config, device_id="00000000-0000-4000-8000-000000000001")
    response = await client.delete(DEVICES + "/self", headers=_bearer(token))
    assert response.status_code == 200
    assert [row["device_id"] for row in _rows(config)] == ["00000000-0000-4000-8000-000000000001"]
    again = await client.delete(DEVICES + "/self", headers=_bearer(token))
    assert again.status_code == 401
    assert (await client.post(NOTES, headers=_bearer(other), json={"content": "Still here"})).status_code == 201


async def test_self_revocation_needs_a_device_token(client, config):
    _mint_direct(config)
    cookies = await _login(client)
    response = await client.delete(DEVICES + "/self", cookies=cookies, headers=ORIGIN)
    assert response.status_code == 401
    assert response.json()["code"] == "device_token_required"
    assert len(_rows(config)) == 1


# --- the bearer on POST /notes -------------------------------------------------


async def test_a_bearer_creates_a_note_with_no_cookie_and_no_origin(client, config):
    _, token = _mint_direct(config)
    response = await client.post(NOTES, headers=_bearer(token), json={
        "content": "Call the plumber", "client_note_id": "0F8FAD5B-D9CB-469F-A165-70867728950E",
    })
    assert response.status_code == 201
    assert response.json()["path"] == "Notes/Inbox/Call the plumber.md"
    assert (_notes_dir(config) / "Call the plumber.md").read_text() == "Call the plumber"
    [row] = _rows(config)
    assert row["last_used_at"] is not None


async def test_the_session_path_still_needs_the_origin(client, config):
    cookies = await _login(client)
    refused = await client.post(NOTES, cookies=cookies, json={"content": "Buy milk"})
    assert refused.status_code == 403
    accepted = await client.post(NOTES, cookies=cookies, headers=ORIGIN, json={"content": "Buy milk"})
    assert accepted.status_code == 201


async def test_a_token_without_the_notes_scope_is_a_403(client, config, monkeypatch):
    from istota.webui import device_tokens

    monkeypatch.setattr(device_tokens, "KNOWN_SCOPES", frozenset({"notes", "voice"}))
    _, token = _mint_direct(config, scopes=("voice",))
    response = await client.post(NOTES, headers=_bearer(token), json={"content": "Buy milk"})
    assert response.status_code == 403
    assert not _notes_dir(config).exists()
    # Any scope is enough to revoke itself.
    assert (await client.delete(DEVICES + "/self", headers=_bearer(token))).status_code == 200


@pytest.mark.parametrize("header", ["Bearer not-a-token", "Bearer ", "bearer   "])
async def test_a_bad_bearer_is_a_401_even_beside_a_valid_session(client, config, header):
    cookies = await _login(client)
    response = await client.post(
        NOTES, cookies=cookies, headers={**ORIGIN, "authorization": header}, json={"content": "Buy milk"},
    )
    assert response.status_code == 401
    assert not _notes_dir(config).exists()


async def test_a_non_bearer_authorization_header_leaves_the_session_path(client, config):
    # A basic-auth proxy in front of the app makes the browser send this on every request.
    cookies = await _login(client)
    response = await client.post(
        NOTES, cookies=cookies, headers={**ORIGIN, "authorization": "Basic dXNlcjpwYXNz"},
        json={"content": "Buy milk"},
    )
    assert response.status_code == 201


async def test_a_disabled_account_cannot_use_its_token(client, config):
    from istota.webui import auth

    _, token = _mint_direct(config)
    auth.upsert_identity(config.db_path, "alice", "alice@example.com")
    auth.set_disabled(config.db_path, "alice", True)
    response = await client.post(NOTES, headers=_bearer(token), json={"content": "Buy milk"})
    assert response.status_code == 401
    assert not _notes_dir(config).exists()


async def test_a_bearer_reaches_no_other_route(client, config):
    _, token = _mint_direct(config)
    assert (await client.get(DEVICES, headers=_bearer(token))).status_code == 401
    assert (await client.get("/istota/api/me", headers=_bearer(token))).status_code == 401
    put = await client.put(NOTES, headers=_bearer(token), json={"path": "x.md", "content": "", "revision": ""})
    assert put.status_code == 401


def test_last_used_is_written_at_most_once_a_minute(config):
    from istota import db
    from istota.webui import device_tokens

    _, token = _mint_direct(config)
    with db.get_db(config.db_path) as conn:
        assert device_tokens.authenticate(conn, token) is not None
        first = conn.execute("SELECT last_used_at FROM device_tokens").fetchone()[0]
        conn.execute("UPDATE device_tokens SET last_used_at = ?", ("2000-01-01T00:00:00.000Z",))
    with db.get_db(config.db_path) as conn:
        device_tokens.authenticate(conn, token)
        assert conn.execute("SELECT last_used_at FROM device_tokens").fetchone()[0] > "2000"
        assert first is not None
        stamped = conn.execute("SELECT last_used_at FROM device_tokens").fetchone()[0]
        device_tokens.authenticate(conn, token)
        assert conn.execute("SELECT last_used_at FROM device_tokens").fetchone()[0] == stamped
    with db.get_db(config.db_path) as conn:
        assert device_tokens.authenticate(conn, "") is None
        assert device_tokens.authenticate(conn, "nope") is None


# --- the guard ---------------------------------------------------------------


def _depends_on(dependant, target) -> bool:
    for sub in dependant.dependencies:
        if sub.call is target or _depends_on(sub, target):
            return True
    return False


def test_exactly_two_routes_accept_a_device_token(app):
    from fastapi.routing import APIRoute

    from istota.webui.router_stubs import session_or_device

    reached = sorted(
        (method, route.path)
        for route in app.routes if isinstance(route, APIRoute) and _depends_on(route.dependant, session_or_device)
        for method in route.methods
    )
    assert reached == [("DELETE", "/istota/api/devices/self"), ("POST", "/istota/api/workspace/notes")]
    # The body guard lets a bearer through on exactly these, before the body is read.
    import istota.webui.app as mod

    assert sorted(mod.DEVICE_TOKEN_ROUTES) == reached


async def test_the_body_guard_refuses_a_bad_bearer_before_the_route(client, config):
    cookies = await _login(client)
    response = await client.post(
        NOTES, cookies=cookies, headers={**ORIGIN, **_bearer("not-a-token")}, content=b"x" * 1000,
    )
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


# --- user deletion -----------------------------------------------------------


async def test_deleting_a_user_removes_their_tokens_and_note_keys(client, config):
    from istota import db, user_profiles

    _, token = _mint_direct(config)
    _mint_direct(config, user_id="bob")
    created = await client.post(NOTES, headers=_bearer(token), json={
        "content": "Buy milk", "client_note_id": "note-key-0001",
    })
    assert created.status_code == 201
    assert user_profiles.delete_profile(config.db_path, "alice")
    assert _rows(config) == []
    assert len(_rows(config, "bob")) == 1
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM quick_note_keys WHERE user_id = 'alice'").fetchone()[0] == 0
    response = await client.post(NOTES, headers=_bearer(token), json={"content": "After deletion"})
    assert response.status_code == 401
