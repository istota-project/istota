"""OpenRouter model-catalog parse + fetch + disk-cache helpers (Stage 2)."""

import json

import pytest

from istota.llm.catalog import ModelInfo
from istota.llm.openrouter_catalog import (
    cache_path,
    fetch_openrouter_catalog,
    parse_openrouter_models,
    read_cache,
    read_cache_any_age,
    write_cache,
)

# A trimmed, representative OpenRouter /models payload:
#  - a reasoning + vision Anthropic model with cache pricing
#  - a non-reasoning, no-vision model
#  - a model missing context_length (must be skipped)
#  - a model with non-numeric pricing (parsed; bad price → 0.0)
FIXTURE = {
    "data": [
        {
            "id": "anthropic/claude-opus-4.8",
            "context_length": 200000,
            "top_provider": {"max_completion_tokens": 64000},
            "pricing": {
                "prompt": "0.000005",
                "completion": "0.000025",
                "input_cache_read": "0.0000005",
                "input_cache_write": "0.00000625",
            },
            "architecture": {"input_modalities": ["text", "image"]},
            "supported_parameters": ["tools", "reasoning"],
        },
        {
            "id": "meta-llama/llama-3-8b",
            "context_length": 8192,
            "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
            "architecture": {"input_modalities": ["text"]},
            "supported_parameters": ["tools"],
        },
        {
            "id": "broken/no-context",
            "pricing": {"prompt": "0.000001"},
        },
        {
            "id": "weird/bad-pricing",
            "context_length": 32000,
            "pricing": {"prompt": "not-a-number", "completion": "0.0000002"},
            "architecture": {"input_modalities": ["text"]},
            "supported_parameters": [],
        },
    ]
}


class TestParse:
    def test_reasoning_vision_model_full_fields(self):
        out = parse_openrouter_models(FIXTURE)
        info = out["anthropic/claude-opus-4.8"]
        assert info.context_window == 200000
        assert info.max_output_tokens == 64000
        assert info.supports_vision is True
        assert info.supports_thinking is True
        assert info.supports_tools is True
        # USD/token strings → USD/mtok (exact floats)
        assert info.input_price_per_mtok == pytest.approx(5.0)
        assert info.output_price_per_mtok == pytest.approx(25.0)
        assert info.cache_read_price_per_mtok == pytest.approx(0.5)
        assert info.cache_write_price_per_mtok == pytest.approx(6.25)

    def test_non_reasoning_model(self):
        out = parse_openrouter_models(FIXTURE)
        info = out["meta-llama/llama-3-8b"]
        assert info.supports_vision is False
        assert info.supports_thinking is False
        assert info.context_window == 8192
        # no max_completion_tokens reported → floor bounded by window
        assert info.max_output_tokens == 8192

    def test_missing_context_length_skipped(self):
        out = parse_openrouter_models(FIXTURE)
        assert "broken/no-context" not in out

    def test_bad_pricing_field_becomes_zero_siblings_survive(self):
        out = parse_openrouter_models(FIXTURE)
        info = out["weird/bad-pricing"]
        assert info.input_price_per_mtok == 0.0
        assert info.output_price_per_mtok == pytest.approx(0.2)
        # empty supported_parameters → tools defaults True (not over-restricted)
        assert info.supports_tools is True

    def test_max_completion_floor_capped_at_window(self):
        # a tiny-window model with no reported completion cap → window, not 16384
        out = parse_openrouter_models(
            {"data": [{"id": "x/y", "context_length": 4000}]}
        )
        assert out["x/y"].max_output_tokens == 4000

    def test_empty_or_malformed_payload(self):
        assert parse_openrouter_models({}) == {}
        assert parse_openrouter_models({"data": "nope"}) == {}
        assert parse_openrouter_models({"data": []}) == {}
        assert parse_openrouter_models("not-a-dict") == {}  # type: ignore[arg-type]


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self._status = status_code
        self.calls = []

    async def get(self, url, headers=None):
        self.calls.append((url, headers or {}))
        return _FakeResponse(self._payload, self._status)


class TestFetch:
    async def test_fetch_hits_models_url_with_bearer(self):
        client = _FakeClient(FIXTURE)
        out = await fetch_openrouter_catalog(
            "https://openrouter.ai/api/v1", client=client, api_key="sk-abc"
        )
        assert "anthropic/claude-opus-4.8" in out
        url, headers = client.calls[0]
        assert url == "https://openrouter.ai/api/v1/models"
        assert headers.get("Authorization") == "Bearer sk-abc"

    async def test_fetch_no_key_omits_bearer(self):
        client = _FakeClient(FIXTURE)
        await fetch_openrouter_catalog("https://openrouter.ai/api/v1", client=client)
        _, headers = client.calls[0]
        assert "Authorization" not in headers

    async def test_fetch_http_error_raises(self):
        import httpx

        client = _FakeClient({}, status_code=500)
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_openrouter_catalog("https://openrouter.ai/api/v1", client=client)


class TestCache:
    def test_round_trip_within_ttl(self, tmp_path):
        path = cache_path(tmp_path)
        entries = parse_openrouter_models(FIXTURE)
        write_cache(path, entries, now_ts=1000.0)
        got = read_cache(path, ttl_hours=24.0, now_ts=1000.0 + 3600)
        assert got is not None
        assert got["anthropic/claude-opus-4.8"].context_window == 200000
        assert isinstance(got["anthropic/claude-opus-4.8"], ModelInfo)

    def test_past_ttl_returns_none(self, tmp_path):
        path = cache_path(tmp_path)
        write_cache(path, parse_openrouter_models(FIXTURE), now_ts=1000.0)
        # 25h later, ttl 24h
        assert read_cache(path, ttl_hours=24.0, now_ts=1000.0 + 25 * 3600) is None

    def test_any_age_ignores_ttl(self, tmp_path):
        path = cache_path(tmp_path)
        write_cache(path, parse_openrouter_models(FIXTURE), now_ts=1000.0)
        got = read_cache_any_age(path)
        assert got is not None
        assert "anthropic/claude-opus-4.8" in got

    def test_missing_file(self, tmp_path):
        path = cache_path(tmp_path)
        assert read_cache(path, ttl_hours=24.0, now_ts=1000.0) is None
        assert read_cache_any_age(path) is None

    def test_corrupt_file_no_raise(self, tmp_path):
        path = cache_path(tmp_path)
        path.write_text("{not valid json")
        assert read_cache(path, ttl_hours=24.0, now_ts=1000.0) is None
        assert read_cache_any_age(path) is None

    def test_cache_stores_parsed_fields_not_raw(self, tmp_path):
        path = cache_path(tmp_path)
        write_cache(path, parse_openrouter_models(FIXTURE), now_ts=1000.0)
        raw = json.loads(path.read_text())
        assert set(raw.keys()) == {"fetched_at", "models"}
        model = raw["models"]["anthropic/claude-opus-4.8"]
        # ModelInfo field names, not OpenRouter's (context_window, not context_length)
        assert "context_window" in model
        assert "context_length" not in model

    def test_write_error_swallowed(self, tmp_path):
        # a path whose parent is a file → mkdir/write fails, must not raise
        bad = tmp_path / "afile"
        bad.write_text("x")
        write_cache(bad / "sub" / "cache.json", {}, now_ts=1000.0)
