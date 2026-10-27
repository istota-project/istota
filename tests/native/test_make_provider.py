"""The provider factory: `make_provider(spec)` over a `ProviderSpec`.

What an endpoint accepts comes from the provider's kind (`KIND_DEFAULTS`) and
its explicit settings, never from a base-URL substring.
"""

import pytest

from istota.llm import make_provider
from istota.llm.openai_compat import OpenAICompatibleProvider
from istota.llm.providers import InferenceKey, ProviderSpec, resolve_compat


def _spec(kind="openai_compat", base_url="https://x.example/v1", key="k", headers=None, **settings):
    return ProviderSpec(
        id="p",
        kind=kind,
        base_url=base_url,
        inference_key=InferenceKey(key),
        extra_headers=headers or {},
        compat=resolve_compat(kind, base_url, settings),
    )


def test_a_spec_builds_the_openai_compatible_provider():
    assert isinstance(make_provider(_spec()), OpenAICompatibleProvider)


def test_anything_but_a_spec_is_refused():
    from types import SimpleNamespace

    with pytest.raises(TypeError, match="ProviderSpec"):
        make_provider(SimpleNamespace(provider="openai_compat", api_key="k", base_url="https://x/v1"))


def test_a_plain_string_key_is_refused():
    spec = ProviderSpec(id="p", kind="openai_compat", base_url="https://x/v1", inference_key="k")
    with pytest.raises(TypeError, match="InferenceKey"):
        make_provider(spec)


class TestThePathRule:
    def test_an_anthropic_provider_gets_v1_added(self):
        provider = make_provider(_spec("anthropic", "https://api.anthropic.com"))
        assert str(provider._client.base_url).rstrip("/") == "https://api.anthropic.com/v1"

    def test_other_kinds_keep_their_base_url(self):
        provider = make_provider(_spec("openrouter", "https://openrouter.ai/api/v1"))
        assert str(provider._client.base_url).rstrip("/") == "https://openrouter.ai/api/v1"


class TestCachingDefaultResolution:
    def test_the_anthropic_kind_defaults_caching_on(self):
        assert make_provider(_spec("anthropic", "https://gateway.example"))._prompt_caching is True

    def test_other_kinds_default_caching_off(self):
        assert make_provider(_spec("openai_compat", "http://localhost:11434/v1"))._prompt_caching is False
        assert make_provider(_spec("openrouter", "https://openrouter.ai/api/v1"))._prompt_caching is False

    def test_explicit_false_honored_on_anthropic(self):
        spec = _spec("anthropic", "https://api.anthropic.com", prompt_caching=False)
        assert make_provider(spec)._prompt_caching is False

    def test_explicit_true_honored_on_local(self):
        spec = _spec("openai_compat", "http://localhost:11434/v1", prompt_caching=True)
        assert make_provider(spec)._prompt_caching is True


class TestOpenRouterAppAttribution:
    def test_openrouter_injects_attribution_headers(self):
        headers = make_provider(_spec("openrouter", "https://openrouter.ai/api/v1"))._client.headers
        assert headers["HTTP-Referer"] == "https://istota.cynium.com"
        assert headers["X-Title"] == "Istota"

    def test_other_kinds_get_no_attribution(self):
        headers = make_provider(_spec("anthropic", "https://api.anthropic.com"))._client.headers
        assert "HTTP-Referer" not in headers
        assert "X-Title" not in headers

    def test_an_openai_compat_provider_on_openrouters_host_gets_none(self):
        # The kind decides, not the host.
        headers = make_provider(_spec("openai_compat", "https://openrouter.ai/api/v1"))._client.headers
        assert "X-Title" not in headers

    def test_operator_extra_headers_override_attribution(self):
        spec = _spec(
            "openrouter", "https://openrouter.ai/api/v1",
            # Case-insensitive: operator's own casing wins, defaults not added.
            headers={"http-referer": "https://example.com", "X-Title": "Custom"},
        )
        headers = make_provider(spec)._client.headers
        assert headers["HTTP-Referer"] == "https://example.com"
        assert headers["X-Title"] == "Custom"


class TestPromptCacheKeyResolution:
    """Unset means on for OpenRouter and OpenAI's own host, off elsewhere,
    including Anthropic; an explicit value always wins."""

    @pytest.mark.parametrize(
        "kind, base_url, setting, expected",
        [
            ("openrouter", "https://openrouter.ai/api/v1", None, True),
            ("openai_compat", "https://api.openai.com/v1", None, True),
            ("anthropic", "https://api.anthropic.com", None, False),
            ("openai_compat", "http://localhost:11434/v1", None, False),
            ("anthropic", "https://api.anthropic.com", True, True),
            ("openai_compat", "http://localhost:11434/v1", True, True),
            ("openrouter", "https://openrouter.ai/api/v1", False, False),
            ("openai_compat", "https://api.openai.com/v1", False, False),
        ],
    )
    def test_matrix(self, kind, base_url, setting, expected):
        assert make_provider(_spec(kind, base_url, prompt_cache_key=setting)).prompt_cache_key is expected


class TestPromptCacheTtl:
    def test_defaults_to_5m(self):
        assert make_provider(_spec("anthropic", "https://api.anthropic.com")).prompt_cache_ttl == "5m"

    def test_passes_1h_through(self):
        spec = _spec("anthropic", "https://api.anthropic.com", prompt_cache_ttl="1h")
        assert make_provider(spec).prompt_cache_ttl == "1h"

    def test_an_unknown_value_runs_as_5m(self):
        spec = _spec("anthropic", "https://api.anthropic.com", prompt_cache_ttl="1d")
        assert make_provider(spec).prompt_cache_ttl == "5m"
