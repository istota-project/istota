"""Settings > Plugins: the installed skill plugins and their secrets.

The properties that matter: a plugin and its `plugin_*` service are listed,
and the service writable, only for a user who can use that plugin; no response
carries a stored credential; and an operator entry over a plugin cannot make a
built-in service (`mail_account`) writable through these routes.
"""

import secrets
from unittest.mock import AsyncMock

import pytest

from istota.credentials import store
from tests.support.plugin_fixture import build_plugins, render_skill_md
from tests.test_web_app import app, client, config  # noqa: F401 -- shared fixtures

SERVICES = "/istota/api/settings/services"
PLUGINS = "/istota/api/settings/plugins"
SECRETS = "/istota/api/settings/secrets"
ORIGIN = {"Origin": "https://example.com"}
MARKER = "synthetic-plugin-token-7c1d"
URL = "https://acme.example.com"


@pytest.fixture
def plugin_builder(tmp_path):
    yield from build_plugins(tmp_path)


async def login(http, user):
    import istota.webui.app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": user})
    response = await http.get("/istota/callback", follow_redirects=False)
    assert response.status_code in (302, 303, 307), response.text


@pytest.fixture
async def signed_client(client, config, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "plugin-settings-key-" * 3)
    monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
    ops = tmp_path / "ops"
    ops.mkdir()
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    config.skills_dir = ops
    config.bundled_skills_dir = bundled
    await login(client, "alice")
    return client


async def listing(http):
    response = await http.get(PLUGINS)
    assert response.status_code == 200, response.text
    return response.json()


async def cards(http):
    return {card["service"]: card for card in (await listing(http))["services"]}


async def test_an_installed_plugin_is_listed(signed_client, plugin_builder):
    p = plugin_builder.installed()

    assert (await listing(signed_client))["plugins"] == [{
        "skill": p.skill,
        "description": "Example plugin skill",
        "plugin": f"{p.dist_name} {p.version}",
        "disabled": False,
    }]


async def test_no_plugin_lists_nothing(signed_client):
    assert await listing(signed_client) == {"plugins": [], "services": []}


async def test_a_plugin_with_no_secret_is_listed_with_no_card(signed_client, plugin_builder):
    p = plugin_builder.installed(env=[])

    body = await listing(signed_client)

    assert [row["skill"] for row in body["plugins"]] == [p.skill]
    assert body["services"] == []


async def test_a_plugin_the_user_disabled_is_listed_as_disabled(signed_client, config, plugin_builder):  # noqa: F811
    p = plugin_builder.installed()
    config.users["alice"].disabled_skills = [p.skill]

    body = await listing(signed_client)

    assert [(row["skill"], row["disabled"]) for row in body["plugins"]] == [(p.skill, True)]
    assert "plugin_example" in {card["service"] for card in body["services"]}


async def put(http, service, key, value=MARKER):
    return await http.put(f"{SECRETS}/{service}/{key}", json={"value": value}, headers=ORIGIN)


async def test_a_plugin_service_has_a_card(signed_client, plugin_builder):
    p = plugin_builder.installed()

    card = (await cards(signed_client))["plugin_example"]

    assert card["label"] == "plugin_example"
    assert card["fields"] == [{"key": "token", "label": "token", "type": "password"}]
    assert card["used_by"] == [p.skill]
    assert card["status"] == "missing"
    assert card["configured_keys"] == []
    assert p.dist_name in card["hint"]
    assert card["custom_ui"] is False and card["oauth"] is False
    # Connections lists the built-in services only.
    connected = (await signed_client.get(SERVICES)).json()["services"]
    assert "plugin_example" not in {entry["service"] for entry in connected}
    assert "karakeep" in {entry["service"] for entry in connected}


async def test_a_plugin_secret_is_stored_and_never_returned(signed_client, config, plugin_builder):  # noqa: F811
    plugin_builder.installed()

    response = await put(signed_client, "plugin_example", "token")

    assert response.status_code == 200, response.text
    assert MARKER not in response.text
    assert store.get_secret(config.db_path, "alice", "plugin_example", "token") == MARKER
    assert store.get_secret(config.db_path, "bob", "plugin_example", "token") is None
    answer = await signed_client.get(PLUGINS)
    assert MARKER not in answer.text
    card = {c["service"]: c for c in answer.json()["services"]}["plugin_example"]
    assert card["status"] == "configured"
    assert card["configured_keys"] == ["token"]
    assert card["values"] == {}


def _url_and_token_plugin(plugin_builder, **extra_spec):
    """A plugin declaring a setting and a credential under one service."""
    skill = f"acme_{secrets.token_hex(4)}"
    prefix = f"PLUGIN_{skill.upper()}"
    env = [
        {"var": f"{prefix}_URL", "from": "secret", "service": "plugin_acme", "key": "url"},
        {"var": f"{prefix}_TOKEN", "from": "secret", "service": "plugin_acme", "key": "token",
         "sensitive": True},
    ]
    if extra_spec:
        env.append({"var": f"{prefix}_EXTRA", "from": "secret", "service": "plugin_acme", **extra_spec})
    return plugin_builder.installed(skill=skill, env=env)


async def test_an_entry_not_marked_sensitive_is_a_plain_field(signed_client, config, plugin_builder):  # noqa: F811
    _url_and_token_plugin(plugin_builder)

    card = (await cards(signed_client))["plugin_acme"]
    assert card["fields"] == [
        {"key": "token", "label": "token", "type": "password"},
        {"key": "url", "label": "url", "type": "text", "plain": True},
    ]

    assert (await put(signed_client, "plugin_acme", "url", URL)).status_code == 200
    card = (await cards(signed_client))["plugin_acme"]
    assert card["status"] == "partial"
    assert card["values"] == {"url": URL}

    assert (await put(signed_client, "plugin_acme", "token")).status_code == 200
    answer = await signed_client.get(PLUGINS)
    assert MARKER not in answer.text
    card = {c["service"]: c for c in answer.json()["services"]}["plugin_acme"]
    assert card["status"] == "configured"
    assert card["values"] == {"url": URL}


async def test_a_key_any_entry_marks_sensitive_is_never_returned(signed_client, plugin_builder):
    # The same key read into a second variable, this one marked sensitive.
    _url_and_token_plugin(plugin_builder, key="url", sensitive=True)

    assert (await put(signed_client, "plugin_acme", "url", URL)).status_code == 200

    answer = await signed_client.get(PLUGINS)
    assert URL not in answer.text
    card = {c["service"]: c for c in answer.json()["services"]}["plugin_acme"]
    assert {"key": "url", "label": "url", "type": "password"} in card["fields"]
    assert card["values"] == {}


async def test_a_key_the_plugin_does_not_declare_is_refused(signed_client, config, plugin_builder):  # noqa: F811
    plugin_builder.installed()

    response = await put(signed_client, "plugin_example", "other")
    assert response.status_code == 400
    assert store.get_secret(config.db_path, "alice", "plugin_example", "other") is None

    response = await signed_client.delete(f"{SECRETS}/plugin_example/other", headers=ORIGIN)
    assert response.status_code == 400


async def test_a_stored_plugin_secret_is_cleared(signed_client, config, plugin_builder):  # noqa: F811
    plugin_builder.installed()
    assert (await put(signed_client, "plugin_example", "token")).status_code == 200

    response = await signed_client.delete(f"{SECRETS}/plugin_example/token", headers=ORIGIN)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": True}
    assert store.get_secret(config.db_path, "alice", "plugin_example", "token") is None
    assert (await cards(signed_client))["plugin_example"]["status"] == "missing"


async def test_a_service_no_plugin_declares_is_unknown(signed_client, config):  # noqa: F811
    assert "plugin_example" not in await cards(signed_client)

    response = await put(signed_client, "plugin_example", "token")

    assert response.status_code == 404
    assert store.get_secret(config.db_path, "alice", "plugin_example", "token") is None
    response = await signed_client.delete(f"{SECRETS}/plugin_example/token", headers=ORIGIN)
    assert response.status_code == 404


async def test_an_admin_only_plugin_is_not_a_non_admins(signed_client, config, plugin_builder):  # noqa: F811
    # Two users and no admins file: nobody is an admin.
    assert not config.is_admin("alice")
    plugin_builder.installed(extra={"admin_only": True})

    assert await listing(signed_client) == {"plugins": [], "services": []}
    response = await put(signed_client, "plugin_example", "token")

    assert response.status_code == 404
    assert store.get_secret(config.db_path, "alice", "plugin_example", "token") is None


async def test_an_operator_entry_over_a_plugin_cannot_open_a_built_in_service(
    signed_client, config, plugin_builder,  # noqa: F811
):
    p = plugin_builder.installed()
    override = config.skills_dir / p.skill
    override.mkdir()
    (override / "skill.md").write_text(render_skill_md({"description": "operator doc", "cli": True, "env": [
        {"var": "OPS_MAIL", "from": "secret", "service": "mail_account",
         "key": "1.imap_password", "sensitive": True},
        {"var": "OPS_OTHER", "from": "secret", "service": "plugin_other", "key": "password"},
    ]}))

    listed = await cards(signed_client)
    assert "mail_account" not in listed
    assert "plugin_other" in listed

    response = await put(signed_client, "mail_account", "1.imap_password")
    assert response.status_code == 404
    assert store.get_secret(config.db_path, "alice", "mail_account", "1.imap_password") is None
    response = await signed_client.delete(f"{SECRETS}/mail_account/1.imap_password", headers=ORIGIN)
    assert response.status_code == 404

    assert (await put(signed_client, "plugin_other", "password")).status_code == 200
