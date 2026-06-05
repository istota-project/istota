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
