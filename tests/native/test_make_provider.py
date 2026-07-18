"""Phase 1 — provider factory (duck-typed config)."""

from types import SimpleNamespace

import pytest

from istota.llm import make_provider
from istota.llm.openai_compat import OpenAICompatibleProvider


def test_openai_compat_selected():
    cfg = SimpleNamespace(
        provider="openai_compat", api_key="k", base_url="https://x/v1", extra_headers={}
    )
    assert isinstance(make_provider(cfg), OpenAICompatibleProvider)


def test_defaults_to_openai_compat_when_unset():
    cfg = SimpleNamespace(api_key="k")
    assert isinstance(make_provider(cfg), OpenAICompatibleProvider)


def test_unknown_provider_raises():
    cfg = SimpleNamespace(provider="quantum")
    with pytest.raises(ValueError, match="Unknown provider"):
        make_provider(cfg)


class TestCachingDefaultResolution:
    def test_anthropic_base_url_defaults_caching_on(self):
        cfg = SimpleNamespace(
            api_key="k",
            base_url="https://api.anthropic.com/v1",
            prompt_caching=None,
        )
        assert make_provider(cfg)._prompt_caching is True

    def test_non_anthropic_base_url_defaults_caching_off(self):
        cfg = SimpleNamespace(
            api_key="k",
            base_url="http://localhost:11434/v1",
            prompt_caching=None,
        )
        assert make_provider(cfg)._prompt_caching is False

    def test_missing_attr_defaults_from_base_url(self):
        # A config object that never set the field at all behaves like None.
        cfg = SimpleNamespace(api_key="k", base_url="https://api.anthropic.com/v1")
        assert make_provider(cfg)._prompt_caching is True

    def test_explicit_false_honored_on_anthropic(self):
        cfg = SimpleNamespace(
            api_key="k",
            base_url="https://api.anthropic.com/v1",
            prompt_caching=False,
        )
        assert make_provider(cfg)._prompt_caching is False

    def test_explicit_true_honored_on_local(self):
        cfg = SimpleNamespace(
            api_key="k",
            base_url="http://localhost:11434/v1",
            prompt_caching=True,
        )
        assert make_provider(cfg)._prompt_caching is True


class TestOpenRouterAppAttribution:
    def test_openrouter_injects_attribution_headers(self):
        cfg = SimpleNamespace(
            api_key="k",
            base_url="https://openrouter.ai/api/v1",
            extra_headers={},
        )
        headers = make_provider(cfg)._client.headers
        assert headers["HTTP-Referer"] == "https://istota.cynium.com"
        assert headers["X-Title"] == "Istota"

    def test_non_openrouter_endpoint_gets_no_attribution(self):
        cfg = SimpleNamespace(
            api_key="k",
            base_url="https://api.anthropic.com/v1",
            extra_headers={},
        )
        headers = make_provider(cfg)._client.headers
        assert "HTTP-Referer" not in headers
        assert "X-Title" not in headers

    def test_operator_extra_headers_override_attribution(self):
        cfg = SimpleNamespace(
            api_key="k",
            base_url="https://openrouter.ai/api/v1",
            # Case-insensitive: operator's own casing wins, defaults not added.
            extra_headers={"http-referer": "https://example.com", "X-Title": "Custom"},
        )
        headers = make_provider(cfg)._client.headers
        assert headers["HTTP-Referer"] == "https://example.com"
        assert headers["X-Title"] == "Custom"

    def test_missing_extra_headers_attr_still_attributes(self):
        # A config that never set extra_headers behaves like {}.
        cfg = SimpleNamespace(api_key="k", base_url="https://openrouter.ai/api/v1")
        headers = make_provider(cfg)._client.headers
        assert headers["X-Title"] == "Istota"
