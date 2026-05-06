"""Tests for istota.web_app — authenticated web interface."""

import re
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
                resources=[ResourceConfig(type="feeds", name="Feeds")],
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
    mod.app.state.istota_config = config
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

    async def test_returns_no_feeds_for_user_without_feeds_resource(self, client, app):
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
        # May succeed or fail downstream depending on whether the user's feeds
        # workspace exists, but must not be rejected at the CSRF gate.
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


@_needs_web_deps
class TestAdminStats:
    """Phase 1 admin dashboard endpoint."""

    def _config_with_admin(self, tmp_path):
        from istota import db
        config = _make_config(tmp_path)
        config.db_path = tmp_path / "istota.db"
        config.admin_users = {"alice"}
        db.init_db(config.db_path)
        return config

    async def _login(self, client, username):
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": username, "name": username.title()},
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    async def test_me_includes_is_admin(self, tmp_path):
        config = self._config_with_admin(tmp_path)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/me", cookies=cookies)
            assert resp.status_code == 200
            data = resp.json()
            assert data["is_admin"] is True
            assert data["features"]["admin"] is True

    async def test_me_non_admin(self, tmp_path):
        config = self._config_with_admin(tmp_path)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "bob")
            resp = await client.get("/istota/api/me", cookies=cookies)
            assert resp.status_code == 200
            data = resp.json()
            assert data["is_admin"] is False
            assert data["features"]["admin"] is False

    async def test_admin_stats_requires_auth(self, tmp_path):
        config = self._config_with_admin(tmp_path)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            resp = await client.get("/istota/api/admin/stats")
            assert resp.status_code == 401

    async def test_admin_stats_forbidden_for_non_admin(self, tmp_path):
        config = self._config_with_admin(tmp_path)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "bob")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            assert resp.status_code == 403

    async def test_admin_stats_empty_db(self, tmp_path):
        config = self._config_with_admin(tmp_path)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            assert resp.status_code == 200
            data = resp.json()

            assert "system" in data
            assert data["system"]["python_version"]
            assert isinstance(data["system"]["uptime_seconds"], int)
            assert data["system"]["db_size_bytes"] >= 0

            assert isinstance(data["users"], list)
            usernames = {u["username"] for u in data["users"]}
            assert {"alice", "bob"}.issubset(usernames)
            alice_row = next(u for u in data["users"] if u["username"] == "alice")
            assert alice_row["is_admin"] is True
            assert alice_row["tasks_total"] == 0

            assert data["scheduler"]["jobs_total"] == 0
            assert data["tasks"]["total"] == 0
            assert "storage" in data
            assert "modules" in data

    async def test_admin_stats_aggregates_tasks(self, tmp_path):
        from istota import db
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.create_task(conn, "task one", "alice", source_type="talk")
            db.create_task(conn, "task two", "alice", source_type="email")
            db.create_task(conn, "task three", "bob", source_type="talk")

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            assert resp.status_code == 200
            data = resp.json()

            assert data["tasks"]["total"] == 3
            assert data["tasks"]["last_24h"] == 3
            assert data["tasks"]["by_source"]["talk"] == 2
            assert data["tasks"]["by_source"]["email"] == 1

            alice_row = next(u for u in data["users"] if u["username"] == "alice")
            assert alice_row["tasks_total"] == 2
            assert alice_row["tasks_last_24h"] == 2
            bob_row = next(u for u in data["users"] if u["username"] == "bob")
            assert bob_row["tasks_total"] == 1

    async def test_me_admin_fails_closed_when_admins_unset(self, tmp_path):
        """Empty ``admin_users`` set must NOT grant admin via the web UI.

        ``Config.is_admin`` returns True for everyone when the admins file
        is missing (back-compat for sandbox/skill checks). The web admin
        dashboard requires an explicit allowlist — fail closed.
        """
        config = self._config_with_admin(tmp_path)
        config.admin_users = set()
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/me", cookies=cookies)
            assert resp.status_code == 200
            data = resp.json()
            assert data["is_admin"] is False
            assert data["features"]["admin"] is False

            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            assert resp.status_code == 403

    async def test_admin_stats_error_rate_uses_terminal_states(self, tmp_path):
        """A quiet day with one failure and pending work must not read 100%."""
        from istota import db
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            t1 = db.create_task(conn, "ok 1", "alice", source_type="talk")
            t2 = db.create_task(conn, "ok 2", "alice", source_type="talk")
            t3 = db.create_task(conn, "fail", "alice", source_type="talk")
            db.create_task(conn, "pending", "alice", source_type="talk")  # pending
            db.create_task(conn, "running", "alice", source_type="talk")  # pending → no terminal
            conn.execute("UPDATE tasks SET status = 'completed' WHERE id IN (?, ?)", (t1, t2))
            conn.execute("UPDATE tasks SET status = 'failed' WHERE id = ?", (t3,))
            conn.commit()

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            data = resp.json()
            # 1 failed / (2 completed + 1 failed) = 0.3333… not 0.2 (1/5)
            assert data["tasks"]["error_rate_24h"] == pytest.approx(0.3333, rel=1e-3)

    async def test_admin_stats_error_rate_zero_terminal_tasks(self, tmp_path):
        """All-pending day must not crash on zero division."""
        from istota import db
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.create_task(conn, "pending 1", "alice", source_type="talk")
            db.create_task(conn, "pending 2", "alice", source_type="talk")

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            data = resp.json()
            assert data["tasks"]["error_rate_24h"] == 0.0

    async def test_admin_stats_timestamps_normalized_to_iso_z(self, tmp_path):
        """Backend must emit a single canonical timestamp shape."""
        from istota import db
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            tid = db.create_task(conn, "task", "alice", source_type="talk")
            conn.execute(
                "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, "
                "enabled, last_run_at, last_success_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("alice", "j", "* * * * *", "p", 1,
                 "2026-05-04 12:00:00", "2026-05-04 11:45:00"),
            )
            conn.commit()
            assert tid > 0

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            data = resp.json()

            iso_re = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
            assert re.match(iso_re, data["system"]["last_scheduler_run"])
            alice_row = next(u for u in data["users"] if u["username"] == "alice")
            assert re.match(iso_re, alice_row["last_active"])
            job = data["scheduler"]["jobs"][0]
            assert re.match(iso_re, job["last_run_at"])
            assert re.match(iso_re, job["last_success_at"])

    async def test_admin_stats_storage_backups(self, tmp_path):
        """Storage card must reflect actual backup files alongside the DB."""
        import os
        config = self._config_with_admin(tmp_path)
        backups = config.db_path.parent / "backups"
        (backups / "daily").mkdir(parents=True)
        (backups / "weekly").mkdir(parents=True)
        old = backups / "daily" / "istota-2026-05-03_020000.db.gz"
        mid = backups / "daily" / "istota-2026-05-04_020000.db.gz"
        new = backups / "weekly" / "istota-2026-05-05_020000.db.gz"
        for p in (old, mid, new):
            p.write_bytes(b"x")
        # Force distinct mtimes so "latest" is unambiguous.
        os.utime(old, (1714694400, 1714694400))   # 2024-05-03 UTC
        os.utime(mid, (1714780800, 1714780800))   # 2024-05-04 UTC
        os.utime(new, (1714867200, 1714867200))   # 2024-05-05 UTC
        # Stray non-backup file must be ignored.
        (backups / "daily" / "README.txt").write_text("notes")

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            data = resp.json()
            assert data["storage"]["backups_count"] == 3
            assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                            data["storage"]["last_backup"])
            assert data["storage"]["last_backup"].startswith("2024-05-05T")

    async def test_admin_stats_storage_backups_missing_dir(self, tmp_path):
        """No backups dir → zero count, null timestamp, no exception."""
        config = self._config_with_admin(tmp_path)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            data = resp.json()
            assert data["storage"]["backups_count"] == 0
            assert data["storage"]["last_backup"] is None

    async def test_admin_stats_feeds_module_unreachable_no_mount(self, tmp_path):
        """Native feeds users without resolvable mount must not vanish."""
        config = self._config_with_admin(tmp_path)
        config.users["alice"] = UserConfig(
            display_name="Alice",
            resources=[ResourceConfig(type="feeds", name="Feeds")],
        )
        config.nextcloud_mount_path = None  # docker-compose-style deploy

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            data = resp.json()
            assert "feeds" in data["modules"]
            feeds = data["modules"]["feeds"]
            assert feeds["users_configured"] == 1
            assert feeds["users_resolved"] == 0
            assert feeds["status"] == "unreachable"

    async def test_admin_stats_last_active_uses_created_at(self, tmp_path):
        """Background ``updated_at`` bumps must not skew last_active."""
        from istota import db
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            tid = db.create_task(conn, "old", "alice", source_type="talk")
            # Simulate a much later background retry stamping updated_at.
            conn.execute(
                "UPDATE tasks SET created_at = ?, updated_at = ? WHERE id = ?",
                ("2026-04-01 12:00:00", "2026-05-04 12:00:00", tid),
            )
            conn.commit()

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            alice = next(u for u in resp.json()["users"] if u["username"] == "alice")
            assert alice["last_active"] == "2026-04-01T12:00:00Z"

    async def test_admin_stats_includes_scheduled_jobs(self, tmp_path):
        from istota import db
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                """
                INSERT INTO scheduled_jobs
                  (user_id, name, cron_expression, prompt, enabled,
                   consecutive_failures, last_error, last_run_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("alice", "morning", "0 7 * * *", "say hi", 1, 0, None, None),
            )
            conn.execute(
                """
                INSERT INTO scheduled_jobs
                  (user_id, name, cron_expression, prompt, enabled,
                   consecutive_failures, last_error, last_run_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("alice", "broken", "0 * * * *", "do thing", 0, 3,
                 "timeout after 30s", "2026-05-04 12:00:00"),
            )
            conn.commit()

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            data = resp.json()
            assert data["scheduler"]["jobs_total"] == 2
            assert data["scheduler"]["jobs_active"] == 1
            assert data["scheduler"]["jobs_paused"] == 1
            errs = data["scheduler"]["last_errors"]
            assert any(e["job_name"] == "alice/broken" for e in errs)


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


@_needs_web_deps
class TestResolveTz:
    """Client-supplied timezone is accepted when valid, else falls back (ISSUE-049)."""

    def test_valid_client_tz_overrides_fallback(self):
        from istota.web_app import _resolve_tz
        assert _resolve_tz("Asia/Tokyo", "America/Los_Angeles") == "Asia/Tokyo"

    def test_empty_client_tz_returns_fallback(self):
        from istota.web_app import _resolve_tz
        assert _resolve_tz("", "America/Los_Angeles") == "America/Los_Angeles"

    def test_invalid_client_tz_returns_fallback(self):
        from istota.web_app import _resolve_tz
        assert _resolve_tz("Not/A/Real/Zone", "America/Los_Angeles") == "America/Los_Angeles"

    def test_malformed_client_tz_returns_fallback(self):
        from istota.web_app import _resolve_tz
        # Null bytes and other oddities should not crash
        assert _resolve_tz("../etc/passwd", "UTC") == "UTC"


@_needs_web_deps
class TestSettingsEndpoints:
    """Web UI settings page: per-service credential cards + write-only secrets API."""

    @pytest.fixture(autouse=True)
    def _secret_key(self, monkeypatch, tmp_path):
        # Real DB so the secrets table exists.
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key" * 8)
        self._db_path = db_path

    def _make_test_config(self, tmp_path):
        cfg = _make_config(
            tmp_path,
            users={
                "alice": UserConfig(
                    display_name="Alice",
                    resources=[
                        ResourceConfig(type="money", name="Money"),
                        ResourceConfig(type="karakeep", name="Karakeep",
                                       base_url="https://k.example", api_key=""),
                    ],
                ),
                "bob": UserConfig(display_name="Bob"),  # no money/karakeep resources
            },
        )
        cfg.db_path = self._db_path
        return cfg

    async def _login_alice(self, client, app):
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    async def test_services_returns_cards(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)
        resp = await client.get("/istota/api/settings/services", cookies=cookies)
        assert resp.status_code == 200
        services = {s["service"]: s for s in resp.json()["services"]}
        # money resource → monarch card available
        assert services["monarch"]["status"] in ("missing", "partial", "configured")
        # karakeep resource present
        assert services["karakeep"]["status"] in ("missing", "partial", "configured")

    async def test_services_unavailable_when_no_resource(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        # Bob has no resources at all.
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "bob", "name": "Bob"},
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = resp.cookies
        resp = await client.get("/istota/api/settings/services", cookies=cookies)
        services = {s["service"]: s for s in resp.json()["services"]}
        assert services["monarch"]["status"] == "unavailable"
        assert services["karakeep"]["status"] == "unavailable"

    async def test_services_never_returns_plaintext(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        from istota import secrets_store
        secrets_store.set_secret(self._db_path, "alice", "monarch", "email", "secret@x.com")
        cookies = await self._login_alice(client, app)
        resp = await client.get("/istota/api/settings/services", cookies=cookies)
        body = resp.text
        assert "secret@x.com" not in body

    async def test_set_secret_persists(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)
        resp = await client.put(
            "/istota/api/settings/secrets/monarch/email",
            json={"value": "alice@example.com"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["configured"] is True
        from istota import secrets_store
        assert secrets_store.get_secret(self._db_path, "alice", "monarch", "email") == "alice@example.com"

    async def test_set_secret_rejects_unknown_service(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)
        resp = await client.put(
            "/istota/api/settings/secrets/bogus/api_key",
            json={"value": "x"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 404

    async def test_set_secret_rejects_unknown_key(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)
        resp = await client.put(
            "/istota/api/settings/secrets/monarch/nonexistent_field",
            json={"value": "x"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_set_secret_csrf_origin_required(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)
        resp = await client.put(
            "/istota/api/settings/secrets/monarch/email",
            json={"value": "x"},
            cookies=cookies,
            # wrong origin
            headers={"origin": "https://evil.example.com"},
        )
        assert resp.status_code == 403

    async def test_set_secret_requires_auth(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        resp = await client.put(
            "/istota/api/settings/secrets/monarch/email",
            json={"value": "x"},
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 401

    async def test_delete_secret(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        from istota import secrets_store
        secrets_store.set_secret(self._db_path, "alice", "monarch", "email", "x@y")
        cookies = await self._login_alice(client, app)
        resp = await client.delete(
            "/istota/api/settings/secrets/monarch/email",
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert secrets_store.get_secret(self._db_path, "alice", "monarch", "email") is None

    async def test_delete_rejects_unknown_key(self, tmp_path, client, app):
        """Symmetric with the PUT handler — DELETE must not let callers remove
        rows whose key is not part of the schema."""
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)
        resp = await client.delete(
            "/istota/api/settings/secrets/monarch/nonexistent_field",
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_set_secret_per_user_scoping(self, tmp_path, client, app):
        """Alice setting a value must not affect bob's namespace."""
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)
        await client.put(
            "/istota/api/settings/secrets/monarch/email",
            json={"value": "alice@x"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        from istota import secrets_store
        assert secrets_store.get_secret(self._db_path, "alice", "monarch", "email") == "alice@x"
        assert secrets_store.get_secret(self._db_path, "bob", "monarch", "email") is None


# ============================================================================
# Phase 6 — profile + resources endpoints
# ============================================================================

@_needs_web_deps
class TestProfileEndpoints:
    """Settings → profile section: scalars, list fields, validation, scoping."""

    @pytest.fixture(autouse=True)
    def _setup_db(self, monkeypatch, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        # Phase 5 store needs the secret key, but profile tests don't depend on it.
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key" * 8)
        self._db_path = db_path

    def _make_test_config(self, tmp_path):
        cfg = _make_config(
            tmp_path,
            users={"alice": UserConfig(display_name="Alice"), "bob": UserConfig(display_name="Bob")},
        )
        cfg.db_path = self._db_path
        return cfg

    async def _login(self, client, username="alice", display="Alice"):
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": username, "name": display},
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    async def test_callback_seeds_profile(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        await self._login(client, "alice", "Alice the Great")
        from istota import user_profiles
        p = user_profiles.get_profile(self._db_path, "alice")
        assert p is not None
        assert p.display_name == "Alice the Great"

    async def test_get_profile_returns_seeded_row(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.get("/istota/api/settings/profile", cookies=cookies)
        assert resp.status_code == 200
        body = resp.json()
        assert body["profile"]["user_id"] == "alice"
        assert body["profile"]["timezone"] == "UTC"

    async def test_update_profile_partial(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"timezone": "America/Los_Angeles", "ntfy_topic": "alice-alerts"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        from istota import user_profiles
        p = user_profiles.get_profile(self._db_path, "alice")
        assert p.timezone == "America/Los_Angeles"
        assert p.ntfy_topic == "alice-alerts"
        assert p.display_name == "Alice"  # untouched

    async def test_update_profile_lists(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"email_addresses": ["a@x.com", "b@x.com"], "trusted_email_senders": ["*@trusted.org"]},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        from istota import user_profiles
        p = user_profiles.get_profile(self._db_path, "alice")
        assert p.email_addresses == ["a@x.com", "b@x.com"]
        assert p.trusted_email_senders == ["*@trusted.org"]

    async def test_update_profile_rejects_unknown_field(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"evil": "value"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_update_profile_rejects_bad_type(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"max_foreground_workers": "not-a-number"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_update_profile_csrf_required(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"timezone": "UTC"},
            cookies=cookies,
            headers={"origin": "https://evil.example.com"},
        )
        assert resp.status_code == 403

    async def test_update_profile_requires_auth(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"timezone": "UTC"},
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 401

    async def test_per_user_scoping(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        await client.put(
            "/istota/api/settings/profile",
            json={"ntfy_topic": "alice-only"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        from istota import user_profiles
        bob = user_profiles.get_profile(self._db_path, "bob")
        # Bob has no row yet; alice's update must not have created one.
        assert bob is None


@_needs_web_deps
class TestResourceEndpoints:
    """Settings → resources: list TOML+DB merged, add/remove DB rows."""

    @pytest.fixture(autouse=True)
    def _setup_db(self, monkeypatch, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key" * 8)
        self._db_path = db_path

    def _make_test_config(self, tmp_path):
        cfg = _make_config(
            tmp_path,
            users={
                "alice": UserConfig(
                    display_name="Alice",
                    resources=[ResourceConfig(type="feeds", name="Feeds (TOML)")],
                ),
            },
        )
        cfg.db_path = self._db_path
        return cfg

    async def _login(self, client):
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    async def test_list_includes_toml_and_db(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        from istota import db
        with db.get_db(self._db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/Notes", display_name="Notes",
            )
        cookies = await self._login(client)
        resp = await client.get("/istota/api/settings/resources", cookies=cookies)
        assert resp.status_code == 200
        body = resp.json()
        types = {t["type"] for t in body["types"]}
        assert "calendar" in types and "feeds" in types
        managed = {(r["type"], r["managed"]) for r in body["resources"]}
        assert ("feeds", "config") in managed
        assert ("folder", "db") in managed

    async def test_add_resource(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.post(
            "/istota/api/settings/resources",
            json={"type": "calendar", "path": "/dav/work", "name": "Work"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        rid = resp.json()["id"]
        assert rid > 0
        from istota import db
        with db.get_db(self._db_path) as conn:
            rows = db.get_user_resources(conn, "alice", resource_type="calendar")
        assert len(rows) == 1
        assert rows[0].resource_path == "/dav/work"

    async def test_add_resource_rejects_unknown_type(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.post(
            "/istota/api/settings/resources",
            json={"type": "evil", "path": "/x"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_add_resource_requires_path_when_needed(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.post(
            "/istota/api/settings/resources",
            json={"type": "folder"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_add_resource_no_path_for_module_types(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.post(
            "/istota/api/settings/resources",
            json={"type": "money", "name": "Money"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200

    async def test_delete_db_resource(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        from istota import db
        with db.get_db(self._db_path) as conn:
            rid = db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/x", display_name="X",
            )
        cookies = await self._login(client)
        resp = await client.delete(
            f"/istota/api/settings/resources/{rid}",
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        with db.get_db(self._db_path) as conn:
            assert db.get_user_resources(conn, "alice", resource_type="folder") == []

    async def test_delete_other_users_resource_returns_404(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        from istota import db
        with db.get_db(self._db_path) as conn:
            rid = db.add_user_resource(
                conn, user_id="bob", resource_type="folder",
                resource_path="/bobs", display_name="Bob's",
            )
        cookies = await self._login(client)  # logged in as alice
        resp = await client.delete(
            f"/istota/api/settings/resources/{rid}",
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        # 404 is intentional: indistinguishable from "no such id" so a
        # caller can't probe for other users' resource IDs.
        assert resp.status_code == 404
        with db.get_db(self._db_path) as conn:
            still_there = db.get_user_resources(conn, "bob", resource_type="folder")
        assert len(still_there) == 1

    async def test_delete_negative_id_rejected(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.delete(
            "/istota/api/settings/resources/-1",
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_add_resource_with_extras_persists(self, tmp_path, client, app):
        # Module-shaped resources carry their config in extras (overland's
        # ingest_token, money's data_dir, feeds' tumblr_api_key, etc.).
        # Without this the web UI can't fully replace the per-user TOML.
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.post(
            "/istota/api/settings/resources",
            json={
                "type": "overland", "name": "GPS",
                "extras": {"ingest_token": "tok-xyz", "default_radius": 75},
            },
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        from istota import db
        with db.get_db(self._db_path) as conn:
            rows = db.get_user_resources(conn, "alice", resource_type="overland")
        assert rows[0].extras == {"ingest_token": "tok-xyz", "default_radius": 75}

    async def test_list_returns_extras_on_db_rows(self, tmp_path, client, app):
        # The UI needs to read back extras to render an editable form;
        # otherwise users can only delete-and-recreate.
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        from istota import db
        with db.get_db(self._db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="overland",
                resource_path="overland", display_name="GPS",
                extras={"ingest_token": "tok-xyz"},
            )
        cookies = await self._login(client)
        resp = await client.get("/istota/api/settings/resources", cookies=cookies)
        assert resp.status_code == 200
        db_rows = [r for r in resp.json()["resources"] if r["managed"] == "db"]
        overland = next(r for r in db_rows if r["type"] == "overland")
        assert overland["extras"] == {"ingest_token": "tok-xyz"}

    async def test_add_resource_rejects_non_dict_extras(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.post(
            "/istota/api/settings/resources",
            json={"type": "feeds", "name": "Feeds", "extras": "not a dict"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_db_row_not_double_listed_when_in_uc_resources(self, tmp_path, client, app):
        # _apply_user_resources merges DB rows into UserConfig.resources so
        # other code paths see them through one surface. The settings GET
        # must not show the same row twice (once as "config" via the
        # merged UserConfig, once as "db" via the direct DB query).
        from istota import db
        with db.get_db(self._db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="overland",
                resource_path="overland", display_name="GPS",
                extras={"ingest_token": "tok-xyz"},
            )
        # Build a config whose UserConfig already holds the DB row (as
        # _apply_user_resources would have done) plus a separate TOML feed
        # entry.
        cfg = _make_config(
            tmp_path,
            users={
                "alice": UserConfig(
                    display_name="Alice",
                    resources=[
                        ResourceConfig(type="feeds", name="Feeds (TOML)"),
                        ResourceConfig(
                            type="overland", path="overland",
                            name="GPS", extra={"ingest_token": "tok-xyz"},
                        ),
                    ],
                ),
            },
        )
        cfg.db_path = self._db_path
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.get("/istota/api/settings/resources", cookies=cookies)
        assert resp.status_code == 200
        rows = resp.json()["resources"]
        overlands = [r for r in rows if r["type"] == "overland"]
        assert len(overlands) == 1
        assert overlands[0]["managed"] == "db"
        # The unrelated TOML feed entry still renders as config.
        feeds = [r for r in rows if r["type"] == "feeds"]
        assert feeds[0]["managed"] == "config"


@_needs_web_deps
class TestBriefingEndpoints:
    """Settings → briefings: list TOML+DB merged, upsert/delete DB rows."""

    @pytest.fixture(autouse=True)
    def _setup_db(self, monkeypatch, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key" * 8)
        self._db_path = db_path

    def _make_test_config(self, tmp_path):
        from istota.config import BriefingConfig
        cfg = _make_config(
            tmp_path,
            users={
                "alice": UserConfig(
                    display_name="Alice",
                    log_channel="logtoken",
                    alerts_channel="alertstoken",
                    briefings=[
                        BriefingConfig(
                            name="morning",
                            cron="0 7 * * 1-5",
                            conversation_token="logtoken",
                            output="talk",
                            components={"calendar": True},
                        ),
                    ],
                ),
            },
        )
        cfg.db_path = self._db_path
        return cfg

    async def _login(self, client):
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "userinfo": {"preferred_username": "alice", "name": "Alice"},
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    async def test_list_includes_toml_and_db(self, tmp_path, client, app):
        from istota import user_briefings
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        user_briefings.ensure_briefing(
            self._db_path, user_id="alice", name="evening",
            cron="0 19 * * *", conversation_token="logtoken", output="talk",
        )
        cookies = await self._login(client)
        resp = await client.get("/istota/api/settings/briefings", cookies=cookies)
        assert resp.status_code == 200
        body = resp.json()
        managed = {(b["name"], b["managed"]) for b in body["briefings"]}
        assert ("morning", "config") in managed
        assert ("evening", "db") in managed
        # rooms come from log_channel / alerts_channel
        room_tokens = {r["token"] for r in body["rooms"]}
        assert "logtoken" in room_tokens
        assert "alertstoken" in room_tokens

    async def test_post_creates_briefing(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.post(
            "/istota/api/settings/briefings",
            json={
                "name": "midday",
                "cron": "0 12 * * *",
                "conversation_token": "logtoken",
                "output": "talk",
                "components": {"calendar": True},
            },
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "created"
        assert body["id"] > 0

    async def test_post_idempotent_returns_noop(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        payload = {
            "name": "midday",
            "cron": "0 12 * * *",
            "conversation_token": "logtoken",
            "output": "talk",
            "components": {"calendar": True},
        }
        await client.post(
            "/istota/api/settings/briefings",
            json=payload, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        resp = await client.post(
            "/istota/api/settings/briefings",
            json=payload, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "noop"

    async def test_post_rejects_talk_without_token(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.post(
            "/istota/api/settings/briefings",
            json={"name": "x", "cron": "0 7 * * *", "output": "talk"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_post_rejects_unknown_output(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.post(
            "/istota/api/settings/briefings",
            json={"name": "x", "cron": "0 7 * * *", "output": "sms"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_delete_db_briefing(self, tmp_path, client, app):
        from istota import user_briefings
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        b, _ = user_briefings.ensure_briefing(
            self._db_path, user_id="alice", name="x",
            cron="0 7 * * *", conversation_token="t",
        )
        cookies = await self._login(client)
        resp = await client.delete(
            f"/istota/api/settings/briefings/{b.id}",
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    async def test_delete_other_users_briefing_returns_404(self, tmp_path, client, app):
        from istota import user_briefings
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        b, _ = user_briefings.ensure_briefing(
            self._db_path, user_id="bob", name="x",
            cron="0 7 * * *", conversation_token="t",
        )
        cookies = await self._login(client)  # logged in as alice
        resp = await client.delete(
            f"/istota/api/settings/briefings/{b.id}",
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 404
        # Bob's briefing untouched
        rows = user_briefings.list_briefings(self._db_path, "bob")
        assert len(rows) == 1

    async def test_delete_negative_id_rejected(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.delete(
            "/istota/api/settings/briefings/-1",
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400
