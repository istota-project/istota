"""Tests for the brain-scoped model namespace and operator role overrides.

Two seams under test:

1. ``istota.brain.claude_code`` — Anthropic-shaped model identity and the
   Brain Protocol resolver methods on ``ClaudeCodeBrain``. A future brain
   would ship parallel tests against its own resolver.

2. ``istota.brain._roles`` — global operator override state, brain-agnostic.
"""

import pytest

from istota.brain import (
    BrainConfig,
    get_role_overrides,
    make_brain,
    set_role_overrides,
)
from istota.brain.claude_code import (
    DEFAULT_ROLE_TARGETS,
    HAIKU,
    MODEL_ALIASES,
    OPUS,
    OPUS_46,
    SONNET,
    ClaudeCodeBrain,
)


@pytest.fixture(autouse=True)
def _reset_role_overrides():
    """Roles are global state — reset before and after every test."""
    set_role_overrides({})
    yield
    set_role_overrides({})


@pytest.fixture
def brain():
    return make_brain(BrainConfig(kind="claude_code"))


class TestCanonicalIds:
    def test_constants_are_versioned(self):
        for ident in (OPUS, OPUS_46, SONNET, HAIKU):
            assert ident.startswith("claude-")
            # Must contain a version digit so a model release can't silently
            # re-route us via a floating tag like "opus".
            assert any(ch.isdigit() for ch in ident), f"unversioned: {ident}"

    def test_default_role_targets_point_to_constants(self):
        assert DEFAULT_ROLE_TARGETS["fast"] == HAIKU
        assert DEFAULT_ROLE_TARGETS["general"] == SONNET
        assert DEFAULT_ROLE_TARGETS["smart"] == OPUS


class TestBrainResolveModelName:
    def test_empty_passes_through(self, brain):
        assert brain.resolve_model_name("") == ""
        assert brain.resolve_model_name(None) == ""

    def test_role_resolves_to_canonical(self, brain):
        assert brain.resolve_model_name("smart") == OPUS
        assert brain.resolve_model_name("general") == SONNET
        assert brain.resolve_model_name("fast") == HAIKU

    def test_provider_alias_resolves_to_canonical(self, brain):
        assert brain.resolve_model_name("opus") == OPUS
        assert brain.resolve_model_name("opus-high") == OPUS
        assert brain.resolve_model_name("haiku") == HAIKU
        assert brain.resolve_model_name("opus-46") == OPUS_46

    def test_canonical_id_passes_through(self, brain):
        assert brain.resolve_model_name(OPUS) == OPUS

    def test_unknown_name_passes_through(self, brain):
        # Pass-through preserves backward compat for raw model IDs typed
        # directly into config (e.g., a future model not yet in the table).
        assert brain.resolve_model_name("claude-future-9-0") == "claude-future-9-0"

    def test_case_insensitive(self, brain):
        assert brain.resolve_model_name("SMART") == OPUS
        assert brain.resolve_model_name("Opus-High") == OPUS


class TestBrainResolveAlias:
    def test_role_alias_returns_no_effort(self, brain):
        assert brain.resolve_alias("smart") == (OPUS, None)
        assert brain.resolve_alias("general") == (SONNET, None)
        assert brain.resolve_alias("fast") == (HAIKU, None)

    def test_provider_alias_carries_effort(self, brain):
        assert brain.resolve_alias("opus-high") == (OPUS, "high")
        assert brain.resolve_alias("opus-xhigh") == (OPUS, "xhigh")
        assert brain.resolve_alias("opus-46-high") == (OPUS_46, "high")

    def test_unknown_returns_none(self, brain):
        assert brain.resolve_alias("not-a-thing") is None

    def test_default_alias_resolves_to_no_override(self, brain):
        assert brain.resolve_alias("default") == (None, None)


class TestRoleOverrides:
    def test_override_rebinds_role(self, brain):
        set_role_overrides({"smart": "opus-46-high"})
        assert brain.resolve_model_name("smart") == OPUS_46
        # provider alias still works unchanged
        assert brain.resolve_model_name("opus") == OPUS

    def test_override_via_provider_alias_resolves_through_brain_table(self, brain):
        # Operator wrote `smart = "opus"`: brain should resolve through its
        # own MODEL_ALIASES to the canonical OPUS id.
        set_role_overrides({"smart": "opus"})
        assert brain.resolve_model_name("smart") == OPUS

    def test_override_with_canonical_id(self, brain):
        set_role_overrides({"smart": OPUS_46})
        assert brain.resolve_model_name("smart") == OPUS_46

    def test_empty_overrides_resets_to_defaults(self, brain):
        set_role_overrides({"smart": OPUS_46})
        set_role_overrides({})
        assert brain.resolve_model_name("smart") == OPUS

    def test_empty_override_value_is_dropped(self, brain):
        # Empty string should not silently rebind — stays at the default.
        set_role_overrides({"smart": ""})
        assert brain.resolve_model_name("smart") == OPUS

    def test_custom_role_can_be_added(self, brain):
        # Operators may define new roles beyond the default three.
        set_role_overrides({"deep": "opus-46-high"})
        assert brain.resolve_model_name("deep") == OPUS_46
        # And the brain surfaces it via resolve_alias too.
        assert brain.resolve_alias("deep") == (OPUS_46, None)

    def test_override_does_not_mutate_brain_alias_table(self, brain):
        set_role_overrides({"opus": OPUS_46})
        # Provider-alias resolution is unchanged: "opus" stays a provider
        # alias for OPUS in MODEL_ALIASES — but the override for the role
        # named "opus" wins in resolve_alias because roles are checked first.
        # `validate_role_override` warns on this collision at config-load.
        assert MODEL_ALIASES["opus"] == (OPUS, None)
        assert brain.resolve_alias("opus") == (OPUS_46, None)

    def test_get_role_overrides_returns_copy(self):
        set_role_overrides({"smart": OPUS_46})
        snapshot = get_role_overrides()
        snapshot["smart"] = "tampered"
        # Mutating the returned dict must not bleed back into module state.
        assert get_role_overrides()["smart"] == OPUS_46


class TestBrainListAliases:
    def test_includes_roles_and_provider_aliases(self, brain):
        flat = brain.list_aliases()
        names = [a for a, *_ in flat]
        assert "smart" in names
        assert "opus" in names
        assert "opus-46" in names
        assert "default" in names

    def test_role_overrides_reflected(self, brain):
        set_role_overrides({"smart": "opus-46-high"})
        flat = {a: m for a, m, _ in brain.list_aliases()}
        assert flat["smart"] == OPUS_46


class TestProtocolConformance:
    def test_claude_code_brain_satisfies_protocol(self):
        # Type-system check: ClaudeCodeBrain must expose all the resolver
        # methods we just added to the Brain Protocol.
        brain = ClaudeCodeBrain()
        assert callable(brain.resolve_alias)
        assert callable(brain.resolve_model_name)
        assert callable(brain.list_aliases)
        assert callable(brain.validate_role_override)
        assert callable(brain.execute)


class TestValidateRoleOverride:
    def test_clean_override_returns_no_warnings(self, brain):
        assert brain.validate_role_override("smart", "opus-46-high") == []
        assert brain.validate_role_override("deep", OPUS_46) == []
        assert brain.validate_role_override("custom", "haiku") == []

    def test_collision_with_provider_alias_warns(self, brain):
        warnings = brain.validate_role_override("opus", "haiku")
        assert any("shadows" in w.lower() for w in warnings)

    def test_collision_warning_includes_role_name(self, brain):
        warnings = brain.validate_role_override("opus-high", "haiku")
        assert any("opus-high" in w for w in warnings)

    def test_unknown_target_warns(self, brain):
        warnings = brain.validate_role_override("smart", "garbage-not-a-real-model")
        assert any("canonical" in w.lower() or "neither" in w.lower() for w in warnings)

    def test_unknown_target_warning_includes_target(self, brain):
        warnings = brain.validate_role_override("smart", "garbage-9000")
        assert any("garbage-9000" in w for w in warnings)

    def test_provider_alias_target_is_clean(self, brain):
        # "opus-46-high" is a known provider alias → no warning
        assert brain.validate_role_override("smart", "opus-46-high") == []

    def test_canonical_id_target_is_clean(self, brain):
        # Bare canonical id (even one not in MODEL_ALIASES) → no warning,
        # since pass-through is intentional for forward-compat.
        assert brain.validate_role_override("smart", "claude-opus-5-0") == []

    def test_collision_and_unknown_target_both_reported(self, brain):
        warnings = brain.validate_role_override("opus", "garbage-not-a-real-model")
        assert len(warnings) == 2


class TestLoadConfigIntegration:
    """End-to-end: [models.roles] TOML through load_config to resolver."""

    def test_models_roles_section_applies_to_brain(self, tmp_path):
        from istota.config import load_config

        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[models.roles]\n'
            'smart = "opus-46-high"\n'
            'deep  = "opus-max"\n'
        )
        config = load_config(config_file)
        # Roles parsed into config.models
        assert config.models.roles == {"smart": "opus-46-high", "deep": "opus-max"}
        # And applied to the global override table — the active brain's
        # resolver picks them up.
        active = make_brain(config.brain)
        assert active.resolve_model_name("smart") == OPUS_46
        assert active.resolve_model_name("deep") == OPUS

    def test_empty_models_section_resets_overrides(self, tmp_path):
        from istota.config import load_config

        # Pre-seed an override that should NOT survive load_config.
        set_role_overrides({"smart": OPUS_46})

        config_file = tmp_path / "config.toml"
        config_file.write_text("# no [models] section\n")
        config = load_config(config_file)
        active = make_brain(config.brain)
        # No overrides → smart resolves to default OPUS again
        assert active.resolve_model_name("smart") == OPUS
        assert config.models.roles == {}

    def test_invalid_target_logged_at_load_but_does_not_fail(self, tmp_path, caplog):
        from istota.config import load_config

        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[models.roles]\n'
            'smart = "garbage-not-a-real-model"\n'
        )
        with caplog.at_level("WARNING"):
            config = load_config(config_file)
        # Override is still applied (warn, don't reject) but a warning is logged.
        assert config.models.roles == {"smart": "garbage-not-a-real-model"}
        assert any("garbage-not-a-real-model" in r.message for r in caplog.records)

    def test_collision_with_provider_alias_logged(self, tmp_path, caplog):
        from istota.config import load_config

        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[models.roles]\n'
            'opus = "haiku"\n'
        )
        with caplog.at_level("WARNING"):
            load_config(config_file)
        assert any("shadows" in r.message.lower() for r in caplog.records)
