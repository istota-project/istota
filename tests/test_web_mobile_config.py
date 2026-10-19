"""Credential-free discovery through the real web middleware stack."""

from httpx import ASGITransport, AsyncClient
import pytest

from istota.config import Config, PushConfig, SiteConfig, WebConfig


URL = "/istota/api/mobile/config"


@pytest.fixture
def configured(monkeypatch):
    from istota.webui import app as web

    config = Config(site=SiteConfig(hostname="example.com"),
                    web=WebConfig(enabled=True, auth=["email"]))
    monkeypatch.setattr(web, "_config", config)
    monkeypatch.setattr(web.app.state, "istota_config", config, raising=False)
    return web


@pytest.fixture
async def client(configured):
    async with AsyncClient(transport=ASGITransport(app=configured.app),
                           base_url="https://example.com") as client:
        yield client


async def test_anonymous_discovery_is_small_and_has_no_session(client):
    response = await client.get(URL)
    assert response.status_code == 200
    assert response.json() == {
        "product": "istota",
        "mobile_protocol": 1,
        "minimum_app_version": "0.12.0",
        "web_base_path": "/istota/",
        "auth": {"mode": "browser_handoff", "version": 1},
        "location_ingest_path": None,
        "push": {"delivery": "unavailable"},
    }
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert len(response.content) < 512
    assert int(response.headers["content-length"]) == len(response.content)
    assert "set-cookie" not in response.headers
    assert not client.cookies


@pytest.mark.parametrize("location", [False, True])
@pytest.mark.parametrize("push", [False, True])
async def test_capabilities_follow_deployment_config(client, configured, location, push):
    config = configured._config
    config.location.enabled = location
    config.push = PushConfig(enabled=push, apns_key_path="/run/secrets/apns-key",
                             apns_key_id="example-key", apns_team_id="example-team")
    response = await client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["location_ingest_path"] == ("/webhooks/location" if location else None)
    assert body["push"] == {"delivery": "direct" if push else "unavailable"}
    assert len(response.content) < 512


@pytest.mark.parametrize("field,value", [
    ("apns_key_path", ""), ("apns_key_id", ""), ("apns_team_id", ""),
    ("apns_topic", ""), ("dispatch_interval_seconds", 0),
])
async def test_incomplete_push_configuration_is_unavailable(client, configured, field, value):
    config = configured._config
    config.push = PushConfig(enabled=True, apns_key_path="/run/secrets/apns-key",
                             apns_key_id="example-key", apns_team_id="example-team")
    setattr(config.push, field, value)
    assert (await client.get(URL)).json()["push"] == {"delivery": "unavailable"}


async def test_request_and_private_config_never_enter_manifest(client, configured):
    config = configured._config
    config.bot_name = "private display name" * 10000
    config.web.oauth2_provider = "https://identity.example.com/private"
    config.web.oauth2_client_secret = "test-only-oauth-secret"
    config.web.session_secret_key = "test-only-session-secret"
    response = await client.get(URL + "?server=https://other.example.com/", headers={
        "host": "other.example.com",
        "x-forwarded-host": "forwarded.example.com",
        "x-forwarded-proto": "http",
        "authorization": "Bearer test-only-ignored",
    })
    assert response.status_code == 200
    assert response.content == (await client.get(URL)).content
    assert len(response.content) < 512
    assert "example.com" not in response.text
    assert "secret" not in response.text
    assert "private" not in response.text
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("hostname", ["", "localhost:8766", "127.0.0.1:8766"])
async def test_missing_or_plaintext_public_origin_fails_closed(client, configured, hostname):
    configured._config.site.hostname = hostname
    response = await client.get(URL)
    assert response.status_code == 503
    assert response.json() == {"error": "mobile_configuration_unavailable"}
    assert response.headers["cache-control"] == "no-store"


async def test_unloaded_config_fails_closed(client, configured, monkeypatch):
    monkeypatch.setattr(configured.app.state, "istota_config", None)
    response = await client.get(URL)
    assert response.status_code == 503
    assert response.json() == {"error": "mobile_configuration_unavailable"}


async def test_discovery_does_not_exempt_other_api_routes_or_request_bodies(client):
    assert (await client.get("/istota/api/me")).status_code == 401
    for path in (URL, "/istota/api/push/devices"):
        async def unread_body():
            pytest.fail("anonymous API body was consumed")
            yield b"{}"

        response = await client.post(path, content=unread_body())
        assert response.status_code == 401
    response = await client.request("GET", URL, content=unread_body())
    assert response.status_code == 401
    response = await client.get("/api/mobile/config")
    assert response.status_code == 404
