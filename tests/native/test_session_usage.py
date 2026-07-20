"""Per-session cost and token telemetry (Crush refinement item 6)."""

from istota.llm.catalog import ModelInfo
from istota.llm.types import Usage
from istota.session.usage import TaskUsage, price_usage


class TestPriceUsage:
    def test_zero_price_catalog_yields_zero_cost(self):
        info = ModelInfo(id="m", context_window=200_000, max_output_tokens=16384)
        usage = Usage(input_tokens=1000, output_tokens=2000)
        assert price_usage(usage, info) == 0.0

    def test_prices_each_token_class(self):
        info = ModelInfo(
            id="m",
            context_window=200_000,
            max_output_tokens=16384,
            input_price_per_mtok=3.0,
            output_price_per_mtok=15.0,
            cache_read_price_per_mtok=0.3,
            cache_write_price_per_mtok=3.75,
        )
        usage = Usage(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
        )
        # 3 + 15 + 0.3 + 3.75
        assert price_usage(usage, info) == 22.05

    def test_partial_million(self):
        info = ModelInfo(
            id="m",
            context_window=200_000,
            max_output_tokens=16384,
            input_price_per_mtok=3.0,
        )
        usage = Usage(input_tokens=500_000)
        assert price_usage(usage, info) == 1.5


class TestTaskUsage:
    def test_add_accumulates_tokens_and_turns(self):
        info = ModelInfo(id="m", context_window=200_000, max_output_tokens=16384)
        tu = TaskUsage()
        tu.add(Usage(input_tokens=10, output_tokens=5), info)
        tu.add(Usage(input_tokens=20, output_tokens=7, cache_read_tokens=3), info)
        assert tu.input_tokens == 30
        assert tu.output_tokens == 12
        assert tu.cache_read_tokens == 3
        assert tu.turns == 2

    def test_add_accumulates_cost(self):
        info = ModelInfo(
            id="m",
            context_window=200_000,
            max_output_tokens=16384,
            input_price_per_mtok=3.0,
            output_price_per_mtok=15.0,
        )
        tu = TaskUsage()
        tu.add(Usage(input_tokens=1_000_000, output_tokens=1_000_000), info)
        assert tu.cost_usd == 18.0

    def test_provider_reported_cost_preferred_over_catalog(self):
        # Catalog has real prices, but the provider reported its own cost —
        # the reported figure wins (OpenRouter markup would differ from ours).
        info = ModelInfo(
            id="m",
            context_window=200_000,
            max_output_tokens=16384,
            input_price_per_mtok=3.0,
            output_price_per_mtok=15.0,
        )
        tu = TaskUsage()
        tu.add(
            Usage(input_tokens=1_000_000, output_tokens=1_000_000, cost_usd=0.42),
            info,
        )
        assert tu.cost_usd == 0.42

    def test_falls_back_to_catalog_when_no_reported_cost(self):
        info = ModelInfo(
            id="m",
            context_window=200_000,
            max_output_tokens=16384,
            input_price_per_mtok=3.0,
        )
        tu = TaskUsage()
        tu.add(Usage(input_tokens=1_000_000, cost_usd=None), info)
        assert tu.cost_usd == 3.0

    def test_reported_zero_cost_respected_not_recomputed(self):
        # A genuine free turn (reported 0.0) must not silently fall back to
        # catalog pricing — None means "unreported", 0.0 means "free".
        info = ModelInfo(
            id="m",
            context_window=200_000,
            max_output_tokens=16384,
            input_price_per_mtok=3.0,
        )
        tu = TaskUsage()
        tu.add(Usage(input_tokens=1_000_000, cost_usd=0.0), info)
        assert tu.cost_usd == 0.0

    def test_merge_rolls_subagent_into_parent(self):
        parent = TaskUsage(input_tokens=10, output_tokens=5, cost_usd=1.0, turns=1)
        child = TaskUsage(input_tokens=20, output_tokens=8, cost_usd=2.0, turns=3)
        parent.merge(child)
        assert parent.input_tokens == 30
        assert parent.output_tokens == 13
        assert parent.cost_usd == 3.0
        assert parent.turns == 4
