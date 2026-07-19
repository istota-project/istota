"""Portable alias classifier (brain-fallback spec, Stage 2)."""

import pytest

from istota.brain._aliases import CANONICAL_ROLES, is_portable_alias


class TestCanonicalRoles:
    def test_canonical_roles(self):
        assert CANONICAL_ROLES == ("fast", "general", "smart")


class TestIsPortableAlias:
    @pytest.mark.parametrize("name", ["fast", "general", "smart", "SMART", " smart "])
    def test_canonical_roles_are_portable(self, name):
        assert is_portable_alias(name) is True

    @pytest.mark.parametrize(
        "name",
        ["opus-high", "sonnet", "haiku", "opus-46", "claude-opus-4-8", "gpt-4o"],
    )
    def test_provider_aliases_and_ids_not_portable(self, name):
        assert is_portable_alias(name) is False

    @pytest.mark.parametrize("name", ["", "   ", None])
    def test_empty_not_portable(self, name):
        assert is_portable_alias(name) is False

    def test_custom_role_from_overrides_is_portable(self):
        overrides = {"deep": "opus-max", "cheap": "haiku"}
        assert is_portable_alias("deep", overrides) is True
        assert is_portable_alias("cheap", overrides) is True
        assert is_portable_alias("DEEP", overrides) is True

    def test_custom_role_not_portable_without_overrides(self):
        assert is_portable_alias("deep") is False

    def test_non_role_not_portable_even_with_overrides(self):
        overrides = {"deep": "opus-max"}
        assert is_portable_alias("opus-high", overrides) is False
