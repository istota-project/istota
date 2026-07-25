"""Tests for the brain-scoped model namespace and operator alias overrides.

Two seams under test:

1. ``istota.brain.claude_code`` — Anthropic-shaped model identity and the
   Brain Protocol resolver methods on ``ClaudeCodeBrain``. A future brain
   would ship parallel tests against its own resolver.

2. ``istota.brain._roles`` — global operator override state, brain-agnostic.
"""

import pytest

from istota.brain import (
    BrainConfig,
    get_alias_overrides,
    make_brain,
    set_alias_overrides,
)
from istota.brain.claude_code import (
    DEFAULT_ALIASES,
    HAIKU,
    OPUS,
    SONNET,
    ClaudeCodeBrain,
)


@pytest.fixture(autouse=True)
def _reset_alias_overrides():
    """Aliases are global state — reset before and after every test."""
    set_alias_overrides({})
    yield
    set_alias_overrides({})


@pytest.fixture
def brain():
    return make_brain(BrainConfig(kind="claude_code"))


class TestCanonicalIds:
    def test_constants_are_versioned(self):
        for ident in (OPUS, SONNET, HAIKU):
            assert ident.startswith("claude-")
            # Must contain a version digit so a model release can't silently
            # re-route us via a floating tag like "opus".
            assert any(ch.isdigit() for ch in ident), f"unversioned: {ident}"

    def test_default_aliases_map_tiers_to_constants(self):
        assert DEFAULT_ALIASES["fast"] == (HAIKU, None)
        assert DEFAULT_ALIASES["general"] == (SONNET, None)
        assert DEFAULT_ALIASES["smart"] == (OPUS, None)

    def test_default_aliases_map_shortcuts_base_names_only(self):
        # Shortcuts are base names with no baked effort — effort is the modifier.
        assert DEFAULT_ALIASES["opus"] == (OPUS, None)
        assert DEFAULT_ALIASES["sonnet"] == (SONNET, None)
        assert DEFAULT_ALIASES["haiku"] == (HAIKU, None)
        assert DEFAULT_ALIASES["default"] == (None, None)

    def test_effort_variants_and_version_pins_removed(self):
        # The whole effort×model cross-product and stale pins are gone.
        for removed in ("opus-high", "opus-xhigh", "opus-max", "sonnet-high",
                        "opus-46", "opus-46-high", "opus-47", "opus-47-high"):
            assert removed not in DEFAULT_ALIASES


class TestBrainResolveModelName:
    def test_empty_passes_through(self, brain):
        assert brain.resolve_model_name("") == ""
        assert brain.resolve_model_name(None) == ""

    def test_role_resolves_to_canonical(self, brain):
        assert brain.resolve_model_name("smart") == OPUS
        assert brain.resolve_model_name("general") == SONNET
        assert brain.resolve_model_name("fast") == HAIKU

    def test_shortcut_resolves_to_canonical(self, brain):
        assert brain.resolve_model_name("opus") == OPUS
        assert brain.resolve_model_name("haiku") == HAIKU

    def test_effort_modifier_stripped_from_model_name(self, brain):
        # ``:effort`` never leaks into the resolved model id.
        assert brain.resolve_model_name("opus:high") == OPUS
        assert brain.resolve_model_name("claude-opus-4-8:xhigh") == OPUS

    def test_canonical_id_passes_through(self, brain):
        assert brain.resolve_model_name(OPUS) == OPUS

    def test_unknown_name_passes_through(self, brain):
        # Pass-through preserves backward compat for raw model IDs typed
        # directly into config (e.g., a future model not yet in the table).
        assert brain.resolve_model_name("claude-future-9-0") == "claude-future-9-0"

    def test_case_insensitive(self, brain):
        assert brain.resolve_model_name("SMART") == OPUS
        assert brain.resolve_model_name("Opus:High") == OPUS


class TestBrainResolveAlias:
    def test_tier_returns_no_effort(self, brain):
        assert brain.resolve_alias("smart") == (OPUS, None)
        assert brain.resolve_alias("general") == (SONNET, None)
        assert brain.resolve_alias("fast") == (HAIKU, None)

    def test_shortcut_base_returns_no_effort(self, brain):
        assert brain.resolve_alias("opus") == (OPUS, None)
        assert brain.resolve_alias("sonnet") == (SONNET, None)
        assert brain.resolve_alias("haiku") == (HAIKU, None)

    def test_effort_modifier_composes_on_shortcut(self, brain):
        # The old ``opus-high`` outcome, now composed from base + modifier.
        assert brain.resolve_alias("opus:high") == (OPUS, "high")
        assert brain.resolve_alias("opus:xhigh") == (OPUS, "xhigh")
        assert brain.resolve_alias("opus:max") == (OPUS, "max")

    def test_effort_modifier_composes_on_tier(self, brain):
        assert brain.resolve_alias("smart:low") == (OPUS, "low")

    def test_canonical_id_plus_effort(self, brain):
        assert brain.resolve_alias("claude-opus-4-8:xhigh") == (OPUS, "xhigh")
        # A prior-version canonical id still works via passthrough + modifier
        # (this covers the retired ``opus-47-high`` need).
        assert brain.resolve_alias("claude-opus-4-7:high") == ("claude-opus-4-7", "high")

    def test_canonical_id_without_effort_passthrough(self, brain):
        assert brain.resolve_alias(OPUS) == (OPUS, None)

    def test_removed_effort_suffix_forms_no_longer_resolve(self, brain):
        # HARD CUT — no shim maps ``opus-high`` to ``opus:high``.
        for removed in ("opus-high", "opus-xhigh", "opus-max", "sonnet-high",
                        "opus-46", "opus-46-high", "opus-47", "opus-47-high"):
            assert brain.resolve_alias(removed) is None, removed

    def test_unknown_returns_none(self, brain):
        assert brain.resolve_alias("not-a-thing") is None

    def test_default_alias_resolves_to_no_override(self, brain):
        assert brain.resolve_alias("default") == (None, None)

    def test_default_plus_effort(self, brain):
        # default:high → "brain/config default model, effort high".
        assert brain.resolve_alias("default:high") == (None, "high")

    def test_unknown_effort_suffix_not_split(self, brain):
        # ``opus:turbo`` — turbo isn't an effort level, so the whole thing is a
        # name → unknown.
        assert brain.resolve_alias("opus:turbo") is None


class TestAliasOverrides:
    def test_override_rebinds_alias(self, brain):
        set_alias_overrides({"smart": "claude-opus-4-6"})
        assert brain.resolve_model_name("smart") == "claude-opus-4-6"
        # shortcut still works unchanged
        assert brain.resolve_model_name("opus") == OPUS

    def test_override_via_shortcut_resolves_through_brain_table(self, brain):
        # Operator wrote `smart = "opus"`: brain resolves through its own
        # DEFAULT_ALIASES to the canonical OPUS id.
        set_alias_overrides({"smart": "opus"})
        assert brain.resolve_model_name("smart") == OPUS

    def test_override_with_canonical_id(self, brain):
        set_alias_overrides({"smart": "claude-opus-4-6"})
        assert brain.resolve_model_name("smart") == "claude-opus-4-6"

    def test_empty_overrides_resets_to_defaults(self, brain):
        set_alias_overrides({"smart": "claude-opus-4-6"})
        set_alias_overrides({})
        assert brain.resolve_model_name("smart") == OPUS

    def test_empty_override_value_is_dropped(self, brain):
        set_alias_overrides({"smart": ""})
        assert brain.resolve_model_name("smart") == OPUS

    def test_custom_alias_can_be_added(self, brain):
        set_alias_overrides({"deep": "opus:max"})
        assert brain.resolve_model_name("deep") == OPUS
        # The modifier's effort is preserved.
        assert brain.resolve_alias("deep") == (OPUS, "max")

    def test_flat_override_via_effort_modifier_preserves_effort(self, brain):
        set_alias_overrides({"smart": "opus:high"})
        assert brain.resolve_alias("smart") == (OPUS, "high")

    def test_override_suffix_modifier_wins_over_entry_effort(self, brain):
        # `!model smart:low` when smart's override default is high → low wins.
        set_alias_overrides({"smart": "opus:high"})
        assert brain.resolve_alias("smart:low") == (OPUS, "low")

    def test_override_does_not_mutate_default_aliases(self, brain):
        set_alias_overrides({"opus": "claude-opus-4-6"})
        # The shipped shortcut default is unchanged; the override wins in
        # resolve_alias. validate_alias_override warns on this collision.
        assert DEFAULT_ALIASES["opus"] == (OPUS, None)
        assert brain.resolve_alias("opus") == ("claude-opus-4-6", None)

    def test_get_alias_overrides_returns_copy(self):
        set_alias_overrides({"smart": "claude-opus-4-6"})
        snapshot = get_alias_overrides()
        # Flat value lands under the reserved "*" namespace.
        assert snapshot["smart"]["*"].model == "claude-opus-4-6"
        snapshot["smart"] = "tampered"
        assert get_alias_overrides()["smart"]["*"].model == "claude-opus-4-6"


class TestPerNamespaceOverrides:
    """claude_code reads the 'anthropic' namespace value; an openai_compat key
    on the same alias is invisible to it."""

    def test_anthropic_key_resolves(self, brain):
        set_alias_overrides(
            {
                "smart": {
                    "anthropic": "opus:high",
                    "openai_compat": "anthropic/claude-opus-4.8",
                }
            }
        )
        assert brain.resolve_alias("smart") == (OPUS, "high")
        assert brain.resolve_model_name("smart") == OPUS

    def test_anthropic_inline_table_effort_wins(self, brain):
        # Explicit RoleTarget.effort overrides the modifier-encoded effort.
        set_alias_overrides(
            {"smart": {"anthropic": {"model": "opus:high", "effort": "max"}}}
        )
        assert brain.resolve_alias("smart") == (OPUS, "max")

    def test_missing_anthropic_key_falls_to_default(self, brain):
        set_alias_overrides({"smart": {"openai_compat": "slug/x"}})
        assert brain.resolve_alias("smart") == (OPUS, None)
        assert brain.resolve_model_name("smart") == OPUS

    def test_list_aliases_reflects_anthropic_value(self, brain):
        set_alias_overrides(
            {"smart": {"anthropic": "opus:high", "openai_compat": "slug/x"}}
        )
        listed = {a: (m, e) for a, m, e in brain.list_aliases()}
        assert listed["smart"] == (OPUS, "high")


class TestBrainListAliases:
    def test_includes_tiers_and_shortcuts(self, brain):
        flat = brain.list_aliases()
        names = [a for a, *_ in flat]
        assert "smart" in names
        assert "opus" in names
        assert "sonnet" in names
        assert "haiku" in names
        assert "default" in names

    def test_tiers_listed_before_shortcuts(self, brain):
        names = [a for a, *_ in brain.list_aliases()]
        assert names[:3] == ["fast", "general", "smart"]

    def test_no_effort_variant_rows(self, brain):
        names = [a for a, *_ in brain.list_aliases()]
        assert "opus-high" not in names
        assert "opus-46" not in names

    def test_alias_overrides_reflected(self, brain):
        set_alias_overrides({"smart": "claude-opus-4-6"})
        flat = {a: m for a, m, _ in brain.list_aliases()}
        assert flat["smart"] == "claude-opus-4-6"

    def test_custom_alias_listed_after_shortcuts(self, brain):
        set_alias_overrides({"deep": "opus:max"})
        names = [a for a, *_ in brain.list_aliases()]
        assert "deep" in names
        assert names.index("deep") > names.index("opus")


class TestProtocolConformance:
    def test_claude_code_brain_satisfies_protocol(self):
        brain = ClaudeCodeBrain()
        assert callable(brain.resolve_alias)
        assert callable(brain.resolve_model_name)
        assert callable(brain.list_aliases)
        assert callable(brain.validate_alias_override)
        assert callable(brain.execute)


class TestValidateAliasOverride:
    def test_clean_override_returns_no_warnings(self, brain):
        assert brain.validate_alias_override("smart", "opus:high") == []
        assert brain.validate_alias_override("deep", "claude-opus-4-6") == []
        assert brain.validate_alias_override("custom", "haiku") == []

    def test_collision_with_shortcut_warns(self, brain):
        warnings = brain.validate_alias_override("opus", "haiku")
        assert any("shadows" in w.lower() for w in warnings)

    def test_tier_override_does_not_warn(self, brain):
        # Overriding a tier (smart/general/fast) is the normal case.
        assert brain.validate_alias_override("smart", "opus") == []

    def test_collision_warning_includes_alias_name(self, brain):
        warnings = brain.validate_alias_override("sonnet", "haiku")
        assert any("sonnet" in w for w in warnings)

    def test_unknown_target_warns(self, brain):
        warnings = brain.validate_alias_override("smart", "garbage-not-a-real-model")
        assert any("canonical" in w.lower() or "neither" in w.lower() for w in warnings)

    def test_unknown_target_warning_includes_target(self, brain):
        warnings = brain.validate_alias_override("smart", "garbage-9000")
        assert any("garbage-9000" in w for w in warnings)

    def test_shortcut_target_is_clean(self, brain):
        assert brain.validate_alias_override("smart", "opus") == []

    def test_target_with_effort_modifier_is_clean(self, brain):
        assert brain.validate_alias_override("smart", "opus:high") == []
        assert brain.validate_alias_override("smart", "claude-opus-4-7:high") == []

    def test_canonical_id_target_is_clean(self, brain):
        assert brain.validate_alias_override("smart", "claude-opus-5-0") == []

    def test_collision_and_unknown_target_both_reported(self, brain):
        warnings = brain.validate_alias_override("opus", "garbage-not-a-real-model")
        assert len(warnings) == 2


class TestLoadConfigIntegration:
    """End-to-end: [models.aliases] TOML through load_config to resolver."""

    def test_models_aliases_section_applies_to_brain(self, tmp_path):
        from istota.config import load_config

        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[models.aliases]\n'
            'smart = "claude-opus-4-6"\n'
            'deep  = "opus:max"\n'
        )
        config = load_config(config_file)
        assert config.models.aliases == {"smart": "claude-opus-4-6", "deep": "opus:max"}
        active = make_brain(config.brain)
        assert active.resolve_model_name("smart") == "claude-opus-4-6"
        assert active.resolve_model_name("deep") == OPUS

    def test_empty_models_section_resets_overrides(self, tmp_path):
        from istota.config import load_config

        set_alias_overrides({"smart": "claude-opus-4-6"})

        config_file = tmp_path / "config.toml"
        config_file.write_text("# no [models] section\n")
        config = load_config(config_file)
        active = make_brain(config.brain)
        assert active.resolve_model_name("smart") == OPUS
        assert config.models.aliases == {}

    def test_invalid_target_logged_at_load_but_does_not_fail(self, tmp_path, caplog):
        from istota.config import load_config

        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[models.aliases]\n'
            'smart = "garbage-not-a-real-model"\n'
        )
        with caplog.at_level("WARNING"):
            config = load_config(config_file)
        assert config.models.aliases == {"smart": "garbage-not-a-real-model"}
        assert any("garbage-not-a-real-model" in r.message for r in caplog.records)

    def test_collision_with_shortcut_logged(self, tmp_path, caplog):
        from istota.config import load_config

        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[models.aliases]\n'
            'opus = "haiku"\n'
        )
        with caplog.at_level("WARNING"):
            load_config(config_file)
        assert any("shadows" in r.message.lower() for r in caplog.records)

    def test_stale_models_roles_ignored_with_migration_warning(self, tmp_path, caplog):
        from istota.config import load_config

        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[models.roles]\n'
            'smart = "claude-opus-4-6"\n'
        )
        with caplog.at_level("WARNING"):
            config = load_config(config_file)
        # The stale key does not populate aliases, and roles fall to the code floor.
        assert config.models.aliases == {}
        active = make_brain(config.brain)
        assert active.resolve_model_name("smart") == OPUS
        assert any(
            "models.roles" in r.message and "models.aliases" in r.message
            for r in caplog.records
        )

    def test_per_namespace_table_parses_and_resolves(self, tmp_path):
        from istota.config import load_config

        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[models.aliases.smart]\n"
            'anthropic = "opus:high"\n'
            'openai_compat = { model = "anthropic/claude-opus-4.8", effort = "high" }\n'
            "[models.aliases.general]\n"
            'anthropic = "claude-sonnet-5"\n'
            'openai_compat = "anthropic/claude-sonnet-4.6"\n'
        )
        config = load_config(config_file)
        assert config.models.aliases["smart"]["anthropic"] == "opus:high"
        assert config.models.aliases["smart"]["openai_compat"] == {
            "model": "anthropic/claude-opus-4.8",
            "effort": "high",
        }
        active = make_brain(config.brain)
        assert active.resolve_model_name("smart") == OPUS
        assert active.resolve_alias("smart") == (OPUS, "high")

    def test_per_namespace_anthropic_shadow_warns_openai_compat_quiet(
        self, tmp_path, caplog
    ):
        from istota.config import load_config

        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[models.aliases.opus]\n"
            'anthropic = "haiku"\n'
            'openai_compat = "some/endpoint-slug-that-is-not-validated"\n'
        )
        with caplog.at_level("WARNING"):
            config = load_config(config_file)
        assert any("shadows" in r.message.lower() for r in caplog.records)
        assert "opus" in config.models.aliases

    def test_portable_key_recorded_and_stripped(self, tmp_path):
        from istota.brain._roles import get_portable_alias_names
        from istota.config import load_config

        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[models.aliases.deep]\n"
            'anthropic = "opus:max"\n'
            'openai_compat = "anthropic/claude-opus-4.8"\n'
            "portable = true\n"
        )
        config = load_config(config_file)
        # ``deep`` is recorded portable; the reserved key isn't a namespace.
        assert "deep" in get_portable_alias_names()
        active = make_brain(config.brain)
        assert active.resolve_model_name("deep") == OPUS
