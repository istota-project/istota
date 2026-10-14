"""The Nextcloud sharing routes and the `unshare` disconnect, through the app.

The user logs in through the OAuth callback, which stores their token, and
the fake Nextcloud from `test_nextcloud_sharing` stands behind `httpx.stream`.
"""

import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from istota import db
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.webui import tokens
from tests.test_nextcloud_sharing import BOOK_HOME, CAL_HOME, FakeNextcloud, _id, _posts

pytest.importorskip("fastapi")
pytest.importorskip("authlib")

ORIGIN = {"origin": "https://example.com"}
URL = "/istota/api/settings/nextcloud-sharing"
TOKEN_URL = "/istota/api/settings/nextcloud-token"
PERSONAL = _id(f"{CAL_HOME}personal/")
WORK = _id(f"{CAL_HOME}work/")
CONTACTS = _id(f"{BOOK_HOME}contacts/")


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv(tokens._KEY_ENV_VAR, "w" * 64)
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    config = Config(
        db_path=db_path,
        workspace_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com/nc",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
            token_storage="encrypted",
        ),
        bot_name="Istota",
    )
    config.nextcloud.url = "https://internal.example.com"
    config.nextcloud.username = "istota"
    config.nextcloud.app_password = "bot-password"
    return config


@pytest.fixture
def nextcloud(monkeypatch):
    fake = FakeNextcloud()

    @contextmanager
    def stream(method, url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(fake.handle)) as client:
            with client.stream(method, url, **kwargs) as response:
                yield response

    monkeypatch.setattr(httpx, "stream", stream)
    return fake


@pytest.fixture(autouse=True)
def _reset_locks():
    tokens._refresh_locks.clear()
    yield
    tokens._refresh_locks.clear()


@pytest.fixture
async def client(config, nextcloud):
    import istota.webui.app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
        "user_id": "alice", "access_token": "alice-access",
        "refresh_token": "alice-refresh", "expires_in": 3600,
    })
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mod.app), base_url="https://example.com",
    ) as client:
        resp = await client.get("/istota/callback", follow_redirects=False)
        assert resp.status_code == 302
        client.cookies = resp.cookies
        nextcloud.requests.clear()
        yield client


def _levels(payload):
    return {c["name"]: c["level"] for c in payload["collections"]}


class TestGet:
    async def test_lists_collections_and_kinds(self, client):
        resp = await client.get(URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["bot"] == "istota"
        assert body["kinds"] == {
            "calendars": {"ceiling": "full", "available": True},
            "contacts": {"ceiling": "full", "available": True},
        }
        assert {tuple(sorted(c)) for c in body["collections"]} == {("id", "kind", "level", "name")}
        assert _levels(body) == {"Personal": "off", "Work": "full", "Contacts": "readonly"}
        assert {c["id"] for c in body["collections"]} == {PERSONAL, WORK, CONTACTS}

    @pytest.mark.parametrize("caldav, available", [
        ({"url": "https://caldav.example.com"}, False),
        ({"username": "someone-else"}, False),
        ({"username": "istota"}, True),
    ])
    async def test_kinds_reflect_ceiling_and_caldav_override(self, client, config, caldav, available):
        config.nextcloud.sharing.calendars = "readonly"
        config.nextcloud.sharing.contacts = "off"
        for key, value in caldav.items():
            setattr(config.caldav, key, value)
        body = (await client.get(URL)).json()
        assert body["kinds"] == {
            "calendars": {"ceiling": "readonly", "available": available},
            "contacts": {"ceiling": "off", "available": True},
        }
        # Listed whatever the ceiling, so existing shares stay removable.
        assert _levels(body)["Work"] == "full"

    async def test_no_token_is_a_reconnect(self, client, config, nextcloud):
        tokens.delete_tokens(config.db_path, "alice")
        resp = await client.get(URL)
        assert resp.status_code == 409
        assert "Reconnect" in resp.json()["error"]
        assert not nextcloud.requests

    async def test_requires_auth(self, client, nextcloud):
        client.cookies.clear()
        resp = await client.get(URL)
        assert resp.status_code == 401
        assert not nextcloud.requests


class TestPut:
    async def test_sets_levels_and_returns_the_reread(self, client, nextcloud, caplog):
        with caplog.at_level(logging.INFO, logger="istota"):
            resp = await client.put(
                URL, json={"changes": {PERSONAL: "full", CONTACTS: "off"}}, headers=ORIGIN,
            )
        assert resp.status_code == 200
        body = resp.json()
        assert _levels(body) == {"Personal": "full", "Work": "full", "Contacts": "off"}
        assert body["failed"] == []
        assert body["bot"] == "istota" and set(body["kinds"]) == {"calendars", "contacts"}
        assert nextcloud.calendars["personal"]["shares"]["istota"] == "read-write"
        assert PERSONAL in caplog.text and CONTACTS in caplog.text
        assert "alice-access" not in caplog.text

    async def test_origin_check(self, client, nextcloud):
        resp = await client.put(URL, json={"changes": {PERSONAL: "full"}})
        assert resp.status_code == 403
        assert not nextcloud.requests

    @pytest.mark.parametrize("payload", [
        {"changes": {PERSONAL: "admin"}},
        {"changes": {PERSONAL: []}},
        {"changes": {PERSONAL: {"x": 1}}},
        {"changes": {"../../etc": "full"}},
        {"changes": [PERSONAL]},
        {},
    ])
    async def test_malformed_request_is_a_400(self, client, nextcloud, payload):
        resp = await client.put(URL, json=payload, headers=ORIGIN)
        assert resp.status_code == 400
        assert "error" in resp.json()
        assert not _posts(nextcloud)

    async def test_raise_above_the_ceiling_is_a_403(self, client, config, nextcloud):
        config.nextcloud.sharing.calendars = "readonly"
        resp = await client.put(URL, json={"changes": {PERSONAL: "full"}}, headers=ORIGIN)
        assert resp.status_code == 403
        assert not _posts(nextcloud)

    async def test_partial_failure_is_a_200_with_failed(self, client, nextcloud):
        nextcloud.post_status["personal"] = 403
        resp = await client.put(
            URL, json={"changes": {PERSONAL: "full", CONTACTS: "full"}}, headers=ORIGIN,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["failed"] == [
            {"id": PERSONAL, "name": "Personal", "reason": "Nextcloud did not allow this share."},
        ]
        assert _levels(body)["Contacts"] == "full"

    async def test_every_change_failing_is_a_502(self, client, nextcloud):
        nextcloud.ignore_shares = True
        resp = await client.put(
            URL, json={"changes": {PERSONAL: "full", CONTACTS: "full"}}, headers=ORIGIN,
        )
        assert resp.status_code == 502
        body = resp.json()
        assert {f["id"] for f in body["failed"]} == {PERSONAL, CONTACTS}
        assert _levels(body)["Personal"] == "off"


class TestDisconnect:
    async def test_without_unshare_behaves_as_before(self, client, config, nextcloud):
        resp = await client.delete(TOKEN_URL, headers=ORIGIN)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "was_connected": True}
        assert tokens.token_status(config.db_path, "alice") is None
        assert not nextcloud.requests
        assert nextcloud.calendars["work"]["shares"] == {"istota": "read-write"}

    async def test_unshare_removes_shares_then_the_token(self, client, config, nextcloud):
        resp = await client.delete(TOKEN_URL, params={"unshare": "true"}, headers=ORIGIN)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "was_connected": True}
        assert tokens.token_status(config.db_path, "alice") is None
        assert "istota" not in nextcloud.calendars["work"]["shares"]
        assert "istota" not in nextcloud.books["contacts"]["shares"]

    async def test_unshare_keeps_the_token_when_a_removal_fails(self, client, config, nextcloud):
        nextcloud.post_status["contacts"] = 500
        resp = await client.delete(TOKEN_URL, params={"unshare": "true"}, headers=ORIGIN)
        assert resp.status_code == 409
        assert resp.json()["still_shared"] == ["Contacts"]
        assert tokens.token_status(config.db_path, "alice") is not None
        # The removal that succeeded stays removed.
        assert "istota" not in nextcloud.calendars["work"]["shares"]

    async def test_unshare_keeps_the_token_when_the_listing_fails(self, client, config, monkeypatch):
        @contextmanager
        def unreachable(method, url, **kwargs):
            raise httpx.ConnectError("down")
            yield

        monkeypatch.setattr(httpx, "stream", unreachable)
        resp = await client.delete(TOKEN_URL, params={"unshare": "true"}, headers=ORIGIN)
        assert resp.status_code == 409
        assert resp.json()["error"] == "Nextcloud is not reachable right now."
        assert tokens.token_status(config.db_path, "alice") is not None

    async def test_unshare_origin_check(self, client, config, nextcloud):
        resp = await client.delete(TOKEN_URL, params={"unshare": "true"})
        assert resp.status_code == 403
        assert not nextcloud.requests
        assert tokens.token_status(config.db_path, "alice") is not None
