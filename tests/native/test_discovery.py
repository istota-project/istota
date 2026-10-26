"""Model discovery: the three parsers, the per-provider caches, the refresh and
the install into the catalogue (model-catalogue spec, Stage 4)."""

import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from istota.config import Config
from istota.llm import catalog, discovery
from istota.llm.catalog import ModelInfo, get_model_info
from istota.llm.discovery import (
    ANTHROPIC_MODELS_URL,
    LAST_SEEN_WINDOW_SECONDS,
    fetch_anthropic_models,
    fetch_openrouter_catalog,
    merge_anthropic,
    parse_anthropic_models,
    parse_openai_models,
    parse_openrouter_models,
)
from istota.llm.providers import InferenceKey, ModelListingKey, ProviderSpec

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "model_catalogue"
PAGE1 = json.loads((FIXTURES / "anthropic_models_page1.json").read_text())
PAGE2 = json.loads((FIXTURES / "anthropic_models_page2.json").read_text())

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
            "supported_parameters": ["tools", "reasoning", "structured_outputs"],
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

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _clean_install():
    discovery.reset_install_state()
    catalog.set_discovered({})
    yield
    discovery.reset_install_state()
    catalog.set_discovered({})


class _Stub:
    """A transport that answers from a queue and records every request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer[0], answer[1]
        headers_out = answer[2] if len(answer) > 2 else {}
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        return status, body, headers_out


def _openrouter(**changes) -> ProviderSpec:
    spec = ProviderSpec(id="openrouter", kind="openrouter", base_url="https://openrouter.ai/api/v1")
    return replace(spec, **changes)


def _anthropic(**changes) -> ProviderSpec:
    spec = ProviderSpec(id="anthropic", kind="anthropic", base_url="https://api.anthropic.com")
    return replace(spec, **changes)


def _key_file(tmp_path, value="LISTING-SENTINEL") -> Path:
    path = tmp_path / "listing-key"
    path.write_text(value)
    os.chmod(path, 0o600)
    return path


# --- parsers ------------------------------------------------------------------


class TestParseOpenRouter:
    def test_reasoning_vision_model_full_fields(self):
        info = parse_openrouter_models(FIXTURE)["anthropic/claude-opus-4.8"]
        assert info.context_window == 200000
        assert info.max_output_tokens == 64000
        assert info.supports_vision is True
        assert info.supports_thinking is True
        assert info.supports_tools is True
        assert info.supports_structured_outputs is True
        assert info.input_price_per_mtok == pytest.approx(5.0)
        assert info.output_price_per_mtok == pytest.approx(25.0)
        assert info.cache_read_price_per_mtok == pytest.approx(0.5)
        assert info.cache_write_price_per_mtok == pytest.approx(6.25)
        assert info.price_source == "discovered"

    def test_non_reasoning_model(self):
        info = parse_openrouter_models(FIXTURE)["meta-llama/llama-3-8b"]
        assert info.supports_vision is False
        assert info.supports_thinking is False
        assert info.context_window == 8192
        assert info.max_output_tokens == 8192

    def test_missing_context_length_skipped(self):
        assert "broken/no-context" not in parse_openrouter_models(FIXTURE)

    def test_bad_pricing_field_becomes_zero_siblings_survive(self):
        info = parse_openrouter_models(FIXTURE)["weird/bad-pricing"]
        assert info.input_price_per_mtok == 0.0
        assert info.output_price_per_mtok == pytest.approx(0.2)
        assert info.supports_tools is True

    def test_a_listed_model_without_tools_is_marked_so(self):
        payload = {"data": [{"id": "x/notools", "context_length": 4000, "supported_parameters": ["temperature"]}]}
        assert parse_openrouter_models(payload)["x/notools"].supports_tools is False

    def test_empty_or_malformed_payload(self):
        assert parse_openrouter_models({}) == {}
        assert parse_openrouter_models({"data": "nope"}) == {}
        assert parse_openrouter_models("not-a-dict") == {}  # type: ignore[arg-type]


class TestParseOpenAI:
    def test_ids_only_on_the_conservative_defaults(self):
        out = parse_openai_models({"object": "list", "data": [{"id": "qwen3-32b", "object": "model"}, {"id": ""}, "x"]})
        assert list(out) == ["qwen3-32b"]
        info = out["qwen3-32b"]
        assert info.context_window == 200_000
        assert info.price_source == "unknown"
        assert info.input_price_per_mtok == 0.0


class TestParseAnthropic:
    def test_the_stated_fields_and_nothing_else(self):
        out = parse_anthropic_models(PAGE1)
        sonnet = out["claude-sonnet-6"]
        assert sonnet["name"] == "Claude Sonnet 6"
        assert sonnet["family"] == "sonnet"
        assert sonnet["released"] == "2026-10-20"
        assert sonnet["deprecated"] is False
        assert sonnet["context_window"] == 1_000_000
        assert sonnet["max_output_tokens"] == 128000
        assert sonnet["input"] == ["text", "image"]
        assert sonnet["reasoning"] is True
        # A null field is absent, so the shipped value survives the merge.
        assert "max_output_tokens" not in out["claude-opus-5-5"]

    def test_lifecycle_epoch_and_a_missing_line(self):
        out = parse_anthropic_models(PAGE2)
        assert out["claude-opus-4-5"]["deprecated"] is True
        mystery = out["claude-mystery-1"]
        assert "released" not in mystery  # an epoch created_at is unknown
        assert "family" not in mystery  # never parsed out of the id
        assert mystery["input"] == ["text"]


class TestMergeAnthropic:
    def _merged(self):
        stated = {**parse_anthropic_models(PAGE1), **parse_anthropic_models(PAGE2)}
        return {m.id: m for m in merge_anthropic(stated, catalog.shipped_anthropic_models())}

    def test_the_api_wins_for_what_it_states_and_shipped_prices_stay(self):
        shipped = {m.id: m for m in catalog.shipped_anthropic_models()}["claude-opus-5-5"]
        opus = self._merged()["claude-opus-5-5"]
        assert opus.context_window == 900_000
        assert opus.name == "Claude Opus 5.5 (API name)"
        assert opus.max_output_tokens == shipped.max_output_tokens
        assert opus.input_price_per_mtok == shipped.input_price_per_mtok
        assert opus.price_source == "shipped"
        assert opus.effort_map == {"low": None, "xhigh": "xhigh", "max": None}

    def test_a_model_only_the_api_lists_is_priced_unknown(self):
        sonnet = self._merged()["claude-sonnet-6"]
        assert sonnet.price_source == "unknown"
        assert sonnet.input_price_per_mtok == 0.0
        assert sonnet.family == "sonnet"
        assert sonnet.effort_map == {"xhigh": "xhigh", "max": "max"}

    def test_lifecycle_marks_deprecated(self):
        assert self._merged()["claude-opus-4-5"].deprecated is True


# --- the Anthropic fetch --------------------------------------------------------


class TestFetchAnthropic:
    def test_pages_with_after_id_to_the_fixed_url_with_the_listing_key(self):
        stub = _Stub((200, PAGE1), (200, PAGE2))
        out = fetch_anthropic_models(ModelListingKey("LISTING-SENTINEL"), transport=stub)
        assert set(out) == {"claude-sonnet-6", "claude-opus-5-5", "claude-opus-4-5", "claude-mystery-1"}
        assert [url for url, _ in stub.calls] == [
            f"{ANTHROPIC_MODELS_URL}?limit=1000",
            f"{ANTHROPIC_MODELS_URL}?limit=1000&after_id=claude-opus-5-5",
        ]
        headers = stub.calls[0][1]
        assert headers == {
            "x-api-key": "LISTING-SENTINEL",
            "anthropic-version": discovery.ANTHROPIC_VERSION,
            "Accept": "application/json",
        }

    def test_an_inference_key_is_refused(self):
        with pytest.raises(TypeError):
            fetch_anthropic_models(InferenceKey("k"), transport=_Stub((200, PAGE2)))  # type: ignore[arg-type]

    def test_a_status_is_reported_without_the_body(self):
        stub = _Stub((401, b'{"error": "LISTING-SENTINEL is invalid"}'))
        with pytest.raises(discovery.ListingFailed) as caught:
            fetch_anthropic_models(ModelListingKey("LISTING-SENTINEL"), transport=stub)
        assert caught.value.status == 401
        assert "SENTINEL" not in str(caught.value)

    def test_a_transport_failure_is_its_class_name(self):
        stub = _Stub(OSError("connect to LISTING-SENTINEL failed"))
        with pytest.raises(discovery.ListingFailed) as caught:
            fetch_anthropic_models(ModelListingKey("LISTING-SENTINEL"), transport=stub)
        assert str(caught.value) == "OSError"


# --- the OpenRouter fetch the setup server uses -----------------------------------


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


class TestSetupFetch:
    async def test_fetch_hits_models_url_with_bearer(self):
        client = _FakeClient(FIXTURE)
        out = await fetch_openrouter_catalog("https://openrouter.ai/api/v1", client=client, api_key="sk-abc")
        assert "anthropic/claude-opus-4.8" in out
        url, headers = client.calls[0]
        assert url == "https://openrouter.ai/api/v1/models"
        assert headers.get("Authorization") == "Bearer sk-abc"

    async def test_fetch_http_error_raises(self):
        import httpx

        with pytest.raises(httpx.HTTPStatusError):
            await fetch_openrouter_catalog("https://openrouter.ai/api/v1", client=_FakeClient({}, 500))


# --- refresh and cache ------------------------------------------------------------


class TestRefreshOpenRouter:
    def test_a_fetch_writes_the_cache_and_sends_the_providers_own_key(self, tmp_path):
        stub = _Stub((200, FIXTURE, {"ETag": '"v1"'}))
        spec = _openrouter(inference_key=InferenceKey("OR-KEY"))
        result = discovery.refresh_provider(spec, tmp_path, NOW, transport=stub)
        assert result.outcome == "refreshed"
        assert stub.calls[0][0] == "https://openrouter.ai/api/v1/models"
        assert stub.calls[0][1]["Authorization"] == "Bearer OR-KEY"
        doc = discovery.read_cache(tmp_path, "openrouter")
        assert doc["etag"] == '"v1"'
        assert doc["fetched_at"] == NOW
        assert doc["models"]["anthropic/claude-opus-4.8"]["last_seen"] == NOW
        assert (tmp_path / "openrouter.json").stat().st_mode & 0o777 == 0o600

    def test_etag_round_trip_with_a_304(self, tmp_path):
        discovery.refresh_provider(_openrouter(), tmp_path, NOW, transport=_Stub((200, FIXTURE, {"etag": '"v1"'})))
        stub = _Stub((304, b""))
        result = discovery.refresh_provider(_openrouter(), tmp_path, NOW + 100, transport=stub)
        assert result.outcome == "not_modified"
        assert stub.calls[0][1]["If-None-Match"] == '"v1"'
        doc = discovery.read_cache(tmp_path, "openrouter")
        assert doc["fetched_at"] == NOW + 100
        assert "anthropic/claude-opus-4.8" in doc["models"]

    def test_an_empty_200_keeps_the_old_cache(self, tmp_path):
        discovery.refresh_provider(_openrouter(), tmp_path, NOW, transport=_Stub((200, FIXTURE)))
        result = discovery.refresh_provider(_openrouter(), tmp_path, NOW + 100, transport=_Stub((200, {"data": []})))
        assert result.outcome == "failed"
        doc = discovery.read_cache(tmp_path, "openrouter")
        assert doc["fetched_at"] == NOW
        assert "anthropic/claude-opus-4.8" in doc["models"]

    def test_a_401_records_the_status_and_keeps_the_models(self, tmp_path):
        discovery.refresh_provider(_openrouter(), tmp_path, NOW, transport=_Stub((200, FIXTURE)))
        result = discovery.refresh_provider(_openrouter(), tmp_path, NOW + 100, transport=_Stub((401, b"nope")))
        assert result.outcome == "failed"
        doc = discovery.read_cache(tmp_path, "openrouter")
        assert doc["last_status"] == 401
        assert doc["last_error"] == "HTTP 401"
        assert "anthropic/claude-opus-4.8" in doc["models"]

    def test_a_model_gone_from_the_list_lasts_seven_days(self, tmp_path):
        discovery.refresh_provider(_openrouter(), tmp_path, NOW, transport=_Stub((200, FIXTURE)))
        only_one = {"data": [FIXTURE["data"][1]]}
        later = NOW + LAST_SEEN_WINDOW_SECONDS - 60
        discovery.refresh_provider(_openrouter(), tmp_path, later, transport=_Stub((200, only_one)))
        ids = {m.id for m in discovery.load_discovered(tmp_path)["openrouter"]}
        assert "anthropic/claude-opus-4.8" in ids
        past = NOW + LAST_SEEN_WINDOW_SECONDS + 60
        discovery.refresh_provider(_openrouter(), tmp_path, past, transport=_Stub((200, only_one)))
        ids = {m.id for m in discovery.load_discovered(tmp_path)["openrouter"]}
        assert ids == {"meta-llama/llama-3-8b"}

    def test_a_provider_that_is_down_loses_nothing(self, tmp_path):
        discovery.refresh_provider(_openrouter(), tmp_path, NOW, transport=_Stub((200, FIXTURE)))
        discovery.refresh_provider(
            _openrouter(), tmp_path, NOW + 30 * 86400, transport=_Stub(OSError("down")),
        )
        ids = {m.id for m in discovery.load_discovered(tmp_path)["openrouter"]}
        assert "anthropic/claude-opus-4.8" in ids

    def test_an_openai_compat_provider_uses_the_plain_parser(self, tmp_path):
        spec = ProviderSpec(id="local", kind="openai_compat", base_url="http://192.0.2.10:8080/v1")
        stub = _Stub((200, {"data": [{"id": "qwen3-32b"}]}))
        discovery.refresh_provider(spec, tmp_path, NOW, transport=stub)
        assert stub.calls[0][0] == "http://192.0.2.10:8080/v1/models"
        assert [m.ref for m in discovery.load_discovered(tmp_path)["local"]] == ["local/qwen3-32b"]

    def test_the_old_single_file_cache_is_read_as_the_initial_one(self, tmp_path):
        cache_dir = tmp_path / discovery.CACHE_DIRNAME
        legacy = {"fetched_at": NOW, "models": {
            mid: discovery._record_json(info) for mid, info in parse_openrouter_models(FIXTURE).items()
        }}
        (tmp_path / discovery.LEGACY_OPENROUTER_CACHE).write_text(json.dumps(legacy))
        layers = discovery.load_discovered(cache_dir)
        assert any(m.id == "anthropic/claude-opus-4.8" for m in layers["openrouter"])
        assert not discovery.is_due(_openrouter(), cache_dir, NOW + 60)


class TestRefreshAnthropic:
    def test_it_uses_the_listing_key_file_and_nothing_else(self, tmp_path):
        stub = _Stub((200, PAGE1), (200, PAGE2))
        spec = _anthropic(inference_key=InferenceKey("INFERENCE-SENTINEL"))
        result = discovery.refresh_provider(
            spec, tmp_path, NOW, listing_key_path=_key_file(tmp_path), transport=stub,
        )
        assert result.outcome == "refreshed"
        for _url, headers in stub.calls:
            assert headers["x-api-key"] == "LISTING-SENTINEL"
            assert "INFERENCE-SENTINEL" not in json.dumps(headers)
        assert "LISTING-SENTINEL" not in (tmp_path / "anthropic.json").read_text()

    def test_an_inference_key_alone_leaves_it_off(self, tmp_path):
        stub = _Stub((200, PAGE1))
        spec = _anthropic(inference_key=InferenceKey("INFERENCE-SENTINEL"))
        result = discovery.refresh_provider(
            spec, tmp_path, NOW, listing_key_path=tmp_path / "missing", transport=stub,
        )
        assert result.outcome == "skipped"
        assert stub.calls == []
        empty = tmp_path / "empty"
        empty.write_text("\n")
        assert discovery.refresh_provider(spec, tmp_path, NOW, listing_key_path=empty, transport=stub).outcome == "skipped"
        assert stub.calls == []

    def test_a_listing_key_file_that_is_a_link_is_not_followed(self, tmp_path):
        target = _key_file(tmp_path)
        link = tmp_path / "link"
        link.symlink_to(target)
        stub = _Stub((200, PAGE2))
        assert discovery.refresh_provider(
            _anthropic(), tmp_path, NOW, listing_key_path=link, transport=stub,
        ).outcome == "skipped"

    def test_a_403_is_recorded_on_the_cache(self, tmp_path):
        result = discovery.refresh_provider(
            _anthropic(), tmp_path, NOW, listing_key_path=_key_file(tmp_path), transport=_Stub((403, b"{}")),
        )
        assert result.outcome == "failed"
        assert discovery.read_cache(tmp_path, "anthropic")["last_status"] == 403

    def test_another_anthropic_kind_provider_lists_nothing(self, tmp_path):
        stub = _Stub((200, PAGE1))
        spec = ProviderSpec(id="native", kind="anthropic", base_url="https://api.anthropic.com")
        result = discovery.refresh_provider(
            spec, tmp_path, NOW, listing_key_path=_key_file(tmp_path), transport=stub,
        )
        assert result.outcome == "skipped"
        assert stub.calls == []

    def test_the_merged_records_reach_the_catalogue(self, tmp_path):
        discovery.refresh_provider(
            _anthropic(), tmp_path, NOW, listing_key_path=_key_file(tmp_path),
            transport=_Stub((200, PAGE1), (200, PAGE2)),
        )
        assert discovery.install_from_cache_dir(tmp_path) is True
        current = catalog.current_catalogue()
        assert current.latest("sonnet").id == "claude-sonnet-6"
        assert current.get("anthropic/claude-opus-5-5").context_window == 900_000
        assert current.get("anthropic/claude-opus-4-5").deprecated is True
        # Shipped models the API did not list keep their records.
        assert current.get("anthropic/claude-haiku-5-5").price_source == "shipped"
        # Reached by ref only, as the shipped file is.
        assert get_model_info("claude-sonnet-6").context_window == 200_000


class TestInstall:
    def test_it_installs_once_per_change(self, tmp_path):
        discovery.refresh_provider(_openrouter(), tmp_path, NOW, transport=_Stub((200, FIXTURE)))
        assert discovery.install_from_cache_dir(tmp_path) is True
        assert discovery.install_from_cache_dir(tmp_path) is False
        assert get_model_info("anthropic/claude-opus-4.8").context_window == 200000
        assert get_model_info("openrouter/anthropic/claude-opus-4.8").context_window == 200000

    def test_a_corrupt_cache_installs_nothing_and_does_not_raise(self, tmp_path):
        (tmp_path / "openrouter.json").write_text("{not json")
        discovery.install_from_cache_dir(tmp_path)
        assert catalog.current_catalogue().discovered == {}


def _config(tmp_path, *, native_provider="openrouter") -> Config:
    config = Config()
    config.db_path = tmp_path / "istota.db"
    config.brain.native.provider = native_provider
    return config


class TestWhoIsFetched:
    def test_an_unused_builtin_openrouter_is_not_fetched(self, tmp_path):
        stub = _Stub((200, FIXTURE))
        results = discovery.refresh_all(_config(tmp_path, native_provider=""), NOW, transport=stub)
        assert stub.calls == []
        assert [r.provider for r in results] == ["anthropic"]

    def test_a_deployment_key_or_natives_choice_makes_it_used(self, tmp_path):
        config = _config(tmp_path, native_provider="")
        spec = _openrouter()
        assert discovery.wants_discovery(spec, config) is False
        assert discovery.wants_discovery(replace(spec, inference_key=InferenceKey("k")), config) is True
        config.brain.native.provider = "openrouter"
        assert discovery.wants_discovery(spec, config) is True
        assert discovery.wants_discovery(replace(spec, discover=False), config) is False

    def test_an_operators_own_provider_is_fetched(self, tmp_path):
        config = _config(tmp_path, native_provider="")
        local = ProviderSpec(id="local", kind="openai_compat", base_url="http://192.0.2.10:8080/v1")
        assert discovery.wants_discovery(local, config) is True


class TestTheGateAndTheBoot:
    def test_refresh_all_fetches_only_what_is_due(self, tmp_path):
        config = _config(tmp_path)
        stub = _Stub((200, FIXTURE))
        results = discovery.refresh_all(config, NOW, transport=stub)
        assert [(r.provider, r.outcome) for r in results] == [
            ("anthropic", "skipped"), ("openrouter", "refreshed"),
        ]
        assert len(stub.calls) == 1
        # Inside the TTL nothing is fetched again.
        discovery.refresh_all(config, NOW + 3600, transport=stub)
        assert len(stub.calls) == 1
        assert get_model_info("anthropic/claude-opus-4.8").context_window == 200000

    def test_boot_refresh_is_bounded_by_one_budget(self, tmp_path):
        config = _config(tmp_path)
        release = threading.Event()

        def slow(url, headers, timeout):
            release.wait(5)
            return 200, json.dumps(FIXTURE).encode(), {}

        started = time.monotonic()
        discovery.boot_refresh(config, budget=0.2, transport=slow)
        assert time.monotonic() - started < 2
        release.set()

    def test_boot_refresh_skips_a_provider_with_a_cache(self, tmp_path):
        config = _config(tmp_path)
        cache_dir = discovery.config_cache_dir(config)
        discovery.refresh_provider(_openrouter(), cache_dir, NOW, transport=_Stub((200, FIXTURE)))
        stub = _Stub((200, FIXTURE))
        discovery.boot_refresh(config, transport=stub)
        assert stub.calls == []
        assert get_model_info("anthropic/claude-opus-4.8").context_window == 200000


def test_a_model_info_round_trips_through_the_cache(tmp_path):
    info = ModelInfo(id="x/y", context_window=1000, max_output_tokens=10, supports_vision=True,
                     effort_map={"low": None}, price_tiers=[{"input_tokens_above": 5, "input_price_per_mtok": 1.0}])
    raw = json.loads(json.dumps(discovery._record_json(info)))
    back = catalog.model_info_from_dict(raw)
    assert back == info
