"""Tests for istota.web_app — authenticated web interface."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from istota.config import (
    Config,
    NextcloudConfig,
    ResourceConfig,
    SiteConfig,
    UserConfig,
    WebConfig,
    load_config,
)


def _make_config(tmp_path, users=None, mount_path=None, web=None):
    """Build a Config for testing."""
    if users is None:
        users = {
            "alice": UserConfig(
                display_name="Alice",
                resources=[ResourceConfig(type="miniflux", name="Feeds", base_url="http://mini")],
            ),
            "bob": UserConfig(display_name="Bob"),
        }
    return Config(
        nextcloud_mount_path=Path(mount_path) if mount_path else tmp_path / "mount",
        site=SiteConfig(enabled=True, hostname="example.com"),
        users=users,
        web=web or WebConfig(
            enabled=True,
            port=8766,
            oidc_issuer="https://cloud.example.com",
            oidc_client_id="istota-web",
            oidc_client_secret="test-secret",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


@pytest.fixture
def config(tmp_path):
    return _make_config(tmp_path)


@pytest.fixture
def app(config):
    """Provide a patched app with test config."""
    import istota.web_app as mod

    mod._config = config

    # Create a mock OAuth that has a nextcloud attribute
    mock_oauth = MagicMock()
    mock_nc = MagicMock()
    mock_oauth.nextcloud = mock_nc
    mod._oauth = mock_oauth

    return mod.app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


class TestLoginRoute:
    async def test_login_redirects_to_oidc(self, client, app):
        import istota.web_app as mod

        # Mock authorize_redirect to return a redirect response
        from fastapi.responses import RedirectResponse
        mock_redirect = RedirectResponse(url="https://cloud.example.com/authorize?client_id=istota-web")
        mod._oauth.nextcloud.authorize_redirect = AsyncMock(return_value=mock_redirect)

        resp = await client.get("/istota/login", follow_redirects=False)
        assert resp.status_code in (302, 307)
        mod._oauth.nextcloud.authorize_redirect.assert_called_once()


class TestCallbackRoute:
    async def test_callback_valid_user_sets_session(self, client, app):
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })

        resp = await client.get("/istota/callback", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/istota/"
        # Session cookie should be set
        assert any("istota_session" in c for c in resp.headers.get_list("set-cookie"))

    async def test_callback_unknown_user_returns_403(self, client, app):
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "unknown_person", "name": "Unknown"},
        })

        resp = await client.get("/istota/callback", follow_redirects=False)
        assert resp.status_code == 403

    async def test_callback_falls_back_to_userinfo_endpoint(self, client, app):
        import istota.web_app as mod

        mock_token = {"access_token": "abc"}  # no userinfo key
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value=mock_token)
        mod._oauth.nextcloud.userinfo = AsyncMock(return_value={
            "preferred_username": "alice",
            "name": "Alice",
        })

        resp = await client.get("/istota/callback", follow_redirects=False)
        assert resp.status_code == 302
        mod._oauth.nextcloud.userinfo.assert_called_once()


class TestUnauthenticatedAccess:
    async def test_dashboard_redirects_to_login(self, client):
        resp = await client.get("/istota/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/istota/login" in resp.headers["location"]

    async def test_feeds_redirects_to_login(self, client):
        resp = await client.get("/istota/feeds", follow_redirects=False)
        assert resp.status_code == 302
        assert "/istota/login" in resp.headers["location"]


class TestAuthenticatedDashboard:
    async def test_dashboard_shows_user_info(self, client, app):
        import istota.web_app as mod

        # Simulate login via callback
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = login_resp.cookies

        resp = await client.get("/istota/", cookies=cookies)
        assert resp.status_code == 200
        assert "Alice" in resp.text
        assert "alice" in resp.text

    async def test_dashboard_shows_feeds_link_for_miniflux_user(self, client, app):
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = login_resp.cookies

        resp = await client.get("/istota/", cookies=cookies)
        assert resp.status_code == 200
        assert "/istota/feeds" in resp.text

    async def test_dashboard_no_feeds_link_without_miniflux(self, client, app):
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "bob", "name": "Bob"},
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = login_resp.cookies

        resp = await client.get("/istota/", cookies=cookies)
        assert resp.status_code == 200
        assert "/istota/feeds" not in resp.text


class TestFeedsRoute:
    async def test_feeds_serves_static_file(self, client, app, config):
        import istota.web_app as mod

        # Create the feed file
        feeds_dir = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "html" / "feeds"
        feeds_dir.mkdir(parents=True)
        (feeds_dir / "index.html").write_text("<html><body>Feed content</body></html>")

        # Login as alice
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = login_resp.cookies

        resp = await client.get("/istota/feeds", cookies=cookies)
        assert resp.status_code == 200
        assert "Feed content" in resp.text

    async def test_feeds_returns_404_when_missing(self, client, app, config):
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = login_resp.cookies

        resp = await client.get("/istota/feeds", cookies=cookies)
        assert resp.status_code == 404


class TestLogout:
    async def test_logout_clears_session(self, client, app):
        import istota.web_app as mod

        # Login first
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = login_resp.cookies

        # Logout
        resp = await client.get("/istota/logout", cookies=cookies, follow_redirects=False)
        assert resp.status_code == 302
        assert "/istota/login" in resp.headers["location"]

        # Verify session is cleared — dashboard should redirect
        resp = await client.get("/istota/", cookies=resp.cookies, follow_redirects=False)
        assert resp.status_code == 302


class TestWebConfigParsing:
    def test_web_config_defaults(self):
        cfg = Config()
        assert cfg.web.enabled is False
        assert cfg.web.port == 8766
        assert cfg.web.oidc_issuer == ""
        assert cfg.web.oidc_client_id == ""
        assert cfg.web.oidc_client_secret == ""
        assert cfg.web.session_secret_key == ""

    def test_web_config_from_toml(self, tmp_path):
        toml_content = """
[web]
enabled = true
port = 9000
oidc_issuer = "https://cloud.example.com"
oidc_client_id = "my-client"
oidc_client_secret = "my-secret"
session_secret_key = "my-key"
"""
        config_file = tmp_path / "config.toml"
        config_file.write_text(toml_content)
        cfg = load_config(config_file)
        assert cfg.web.enabled is True
        assert cfg.web.port == 9000
        assert cfg.web.oidc_issuer == "https://cloud.example.com"
        assert cfg.web.oidc_client_id == "my-client"
        assert cfg.web.oidc_client_secret == "my-secret"
        assert cfg.web.session_secret_key == "my-key"

    def test_env_var_overrides(self, tmp_path, monkeypatch):
        toml_content = """
[web]
enabled = true
oidc_issuer = "https://cloud.example.com"
oidc_client_id = "my-client"
"""
        config_file = tmp_path / "config.toml"
        config_file.write_text(toml_content)
        monkeypatch.setenv("ISTOTA_OIDC_CLIENT_SECRET", "env-secret")
        monkeypatch.setenv("ISTOTA_WEB_SECRET_KEY", "env-key")
        cfg = load_config(config_file)
        assert cfg.web.oidc_client_secret == "env-secret"
        assert cfg.web.session_secret_key == "env-key"
