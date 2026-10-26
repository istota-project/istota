"""A room's brain sets the providers its models come from.

A room or job on `claude_code` or `tmux` uses the `anthropic` provider only; a
room or job on `native` uses the providers `[brain.native] providers` lists,
its default provider always among them. An `anthropic` key held for the CLI
does not make Anthropic a native provider. Every reading of a model for the
room stays within those providers, with or without a discovery cache: a value
naming another provider is the default provider's id when it lists it, else
refused, and it is never routed or billed elsewhere. A fallback across brains
drops an exact pin the target cannot reach and re-resolves a portable alias.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from istota import db
from istota.brain import make_brain
from istota.brain._resolve import (
    Resolution,
    ResolveError,
    resolve_for_person,
    selection_for_task,
    stored_selection,
)
from istota.config import Config, SecurityConfig, UserConfig, config_from_document
from istota.llm.catalog import ModelInfo, set_discovered
from istota.llm.types import AssistantMessage, TextContent, Usage
from istota.transport.ingest import record_inbound

from .native._mock_provider import MockProvider

ANTHROPIC_SPELLING = "anthropic/claude-opus-5"
KIMI = "moonshotai/kimi-k2.6"


def _openrouter_list(*extra):
    return {
        "openrouter": [
            ModelInfo(
                id=KIMI, context_window=200000, max_output_tokens=8000,
                provider="openrouter", reasoning=True, supports_tools=True,
            ),
            *extra,
        ],
    }


def _openrouter_spelling_of_opus():
    return ModelInfo(
        id=ANTHROPIC_SPELLING, context_window=200000, max_output_tokens=8000,
        provider="openrouter", supports_tools=True,
    )


@pytest.fixture(autouse=True)
def _no_discovered():
    set_discovered(None)
    yield
    set_discovered(None)


def _document(*, kind="native", fallback="", discover=True, provider="openrouter", providers=()):
    return {
        "brain": {
            "kind": kind,
            "fallback": fallback,
            "room_selectable": ["native", "claude_code"],
            "native": {"provider": provider, "providers": list(providers), "model": "z-ai/glm-5"},
        },
        "providers": {
            "anthropic": {"api_key": "anthropic-key"},
            "openrouter": {"api_key": "or-key", "discover": discover},
            "local": {"kind": "openai_compat", "base_url": "http://192.0.2.10:8080/v1"},
        },
    }


def _config(tmp_path, **kwargs) -> Config:
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    source = config_from_document(_document(**kwargs))
    config = Config(
        db_path=db_path,
        skills_dir=tmp_path / "skills",
        bundled_skills_dir=tmp_path / "_empty_bundled",
        temp_dir=tmp_path / "temp",
        workspace_path=tmp_path / "workspace",
        security=SecurityConfig(skill_proxy_enabled=False),
        users={"alice": UserConfig()},
    )
    config.brain = source.brain
    config.providers = source.providers
    config.models = source.models
    config.admin_users = {"alice"}
    return config


def _room(conn, token="room1", *, brain=None, model=None):
    db.register_room(conn, token, "alice", origin="web")
    db.add_room_binding(conn, token, "web", token)
    if brain:
        db.set_room_brain(conn, token, brain)
    if model:
        db.set_room_model(conn, token, model)


def _inbound_task(config, *, brain=None, model=None):
    with db.get_db(config.db_path) as conn:
        _room(conn, brain=brain, model=model)
        inbound = record_inbound(
            conn, config, surface="web", surface_ref="room1",
            user_id="alice", text="hello", source_type="web",
        )
        return db.get_task(conn, inbound.task_id)


def _run_native(config, tmp_path, model, *, through_executor=None):
    from istota.brain import BrainRequest

    specs, providers = [], []

    def _make(spec):
        specs.append(spec)
        provider = MockProvider([AssistantMessage(
            content=[TextContent(text="ok")], stop_reason="stop", usage=Usage(),
        )])
        providers.append(provider)
        return provider

    req = BrainRequest(
        prompt="hi", allowed_tools=[], cwd=str(tmp_path), env={},
        timeout_seconds=30, model=model,
    )
    brain_config = config.brain
    if through_executor is not None:
        # The executor's own request model and per-user key overlay.
        from istota.executor import _request_model, _with_user_provider_key

        brain_config = _with_user_provider_key(brain_config, config, "alice", through_executor.model)
        req = BrainRequest(
            prompt="hi", allowed_tools=[], cwd=str(tmp_path), env={}, timeout_seconds=30,
            model=_request_model(through_executor, config, make_brain(brain_config)),
        )
    with patch("istota.brain.native.make_provider", _make), \
            patch("istota.llm.discovery.install_from_cache_dir", return_value=False):
        result = make_brain(brain_config).execute(req)
    assert result.success, result.result_text
    return [s.id for s in specs], [c["model"] for p in providers for c in p.calls]


class TestAnOpenRouterNativeRoom:
    def test_without_a_cache_an_anthropic_ref_is_refused_not_read_as_anthropic(self, tmp_path):
        config = _config(tmp_path)
        picked = resolve_for_person(ANTHROPIC_SPELLING, config, kind="native", is_admin=True)
        assert isinstance(picked, ResolveError)
        assert picked.code == "model_not_runnable"

    def test_with_a_cache_the_pick_is_openrouter_s_id(self, tmp_path):
        set_discovered(_openrouter_list(_openrouter_spelling_of_opus()))
        config = _config(tmp_path)
        picked = resolve_for_person(ANTHROPIC_SPELLING, config, kind="native", is_admin=True)
        assert isinstance(picked, Resolution)
        assert stored_selection(picked) == f"openrouter/{ANTHROPIC_SPELLING}"

    def test_with_a_cache_an_anthropic_direct_model_is_refused(self, tmp_path):
        # Listed by OpenRouter under another id only: the anthropic provider's
        # own ref is not runnable here.
        set_discovered(_openrouter_list())
        config = _config(tmp_path)
        picked = resolve_for_person(ANTHROPIC_SPELLING, config, kind="native", is_admin=True)
        assert isinstance(picked, ResolveError)
        assert picked.code == "model_not_runnable"

    def test_with_discovery_off_the_task_runs_on_openrouter(self, tmp_path):
        config = _config(tmp_path, discover=False)
        model, _effort = selection_for_task(config, ANTHROPIC_SPELLING, kind="native")
        assert model == ANTHROPIC_SPELLING
        built, wire = _run_native(config, tmp_path, model)
        assert built == ["openrouter"]
        assert wire == [ANTHROPIC_SPELLING]

    @pytest.mark.parametrize("cached", [False, True])
    def test_through_record_inbound_the_task_keeps_openrouter_s_reading(self, tmp_path, cached):
        if cached:
            set_discovered(_openrouter_list(_openrouter_spelling_of_opus()))
        config = _config(tmp_path)
        task = _inbound_task(config, brain="native", model=ANTHROPIC_SPELLING)
        assert task.brain == "native"
        if cached:
            assert task.model == f"openrouter/{ANTHROPIC_SPELLING}"
        built, wire = _run_native(config, tmp_path, task.model)
        assert built == ["openrouter"]
        assert wire == [ANTHROPIC_SPELLING]


class TestAListedProvider:
    """Native on an operator provider: `anthropic` runs only once listed."""

    def test_unlisted_an_anthropic_ref_is_refused(self, tmp_path):
        config = _config(tmp_path, provider="local")
        picked = resolve_for_person(ANTHROPIC_SPELLING, config, kind="native", is_admin=True)
        assert isinstance(picked, ResolveError)
        assert picked.code == "model_not_runnable"

    def test_listed_it_is_accepted_and_routed_through_record_inbound(self, tmp_path):
        config = _config(tmp_path, provider="local", providers=("anthropic",))
        picked = resolve_for_person(ANTHROPIC_SPELLING, config, kind="native", is_admin=True)
        assert isinstance(picked, Resolution)
        assert stored_selection(picked) == ANTHROPIC_SPELLING
        task = _inbound_task(config, brain="native", model=ANTHROPIC_SPELLING)
        assert task.model == ANTHROPIC_SPELLING
        built, wire = _run_native(config, tmp_path, task.model, through_executor=task)
        assert built[-1] == "anthropic"
        assert wire == ["claude-opus-5"]


class TestAClaudeCodeRoom:
    def test_an_openrouter_ref_is_refused(self, tmp_path):
        set_discovered(_openrouter_list())
        config = _config(tmp_path, kind="claude_code")
        picked = resolve_for_person(
            f"openrouter/{KIMI}", config, kind="claude_code", is_admin=True,
        )
        assert isinstance(picked, ResolveError)
        assert picked.code == "model_not_runnable"


class TestTheFallbackAcrossBrains:
    """claude_code -> native(openrouter): a provider change by definition."""

    def _crossing(self, config, task):
        import dataclasses

        from istota.executor import _pin_origin_namespace, _resolve_crossing_model_effort

        target = make_brain(dataclasses.replace(config.brain, kind="native"))
        return _resolve_crossing_model_effort(
            task, config, target, task.effort or "",
            origin_namespace=_pin_origin_namespace(task, config),
        )

    def test_an_exact_anthropic_pin_is_dropped(self, tmp_path):
        config = _config(tmp_path, kind="claude_code", fallback="native")
        task = _inbound_task(config, model=ANTHROPIC_SPELLING)
        assert task.model == ANTHROPIC_SPELLING
        model, _effort, dropped = self._crossing(config, task)
        assert (model, dropped) == ("", ANTHROPIC_SPELLING)

    def test_a_portable_alias_survives_task_creation_and_re_resolves(self, tmp_path):
        from istota.brain._roles import set_alias_overrides
        from istota.executor import _request_model

        config = _config(tmp_path, kind="claude_code", fallback="native")
        set_alias_overrides({"smart": {"openai_compat": KIMI}})
        try:
            task = _inbound_task(config, model="smart")
            assert task.model == "smart"
            primary = _request_model(task, config, make_brain(config.brain))
            model, _effort, dropped = self._crossing(config, task)
        finally:
            set_alias_overrides({})
        assert primary.startswith("claude-opus-")
        assert (model, dropped) == (KIMI, None)


@pytest.mark.asyncio
async def test_chat_commands_lists_only_the_room_brain_s_provider(tmp_path):
    pytest.importorskip("fastapi")
    from .test_web_chat_commands import (
        _client_for,
        _kimi,
        _login,
        _make_config,
        _make_room,
        _write_openrouter_cache,
    )

    config = _make_config(tmp_path)
    source = config_from_document(_document())
    config.brain = source.brain
    config.providers = source.providers
    config.admin_users = {"alice"}
    _write_openrouter_cache(config, [_kimi()])
    async with _client_for(config) as client:
        cookies = await _login(client, "alice")
        room = await _make_room(client, cookies)
        with db.get_db(config.db_path) as conn:
            db.set_room_brain(conn, room["token"], "native")
        native = (await client.get(
            f"/istota/api/chat/commands?room_id={room['id']}", cookies=cookies,
        )).json()["models"]
        with db.get_db(config.db_path) as conn:
            db.set_room_brain(conn, room["token"], "claude_code")
        cli = (await client.get(
            f"/istota/api/chat/commands?room_id={room['id']}", cookies=cookies,
        )).json()["models"]
    assert native and {m["provider"] for m in native} == {"openrouter"}
    assert f"openrouter/{KIMI}" in {m["ref"] for m in native}
    assert cli and {m["provider"] for m in cli} == {"anthropic"}


@pytest.mark.asyncio
async def test_chat_commands_lists_every_listed_provider(tmp_path):
    pytest.importorskip("fastapi")
    from .test_web_chat_commands import (
        _client_for,
        _kimi,
        _login,
        _make_config,
        _make_room,
        _write_openrouter_cache,
    )

    config = _make_config(tmp_path)
    source = config_from_document(_document(providers=("anthropic",)))
    config.brain = source.brain
    config.providers = source.providers
    config.admin_users = {"alice"}
    _write_openrouter_cache(config, [_kimi()])
    async with _client_for(config) as client:
        cookies = await _login(client, "alice")
        room = await _make_room(client, cookies)
        with db.get_db(config.db_path) as conn:
            db.set_room_brain(conn, room["token"], "native")
        native = (await client.get(
            f"/istota/api/chat/commands?room_id={room['id']}", cookies=cookies,
        )).json()["models"]
    assert {m["provider"] for m in native} == {"openrouter", "anthropic"}


class TestDoctorNamesTheAmbiguity:
    def test_anthropic_listed_beside_openrouter_warns(self, tmp_path):
        from istota.doctor import WARN, check_models_catalogue

        config = _config(tmp_path, providers=("anthropic",))
        result = check_models_catalogue(config, probe=False)
        assert result.status == WARN
        assert "ambiguous" in result.detail

    def test_openrouter_alone_does_not(self, tmp_path):
        from istota.doctor import check_models_catalogue

        result = check_models_catalogue(_config(tmp_path), probe=False)
        assert "ambiguous" not in result.detail
