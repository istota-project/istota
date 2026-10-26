"""``[brain.native] prompt_cache_key`` and ``prompt_cache_ttl``."""

from istota.config import NativeBrainConfig, config_from_document
from istota.config_mapper import collect_problems
from istota.llm.types import PROMPT_CACHE_TTLS


def _native(raw: dict):
    with collect_problems() as problems:
        config = config_from_document({"brain": {"native": raw}})
    return config.brain.native, problems


def test_the_defaults():
    assert NativeBrainConfig().prompt_cache_key is None
    assert NativeBrainConfig().prompt_cache_ttl == "5m"
    native, problems = _native({})
    assert native.prompt_cache_key is None
    assert native.prompt_cache_ttl == "5m"
    assert problems == []


def test_an_explicit_key_setting_loads():
    for value in (True, False):
        native, problems = _native({"prompt_cache_key": value})
        assert native.prompt_cache_key is value
        assert problems == []


def test_each_ttl_loads():
    assert PROMPT_CACHE_TTLS == ("5m", "1h")
    for ttl in PROMPT_CACHE_TTLS:
        native, problems = _native({"prompt_cache_ttl": ttl})
        assert native.prompt_cache_ttl == ttl
        assert problems == []


def test_an_unknown_ttl_warns_and_keeps_the_default():
    native, problems = _native({"prompt_cache_ttl": "24h"})
    assert native.prompt_cache_ttl == "5m"
    assert [p["path"] for p in problems] == [("brain", "native", "prompt_cache_ttl")]
    assert problems[0]["kind"] == "discarded"
