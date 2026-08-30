"""Tests for GET /chat/commands (command autocomplete data source)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db
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


async def _login(client, username):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


@pytest.fixture
async def chat_client(tmp_path):
    config = _make_config(tmp_path)
    app = _patch_app(config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


@_needs_web_deps
class TestChatCommandsApi:
    async def test_requires_auth(self, chat_client):
        resp = await chat_client.get("/istota/api/chat/commands")
        assert resp.status_code == 401

    async def test_lists_registered_commands(self, chat_client):
        from istota import commands
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert resp.status_code == 200
        body = resp.json()
        assert "commands" in body
        names = {c["name"] for c in body["commands"]}
        # Every registered command is present.
        assert names == set(commands.COMMANDS)
        # Representative commands + their help text.
        by_name = {c["name"]: c["help"] for c in body["commands"]}
        assert by_name["help"] == commands.COMMANDS["help"][1]
        assert "more" in by_name
        assert "stop" in by_name
        # Sorted alphabetically.
        assert [c["name"] for c in body["commands"]] == sorted(names)

    async def test_includes_model_aliases(self, chat_client):
        from istota.brain import make_brain
        import istota.web_app as mod
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert resp.status_code == 200
        aliases = resp.json()["model_aliases"]
        assert isinstance(aliases, list) and aliases
        expected = {
            a for a, _m, _e in make_brain(mod._config.brain).list_aliases()
        }
        assert {a["alias"] for a in aliases} == expected
        # Each carries alias/target/effort keys.
        first = aliases[0]
        assert set(first) == {"alias", "target", "effort"}

    async def test_includes_command_aliases(self, chat_client):
        """The hidden alias table is published, so a client can tell that
        `!inject` is a command without learning the table itself (ISSUE-350).
        """
        from istota import commands
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert resp.status_code == 200
        aliases = resp.json()["command_aliases"]
        assert {a["alias"] for a in aliases} == set(commands._COMMAND_ALIASES)
        by_alias = {a["alias"]: a["target"] for a in aliases}
        assert by_alias["inject"] == "steer"
        assert by_alias["yes"] == "confirm"
        # Every target is a real command, so a client that resolves through
        # this table lands somewhere `dispatch` will also land.
        assert set(by_alias.values()) <= set(commands.COMMANDS)
        assert all(set(a) == {"alias", "target"} for a in aliases)
        assert [a["alias"] for a in aliases] == sorted(by_alias)

    async def test_aliases_stay_out_of_the_command_list(self, chat_client):
        """`commands` feeds autocomplete and `!help`, which are exactly what
        the alias table is meant to stay out of. Publishing it must not leak.
        """
        from istota import commands
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        names = {c["name"] for c in resp.json()["commands"]}
        assert names == set(commands.COMMANDS)
        assert names.isdisjoint(set(commands._COMMAND_ALIASES))

    async def test_degrades_when_aliases_fail(self, chat_client, monkeypatch):
        import istota.web_app as mod
        cookies = await _login(chat_client, "alice")

        class _BadBrain:
            def list_aliases(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(mod, "make_brain", lambda _cfg: _BadBrain())
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert resp.status_code == 200
        body = resp.json()
        # Commands still served; aliases degrade to empty.
        assert body["commands"]
        assert body["model_aliases"] == []
