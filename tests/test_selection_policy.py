"""Who may pick which model, and what a room or job stores (model-catalogue Stage 7).

`[models] enabled` bounds a non-admin's selection; an admin picks from the
whole catalogue (D9). Aliases are open to both. The bound is on setting a
selection, not on running one, so an admin's pin outside the set runs for
every member. A room stores the alias or the ref it was given and the task
records the ref it resolves to at creation (D6), except a portable alias,
which it keeps. Native runs on Anthropic here and lists OpenRouter in
`[brain.native] providers`, so OpenRouter's models are offered and routed.

Each surface is driven as a pair, admin and non-admin, so a refusal cannot
pass against a writer that refuses everything. The integration test at the
end goes through the web PATCH, `record_inbound` and `execute_task` with a
stub provider.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from istota import db
from istota.brain._resolve import (
    Resolution,
    ResolveError,
    enabled_patterns,
    resolve_for_person,
    selection_for_task,
    stored_selection,
)
from istota.commands import CommandContext, cmd_models, cmd_room, resolve_model_prefix
from istota.config import (
    Config,
    ModelsConfig,
    SecurityConfig,
    UserConfig,
    config_from_document,
    load_config,
)
from istota.llm.catalog import ModelInfo, set_discovered
from istota.transport.ingest import record_inbound

OUTSIDE = "openrouter/moonshotai/kimi-k2.6"


def _brain_and_providers():
    """`[brain]` (native on Anthropic reaching OpenRouter, room-selectable)
    and `[providers]` with keys for both built-ins."""
    doc = {
        "brain": {
            "kind": "native",
            "room_selectable": ["native"],
            "native": {
                "provider": "anthropic", "providers": ["openrouter"],
                "model": "claude-sonnet-5-5",
            },
        },
        "providers": {
            "anthropic": {"api_key": "anthropic-test-key"},
            "openrouter": {"api_key": "openrouter-test-key"},
        },
    }
    return config_from_document(doc)


def _config(tmp_path, *, admins=(), users=("alice", "bob"), enabled=None) -> Config:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    skills_dir = tmp_path / "config" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    source = _brain_and_providers()
    config = Config(
        db_path=db_path,
        skills_dir=skills_dir,
        bundled_skills_dir=tmp_path / "_empty_bundled",
        temp_dir=tmp_path / "temp",
        workspace_path=tmp_path / "workspace",
        security=SecurityConfig(skill_proxy_enabled=False),
        users={u: UserConfig() for u in users},
    )
    config.brain = source.brain
    config.providers = source.providers
    config.models = ModelsConfig(enabled=enabled)
    config.admin_users = set(admins)
    (tmp_path / "temp").mkdir(exist_ok=True)
    return config


@pytest.fixture(autouse=True)
def _openrouter_list():
    set_discovered({
        "openrouter": [
            ModelInfo(
                id="moonshotai/kimi-k2.6", context_window=200000, max_output_tokens=8000,
                provider="openrouter", reasoning=True, name="Kimi K2.6",
            ),
            ModelInfo(
                id="z-ai/glm-5", context_window=200000, max_output_tokens=8000,
                provider="openrouter",
            ),
        ],
    })
    yield
    set_discovered(None)


class TestTheEnabledSet:
    def test_unset_is_anthropic_plus_native_s_model(self, tmp_path):
        config = _config(tmp_path)
        assert config.models.enabled is None
        assert enabled_patterns(config) == ["anthropic/*", "anthropic/claude-sonnet-5-5"]

    def test_a_written_list_replaces_the_default_and_empty_means_aliases_only(self, tmp_path):
        assert enabled_patterns(_config(tmp_path, enabled=["openrouter/z-ai/*"])) == [
            "openrouter/z-ai/*",
        ]
        config = _config(tmp_path / "b", enabled=[])
        assert enabled_patterns(config) == []
        refused = resolve_for_person("anthropic/claude-opus-5-5", config, kind="native", is_admin=False)
        assert isinstance(refused, ResolveError) and refused.code == "model_not_enabled"
        assert isinstance(
            resolve_for_person("smart", config, kind="claude_code", is_admin=False), Resolution,
        )

    def test_the_loader_reads_it_and_drops_what_is_not_a_pattern(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[models]\nenabled = ["anthropic/*", 3, " "]\n')
        assert load_config(path, apply_db_overlays=False).models.enabled == ["anthropic/*"]
        path.write_text('[models]\nenabled = "anthropic/*"\n')
        assert load_config(path, apply_db_overlays=False).models.enabled is None


class TestAPersonsSelection:
    def test_an_admin_picks_outside_the_set_and_a_non_admin_does_not(self, tmp_path):
        config = _config(tmp_path)
        admin = resolve_for_person(OUTSIDE, config, kind="native", is_admin=True)
        assert isinstance(admin, Resolution) and admin.ref == OUTSIDE
        member = resolve_for_person(OUTSIDE, config, kind="native", is_admin=False)
        assert isinstance(member, ResolveError) and member.code == "model_not_enabled"

    def test_what_is_stored_is_the_alias_or_the_ref(self, tmp_path):
        config = _config(tmp_path)
        alias = resolve_for_person("smart", config, kind="claude_code", is_admin=False)
        assert stored_selection(alias) == "smart"
        bare = resolve_for_person("moonshotai/kimi-k2.6", config, kind="native", is_admin=True)
        assert stored_selection(bare) == OUTSIDE

    def test_a_pattern_only_where_asked(self, tmp_path):
        config = _config(tmp_path)
        assert isinstance(
            resolve_for_person("kimi", config, kind="native", is_admin=True), ResolveError,
        )
        found = resolve_for_person("kimi", config, kind="native", is_admin=True, allow_patterns=True)
        assert isinstance(found, Resolution) and found.ref == OUTSIDE

    def test_the_task_records_the_ref_an_alias_names_now(self, tmp_path):
        from istota.brain._roles import set_alias_overrides

        config = _config(tmp_path)
        model, _effort = selection_for_task(config, "opus", kind="claude_code")
        assert model.startswith("anthropic/claude-opus-")
        set_alias_overrides({"deep": {"anthropic": "sonnet:high"}})
        try:
            model, effort = selection_for_task(config, "deep", kind="claude_code")
        finally:
            set_alias_overrides({})
        assert model.startswith("anthropic/claude-sonnet-")
        assert effort == "high"

    def test_a_portable_alias_is_kept_by_name(self, tmp_path):
        # So a fallback to a brain on another provider re-resolves it there.
        config = _config(tmp_path)
        assert selection_for_task(config, "smart", kind="claude_code") == ("smart", None)


def _ctx(config, conn, args, user_id, token="room1"):
    return CommandContext(
        config=config, conn=conn, user_id=user_id,
        conversation_token=token, args=args, surface="web",
    )


def _room(conn, owner="alice", token="room1"):
    db.register_room(conn, token, owner, origin="web")
    db.add_room_binding(conn, token, "web", token)
    db.set_room_brain(conn, token, "native")


@pytest.mark.asyncio
class TestRoomModel:
    async def test_an_admin_sets_a_model_outside_the_set(self, tmp_path):
        config = _config(tmp_path, admins=("alice",))
        with db.get_db(config.db_path) as conn:
            _room(conn)
            await cmd_room(_ctx(config, conn, f"model {OUTSIDE}", "alice"))
            assert db.get_room(conn, "room1").model == OUTSIDE

    async def test_a_non_admin_is_refused_and_told_why(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _room(conn)
            out = await cmd_room(_ctx(config, conn, f"model {OUTSIDE}", "alice"))
            assert db.get_room(conn, "room1").model is None
        assert "not one of the models you can pick" in out

    async def test_a_pattern_is_stored_as_the_ref_it_matched(self, tmp_path):
        config = _config(tmp_path, admins=("alice",))
        with db.get_db(config.db_path) as conn:
            _room(conn)
            await cmd_room(_ctx(config, conn, "model kimi:high", "alice"))
            room = db.get_room(conn, "room1")
        assert (room.model, room.effort) == (OUTSIDE, "high")

    async def test_an_admin_s_pin_runs_for_someone_outside_the_set(self, tmp_path):
        config = _config(tmp_path, admins=("alice",))
        with db.get_db(config.db_path) as conn:
            _room(conn)
            await cmd_room(_ctx(config, conn, f"model {OUTSIDE}", "alice"))
            # The same room, now spoken in by someone who could not set it.
            config.admin_users = set()
            assert not config.is_admin("alice")
            inbound = record_inbound(
                conn, config, surface="web", surface_ref="room1",
                user_id="alice", text="hello", source_type="web",
            )
            task = db.get_task(conn, inbound.task_id)
        assert task.model == OUTSIDE


@pytest.mark.asyncio
class TestTheModelPrefix:
    def _brain(self, config):
        from istota.brain import make_brain

        return make_brain(config.brain)

    async def test_an_admin_may_name_any_model_and_a_member_may_not(self, tmp_path):
        config = _config(tmp_path, admins=("alice",))
        brain = self._brain(config)
        admin = resolve_model_prefix(
            f"!model {OUTSIDE} hello", brain, config=config, user_id="alice",
        )
        assert (admin.model, admin.content) == (OUTSIDE, "hello")
        member = resolve_model_prefix(
            f"!model {OUTSIDE} hello", brain, config=config, user_id="bob",
        )
        assert member.model is None
        assert "not one of the models you can pick" in member.usage

    async def test_an_alias_is_open_to_everyone(self, tmp_path):
        config = _config(tmp_path)
        out = resolve_model_prefix(
            "!model smart:low hello", self._brain(config), config=config, user_id="bob",
        )
        assert (out.model, out.effort) == ("smart", "low")


@pytest.mark.asyncio
class TestModelsListing:
    async def test_a_member_sees_the_enabled_set_only(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _room(conn)
            out = await cmd_models(_ctx(config, conn, "", "bob"))
        assert "anthropic/claude-sonnet-5-5" in out
        assert OUTSIDE not in out
        assert "!models all" not in out

    async def test_an_admin_sees_the_rest_counted_and_can_list_it(self, tmp_path):
        config = _config(tmp_path, admins=("alice",))
        with db.get_db(config.db_path) as conn:
            _room(conn)
            out = await cmd_models(_ctx(config, conn, "", "alice"))
            listed = await cmd_models(_ctx(config, conn, "all openrouter", "alice"))
            refused = await cmd_models(_ctx(config, conn, "all openrouter", "bob"))
        assert "**openrouter**" in out
        assert "2 more: `!models all openrouter`" in out
        assert OUTSIDE in listed
        assert "Only an admin" in refused


class TestCronJobs:
    def _parse(self, config, user_id, model):
        from istota.cron_loader import _file_owned_values, _parse_jobs

        errors: list[str] = []
        jobs, _skipped, _refused = _parse_jobs(
            {"jobs": [{"name": "digest", "cron": "0 8 * * *", "prompt": "hi", "model": model}]},
            config, user_id, errors,
        )
        job = jobs[0]
        return job, _file_owned_values(job, None, None, None, None)["model"], errors

    def test_an_admin_s_job_keeps_a_model_outside_the_set(self, tmp_path):
        config = _config(tmp_path, admins=("alice",))
        job, stored, errors = self._parse(config, "alice", OUTSIDE)
        assert (job.model, stored, errors) == (OUTSIDE, OUTSIDE, [])

    def test_a_non_admin_s_job_runs_on_the_default_and_keeps_its_line(self, tmp_path):
        config = _config(tmp_path)
        job, stored, errors = self._parse(config, "bob", OUTSIDE)
        # The file still says it, so a rewrite of CRON.md never deletes it.
        assert job.model == OUTSIDE
        assert stored is None
        assert any("not one you can pick" in e for e in errors)

    def test_a_non_admin_s_alias_is_kept(self, tmp_path):
        config = _config(tmp_path)
        _job, stored, errors = self._parse(config, "bob", "smart")
        assert (stored, errors) == ("smart", [])

    def test_a_pattern_is_not_a_selection_in_a_file(self, tmp_path):
        config = _config(tmp_path)
        _job, stored, _errors = self._parse(config, "bob", "kimi")
        assert stored is None


class TestDoctor:
    def test_an_enabled_pattern_matching_nothing_warns(self, tmp_path):
        from istota.doctor import WARN, check_models_catalogue

        config = _config(tmp_path, enabled=["anthropic/*", "nobody/*"])
        result = check_models_catalogue(config, probe=False)
        assert result.status == WARN
        assert "'nobody/*'" in result.detail

    def test_the_default_set_is_not_warned_about(self, tmp_path):
        from istota.doctor import check_models_catalogue

        result = check_models_catalogue(_config(tmp_path), probe=False)
        assert "match no model" not in result.detail


# --- the integration: PATCH, record_inbound, execute_task --------------------


class _ToolServer:
    failure = None

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_a_web_room_pick_reaches_the_provider_it_names(tmp_path):
    """An admin pins an OpenRouter model in a native room from the web
    settings (native on Anthropic, OpenRouter listed in `[brain.native]
    providers`); the next turn's task records the ref and the request goes to
    OpenRouter with its key, its own id and the effort the model supports.

    `execute_task(dry_run=True)` returns once the prompt is rendered and builds
    no request, so this runs it for real, with the provider and the tool server
    stubbed.
    """
    pytest.importorskip("fastapi")
    from httpx import ASGITransport, AsyncClient

    from istota.brain.native import NativeBrain
    from istota.executor import execute_task
    from istota.llm.providers import InferenceKey
    from istota.llm.types import AssistantMessage, TextContent, Usage

    from .native._mock_provider import MockProvider
    from .test_web_chat import _login, _make_config, _patch_app

    web_config = _make_config(tmp_path)
    config = _config(tmp_path, admins=("alice",))
    config.workspace_path = web_config.workspace_path
    config.site = web_config.site
    config.web = web_config.web
    config.bot_name = web_config.bot_name

    app = _patch_app(config)
    # The PATCH takes up the discovery caches too; with none on disk that
    # would swap in an empty layer over the list the fixture installed.
    no_cache = patch("istota.llm.discovery.install_from_cache_dir", return_value=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://example.com") as client:
        cookies = await _login(client, "alice")
        room = (await client.post(
            "/istota/api/chat/rooms", json={"name": "r"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )).json()
        with no_cache:
            resp = await client.patch(
                f"/istota/api/chat/rooms/{room['id']}",
                json={"brain": "native", "model": OUTSIDE, "effort": "xhigh"},
                cookies=cookies, headers={"origin": "https://example.com"},
            )
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == OUTSIDE

    built_specs = []
    providers = []

    def _make_provider(spec):
        built_specs.append(spec)
        provider = MockProvider([AssistantMessage(
            content=[TextContent(text="done")], stop_reason="stop", usage=Usage(),
        )])
        providers.append(provider)
        return provider

    async def _start(self, req, abort):
        return _ToolServer()

    (tmp_path / "temp" / "alice").mkdir(parents=True, exist_ok=True)
    with db.get_db(config.db_path) as conn:
        inbound = record_inbound(
            conn, config, surface="web", surface_ref=room["token"],
            user_id="alice", text="hello", source_type="web",
        )
        task = db.get_task(conn, inbound.task_id)
    assert (task.model, task.effort, task.brain) == (OUTSIDE, "xhigh", "native")

    def _execute():
        with db.get_db(config.db_path) as conn:
            return execute_task(task, config, [], conn=conn)

    # No discovery cache on disk: keep the list installed above rather than
    # let the brain swap in an empty discovered layer.
    with patch("istota.brain.native.make_provider", _make_provider), \
            patch.object(NativeBrain, "_start_tool_server", _start), \
            patch("istota.llm.discovery.install_from_cache_dir", return_value=False):
        # On a thread: the native brain runs its own event loop.
        ok, result, _actions, trace = await asyncio.to_thread(_execute)

    assert ok, result
    routed = built_specs[-1]
    assert routed.id == "openrouter"
    assert routed.inference_key == InferenceKey("openrouter-test-key")
    call = providers[-1].calls[0]
    assert call["model"] == "moonshotai/kimi-k2.6"
    # The model reasons but names no `xhigh`, so the request carries `high`.
    assert call["reasoning_effort"] == "high"
    notes = [e for e in json.loads(trace) if e.get("type") == "effort"]
    assert notes and notes[0]["requested"] == "xhigh" and notes[0]["used"] == "high"
