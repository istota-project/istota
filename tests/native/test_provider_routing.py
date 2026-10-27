"""Native picks its provider per request (D4 of the model-catalogue spec),
among the providers `[brain.native] providers` lists.

A ref of a listed provider goes to that provider, with that provider's record
and key and its own id on the wire. Everything else still goes to
`[brain.native] provider`: its own refs, an id it lists that reads like
another provider's ref, a ref of a provider not on the list (whatever keys
the deployment holds, cached list or not), a model the catalogue does not
hold.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from istota.brain import make_brain
from istota.config import config_from_document
from istota.llm.catalog import ModelInfo, set_discovered
from istota.llm.providers import InferenceKey, brain_can_run
from istota.llm.types import AssistantMessage, TextContent, Usage

from ._mock_provider import MockProvider


def _cfg(*, openrouter_key="or-key", native_provider="anthropic", providers=("openrouter", "local")):
    return config_from_document({
        "brain": {"kind": "native", "native": {
            "provider": native_provider, "providers": list(providers), "model": "claude-x",
        }},
        "providers": {
            "anthropic": {"api_key": "anthropic-key"},
            "openrouter": {"api_key": openrouter_key},
            "local": {"kind": "openai_compat", "base_url": "http://192.0.2.10:8080/v1"},
        },
    })


@pytest.fixture(autouse=True)
def _discovered():
    set_discovered({
        "openrouter": [
            ModelInfo(
                id="moonshotai/kimi-k2.6", context_window=200000, max_output_tokens=8000,
                provider="openrouter", reasoning=True,
            ),
            ModelInfo(
                id="anthropic/claude-opus-5", context_window=200000, max_output_tokens=8000,
                provider="openrouter",
            ),
        ],
        "local": [
            ModelInfo(id="qwen", context_window=32000, max_output_tokens=4000, provider="local"),
        ],
    })
    yield
    set_discovered(None)


def _req(tmp_path, model, effort=""):
    from istota.brain import BrainRequest

    return BrainRequest(
        prompt="hi", allowed_tools=[], cwd=str(tmp_path), env={},
        timeout_seconds=30, model=model, effort=effort,
    )


def _reply():
    return AssistantMessage(
        content=[TextContent(text="done")], stop_reason="stop", usage=Usage(),
    )


class _Built:
    """Stands in for `make_provider`: records each spec, hands back a mock."""

    def __init__(self):
        self.specs = []
        self.providers = []

    def __call__(self, spec):
        self.specs.append(spec)
        provider = MockProvider([_reply()])
        self.providers.append(provider)
        return provider


def _run(config, tmp_path, model, effort=""):
    built = _Built()
    with patch("istota.brain.native.make_provider", built):
        result = make_brain(config.brain).execute(_req(tmp_path, model, effort))
    return built, result


class TestRouting:
    def test_a_listed_providers_ref_goes_to_that_provider_with_its_key(self, tmp_path):
        built, result = _run(_cfg(), tmp_path, "openrouter/moonshotai/kimi-k2.6", "high")
        assert result.success, result.result_text
        # The brain's own provider is built, then the routed one runs.
        routed = built.specs[-1]
        assert routed.id == "openrouter"
        assert routed.inference_key == InferenceKey("or-key")
        call = built.providers[-1].calls[0]
        assert call["model"] == "moonshotai/kimi-k2.6"
        assert call["reasoning_effort"] == "high"
        assert built.providers[0].calls == []

    def test_its_own_ref_stays_on_native_s_provider(self, tmp_path):
        config = _cfg(native_provider="openrouter")
        built, _result = _run(config, tmp_path, "openrouter/moonshotai/kimi-k2.6")
        assert [s.id for s in built.specs] == ["openrouter"]
        assert built.providers[0].calls[0]["model"] == "moonshotai/kimi-k2.6"

    def test_an_id_its_own_provider_lists_is_not_read_as_another_ref(self, tmp_path):
        # OpenRouter's own id `anthropic/claude-opus-5` spells an anthropic ref.
        config = _cfg(native_provider="openrouter", providers=("anthropic",))
        built, _result = _run(config, tmp_path, "anthropic/claude-opus-5")
        assert [s.id for s in built.specs] == ["openrouter"]
        assert built.providers[0].calls[0]["model"] == "anthropic/claude-opus-5"

    def test_a_provider_off_the_list_is_not_routed_to_whatever_its_key(self, tmp_path):
        built, _result = _run(_cfg(providers=()), tmp_path, "openrouter/moonshotai/kimi-k2.6")
        assert [s.id for s in built.specs] == ["anthropic"]
        # Sent as written to native's own provider, as before routing existed.
        assert built.providers[0].calls[0]["model"] == "openrouter/moonshotai/kimi-k2.6"

    def test_with_no_discovery_cache_an_anthropic_spelling_stays_on_openrouter(self, tmp_path):
        # The billing hole: an OpenRouter-native room's `anthropic/...` value,
        # with no OpenRouter list on disk (or `discover = false`) and an
        # `anthropic` key held for the CLI. Not on the list, never Anthropic's.
        set_discovered(None)
        config = _cfg(native_provider="openrouter", providers=())
        built, result = _run(config, tmp_path, "anthropic/claude-opus-5")
        assert result.success, result.result_text
        assert [s.id for s in built.specs] == ["openrouter"]
        assert built.specs[0].inference_key == InferenceKey("or-key")
        assert built.providers[0].calls[0]["model"] == "anthropic/claude-opus-5"

    def test_with_anthropic_listed_beside_openrouter_the_default_s_reading_wins(self, tmp_path):
        # Ambiguous until the provider is stored apart from the id (doctor
        # warns); read as OpenRouter's, cached list or not.
        set_discovered(None)
        config = _cfg(native_provider="openrouter", providers=("anthropic",))
        built, _result = _run(config, tmp_path, "anthropic/claude-opus-5")
        assert [s.id for s in built.specs] == ["openrouter"]

    def test_a_listed_operator_provider_is_routed_to(self, tmp_path):
        built, _result = _run(_cfg(), tmp_path, "local/qwen")
        assert built.specs[-1].id == "local"
        assert built.providers[-1].calls[0]["model"] == "qwen"

    def test_a_model_the_catalogue_does_not_hold_is_not_routed(self, tmp_path):
        built, _result = _run(_cfg(), tmp_path, "openrouter/nobody/unknown")
        assert [s.id for s in built.specs] == ["anthropic"]


class TestBrainCanRun:
    def test_native_runs_the_listed_providers_only(self):
        providers = _cfg().providers
        reach = ("anthropic", "openrouter")
        assert brain_can_run("native", "anthropic/x", providers, native_provider="anthropic")
        assert brain_can_run(
            "native", "openrouter/x", providers, native_provider="anthropic", native_reach=reach,
        )
        # A key is not a listing; nor is being an `openai_compat` provider.
        assert not brain_can_run("native", "openrouter/x", providers, native_provider="anthropic")
        assert not brain_can_run("native", "local/qwen", providers, native_provider="anthropic")
        assert not brain_can_run(
            "native", "anthropic/x", providers, native_provider="openrouter",
            native_reach=("openrouter",),
        )

    def test_its_own_provider_needs_no_key(self):
        config = _cfg(openrouter_key="", native_provider="openrouter")
        assert brain_can_run("native", "openrouter/x", config.providers, native_provider="openrouter")

    def test_native_with_no_provider_runs_nothing(self):
        assert not brain_can_run(
            "native", "anthropic/x", _cfg().providers, native_provider=None,
            native_reach=("anthropic",),
        )

    def test_the_cli_brains_run_only_anthropic(self):
        providers = _cfg().providers
        for kind in ("claude_code", "tmux_claude"):
            assert brain_can_run(kind, "anthropic/x", providers)
            assert not brain_can_run(kind, "openrouter/x", providers)
