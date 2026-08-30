"""Tests for istota.web_app — authenticated web interface."""

import json
import re
import sqlite3
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
    LocationReceiverConfig,
    SiteConfig,
    UserConfig,
    WebConfig,
    load_config,
)


def _make_config(tmp_path, users=None, mount_path=None, web=None):
    """Build a Config for testing."""
    if users is None:
        # Modules (feeds, money, location) are on by default. Bob explicitly
        # opts out of feeds via ``disabled_modules`` so the
        # "feeds=False" assertion in TestApiMe still has a way to fire.
        users = {
            "alice": UserConfig(display_name="Alice"),
            "bob": UserConfig(
                display_name="Bob",
                disabled_modules=["feeds", "money", "location"],
            ),
        }
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=Path(mount_path) if mount_path else tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        users=users,
        web=web or WebConfig(
            enabled=True,
            port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web",
            oauth2_client_secret="test-secret",
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
        "user_id": "alice",
    })


@_needs_web_deps
class TestRootRedirect:
    async def test_bare_root_redirects_to_app(self, client):
        # The UI lives under /istota; opening the bare port must land in the app
        # (matters for standalone / direct-uvicorn where no nginx rewrites /).
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/istota/"


@_needs_web_deps
class TestLoginRoute:
    async def test_login_shows_landing_page(self, client, app):
        resp = await client.get("/istota/login")
        assert resp.status_code == 200
        assert "Log in with Nextcloud" in resp.text

    async def test_login_page_shows_logo_version_and_email_placeholder(self, client, app):
        from istota import __version__

        resp = await client.get("/istota/login")
        # Logo + project link + running version in the footer.
        assert "/istota/octopus-sigil.webp" in resp.text
        assert "istota.cynium.com" in resp.text
        assert f"v{__version__}" in resp.text
        # Email login is advertised but not yet an actionable control — it must
        # not render as a link, or it would 404 into the Nextcloud flow.
        assert "Coming soon" in resp.text
        assert 'href="/istota/login?go=1"' in resp.text
        assert resp.text.count('href="/istota/login?go=1"') == 1

    async def test_login_page_escapes_bot_name(self, client, app):
        import istota.web_app as mod

        original = mod._config.bot_name
        mod._config.bot_name = '<script>alert(1)</script>'
        try:
            resp = await client.get("/istota/login")
        finally:
            mod._config.bot_name = original
        assert "<script>alert(1)</script>" not in resp.text
        assert "&lt;script&gt;" in resp.text

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
            "user_id": "alice",
        })

        resp = await client.get("/istota/callback", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/istota/"
        assert any("istota_session" in c for c in resp.headers.get_list("set-cookie"))

    async def test_callback_unknown_user_returns_403(self, client, app):
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "user_id": "unknown_person",
        })

        resp = await client.get("/istota/callback", follow_redirects=False)
        assert resp.status_code == 403

    async def test_callback_falls_back_to_ocs_userinfo(self, client, app):
        """When NC's OAuth2 token response omits ``user_id`` (older NC or
        custom auth backend), the callback fetches identity from the OCS
        userinfo endpoint and discards the bearer token."""
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"access_token": "abc"})
        mod._nc_oauth2_userinfo = AsyncMock(return_value={
            "id": "alice",
            "displayname": "Alice",
        })

        resp = await client.get("/istota/callback", follow_redirects=False)
        assert resp.status_code == 302
        mod._nc_oauth2_userinfo.assert_called_once()

    async def test_callback_state_mismatch_returns_400_not_500(self, client, app):
        """A failed state check is a recoverable client-side condition, not a
        server fault.

        It happens whenever the session cookie carrying the OAuth ``state`` is
        not the one the callback is read with: cookies cleared mid-login, a
        bookmarked or re-submitted callback URL, or an embedded WebView handing
        the authorize hop to the system browser (a separate cookie jar). Left
        unhandled it surfaces as a bare 500 with a traceback, which says
        nothing about how to recover.
        """
        import istota.web_app as mod
        from authlib.integrations.base_client.errors import MismatchingStateError

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(
            side_effect=MismatchingStateError()
        )

        resp = await client.get("/istota/callback", follow_redirects=False)
        assert resp.status_code == 400
        # The user must be told how to get out of it.
        assert "/istota/login" in resp.text

    async def test_callback_provider_error_returns_400(self, client, app):
        """The provider declining (e.g. the user cancels consent) comes back as
        an OAuthError too, and is equally not a server fault."""
        import istota.web_app as mod
        from authlib.integrations.base_client.errors import OAuthError

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(
            side_effect=OAuthError(error="access_denied")
        )

        resp = await client.get("/istota/callback", follow_redirects=False)
        assert resp.status_code == 400

    async def test_callback_token_exchange_failure_returns_502(self, client, app):
        """A token-endpoint failure is an upstream problem, so it must stay
        distinguishable from the 400s above rather than collapsing into one
        generic error — 502 matches what the userinfo fallback already returns."""
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(
            side_effect=RuntimeError("connection refused")
        )

        resp = await client.get("/istota/callback", follow_redirects=False)
        assert resp.status_code == 502

    async def test_callback_failure_does_not_leak_exception_text(self, client, app):
        """Whatever the provider put in the error, it is attacker-influenceable
        and reflected into a browser response, so it must not be echoed."""
        import istota.web_app as mod
        from authlib.integrations.base_client.errors import OAuthError

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(
            side_effect=OAuthError(description="<script>alert(1)</script>")
        )

        resp = await client.get("/istota/callback", follow_redirects=False)
        assert resp.status_code == 400
        # Not `"<script>" not in ...`: the page carries its own inline theme
        # script, so only the payload itself is evidence of reflection.
        assert "alert(1)" not in resp.text


@_needs_web_deps
class TestGoogleConnectRoute:
    """The connect redirect must ask Google for offline access explicitly.

    ``google_callback`` hard-rejects a token response carrying no refresh
    token (it can't refresh an hour later, so there is nothing worth
    storing) and bounces to ``?google=error``. Google only issues a refresh
    token when ``access_type=offline`` is requested, and only on the *first*
    consent for a given grant unless ``prompt=consent`` re-prompts. Without
    both, a reconnect — the normal path after widening scopes, or after a
    user revokes access in their Google account — silently returns no
    refresh token and the connect fails with no clue why.
    """

    async def _connect(self, client, app):
        import istota.web_app as mod

        from fastapi.responses import RedirectResponse
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "user_id": "alice",
        })
        await client.get("/istota/callback", follow_redirects=False)

        mod._oauth.google.authorize_redirect = AsyncMock(
            return_value=RedirectResponse(url="https://accounts.google.com/o/oauth2/auth")
        )
        resp = await client.get("/istota/google/connect", follow_redirects=False)
        return resp, mod._oauth.google.authorize_redirect

    async def test_requests_offline_access_and_reconsent(self, client, app):
        resp, redirect = await self._connect(client, app)
        assert resp.status_code in (302, 307)
        redirect.assert_called_once()
        kwargs = redirect.call_args.kwargs
        assert kwargs["access_type"] == "offline"
        assert kwargs["prompt"] == "consent"

    async def test_redirect_uri_still_passed(self, client, app):
        """The added params must not displace the callback URI."""
        _, redirect = await self._connect(client, app)
        args = redirect.call_args.args
        assert args[1] == "https://example.com/istota/google/callback"

    async def test_unauthenticated_connect_redirects_to_login(self, client, app):
        resp = await client.get("/istota/google/connect", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/istota/login"


DRIVE_RO = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FULL = "https://www.googleapis.com/auth/drive"
GMAIL_RO = "https://www.googleapis.com/auth/gmail.readonly"
CAL_RO = "https://www.googleapis.com/auth/calendar.readonly"


@_needs_web_deps
class TestGoogleScopeSelection:
    """Per-user scope selection (ISSUE-240).

    The instance ``[google_workspace] scopes`` list is a *ceiling*, not a
    request: it names what the operator's Google Cloud project has enabled.
    Each user picks a subset of that, and the connect redirect carries the
    resolved set per request rather than the registered client default.
    """

    @pytest.fixture(autouse=True)
    def _db(self, tmp_path, monkeypatch):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key" * 8)
        self._db_path = db_path

    def _cfg(self, tmp_path, ceiling=None):
        from istota.config import GoogleWorkspaceConfig

        cfg = _make_config(tmp_path)
        cfg.db_path = self._db_path
        cfg.google_workspace = GoogleWorkspaceConfig(
            enabled=True,
            client_id="cid",
            scopes=list(ceiling if ceiling is not None else [DRIVE_RO, GMAIL_RO, CAL_RO]),
        )
        return cfg

    async def _login(self, client, app):
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "user_id": "alice",
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    async def _connect(self, client, app):
        import istota.web_app as mod
        from fastapi.responses import RedirectResponse

        mod._oauth.google.authorize_redirect = AsyncMock(
            return_value=RedirectResponse(url="https://accounts.google.com/o/oauth2/auth")
        )
        resp = await client.get("/istota/google/connect", follow_redirects=False)
        return resp, mod._oauth.google.authorize_redirect

    def _store_selection(self, selection):
        from istota import user_profiles
        user_profiles.ensure_profile(self._db_path, "alice")
        user_profiles.update_profile(self._db_path, "alice", google_scopes=selection)

    # -- connect ---------------------------------------------------------

    async def test_unset_selection_requests_the_whole_ceiling(self, tmp_path, client, app):
        _patch_app(self._cfg(tmp_path))
        await self._login(client, app)
        _, redirect = await self._connect(client, app)
        assert redirect.call_args.kwargs["scope"] == f"{DRIVE_RO} {GMAIL_RO} {CAL_RO}"

    async def test_stored_selection_narrows_the_request(self, tmp_path, client, app):
        _patch_app(self._cfg(tmp_path))
        await self._login(client, app)
        self._store_selection({"drive": "readonly", "gmail": "off", "calendar": "off"})
        _, redirect = await self._connect(client, app)
        assert redirect.call_args.kwargs["scope"] == DRIVE_RO

    async def test_selection_is_clamped_to_the_operator_ceiling(self, tmp_path, client, app):
        """A scope the Cloud project has not enabled fails at Google's end, so
        asking for more than the ceiling is never sent."""
        _patch_app(self._cfg(tmp_path, ceiling=[DRIVE_RO]))
        await self._login(client, app)
        self._store_selection({"drive": "full"})
        _, redirect = await self._connect(client, app)
        assert redirect.call_args.kwargs["scope"] == DRIVE_RO

    async def test_offline_and_reconsent_survive_the_scope_param(self, tmp_path, client, app):
        """Changing the selection means re-consent; the refresh token must
        still be issued on that reconnect."""
        _patch_app(self._cfg(tmp_path))
        await self._login(client, app)
        _, redirect = await self._connect(client, app)
        kwargs = redirect.call_args.kwargs
        assert kwargs["access_type"] == "offline"
        assert kwargs["prompt"] == "consent"
        assert redirect.call_args.args[1] == "https://example.com/istota/google/callback"

    async def test_empty_selection_never_reaches_google(self, tmp_path, client, app):
        """An empty scope string would send the user to a consent screen for
        nothing; bounce with a cause instead."""
        _patch_app(self._cfg(tmp_path))
        await self._login(client, app)
        self._store_selection({"drive": "off", "gmail": "off", "calendar": "off"})
        resp, redirect = await self._connect(client, app)
        assert resp.status_code == 302
        assert "google=no_scopes" in resp.headers["location"]
        redirect.assert_not_called()

    async def test_empty_ceiling_never_reaches_google(self, tmp_path, client, app):
        _patch_app(self._cfg(tmp_path, ceiling=[]))
        await self._login(client, app)
        resp, redirect = await self._connect(client, app)
        assert resp.status_code == 302
        assert "google=no_scopes" in resp.headers["location"]
        redirect.assert_not_called()

    async def test_a_ceiling_of_only_unmapped_scopes_still_connects(
        self, tmp_path, client, app,
    ):
        """An operator running narrow scopes (drive.file, gmail.send) has no
        picker row for any of them; dropping them would take connect away
        entirely rather than narrowing it."""
        narrow = [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/drive.file",
        ]
        _patch_app(self._cfg(tmp_path, ceiling=narrow))
        await self._login(client, app)
        resp, redirect = await self._connect(client, app)
        assert resp.status_code in (302, 307)
        assert redirect.call_args.kwargs["scope"] == " ".join(narrow)

    async def test_unmapped_ceiling_scopes_survive_a_narrowing_selection(
        self, tmp_path, client, app,
    ):
        """No picker row can turn one off, so its absence is not a choice."""
        ceiling = [DRIVE_RO, "https://www.googleapis.com/auth/gmail.send"]
        _patch_app(self._cfg(tmp_path, ceiling=ceiling))
        await self._login(client, app)
        self._store_selection({"drive": "off"})
        _, redirect = await self._connect(client, app)
        assert (
            redirect.call_args.kwargs["scope"]
            == "https://www.googleapis.com/auth/gmail.send"
        )

    # -- status ----------------------------------------------------------

    async def test_status_reports_the_offered_ceiling(self, tmp_path, client, app):
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        resp = await client.get("/istota/api/google/status", cookies=cookies)
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["connected"] is False
        offered = {o["service"]: o["max_level"] for o in body["offered"]}
        assert offered["drive"] == "readonly"
        # Not in the ceiling: "this instance does not offer it", which the UI
        # must render differently from "you have not granted it".
        assert offered["chat"] == "off"

    async def test_status_shows_granted_scopes_grouped_by_service(self, tmp_path, client, app):
        from istota import db
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        with db.get_db(self._db_path) as conn:
            db.upsert_google_token(
                conn, "alice", "a", "r", "2099-01-01T00:00:00+00:00",
                json.dumps([DRIVE_FULL, CAL_RO]),
            )
        body = (await client.get("/istota/api/google/status", cookies=cookies)).json()
        assert body["connected"] is True
        granted = {g["service"]: g for g in body["granted"]}
        assert granted["drive"]["level"] == "full"
        assert granted["calendar"]["level"] == "readonly"
        assert "gmail" not in granted

    async def test_status_flags_a_grant_narrower_than_the_current_request(
        self, tmp_path, client, app,
    ):
        """The state that produces confusing task failures: the config moved,
        the grant did not."""
        from istota import db
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        with db.get_db(self._db_path) as conn:
            db.upsert_google_token(
                conn, "alice", "a", "r", "2099-01-01T00:00:00+00:00",
                json.dumps([DRIVE_RO]),
            )
        body = (await client.get("/istota/api/google/status", cookies=cookies)).json()
        assert body["missing_scopes"] == [GMAIL_RO, CAL_RO]

    async def test_status_reports_no_shortfall_when_the_grant_matches(
        self, tmp_path, client, app,
    ):
        from istota import db
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        with db.get_db(self._db_path) as conn:
            db.upsert_google_token(
                conn, "alice", "a", "r", "2099-01-01T00:00:00+00:00",
                json.dumps([DRIVE_RO, GMAIL_RO, CAL_RO]),
            )
        body = (await client.get("/istota/api/google/status", cookies=cookies)).json()
        assert body["missing_scopes"] == []

    async def test_status_surfaces_an_unrecognised_granted_scope(self, tmp_path, client, app):
        """The map lags a hand-edited config; a granted scope is never hidden."""
        from istota import db
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        with db.get_db(self._db_path) as conn:
            db.upsert_google_token(
                conn, "alice", "a", "r", "2099-01-01T00:00:00+00:00",
                json.dumps([DRIVE_RO, "https://www.googleapis.com/auth/tasks"]),
            )
        body = (await client.get("/istota/api/google/status", cookies=cookies)).json()
        assert body["unrecognized_scopes"] == ["https://www.googleapis.com/auth/tasks"]

    async def test_status_survives_a_rotated_secret_key(self, tmp_path, client, app, monkeypatch):
        """The scope read is decryption-free by design — a stale key must not
        blank the display on a row that still exists."""
        from istota import db
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        with db.get_db(self._db_path) as conn:
            db.upsert_google_token(
                conn, "alice", "a", "r", "2099-01-01T00:00:00+00:00",
                json.dumps([DRIVE_RO]),
            )
        monkeypatch.delenv("ISTOTA_SECRET_KEY", raising=False)
        body = (await client.get("/istota/api/google/status", cookies=cookies)).json()
        assert body["connected"] is True
        assert [g["service"] for g in body["granted"]] == ["drive"]

    async def test_status_when_the_instance_has_google_disabled(self, tmp_path, client, app):
        cfg = self._cfg(tmp_path)
        cfg.google_workspace.enabled = False
        _patch_app(cfg)
        cookies = await self._login(client, app)
        body = (await client.get("/istota/api/google/status", cookies=cookies)).json()
        assert body["enabled"] is False
        assert body["connected"] is False
        assert body["offered"] == []

    async def test_status_names_the_scopes_no_picker_row_covers(
        self, tmp_path, client, app,
    ):
        ceiling = [DRIVE_RO, "https://www.googleapis.com/auth/gmail.send"]
        _patch_app(self._cfg(tmp_path, ceiling=ceiling))
        cookies = await self._login(client, app)
        body = (await client.get("/istota/api/google/status", cookies=cookies)).json()
        assert body["unoffered_scopes"] == ["https://www.googleapis.com/auth/gmail.send"]

    async def test_status_ignores_google_boilerplate_in_the_grant(
        self, tmp_path, client, app,
    ):
        """openid/userinfo ride along on any OIDC-discovered grant and are
        never requested back, so counting them as "extra" would pin a
        reconnect banner that reconnecting cannot clear."""
        from istota import db
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        with db.get_db(self._db_path) as conn:
            db.upsert_google_token(
                conn, "alice", "a", "r", "2099-01-01T00:00:00+00:00",
                json.dumps([DRIVE_RO, GMAIL_RO, CAL_RO, "openid",
                            "https://www.googleapis.com/auth/userinfo.email"]),
            )
        body = (await client.get("/istota/api/google/status", cookies=cookies)).json()
        assert body["extra_scopes"] == []
        assert body["unrecognized_scopes"] == []

    async def test_status_clamps_a_selection_the_ceiling_no_longer_allows(
        self, tmp_path, client, app,
    ):
        """The picker's option list is truncated at the ceiling, so reporting
        the raw stored level would hand the control a value it cannot show."""
        _patch_app(self._cfg(tmp_path, ceiling=[DRIVE_RO]))
        cookies = await self._login(client, app)
        self._store_selection({"drive": "full"})
        body = (await client.get("/istota/api/google/status", cookies=cookies)).json()
        assert body["selection"]["drive"] == "readonly"
        # …and the stored value is left alone, so re-widening the ceiling
        # restores the user's original intent rather than their clamped one.
        from istota import user_profiles
        assert user_profiles.get_profile(self._db_path, "alice").google_scopes == {
            "drive": "full",
        }

    async def test_status_surfaces_a_db_failure_rather_than_reporting_disconnected(
        self, tmp_path, client, app,
    ):
        """A transient read failure used to render as "Not connected", which
        invites the user to redo a consent flow they do not need."""
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        with patch("istota.db.get_google_scopes", side_effect=sqlite3.OperationalError("locked")):
            with pytest.raises(sqlite3.OperationalError):
                await client.get("/istota/api/google/status", cookies=cookies)

    async def test_status_requires_auth(self, tmp_path, client, app):
        _patch_app(self._cfg(tmp_path))
        resp = await client.get("/istota/api/google/status")
        assert resp.status_code == 401

    async def test_status_selection_defaults_to_the_ceiling(self, tmp_path, client, app):
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        body = (await client.get("/istota/api/google/status", cookies=cookies)).json()
        assert body["selection_set"] is False
        assert body["selection"] == {
            "drive": "readonly", "gmail": "readonly", "calendar": "readonly",
            "sheets": "off", "docs": "off", "chat": "off",
        }

    # -- saving the selection --------------------------------------------

    async def test_put_scopes_stores_the_selection(self, tmp_path, client, app):
        from istota import user_profiles
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        resp = await client.put(
            "/istota/api/google/scopes",
            json={"selection": {"drive": "readonly", "gmail": "off", "calendar": "off"}},
            cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 200
        stored = user_profiles.get_profile(self._db_path, "alice").google_scopes
        assert stored == {"drive": "readonly", "gmail": "off", "calendar": "off"}

    async def test_put_scopes_drops_unknown_services(self, tmp_path, client, app):
        from istota import user_profiles
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        resp = await client.put(
            "/istota/api/google/scopes",
            json={"selection": {"drive": "full", "dropbox": "full", "gmail": "sideways"}},
            cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert user_profiles.get_profile(self._db_path, "alice").google_scopes == {
            "drive": "full",
        }

    async def test_put_scopes_reports_the_resolved_request(self, tmp_path, client, app):
        """The response tells the card what a reconnect would now ask for."""
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        resp = await client.put(
            "/istota/api/google/scopes",
            json={"selection": {"drive": "full", "gmail": "off", "calendar": "off"}},
            cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        # Clamped: the ceiling only offers read-only Drive.
        assert resp.json()["requested_scopes"] == [DRIVE_RO]

    async def test_put_scopes_rejects_a_non_object_selection(self, tmp_path, client, app):
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        resp = await client.put(
            "/istota/api/google/scopes",
            json={"selection": ["drive"]},
            cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_put_scopes_refuses_when_the_instance_has_google_off(
        self, tmp_path, client, app,
    ):
        cfg = self._cfg(tmp_path)
        cfg.google_workspace.enabled = False
        _patch_app(cfg)
        cookies = await self._login(client, app)
        resp = await client.put(
            "/istota/api/google/scopes",
            json={"selection": {"drive": "readonly"}},
            cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 409

    async def test_put_scopes_requires_auth(self, tmp_path, client, app):
        _patch_app(self._cfg(tmp_path))
        resp = await client.put(
            "/istota/api/google/scopes",
            json={"selection": {}},
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 401

    async def test_put_scopes_rejects_a_cross_origin_write(self, tmp_path, client, app):
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        resp = await client.put(
            "/istota/api/google/scopes",
            json={"selection": {"drive": "readonly"}},
            cookies=cookies,
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403

    async def test_saving_does_not_disturb_an_existing_grant(self, tmp_path, client, app):
        """Changing the selection needs a reconnect — it must not silently
        revoke or rewrite what the user already granted."""
        from istota import db
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login(client, app)
        with db.get_db(self._db_path) as conn:
            db.upsert_google_token(
                conn, "alice", "a", "r", "2099-01-01T00:00:00+00:00",
                json.dumps([DRIVE_RO, GMAIL_RO, CAL_RO]),
            )
        await client.put(
            "/istota/api/google/scopes",
            json={"selection": {"drive": "readonly", "gmail": "off", "calendar": "off"}},
            cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        body = (await client.get("/istota/api/google/status", cookies=cookies)).json()
        assert body["connected"] is True
        assert {g["service"] for g in body["granted"]} == {"drive", "gmail", "calendar"}
        # …but the card can now say the grant is wider than the request.
        assert body["extra_scopes"] == [GMAIL_RO, CAL_RO]


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
            "user_id": "alice",
        })
        login_resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = login_resp.cookies

        resp = await client.get("/istota/api/me", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "alice"
        # NC's built-in OAuth2 inlines user_id but not display_name; the
        # callback falls back to username when no OCS roundtrip happens.
        assert data["display_name"] == "alice"
        assert data["bot_name"] == "Istota"
        assert data["features"]["feeds"] is True

    async def test_returns_no_feeds_for_user_without_feeds_resource(self, client, app):
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "user_id": "bob",
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
            "user_id": "alice",
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
            "user_id": "alice",
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
            "user_id": "alice",
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = resp.cookies

        # Log in again as bob — old session data should be cleared
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "user_id": "bob",
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
            "user_id": username,
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

            # Usage rides the same payload. Present and empty rather than
            # absent, so the frontend has one shape to render either way.
            assert "usage" in data
            assert data["usage"]["totals_24h"]["rows"] == 0
            assert data["usage"]["totals_24h"]["cost_by_basis"] == {}
            # And on the user rows, beside the task counts.
            assert alice_row["usage_tokens_24h"] == 0
            assert alice_row["usage_cost_24h"] == {}
            assert alice_row["usage_avg_initial_context"] is None

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

    async def test_admin_stats_user_source_breakdown(self, tmp_path):
        """Per-user 24h breakdown must split by source_type so module pollers
        don't mask interactive activity."""
        from istota import db
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.create_task(conn, "talk 1", "alice", source_type="talk")
            db.create_task(conn, "talk 2", "alice", source_type="talk")
            db.create_task(conn, "email 1", "alice", source_type="email")
            for _ in range(20):
                db.create_task(conn, "tick", "alice", source_type="scheduled")

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            data = resp.json()
            alice = next(u for u in data["users"] if u["username"] == "alice")

            assert alice["tasks_by_source_24h"]["talk"]["count"] == 2
            assert alice["tasks_by_source_24h"]["email"]["count"] == 1
            assert alice["tasks_by_source_24h"]["scheduled"]["count"] == 20

            assert alice["tasks_interactive_24h"] == 3
            assert alice["tasks_automated_24h"] == 20
            assert alice["tasks_failed_24h"] == 0

    async def test_admin_stats_user_failures_per_source(self, tmp_path):
        """Per-user failed counts must aggregate across source_types."""
        from istota import db
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            t_talk = db.create_task(conn, "talk fail", "alice", source_type="talk")
            t_sched = db.create_task(conn, "sched fail", "alice", source_type="scheduled")
            db.create_task(conn, "talk ok", "alice", source_type="talk")
            conn.execute("UPDATE tasks SET status = 'failed' WHERE id IN (?, ?)",
                         (t_talk, t_sched))
            conn.commit()

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            data = resp.json()
            alice = next(u for u in data["users"] if u["username"] == "alice")
            assert alice["tasks_failed_24h"] == 2
            assert alice["tasks_by_source_24h"]["talk"]["failed"] == 1
            assert alice["tasks_by_source_24h"]["scheduled"]["failed"] == 1

    async def test_admin_stats_interactive_vs_automated_split(self, tmp_path):
        """Headline must split interactive (talk/email/cli/tasks_file) from
        automated (scheduled/briefing/heartbeat/subtask)."""
        from istota import db
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.create_task(conn, "talk 1", "alice", source_type="talk")
            db.create_task(conn, "email 1", "alice", source_type="email")
            for _ in range(10):
                db.create_task(conn, "tick", "alice", source_type="scheduled")
            db.create_task(conn, "brief", "alice", source_type="briefing")

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            tasks = resp.json()["tasks"]
            assert tasks["interactive_24h"] == 2
            assert tasks["automated_24h"] == 11

    async def test_admin_stats_unknown_sources_classified_as_automated(self, tmp_path):
        """``interactive_24h + automated_24h`` must equal ``last_24h``.

        Unrecognized ``source_type`` values used to land in an ``other``
        bucket excluded from both interactive and automated counters,
        which silently undercounted the headline split. Pin the invariant
        so adding a future source_type without updating the classifier
        doesn't quietly drift the headline.
        """
        from istota import db
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.create_task(conn, "talk", "alice", source_type="talk")
            db.create_task(conn, "sched", "alice", source_type="scheduled")
            db.create_task(conn, "future thing", "alice", source_type="totally_new_kind")

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            tasks = resp.json()["tasks"]
            assert tasks["interactive_24h"] + tasks["automated_24h"] == tasks["last_24h"]
            assert tasks["interactive_24h"] == 1
            assert tasks["automated_24h"] == 2  # scheduled + unrecognized

    async def test_admin_stats_failed_count_and_per_source(self, tmp_path):
        """Tasks card must surface both failed_24h and per-source failures."""
        from istota import db
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            t_talk_fail = db.create_task(conn, "talk fail", "alice", source_type="talk")
            t_sched_fail_a = db.create_task(conn, "sched fail a", "alice", source_type="scheduled")
            t_sched_fail_b = db.create_task(conn, "sched fail b", "bob", source_type="scheduled")
            db.create_task(conn, "ok", "alice", source_type="talk")
            conn.execute(
                "UPDATE tasks SET status = 'failed' WHERE id IN (?, ?, ?)",
                (t_talk_fail, t_sched_fail_a, t_sched_fail_b),
            )
            conn.commit()

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            tasks = resp.json()["tasks"]
            assert tasks["failed_24h"] == 3
            assert tasks["failed_by_source_24h"]["talk"] == 1
            assert tasks["failed_by_source_24h"]["scheduled"] == 2

    async def test_admin_stats_user_avg_duration_per_source(self, tmp_path):
        """Per-user avg duration must be reported per source_type."""
        from istota import db
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            t_talk = db.create_task(conn, "talk", "alice", source_type="talk")
            t_sched = db.create_task(conn, "sched", "alice", source_type="scheduled")
            conn.execute(
                "UPDATE tasks SET status='completed', "
                "started_at='2026-05-06 12:00:00', completed_at='2026-05-06 12:00:30' "
                "WHERE id = ?",
                (t_talk,),
            )
            conn.execute(
                "UPDATE tasks SET status='completed', "
                "started_at='2026-05-06 12:00:00', completed_at='2026-05-06 12:00:02' "
                "WHERE id = ?",
                (t_sched,),
            )
            conn.commit()

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            data = resp.json()
            alice = next(u for u in data["users"] if u["username"] == "alice")
            assert alice["tasks_by_source_24h"]["talk"]["avg_duration_seconds"] == pytest.approx(30.0, abs=0.5)
            assert alice["tasks_by_source_24h"]["scheduled"]["avg_duration_seconds"] == pytest.approx(2.0, abs=0.5)

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
        config.users["alice"] = UserConfig(display_name="Alice")
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

    async def test_admin_stats_includes_health_module(self, tmp_path):
        """The health module surfaces per-user panel/stat counts + last update."""
        from istota import health as health_mod
        config = self._config_with_admin(tmp_path)

        health_db = config.module_db_path("alice", "health")
        health_db.parent.mkdir(parents=True, exist_ok=True)
        health_mod.init_db(health_db)
        with health_mod.connect(health_db) as conn:
            conn.execute(
                "INSERT INTO panels (drawn_at, lab_name) VALUES (?, ?)",
                ("2026-07-01", "Quest"),
            )
            conn.execute(
                "INSERT INTO stats (measured_at, metric, value, unit) "
                "VALUES (?, ?, ?, ?)",
                ("2026-07-10", "weight", 70.0, "kg"),
            )
            conn.commit()

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            data = resp.json()
            assert "health" in data["modules"]
            health = data["modules"]["health"]
            assert health["users_configured"] >= 1
            assert health["panels_total"] == 1
            assert health["biomarkers_total"] == 0
            assert health["last_update"] == "2026-07-10T00:00:00Z"

    async def test_admin_stats_includes_briefings_module(self, tmp_path):
        """The briefings module surfaces block/source/archive counts."""
        from istota.briefings import db as briefings_db
        config = self._config_with_admin(tmp_path)

        bdb_path = config.module_db_path("alice", "briefings")
        bdb_path.parent.mkdir(parents=True, exist_ok=True)
        briefings_db.init_db(bdb_path)
        with briefings_db.connect(bdb_path) as conn:
            conn.execute(
                "INSERT INTO briefing_blocks "
                "(briefing_name, position, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("morning", 0, "Headlines", "2026-07-01 00:00:00", "2026-07-01 00:00:00"),
            )
            conn.execute(
                "INSERT INTO briefing_archive "
                "(briefing_name, body_md, generated_at) VALUES (?, ?, ?)",
                ("morning", "# hi", "2026-07-12 07:00:00"),
            )
            conn.commit()

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            data = resp.json()
            assert "briefings" in data["modules"]
            briefings = data["modules"]["briefings"]
            assert briefings["users_configured"] >= 1
            assert briefings["blocks_total"] == 1
            assert briefings["archived_total"] == 1
            assert briefings["last_generated"] == "2026-07-12T07:00:00Z"

    async def test_admin_stats_includes_location_module(self, tmp_path):
        """Location surfaces on per-user module enablement (no receiver gate)."""
        from istota import location as location_mod
        config = self._config_with_admin(tmp_path)
        # Receiver flag defaults off; the module still surfaces from per-user
        # enablement alone.
        config.location.enabled = False

        loc_db = config.module_db_path("alice", "location")
        loc_db.parent.mkdir(parents=True, exist_ok=True)
        location_mod.init_db(loc_db)
        with location_mod.connect(loc_db) as conn:
            conn.execute(
                "INSERT INTO places (name, lat, lon) VALUES (?, ?, ?)",
                ("Home", 0.0, 0.0),
            )
            conn.commit()

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            loc = resp.json()["modules"]["location"]
            assert loc["users_configured"] >= 1
            assert loc["places_total"] == 1

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

    async def test_admin_stats_last_active_ignores_automated_sources(self, tmp_path):
        """A user whose only traffic is scheduled has never been active."""
        from istota import db
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            for source in ("scheduled", "briefing", "heartbeat", "subtask"):
                db.create_task(conn, source, "alice", source_type=source)
            conn.commit()

        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)
            alice = next(u for u in resp.json()["users"] if u["username"] == "alice")
            assert alice["last_active"] is None
            # Volume is still all-sources; only presence is interactive-only.
            assert alice["tasks_total"] == 4

    async def test_admin_stats_last_active_counts_every_interactive_source(self, tmp_path):
        """Positive control per source type, spelled as its writer spells it.

        The list is deliberately duplicated rather than read off
        ``_INTERACTIVE_SOURCES``: the point is to catch the set drifting from
        what the tree actually stores. ``tasks_file`` sat in that set for
        months and matched nothing, because the poller writes ``istota_file``.
        """
        from istota import db, web_app
        for source in ("talk", "email", "web", "cli", "repl", "istota_file"):
            root = tmp_path / source
            root.mkdir()
            config = self._config_with_admin(root)
            with db.get_db(config.db_path) as conn:
                tid = db.create_task(conn, "hello", "alice", source_type=source)
                conn.execute(
                    "UPDATE tasks SET created_at = ? WHERE id = ?",
                    ("2026-04-01 12:00:00", tid),
                )
                conn.commit()

            app = _patch_app(config)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="https://example.com") as client:
                cookies = await self._login(client, "alice")
                resp = await client.get("/istota/api/admin/stats", cookies=cookies)
                alice = next(u for u in resp.json()["users"] if u["username"] == "alice")
                assert alice["last_active"] == "2026-04-01T12:00:00Z", source
                assert web_app._classify_source(source) == "interactive", source

    async def test_admin_stats_last_active_is_oldest_interactive_row(self, tmp_path):
        """Newer automated rows must not bury an older interactive one."""
        from istota import db
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            tid = db.create_task(conn, "the real one", "alice", source_type="talk")
            conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id = ?",
                ("2026-04-01 12:00:00", tid),
            )
            for n in range(3):
                jid = db.create_task(conn, f"poll {n}", "alice", source_type="scheduled")
                conn.execute(
                    "UPDATE tasks SET created_at = ? WHERE id = ?",
                    (f"2026-05-0{n + 1} 12:00:00", jid),
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


@_needs_web_deps
class TestAdminBrainStatus:
    """ISSUE-188 — live brain posture (degraded/fallback) on the admin dashboard.

    Distinct from the ``models`` section (configured brain): this consults the
    process-global availability breaker so a degraded primary (running on the
    fallback) is visible to the operator at a glance.
    """

    def _config_with_fallback(self, tmp_path):
        from istota import db
        from istota.config import BrainConfig
        config = _make_config(tmp_path)
        config.db_path = tmp_path / "istota.db"
        config.admin_users = {"alice"}
        config.brain = BrainConfig(
            kind="claude_code", fallback="native", fallback_cooldown_seconds=900
        )
        db.init_db(config.db_path)
        return config

    def _open_breaker(self, kind, cooldown=900):
        from istota.brain._fallback import get_availability_breaker
        get_availability_breaker().open(kind, cooldown)

    def _reset_breaker(self):
        from istota.brain._fallback import reset_availability_breaker
        reset_availability_breaker()

    def test_healthy_when_breaker_closed(self, tmp_path):
        import istota.web_app as mod
        self._reset_breaker()
        mod._config = self._config_with_fallback(tmp_path)
        section = mod._admin_brain_status_section()
        assert section["degraded"] is False
        assert section["active"] == "claude_code"
        assert section["primary"] == "claude_code"

    def test_degraded_when_breaker_open(self, tmp_path):
        import istota.web_app as mod
        mod._config = self._config_with_fallback(tmp_path)
        self._reset_breaker()
        self._open_breaker("claude_code")
        try:
            section = mod._admin_brain_status_section()
            assert section["degraded"] is True
            assert section["primary"] == "claude_code"
            assert section["active"] == "native"
            assert section["reason"]
        finally:
            self._reset_breaker()

    def test_degraded_from_scheduler_availability_state(self, tmp_path):
        """The web service has a separate process from the scheduler."""
        import istota.web_app as mod
        from istota.brain_availability import record_unavailable

        config = self._config_with_fallback(tmp_path)
        record_unavailable(
            config, "claude_code", "usage_limit", cooldown_seconds=900
        )
        self._reset_breaker()
        mod._config = config

        section = mod._admin_brain_status_section()

        assert section["degraded"] is True
        assert section["primary"] == "claude_code"
        assert section["active"] == "native"
        assert section["reason"] == "usage_limit"

    def test_degraded_with_no_fallback_configured(self, tmp_path):
        import istota.web_app as mod
        from istota.config import BrainConfig
        config = self._config_with_fallback(tmp_path)
        config.brain = BrainConfig(kind="claude_code", fallback_cooldown_seconds=900)
        mod._config = config
        self._reset_breaker()
        self._open_breaker("claude_code")
        try:
            section = mod._admin_brain_status_section()
            assert section["degraded"] is True
            assert section["primary"] == "claude_code"
            # No fallback configured — active is null so the frontend can say so.
            assert section["active"] is None
        finally:
            self._reset_breaker()

    def test_cooldown_zero_never_degrades(self, tmp_path):
        # Stickiness disabled: the breaker is never consulted for skipping, so
        # there's no persistent degraded posture to display.
        import istota.web_app as mod
        from istota.config import BrainConfig
        config = self._config_with_fallback(tmp_path)
        config.brain = BrainConfig(
            kind="claude_code", fallback="native", fallback_cooldown_seconds=0
        )
        mod._config = config
        self._reset_breaker()
        self._open_breaker("claude_code", cooldown=0)
        try:
            section = mod._admin_brain_status_section()
            assert section["degraded"] is False
        finally:
            self._reset_breaker()

    async def _login(self, client, username):
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "user_id": username,
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    async def test_brain_status_in_stats_payload(self, tmp_path):
        config = self._config_with_fallback(tmp_path)
        self._reset_breaker()
        self._open_breaker("claude_code")
        try:
            app = _patch_app(config)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="https://example.com") as client:
                cookies = await self._login(client, "alice")
                resp = await client.get("/istota/api/admin/stats", cookies=cookies)
                assert resp.status_code == 200
                bs = resp.json()["brain_status"]
                assert bs["degraded"] is True
                assert bs["active"] == "native"
                assert bs["primary"] == "claude_code"
        finally:
            self._reset_breaker()


@_needs_web_deps
class TestTaskEventEndpoints:
    """SSE / snapshot / admin consumers of the task_events table."""

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
            "user_id": username,
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    def _seed_task_with_events(self, config, user_id="alice"):
        from istota import db
        from istota.events import EventWriter
        with db.get_db(config.db_path) as conn:
            tid = db.create_task(conn, "do a thing", user_id, source_type="talk")
        w = EventWriter(tid, str(config.db_path))
        w.emit("task_started")
        w.emit("tool_start", {"tool_name": "Read", "description": "📄 Reading f", "tool_call_id": "t1"})
        w.emit("result", {"text": "all done", "truncated": False})
        w.emit("done", {"stop_reason": "completed", "duration_seconds": 1.2})
        return tid

    async def test_snapshot_returns_events_for_owner(self, tmp_path):
        config = self._config_with_admin(tmp_path)
        tid = self._seed_task_with_events(config)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get(f"/istota/api/chat/tasks/{tid}/events", cookies=cookies)
            assert resp.status_code == 200
            events = resp.json()["events"]
            assert [e["kind"] for e in events] == [
                "task_started", "tool_start", "result", "done",
            ]
            assert events[1]["payload"]["description"] == "📄 Reading f"

    async def test_snapshot_since_seq(self, tmp_path):
        config = self._config_with_admin(tmp_path)
        tid = self._seed_task_with_events(config)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get(
                f"/istota/api/chat/tasks/{tid}/events?since_seq=2", cookies=cookies,
            )
            assert [e["seq"] for e in resp.json()["events"]] == [3, 4]

    async def test_snapshot_other_users_task_forbidden(self, tmp_path):
        config = self._config_with_admin(tmp_path)
        tid = self._seed_task_with_events(config, user_id="alice")
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "bob")
            resp = await client.get(f"/istota/api/chat/tasks/{tid}/events", cookies=cookies)
            assert resp.status_code == 403

    async def test_snapshot_unknown_task_404(self, tmp_path):
        config = self._config_with_admin(tmp_path)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/chat/tasks/999/events", cookies=cookies)
            assert resp.status_code == 404

    async def test_snapshot_requires_auth(self, tmp_path):
        config = self._config_with_admin(tmp_path)
        tid = self._seed_task_with_events(config)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            resp = await client.get(f"/istota/api/chat/tasks/{tid}/events")
            assert resp.status_code == 401

    async def test_sse_stream_dumps_history_and_closes(self, tmp_path):
        config = self._config_with_admin(tmp_path)
        tid = self._seed_task_with_events(config)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get(f"/istota/api/chat/tasks/{tid}/stream", cookies=cookies)
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = resp.text
            # Each event framed with id / event / data; stream ends after done.
            assert "event: task_started" in body
            assert "event: done" in body
            assert "id: 4" in body

    def _seed_failed_task_no_done(self, config, user_id="alice"):
        """A terminal (failed) task whose event log has NO `done` — mimics the
        retry path deleting + seq-resetting the log so the final attempt's
        terminal frames are unreachable (seq below the client's cursor), or a
        crash skipping EventWriter.finish()."""
        from istota import db
        from istota.events import EventWriter
        with db.get_db(config.db_path) as conn:
            tid = db.create_task(conn, "do a thing", user_id, source_type="web")
        w = EventWriter(tid, str(config.db_path))
        w.emit("task_started")
        w.emit("tool_start", {"tool_name": "Read", "description": "📄 Reading f",
                              "tool_call_id": "t1"})
        with db.get_db(config.db_path) as conn:
            db.update_task_status(conn, tid, "failed",
                                  error="Stream parsing failed (rc=-15, 17 lines)")
        return tid

    async def test_snapshot_synthesizes_terminal_for_failed_task(self, tmp_path):
        """A failed task with no `done` in the log yields synthesized error+done
        (seq above the client's cursor) so the poll loop settles."""
        config = self._config_with_admin(tmp_path)
        tid = self._seed_failed_task_no_done(config)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            # Client parked past the surviving rows (mimics a retry seq-reset).
            resp = await client.get(
                f"/istota/api/chat/tasks/{tid}/events?since_seq=2", cookies=cookies,
            )
            assert resp.status_code == 200
            events = resp.json()["events"]
            assert [e["kind"] for e in events] == ["error", "done"]
            assert events[0]["payload"]["message"].startswith("Stream parsing failed")
            assert all(e["seq"] > 2 for e in events)

    async def test_snapshot_synthesized_result_is_not_truncated(self, tmp_path):
        """The synthetic/backstop `result` frame carries the full task body, not
        an 8000-char slice — the resume path must match what the live path now
        delivers whole (ISSUE-178)."""
        from istota import db
        from istota.events import EventWriter
        config = self._config_with_admin(tmp_path)
        long_result = "word " * 4000  # 20000 chars, well over the old 8000 cap
        with db.get_db(config.db_path) as conn:
            tid = db.create_task(conn, "write a lot", "alice", source_type="web")
        w = EventWriter(tid, str(config.db_path))
        w.emit("task_started")
        w.emit("tool_start", {"tool_name": "Read", "description": "reading",
                              "tool_call_id": "t1"})
        with db.get_db(config.db_path) as conn:
            db.update_task_status(conn, tid, "completed", result=long_result)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get(
                f"/istota/api/chat/tasks/{tid}/events?since_seq=2", cookies=cookies,
            )
            assert resp.status_code == 200
            events = resp.json()["events"]
            assert [e["kind"] for e in events] == ["result", "done"]
            assert events[0]["payload"]["text"] == long_result

    async def test_snapshot_no_synthesis_while_running(self, tmp_path):
        """A still-running task gets no synthetic terminal frame."""
        from istota import db
        from istota.events import EventWriter
        config = self._config_with_admin(tmp_path)
        with db.get_db(config.db_path) as conn:
            tid = db.create_task(conn, "x", "alice", source_type="web")
            db.update_task_status(conn, tid, "running")
        EventWriter(tid, str(config.db_path)).emit("task_started")
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get(
                f"/istota/api/chat/tasks/{tid}/events?since_seq=1", cookies=cookies,
            )
            assert resp.json()["events"] == []

    async def test_snapshot_no_double_terminal_when_done_present(self, tmp_path):
        """The normal completed-with-done task is untouched (no extra frames)."""
        config = self._config_with_admin(tmp_path)
        tid = self._seed_task_with_events(config)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get(f"/istota/api/chat/tasks/{tid}/events", cookies=cookies)
            assert [e["kind"] for e in resp.json()["events"]] == [
                "task_started", "tool_start", "result", "done",
            ]

    async def test_sse_synthesizes_terminal_for_stuck_failed_task(self, tmp_path):
        """SSE for a failed task whose `done` is unreachable still ends the
        stream (synthesized error+done) instead of polling forever."""
        config = self._config_with_admin(tmp_path)
        tid = self._seed_failed_task_no_done(config)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get(
                f"/istota/api/chat/tasks/{tid}/stream?since_seq=2", cookies=cookies,
            )
            assert resp.status_code == 200
            body = resp.text
            assert "event: error" in body
            assert "event: done" in body

    async def test_admin_events_endpoint(self, tmp_path):
        config = self._config_with_admin(tmp_path)
        tid = self._seed_task_with_events(config, user_id="bob")
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")  # admin
            resp = await client.get(f"/istota/api/admin/tasks/{tid}/events", cookies=cookies)
            assert resp.status_code == 200
            assert len(resp.json()["events"]) == 4

    async def test_admin_events_forbidden_for_non_admin(self, tmp_path):
        config = self._config_with_admin(tmp_path)
        tid = self._seed_task_with_events(config, user_id="bob")
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "bob")
            resp = await client.get(f"/istota/api/admin/tasks/{tid}/events", cookies=cookies)
            assert resp.status_code == 403


class TestWebConfigParsing:
    def test_web_config_defaults(self):
        cfg = Config()
        assert cfg.web.enabled is False
        assert cfg.web.port == 8766
        assert cfg.web.oauth2_provider == ""
        assert cfg.web.oauth2_client_id == ""
        assert cfg.web.oauth2_client_secret == ""
        assert cfg.web.session_secret_key == ""

    def test_web_config_from_toml(self, tmp_path):
        toml_content = """
[web]
enabled = true
port = 9000
oauth2_provider = "https://cloud.example.com"
oauth2_client_id = "my-client"
oauth2_client_secret = "my-secret"
session_secret_key = "my-key"
"""
        config_file = tmp_path / "config.toml"
        config_file.write_text(toml_content)
        cfg = load_config(config_file)
        assert cfg.web.enabled is True
        assert cfg.web.port == 9000
        assert cfg.web.oauth2_provider == "https://cloud.example.com"
        assert cfg.web.oauth2_client_id == "my-client"
        assert cfg.web.oauth2_client_secret == "my-secret"
        assert cfg.web.session_secret_key == "my-key"

    def test_env_var_overrides(self, tmp_path, monkeypatch):
        toml_content = """
[web]
enabled = true
oauth2_provider = "https://cloud.example.com"
oauth2_client_id = "my-client"
"""
        config_file = tmp_path / "config.toml"
        config_file.write_text(toml_content)
        monkeypatch.setenv("ISTOTA_WEB_OAUTH2_CLIENT_SECRET", "env-secret")
        monkeypatch.setenv("ISTOTA_WEB_SESSION_SECRET_KEY", "env-key")
        cfg = load_config(config_file)
        assert cfg.web.oauth2_client_secret == "env-secret"
        assert cfg.web.session_secret_key == "env-key"


@_needs_web_deps
class TestSessionSecretResolution:
    """ISSUE-124: the web session signing key must never fall back to a shared
    constant — it resolves from the env var, then config.toml, then fails
    closed unless an explicit dev override is set."""

    def test_env_var_wins(self, monkeypatch):
        from istota import web_app
        monkeypatch.setenv("ISTOTA_WEB_SESSION_SECRET_KEY", "env-signing-key")
        # Even if config supplies one, the env var takes precedence.
        cfg = MagicMock()
        cfg.web.session_secret_key = "config-key"
        monkeypatch.setattr(web_app, "load_config", lambda: cfg)
        assert web_app._resolve_session_secret() == "env-signing-key"

    def test_falls_back_to_config_session_secret(self, monkeypatch):
        from istota import web_app
        monkeypatch.delenv("ISTOTA_WEB_SESSION_SECRET_KEY", raising=False)
        cfg = MagicMock()
        cfg.web.session_secret_key = "config-signing-key"
        monkeypatch.setattr(web_app, "load_config", lambda: cfg)
        assert web_app._resolve_session_secret() == "config-signing-key"

    def test_dev_override_yields_random_per_process_key(self, monkeypatch):
        from istota import web_app
        monkeypatch.delenv("ISTOTA_WEB_SESSION_SECRET_KEY", raising=False)
        monkeypatch.setenv("ISTOTA_WEB_ALLOW_INSECURE_SESSION", "1")
        cfg = MagicMock()
        cfg.web.session_secret_key = ""
        monkeypatch.setattr(web_app, "load_config", lambda: cfg)
        secret = web_app._resolve_session_secret()
        # Never the old public constant; long and random.
        assert secret != "change-me-insecure-default"
        assert len(secret) >= 32
        # A second call yields a different key (per-process random, no constant).
        assert web_app._resolve_session_secret() != secret

    def test_fails_closed_without_secret_or_override(self, monkeypatch):
        from istota import web_app
        monkeypatch.delenv("ISTOTA_WEB_SESSION_SECRET_KEY", raising=False)
        monkeypatch.delenv("ISTOTA_WEB_ALLOW_INSECURE_SESSION", raising=False)
        cfg = MagicMock()
        cfg.web.session_secret_key = ""
        monkeypatch.setattr(web_app, "load_config", lambda: cfg)
        with pytest.raises(RuntimeError, match="session signing secret"):
            web_app._resolve_session_secret()

    def test_config_load_failure_is_not_fatal(self, monkeypatch):
        from istota import web_app
        monkeypatch.delenv("ISTOTA_WEB_SESSION_SECRET_KEY", raising=False)
        monkeypatch.setenv("ISTOTA_WEB_ALLOW_INSECURE_SESSION", "1")

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(web_app, "load_config", _boom)
        # A broken config must not crash resolution — it degrades to the
        # dev-override path here, or would fail closed without it.
        assert len(web_app._resolve_session_secret()) >= 32


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
                "alice": UserConfig(display_name="Alice"),
                "bob": UserConfig(
                    display_name="Bob",
                    disabled_modules=["feeds", "money", "location"],
                ),
            },
        )
        cfg.db_path = self._db_path
        return cfg

    async def _login_alice(self, client, app):
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "user_id": "alice",
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    async def test_services_returns_only_connected_cards(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)
        resp = await client.get("/istota/api/settings/services", cookies=cookies)
        assert resp.status_code == 200
        services = {s["service"]: s for s in resp.json()["services"]}
        # Connected services only — karakeep + google_workspace (OAuth).
        # Module-owned services (monarch, feeds, overland) live on the
        # per-module endpoint and must NOT leak into /settings/services.
        assert "karakeep" in services
        assert "monarch" not in services
        assert "feeds" not in services
        assert "overland" not in services
        assert services["karakeep"]["status"] in ("missing", "partial", "configured")
        assert services["karakeep"]["used_by"] == ["bookmarks"]
        # native_brain is CLI-only — it must not surface in the web UI even
        # though it's a connected service (the per-user key knob did less
        # than it looked like; operator-provisioned via `istota secret`).
        assert "native_brain" not in services

    async def test_google_card_is_bespoke_and_carries_its_own_state(
        self, tmp_path, client, app,
    ):
        """The Google card outgrew the generic OAuth branch (ISSUE-240): it
        renders a scope picker, so it declares custom_ui like Garmin and reads
        the rest from /api/google/status."""
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)
        resp = await client.get("/istota/api/settings/services", cookies=cookies)
        google = {s["service"]: s for s in resp.json()["services"]}["google_workspace"]
        assert google["oauth"] is True
        assert google["custom_ui"] is True
        assert google["connected"] is False
        assert "enabled" in google

    async def test_native_brain_is_cli_only_but_still_known(self, tmp_path, client, app):
        # The web surface is gone, but CLI/runtime validation must still
        # accept native_brain so `istota secret ensure -s native_brain` and
        # the executor's per-user-key overlay keep working.
        from istota import secret_schema

        assert "native_brain" in secret_schema.all_known_services()
        assert "api_key" in secret_schema.known_service_keys()["native_brain"]
        assert secret_schema.CONNECTED_SERVICE_SCHEMA["native_brain"].get("cli_only") is True

        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)
        # PUT still works (no UI offers it, but the route stays permissive).
        resp = await client.put(
            "/istota/api/settings/secrets/native_brain/api_key",
            json={"value": "user-byo-key"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200

    async def test_module_services_endpoint(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)

        resp = await client.get(
            "/istota/api/settings/module-services/money", cookies=cookies,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["module"] == "money"
        assert body["module_enabled"] is True
        services = {s["service"]: s for s in body["services"]}
        assert "monarch" in services
        assert services["monarch"]["used_by"] == ["money"]

        resp = await client.get(
            "/istota/api/settings/module-services/location", cookies=cookies,
        )
        body = resp.json()
        services = {s["service"]: s for s in body["services"]}
        assert "overland" in services

        # Unknown module → 404.
        resp = await client.get(
            "/istota/api/settings/module-services/bogus", cookies=cookies,
        )
        assert resp.status_code == 404

    async def test_module_services_disabled_module(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        # Bob has every module disabled in the shared fixture.
        _patch_app(cfg)
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "user_id": "bob",
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        cookies = resp.cookies
        resp = await client.get(
            "/istota/api/settings/module-services/money", cookies=cookies,
        )
        body = resp.json()
        assert body["module_enabled"] is False
        # Schema is still served so the page can render the banner + a
        # placeholder list, but the user is meant to see the banner first.

    async def test_modules_endpoint(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)
        resp = await client.get("/istota/api/settings/modules", cookies=cookies)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["modules"]) == {"feeds", "money", "location", "health", "briefings"}
        # Alice has no disabled_modules in the fixture.
        assert body["disabled"] == []
        assert body["enabled_for_user"]["feeds"] is True
        assert body["enabled_for_user"]["health"] is True

    async def test_services_never_returns_plaintext(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        from istota import secrets_store
        secrets_store.set_secret(self._db_path, "alice", "monarch", "session_id", "secret-sid-x")
        cookies = await self._login_alice(client, app)
        resp = await client.get("/istota/api/settings/services", cookies=cookies)
        body = resp.text
        assert "secret-sid-x" not in body

    async def test_set_secret_persists(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)
        resp = await client.put(
            "/istota/api/settings/secrets/monarch/session_id",
            json={"value": "SID-alice"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["configured"] is True
        from istota import secrets_store
        assert secrets_store.get_secret(self._db_path, "alice", "monarch", "session_id") == "SID-alice"

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
            "/istota/api/settings/secrets/monarch/session_id",
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
            "/istota/api/settings/secrets/monarch/session_id",
            json={"value": "x"},
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 401

    async def test_delete_secret(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        from istota import secrets_store
        secrets_store.set_secret(self._db_path, "alice", "monarch", "session_id", "SID-x")
        cookies = await self._login_alice(client, app)
        resp = await client.delete(
            "/istota/api/settings/secrets/monarch/session_id",
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert secrets_store.get_secret(self._db_path, "alice", "monarch", "session_id") is None

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
            "/istota/api/settings/secrets/monarch/session_id",
            json={"value": "SID-alice"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        from istota import secrets_store
        assert secrets_store.get_secret(self._db_path, "alice", "monarch", "session_id") == "SID-alice"
        assert secrets_store.get_secret(self._db_path, "bob", "monarch", "session_id") is None


@_needs_web_deps
class TestGenerateIngestToken:
    """Server-minted ingest token for the location receiver.

    The one endpoint that returns a secret in its response body, because a
    token the user never sees cannot go in a QR code — and typing a 32-byte
    secret into a phone is the provisioning step this exists to delete. It
    is returned exactly once; every later read is write-only, like the rest
    of the secrets API.
    """

    @pytest.fixture(autouse=True)
    def _secret_key(self, monkeypatch, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key" * 8)
        self._db_path = db_path

    def _make_test_config(self, tmp_path):
        cfg = _make_config(
            tmp_path,
            users={
                "alice": UserConfig(display_name="Alice"),
                "bob": UserConfig(
                    display_name="Bob", disabled_modules=["location"],
                ),
            },
        )
        cfg.db_path = self._db_path
        # The receiver is off by default in the lean footprint, and a token
        # is useless without it — so the endpoint gates on both halves.
        cfg.location = LocationReceiverConfig(enabled=True)
        return cfg

    async def _login(self, client, app, user_id="alice"):
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "user_id": user_id,
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    _URL = "/istota/api/settings/secrets/overland/ingest_token/generate"

    async def test_generate_returns_and_stores_a_token(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, app)
        resp = await client.post(
            self._URL, cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        token = resp.json()["token"]
        # token_urlsafe(32) is ~43 chars; assert it is not guessable rather
        # than pinning an exact length.
        assert len(token) >= 32

        from istota import secrets_store
        assert secrets_store.get_secret(
            self._db_path, "alice", "overland", "ingest_token",
        ) == token

    async def test_generate_returns_the_webhook_url(self, tmp_path, client, app):
        """The QR carries the whole URL, not a token the user then has to
        paste into an address they assemble by hand."""
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, app)
        resp = await client.post(
            self._URL, cookies=cookies, headers={"origin": "https://example.com"},
        )
        body = resp.json()
        assert body["webhook_url"].startswith("https://example.com/webhooks/location")
        assert body["token"] in body["webhook_url"]

    def _standalone_config(self, tmp_path):
        """The one shape that can actually reach the hostname guard.

        Under Nextcloud auth a blank ``[site] hostname`` already 403s every
        request at ``_verify_origin``, so the endpoint is unreachable. In
        no-auth mode that check is a deliberate no-op — which is what leaves
        the standalone local install able to mint a token whose URL is
        relative, and therefore a QR the phone can only reject.
        """
        from istota.config import SiteConfig, WebConfig

        cfg = self._make_test_config(tmp_path)
        cfg.site = SiteConfig(hostname="")
        cfg.web = WebConfig(enabled=True, port=8766, auth="none")
        cfg.users = {"alice": UserConfig(display_name="Alice")}
        return cfg

    async def test_generate_refuses_without_a_public_hostname(self, tmp_path, client, app):
        """A relative webhook URL is a QR the phone can only reject.

        The payload decoder refuses an endpoint that is not https, so the
        device would report "not an Istota provisioning code" and blame the
        code rather than the missing config.
        """
        _patch_app(self._standalone_config(tmp_path))
        resp = await client.post(self._URL)
        assert resp.status_code == 409
        assert "hostname" in resp.json()["detail"]

    async def test_a_refused_generate_does_not_rotate_the_token(self, tmp_path, client, app):
        """Refusing after minting would cut off working devices for nothing."""
        from istota import secrets_store

        secrets_store.set_secret(
            self._db_path, "alice", "overland", "ingest_token", "still-working",
        )
        _patch_app(self._standalone_config(tmp_path))
        await client.post(self._URL)
        assert secrets_store.get_secret(
            self._db_path, "alice", "overland", "ingest_token",
        ) == "still-working"

    async def test_generate_rotates_an_existing_token(self, tmp_path, client, app):
        from istota import secrets_store
        secrets_store.set_secret(
            self._db_path, "alice", "overland", "ingest_token", "old-token",
        )
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, app)
        resp = await client.post(
            self._URL, cookies=cookies, headers={"origin": "https://example.com"},
        )
        new_token = resp.json()["token"]
        assert new_token != "old-token"
        assert secrets_store.get_secret(
            self._db_path, "alice", "overland", "ingest_token",
        ) == new_token

    async def test_generate_signals_the_receiver(self, tmp_path, client, app):
        """Otherwise the token 403s until the receiver process restarts."""
        from istota.location import ingest_signal

        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        before = ingest_signal.reload_stamp(self._db_path)
        cookies = await self._login(client, app)
        await client.post(
            self._URL, cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert ingest_signal.reload_stamp(self._db_path) > before

    async def test_manual_token_write_also_signals(self, tmp_path, client, app):
        """A pasted token has the same staleness problem as a minted one."""
        from istota.location import ingest_signal

        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        before = ingest_signal.reload_stamp(self._db_path)
        cookies = await self._login(client, app)
        resp = await client.put(
            "/istota/api/settings/secrets/overland/ingest_token",
            json={"value": "hand-typed"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert ingest_signal.reload_stamp(self._db_path) > before

    async def test_unrelated_secret_write_does_not_signal(self, tmp_path, client, app):
        from istota.location import ingest_signal

        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, app)
        await client.put(
            "/istota/api/settings/secrets/monarch/session_id",
            json={"value": "x"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert ingest_signal.reload_stamp(self._db_path) == 0.0

    async def test_generate_refused_when_location_is_off(self, tmp_path, client, app):
        """A token for a disabled module is a token that 403s on first use —
        the receiver only scans users with the module on. Refusing here says
        why; issuing it would be a silent dead end."""
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, app, user_id="bob")
        resp = await client.post(
            self._URL, cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 409

        from istota import secrets_store
        assert secrets_store.get_secret(
            self._db_path, "bob", "overland", "ingest_token",
        ) is None

    async def test_generate_requires_csrf_origin(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, app)
        resp = await client.post(
            self._URL, cookies=cookies,
            headers={"origin": "https://evil.example.com"},
        )
        assert resp.status_code == 403

    async def test_generate_requires_auth(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        resp = await client.post(
            self._URL, headers={"origin": "https://example.com"},
        )
        assert resp.status_code in (401, 403)

    async def test_token_is_not_readable_afterwards(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, app)
        resp = await client.post(
            self._URL, cookies=cookies, headers={"origin": "https://example.com"},
        )
        token = resp.json()["token"]

        listing = await client.get(
            "/istota/api/settings/module-services/location", cookies=cookies,
        )
        assert token not in listing.text
        info = await client.get(
            "/istota/api/location/settings-info", cookies=cookies,
        )
        assert token not in info.text


@_needs_web_deps
class TestMonarchLoginRoute:
    """The /money/monarch/login endpoint takes plaintext credentials,
    POSTs them to Monarch's /auth/login/ via the vendored client, and on
    success persists the returned session_id + csrftoken cookies. The
    plaintext credentials are never written to disk."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key" * 8)
        self._db_path = db_path

    def _make_test_config(self, tmp_path):
        cfg = _make_config(
            tmp_path,
            users={"alice": UserConfig(display_name="Alice")},
        )
        cfg.db_path = self._db_path
        return cfg

    async def _login_alice(self, client, app):
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "user_id": "alice",
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    async def test_login_persists_cookies_on_success(
        self, monkeypatch, tmp_path, client, app,
    ):
        from istota import secrets_store
        from istota.money._vendor.monarch_client import MonarchCookieAuth

        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)

        # Stub the vendored client so the test never hits the network.
        async def fake_login(*, email, password, mfa_totp=None, **kw):
            assert email == "alice@example.com"
            assert password == "hunter2"
            assert mfa_totp == "123456"
            return MonarchCookieAuth(session_id="SID-new", csrftoken="CSRF-new")

        monkeypatch.setattr(
            "istota.money._vendor.monarch_client.MonarchClient.login_with_credentials",
            staticmethod(fake_login),
        )

        resp = await client.post(
            "/istota/api/money/monarch/login",
            json={
                "email": "alice@example.com",
                "password": "hunter2",
                "mfa_totp": "123456",
            },
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}
        # Cookies were persisted ...
        assert secrets_store.get_secret(
            self._db_path, "alice", "monarch", "session_id",
        ) == "SID-new"
        assert secrets_store.get_secret(
            self._db_path, "alice", "monarch", "csrftoken",
        ) == "CSRF-new"
        # ... and plaintext password did NOT leak into secrets.
        assert secrets_store.get_secret(
            self._db_path, "alice", "monarch", "password",
        ) is None

    async def test_login_returns_412_when_mfa_required(
        self, monkeypatch, tmp_path, client, app,
    ):
        from istota.money._vendor.monarch_client import MonarchMFARequired

        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)

        async def fake_login(**kw):
            raise MonarchMFARequired("MFA token required")
        monkeypatch.setattr(
            "istota.money._vendor.monarch_client.MonarchClient.login_with_credentials",
            staticmethod(fake_login),
        )

        resp = await client.post(
            "/istota/api/money/monarch/login",
            json={"email": "a@b.com", "password": "x"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 412
        # Structured rather than prose: the client distinguishes the two
        # challenge kinds (and both from a bad password) on this code.
        detail = resp.json()["detail"]
        assert detail["code"] == "mfa_required"
        assert "MFA token required" in detail["message"]

    async def test_login_returns_503_when_cloudflare_blocks(
        self, monkeypatch, tmp_path, client, app,
    ):
        from istota.money._vendor.monarch_client import MonarchCloudflareBlocked

        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)

        async def fake_login(**kw):
            raise MonarchCloudflareBlocked("blocked")
        monkeypatch.setattr(
            "istota.money._vendor.monarch_client.MonarchClient.login_with_credentials",
            staticmethod(fake_login),
        )

        resp = await client.post(
            "/istota/api/money/monarch/login",
            json={"email": "a@b.com", "password": "x"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 503

    async def test_login_returns_401_on_bad_credentials(
        self, monkeypatch, tmp_path, client, app,
    ):
        from istota.money._vendor.monarch_client import MonarchAuthError

        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)

        async def fake_login(**kw):
            raise MonarchAuthError("wrong password")
        monkeypatch.setattr(
            "istota.money._vendor.monarch_client.MonarchClient.login_with_credentials",
            staticmethod(fake_login),
        )

        resp = await client.post(
            "/istota/api/money/monarch/login",
            json={"email": "a@b.com", "password": "x"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 401

    async def test_login_returns_structured_email_otp_challenge(
        self, monkeypatch, tmp_path, client, app,
    ):
        """The UI has to tell "we emailed you a code" apart from "wrong
        password" without matching on prose, so the code is machine-readable."""
        from istota.money._vendor.monarch_client import MonarchEmailOTPRequired

        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)

        async def fake_login(**kw):
            raise MonarchEmailOTPRequired("Retrieve the code from your email")
        monkeypatch.setattr(
            "istota.money._vendor.monarch_client.MonarchClient.login_with_credentials",
            staticmethod(fake_login),
        )

        resp = await client.post(
            "/istota/api/money/monarch/login",
            json={"email": "a@b.com", "password": "x"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 412
        assert resp.json()["detail"]["code"] == "email_otp_required"

    async def test_login_forwards_the_email_otp_code(
        self, monkeypatch, tmp_path, client, app,
    ):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)

        seen: dict = {}

        async def fake_login(**kw):
            seen.update(kw)
            from istota.money._vendor.monarch_client import MonarchCookieAuth
            return MonarchCookieAuth(session_id="s", csrftoken="c")
        monkeypatch.setattr(
            "istota.money._vendor.monarch_client.MonarchClient.login_with_credentials",
            staticmethod(fake_login),
        )

        resp = await client.post(
            "/istota/api/money/monarch/login",
            json={"email": "a@b.com", "password": "x", "email_otp": "820512"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert seen["email_otp"] == "820512"

    async def test_login_returns_400_on_missing_fields(
        self, tmp_path, client, app,
    ):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)

        resp = await client.post(
            "/istota/api/money/monarch/login",
            json={"email": "", "password": ""},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_login_requires_auth(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)

        resp = await client.post(
            "/istota/api/money/monarch/login",
            json={"email": "x", "password": "y"},
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 401

    async def test_login_rejects_bad_origin(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login_alice(client, app)

        resp = await client.post(
            "/istota/api/money/monarch/login",
            json={"email": "x", "password": "y"},
            cookies=cookies,
            headers={"origin": "https://evil.example.com"},
        )
        assert resp.status_code == 403


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
        """Drive the login through the OCS userinfo path so the test can
        supply a separate display name (NC's built-in OAuth2 token
        response does not carry display_name; production fetches it from
        OCS when the token's ``user_id`` is absent)."""
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"access_token": "stub"})
        mod._nc_oauth2_userinfo = AsyncMock(return_value={
            "id": username,
            "displayname": display,
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
        assert body["profile"]["briefing_email_html"] is True

    async def test_update_profile_briefing_email_html(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"briefing_email_html": False},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        from istota import user_profiles
        p = user_profiles.get_profile(self._db_path, "alice")
        assert p.briefing_email_html is False

        got = await client.get("/istota/api/settings/profile", cookies=cookies)
        assert got.json()["profile"]["briefing_email_html"] is False

    async def test_update_profile_briefing_email_html_rejects_non_bool(
        self, tmp_path, client, app,
    ):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"briefing_email_html": 3},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_get_profile_timezone_follow_location_defaults_off(
        self, tmp_path, client, app,
    ):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.get("/istota/api/settings/profile", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json()["profile"]["timezone_follow_location"] is False

    async def test_update_profile_timezone_follow_location(
        self, tmp_path, client, app,
    ):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"timezone_follow_location": True},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        from istota import user_profiles
        p = user_profiles.get_profile(self._db_path, "alice")
        assert p.timezone_follow_location is True

        got = await client.get("/istota/api/settings/profile", cookies=cookies)
        assert got.json()["profile"]["timezone_follow_location"] is True

    async def test_update_profile_timezone_follow_location_rejects_non_bool(
        self, tmp_path, client, app,
    ):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"timezone_follow_location": 3},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_get_profile_external_turn_display_defaults_collapsed(
        self, tmp_path, client, app,
    ):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.get("/istota/api/settings/profile", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json()["profile"]["external_turn_display"] == "collapsed"

    async def test_update_profile_external_turn_display(
        self, tmp_path, client, app,
    ):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"external_turn_display": "hidden"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        from istota import user_profiles
        p = user_profiles.get_profile(self._db_path, "alice")
        assert p.external_turn_display == "hidden"

        got = await client.get("/istota/api/settings/profile", cookies=cookies)
        assert got.json()["profile"]["external_turn_display"] == "hidden"

    async def test_external_turn_display_reaches_chat_config_without_a_restart(
        self, tmp_path, client, app,
    ):
        """The pane and the transcript must not disagree about the setting.

        `/chat/config` is the only place the chat store reads it, and it used to
        resolve it off `_config.users` — an in-memory snapshot rebuilt only at
        startup and on SIGHUP. `PUT /settings/profile` writes the row and
        deliberately syncs nothing in memory, so the pane showed the new value
        (its own read is live) while the transcript kept applying the old one
        until the web process was restarted.
        """
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        before = await client.get("/istota/api/chat/config", cookies=cookies)
        assert before.json()["external_turn_display"] == "collapsed"

        await client.put(
            "/istota/api/settings/profile",
            json={"external_turn_display": "full"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )

        after = await client.get("/istota/api/chat/config", cookies=cookies)
        assert after.json()["external_turn_display"] == "full"

    async def test_chat_config_carries_the_authenticated_username(
        self, tmp_path, client, app,
    ):
        """The send queue's storage key is built from this (ISSUE-238).

        A queued message is persisted per room under `<user>:room:<token>`,
        because a shared Talk room has one token across every member — so on a
        browser profile two people take turns using, a bare token would restore
        one person's unsent message into the other's transcript. It rides this
        endpoint rather than `GET /me` because the chat store awaits this one
        before anything else, and the restore happens in that same `init()`.
        """
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.get("/istota/api/chat/config", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "alice"

    async def test_update_profile_external_turn_display_rejects_unknown(
        self, tmp_path, client, app,
    ):
        # Three values, and a hand-supplied fourth is a 400 rather than a
        # silently stored string the client would fall back to `collapsed` on —
        # the pane would then show a selection nobody made.
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"external_turn_display": "sideways"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_update_profile_partial(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"timezone": "America/Los_Angeles", "log_channel": "alice-logs"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        from istota import user_profiles
        p = user_profiles.get_profile(self._db_path, "alice")
        assert p.timezone == "America/Los_Angeles"
        assert p.log_channel == "alice-logs"
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

    async def test_update_profile_routing_and_default_destination(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"default_destination": "both", "routing": {"alert": "email"}},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        from istota import user_profiles
        p = user_profiles.get_profile(self._db_path, "alice")
        assert p.default_destination == "both"
        assert p.routing == {"alert": "email"}

    async def test_update_profile_log_route_round_trips(self, tmp_path, client, app):
        # The log destination is stored as routing["log"] (D5: one descriptor
        # control, no separate raw log_channel field in the UI).
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"routing": {"log": "ntfy"}},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        from istota import user_profiles
        p = user_profiles.get_profile(self._db_path, "alice")
        assert p.routing == {"log": "ntfy"}
        # And it comes back on the GET so the card can render it.
        got = await client.get("/istota/api/settings/profile", cookies=cookies)
        assert got.json()["profile"]["routing"]["log"] == "ntfy"

    async def test_update_profile_log_route_none_disables(self, tmp_path, client, app):
        # "(off)" in the UI writes the explicit "none" sentinel so the log can be
        # turned off even when a log_channel is provisioned. It must validate.
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"routing": {"log": "none"}},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        from istota import user_profiles
        assert user_profiles.get_profile(self._db_path, "alice").routing == {"log": "none"}

    async def test_update_profile_rejects_unknown_log_route_surface(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"routing": {"log": "carrier-pigeon"}},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_update_profile_accepts_web_routing_surface(self, tmp_path, client, app):
        # Web chat is a registered, user-routable surface (ISSUE-121), so an
        # alert/log route to it is valid on the wire.
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"routing": {"alert": "web", "log": "web"}},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200

    async def test_update_profile_rejects_unknown_routing_surface(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"routing": {"alert": "carrier-pigeon"}},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_update_profile_rejects_unknown_routing_purpose(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"routing": {"bogus_purpose": "email"}},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

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
            json={"log_channel": "alice-only"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        from istota import user_profiles
        bob = user_profiles.get_profile(self._db_path, "bob")
        # Bob has no row yet; alice's update must not have created one.
        assert bob is None

    async def test_update_disabled_modules_takes_effect_immediately(self, tmp_path, client, app):
        # Regression: is_module_enabled must reflect the new value on the
        # very next call. Previously this only updated the user_profiles
        # row, while is_module_enabled kept reading the stale in-memory
        # UserConfig — the module stayed accessible until SIGHUP.
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        import istota.web_app as mod
        assert mod._config.is_module_enabled("alice", "feeds") is True
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"disabled_modules": ["feeds"]},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert mod._config.is_module_enabled("alice", "feeds") is False

    async def test_disabled_modules_rejects_unknown(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"disabled_modules": ["bogus_module"]},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_disabled_modules_accepts_health(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client, "alice", "Alice")
        resp = await client.put(
            "/istota/api/settings/profile",
            json={"disabled_modules": ["health"]},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200


@_needs_web_deps
class TestResourcesSettingsRemoved:
    """The /settings/resources surface was removed by the Resources sunset
    (folder mounts are now operator-only via `istota resource` / TOML; the
    web UI no longer lists resources). No API route handles it anymore — a
    GET falls through to the SvelteKit static catch-all as a 404, while a
    POST/DELETE hits the same catch-all and gets 405 (StaticFiles serves
    only GET/HEAD). Either proves the API handler is gone; the point is the
    request is never handled by a real resources endpoint."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key" * 8)
        self._cfg = _make_config(tmp_path)

    async def test_get_returns_404(self, client, app):
        resp = await client.get("/istota/api/settings/resources")
        assert resp.status_code == 404

    async def test_post_is_unhandled(self, client, app):
        resp = await client.post(
            "/istota/api/settings/resources",
            json={"type": "folder", "path": "/x"},
        )
        assert resp.status_code in (404, 405)

    async def test_delete_is_unhandled(self, client, app):
        resp = await client.delete("/istota/api/settings/resources/1")
        assert resp.status_code in (404, 405)


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
            "user_id": "alice",
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

    async def test_outputs_lists_only_routable_surfaces(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        cfg.talk.enabled = True
        cfg.email.enabled = True
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.get(
            "/istota/api/settings/briefings", cookies=cookies,
        )
        assert resp.status_code == 200
        outputs = set(resp.json()["outputs"])
        # User-routable surfaces are offered — including web chat (ISSUE-121).
        assert {"talk", "email", "ntfy", "web"} <= outputs
        # Self-routing / inline surfaces are never offered.
        assert "istota_file" not in outputs
        assert "repl" not in outputs
        assert "stream" not in outputs

    async def test_post_email_output_needs_no_token(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.post(
            "/istota/api/settings/briefings",
            json={"name": "e", "cron": "0 7 * * *", "output": "email"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200

    async def test_post_ntfy_output_accepted(self, tmp_path, client, app):
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.post(
            "/istota/api/settings/briefings",
            json={"name": "n", "cron": "0 7 * * *", "output": "ntfy"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200

    async def test_post_legacy_both_still_parses(self, tmp_path, client, app):
        """`both` is kept as a silent back-compat alias — token still required
        because it expands to a talk leg."""
        cfg = self._make_test_config(tmp_path)
        _patch_app(cfg)
        cookies = await self._login(client)
        resp = await client.post(
            "/istota/api/settings/briefings",
            json={
                "name": "legacy", "cron": "0 7 * * *",
                "output": "both", "conversation_token": "room1",
            },
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200

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


@_needs_web_deps
class TestChatUnreadRoomIndicators:
    """Stage 2 — unread counts in the rooms-list payload + mark-read endpoint."""

    @pytest.fixture(autouse=True)
    def _setup_db(self, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        self._db_path = db_path

    def _cfg(self, tmp_path):
        cfg = _make_config(tmp_path, users={"alice": UserConfig(display_name="Alice")})
        cfg.db_path = self._db_path
        return cfg

    async def _login(self, client):
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
            "user_id": "alice",
        })
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    def _room(self):
        from istota import db
        with db.get_db(self._db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
        return room

    async def test_list_includes_unread_count(self, tmp_path, client, app):
        from istota import db
        _patch_app(self._cfg(tmp_path))
        room = self._room()
        # first listing seeds the cursor at the (empty) max → 0, then we add
        # two assistant messages that should count as unread on the next list.
        import istota.web_app as mod
        mod._chat_list_rooms("alice")  # first-surface init
        with db.get_db(self._db_path) as conn:
            db.add_message(conn, room.token, role="assistant", body="a1", origin_surface="web")
            db.add_message(conn, room.token, role="assistant", body="a2", origin_surface="web")
        cookies = await self._login(client)
        resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
        assert resp.status_code == 200
        rooms = {r["token"]: r for r in resp.json()["rooms"]}
        assert rooms[room.token]["unread_count"] == 2

    async def test_first_listing_initializes_to_zero(self, tmp_path, client, app):
        from istota import db
        _patch_app(self._cfg(tmp_path))
        room = self._room()
        # pre-existing backlog before the room is ever listed in web
        with db.get_db(self._db_path) as conn:
            db.add_message(conn, room.token, role="assistant", body="old", origin_surface="web")
            db.add_message(conn, room.token, role="system", body="old2", origin_surface="web")
        cookies = await self._login(client)
        resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
        rooms = {r["token"]: r for r in resp.json()["rooms"]}
        # backlog treated as already-read → no flood
        assert rooms[room.token]["unread_count"] == 0

    async def test_mark_read_clears_count(self, tmp_path, client, app):
        from istota import db
        _patch_app(self._cfg(tmp_path))
        room = self._room()
        import istota.web_app as mod
        mod._chat_list_rooms("alice")
        with db.get_db(self._db_path) as conn:
            db.add_message(conn, room.token, role="assistant", body="a", origin_surface="web")
        cookies = await self._login(client)
        resp = await client.post(
            f"/istota/api/chat/rooms/{room.id}/read",
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
        rooms = {r["token"]: r for r in resp.json()["rooms"]}
        assert rooms[room.token]["unread_count"] == 0

    async def test_mark_read_foreign_room_404(self, tmp_path, client, app):
        from istota import db
        _patch_app(self._cfg(tmp_path))
        # a room owned by someone else
        with db.get_db(self._db_path) as conn:
            other = db.create_web_chat_room(conn, "mallory", "Secret")
        cookies = await self._login(client)
        resp = await client.post(
            f"/istota/api/chat/rooms/{other.id}/read",
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 404
        # no cursor written for the foreign room
        with db.get_db(self._db_path) as conn:
            assert db.get_room_read_state(conn, other.token, "web", "alice") == 0

    async def test_count_excludes_user_role_over_endpoint(self, tmp_path, client, app):
        from istota import db
        _patch_app(self._cfg(tmp_path))
        room = self._room()
        import istota.web_app as mod
        mod._chat_list_rooms("alice")
        with db.get_db(self._db_path) as conn:
            db.add_message(conn, room.token, role="user", body="my own turn", origin_surface="talk")
            db.add_message(conn, room.token, role="assistant", body="reply", origin_surface="web")
        cookies = await self._login(client)
        resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
        rooms = {r["token"]: r for r in resp.json()["rooms"]}
        assert rooms[room.token]["unread_count"] == 1


class TestBasemapEndpoint:
    """`GET /api/map/basemap` — the seam that replaced two hardcoded CARTO URLs.

    What these cover and the unit tests cannot: deployment config and a stored
    per-user key reaching the resolver, and the resolved key coming back
    embedded in a tile URL rather than as a readable field.
    """

    @pytest.fixture(autouse=True)
    def _secret_key(self, monkeypatch, tmp_path):
        from istota import db
        db_path = tmp_path / "basemap.db"
        db.init_db(db_path)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key" * 8)
        self._db_path = db_path

    def _cfg(self, tmp_path, **map_kwargs):
        from dataclasses import replace
        from istota.config import WebMapConfig

        cfg = _make_config(tmp_path, users={"alice": UserConfig(display_name="Alice")})
        cfg.db_path = self._db_path
        cfg.web = replace(cfg.web, map=WebMapConfig(**map_kwargs))
        return cfg

    async def _login_alice(self, client, app):
        import istota.web_app as mod
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(
            return_value={"user_id": "alice"}
        )
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    async def test_it_requires_a_session(self, tmp_path, client, app):
        _patch_app(self._cfg(tmp_path))
        resp = await client.get("/istota/api/map/basemap")
        assert resp.status_code in (401, 403)

    async def test_the_default_deployment_answers_with_a_keyless_provider(
        self, tmp_path, client, app,
    ):
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login_alice(client, app)
        resp = await client.get("/istota/api/map/basemap", cookies=cookies)
        assert resp.status_code == 200
        body = resp.json()
        assert body["provider"] == "openfreemap"
        assert body["needs_key"] is False
        assert "cartocdn" not in body["dark"]

    async def test_deployment_config_reaches_the_browser(self, tmp_path, client, app):
        _patch_app(self._cfg(tmp_path, provider="osm"))
        cookies = await self._login_alice(client, app)
        body = (await client.get("/istota/api/map/basemap", cookies=cookies)).json()
        assert body["provider"] == "osm"
        assert body["kind"] == "raster"

    async def test_a_stored_user_key_selects_carto_and_lands_in_the_tile_url(
        self, tmp_path, client, app,
    ):
        """The whole point of the settings input: paste a key, get a map."""
        from istota import secrets_store

        _patch_app(self._cfg(tmp_path))
        secrets_store.set_secret(self._db_path, "alice", "carto", "api_key", "alicekey")
        cookies = await self._login_alice(client, app)
        body = (await client.get("/istota/api/map/basemap", cookies=cookies)).json()
        assert body["provider"] == "carto"
        assert body["needs_key"] is False
        assert "api_key=alicekey" in body["dark"]

    async def test_the_key_is_never_returned_as_a_field_of_its_own(
        self, tmp_path, client, app,
    ):
        """The secrets store stays write-only to the browser as a *value*."""
        from istota import secrets_store

        _patch_app(self._cfg(tmp_path))
        secrets_store.set_secret(self._db_path, "alice", "carto", "api_key", "alicekey")
        cookies = await self._login_alice(client, app)
        body = (await client.get("/istota/api/map/basemap", cookies=cookies)).json()
        assert "api_key" not in body

    async def test_one_users_key_does_not_reach_another_user(
        self, tmp_path, client, app,
    ):
        from istota import secrets_store

        _patch_app(self._cfg(tmp_path))
        secrets_store.set_secret(self._db_path, "bob", "carto", "api_key", "bobkey")
        cookies = await self._login_alice(client, app)
        body = (await client.get("/istota/api/map/basemap", cookies=cookies)).json()
        assert "bobkey" not in body["dark"]
        assert body["provider"] == "openfreemap"

    async def test_a_deployment_key_applies_when_the_user_has_none(
        self, tmp_path, client, app,
    ):
        _patch_app(self._cfg(tmp_path, provider="carto", api_key="deploykey"))
        cookies = await self._login_alice(client, app)
        body = (await client.get("/istota/api/map/basemap", cookies=cookies)).json()
        assert "api_key=deploykey" in body["dark"]

    async def test_carto_with_no_key_anywhere_serves_the_fallback_not_watermarks(
        self, tmp_path, client, app,
    ):
        """The reported bug, end to end: the browser must never get those URLs."""
        _patch_app(self._cfg(tmp_path, provider="carto"))
        cookies = await self._login_alice(client, app)
        body = (await client.get("/istota/api/map/basemap", cookies=cookies)).json()
        assert "cartocdn" not in body["dark"]
        assert "cartocdn" not in body["light"]
        assert body["needs_key"] is True

    async def test_a_whitespace_key_does_not_override_a_working_deployment_key(
        self, tmp_path, client, app,
    ):
        from istota import secrets_store

        _patch_app(self._cfg(tmp_path, provider="carto", api_key="deploykey"))
        secrets_store.set_secret(self._db_path, "alice", "carto", "api_key", "   ")
        cookies = await self._login_alice(client, app)
        body = (await client.get("/istota/api/map/basemap", cookies=cookies)).json()
        assert "api_key=deploykey" in body["dark"]
        assert "%20" not in body["dark"]

    async def test_a_broken_secrets_store_still_returns_a_usable_map(
        self, tmp_path, client, app, monkeypatch,
    ):
        """A basemap that 500s is a blank rectangle, which is the failure again."""
        import istota.secrets_store as store

        def _boom(*args, **kwargs):
            raise RuntimeError("db is gone")

        _patch_app(self._cfg(tmp_path))
        monkeypatch.setattr(store, "get_secret", _boom)
        cookies = await self._login_alice(client, app)
        resp = await client.get("/istota/api/map/basemap", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json()["provider"] == "openfreemap"

    async def test_the_carto_service_card_is_offered_on_the_location_module(
        self, tmp_path, client, app,
    ):
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login_alice(client, app)
        resp = await client.get(
            "/istota/api/settings/module-services/location", cookies=cookies
        )
        assert resp.status_code == 200
        cards = {s["service"]: s for s in resp.json()["services"]}
        assert "carto" in cards
        assert [f["key"] for f in cards["carto"]["fields"]] == ["api_key"]
        assert cards["carto"]["status"] == "missing"
        assert "carto.com" in cards["carto"]["hint"]

    async def test_adding_the_carto_card_did_not_disturb_the_overland_one(
        self, tmp_path, client, app,
    ):
        """A second field on `overland` would have flipped it to partial."""
        from istota import secrets_store

        _patch_app(self._cfg(tmp_path))
        secrets_store.set_secret(
            self._db_path, "alice", "overland", "ingest_token", "tok"
        )
        cookies = await self._login_alice(client, app)
        resp = await client.get(
            "/istota/api/settings/module-services/location", cookies=cookies
        )
        cards = {s["service"]: s for s in resp.json()["services"]}
        assert cards["overland"]["status"] == "configured"

    async def test_the_key_can_be_saved_through_the_ordinary_secrets_route(
        self, tmp_path, client, app,
    ):
        _patch_app(self._cfg(tmp_path))
        cookies = await self._login_alice(client, app)
        resp = await client.put(
            "/istota/api/settings/secrets/carto/api_key",
            json={"value": "typed-in-the-ui"},
            cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = (await client.get("/istota/api/map/basemap", cookies=cookies)).json()
        assert "api_key=typed-in-the-ui" in body["dark"]
