"""The shipped Anthropic catalogue (`src/istota/llm/data/anthropic.json`)."""

import json
from importlib.resources import files

import pytest

from istota.brain import claude_code
from istota.brain._aliases import EFFORT_LEVELS
from istota.llm.catalog import (
    build_catalogue,
    latest_shipped_id,
    shipped_anthropic_models,
)

FAMILIES = ("opus", "sonnet", "haiku")


def test_the_file_loads():
    document = json.loads(files("istota.llm").joinpath("data/anthropic.json").read_text())
    models = shipped_anthropic_models()
    assert len(models) == len(document["models"]) > 0
    assert all(m.provider == "anthropic" and m.price_source == "shipped" for m in models)


def test_every_entry_has_a_family_and_a_valid_id():
    for info in shipped_anthropic_models():
        assert info.family, info.id
        assert claude_code._looks_canonical(info.id), info.id
        assert info.context_window > 0 and info.max_output_tokens > 0, info.id


def test_ids_are_unique():
    ids = [m.id for m in shipped_anthropic_models()]
    assert len(ids) == len(set(ids))


def test_effort_maps_name_only_known_levels():
    for info in shipped_anthropic_models():
        assert set(info.effort_map) <= set(EFFORT_LEVELS), info.id


@pytest.mark.parametrize("family", FAMILIES)
def test_each_family_has_a_current_model(family):
    assert build_catalogue(shipped=shipped_anthropic_models()).latest(family) is not None


def test_the_constants_are_the_latest_of_each_family():
    assert claude_code.OPUS == latest_shipped_id("opus")
    assert claude_code.SONNET == latest_shipped_id("sonnet")
    assert claude_code.HAIKU == latest_shipped_id("haiku")


def test_the_default_tiers_follow_their_families():
    assert claude_code.DEFAULT_ALIASES["fast"] == (claude_code.HAIKU, None)
    assert claude_code.DEFAULT_ALIASES["general"] == (claude_code.SONNET, None)
    assert claude_code.DEFAULT_ALIASES["smart"] == (claude_code.OPUS, None)
