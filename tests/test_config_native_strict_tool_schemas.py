"""``[brain.native] strict_tool_schemas``: a closed vocabulary, auto by default."""

from istota.config import NativeBrainConfig, config_from_document
from istota.config_mapper import collect_problems
from istota.llm.types import STRICT_TOOL_SCHEMA_MODES


def _native(raw: dict):
    with collect_problems() as problems:
        config = config_from_document({"brain": {"native": raw}})
    return config.brain.native, problems


def test_the_default_is_auto():
    assert NativeBrainConfig().strict_tool_schemas == "auto"
    native, problems = _native({})
    assert native.strict_tool_schemas == "auto"
    assert problems == []


def test_each_mode_loads():
    for mode in STRICT_TOOL_SCHEMA_MODES:
        native, problems = _native({"strict_tool_schemas": mode})
        assert native.strict_tool_schemas == mode
        assert problems == []


def test_an_unknown_mode_warns_and_keeps_the_default():
    native, problems = _native({"strict_tool_schemas": "always"})
    assert native.strict_tool_schemas == "auto"
    assert [p["path"] for p in problems] == [("brain", "native", "strict_tool_schemas")]
    assert problems[0]["kind"] == "discarded"


def test_model_overrides_carry_the_capability_flag():
    native, problems = _native(
        {"model_overrides": {"x/y": {"supports_structured_outputs": True}}}
    )
    assert native.model_overrides == {"x/y": {"supports_structured_outputs": True}}
    assert problems == []
