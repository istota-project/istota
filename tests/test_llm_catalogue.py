"""The model catalogue: layering, the widened record, family lookup, tiered prices."""

from dataclasses import asdict, replace

import pytest

from istota.llm import catalog
from istota.llm.catalog import (
    Catalogue,
    ModelInfo,
    PriceTier,
    build_catalogue,
    get_model_info,
    model_info_from_dict,
    set_fetched_catalog,
    set_model_overrides,
)
from istota.llm.types import Usage
from istota.session.usage import price_usage


def _m(id, provider="anthropic", **kw):
    kw.setdefault("context_window", 100_000)
    kw.setdefault("max_output_tokens", 8_000)
    return ModelInfo(id=id, provider=provider, **kw)


class TestModelInfo:
    def test_legacy_capability_keywords_set_the_new_fields(self):
        info = ModelInfo(
            id="m", context_window=1, max_output_tokens=1,
            supports_vision=True, supports_thinking=True,
        )
        assert info.input == ("text", "image")
        assert info.reasoning is True
        assert info.supports_vision is True
        assert info.supports_thinking is True

    def test_replace_with_a_legacy_keyword_wins_over_the_copied_field(self):
        info = _m("m", input=("text", "image"), reasoning=True)
        assert replace(info, supports_vision=False).supports_vision is False
        assert replace(info, supports_thinking=False).reasoning is False
        assert replace(info, context_window=5).supports_vision is True

    def test_ref_and_name_default(self):
        info = _m("claude-x")
        assert info.ref == "anthropic/claude-x"
        assert info.name == "claude-x"
        assert ModelInfo(id="bare", context_window=1, max_output_tokens=1).ref == "bare"

    def test_round_trips_through_asdict(self):
        info = _m(
            "m", input=("text", "image"), effort_map={"max": "max", "low": None},
            price_tiers=(PriceTier(100_000, 2.0, 4.0, 0.2, 2.5),),
        )
        assert model_info_from_dict(asdict(info)) == info


class TestLayers:
    def test_layer_order(self):
        shipped = [_m("a", context_window=1), _m("b", context_window=1), _m("c", context_window=1)]
        cat = build_catalogue(
            shipped=shipped,
            discovered={"anthropic": [_m("b", context_window=2), _m("c", context_window=2)]},
            operator={"anthropic": [_m("c", context_window=3)]},
            overrides={"anthropic/a": {"context_window": 4}},
        )
        assert cat.get("anthropic/a").context_window == 4
        assert cat.get("anthropic/b").context_window == 2
        assert cat.get("anthropic/c").context_window == 3
        assert cat.get("nothing").context_window == 200_000

    def test_override_merges_field_by_field(self):
        cat = build_catalogue(
            shipped=[_m("a", input_price_per_mtok=3.0, reasoning=True)],
            overrides={"anthropic/a": {"output_price_per_mtok": 9.0}},
        )
        info = cat.get("anthropic/a")
        assert info.input_price_per_mtok == 3.0
        assert info.output_price_per_mtok == 9.0
        assert info.reasoning is True

    def test_override_replaces_effort_map_whole(self):
        cat = build_catalogue(
            shipped=[_m("a", effort_map={"max": "max", "xhigh": "xhigh"})],
            overrides={"anthropic/a": {"effort_map": {"low": None}}},
        )
        assert cat.get("anthropic/a").effort_map == {"low": None}

    def test_a_shipped_model_is_reached_by_ref_not_bare_id(self):
        # A bare Claude id on native goes to an endpoint the shipped file does
        # not describe; until a provider owns bare ids, they miss the file.
        cat = build_catalogue(shipped=[_m("claude-x", context_window=1)])
        assert cat.get("anthropic/claude-x").context_window == 1
        assert cat.get("claude-x").context_window == 200_000

    def test_a_discovered_model_is_reached_by_bare_id_and_ref(self):
        cat = build_catalogue(discovered={"openrouter": [_m("x/y", provider="openrouter")]})
        assert cat.get("x/y") is cat.get("openrouter/x/y")

    def test_an_exact_id_beats_a_ref_reading(self):
        # OpenRouter's id ``anthropic/claude-x`` is not the shipped claude-x.
        cat = build_catalogue(
            shipped=[_m("claude-x", context_window=1)],
            discovered={"openrouter": [_m("anthropic/claude-x", provider="", context_window=2)]},
        )
        info = cat.get("anthropic/claude-x")
        assert info.context_window == 2
        assert info.provider == "openrouter"

    def test_miss_carries_the_queried_id(self):
        info = build_catalogue().get("made-up")
        assert info.id == "made-up"
        assert info.name == "made-up"
        assert info.price_source == "unknown"


class TestLatest:
    def test_newest_current_model_in_a_family(self):
        cat = build_catalogue(shipped=[
            _m("o1", family="opus", released="2026-01-01"),
            _m("o2", family="opus", released="2026-03-01"),
            _m("o3", family="opus", released="2026-05-01", deprecated=True),
            _m("s1", family="sonnet", released="2026-06-01"),
        ])
        assert cat.latest("opus").id == "o2"
        assert cat.latest("haiku") is None

    def test_other_providers_never_count(self):
        cat = build_catalogue(
            discovered={"openrouter": [_m("x/opus", provider="openrouter", family="opus", released="2027-01-01")]},
            shipped=[_m("o1", family="opus", released="2026-01-01")],
        )
        assert cat.latest("opus").id == "o1"

    def test_models_and_refs(self):
        cat = build_catalogue(
            shipped=[_m("a")],
            discovered={"openrouter": [_m("x/b", provider="openrouter")]},
        )
        assert set(cat.refs()) == {"anthropic/a", "openrouter/x/b"}
        assert [m.id for m in cat.models("openrouter")] == ["x/b"]

    def test_an_openrouter_id_spelling_a_shipped_ref_does_not_replace_it(self):
        """`anthropic/o2` is OpenRouter's id and also the shipped record's ref;
        `models()` takes each record by its ref, so the family stays intact."""
        cat = build_catalogue(
            shipped=[_m("o1", family="opus", released="2026-01-01"),
                     _m("o2", family="opus", released="2026-05-01")],
            discovered={"openrouter": [_m("anthropic/o2", provider="openrouter")]},
        )
        assert cat.latest("opus").id == "o2"
        assert {m.provider for m in cat.models("anthropic")} == {"anthropic"}


class TestModuleSnapshot:
    def teardown_method(self):
        set_fetched_catalog(None)
        set_model_overrides({})

    def test_shipped_anthropic_models_resolve_by_ref(self):
        from istota.brain.claude_code import OPUS

        info = get_model_info(f"anthropic/{OPUS}")
        assert info.id == OPUS
        assert info.provider == "anthropic"
        assert info.price_source == "shipped"
        assert info.family == "opus"

    def test_a_bare_claude_id_keeps_todays_default(self):
        from istota.brain.claude_code import OPUS

        assert get_model_info(OPUS).price_source == "unknown"

    def test_shims_swap_the_snapshot(self):
        before = catalog.current_catalogue()
        set_model_overrides({"some-model": {"context_window": 7}})
        after = catalog.current_catalogue()
        assert isinstance(after, Catalogue)
        assert after is not before
        assert get_model_info("some-model").context_window == 7

    def test_legacy_override_keys_still_apply(self):
        set_model_overrides({"some-model": {"supports_vision": True, "bogus": 1}})
        assert get_model_info("some-model").supports_vision is True


class TestTieredPricing:
    INFO = _m(
        "m", input_price_per_mtok=1.0, output_price_per_mtok=2.0,
        price_tiers=(
            PriceTier(100_000, 10.0, 20.0, 0.0, 0.0),
            PriceTier(500_000, 100.0, 200.0, 0.0, 0.0),
        ),
    )

    def test_base_prices_at_or_below_the_threshold(self):
        usage = Usage(input_tokens=100_000, output_tokens=1_000_000)
        assert price_usage(usage, self.INFO) == pytest.approx(0.1 + 2.0)

    def test_highest_exceeded_tier_prices_the_whole_request(self):
        usage = Usage(input_tokens=600_000, output_tokens=1_000_000)
        assert price_usage(usage, self.INFO) == pytest.approx(60.0 + 200.0)

    def test_cached_input_counts_toward_the_threshold(self):
        usage = Usage(input_tokens=50_000, cache_read_tokens=60_000, output_tokens=0)
        assert price_usage(usage, self.INFO) == pytest.approx(0.5)
