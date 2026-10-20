"""The per-device location ingest token routes under `/istota/api/location/devices`.

Listing, minting (`'app'` for the tracker's own device, `'code'` for a QR code
or Overland), removing a row, and removing the legacy shared secret. Every
refusal happens before anything is written, and every write stamps the
receiver's reload sentinel so a new or removed token takes effect on the
device's next request.
"""

from unittest.mock import AsyncMock

import pytest

from tests.test_web_app import _make_config, _needs_web_deps, _patch_app

pytestmark = _needs_web_deps

try:
    from httpx import ASGITransport, AsyncClient
except ImportError:  # pragma: no cover - covered by _needs_web_deps
    pass

URL = "/istota/api/location/devices"
ORIGIN = {"origin": "https://example.com"}
DEVICE = "6f1c2b3a-4d5e-4f60-8a7b-9c0d1e2f3a4b"


@pytest.fixture
def config(tmp_path, monkeypatch):
    from istota.config import LocationReceiverConfig, UserConfig

    monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key" * 8)
    cfg = _make_config(
        tmp_path,
        users={
            "alice": UserConfig(display_name="Alice"),
            "bob": UserConfig(display_name="Bob", disabled_modules=["location"]),
            "carol": UserConfig(display_name="Carol"),
        },
    )
    cfg.location = LocationReceiverConfig(enabled=True)
    return cfg


@pytest.fixture
def app(config):
    return _patch_app(config)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


async def _login(client, user_id="alice"):
    import istota.webui.app as mod

    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": user_id})
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


def _rows(config, user_id="alice"):
    from istota import db
    from istota.location import ingest_tokens

    with db.get_db(config.db_path) as conn:
        return ingest_tokens.list_tokens(conn, user_id)


def _stored_hashes(config):
    from istota import db

    with db.get_db(config.db_path) as conn:
        return [r[0] for r in conn.execute("SELECT token_hash FROM location_ingest_tokens")]


def _stamp(config):
    from istota.location import ingest_signal

    return ingest_signal.reload_stamp(config.db_path)


# --- auth and origin -------------------------------------------------------


@pytest.mark.parametrize("method,path", [
    ("get", URL), ("post", URL), ("delete", URL + "/1"), ("delete", URL + "/legacy"),
])
async def test_routes_require_auth(client, method, path):
    response = await getattr(client, method)(path, headers=ORIGIN)
    assert response.status_code == 401


@pytest.mark.parametrize("origin", [None, "https://evil.example.com"])
async def test_mutations_require_origin(client, config, origin):
    cookies = await _login(client)
    headers = {"origin": origin} if origin else {}
    post = await client.post(URL, cookies=cookies, headers=headers, json={"kind": "code"})
    assert post.status_code == 403
    assert (await client.delete(URL + "/1", cookies=cookies, headers=headers)).status_code == 403
    assert (await client.delete(URL + "/legacy", cookies=cookies, headers=headers)).status_code == 403
    assert _rows(config) == []


# --- mint ------------------------------------------------------------------


async def test_mint_code_returns_token_endpoint_and_url_once(client, config):
    from istota.location import ingest_tokens

    cookies = await _login(client)
    before = _stamp(config)
    resp = await client.post(URL, cookies=cookies, headers=ORIGIN, json={"kind": "code"})
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"id", "token", "endpoint", "webhook_url"}
    assert len(body["token"]) == 43
    assert body["endpoint"] == "https://example.com/webhooks/location"
    assert body["webhook_url"] == f"https://example.com/webhooks/location?token={body['token']}"
    assert _stored_hashes(config) == [ingest_tokens.hash_token(body["token"])]
    [row] = _rows(config)
    assert row["id"] == body["id"]
    assert row["kind"] == "code"
    assert row["device_id"] is None
    assert row["label"] == "QR code"
    assert _stamp(config) > before


async def test_mint_code_twice_inserts_two_rows(client, config):
    cookies = await _login(client)
    for _ in range(2):
        resp = await client.post(URL, cookies=cookies, headers=ORIGIN, json={"kind": "code"})
        assert resp.status_code == 201
    assert len(_rows(config)) == 2


async def test_mint_app_stores_uppercase_device_and_replaces_in_place(client, config):
    cookies = await _login(client)
    first = await client.post(URL, cookies=cookies, headers=ORIGIN, json={"kind": "app", "device_id": DEVICE})
    assert first.status_code == 201
    [row] = _rows(config)
    assert row["device_id"] == DEVICE.upper()
    assert row["label"] == "iPhone"
    again = await client.post(
        URL, cookies=cookies, headers=ORIGIN, json={"kind": "app", "device_id": DEVICE.upper()},
    )
    assert again.status_code == 201
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["token"] != first.json()["token"]
    assert len(_rows(config)) == 1


async def test_mint_label_is_trimmed_and_stripped_of_control_characters(client, config):
    cookies = await _login(client)
    resp = await client.post(
        URL, cookies=cookies, headers=ORIGIN,
        json={"kind": "code", "label": "  Kitchen\x00 tab\x1blet\n  "},
    )
    assert resp.status_code == 201
    assert _rows(config)[0]["label"] == "Kitchen tablet"


async def test_mint_blank_label_takes_the_default(client, config):
    cookies = await _login(client)
    resp = await client.post(
        URL, cookies=cookies, headers=ORIGIN, json={"kind": "app", "device_id": DEVICE, "label": " \t "},
    )
    assert resp.status_code == 201
    assert _rows(config)[0]["label"] == "iPhone"


async def test_mint_accepts_a_64_character_label(client, config):
    cookies = await _login(client)
    resp = await client.post(URL, cookies=cookies, headers=ORIGIN, json={"kind": "code", "label": "x" * 64})
    assert resp.status_code == 201


@pytest.mark.parametrize("body,field", [
    ({}, "kind"),
    ({"kind": "phone"}, "kind"),
    ({"kind": ["code"]}, "kind"),
    ({"kind": "app"}, "device_id"),
    ({"kind": "app", "device_id": "not-a-uuid"}, "device_id"),
    ({"kind": "app", "device_id": DEVICE.replace("-", "")}, "device_id"),
    ({"kind": "app", "device_id": "{" + DEVICE + "}"}, "device_id"),
    ({"kind": "app", "device_id": 12}, "device_id"),
    ({"kind": "code", "device_id": DEVICE}, "device_id"),
    ({"kind": "code", "device_id": None}, "device_id"),
    ({"kind": "code", "label": 5}, "label"),
    ({"kind": "code", "label": "x" * 65}, "label"),
    ({"kind": "code", "user_id": "bob"}, "user_id"),
])
async def test_mint_invalid_body_is_400_naming_the_field(client, config, body, field):
    cookies = await _login(client)
    before = _stamp(config)
    resp = await client.post(URL, cookies=cookies, headers=ORIGIN, json=body)
    assert resp.status_code == 400
    assert field in resp.json()["detail"]
    assert _rows(config) == []
    assert _stamp(config) == before


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'"code"'])
async def test_mint_non_object_body_is_400(client, config, raw):
    cookies = await _login(client)
    resp = await client.post(
        URL, cookies=cookies, headers={**ORIGIN, "content-type": "application/json"}, content=raw,
    )
    assert resp.status_code == 400
    assert _rows(config) == []


async def test_mint_oversized_body_is_refused(client, config):
    cookies = await _login(client)
    resp = await client.post(
        URL, cookies=cookies, headers=ORIGIN, json={"kind": "code", "label": "x" * 5000},
    )
    assert resp.status_code in (400, 413)
    assert _rows(config) == []


async def test_mint_refused_when_location_is_off(client, config):
    cookies = await _login(client, "bob")
    resp = await client.post(URL, cookies=cookies, headers=ORIGIN, json={"kind": "code"})
    assert resp.status_code == 409
    assert "location module is off" in resp.json()["detail"]
    assert _rows(config, "bob") == []


async def test_module_off_comes_before_the_body_check(client, config):
    cookies = await _login(client, "bob")
    resp = await client.post(URL, cookies=cookies, headers=ORIGIN, json={"kind": "bogus"})
    assert resp.status_code == 409


async def test_mint_refused_when_the_receiver_is_off(client, config):
    from istota.config import LocationReceiverConfig

    config.location = LocationReceiverConfig(enabled=False)
    cookies = await _login(client)
    resp = await client.post(URL, cookies=cookies, headers=ORIGIN, json={"kind": "code"})
    assert resp.status_code == 409
    assert _rows(config) == []


async def test_mint_503_when_config_not_loaded(client, app, config, monkeypatch):
    """The session and origin checks already refuse with no config, so the
    route's own check is reached only with all three stood in."""
    import istota.webui.app as mod

    monkeypatch.setattr(mod, "_stream_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr(mod, "_verify_origin", lambda request: None)
    app.dependency_overrides[mod._require_api_auth] = lambda: {"username": "alice"}
    mod._config = None
    try:
        resp = await client.post(URL, headers=ORIGIN, json={"kind": "code"})
    finally:
        mod._config = config
        app.dependency_overrides.pop(mod._require_api_auth, None)
    assert resp.status_code == 503
    assert _rows(config) == []


async def test_mint_refused_at_the_cap_but_a_replace_still_works(client, config):
    from istota import db
    from istota.location import ingest_tokens

    cookies = await _login(client)
    with db.get_db(config.db_path) as conn:
        ingest_tokens.mint(conn, "alice", "app", DEVICE, "iPhone")
        for _ in range(ingest_tokens.MAX_TOKENS_PER_USER - 1):
            ingest_tokens.mint(conn, "alice", "code", None, "QR code")
    before = _stamp(config)
    refused = await client.post(URL, cookies=cookies, headers=ORIGIN, json={"kind": "code"})
    assert refused.status_code == 409
    assert refused.json()["detail"] == "This account already has 20 devices. Remove one first."
    assert len(_rows(config)) == ingest_tokens.MAX_TOKENS_PER_USER
    assert _stamp(config) == before
    replaced = await client.post(URL, cookies=cookies, headers=ORIGIN, json={"kind": "app", "device_id": DEVICE})
    assert replaced.status_code == 201
    assert len(_rows(config)) == ingest_tokens.MAX_TOKENS_PER_USER


def _standalone(config):
    """The one shape that reaches the hostname check.

    Under Nextcloud auth a blank `[site] hostname` already 403s every request at
    `_verify_origin`; in no-auth mode that check is a no-op, which is what leaves
    the standalone install able to reach the mint with no public hostname.
    """
    from istota.config import SiteConfig, UserConfig, WebConfig

    config.site = SiteConfig(hostname="")
    config.web = WebConfig(enabled=True, port=8766, auth="none")
    config.users = {"alice": UserConfig(display_name="Alice")}
    return _patch_app(config)


async def _post_as_local(app, json):
    # No-auth answers only at its loopback names (ISSUE-778).
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8766") as local:
        return await local.post(URL, json=json)


async def test_mint_refused_without_a_public_hostname(config):
    """A relative webhook URL is a QR the phone can only reject, and a tracker
    cannot post to one."""
    resp = await _post_as_local(_standalone(config), {"kind": "code"})
    assert resp.status_code == 409
    assert "hostname" in resp.json()["detail"]
    assert _rows(config) == []


async def test_hostname_check_comes_before_the_body_check(config):
    resp = await _post_as_local(_standalone(config), {"kind": "bogus"})
    assert resp.status_code == 409
    assert "hostname" in resp.json()["detail"]


async def test_mint_without_a_hostname_leaves_the_legacy_token_alone(config):
    from istota.credentials import store as secrets_store

    secrets_store.set_secret(config.db_path, "alice", "overland", "ingest_token", "still-working")
    resp = await _post_as_local(_standalone(config), {"kind": "code"})
    assert resp.status_code == 409
    assert secrets_store.get_secret(config.db_path, "alice", "overland", "ingest_token") == "still-working"


async def test_mint_does_not_touch_the_legacy_token(client, config):
    """Creating a code no longer rotates anything."""
    from istota.credentials import store as secrets_store

    secrets_store.set_secret(config.db_path, "alice", "overland", "ingest_token", "still-working")
    cookies = await _login(client)
    resp = await client.post(URL, cookies=cookies, headers=ORIGIN, json={"kind": "code"})
    assert resp.status_code == 201
    assert secrets_store.get_secret(config.db_path, "alice", "overland", "ingest_token") == "still-working"


# --- list ------------------------------------------------------------------


async def test_list_shape_has_no_hash_or_token(client, config):
    cookies = await _login(client)
    empty = await client.get(URL, cookies=cookies)
    assert empty.status_code == 200
    assert empty.json() == {"devices": [], "legacy_token": False}

    app_mint = (await client.post(
        URL, cookies=cookies, headers=ORIGIN, json={"kind": "app", "device_id": DEVICE, "label": "Phone"},
    )).json()
    code_mint = (await client.post(URL, cookies=cookies, headers=ORIGIN, json={"kind": "code"})).json()

    resp = await client.get(URL, cookies=cookies)
    body = resp.json()
    assert body["legacy_token"] is False
    assert [d["id"] for d in body["devices"]] == [code_mint["id"], app_mint["id"]]
    for device in body["devices"]:
        assert set(device) == {"id", "kind", "device_id", "label", "token_tail", "created_at", "last_used_at"}
    assert body["devices"][1]["token_tail"] == app_mint["token"][-4:]
    assert body["devices"][1]["device_id"] == DEVICE.upper()
    assert app_mint["token"] not in resp.text
    assert code_mint["token"] not in resp.text
    for token_hash in _stored_hashes(config):
        assert token_hash not in resp.text


async def test_list_reports_the_legacy_token_without_reading_it(client, config):
    from istota import db
    from istota.credentials import store as secrets_store

    secrets_store.set_secret(config.db_path, "alice", "overland", "ingest_token", "shared-secret")
    cookies = await _login(client)
    resp = await client.get(URL, cookies=cookies)
    assert resp.json() == {"devices": [], "legacy_token": True}
    assert "shared-secret" not in resp.text
    with db.get_db(config.db_path) as conn:
        touched = conn.execute(
            "SELECT last_accessed_at FROM secrets WHERE user_id = 'alice' AND service = 'overland'",
        ).fetchone()[0]
    assert touched is None


async def test_a_minted_token_is_not_readable_afterwards(client, config):
    cookies = await _login(client)
    token = (await client.post(URL, cookies=cookies, headers=ORIGIN, json={"kind": "code"})).json()["token"]
    for path in (
        URL, "/istota/api/settings/module-services/location", "/istota/api/location/settings-info",
    ):
        resp = await client.get(path, cookies=cookies)
        assert resp.status_code == 200
        assert token not in resp.text


async def test_list_shows_only_the_callers_rows(client, config):
    alice = await _login(client)
    await client.post(URL, cookies=alice, headers=ORIGIN, json={"kind": "code"})
    carol = await _login(client, "carol")
    assert (await client.get(URL, cookies=carol)).json() == {"devices": [], "legacy_token": False}


# --- delete ----------------------------------------------------------------


async def test_delete_own_row_signals_and_404s_after(client, config):
    cookies = await _login(client)
    minted = (await client.post(URL, cookies=cookies, headers=ORIGIN, json={"kind": "code"})).json()
    before = _stamp(config)
    resp = await client.delete(f"{URL}/{minted['id']}", cookies=cookies, headers=ORIGIN)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert _rows(config) == []
    assert _stamp(config) > before
    again = await client.delete(f"{URL}/{minted['id']}", cookies=cookies, headers=ORIGIN)
    assert again.status_code == 404


async def test_delete_another_users_row_is_404(client, config):
    alice = await _login(client)
    minted = (await client.post(URL, cookies=alice, headers=ORIGIN, json={"kind": "code"})).json()
    carol = await _login(client, "carol")
    before = _stamp(config)
    resp = await client.delete(f"{URL}/{minted['id']}", cookies=carol, headers=ORIGIN)
    assert resp.status_code == 404
    assert len(_rows(config)) == 1
    assert _stamp(config) == before


async def test_delete_legacy_removes_the_secret_and_signals(client, config):
    from istota.credentials import store as secrets_store

    secrets_store.set_secret(config.db_path, "alice", "overland", "ingest_token", "shared-secret")
    cookies = await _login(client)
    minted = (await client.post(URL, cookies=cookies, headers=ORIGIN, json={"kind": "code"})).json()
    before = _stamp(config)
    resp = await client.delete(f"{URL}/legacy", cookies=cookies, headers=ORIGIN)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert secrets_store.get_secret(config.db_path, "alice", "overland", "ingest_token") is None
    assert [r["id"] for r in _rows(config)] == [minted["id"]]
    assert _stamp(config) > before
    assert (await client.get(URL, cookies=cookies)).json()["legacy_token"] is False


async def test_delete_legacy_without_one_is_404(client, config):
    from istota.credentials import store as secrets_store

    secrets_store.set_secret(config.db_path, "carol", "overland", "ingest_token", "carols")
    cookies = await _login(client)
    before = _stamp(config)
    resp = await client.delete(f"{URL}/legacy", cookies=cookies, headers=ORIGIN)
    assert resp.status_code == 404
    assert _stamp(config) == before
    assert secrets_store.get_secret(config.db_path, "carol", "overland", "ingest_token") == "carols"


# --- the removed generate route ---------------------------------------------


async def test_the_old_generate_route_is_gone(client, config):
    from istota.credentials import store as secrets_store

    cookies = await _login(client)
    resp = await client.post(
        "/istota/api/settings/secrets/overland/ingest_token/generate", cookies=cookies, headers=ORIGIN,
    )
    assert resp.status_code in (404, 405)
    assert secrets_store.get_secret(config.db_path, "alice", "overland", "ingest_token") is None
