"""The writers of the provider column (D22 of the model-catalogue spec).

Choosing a native room's or job's provider is admin-only, because the
provider decides which key is billed, and it is offered only from
`[brain.native] providers`. `!room provider`, the room PATCH and CRON.md's
`provider` are the three writers; an admin's `provider/id` is split into the
two columns wherever a model is written. A task copies the room's or job's
provider, and a retry or subtask copies the task's. Aliases have a target per
provider, and one with no target for the room's provider never resolves into
another.
"""

from __future__ import annotations

import pytest

from istota import db
from istota.brain._roles import set_alias_overrides
from istota.commands import CommandContext, _create_retry_task, cmd_room, resolve_model_prefix
from istota.config import Config, SecurityConfig, UserConfig, config_from_document
from istota.llm.catalog import ModelInfo, set_discovered
from istota.transport.ingest import record_inbound


@pytest.fixture(autouse=True)
def _models():
    set_discovered({
        "openrouter": [ModelInfo(
            id="z-ai/glm-5", context_window=200000, max_output_tokens=8000,
            provider="openrouter", supports_tools=True,
        )],
        "local": [ModelInfo(
            id="qwen3", context_window=32000, max_output_tokens=4000,
            provider="local", supports_tools=True,
        )],
    })
    yield
    set_discovered(None)
    set_alias_overrides({})


def _document(*, providers=("local",), local_default=""):
    local = {"kind": "openai_compat", "base_url": "http://192.0.2.10:8080/v1"}
    if local_default:
        local["default_model"] = local_default
    return {
        "brain": {
            "kind": "native",
            "room_selectable": ["native", "claude_code"],
            "native": {"provider": "openrouter", "providers": list(providers), "model": "z-ai/glm-5"},
        },
        "providers": {"openrouter": {"api_key": "or-key"}, "local": local},
    }


def _config(tmp_path, *, admins=("alice",), **kwargs) -> Config:
    tmp_path.mkdir(parents=True, exist_ok=True)
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
        users={"alice": UserConfig(), "bob": UserConfig()},
    )
    config.brain = source.brain
    config.providers = source.providers
    config.models = source.models
    config.admin_users = set(admins)
    return config


def _room(conn, token="room1", *, brain="native", model=None, provider=None):
    db.register_room(conn, token, "alice", origin="web")
    db.add_room_binding(conn, token, "web", token)
    db.set_room_brain(conn, token, brain)
    if model:
        db.set_room_model(conn, token, model)
    if provider:
        db.set_room_provider(conn, token, provider)


def _ctx(config, conn, args, user_id="alice"):
    return CommandContext(
        config=config, conn=conn, user_id=user_id,
        conversation_token="room1", args=args, surface="web",
    )


@pytest.mark.asyncio
class TestRoomProviderCommand:
    async def test_an_admin_moves_a_room_to_a_listed_provider(self, tmp_path):
        config = _config(tmp_path, local_default="qwen3")
        with db.get_db(config.db_path) as conn:
            _room(conn, model="z-ai/glm-5")
            out = await cmd_room(_ctx(config, conn, "provider local"))
            room = db.get_room(conn, "room1")
        assert room.provider == "local"
        # OpenRouter's id is not a `local` model, so it goes with the move.
        assert room.model is None
        assert "cleared" in out

    async def test_a_member_is_refused(self, tmp_path):
        config = _config(tmp_path, local_default="qwen3")
        with db.get_db(config.db_path) as conn:
            _room(conn)
            out = await cmd_room(_ctx(config, conn, "provider local", user_id="bob"))
            assert db.get_room(conn, "room1").provider is None
        assert "Only an admin" in out

    async def test_an_unlisted_provider_is_refused(self, tmp_path):
        config = _config(tmp_path, providers=(), local_default="qwen3")
        with db.get_db(config.db_path) as conn:
            _room(conn)
            out = await cmd_room(_ctx(config, conn, "provider local"))
            assert db.get_room(conn, "room1").provider is None
        assert "isn't one of native's providers" in out

    async def test_a_provider_with_nothing_to_run_is_refused(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _room(conn)
            out = await cmd_room(_ctx(config, conn, "provider local"))
            assert db.get_room(conn, "room1").provider is None
        assert "no default model" in out

    async def test_a_model_of_the_new_provider_stays(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _room(conn, model="qwen3")
            await cmd_room(_ctx(config, conn, "provider local"))
            room = db.get_room(conn, "room1")
        assert (room.provider, room.model) == ("local", "qwen3")

    async def test_default_clears_it(self, tmp_path):
        config = _config(tmp_path, local_default="qwen3")
        with db.get_db(config.db_path) as conn:
            _room(conn, provider="local")
            await cmd_room(_ctx(config, conn, "provider default"))
            assert db.get_room(conn, "room1").provider is None

    async def test_a_cli_room_has_none(self, tmp_path):
        config = _config(tmp_path, local_default="qwen3")
        with db.get_db(config.db_path) as conn:
            _room(conn, brain="claude_code")
            out = await cmd_room(_ctx(config, conn, "provider local"))
            assert db.get_room(conn, "room1").provider is None
        assert "Only a native room" in out

    async def test_a_move_to_a_cli_brain_clears_it(self, tmp_path):
        from istota.commands import cmd_brain

        config = _config(tmp_path, local_default="qwen3")
        with db.get_db(config.db_path) as conn:
            _room(conn, provider="local")
            await cmd_brain(_ctx(config, conn, "claude_code"))
            assert db.get_room(conn, "room1").provider is None


@pytest.mark.asyncio
class TestTheModelWritersSplit:
    async def test_room_model_with_another_provider_s_ref(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _room(conn)
            await cmd_room(_ctx(config, conn, "model local/qwen3"))
            room = db.get_room(conn, "room1")
        assert (room.model, room.provider) == ("qwen3", "local")

    async def test_a_member_may_not_move_the_provider_by_model(self, tmp_path):
        config = _config(tmp_path, admins=())
        config.admin_users = {"carol"}
        config.models.enabled = ["local/*", "openrouter/*"]
        with db.get_db(config.db_path) as conn:
            _room(conn)
            out = await cmd_room(_ctx(config, conn, "model local/qwen3", user_id="bob"))
            room = db.get_room(conn, "room1")
        assert (room.model, room.provider) == (None, None)
        assert "only an admin can change the provider" in out

    async def test_the_inline_prefix_names_the_provider_for_its_task(self, tmp_path):
        from istota.brain import make_brain

        config = _config(tmp_path)
        prefix = resolve_model_prefix(
            "!model local/qwen3 hi", make_brain(config.brain), config=config, user_id="alice",
        )
        assert (prefix.model, prefix.provider) == ("qwen3", "local")
        with db.get_db(config.db_path) as conn:
            _room(conn)
            inbound = record_inbound(
                conn, config, surface="web", surface_ref="room1", user_id="alice",
                text=prefix.content, source_type="web", model=prefix.model,
                provider=prefix.provider, apply_room_default=False,
            )
            task = db.get_task(conn, inbound.task_id)
        assert (task.model, task.provider) == ("qwen3", "local")


class TestTaskCreationCopiesTheProvider:
    def test_record_inbound_copies_the_room_s(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _room(conn, model="qwen3", provider="local")
            inbound = record_inbound(
                conn, config, surface="web", surface_ref="room1", user_id="alice",
                text="hi", source_type="web",
            )
            task = db.get_task(conn, inbound.task_id)
        assert (task.brain, task.provider, task.model) == ("native", "local", "qwen3")

    def test_a_retry_inherits_it(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            original_id = db.create_task(
                conn, prompt="hi", user_id="alice", source_type="web",
                brain="native", model="qwen3", provider="local",
            )
            original = db.get_task(conn, original_id)
            retry = db.get_task(conn, _create_retry_task(conn, original, "hi"))
        assert (retry.provider, retry.model) == ("local", "qwen3")

    def test_a_subtask_inherits_it(self, tmp_path):
        import json

        from istota.scheduler_deferred import _process_deferred_subtasks

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="hi", user_id="alice", source_type="web",
                brain="native", model="qwen3", provider="local",
            )
            parent = db.get_task(conn, parent_id)
        deferred = tmp_path / "temp" / "alice"
        deferred.mkdir(parents=True, exist_ok=True)
        (deferred / f"task_{parent_id}_subtasks.json").write_text(
            json.dumps([{"prompt": "child"}]), encoding="utf-8",
        )
        assert _process_deferred_subtasks(config, parent, deferred) == 1
        with db.get_db(config.db_path) as conn:
            child = conn.execute(
                "SELECT provider, model FROM tasks WHERE parent_task_id = ?", (parent_id,),
            ).fetchone()
        assert tuple(child) == ("local", "qwen3")


class TestCronProvider:
    def _sync(self, config, user_id, job):
        from istota.cron_loader import _parse_jobs, sync_cron_jobs_to_db

        errors: list[str] = []
        jobs, _skipped, _refused = _parse_jobs({"jobs": [job]}, config, user_id, errors)
        with db.get_db(config.db_path) as conn:
            sync_cron_jobs_to_db(conn, user_id, jobs, is_admin=config.is_admin(user_id))
            return db.get_scheduled_job_by_name(conn, user_id, job["name"])

    def test_an_admin_s_provider_is_kept(self, tmp_path):
        config = _config(tmp_path)
        row = self._sync(config, "alice", {
            "name": "n", "cron": "0 3 * * *", "prompt": "hi",
            "brain": "native", "provider": "local", "model": "qwen3",
        })
        assert (row.provider, row.model) == ("local", "qwen3")

    def test_a_member_s_provider_is_dropped(self, tmp_path):
        config = _config(tmp_path)
        row = self._sync(config, "bob", {
            "name": "n", "cron": "0 3 * * *", "prompt": "hi", "provider": "local",
        })
        assert row.provider is None

    def test_an_admin_s_ref_is_split(self, tmp_path):
        config = _config(tmp_path)
        row = self._sync(config, "alice", {
            "name": "n", "cron": "0 3 * * *", "prompt": "hi", "model": "local/qwen3",
        })
        assert (row.provider, row.model) == ("local", "qwen3")

    def test_the_job_s_task_copies_it(self, tmp_path):
        from istota.scheduler import _create_scheduled_job_task

        config = _config(tmp_path)
        row = self._sync(config, "alice", {
            "name": "n", "cron": "0 3 * * *", "prompt": "hi", "provider": "local", "model": "qwen3",
        })
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, _create_scheduled_job_task(conn, config, row))
        assert (task.provider, task.model) == ("local", "qwen3")


class TestAliasesPerProvider:
    def _brain(self, config, provider):
        from istota.brain import make_brain

        return make_brain(config.brain).bound_to(provider)

    def test_a_provider_key_applies_on_that_provider_only(self, tmp_path):
        config = _config(tmp_path)
        set_alias_overrides({"deep": {"local": "qwen3", "openrouter": "z-ai/glm-5"}})
        assert self._brain(config, "local").resolve_model_name("deep") == "qwen3"
        assert self._brain(config, "openrouter").resolve_model_name("deep") == "z-ai/glm-5"

    def test_the_old_native_key_is_the_default_provider_s(self, tmp_path):
        config = _config(tmp_path)
        set_alias_overrides({"deep": {"openai_compat": "z-ai/glm-5"}})
        assert self._brain(config, "openrouter").resolve_alias("deep")[0] == "z-ai/glm-5"
        assert self._brain(config, "local").resolve_alias("deep") is None

    def test_an_alias_with_no_target_here_never_resolves_into_another_provider(self, tmp_path):
        from istota.brain._resolve import ResolveError, select_for_person

        config = _config(tmp_path)
        set_alias_overrides({"deep": {"openrouter": "z-ai/glm-5"}})
        picked = select_for_person("deep", config, kind="native", provider="local", is_admin=True)
        assert isinstance(picked, ResolveError)

    def test_a_listed_provider_s_default_model_fills_an_unpinned_task(self, tmp_path):
        config = _config(tmp_path, local_default="qwen3")
        assert self._brain(config, "local").default_model == "qwen3"
        assert self._brain(config, "openrouter").default_model == "z-ai/glm-5"


class TestTheConfigPageSources:
    def test_native_model_names_the_default_provider_and_internal_fields_the_list(self, tmp_path):
        from istota.webui.admin_config_view import _model_sources

        config = _config(tmp_path)
        assert _model_sources(config, "native") == ["openrouter"]
        assert _model_sources(config, "any") == ["anthropic", "openrouter", "local"]
        unlisted = _config(tmp_path / "b", providers=())
        assert _model_sources(unlisted, "any") == ["anthropic", "openrouter"]


def _web_config(tmp_path, *, admins=("alice",), **kwargs):
    from .test_web_chat_commands import _make_config

    config = _make_config(tmp_path)
    config.users = {"alice": UserConfig(), "bob": UserConfig()}
    source = config_from_document(_document(**kwargs))
    config.brain = source.brain
    config.providers = source.providers
    config.admin_users = set(admins)
    return config


@pytest.mark.asyncio
class TestTheRoomPatch:
    HDRS = {"origin": "https://example.com"}

    async def _patch(self, config, body):
        from unittest.mock import patch

        pytest.importorskip("fastapi")
        from .test_web_chat_commands import _client_for, _login, _make_room

        async with _client_for(config) as client:
            cookies = await _login(client, "alice")
            room = await _make_room(client, cookies)
            with db.get_db(config.db_path) as conn:
                db.set_room_brain(conn, room["token"], "native")
            with patch("istota.llm.discovery.install_from_cache_dir", return_value=False):
                resp = await client.patch(
                    f"/istota/api/chat/rooms/{room['id']}", json=body,
                    cookies=cookies, headers=self.HDRS,
                )
                payload = (await client.get(
                    f"/istota/api/chat/commands?room_id={room['id']}", cookies=cookies,
                )).json()
        with db.get_db(config.db_path) as conn:
            stored = db.get_room(conn, room["token"])
        return resp, stored, payload

    async def test_an_admin_sets_a_provider_and_its_model(self, tmp_path):
        config = _web_config(tmp_path)
        resp, stored, payload = await self._patch(config, {"provider": "local", "model": "qwen3"})
        assert resp.status_code == 200, resp.text
        assert (stored.provider, stored.model) == ("local", "qwen3")
        assert payload["room_provider"] == "local"
        assert [p["id"] for p in payload["providers"]] == ["openrouter", "local"]

    async def test_a_member_is_refused(self, tmp_path):
        config = _web_config(tmp_path, admins=("carol",))
        resp, stored, payload = await self._patch(config, {"provider": "local"})
        assert resp.status_code == 403
        assert stored.provider is None
        assert payload["providers"] == []

    async def test_an_unlisted_provider_is_refused(self, tmp_path):
        config = _web_config(tmp_path, providers=(), local_default="qwen3")
        resp, stored, _payload = await self._patch(config, {"provider": "local"})
        assert resp.status_code == 400
        assert stored.provider is None

    async def test_a_provider_with_nothing_to_run_is_refused(self, tmp_path):
        config = _web_config(tmp_path)
        resp, stored, _payload = await self._patch(config, {"provider": "local"})
        assert resp.status_code == 422
        assert resp.json()["code"] == "no_default_model"
        assert stored.provider is None

    async def test_an_admin_s_ref_is_split(self, tmp_path):
        config = _web_config(tmp_path)
        resp, stored, _payload = await self._patch(config, {"model": "local/qwen3"})
        assert resp.status_code == 200, resp.text
        assert (stored.provider, stored.model) == ("local", "qwen3")

    async def test_a_member_s_model_list_is_the_room_s_provider(self, tmp_path):
        config = _web_config(tmp_path, admins=("carol",))
        config.models.enabled = ["openrouter/*", "local/*"]
        _resp, _stored, payload = await self._patch(config, {"name": "r"})
        assert {m["provider"] for m in payload["models"]} == {"openrouter"}
