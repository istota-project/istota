"""Tests for the per-namespace operator role-override state (brain._roles).

Covers ``set_role_overrides`` normalization of the three input shapes,
``get_role_override_target`` precedence (per-namespace > legacy "*" > None),
atomic rebind, and malformed-entry dropping. Brain-agnostic — resolver
behavior is exercised in test_brain_models / test_native_resolution.
"""

import pytest

from istota.brain._roles import (
    LEGACY_NAMESPACE,
    RoleTarget,
    get_role_override_target,
    get_role_overrides,
    set_role_overrides,
)


@pytest.fixture(autouse=True)
def _reset_role_overrides():
    """Role state is process-global — reset before and after every test."""
    set_role_overrides({})
    yield
    set_role_overrides({})


class TestNormalizeFlat:
    def test_flat_string_stored_under_legacy_namespace(self):
        set_role_overrides({"smart": "opus-high"})
        rt = get_role_override_target("smart", LEGACY_NAMESPACE)
        assert rt == RoleTarget(model="opus-high", effort=None)

    def test_flat_string_visible_to_any_namespace(self):
        set_role_overrides({"smart": "opus-high"})
        # A flat value is namespace-agnostic — a per-namespace miss falls to "*".
        assert get_role_override_target("smart", "anthropic").model == "opus-high"
        assert get_role_override_target("smart", "openai_compat").model == "opus-high"

    def test_flat_blank_dropped(self):
        set_role_overrides({"smart": "   "})
        assert get_role_override_target("smart", "anthropic") is None

    def test_role_name_lowercased(self):
        set_role_overrides({"SMART": "opus"})
        assert get_role_override_target("smart", LEGACY_NAMESPACE).model == "opus"


class TestNormalizePerNamespace:
    def test_namespace_string_values(self):
        set_role_overrides(
            {"smart": {"anthropic": "opus-high", "openai_compat": "anthropic/claude-opus-4.8"}}
        )
        assert get_role_override_target("smart", "anthropic") == RoleTarget("opus-high", None)
        assert get_role_override_target("smart", "openai_compat") == RoleTarget(
            "anthropic/claude-opus-4.8", None
        )

    def test_namespace_inline_table_with_effort(self):
        set_role_overrides(
            {"smart": {"openai_compat": {"model": "anthropic/claude-opus-4.8", "effort": "high"}}}
        )
        assert get_role_override_target("smart", "openai_compat") == RoleTarget(
            "anthropic/claude-opus-4.8", "high"
        )

    def test_mixed_string_and_table_per_role(self):
        set_role_overrides(
            {
                "smart": {
                    "anthropic": "opus-high",
                    "openai_compat": {"model": "x/y", "effort": "max"},
                }
            }
        )
        assert get_role_override_target("smart", "anthropic") == RoleTarget("opus-high", None)
        assert get_role_override_target("smart", "openai_compat") == RoleTarget("x/y", "max")

    def test_table_missing_model_dropped(self):
        set_role_overrides({"smart": {"openai_compat": {"effort": "high"}}})
        assert get_role_override_target("smart", "openai_compat") is None

    def test_table_blank_model_dropped(self):
        set_role_overrides({"smart": {"openai_compat": {"model": "  "}}})
        assert get_role_override_target("smart", "openai_compat") is None

    def test_effort_blank_normalized_to_none(self):
        set_role_overrides({"smart": {"anthropic": {"model": "opus", "effort": "  "}}})
        assert get_role_override_target("smart", "anthropic") == RoleTarget("opus", None)


class TestPrecedence:
    def test_per_namespace_wins_over_legacy(self):
        # A role carrying BOTH a flat and a namespace value can't happen in TOML
        # grammar, but the internal precedence still favors the namespace entry.
        set_role_overrides({"smart": {"anthropic": "opus", LEGACY_NAMESPACE: "sonnet"}})
        assert get_role_override_target("smart", "anthropic").model == "opus"
        assert get_role_override_target("smart", "openai_compat").model == "sonnet"

    def test_namespace_miss_no_legacy_returns_none(self):
        set_role_overrides({"smart": {"anthropic": "opus"}})
        assert get_role_override_target("smart", "openai_compat") is None

    def test_unknown_role_returns_none(self):
        set_role_overrides({"smart": "opus"})
        assert get_role_override_target("general", "anthropic") is None


class TestMalformedAndReset:
    def test_non_string_role_key_dropped(self):
        set_role_overrides({123: "opus", "smart": "sonnet"})
        assert get_role_override_target("smart", LEGACY_NAMESPACE).model == "sonnet"

    def test_non_string_non_table_value_dropped(self):
        set_role_overrides({"smart": 42, "fast": "haiku"})
        assert get_role_override_target("smart", "anthropic") is None
        assert get_role_override_target("fast", LEGACY_NAMESPACE).model == "haiku"

    def test_non_string_namespace_key_dropped(self):
        set_role_overrides({"smart": {123: "opus", "anthropic": "sonnet"}})
        assert get_role_override_target("smart", "anthropic").model == "sonnet"

    def test_empty_clears_table(self):
        set_role_overrides({"smart": "opus"})
        set_role_overrides({})
        assert get_role_override_target("smart", "anthropic") is None

    def test_none_clears_table(self):
        set_role_overrides({"smart": "opus"})
        set_role_overrides(None)
        assert get_role_override_target("smart", "anthropic") is None

    def test_atomic_rebind_replaces_not_merges(self):
        set_role_overrides({"smart": "opus", "fast": "haiku"})
        set_role_overrides({"smart": "sonnet"})
        assert get_role_override_target("smart", LEGACY_NAMESPACE).model == "sonnet"
        # The prior "fast" entry is gone — rebind is a replace, not a merge.
        assert get_role_override_target("fast", "anthropic") is None


class TestGetRoleOverrides:
    def test_returns_role_names_as_keys(self):
        set_role_overrides({"smart": {"anthropic": "opus"}, "deep": "sonnet"})
        table = get_role_overrides()
        assert set(table) == {"smart", "deep"}

    def test_copy_is_isolated(self):
        set_role_overrides({"smart": "opus"})
        snapshot = get_role_overrides()
        snapshot["smart"] = "tampered"
        assert get_role_override_target("smart", LEGACY_NAMESPACE).model == "opus"
