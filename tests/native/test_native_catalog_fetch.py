"""NativeBrain reads the discovered model lists from their caches and never
fetches (model-catalogue spec, Stage 4; the fetch is the scheduler gate's)."""

import asyncio
import json
import os
import time

from istota.brain import BrainRequest
from istota.brain.native import NativeBrain
from istota.config import NativeBrainConfig
from istota.llm import discovery
from istota.llm.catalog import ModelInfo, get_model_info
from istota.llm.types import AssistantMessage, TextContent, Usage

from ._mock_provider import MockProvider

_MODEL = "anthropic/claude-opus-4.8"


def _brain(provider=None) -> NativeBrain:
    config = NativeBrainConfig(model=_MODEL, base_url="https://openrouter.ai/api/v1")
    return NativeBrain(config, provider=provider or MockProvider([]))


def _req(tmp_path, *, db_path=True) -> BrainRequest:
    # The cache dir comes from BrainRequest.db_path, not from ISTOTA_DB_PATH in
    # the task env, which never reaches the sandbox env the brain carries.
    return BrainRequest(
        prompt="hi",
        allowed_tools=[],
        cwd=tmp_path,
        env={},
        db_path=(tmp_path / "istota.db") if db_path else None,
        timeout_seconds=30,
        model=_MODEL,
    )


def _write_openrouter_cache(tmp_path, *, window=500_000, fetched_at=None) -> None:
    info = ModelInfo(id=_MODEL, context_window=window, max_output_tokens=64000, supports_thinking=True)
    fetched_at = time.time() if fetched_at is None else fetched_at
    doc = {
        "provider": "openrouter",
        "kind": "openrouter",
        "fetched_at": fetched_at,
        "models": {_MODEL: {**discovery._record_json(info), "last_seen": fetched_at}},
    }
    cache_dir = tmp_path / discovery.CACHE_DIRNAME
    cache_dir.mkdir(exist_ok=True)
    (cache_dir / "openrouter.json").write_text(json.dumps(doc))


class TestItReadsTheCacheOnly:
    def test_a_cache_is_installed(self, tmp_path):
        _write_openrouter_cache(tmp_path)
        asyncio.run(_brain()._ensure_fetched_catalog(_req(tmp_path)))
        assert get_model_info(_MODEL).context_window == 500_000

    def test_no_cache_installs_nothing_and_fetches_nothing(self, tmp_path):
        # The conftest refuses both real transports, so a fetch would raise
        # inside the brain and be swallowed; the cache dir staying absent is
        # the evidence no refresh ran here.
        asyncio.run(_brain()._ensure_fetched_catalog(_req(tmp_path)))
        assert get_model_info(_MODEL).context_window == 200_000
        assert not (tmp_path / discovery.CACHE_DIRNAME).exists()

    def test_a_stale_cache_still_serves(self, tmp_path):
        _write_openrouter_cache(tmp_path, fetched_at=1.0)
        asyncio.run(_brain()._ensure_fetched_catalog(_req(tmp_path)))
        assert get_model_info(_MODEL).context_window == 500_000

    def test_no_db_path_leaves_the_installed_layer(self, tmp_path):
        asyncio.run(_brain()._ensure_fetched_catalog(_req(tmp_path, db_path=False)))
        assert get_model_info(_MODEL).context_window == 200_000

    def test_a_rewritten_cache_is_picked_up_at_the_next_task(self, tmp_path):
        _write_openrouter_cache(tmp_path)
        asyncio.run(_brain()._ensure_fetched_catalog(_req(tmp_path)))
        _write_openrouter_cache(tmp_path, window=600_000)
        path = tmp_path / discovery.CACHE_DIRNAME / "openrouter.json"
        stamp = time.time() + 5
        os.utime(path, (stamp, stamp))
        asyncio.run(_brain()._ensure_fetched_catalog(_req(tmp_path)))
        assert get_model_info(_MODEL).context_window == 600_000

    def test_a_corrupt_cache_never_fails_the_task(self, tmp_path):
        cache_dir = tmp_path / discovery.CACHE_DIRNAME
        cache_dir.mkdir()
        (cache_dir / "openrouter.json").write_text("{nope")
        asyncio.run(_brain()._ensure_fetched_catalog(_req(tmp_path)))
        assert get_model_info(_MODEL).context_window == 200_000


class TestExecuteWiring:
    def test_execute_installs_the_cache_before_the_loop(self, tmp_path):
        _write_openrouter_cache(tmp_path)
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="done")],
                    usage=Usage(input_tokens=10, output_tokens=2),
                    stop_reason="end_turn",
                )
            ]
        )
        result = _brain(provider).execute(_req(tmp_path))
        assert result.success is True
        assert get_model_info(_MODEL).context_window == 500_000
