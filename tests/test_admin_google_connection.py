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
