"""The provider a room, job or task runs on is stored, not parsed (D22).

`rooms`, `scheduled_jobs` and `tasks` carry a nullable `provider` beside
`brain`. NULL is the brain's default provider (`anthropic` for the CLI brains,
`[brain.native] provider` for native), so a row written before the column
existed keeps running where it ran. Native's routing keys on the task's
column: the first segment of a model id is never read as a provider for a
task, and a provider dropped from `[brain.native] providers` falls back to the
default with a WARNING.
"""

from __future__ import annotations

import logging
import sqlite3
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, SecurityConfig, UserConfig, config_from_document
from istota.llm.catalog import ModelInfo, set_discovered
from istota.llm.types import AssistantMessage, TextContent, Usage
from istota.transport.ingest import record_inbound

from .native._mock_provider import MockProvider

_TABLES = ("rooms", "scheduled_jobs", "tasks")


@pytest.fixture(autouse=True)
def _discovered():
    set_discovered({
        "openrouter": [
            ModelInfo(
                id="z-ai/glm-5", context_window=200000, max_output_tokens=8000,
                provider="openrouter", supports_tools=True,
            ),
        ],
        "local": [
            ModelInfo(
                id="qwen3", context_window=32000, max_output_tokens=4000,
                provider="local", supports_tools=True,
            ),
        ],
    })
    yield
    set_discovered(None)


def _document(*, providers=("local",)):
    return {
        "brain": {
            "kind": "native",
            "room_selectable": ["native", "claude_code"],
            "native": {"provider": "openrouter", "providers": list(providers), "model": "z-ai/glm-5"},
        },
        "providers": {
            "openrouter": {"api_key": "or-key"},
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


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _make_old(db_path):
    """The database as the release before the column left it: no `provider`
    on the three tables, stamped schema 1."""
    with sqlite3.connect(db_path) as conn:
        for table in _TABLES:
            if "provider" in _columns(conn, table):
                conn.execute(f"ALTER TABLE {table} DROP COLUMN provider")
        conn.execute("PRAGMA user_version = 1")


def _run(config, tmp_path, task):
    """The executor's brain for ``task`` against stub providers: which
    provider records were built and which model ids reached the wire."""
    from istota.brain import BrainRequest
    from istota.executor import _request_model, _task_brain

    built, providers = [], []

    def _make(spec):
        built.append(spec.id)
        provider = MockProvider([AssistantMessage(
            content=[TextContent(text="ok")], stop_reason="stop", usage=Usage(),
        )])
        providers.append(provider)
        return provider

    with patch("istota.brain.native.make_provider", _make), \
            patch("istota.llm.discovery.install_from_cache_dir", return_value=False):
        _brain_config, brain = _task_brain(task, config)
        req = BrainRequest(
            prompt="hi", allowed_tools=[], cwd=str(tmp_path), env={}, timeout_seconds=30,
            model=_request_model(task, config, brain),
        )
        result = brain.execute(req)
    assert result.success, result.result_text
    return built, [call["model"] for p in providers for call in p.calls]


class TestTheMigration:
    def test_old_rows_read_as_the_default_provider(self, tmp_path):
        config = _config(tmp_path)
        _make_old(config.db_path)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO rooms (token, user_id, origin, brain, model) "
                "VALUES ('old', 'alice', 'web', 'native', 'z-ai/glm-5')"
            )
            conn.execute(
                "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, brain, model) "
                "VALUES ('alice', 'nightly', '0 3 * * *', 'hi', 'native', 'z-ai/glm-5')"
            )
            conn.execute(
                "INSERT INTO tasks (prompt, user_id, source_type, brain, model) "
                "VALUES ('hi', 'alice', 'web', 'native', 'z-ai/glm-5')"
            )

        db.init_db(config.db_path)

        with db.get_db(config.db_path) as conn:
            for table in _TABLES:
                assert "provider" in _columns(conn, table)
            assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
            assert db.get_room(conn, "old").provider is None
            assert db.get_scheduled_job_by_name(conn, "alice", "nightly").provider is None
            task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]
            task = db.get_task(conn, task_id)
        assert task.provider is None
        built, wire = _run(config, tmp_path, task)
        assert built == ["openrouter"]
        assert wire == ["z-ai/glm-5"]

    def test_the_schema_stamp_moved(self):
        assert db.SCHEMA_VERSION >= 2

    def test_an_old_room_s_next_task_runs_where_it_ran(self, tmp_path):
        config = _config(tmp_path)
        _make_old(config.db_path)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO rooms (token, user_id, origin, brain, model) "
                "VALUES ('old', 'alice', 'web', 'native', 'z-ai/glm-5')"
            )
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, "old", "web", "old")
            inbound = record_inbound(
                conn, config, surface="web", surface_ref="old",
                user_id="alice", text="hello", source_type="web",
            )
            task = db.get_task(conn, inbound.task_id)
        assert task.provider is None
        assert task.model == "z-ai/glm-5"
        built, wire = _run(config, tmp_path, task)
        assert built == ["openrouter"]
        assert wire == ["z-ai/glm-5"]


class TestRoutingByTheColumn:
    def _task(self, config, *, model, provider=None):
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="hi", user_id="alice", source_type="web",
                brain="native", model=model, provider=provider,
            )
            return db.get_task(conn, task_id)

    def test_a_task_on_a_listed_provider_goes_there(self, tmp_path):
        config = _config(tmp_path)
        task = self._task(config, model="qwen3", provider="local")
        assert task.provider == "local"
        built, wire = _run(config, tmp_path, task)
        assert built[-1] == "local"
        assert wire == ["qwen3"]

    def test_a_ref_head_is_not_read_as_a_provider(self, tmp_path):
        # The old shape routed `local/qwen3` to `local` by its first segment.
        # With no provider stored, the default provider gets the value as written.
        config = _config(tmp_path)
        task = self._task(config, model="local/qwen3")
        built, wire = _run(config, tmp_path, task)
        assert built == ["openrouter"]
        assert wire == ["local/qwen3"]

    def test_a_provider_dropped_from_the_list_falls_back_with_a_warning(self, tmp_path, caplog):
        config = _config(tmp_path, providers=())
        task = self._task(config, model="z-ai/glm-5", provider="local")
        with caplog.at_level(logging.WARNING):
            built, wire = _run(config, tmp_path, task)
        assert built == ["openrouter"]
        assert wire == ["z-ai/glm-5"]
        assert any("local" in r.getMessage() and "providers" in r.getMessage() for r in caplog.records)

    def test_a_listed_provider_with_no_model_uses_its_default_model(self, tmp_path):
        document = _document()
        document["providers"]["local"]["default_model"] = "qwen3"
        source = config_from_document(document)
        config = _config(tmp_path)
        config.brain = source.brain
        config.providers = source.providers
        task = self._task(config, model=None, provider="local")
        built, wire = _run(config, tmp_path, task)
        assert built[-1] == "local"
        assert wire == ["qwen3"]
