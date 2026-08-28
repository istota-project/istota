"""Re-authorising the Nextcloud connection without a full logout (ISSUE-333).

The only remedy for a dead stored pair used to be a logout/login cycle the user
had to know to perform — the settings card said so in words ("Log out and back
in to connect"), which is the mystery this route replaces with a button.

The flow is deliberately thin: the login callback already mints and stores the
pair for whoever authenticates, so a reconnect is the *existing* authorize round
trip with a different landing page at the end. What is new is the marker that
carries the landing page across the round trip, and the allowlist that keeps that
marker from becoming an open redirect.
"""

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


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(web_tokens._KEY_ENV_VAR, KEY)


@pytest.fixture(autouse=True)
def _reset_locks():
    web_tokens._refresh_locks.clear()
    yield
    web_tokens._refresh_locks.clear()


@pytest.fixture(autouse=True)
def _registry():
    """Autouse, not inline in the one test that needs it: an inline teardown is
    skipped when an assertion above it fails, leaking a populated registry into
    every later test on the worker. The suite runs `-n auto`."""
    from istota import notification_sources as sources

    sources.reset_registry()
    yield
    sources.reset_registry()


def _make_config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
            token_storage="encrypted",
        ),
        bot_name="Istota",
    )


TOKEN_RESPONSE = {
    "user_id": "alice",
    "access_token": "the-access",
    "refresh_token": "the-refresh",
    "expires_in": 3600,
}


def _patch_app(config):
    import istota.web_app as mod

    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    # `authorize_redirect` is what the real authlib client returns: a 302 to the
    # provider. The test double stands in for the provider hop only.
    mod._oauth.nextcloud.authorize_redirect = AsyncMock(
        return_value=_redirect("https://cloud.example.com/authorize?x=1"),
    )
    return mod.app


def _redirect(url):
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url=url, status_code=302)


async def _client_for(config):
    app = _patch_app(config)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="https://example.com")


async def _callback(client, token_response=None):
    import istota.web_app as mod

    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value=dict(token_response or TOKEN_RESPONSE),
    )
    return await client.get("/istota/callback", follow_redirects=False)


@_needs_web_deps
class TestTheRoute:
    async def test_a_signed_in_user_is_sent_to_the_provider(self, tmp_path, keyed):
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            await _callback(client)  # establishes the session

            resp = await client.get("/istota/reconnect", follow_redirects=False)

        assert resp.status_code == 302
        assert "cloud.example.com" in resp.headers["location"]

    async def test_it_refuses_without_a_session(self, tmp_path, keyed):
        """Not an authorize hop for an anonymous caller: this is an action on an
        existing account, and the login page is where an anonymous one belongs."""
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            resp = await client.get("/istota/reconnect", follow_redirects=False)

        assert resp.status_code == 302
        assert resp.headers["location"].endswith("/istota/login")

    async def test_it_does_not_touch_the_stored_pair_on_the_way_out(
        self, tmp_path, keyed,
    ):
        """A reconnect that began and was abandoned at the provider must leave the
        user no worse off than before they clicked."""
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            await _callback(client)
            assert web_tokens.token_status(config.db_path, "alice") is not None

            await client.get("/istota/reconnect", follow_redirects=False)

        assert web_tokens.token_status(config.db_path, "alice") is not None


@_needs_web_deps
class TestWhereItLands:
    async def test_a_reconnect_returns_to_settings(self, tmp_path, keyed):
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            await _callback(client)
            await client.get("/istota/reconnect", follow_redirects=False)

            resp = await _callback(client)

        assert resp.status_code == 302
        assert resp.headers["location"] == "/istota/settings"

    async def test_a_plain_login_still_lands_on_the_app(self, tmp_path, keyed):
        """The regression guard on the change: every other login is unaffected."""
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            resp = await _callback(client)

        assert resp.headers["location"] == "/istota/"

    async def test_the_marker_is_consumed_not_kept(self, tmp_path, keyed):
        """A sticky marker would send every later login to settings for the life
        of the session."""
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            await _callback(client)
            await client.get("/istota/reconnect", follow_redirects=False)
            await _callback(client)  # consumes it

            resp = await _callback(client)

        assert resp.headers["location"] == "/istota/"

    async def test_an_abandoned_reconnect_does_not_redirect_a_later_login(
        self, tmp_path, keyed,
    ):
        """Only a *completed* callback consumes the marker, so closing the
        provider's consent page leaves it set. Starting a plain login is the
        statement that this is not that reconnect."""
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            await _callback(client)
            await client.get("/istota/reconnect", follow_redirects=False)
            # Abandoned at the provider; the user comes back later and logs in.
            await client.get("/istota/login?go=1", follow_redirects=False)

            resp = await _callback(client)

        assert resp.headers["location"] == "/istota/"


@_needs_web_deps
class TestTheSessionSurvives:
    async def test_the_user_is_still_signed_in_afterwards(self, tmp_path, keyed):
        """The point of the whole route. A reconnect that logged the user out
        would be the logout/login cycle it exists to replace."""
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            await _callback(client)
            await client.get("/istota/reconnect", follow_redirects=False)
            await _callback(client)

            me = await client.get("/istota/api/me")

        assert me.status_code == 200
        assert me.json()["username"] == "alice"


@_needs_web_deps
class TestItRestoresTheCredential:
    async def test_a_reconnect_stores_a_fresh_pair(self, tmp_path, keyed):
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            await _callback(client)
            web_tokens.delete_tokens(config.db_path, "alice")  # the dead credential
            assert web_tokens.token_status(config.db_path, "alice") is None

            await client.get("/istota/reconnect", follow_redirects=False)
            await _callback(client)

        assert web_tokens.get_access_token(
            config.db_path, config, "alice",
        ) == "the-access"

    async def test_it_closes_the_reconnect_notice(self, tmp_path, keyed, monkeypatch):
        """End to end through the seam the notification half of this fix adds:
        the row raised when the credential died is closed by the reconnect."""
        from istota.notification_resolvers import connected_service

        monkeypatch.setattr(
            "istota.notifications.send_notification", lambda *a, **k: True,
        )
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            await _callback(client)
            web_tokens.delete_tokens(config.db_path, "alice")
            connected_service.raise_for_service(
                config, "alice", "nextcloud", "revoked",
            )
            assert _state(config) == "open"

            await client.get("/istota/reconnect", follow_redirects=False)
            await _callback(client)

        assert _state(config) == "resolved"


def _state(config):
    with db.get_db(config.db_path) as conn:
        row = conn.execute(
            "SELECT state FROM notifications WHERE source = 'connected_service'",
        ).fetchone()
    return row["state"] if row else None


@_needs_web_deps
class TestDeliberateDisconnect:
    async def test_it_closes_an_open_reconnect_notice(
        self, tmp_path, keyed, monkeypatch,
    ):
        """A user who disconnects on purpose has answered the notice. Closing
        happens in the settings handler rather than in `delete_tokens`, which is
        also the self-heal deletion path that *raises* it."""
        from istota.notification_resolvers import connected_service

        monkeypatch.setattr(
            "istota.notifications.send_notification", lambda *a, **k: True,
        )
        config = _make_config(tmp_path)
        async with await _client_for(config) as client:
            await _callback(client)
            connected_service.raise_for_service(
                config, "alice", "nextcloud", "revoked",
            )
            assert _state(config) == "open"

            resp = await client.delete(
                "/istota/api/settings/nextcloud-token",
                headers={"origin": "https://example.com"},
            )

        assert resp.status_code == 200
        assert _state(config) == "resolved"


@_needs_web_deps
class TestTheRedirectAllowlist:
    """The marker rides in the session cookie, so a user can only ever set it for
    themselves — but the value still becomes a `Location` header, and "only the
    user can poison their own redirect" is the argument that ends with a
    phishing hop off the login flow. The mapping is a fixed table, not a URL."""

    def test_a_known_key_maps_to_its_path(self):
        import istota.web_app as mod

        assert mod._post_login_target("settings") == "/istota/settings"

    @pytest.mark.parametrize("hostile", [
        "https://evil.example/",
        "//evil.example/",
        "/istota/../../evil",
        "javascript:alert(1)",
        "",
        None,
        123,
    ])
    def test_anything_else_falls_back_to_the_app_root(self, hostile):
        import istota.web_app as mod

        assert mod._post_login_target(hostile) == "/istota/"
