"""Stage 3 — NativeBrain live OpenRouter model-catalog fetch wiring."""

import asyncio
import threading
import time

import pytest

import istota.llm.openrouter_catalog as orc
from istota.brain import BrainRequest
from istota.brain import native as native_mod
from istota.brain.native import NativeBrain
from istota.config import NativeBrainConfig
from istota.llm.catalog import ModelInfo, get_model_info, set_fetched_catalog
from istota.llm.types import AssistantMessage, TextContent, Usage

from ._mock_provider import MockProvider


@pytest.fixture(autouse=True)
def _reset_catalog():
    # Process-global state must be clean before + after each test (suite -n auto).
    native_mod._reset_catalog_fetch_state()
    set_fetched_catalog(None)
    yield
    native_mod._reset_catalog_fetch_state()
    set_fetched_catalog(None)


def _brain(**cfg) -> NativeBrain:
    config = NativeBrainConfig(model="anthropic/claude-opus-4.8", **cfg)
    return NativeBrain(config, provider=MockProvider([]))


def _req(tmp_path, *, db_path=True) -> BrainRequest:
    env = {"ISTOTA_DB_PATH": str(tmp_path / "istota.db")} if db_path else {}
    return BrainRequest(
        prompt="hi",
        allowed_tools=[],
        cwd=tmp_path,
        env=env,
        timeout_seconds=30,
        model="anthropic/claude-opus-4.8",
    )


_FETCHED = {
    "anthropic/claude-opus-4.8": ModelInfo(
        id="anthropic/claude-opus-4.8",
        context_window=500_000,
        max_output_tokens=64000,
        supports_thinking=True,
    )
}


class TestSkip:
    def test_skips_non_openrouter_endpoint(self, tmp_path, monkeypatch):
        calls = []

        async def _fake_fetch(*a, **k):
            calls.append(1)
            return _FETCHED

        monkeypatch.setattr(orc, "fetch_openrouter_catalog", _fake_fetch)
        brain = _brain(base_url="https://api.anthropic.com/v1", model_catalog_fetch=True)
        asyncio.run(brain._ensure_fetched_catalog(_req(tmp_path)))
        assert calls == []
        # nothing installed → default resolution
        assert get_model_info("anthropic/claude-opus-4.8").context_window == 200_000

    def test_skips_when_disabled(self, tmp_path, monkeypatch):
        calls = []

        async def _fake_fetch(*a, **k):
            calls.append(1)
            return _FETCHED

        monkeypatch.setattr(orc, "fetch_openrouter_catalog", _fake_fetch)
        brain = _brain(base_url="https://openrouter.ai/api/v1", model_catalog_fetch=False)
        asyncio.run(brain._ensure_fetched_catalog(_req(tmp_path)))
        assert calls == []


class TestFetchOnce:
    def test_fetches_installs_and_caches(self, tmp_path, monkeypatch):
        calls = []

        async def _fake_fetch(base_url, *, api_key="", **k):
            calls.append(base_url)
            return _FETCHED

        monkeypatch.setattr(orc, "fetch_openrouter_catalog", _fake_fetch)
        brain = _brain(base_url="https://openrouter.ai/api/v1")

        asyncio.run(brain._ensure_fetched_catalog(_req(tmp_path)))
        assert len(calls) == 1
        # installed → real window
        assert get_model_info("anthropic/claude-opus-4.8").context_window == 500_000
        # disk cache written
        assert orc.cache_path(tmp_path).exists()

        # second call within TTL: no refetch (module timestamp guard)
        asyncio.run(brain._ensure_fetched_catalog(_req(tmp_path)))
        assert len(calls) == 1

    def test_fresh_disk_cache_used_without_fetch(self, tmp_path, monkeypatch):
        # pre-seed a fresh cache; a fresh process (reset timestamp) reads it
        orc.write_cache(orc.cache_path(tmp_path), _FETCHED, now_ts=time.time())
        calls = []

        async def _fake_fetch(*a, **k):
            calls.append(1)
            return {}

        monkeypatch.setattr(orc, "fetch_openrouter_catalog", _fake_fetch)
        brain = _brain(base_url="https://openrouter.ai/api/v1")
        asyncio.run(brain._ensure_fetched_catalog(_req(tmp_path)))
        assert calls == []
        assert get_model_info("anthropic/claude-opus-4.8").context_window == 500_000

    def test_no_db_path_still_fetches_memory_only(self, tmp_path, monkeypatch):
        calls = []

        async def _fake_fetch(*a, **k):
            calls.append(1)
            return _FETCHED

        monkeypatch.setattr(orc, "fetch_openrouter_catalog", _fake_fetch)
        brain = _brain(base_url="https://openrouter.ai/api/v1")
        asyncio.run(brain._ensure_fetched_catalog(_req(tmp_path, db_path=False)))
        assert len(calls) == 1
        assert get_model_info("anthropic/claude-opus-4.8").context_window == 500_000
        # no cache file written (no data dir)
        assert not orc.cache_path(tmp_path).exists()


class TestFailureFallback:
    def test_fetch_fails_uses_stale_cache(self, tmp_path, monkeypatch):
        # stale cache present (written long ago)
        orc.write_cache(orc.cache_path(tmp_path), _FETCHED, now_ts=1.0)

        async def _boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr(orc, "fetch_openrouter_catalog", _boom)
        brain = _brain(base_url="https://openrouter.ai/api/v1", model_catalog_cache_ttl_hours=24.0)
        asyncio.run(brain._ensure_fetched_catalog(_req(tmp_path)))
        # stale cache installed rather than raising
        assert get_model_info("anthropic/claude-opus-4.8").context_window == 500_000

    def test_fetch_fails_no_cache_falls_to_default(self, tmp_path, monkeypatch):
        async def _boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr(orc, "fetch_openrouter_catalog", _boom)
        brain = _brain(base_url="https://openrouter.ai/api/v1")
        asyncio.run(brain._ensure_fetched_catalog(_req(tmp_path)))
        assert get_model_info("anthropic/claude-opus-4.8").context_window == 200_000
        # timestamp NOT stamped → a later task retries
        assert native_mod._CATALOG_FETCHED_AT is None

    def test_empty_payload_treated_as_failure(self, tmp_path, monkeypatch):
        async def _empty(*a, **k):
            return {}

        monkeypatch.setattr(orc, "fetch_openrouter_catalog", _empty)
        brain = _brain(base_url="https://openrouter.ai/api/v1")
        asyncio.run(brain._ensure_fetched_catalog(_req(tmp_path)))
        assert get_model_info("anthropic/claude-opus-4.8").context_window == 200_000
        assert native_mod._CATALOG_FETCHED_AT is None


class TestConcurrency:
    def test_concurrent_first_tasks_fetch_at_most_once(self, tmp_path, monkeypatch):
        calls = []

        async def _slow_fetch(*a, **k):
            calls.append(1)
            await asyncio.sleep(0.05)
            return _FETCHED

        monkeypatch.setattr(orc, "fetch_openrouter_catalog", _slow_fetch)

        def _run():
            brain = _brain(base_url="https://openrouter.ai/api/v1")
            asyncio.run(brain._ensure_fetched_catalog(_req(tmp_path)))

        threads = [threading.Thread(target=_run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) == 1


class TestExecuteWiring:
    def test_execute_triggers_fetch(self, tmp_path, monkeypatch):
        calls = []

        async def _fake_fetch(*a, **k):
            calls.append(1)
            return _FETCHED

        monkeypatch.setattr(orc, "fetch_openrouter_catalog", _fake_fetch)
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="done")],
                    usage=Usage(input_tokens=10, output_tokens=2),
                    stop_reason="end_turn",
                )
            ]
        )
        config = NativeBrainConfig(
            model="anthropic/claude-opus-4.8", base_url="https://openrouter.ai/api/v1"
        )
        brain = NativeBrain(config, provider=provider)
        result = brain.execute(_req(tmp_path))
        assert result.success is True
        assert len(calls) == 1
        assert get_model_info("anthropic/claude-opus-4.8").context_window == 500_000
