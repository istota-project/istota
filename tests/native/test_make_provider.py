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
