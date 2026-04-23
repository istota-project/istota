"""Tests for istota.web_app — authenticated web interface."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps,
    reason="web dependencies not installed (install with: uv sync --extra web)",
)

if _has_web_deps:
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
                resources=[ResourceConfig(
                    type="miniflux", name="Feeds",
                    base_url="http://miniflux:8080", api_key="test-key",
                )],
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


def _patch_app(config):
    """Inject config and mock OAuth into the web app module."""
    import istota.web_app as mod
    mod._config = config
    mock_oauth = MagicMock()
    mock_oauth.nextcloud = MagicMock()
    mod._oauth = mock_oauth
    return mod.app


@pytest.fixture
def app(config):
    return _patch_app(config)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


def _login_cookies(client, app):
    """Helper: perform OIDC callback and return session cookies."""
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
        "userinfo": {"preferred_username": "alice", "name": "Alice"},
    })


@_needs_web_deps
class TestLoginRoute:
    async def test_login_shows_landing_page(self, client, app):
        resp = await client.get("/istota/login")
        assert resp.status_code == 200
        assert "Log in with Nextcloud" in resp.text

    async def test_login_with_go_redirects_to_oidc(self, client, app):
        import istota.web_app as mod

        from fastapi.responses import RedirectResponse
        mock_redirect = RedirectResponse(url="https://cloud.example.com/authorize?client_id=istota-web")
        mod._oauth.nextcloud.authorize_redirect = AsyncMock(return_value=mock_redirect)

        resp = await client.get("/istota/login?go=1", follow_redirects=False)
        assert resp.status_code in (302, 307)
        mod._oauth.nextcloud.authorize_redirect.assert_called_once()


@_needs_web_deps
class TestCallbackRoute:
    async def test_callback_valid_user_sets_session(self, client, app):
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })

        resp = await client.get("/istota/callback", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/istota/"
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

        mock_token = {"access_token": "abc"}
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value=mock_token)
        mod._oauth.nextcloud.userinfo = AsyncMock(return_value={
            "preferred_username": "alice",
            "name": "Alice",
        })

        resp = await client.get("/istota/callback", follow_redirects=False)
        assert resp.status_code == 302
        mod._oauth.nextcloud.userinfo.assert_called_once()


@_needs_web_deps
class TestUnauthenticatedAccess:
    async def test_api_me_returns_401(self, client):
        resp = await client.get("/istota/api/me")
        assert resp.status_code == 401

    async def test_api_feeds_returns_401(self, client):
        resp = await client.get("/istota/api/feeds")
        assert resp.status_code == 401


@_needs_web_deps
class TestApiMe:
    async def test_returns_user_info_with_feeds(self, client, app):
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = login_resp.cookies

        resp = await client.get("/istota/api/me", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "alice"
        assert data["display_name"] == "Alice"
        assert data["features"]["feeds"] is True

    async def test_returns_no_feeds_for_user_without_miniflux(self, client, app):
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "bob", "name": "Bob"},
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = login_resp.cookies

        resp = await client.get("/istota/api/me", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["features"]["feeds"] is False


@_needs_web_deps
class TestApiFeeds:
    async def test_feeds_proxies_to_miniflux(self, client, app):
        import istota.web_app as mod
        import httpx

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = login_resp.cookies

        mock_entries = {
            "total": 1,
            "entries": [{
                "id": 100,
                "title": "Test Entry",
                "url": "https://example.com/post",
                "content": "<p>Hello world</p>",
                "feed": {"id": 1, "title": "Test Feed"},
                "status": "unread",
                "published_at": "2026-03-31T10:00:00Z",
                "created_at": "2026-03-31T11:00:00Z",
                "enclosures": [],
            }],
        }
        mock_feeds = [
            {"id": 1, "title": "Test Feed", "site_url": "https://example.com"},
        ]

        mock_response_entries = MagicMock()
        mock_response_entries.json.return_value = mock_entries
        mock_response_entries.raise_for_status = MagicMock()

        mock_response_feeds = MagicMock()
        mock_response_feeds.json.return_value = mock_feeds
        mock_response_feeds.raise_for_status = MagicMock()

        async def mock_get(url, **kwargs):
            if "entries" in url:
                return mock_response_entries
            return mock_response_feeds

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("istota.web_app.httpx.AsyncClient", return_value=mock_client):
            resp = await client.get("/istota/api/feeds", cookies=cookies)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["entries"]) == 1
        assert data["entries"][0]["title"] == "Test Entry"
        assert data["entries"][0]["feed"]["title"] == "Test Feed"
        assert len(data["feeds"]) == 1

    async def test_feeds_returns_404_without_miniflux(self, client, app):
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "bob", "name": "Bob"},
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = login_resp.cookies

        resp = await client.get("/istota/api/feeds", cookies=cookies)
        assert resp.status_code == 404

    async def test_feeds_forwards_before_cursor(self, client, app):
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = login_resp.cookies

        mock_response_entries = MagicMock()
        mock_response_entries.json.return_value = {"total": 0, "entries": []}
        mock_response_entries.raise_for_status = MagicMock()
        mock_response_feeds = MagicMock()
        mock_response_feeds.json.return_value = []
        mock_response_feeds.raise_for_status = MagicMock()

        captured_params: dict = {}

        async def mock_get(url, **kwargs):
            if "entries" in url:
                captured_params.update(kwargs.get("params", {}))
                return mock_response_entries
            return mock_response_feeds

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("istota.web_app.httpx.AsyncClient", return_value=mock_client):
            resp = await client.get(
                "/istota/api/feeds?status=unread&before=1700000000",
                cookies=cookies,
            )

        assert resp.status_code == 200
        assert captured_params.get("before") == 1700000000
        assert captured_params.get("status") == "unread"

    async def test_feeds_omits_before_when_zero(self, client, app):
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = login_resp.cookies

        mock_response_entries = MagicMock()
        mock_response_entries.json.return_value = {"total": 0, "entries": []}
        mock_response_entries.raise_for_status = MagicMock()
        mock_response_feeds = MagicMock()
        mock_response_feeds.json.return_value = []
        mock_response_feeds.raise_for_status = MagicMock()

        captured_params: dict = {}

        async def mock_get(url, **kwargs):
            if "entries" in url:
                captured_params.update(kwargs.get("params", {}))
                return mock_response_entries
            return mock_response_feeds

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("istota.web_app.httpx.AsyncClient", return_value=mock_client):
            resp = await client.get("/istota/api/feeds", cookies=cookies)

        assert resp.status_code == 200
        assert "before" not in captured_params


@_needs_web_deps
class TestImageExtraction:
    def test_extract_images_from_enclosures(self):
        from istota.web_app import _extract_images
        entry = {
            "enclosures": [
                {"mime_type": "image/jpeg", "url": "https://img.com/1.jpg"},
                {"mime_type": "audio/mp3", "url": "https://audio.com/1.mp3"},
            ],
            "content": "",
        }
        assert _extract_images(entry) == ["https://img.com/1.jpg"]

    def test_extract_images_from_content_fallback(self):
        from istota.web_app import _extract_images
        entry = {
            "enclosures": [],
            "content": '<p>Text</p><img src="https://img.com/2.jpg">',
        }
        assert _extract_images(entry) == ["https://img.com/2.jpg"]

    def test_no_images(self):
        from istota.web_app import _extract_images
        entry = {"enclosures": [], "content": "<p>Just text</p>"}
        assert _extract_images(entry) == []


@_needs_web_deps
class TestSanitizeHtml:
    def test_strips_disallowed_tags(self):
        from istota.web_app import _sanitize_html
        result = _sanitize_html('<div><script>alert(1)</script><p>Hello</p></div>')
        assert "<script>" not in result
        assert "<p>Hello</p>" in result

    def test_preserves_allowed_tags(self):
        from istota.web_app import _sanitize_html
        result = _sanitize_html('<p><strong>Bold</strong> and <em>italic</em></p>')
        assert "<strong>" in result
        assert "<em>" in result

    def test_strips_event_handler_attributes(self):
        from istota.web_app import _sanitize_html
        result = _sanitize_html('<p onmouseover="alert(1)">text</p>')
        assert "onmouseover" not in result
        assert "<p>" in result
        assert "text" in result

    def test_strips_event_handler_on_blockquote(self):
        from istota.web_app import _sanitize_html
        result = _sanitize_html('<blockquote onclick="alert(1)">quote</blockquote>')
        assert "onclick" not in result
        assert "<blockquote>" in result

    def test_strips_style_attribute(self):
        from istota.web_app import _sanitize_html
        result = _sanitize_html('<b style="color:red">bold</b>')
        assert "style" not in result
        assert "<b>" in result

    def test_blocks_javascript_href(self):
        from istota.web_app import _sanitize_html
        result = _sanitize_html('<a href="javascript:alert(1)">click</a>')
        assert "javascript" not in result
        assert "<a>" in result

    def test_blocks_javascript_href_case_insensitive(self):
        from istota.web_app import _sanitize_html
        result = _sanitize_html('<a href="JaVaScRiPt:alert(1)">click</a>')
        assert "javascript" not in result.lower()

    def test_allows_https_href(self):
        from istota.web_app import _sanitize_html
        result = _sanitize_html('<a href="https://example.com">link</a>')
        assert 'href="https://example.com"' in result

    def test_allows_mailto_href(self):
        from istota.web_app import _sanitize_html
        result = _sanitize_html('<a href="mailto:user@example.com">email</a>')
        assert "mailto:" in result

    def test_blocks_data_href(self):
        from istota.web_app import _sanitize_html
        result = _sanitize_html('<a href="data:text/html,<script>alert(1)</script>">click</a>')
        assert "data:" not in result


@_needs_web_deps
class TestLogout:
    async def test_logout_clears_session(self, client, app):
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = login_resp.cookies

        resp = await client.get("/istota/logout", cookies=cookies, follow_redirects=False)
        assert resp.status_code == 302
        assert "/istota/login" in resp.headers["location"]

        # Session cleared — API should return 401
        resp = await client.get("/istota/api/me", cookies=resp.cookies)
        assert resp.status_code == 401


@_needs_web_deps
class TestCsrfOriginCheck:
    """Tests for Origin header CSRF protection on state-changing endpoints."""

    async def _login(self, client, app):
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        return login_resp.cookies

    async def test_put_without_origin_returns_403(self, client, app):
        cookies = await self._login(client, app)
        resp = await client.put(
            "/istota/api/feeds/entries/batch",
            json={"entry_ids": [1], "status": "read"},
            cookies=cookies,
        )
        assert resp.status_code == 403

    async def test_put_with_wrong_origin_returns_403(self, client, app):
        cookies = await self._login(client, app)
        resp = await client.put(
            "/istota/api/feeds/entries/batch",
            json={"entry_ids": [1], "status": "read"},
            cookies=cookies,
            headers={"origin": "https://evil.com"},
        )
        assert resp.status_code == 403

    async def test_put_with_correct_origin_allowed(self, client, app):
        cookies = await self._login(client, app)
        # Will fail at miniflux proxy (no real backend), but should not be 403
        resp = await client.put(
            "/istota/api/feeds/entries/batch",
            json={"entry_ids": [1], "status": "read"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code != 403

    async def test_empty_hostname_returns_403(self, client, app):
        """When site.hostname is empty and Host header is missing, CSRF check must reject."""
        import istota.web_app as mod
        cookies = await self._login(client, app)
        # Clear hostname after login to test CSRF with no hostname
        original = mod._config.site.hostname
        mod._config.site.hostname = ""
        try:
            resp = await client.put(
                "/istota/api/feeds/entries/batch",
                json={"entry_ids": [1], "status": "read"},
                cookies=cookies,
                headers={"origin": "https://evil.com", "host": ""},
            )
            assert resp.status_code == 403
        finally:
            mod._config.site.hostname = original


@_needs_web_deps
class TestSessionRotation:
    """Test that session is cleared before writing user info on login."""

    async def test_callback_clears_session_before_login(self, client, app):
        import istota.web_app as mod

        # Set pre-existing session data (simulating a pre-login session)
        # First, make a request to establish a session with some data
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = resp.cookies

        # Log in again as bob — old session data should be cleared
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "bob", "name": "Bob"},
        })
        resp = await client.get("/istota/callback", cookies=cookies, follow_redirects=False)
        cookies = resp.cookies

        resp = await client.get("/istota/api/me", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json()["username"] == "bob"


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


