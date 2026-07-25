"""The orthogonal ``:effort`` modifier (centralized-model-alias-registry spec).

``split_effort`` peels a trailing ``:<effort>`` off any model reference when the
suffix is a known effort level. It is the single mechanism that makes effort
compositional on canonical ids, tiers, and shortcuts alike, replacing the old
baked-in ``opus-high`` cross-product.
"""

import pytest

from istota.brain._aliases import EFFORT_LEVELS, split_effort


class TestEffortLevels:
    def test_known_levels(self):
        assert EFFORT_LEVELS == frozenset({"low", "medium", "high", "xhigh", "max"})


class TestSplitEffort:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("opus:low", ("opus", "low")),
            ("opus:medium", ("opus", "medium")),
            ("opus:high", ("opus", "high")),
            ("opus:xhigh", ("opus", "xhigh")),
            ("opus:max", ("opus", "max")),
            ("smart:low", ("smart", "low")),
        ],
    )
    def test_all_effort_levels_split(self, raw, expected):
        assert split_effort(raw) == expected

    def test_no_colon_no_split(self):
        assert split_effort("opus") == ("opus", None)
        assert split_effort("claude-opus-4-8") == ("claude-opus-4-8", None)

    def test_unknown_suffix_not_split(self):
        # A trailing ``:something`` that isn't an effort level is left whole.
        assert split_effort("opus:turbo") == ("opus:turbo", None)

    def test_empty_base_not_split(self):
        # ``:high`` has no base — it stays a (nonsensical) whole name.
        assert split_effort(":high") == (":high", None)

    def test_trailing_colon_not_split(self):
        # ``opus:`` — empty suffix, not an effort level.
        assert split_effort("opus:") == ("opus:", None)

    def test_canonical_id_plus_effort(self):
        assert split_effort("claude-opus-4-8:xhigh") == ("claude-opus-4-8", "xhigh")

    def test_openrouter_slug_plus_effort(self):
        # The ``/`` in the slug is untouched — only the trailing ``:effort`` peels.
        assert split_effort("anthropic/claude-sonnet-4:high") == (
            "anthropic/claude-sonnet-4",
            "high",
        )

    def test_openrouter_slug_without_effort_intact(self):
        assert split_effort("anthropic/claude-sonnet-4") == (
            "anthropic/claude-sonnet-4",
            None,
        )

    def test_case_insensitive_effort(self):
        assert split_effort("opus:HIGH") == ("opus", "high")
        assert split_effort("opus:High") == ("opus", "high")

    def test_empty_and_none(self):
        assert split_effort("") == ("", None)
