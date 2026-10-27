"""Tests for GET /chat/commands (command autocomplete data source)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.brain.claude_code import OPUS

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
        workspace_path=tmp_path / "mount",
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
    import istota.webui.app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    return mod.app


async def _login(client, username):
    import istota.webui.app as mod
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
        import istota.webui.app as mod
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
        assert set(first) == {"alias", "target", "effort", "efforts"}

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
        import istota.webui.app as mod
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


def _brain_config(**kwargs):
    from istota.config import BrainConfig

    return BrainConfig(**kwargs)


async def _make_room(client, cookies, name="r"):
    return (await client.post(
        "/istota/api/chat/rooms", json={"name": name}, cookies=cookies,
        headers={"origin": "https://example.com"},
    )).json()


@_needs_web_deps
class TestSelectableBrains:
    """The picker's catalogue. Published to admins only, because writing the
    pin is admin-gated (D8) and a list of kinds a non-admin cannot select is
    not information they need — which is also what lets `RoomSettings.svelte`
    decide by emptiness alone rather than combining two conditions."""

    async def test_it_lists_the_operators_kinds_with_labels_and_namespaces(
        self, chat_client,
    ):
        import istota.webui.app as mod
        mod._config.brain = _brain_config(
            kind="claude_code", room_selectable=["native", "claude_code"],
        )
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        brains = resp.json()["selectable_brains"]
        assert [b["kind"] for b in brains] == ["claude_code", "native"]
        assert all(set(b) == {"kind", "label", "model_namespace"} for b in brains)
        by_kind = {b["kind"]: b for b in brains}
        assert by_kind["claude_code"]["label"] == "Claude Code"
        # The modal compares these two to decide whether a pending change
        # crosses a namespace, so they have to be the brains' own values.
        assert by_kind["claude_code"]["model_namespace"] == "anthropic"
        assert by_kind["native"]["model_namespace"] == "openai_compat"

    async def test_the_shipped_default_offers_nothing(self, chat_client):
        import istota.webui.app as mod
        mod._config.brain = _brain_config(kind="claude_code")
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert resp.json()["selectable_brains"] == []

    async def test_a_name_that_cannot_be_built_is_dropped(self, chat_client):
        import istota.webui.app as mod
        mod._config.brain = _brain_config(
            kind="claude_code", room_selectable=["native", "no-such-brain"],
        )
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert [b["kind"] for b in resp.json()["selectable_brains"]] == ["native"]

    async def test_it_normalizes_the_operators_list_the_way_the_patch_does(
        self, chat_client,
    ):
        """`room_selectable_kinds` strips each entry and dedupes; building the
        list off the raw setting instead offers `native` twice and drops
        ` claude_code ` entirely — a kind the operator configured and the PATCH
        accepts, missing from the only control that can set it."""
        import istota.webui.app as mod
        mod._config.brain = _brain_config(
            kind="claude_code",
            room_selectable=["native", "native", " claude_code "],
        )
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert [b["kind"] for b in resp.json()["selectable_brains"]] == [
            "claude_code", "native",
        ]

    async def test_the_catalogue_offers_exactly_what_the_patch_accepts(
        self, chat_client,
    ):
        """The drift guard, and the reason the list is `room_selectable_kinds`
        rather than the raw `[brain] room_selectable`: the room PATCH validates
        against that function, so a catalogue built from anything else offers a
        kind the save then refuses with a 400."""
        from istota.brain import room_selectable_kinds
        import istota.webui.app as mod
        mod._config.brain = _brain_config(
            kind="claude_code",
            room_selectable=["native", "claude_code", "no-such-brain"],
        )
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        offered = {b["kind"] for b in resp.json()["selectable_brains"]}
        assert offered == room_selectable_kinds(mod._config.brain)

    async def test_it_names_every_buildable_kind_and_the_inherited_one(
        self, chat_client,
    ):
        """The modal decides whether a pending change crosses a namespace, and
        neither brain it compares is necessarily on the menu: the outgoing one
        is the inherited brain when the room pins none, or a kind the operator
        has since dropped from the allowlist. Answering "unknown" for either
        makes the client over-lock its model select and drop an edit the server
        would have kept."""
        import istota.webui.app as mod
        mod._config.brain = _brain_config(
            kind="native", room_selectable=["native"],
        )
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        body = resp.json()
        # `claude_code` is on neither the menu nor the deployment's own kind,
        # and is exactly the shape a dropped-from-the-allowlist pin takes.
        assert body["brain_namespaces"] == {
            "claude_code": "anthropic", "native": "openai_compat",
        }
        assert [b["kind"] for b in body["selectable_brains"]] == ["native"]
        assert body["inherited_brain"]["kind"] == "native"
        assert body["inherited_brain"]["model_namespace"] == "openai_compat"

    async def test_the_inherited_brain_follows_the_lane_rule(self, chat_client):
        """`resolve_brain_kind("web", …)`, not the bare `[brain] kind`: a
        `source_type_overrides` entry for the web lane is what an unpinned room
        there actually runs, and it is what the clearing rule compares against."""
        import istota.webui.app as mod
        mod._config.brain = _brain_config(
            kind="claude_code",
            source_type_overrides={"web": "native"},
            room_selectable=["native", "claude_code"],
        )
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert resp.json()["inherited_brain"]["kind"] == "native"

    async def test_a_non_admin_gets_none_of_the_three(self, chat_client):
        import istota.webui.app as mod
        mod._config.brain = _brain_config(
            kind="claude_code", room_selectable=["native"],
        )
        cookies = await _login(chat_client, "alice")
        try:
            mod._config.admin_users = {"carol"}
            body = (await chat_client.get(
                "/istota/api/chat/commands", cookies=cookies,
            )).json()
            assert body["selectable_brains"] == []
            assert body["brain_namespaces"] == {}
            assert body["inherited_brain"] is None
        finally:
            mod._config.admin_users = set()

    async def test_a_listed_kind_that_fails_to_construct_costs_only_itself(
        self, chat_client, monkeypatch,
    ):
        """The per-kind guard, which the allowlist filter cannot stand in for:
        a name it admits can still fail to build (a broken nested block). One
        bad kind must not empty the picker."""
        import istota.webui.app as mod
        mod._config.brain = _brain_config(
            kind="native", room_selectable=["native", "claude_code"],
        )
        real = mod.make_brain

        def _sometimes(cfg):
            if getattr(cfg, "kind", None) == "claude_code":
                raise RuntimeError("cannot build here")
            return real(cfg)

        monkeypatch.setattr(mod, "make_brain", _sometimes)
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert [b["kind"] for b in resp.json()["selectable_brains"]] == ["native"]

    async def test_a_non_admin_is_offered_none(self, chat_client):
        import istota.webui.app as mod
        mod._config.brain = _brain_config(
            kind="claude_code", room_selectable=["native"],
        )
        cookies = await _login(chat_client, "alice")
        try:
            # An `admin_users` set that does not name her: `Config.is_admin`
            # reads an *empty* set as "everyone", so a non-admin needs a
            # populated one.
            mod._config.admin_users = {"carol"}
            resp = await chat_client.get(
                "/istota/api/chat/commands", cookies=cookies,
            )
            assert resp.json()["selectable_brains"] == []
            # The control: same deployment, same request, same user, admin.
            mod._config.admin_users = {"alice"}
            resp = await chat_client.get(
                "/istota/api/chat/commands", cookies=cookies,
            )
            assert [b["kind"] for b in resp.json()["selectable_brains"]] == ["native"]
        finally:
            mod._config.admin_users = set()

    async def test_the_command_list_survives_a_broken_brain(
        self, chat_client, monkeypatch,
    ):
        """Same contract `model_aliases` has: the catalogue's primary product
        is the command list, and neither optional half may take it down.

        Since ISSUE-417 the two halves fail independently, which is the point
        of separating them. `selectable_brains` asks whether a kind can be
        *built* and empties when nothing can. `brain_namespaces` asks what
        namespace a kind reads in — a class attribute, true whether or not this
        host can construct the brain — so it survives, and `inherited_brain`
        with it. That is the better answer rather than a tolerated one: the
        modal compares namespaces to predict the server's own clearing rule,
        and answering "unknown" there drops a model edit the server would have
        kept.
        """
        import istota.webui.app as mod
        mod._config.brain = _brain_config(
            kind="claude_code", room_selectable=["native"],
        )
        cookies = await _login(chat_client, "alice")

        def _boom(_cfg):
            raise RuntimeError("boom")

        monkeypatch.setattr(mod, "make_brain", _boom)
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert resp.status_code == 200
        body = resp.json()
        assert body["commands"]
        assert body["selectable_brains"] == []
        assert body["brain_namespaces"]["claude_code"] == "anthropic"
        assert body["inherited_brain"]["kind"] == "claude_code"


@_needs_web_deps
class TestModelAliasesFollowTheRoom:
    """D5 Rule 2's fifth row on the web side: a surface that *offers* a model
    name lists the aliases of the brain that would have to run it. The
    catalogue has no room of its own, so `room_id` is what scopes it."""

    async def test_a_pinned_room_gets_its_own_namespace(self, chat_client):
        import istota.webui.app as mod
        from istota.config import NativeBrainConfig
        mod._config.brain = _brain_config(
            kind="claude_code", room_selectable=["native"],
            native=NativeBrainConfig(model="endpoint/m"),
        )
        cookies = await _login(chat_client, "alice")
        room = await _make_room(chat_client, cookies)
        with db.get_db(mod._config.db_path) as conn:
            db.set_room_brain(conn, room["token"], "native")
        resp = await chat_client.get(
            f"/istota/api/chat/commands?room_id={room['id']}", cookies=cookies,
        )
        targets = {a["target"] for a in resp.json()["model_aliases"]}
        # Both halves: the endpoint degrades `model_aliases` to `[]` on any
        # brain failure, so an absence assertion alone is satisfied by the
        # aliases having gone missing entirely — indistinguishable from the
        # scoping having worked.
        assert "endpoint/m" in targets
        assert OPUS not in targets

    async def test_the_same_room_unpinned_gets_the_deployments(self, chat_client):
        """The control. Same request, same deployment; only the room's brain
        differs, so a catalogue that simply stopped resolving fails here."""
        import istota.webui.app as mod
        mod._config.brain = _brain_config(
            kind="claude_code", room_selectable=["native"],
        )
        cookies = await _login(chat_client, "alice")
        room = await _make_room(chat_client, cookies)
        resp = await chat_client.get(
            f"/istota/api/chat/commands?room_id={room['id']}", cookies=cookies,
        )
        targets = {a["target"] for a in resp.json()["model_aliases"]}
        assert OPUS in targets

    async def test_no_room_id_is_the_deployment_default(self, chat_client):
        """The composer's own autocomplete asks without a room, and that is
        the answer every caller got before rooms could pin a brain."""
        import istota.webui.app as mod
        mod._config.brain = _brain_config(
            kind="claude_code", room_selectable=["native"],
        )
        cookies = await _login(chat_client, "alice")
        room = await _make_room(chat_client, cookies)
        with db.get_db(mod._config.db_path) as conn:
            db.set_room_brain(conn, room["token"], "native")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        targets = {a["target"] for a in resp.json()["model_aliases"]}
        assert OPUS in targets

    async def test_the_effective_default_model_is_published(self, chat_client):
        """#548: the room picker labelled the unpinned choice "Default model"
        with no id, so nothing on it showed the default had moved past what
        the aliases pointed at. Resolved the way the admin Models card does."""
        import istota.webui.app as mod
        from istota.config import ClaudeCodeBrainConfig

        mod._config.brain = _brain_config(
            kind="claude_code", claude_code=ClaudeCodeBrainConfig(model="smart"),
        )
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert resp.json()["default_model"] == OPUS

    async def test_no_configured_default_publishes_none(self, chat_client):
        import istota.webui.app as mod
        mod._config.brain = _brain_config(kind="claude_code")
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert resp.json()["default_model"] is None

    async def test_an_unowned_room_falls_back_rather_than_404ing(
        self, chat_client,
    ):
        """The command list is this endpoint's primary product, so a room id
        that resolves to nothing must not take it down with a 404."""
        import istota.webui.app as mod
        mod._config.brain = _brain_config(kind="claude_code")
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/commands?room_id=999999", cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.json()["commands"]
        assert resp.json()["model_aliases"]


def _provider_config(tmp_path, *, admins):
    """A web config whose native brain runs on Anthropic and lists OpenRouter
    in `[brain.native] providers`, with keys for both built-in providers, so
    OpenRouter models are runnable on native."""
    from istota.config import config_from_document

    config = _make_config(tmp_path)
    config.users = {"alice": UserConfig(), "bob": UserConfig()}
    source = config_from_document({
        "brain": {
            "kind": "native",
            "room_selectable": ["native", "claude_code"],
            "native": {
                "provider": "anthropic", "providers": ["openrouter"],
                "model": "claude-sonnet-5-5",
            },
        },
        "providers": {
            "anthropic": {"api_key": "anthropic-test-key"},
            "openrouter": {"api_key": "openrouter-test-key"},
        },
    })
    config.brain = source.brain
    config.providers = source.providers
    config.admin_users = set(admins)
    return config


def _write_openrouter_cache(config, models):
    """A discovery cache file as the daemon's gate writes it."""
    import json
    import time
    from dataclasses import asdict

    from istota.llm import discovery

    now = time.time()
    cache_dir = discovery.config_cache_dir(config)
    cache_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "provider": "openrouter", "kind": "openrouter", "fetched_at": now,
        "checked_at": now, "etag": None, "last_error": None, "last_status": 200,
        "models": {m.id: {**asdict(m), "last_seen": now} for m in models},
    }
    discovery.cache_file(cache_dir, "openrouter").write_text(json.dumps(doc))


def _kimi():
    from istota.llm.catalog import ModelInfo

    return ModelInfo(
        id="moonshotai/kimi-k2.6", context_window=200000, max_output_tokens=8000,
        provider="openrouter", reasoning=True, name="Kimi K2.6",
    )


def _client_for(config):
    return AsyncClient(
        transport=ASGITransport(app=_patch_app(config)), base_url="https://example.com",
    )


@_needs_web_deps
class TestTheModelsPayload:
    """`models`: what the room picker offers, scoped to the viewer (D9) and to
    the brain that would run the pick. Read from the daemon's discovery cache,
    which the web process takes up at the request (D11)."""

    def _config(self, tmp_path, *, admins):
        config = _provider_config(tmp_path, admins=admins)
        _write_openrouter_cache(config, [_kimi()])
        return config

    async def test_an_admin_sees_a_discovered_model_outside_the_enabled_set(self, tmp_path):
        async with _client_for(self._config(tmp_path, admins={"alice"})) as client:
            cookies = await _login(client, "alice")
            models = (await client.get(
                "/istota/api/chat/commands", cookies=cookies,
            )).json()["models"]
        by_ref = {m["ref"]: m for m in models}
        kimi = by_ref["openrouter/moonshotai/kimi-k2.6"]
        assert kimi["enabled"] is False
        assert kimi["efforts"] == ["low", "medium", "high"]
        assert kimi["price"] is None  # discovered with no price known
        assert by_ref["anthropic/claude-sonnet-5-5"]["enabled"] is True
        # Grouped by provider, the enabled models first within each group.
        providers = [m["provider"] for m in models]
        assert providers == sorted(providers)
        anthropic = [m["enabled"] for m in models if m["provider"] == "anthropic"]
        assert anthropic == sorted(anthropic, reverse=True)

    async def test_a_member_sees_only_the_enabled_set(self, tmp_path):
        async with _client_for(self._config(tmp_path, admins={"carol"})) as client:
            cookies = await _login(client, "bob")
            models = (await client.get(
                "/istota/api/chat/commands", cookies=cookies,
            )).json()["models"]
        refs = {m["ref"] for m in models}
        assert "anthropic/claude-sonnet-5-5" in refs
        assert "openrouter/moonshotai/kimi-k2.6" not in refs
        assert all(m["enabled"] for m in models)

    async def test_the_list_follows_the_rooms_brain(self, tmp_path):
        """A claude_code room cannot run an OpenRouter model, so it is not offered."""
        config = self._config(tmp_path, admins={"alice"})
        async with _client_for(config) as client:
            cookies = await _login(client, "alice")
            room = await _make_room(client, cookies)
            with db.get_db(config.db_path) as conn:
                db.set_room_brain(conn, room["token"], "claude_code")
            models = (await client.get(
                f"/istota/api/chat/commands?room_id={room['id']}", cookies=cookies,
            )).json()["models"]
        assert models
        assert {m["provider"] for m in models} == {"anthropic"}

    async def test_a_cache_the_daemon_rewrites_is_taken_up_at_the_next_request(self, tmp_path):
        from istota.llm.catalog import ModelInfo

        config = self._config(tmp_path, admins={"alice"})
        async with _client_for(config) as client:
            cookies = await _login(client, "alice")
            first = (await client.get("/istota/api/chat/commands", cookies=cookies)).json()
            assert "openrouter/z-ai/glm-5" not in {m["ref"] for m in first["models"]}
            assert "openrouter/moonshotai/kimi-k2.6" in {m["ref"] for m in first["models"]}
            glm = ModelInfo(
                id="z-ai/glm-5", context_window=200000, max_output_tokens=8000,
                provider="openrouter",
            )
            _write_openrouter_cache(config, [_kimi(), glm])
            second = (await client.get("/istota/api/chat/commands", cookies=cookies)).json()
        assert "openrouter/z-ai/glm-5" in {m["ref"] for m in second["models"]}

    async def test_aliases_carry_their_models_effort_levels(self, tmp_path):
        config = self._config(tmp_path, admins={"alice"})
        async with _client_for(config) as client:
            cookies = await _login(client, "alice")
            room = await _make_room(client, cookies)
            with db.get_db(config.db_path) as conn:
                db.set_room_brain(conn, room["token"], "claude_code")
            aliases = (await client.get(
                f"/istota/api/chat/commands?room_id={room['id']}", cookies=cookies,
            )).json()["model_aliases"]
        opus = next(a for a in aliases if a["alias"] == "opus")
        assert opus["efforts"] and "high" in opus["efforts"]


@_needs_web_deps
class TestTheRoomModelStatus:
    """`room_model`: what the picker makes of a pin the viewer's list may not hold."""

    async def _status(self, tmp_path, *, admins, model, provider=None):
        config = _provider_config(tmp_path, admins=admins)
        _write_openrouter_cache(config, [_kimi()])
        async with _client_for(config) as client:
            cookies = await _login(client, "alice")
            room = await _make_room(client, cookies)
            with db.get_db(config.db_path) as conn:
                db.set_room_brain(conn, room["token"], "native")
                if model is not None:
                    db.set_room_model(conn, room["token"], model)
                db.set_room_provider(conn, room["token"], provider)
            body = (await client.get(
                f"/istota/api/chat/commands?room_id={room['id']}", cookies=cookies,
            )).json()
        return body.get("room_model", "absent")

    async def test_an_admins_pin_outside_a_members_set(self, tmp_path):
        # A room on OpenRouter stores its id and the provider (D22).
        status = await self._status(
            tmp_path, admins={"carol"}, model="moonshotai/kimi-k2.6", provider="openrouter",
        )
        assert status == {
            "value": "moonshotai/kimi-k2.6",
            "status": "set_by_admin",
            "ref": "openrouter/moonshotai/kimi-k2.6",
            "efforts": ["low", "medium", "high"],
        }

    async def test_the_same_pin_is_listed_for_an_admin(self, tmp_path):
        status = await self._status(
            tmp_path, admins={"alice"}, model="moonshotai/kimi-k2.6", provider="openrouter",
        )
        assert status["status"] == "listed"

    async def test_another_provider_s_id_is_not_read_as_this_one_s(self, tmp_path):
        # The room runs on native's default (Anthropic): OpenRouter's id is
        # not a model of it, whatever the string looks like (D22).
        status = await self._status(tmp_path, admins={"alice"}, model="moonshotai/kimi-k2.6")
        assert status["status"] == "unavailable"

    async def test_a_pin_that_no_longer_resolves(self, tmp_path):
        status = await self._status(
            tmp_path, admins={"alice"}, model="gone/model", provider="openrouter",
        )
        assert status["status"] == "unavailable"
        assert status["ref"] is None

    async def test_no_pin_no_status(self, tmp_path):
        assert await self._status(tmp_path, admins={"alice"}, model=None) == "absent"


@_needs_web_deps
class TestAdminProviderRows:
    """The admin Models card's provider rows: read from the caches, no key."""

    def _rows(self, config):
        import istota.webui.app as mod

        mod._config = config
        return {r["id"]: r for r in mod._admin_models_section()["providers"]}

    def test_a_discovering_provider_reports_its_cache(self, tmp_path):
        config = _provider_config(tmp_path, admins={"alice"})
        _write_openrouter_cache(config, [_kimi()])
        rows = self._rows(config)
        openrouter = rows["openrouter"]
        assert openrouter["kind"] == "openrouter"
        assert openrouter["host"] == "openrouter.ai"
        assert openrouter["discovers"] is True
        assert openrouter["discovered_at"]
        assert openrouter["last_error"] is None
        assert openrouter["models"] == 1
        assert openrouter["enabled"] == 0
        anthropic = rows["anthropic"]
        assert anthropic["host"] == "api.anthropic.com"
        assert anthropic["models"] > 0
        assert anthropic["enabled"] == anthropic["models"]
        assert "openrouter-test-key" not in str(rows)

    def test_a_failed_fetch_is_named(self, tmp_path):
        import json

        from istota.llm import discovery

        config = _provider_config(tmp_path, admins={"alice"})
        _write_openrouter_cache(config, [_kimi()])
        path = discovery.cache_file(discovery.config_cache_dir(config), "openrouter")
        doc = json.loads(path.read_text())
        doc.update(last_error="HTTP 503", last_status=503)
        path.write_text(json.dumps(doc))
        assert self._rows(config)["openrouter"]["last_error"] == "HTTP 503"
