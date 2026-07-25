"""Portable alias classifier (brain-fallback + centralized-alias-registry specs).

``is_portable_alias`` decides whether a requested model name is a provider-
agnostic *intent* (a tier or an operator-declared portable custom alias) that
re-resolves across the cross-brain fallback boundary, or a non-portable pin
(shortcut / canonical id) that can't cross. It keys on an explicit set of
portable names — CANONICAL_ROLES plus any ``portable = true`` aliases — not on
"any name in the alias table" (which would wrongly read ``opus`` as portable
now that shortcuts share the table). Effort modifiers are stripped first.
"""

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
        ["opus", "sonnet", "haiku", "claude-opus-4-8", "gpt-4o"],
    )
    def test_shortcuts_and_ids_not_portable(self, name):
        # Shortcuts and canonical ids bind to one provider — not portable, even
        # though ``opus`` now lives in the same registry as the tiers.
        assert is_portable_alias(name) is False

    @pytest.mark.parametrize("name", ["", "   ", None])
    def test_empty_not_portable(self, name):
        assert is_portable_alias(name) is False

    def test_effort_modifier_stripped_before_check(self):
        # ``smart:low`` is portable — the tier survives, effort re-applies in the
        # fallback namespace.
        assert is_portable_alias("smart:low") is True
        # ``opus:high`` is a pin + effort — still not portable.
        assert is_portable_alias("opus:high") is False

    def test_declared_portable_custom_alias(self):
        # A custom alias flagged portable=true (its name in the portable set)
        # crosses the boundary; the set is the explicit portable-names set.
        portable = {"deep", "cheap"}
        assert is_portable_alias("deep", portable) is True
        assert is_portable_alias("cheap", portable) is True
        assert is_portable_alias("DEEP", portable) is True
        assert is_portable_alias("deep:high", portable) is True

    def test_custom_alias_not_portable_without_declaration(self):
        assert is_portable_alias("deep") is False

    def test_shortcut_not_portable_even_with_portable_set(self):
        # A portable set that names custom aliases must not make ``opus`` portable.
        assert is_portable_alias("opus", {"deep"}) is False
