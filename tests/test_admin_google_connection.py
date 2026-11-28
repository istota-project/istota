"""Google configuration facts on the real admin Connections index."""

from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("authlib")
pytest.importorskip("fastapi")

from httpx import ASGITransport, AsyncClient, AsyncHTTPTransport, HTTPTransport

from istota import db
from istota.config import Config, GoogleWorkspaceConfig, SiteConfig, UserConfig, WebConfig

INDEX = "/istota/api/admin/connections"
SECRET = "synthetic-google-secret-for-redaction-test"
SCOPE_BASE = "https://www.googleapis.com/auth/"


@pytest.fixture
def config(tmp_path):
    config = Config(
        db_path=tmp_path / "istota.db",
        workspace_path=tmp_path / "workspace",
        site=SiteConfig(hostname="example.com"),
        users={"alice": UserConfig(), "bob": UserConfig()},
        web=WebConfig(
            enabled=True,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="example-web-client",
            oauth2_client_secret="test-secret",
            session_secret_key="test-session-key",
        ),
        google_workspace=GoogleWorkspaceConfig(
            enabled=True,
            client_id="example-google-client",
            client_secret=SECRET,
            scopes=[f"{SCOPE_BASE}drive.readonly", f"{SCOPE_BASE}gmail.modify"],
        ),
    )
    config.admin_users = {"alice"}
    db.init_db(config.db_path)
    return config


@pytest.fixture(autouse=True)
def no_outbound_http(monkeypatch):
    sync = MagicMock(side_effect=AssertionError("Connections must not probe HTTP"))
    async_ = AsyncMock(side_effect=AssertionError("Connections must not probe HTTP"))
    monkeypatch.setattr(HTTPTransport, "handle_request", sync)
    monkeypatch.setattr(AsyncHTTPTransport, "handle_async_request", async_)
    yield
    sync.assert_not_called()
    async_.assert_not_called()


@pytest.fixture
def index(config, monkeypatch):
    import istota.webui.app as mod

    monkeypatch.setattr(mod, "_config", config)
    monkeypatch.setattr(mod.app.state, "istota_config", config, raising=False)
    oauth = MagicMock()
    monkeypatch.setattr(mod, "_oauth", oauth)

    async def get(username="alice", base_url="https://example.com"):
        async with AsyncClient(transport=ASGITransport(app=mod.app),
                               base_url=base_url) as client:
            if username is not None:
                oauth.nextcloud.authorize_access_token = AsyncMock(
                    return_value={"user_id": username},
                )
                login = await client.get("/istota/callback", follow_redirects=False)
                assert login.status_code == 302
            return await client.get(INDEX)

    return get


def google_entry(response):
    assert response.status_code == 200, response.text
    entries = response.json()["connections"]
    assert [entry["id"] for entry in entries] == [
        "nextcloud", "whatsapp", "google_workspace", "servarr",
    ]
    return entries[2]


async def test_configured_facts_and_exact_secret_safe_shape(index):
    response = await index()
    entry = google_entry(response)
    assert set(entry) == {
        "id", "label", "enabled", "client_id", "client_secret_set", "redirect_uri",
        "services", "additional_scopes", "missing", "status",
    }
    assert entry["label"] == "Google Workspace"
    assert entry["enabled"] is True
    assert entry["client_id"] == "example-google-client"
    assert entry["client_secret_set"] is True
    assert entry["redirect_uri"] == "https://example.com/istota/google/callback"
    assert entry["status"] == "configured"
    assert entry["missing"] == []
    assert SECRET not in response.text
    assert entry["services"] == [
        {"service": "drive", "label": "Drive", "max_level": "readonly"},
        {"service": "gmail", "label": "Gmail", "max_level": "full"},
        {"service": "calendar", "label": "Calendar", "max_level": "off"},
        {"service": "sheets", "label": "Sheets", "max_level": "off"},
        {"service": "docs", "label": "Docs", "max_level": "off"},
        {"service": "chat", "label": "Chat", "max_level": "off"},
        {"service": "contacts", "label": "Contacts", "max_level": "off"},
    ]
    assert entry["additional_scopes"] == []


@pytest.mark.parametrize("enabled", [False, True])
async def test_disabled_or_incomplete_with_ordered_missing_fields(config, index, enabled):
    config.google_workspace = GoogleWorkspaceConfig(
        enabled=enabled, client_id="  ", client_secret=" \t", scopes=[],
    )
    config.site.hostname = ""
    entry = google_entry(await index())
    assert entry["enabled"] is enabled
    assert entry["status"] == ("incomplete" if enabled else "disabled")
    assert entry["missing"] == ["client_id", "client_secret", "site_hostname", "scopes"]
    assert entry["client_secret_set"] is False
    assert entry["redirect_uri"] is None


async def test_disabled_even_with_complete_configuration(config, index):
    config.google_workspace.enabled = False
    entry = google_entry(await index())
    assert entry["status"] == "disabled"
    assert entry["missing"] == []


async def test_missing_hostname_keeps_other_cards_available(config, index):
    config.site.hostname = ""
    entry = google_entry(await index())
    assert entry["status"] == "incomplete"
    assert entry["missing"] == ["site_hostname"]
    assert entry["redirect_uri"] is None


@pytest.mark.parametrize("hostname", ["localhost:8766", "127.0.0.1:8766"])
async def test_loopback_callback_uses_canonical_origin(config, index, hostname):
    config.site.hostname = hostname
    entry = google_entry(await index())
    assert entry["redirect_uri"] == f"http://{hostname}/istota/google/callback"
    assert entry["status"] == "configured"


@pytest.mark.parametrize("additional", [False, True])
async def test_empty_and_additional_only_scope_lists(config, index, additional):
    scopes = [f"{SCOPE_BASE}drive.file"] if additional else []
    config.google_workspace.scopes = scopes
    entry = google_entry(await index())
    assert len(entry["services"]) == 7
    assert all(row["max_level"] == "off" for row in entry["services"])
    assert entry["additional_scopes"] == scopes
    assert entry["status"] == ("configured" if additional else "incomplete")
    assert entry["missing"] == ([] if additional else ["scopes"])


async def test_mapped_and_additional_scopes_are_both_preserved(config, index):
    config.google_workspace.scopes += [f"{SCOPE_BASE}drive.file"]
    entry = google_entry(await index())
    assert entry["services"][0]["max_level"] == "readonly"
    assert entry["additional_scopes"] == [f"{SCOPE_BASE}drive.file"]


@pytest.mark.parametrize("username,status", [("alice", 200), ("bob", 403), (None, 401)])
async def test_admin_gate(index, username, status):
    response = await index(username)
    assert response.status_code == status
    if status == 200:
        google_entry(response)
    else:
        assert SECRET not in response.text
        assert "example-google-client" not in response.text


async def test_empty_admin_allowlist_refuses_authenticated_user(config, index):
    config.admin_users = set()
    assert (await index()).status_code == 403


async def test_local_no_auth_user_keeps_access(config, index):
    config.web.auth = ["none"]
    config.users = {"alice": UserConfig()}
    config.admin_users = set()
    assert config.local_user_id == "alice"
    # No-auth answers only at its loopback names (ISSUE-778).
    response = await index(None, base_url="http://localhost:8766")
    assert google_entry(response)["status"] == "configured"


async def test_unloaded_config_retains_503(index, monkeypatch):
    import istota.webui.app as mod

    # With no config the real auth dependency refuses before reaching this guard.
    monkeypatch.setitem(mod.app.dependency_overrides, mod._require_admin, lambda: {})
    monkeypatch.setattr(mod, "_config", None)
    response = await index(None)
    assert response.status_code == 503
    assert response.json() == {"detail": "config not loaded"}


# ---------------------------------------------------------------------------
# Connecting Google from a secret link (#994, secret-entry-links Stage 6)
# ---------------------------------------------------------------------------

DRIVE_RO = f"{SCOPE_BASE}drive.readonly"
GMAIL_MODIFY = f"{SCOPE_BASE}gmail.modify"
CALLBACK = "/istota/google/callback"
CONNECTED = "/istota/google/connected"


class TestConnectingFromALink:
    """A `google_connect` link opens a page with no login; its POST spends the
    link, binds the link's user to the browser session and starts Google's
    consent with that user's scopes, and the callback stores the token for
    that user and nobody else."""

    @pytest.fixture
    def app(self, config, monkeypatch):
        import istota.webui.app as mod
        from starlette.responses import RedirectResponse

        from istota import user_profiles

        monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
        config.bot_name = "Ada"
        config.users["alice"].display_name = "Alice"
        monkeypatch.setattr(mod, "_config", config)
        monkeypatch.setattr(mod.app.state, "istota_config", config, raising=False)
        oauth = MagicMock()
        oauth.google.authorize_redirect = AsyncMock(
            return_value=RedirectResponse("https://accounts.google.com/o/oauth2/auth"))
        oauth.google.authorize_access_token = AsyncMock(return_value={
            "access_token": "test-access", "refresh_token": "test-refresh",
            "expires_in": 3600, "scope": DRIVE_RO,
        })
        monkeypatch.setattr(mod, "_oauth", oauth)
        for user_id, name in (("alice", "Alice"), ("bob", "Bob")):
            user_profiles.ensure_profile(config.db_path, user_id, display_name=name)
        return mod

    @pytest.fixture
    async def client(self, app):
        async with AsyncClient(transport=ASGITransport(app=app.app),
                               base_url="https://example.com") as client:
            yield client

    def _issue(self, config, user_id="alice"):
        from istota.webui import secret_links

        with db.get_db(config.db_path) as conn:
            room = db.create_web_chat_room(conn, user_id, "general").token
            task_id = db.create_task(conn, user_id=user_id, source_type="web",
                                     prompt="connect google", conversation_token=room)
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            token, link = secret_links.issue(conn, user_id=user_id, intent="google_connect",
                                             params={}, task_id=task_id, room_token=room,
                                             ttl_seconds=900)
        return token, link

    def _row(self, config, link_id):
        with db.get_db(config.db_path) as conn:
            return dict(conn.execute("SELECT * FROM secret_links WHERE id=?", (link_id,)).fetchone())

    def _scopes(self, config, user_id):
        with db.get_db(config.db_path) as conn:
            return db.get_google_scopes(conn, user_id)

    def _notices(self, config, user_id="alice"):
        with db.get_db(config.db_path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM notifications WHERE user_id=? ORDER BY id", (user_id,))]

    async def _csrf(self, client, token):
        import re

        page = await client.get("/istota/secret", params={"token": token})
        assert page.status_code == 200, page.text
        return re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]

    async def _post(self, client, token, action="connect", csrf=None):
        if csrf is None:
            csrf = await self._csrf(client, token)
        return await client.post("/istota/secret",
                                 data={"token": token, "csrf_token": csrf, "action": action})

    async def _sign_in(self, app, client, username):
        app._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": username})
        assert (await client.get("/istota/callback", follow_redirects=False)).status_code == 302

    async def test_the_page_names_the_account_and_services_and_writes_nothing(
            self, app, client, config):
        token, link = self._issue(config)
        before = self._row(config, link.id)
        for _ in range(3):
            page = await client.get("/istota/secret", params={"token": token})
        assert page.status_code == 200
        assert "Connect Google" in page.text and "Alice" in page.text
        for words in ("Drive", "Read-only", "Gmail", "Read and write", "Continue to Google",
                      "Cancel this link"):
            assert words in page.text, words
        assert page.headers["content-security-policy"] == "frame-ancestors 'none'"
        assert page.headers["cache-control"] == "no-store"
        assert self._row(config, link.id) == before
        app._oauth.google.authorize_redirect.assert_not_awaited()

    async def test_continue_goes_to_google_with_the_users_own_scopes(self, app, client, config):
        from istota import user_profiles

        user_profiles.update_profile(config.db_path, "alice",
                                     google_scopes={"drive": "readonly", "gmail": "off"})
        token, link = self._issue(config)
        response = await self._post(client, token)
        assert response.status_code in (302, 307)
        assert response.headers["location"] == "https://accounts.google.com/o/oauth2/auth"
        redirect = app._oauth.google.authorize_redirect
        redirect.assert_awaited_once()
        assert redirect.call_args.args[1] == "https://example.com/istota/google/callback"
        assert redirect.call_args.kwargs == {"scope": DRIVE_RO, "access_type": "offline",
                                             "prompt": "consent"}
        row = self._row(config, link.id)
        assert row["used_at"] is not None and row["outcome"] == "consent_started"
        # The link is spent: a second continue goes nowhere near Google.
        page = await client.get("/istota/secret", params={"token": token})
        assert page.status_code == 400 and "expired or was already used" in page.text

    async def test_the_callback_stores_the_token_for_the_link_user_and_clears_the_binding(
            self, app, client, config):
        token, link = self._issue(config)
        await self._post(client, token)
        response = await client.get(CALLBACK)
        assert response.status_code == 302 and response.headers["location"] == CONNECTED
        assert self._scopes(config, "alice") == [DRIVE_RO]
        assert self._scopes(config, "bob") is None
        assert self._row(config, link.id)["outcome"] == "connected"
        (notice,) = self._notices(config)
        assert notice["title"] == "Google connected"
        again = await client.get(CALLBACK)
        assert again.status_code == 302 and again.headers["location"] == "/istota/login"
        app._oauth.google.authorize_access_token.assert_awaited_once()

    async def test_an_abandoned_consent_leaves_the_link_consent_started(self, app, client, config):
        # #1000: pressing Continue is not a connection. Nothing came back from Google.
        token, link = self._issue(config)
        await self._post(client, token)
        row = self._row(config, link.id)
        assert row["used_at"] is not None and row["outcome"] == "consent_started"
        assert self._scopes(config, "alice") is None
        assert self._notices(config) == []

    async def test_a_replayed_binding_for_a_connected_link_is_refused(self, app, client, config):
        # The session cookie from the POST, sent again after the link already
        # connected, must not store a second token with no login behind it.
        token, link = self._issue(config)
        await self._post(client, token)
        bound = dict(client.cookies)
        assert (await client.get(CALLBACK)).headers["location"] == CONNECTED
        app._oauth.google.authorize_access_token = AsyncMock(return_value={
            "access_token": "other-access", "refresh_token": "other-refresh",
            "expires_in": 3600, "scope": GMAIL_MODIFY,
        })
        client.cookies.clear()
        client.cookies.update(bound)
        response = await client.get(CALLBACK)
        assert response.status_code == 400 and "Google wasn" in response.text
        assert self._scopes(config, "alice") == [DRIVE_RO]
        assert self._row(config, link.id)["outcome"] == "connected"
        assert len(self._notices(config)) == 1

    async def test_the_link_and_the_token_are_written_together(
            self, app, client, config, monkeypatch):
        # `upsert_google_token` commits, so the link must be marked before it:
        # a failed mark stores no token and ends on the card, not a 500.
        import sqlite3

        from istota.webui import secret_links

        def locked(conn, link_id):
            raise sqlite3.OperationalError("database is locked")

        token, link = self._issue(config)
        await self._post(client, token)
        monkeypatch.setattr(secret_links, "finish_consent", locked)
        response = await client.get(CALLBACK)
        assert response.status_code == 400 and "Google wasn" in response.text
        assert self._scopes(config, "alice") is None
        assert self._row(config, link.id)["outcome"] == "consent_started"
        assert self._notices(config) == []

    async def test_a_callback_with_neither_login_nor_link_goes_to_login(self, app, client, config):
        response = await client.get(CALLBACK)
        assert response.status_code == 302 and response.headers["location"] == "/istota/login"
        app._oauth.google.authorize_access_token.assert_not_awaited()
        assert self._scopes(config, "alice") is None

    async def test_a_binding_past_the_link_lifetime_goes_to_login(
            self, app, client, config, monkeypatch):
        token, _ = self._issue(config)
        await self._post(client, token)
        later = app.time.time() + config.secret_links.ttl_seconds + 5
        monkeypatch.setattr(app.time, "time", lambda: later)
        response = await client.get(CALLBACK)
        assert response.status_code == 302 and response.headers["location"] == "/istota/login"
        assert self._scopes(config, "alice") is None

    async def test_a_failed_exchange_ends_on_the_card(self, app, client, config):
        app._oauth.google.authorize_access_token = AsyncMock(
            side_effect=RuntimeError("provider said access_denied for code=abc"))
        token, link = self._issue(config)
        await self._post(client, token)
        response = await client.get(CALLBACK)
        assert response.status_code == 400
        assert "Google wasn" in response.text and "code=abc" not in response.text
        assert self._scopes(config, "alice") is None
        assert self._row(config, link.id)["outcome"] == "consent_started"
        assert (await client.get(CALLBACK)).headers["location"] == "/istota/login"

    async def test_no_refresh_token_ends_on_the_card(self, app, client, config):
        app._oauth.google.authorize_access_token = AsyncMock(return_value={"access_token": "a"})
        token, _ = self._issue(config)
        await self._post(client, token)
        response = await client.get(CALLBACK)
        assert response.status_code == 400 and "Google wasn" in response.text
        assert self._scopes(config, "alice") is None

    async def test_an_account_removed_since_stores_nothing(self, app, client, config):
        token, _ = self._issue(config)
        await self._post(client, token)
        del config.users["alice"]
        response = await client.get(CALLBACK)
        assert response.status_code == 400 and "Google wasn" in response.text
        assert self._scopes(config, "alice") is None

    async def test_a_signed_in_owner_lands_on_the_connected_page(self, app, client, config):
        await self._sign_in(app, client, "alice")
        token, link = self._issue(config)
        await self._post(client, token)
        response = await client.get(CALLBACK)
        assert response.status_code == 302 and response.headers["location"] == CONNECTED
        assert self._scopes(config, "alice") == [DRIVE_RO]
        assert self._row(config, link.id)["outcome"] == "connected"

    async def test_another_accounts_session_is_refused(self, app, client, config):
        await self._sign_in(app, client, "bob")
        token, link = self._issue(config)
        page = await client.get("/istota/secret", params={"token": token})
        assert page.status_code == 403 and "another account" in page.text
        assert self._row(config, link.id)["used_at"] is None

    async def test_a_post_without_its_form_token_is_refused(self, app, client, config):
        token, link = self._issue(config)
        await client.get("/istota/secret", params={"token": token})
        response = await self._post(client, token, csrf="forged")
        assert response.status_code == 403
        assert self._row(config, link.id)["used_at"] is None
        app._oauth.google.authorize_redirect.assert_not_awaited()

    async def test_cancel_spends_the_link_and_tells_the_owner(self, app, client, config):
        token, link = self._issue(config)
        response = await self._post(client, token, action="cancel")
        assert response.status_code == 200 and "Link cancelled" in response.text
        assert self._row(config, link.id)["outcome"] == "cancelled"
        (notice,) = self._notices(config)
        assert "cancelled" in notice["title"]
        app._oauth.google.authorize_redirect.assert_not_awaited()

    async def test_without_google_here_the_page_says_so_and_the_link_stays_live(
            self, app, client, config, monkeypatch):
        monkeypatch.setattr(app, "_oauth", MagicMock(spec=["nextcloud"]))
        token, link = self._issue(config)
        page = await client.get("/istota/secret", params={"token": token})
        assert page.status_code == 503 and "set up" in page.text
        assert "Continue to Google" not in page.text
        assert self._row(config, link.id)["used_at"] is None

    async def test_no_services_selected_since_issue_keeps_the_link(self, app, client, config):
        from istota import user_profiles

        token, link = self._issue(config)
        csrf = await self._csrf(client, token)
        user_profiles.update_profile(config.db_path, "alice",
                                     google_scopes={"drive": "off", "gmail": "off"})
        page = await client.get("/istota/secret", params={"token": token})
        assert "Continue to Google" not in page.text and "Settings" in page.text
        response = await self._post(client, token, csrf=csrf)
        assert response.status_code == 400
        assert self._row(config, link.id)["used_at"] is None
        app._oauth.google.authorize_redirect.assert_not_awaited()

    async def test_the_connected_page(self, app, client):
        page = await client.get(CONNECTED)
        assert page.status_code == 200
        assert "Google connected" in page.text and "lucide-circle-check" in page.text
