"""Settings grant writes use the signed-in user and the normal CSRF gate."""

from unittest.mock import AsyncMock
import pytest
from istota import db, secrets_store
from istota.credential_broker.bindings import parse_binding
from tests.test_web_app import app, client, config  # noqa: F401 -- shared web fixtures


@pytest.fixture
async def signed_client(client, monkeypatch, config):  # noqa: F811 -- imported fixtures
    import istota.web_app as mod
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": "alice"})
    await client.get("/istota/callback", follow_redirects=False)
    secrets_store.upsert_secret(config.db_path, "alice", "vault_entries", "portal", "fixture-password",
                               binding=parse_binding("https://portal.example", {}, []))
    return client


async def test_settings_list_and_save_are_user_scoped(signed_client, config):  # noqa: F811 -- imported fixture
    base = "/istota/api/settings/credentials"
    response = await signed_client.get(base)
    assert response.status_code == 200
    body = response.json()
    assert body["credentials"][0]["name"] == "portal"
    assert body["credentials"][0]["grant"] is None
    assert "fixture-password" not in response.text
    assert body["grant_existing_available"] is True
    response = await signed_client.put(base + "/portal", json={}, headers={"Origin": "https://example.com"})
    assert response.status_code == 200
    assert response.json()["grant"]["allow_scheduled"] is False
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT user_id FROM credential_grants").fetchone()[0] == "alice"
    response = await signed_client.put(base + "/portal", json={"scope_mode": "rooms", "rooms": ["foreign"]},
                                      headers={"Origin": "https://example.com"})
    assert response.status_code == 400
    response = await signed_client.delete(base + "/portal", headers={"Origin": "https://example.com"})
    assert response.status_code == 200


async def test_grant_mutations_require_csrf(signed_client):
    base = "/istota/api/settings/credentials"
    for method, suffix in [("put", "/portal"), ("delete", "/portal"), ("post", "/grant-existing")]:
        kwargs = {"json": {}} if method == "put" else {}
        response = await getattr(signed_client, method)(base + suffix, **kwargs)
        assert response.status_code == 403
    response = await signed_client.post(base + "/grant-existing", headers={"Origin": "https://example.com"})
    assert response.json()["count"] == 1
    assert (await signed_client.get(base)).json()["grant_existing_available"] is False


async def test_credentials_require_login(client):  # noqa: F811 -- imported fixture
    for method, suffix in [("get", ""), ("put", "/portal"), ("delete", "/portal"), ("post", "/grant-existing")]:
        kwargs = {"json": {}} if method == "put" else {}
        response = await getattr(client, method)("/istota/api/settings/credentials" + suffix,
                                                headers={"Origin": "https://example.com"}, **kwargs)
        assert response.status_code == 401


async def test_rooms_and_forge_identity_are_checked(signed_client, config):  # noqa: F811 -- imported fixture
    base = "/istota/api/settings/credentials"
    config.admin_users = {"bob"}
    config.developer.enabled = True
    config.developer.github_token = "fixture-token"
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Personal")
    data = (await signed_client.get(base)).json()
    assert [c["name"] for c in data["credentials"]] == ["portal"]
    assert data["rooms"] == [{"token": room.token, "name": "Personal"}]
    response = await signed_client.put(base + "/portal", json={"scope_mode": "rooms", "rooms": [room.token]},
                                      headers={"Origin": "https://example.com"})
    assert response.status_code == 200
    for payload in [{"methods": ["CONNECT"]}, {"allow_scheduled": "false"}, {"user_id": "bob"}]:
        response = await signed_client.put(base + "/portal", json=payload, headers={"Origin": "https://example.com"})
        assert response.status_code == 400
