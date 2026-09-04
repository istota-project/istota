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
        import istota.web_app as mod
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
        import istota.web_app as mod
        mod._config.brain = _brain_config(kind="claude_code")
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert resp.json()["selectable_brains"] == []

    async def test_a_name_that_cannot_be_built_is_dropped(self, chat_client):
        import istota.web_app as mod
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
        ` tmux_claude ` entirely — a kind the operator configured and the PATCH
        accepts, missing from the only control that can set it."""
        import istota.web_app as mod
        mod._config.brain = _brain_config(
            kind="claude_code",
            room_selectable=["native", "native", " tmux_claude "],
        )
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert [b["kind"] for b in resp.json()["selectable_brains"]] == [
            "native", "tmux_claude",
        ]

    async def test_the_catalogue_offers_exactly_what_the_patch_accepts(
        self, chat_client,
    ):
        """The drift guard, and the reason the list is `room_selectable_kinds`
        rather than the raw `[brain] room_selectable`: the room PATCH validates
        against that function, so a catalogue built from anything else offers a
        kind the save then refuses with a 400."""
        from istota.brain import room_selectable_kinds
        import istota.web_app as mod
        mod._config.brain = _brain_config(
            kind="claude_code",
            room_selectable=["native", "tmux_claude", "no-such-brain"],
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
        import istota.web_app as mod
        mod._config.brain = _brain_config(
            kind="claude_code", room_selectable=["native"],
        )
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        body = resp.json()
        # `tmux_claude` is on neither the menu nor the deployment's own kind,
        # and is exactly the shape a dropped-from-the-allowlist pin takes.
        assert body["brain_namespaces"]["tmux_claude"] == "anthropic"
        assert body["brain_namespaces"]["native"] == "openai_compat"
        assert [b["kind"] for b in body["selectable_brains"]] == ["native"]
        assert body["inherited_brain"]["kind"] == "claude_code"
        assert body["inherited_brain"]["model_namespace"] == "anthropic"

    async def test_the_inherited_brain_follows_the_lane_rule(self, chat_client):
        """`resolve_brain_kind("web", …)`, not the bare `[brain] kind`: a
        `source_type_overrides` entry for the web lane is what an unpinned room
        there actually runs, and it is what the clearing rule compares against."""
        import istota.web_app as mod
        mod._config.brain = _brain_config(
            kind="claude_code",
            source_type_overrides={"web": "native"},
            room_selectable=["native", "claude_code"],
        )
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert resp.json()["inherited_brain"]["kind"] == "native"

    async def test_a_non_admin_gets_none_of_the_three(self, chat_client):
        import istota.web_app as mod
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
        a name it admits can still fail to build (a `tmux_claude` on a host
        with no tmux). One bad kind must not empty the picker."""
        import istota.web_app as mod
        mod._config.brain = _brain_config(
            kind="claude_code", room_selectable=["native", "tmux_claude"],
        )
        real = mod.make_brain

        def _sometimes(cfg):
            if getattr(cfg, "kind", None) == "tmux_claude":
                raise RuntimeError("no tmux here")
            return real(cfg)

        monkeypatch.setattr(mod, "make_brain", _sometimes)
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        assert [b["kind"] for b in resp.json()["selectable_brains"]] == ["native"]

    async def test_a_non_admin_is_offered_none(self, chat_client):
        import istota.web_app as mod
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
        import istota.web_app as mod
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
        import istota.web_app as mod
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
        assert "claude-opus-5" not in targets

    async def test_the_same_room_unpinned_gets_the_deployments(self, chat_client):
        """The control. Same request, same deployment; only the room's brain
        differs, so a catalogue that simply stopped resolving fails here."""
        import istota.web_app as mod
        mod._config.brain = _brain_config(
            kind="claude_code", room_selectable=["native"],
        )
        cookies = await _login(chat_client, "alice")
        room = await _make_room(chat_client, cookies)
        resp = await chat_client.get(
            f"/istota/api/chat/commands?room_id={room['id']}", cookies=cookies,
        )
        targets = {a["target"] for a in resp.json()["model_aliases"]}
        assert "claude-opus-5" in targets

    async def test_no_room_id_is_the_deployment_default(self, chat_client):
        """The composer's own autocomplete asks without a room, and that is
        the answer every caller got before rooms could pin a brain."""
        import istota.web_app as mod
        mod._config.brain = _brain_config(
            kind="claude_code", room_selectable=["native"],
        )
        cookies = await _login(chat_client, "alice")
        room = await _make_room(chat_client, cookies)
        with db.get_db(mod._config.db_path) as conn:
            db.set_room_brain(conn, room["token"], "native")
        resp = await chat_client.get("/istota/api/chat/commands", cookies=cookies)
        targets = {a["target"] for a in resp.json()["model_aliases"]}
        assert "claude-opus-5" in targets

    async def test_an_unowned_room_falls_back_rather_than_404ing(
        self, chat_client,
    ):
        """The command list is this endpoint's primary product, so a room id
        that resolves to nothing must not take it down with a 404."""
        import istota.web_app as mod
        mod._config.brain = _brain_config(kind="claude_code")
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/commands?room_id=999999", cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.json()["commands"]
        assert resp.json()["model_aliases"]
