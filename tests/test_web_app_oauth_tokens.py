"""Tests for OAuth token persistence in the web app (Stage 1 of the
user-scoped Nextcloud OAuth spec): callback storage, /api/me status,
the Disconnect endpoint, and the feature gate."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db, web_tokens
from istota.config import Config, SiteConfig, UserConfig, WebConfig

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps, reason="web dependencies not installed",
)

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

KEY = "w" * 64


def _make_config(tmp_path, token_storage="encrypted"):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(enabled=True, hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
            token_storage=token_storage,
        ),
        bot_name="Istota",
    )


def _patch_app(config):
    import istota.web_app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    return mod.app


TOKEN_RESPONSE = {
    "user_id": "alice",
    "access_token": "the-access",
    "refresh_token": "the-refresh",
    "expires_in": 3600,
}


async def _login(client, token_response=None):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value=dict(token_response or TOKEN_RESPONSE),
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(web_tokens._KEY_ENV_VAR, KEY)


@pytest.fixture(autouse=True)
def _reset_locks():
    web_tokens._refresh_locks.clear()
    yield
    web_tokens._refresh_locks.clear()


async def _client_for(config):
    app = _patch_app(config)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="https://example.com")


@_needs_web_deps
class TestCallbackPersistence:
    async def test_persists_when_enabled_and_keyed(self, tmp_path, keyed):
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            resp = await _login(client)
            assert resp.status_code == 302
        assert web_tokens.get_access_token(
            config.db_path, config, "alice",
        ) == "the-access"

    async def test_no_persist_when_ephemeral(self, tmp_path, keyed):
        config = _make_config(tmp_path, token_storage="ephemeral")
        async with await _client_for(config) as client:
            resp = await _login(client)
            assert resp.status_code == 302
        assert web_tokens.token_status(config.db_path, "alice") is None

    async def test_no_persist_when_keyless(self, tmp_path, monkeypatch):
        monkeypatch.delenv(web_tokens._KEY_ENV_VAR, raising=False)
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            resp = await _login(client)
            assert resp.status_code == 302
        assert web_tokens.token_status(config.db_path, "alice") is None

    async def test_no_persist_without_refresh_token(self, tmp_path, keyed):
        config = _make_config(tmp_path)
        token = dict(TOKEN_RESPONSE)
        del token["refresh_token"]
        async with await _client_for(config) as client:
            resp = await _login(client, token)
            assert resp.status_code == 302
        assert web_tokens.token_status(config.db_path, "alice") is None

    async def test_login_survives_storage_failure(self, tmp_path, keyed, monkeypatch):
        config = _make_config(tmp_path)
        monkeypatch.setattr(
            web_tokens, "store_tokens",
            MagicMock(side_effect=RuntimeError("disk full")),
        )
        async with await _client_for(config) as client:
            resp = await _login(client)
            assert resp.status_code == 302  # login still succeeds

    async def test_relogin_overwrites_pair(self, tmp_path, keyed):
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            await _login(client)
            second = dict(TOKEN_RESPONSE, access_token="second-access",
                          refresh_token="second-refresh")
            await _login(client, second)
        assert web_tokens.get_access_token(
            config.db_path, config, "alice",
        ) == "second-access"


@_needs_web_deps
class TestApiMeStatus:
    async def test_reports_connected(self, tmp_path, keyed):
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            resp = await _login(client)
            cookies = resp.cookies
            me = (await client.get("/istota/api/me", cookies=cookies)).json()
        assert me["nextcloud_token"]["connected"] is True
        assert me["nextcloud_token"]["expires_at"]

    async def test_reports_not_connected_without_row(self, tmp_path, keyed):
        config = _make_config(tmp_path, token_storage="encrypted")
        async with await _client_for(config) as client:
            # Log in with no refresh token → nothing stored.
            token = dict(TOKEN_RESPONSE)
            del token["refresh_token"]
            resp = await _login(client, token)
            me = (await client.get("/istota/api/me", cookies=resp.cookies)).json()
        assert me["nextcloud_token"] == {"connected": False, "expires_at": None}

    async def test_null_when_feature_off(self, tmp_path, keyed):
        config = _make_config(tmp_path, token_storage="ephemeral")
        async with await _client_for(config) as client:
            resp = await _login(client)
            me = (await client.get("/istota/api/me", cookies=resp.cookies)).json()
        assert me["nextcloud_token"] is None


@_needs_web_deps
class TestDisconnectEndpoint:
    async def test_disconnect_removes_row(self, tmp_path, keyed):
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            resp = await _login(client)
            cookies = resp.cookies
            out = await client.delete(
                "/istota/api/settings/nextcloud-token", cookies=cookies,
                headers={"origin": "https://example.com"},
            )
            assert out.status_code == 200
            assert out.json() == {"ok": True, "was_connected": True}
            # Session still valid.
            me = await client.get("/istota/api/me", cookies=cookies)
            assert me.status_code == 200
            assert me.json()["nextcloud_token"] == {
                "connected": False, "expires_at": None,
            }

    async def test_disconnect_idempotent(self, tmp_path, keyed):
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            resp = await _login(client)
            cookies = resp.cookies
            await client.delete(
                "/istota/api/settings/nextcloud-token", cookies=cookies,
                headers={"origin": "https://example.com"},
            )
            out = await client.delete(
                "/istota/api/settings/nextcloud-token", cookies=cookies,
                headers={"origin": "https://example.com"},
            )
        assert out.json() == {"ok": True, "was_connected": False}

    async def test_requires_auth(self, tmp_path, keyed):
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            out = await client.delete(
                "/istota/api/settings/nextcloud-token",
                headers={"origin": "https://example.com"},
            )
        assert out.status_code == 401


class TestConfigParsing:
    def test_token_storage_encrypted_parsed(self, tmp_path):
        toml = tmp_path / "config.toml"
        toml.write_text(
            "[web]\nenabled = true\ntoken_storage = \"encrypted\"\n"
            "[web.chat]\ntalk_read_sync_interval = 30\n"
        )
        from istota.config import load_config
        config = load_config(toml)
        assert config.web.token_storage == "encrypted"
        assert config.web.chat.talk_read_sync_interval == 30

    def test_unknown_token_storage_falls_back(self, tmp_path):
        toml = tmp_path / "config.toml"
        toml.write_text("[web]\ntoken_storage = \"plaintext\"\n")
        from istota.config import load_config
        config = load_config(toml)
        assert config.web.token_storage == "ephemeral"

    def test_env_override(self, tmp_path, monkeypatch):
        toml = tmp_path / "config.toml"
        toml.write_text("[web]\nenabled = true\n")
        monkeypatch.setenv("ISTOTA_WEB_TOKEN_STORAGE", "encrypted")
        from istota.config import load_config
        config = load_config(toml)
        assert config.web.token_storage == "encrypted"

    def test_env_override_unknown_ignored(self, tmp_path, monkeypatch):
        toml = tmp_path / "config.toml"
        toml.write_text("[web]\nenabled = true\n")
        monkeypatch.setenv("ISTOTA_WEB_TOKEN_STORAGE", "bogus")
        from istota.config import load_config
        config = load_config(toml)
        assert config.web.token_storage == "ephemeral"
