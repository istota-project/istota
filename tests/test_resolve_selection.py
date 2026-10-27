"""`brain._resolve.resolve_selection`: one resolver for every model selection.

Each step on a small catalogue built here, so a regenerated shipped file moves
nothing: aliases (tiers, family shortcuts, operator overrides, effort-only
overrides), the preferred provider's own id, qualified refs, bare ids,
patterns, and the refusals (`model_not_runnable`, `model_not_enabled`,
`no_tools`). Then the three brains that delegate to it, and `brain_can_run`.
"""

from __future__ import annotations

from istota.brain._resolve import (
    Resolution,
    ResolveError,
    default_enabled_patterns,
    enabled_refs,
    resolve_selection,
    unmatched_patterns,
)
from istota.brain._roles import AliasTable, parse_alias_overrides
from istota.llm.catalog import ModelInfo, build_catalogue
from istota.llm.providers import brain_can_run


def _m(id, provider, family=None, released=None, deprecated=False, tools=True, name=""):
    return ModelInfo(
        id=id, context_window=1000, max_output_tokens=100, provider=provider,
        family=family, released=released, deprecated=deprecated,
        supports_tools=tools, name=name,
    )


SHIPPED = (
    _m("claude-opus-4-8", "anthropic", "opus", "2026-05-28"),
    _m("claude-opus-5", "anthropic", "opus", "2026-07-24"),
    _m("claude-opus-3", "anthropic", "opus", "2027-01-01", deprecated=True),
    _m("claude-sonnet-5", "anthropic", "sonnet", "2026-06-29"),
    _m("claude-haiku-5", "anthropic", "haiku", "2026-07-01"),
)
OPENROUTER = (
    _m("anthropic/claude-opus-5", "openrouter"),
    _m("z-ai/glm-5", "openrouter", name="GLM 5"),
    _m("z-ai/glm-5-air", "openrouter", name="GLM 5 Air"),
    _m("deepseek/deepseek-r1:free", "openrouter"),
    _m("acme/no-tools", "openrouter", tools=False),
)


def _catalogue(discovered=None):
    return build_catalogue(
        shipped=SHIPPED,
        discovered={"openrouter": OPENROUTER} if discovered is None else discovered,
    )


CAT = _catalogue()
NONE = AliasTable()


def _ok(result) -> Resolution:
    assert isinstance(result, Resolution), result
    return result


def _err(result) -> ResolveError:
    assert isinstance(result, ResolveError), result
    return result


class TestAliases:
    def test_a_tier_names_a_family_and_takes_its_newest_current_model(self):
        r = _ok(resolve_selection("smart", CAT, aliases=NONE))
        assert (r.ref, r.source, r.alias) == ("anthropic/claude-opus-5", "alias", "smart")

    def test_a_family_shortcut_never_resolves_to_a_deprecated_model(self):
        # claude-opus-3 is the newest by date and deprecated.
        assert _ok(resolve_selection("opus", CAT, aliases=NONE)).ref == "anthropic/claude-opus-5"

    def test_a_newer_model_the_catalogue_learns_moves_the_shortcut(self):
        newer = build_catalogue(
            shipped=SHIPPED,
            discovered={"anthropic": (_m("claude-opus-6", "anthropic", "opus", "2026-12-01"),)},
        )
        assert _ok(resolve_selection("opus", newer, aliases=NONE)).ref == "anthropic/claude-opus-6"

    def test_a_family_with_no_current_model_is_unknown(self):
        cat = build_catalogue(shipped=(SHIPPED[2],))
        assert _err(resolve_selection("opus", cat, aliases=NONE)).code == "unknown"

    def test_default_is_the_brains_own_model(self):
        r = _ok(resolve_selection("default:high", CAT, aliases=NONE))
        assert r.brain_default and r.ref == "" and r.effort_requested == "high"

    def test_tiers_on_native_mean_the_configured_model(self):
        r = _ok(resolve_selection("general", CAT, aliases=NONE, for_brain="native", native_provider="openrouter"))
        assert r.brain_default

    def test_an_override_target_is_itself_a_selection(self):
        table = parse_alias_overrides({"smart": {"anthropic": "haiku:high"}}, log=False)
        r = _ok(resolve_selection("smart", CAT, aliases=table))
        assert (r.ref, r.effort_requested) == ("anthropic/claude-haiku-5", "high")

    def test_the_suffix_effort_beats_the_override_effort(self):
        table = parse_alias_overrides({"smart": {"anthropic": {"model": "opus", "effort": "low"}}}, log=False)
        assert _ok(resolve_selection("smart:max", CAT, aliases=table)).effort_requested == "max"

    def test_an_effort_only_override_keeps_the_default_target(self):
        table = parse_alias_overrides({"general": {"anthropic": {"effort": "medium"}}}, log=False)
        r = _ok(resolve_selection("general", CAT, aliases=table))
        assert (r.ref, r.effort_requested) == ("anthropic/claude-sonnet-5", "medium")

    def test_an_effort_only_override_on_native_keeps_the_configured_model(self):
        table = parse_alias_overrides({"general": {"openai_compat": {"effort": "high"}}}, log=False)
        r = _ok(resolve_selection("general", CAT, aliases=table, namespace="openai_compat"))
        assert r.brain_default and r.effort_requested == "high"

    def test_an_override_in_another_namespace_is_not_read(self):
        table = parse_alias_overrides({"smart": {"openai_compat": "z-ai/glm-5"}}, log=False)
        assert _ok(resolve_selection("smart", CAT, aliases=table)).ref == "anthropic/claude-opus-5"

    def test_an_unresolvable_override_target_is_named_on_the_error(self):
        table = parse_alias_overrides({"mine": "nothing-like-this"}, log=False)
        err = _err(resolve_selection("mine", CAT, aliases=table))
        assert err.target == "nothing-like-this"


class TestModels:
    def test_a_qualified_ref(self):
        r = _ok(resolve_selection("openrouter/z-ai/glm-5", CAT, aliases=NONE))
        assert (r.ref, r.source, r.info.id) == ("openrouter/z-ai/glm-5", "ref", "z-ai/glm-5")

    def test_a_colon_in_an_id_is_kept_and_a_real_effort_splits(self):
        r = _ok(resolve_selection("openrouter/deepseek/deepseek-r1:free:high", CAT, aliases=NONE))
        assert (r.info.id, r.effort_requested) == ("deepseek/deepseek-r1:free", "high")

    def test_a_prefix_that_is_no_provider_is_a_bare_id(self):
        r = _ok(resolve_selection("z-ai/glm-5", CAT, aliases=NONE))
        assert (r.ref, r.source) == ("openrouter/z-ai/glm-5", "id")

    def test_a_bare_id_on_two_providers_prefers_the_brains(self):
        cat = _catalogue({"openrouter": OPENROUTER, "local": (_m("z-ai/glm-5", "local"),)})
        err = _err(resolve_selection("z-ai/glm-5", cat, aliases=NONE))
        assert err.code == "ambiguous"
        assert set(err.candidates) == {"openrouter/z-ai/glm-5", "local/z-ai/glm-5"}
        r = _ok(resolve_selection("z-ai/glm-5", cat, aliases=NONE, prefer_provider="local"))
        assert r.ref == "local/z-ai/glm-5"

    def test_the_preferred_providers_own_id_wins_over_a_ref_reading(self):
        """The decision that keeps a stored bare id running where it ran: on a
        native brain that uses OpenRouter, `anthropic/claude-opus-5` is
        OpenRouter's own id, not the built-in `anthropic` provider's model."""
        r = _ok(resolve_selection(
            "anthropic/claude-opus-5", CAT, aliases=NONE,
            for_brain="native", native_provider="openrouter",
        ))
        assert (r.ref, r.source) == ("openrouter/anthropic/claude-opus-5", "id")
        # With no preferred provider it is the ref the spec's order reads.
        assert _ok(resolve_selection("anthropic/claude-opus-5", CAT, aliases=NONE)).source == "ref"

    def test_an_unknown_ref_under_a_configured_provider(self):
        err = _err(resolve_selection("openrouter/no/such", CAT, aliases=NONE))
        assert err.code == "unknown" and "openrouter" in err.message

    def test_nothing_named_is_unknown(self):
        assert _err(resolve_selection("", CAT, aliases=NONE)).code == "unknown"
        assert _err(resolve_selection("sonet", CAT, aliases=NONE)).code == "unknown"


class TestPatterns:
    def test_patterns_are_off_unless_asked_for(self):
        assert _err(resolve_selection("glm", CAT, aliases=NONE)).code == "unknown"

    def test_one_family_picks_the_newest_current_model(self):
        kwargs = dict(use_aliases=False, allow_patterns=True)
        r = _ok(resolve_selection("opus", CAT, for_brain="claude_code", **kwargs))
        assert (r.ref, r.source) == ("anthropic/claude-opus-5", "pattern")
        # With no brain to prefer, the OpenRouter id that also matches makes it ambiguous.
        assert _err(resolve_selection("opus", CAT, **kwargs)).code == "ambiguous"

    def test_a_loose_pattern_refuses_and_lists_candidates(self):
        err = _err(resolve_selection("glm", CAT, aliases=NONE, allow_patterns=True))
        assert err.code == "ambiguous"
        assert set(err.candidates) == {"openrouter/z-ai/glm-5", "openrouter/z-ai/glm-5-air"}

    def test_a_pattern_matching_one_model(self):
        r = _ok(resolve_selection("air", CAT, aliases=NONE, allow_patterns=True))
        assert r.ref == "openrouter/z-ai/glm-5-air"

    def test_enabled_only_narrows_patterns_to_the_enabled_set(self):
        kwargs = dict(aliases=NONE, allow_patterns=True, enabled=["openrouter/z-ai/glm-5"])
        r = _ok(resolve_selection("glm", CAT, enabled_only=True, **kwargs))
        assert r.ref == "openrouter/z-ai/glm-5"
        assert _err(resolve_selection("glm", CAT, enabled_only=False, **kwargs)).code == "ambiguous"


class TestRefusals:
    def test_a_cli_brain_cannot_run_another_providers_model(self):
        err = _err(resolve_selection("openrouter/z-ai/glm-5", CAT, aliases=NONE, for_brain="claude_code"))
        assert err.code == "model_not_runnable"

    def test_native_runs_only_its_own_providers_models(self):
        kwargs = dict(aliases=NONE, for_brain="native", native_provider="openrouter")
        assert _ok(resolve_selection("openrouter/z-ai/glm-5", CAT, **kwargs))
        assert _err(resolve_selection("anthropic/claude-sonnet-5", CAT, **kwargs)).code == "model_not_runnable"

    def test_a_model_outside_the_enabled_set(self):
        kwargs = dict(aliases=NONE, enabled=["anthropic/*"])
        assert _err(resolve_selection("openrouter/z-ai/glm-5", CAT, enabled_only=True, **kwargs)).code == "model_not_enabled"
        assert _ok(resolve_selection("openrouter/z-ai/glm-5", CAT, enabled_only=False, **kwargs))

    def test_an_alias_is_always_selectable(self):
        table = parse_alias_overrides({"cheap": "openrouter/z-ai/glm-5"}, log=False)
        r = resolve_selection("cheap", CAT, aliases=table, enabled_only=True, enabled=["anthropic/*"])
        assert _ok(r).ref == "openrouter/z-ai/glm-5"

    def test_a_model_without_tools_is_refused_for_an_agent_task(self):
        err = _err(resolve_selection("openrouter/acme/no-tools", CAT, aliases=NONE, needs_tools=True))
        assert err.code == "no_tools"
        assert _ok(resolve_selection("openrouter/acme/no-tools", CAT, aliases=NONE))


class TestTheEnabledSet:
    def test_globs_over_refs_with_deprecated_named_only_exactly(self):
        refs = enabled_refs(CAT, ["anthropic/claude-opus-*:high"])
        assert "anthropic/claude-opus-5" in refs and "anthropic/claude-opus-3" not in refs
        assert enabled_refs(CAT, ["anthropic/claude-opus-3"]) == ("anthropic/claude-opus-3",)

    def test_unmatched_patterns(self):
        assert unmatched_patterns(CAT, ["anthropic/*", "nobody/*"]) == ["nobody/*"]

    def test_the_default_is_anthropic_plus_natives_model(self):
        from istota.config import BrainConfig, Config, NativeBrainConfig

        config = Config()
        assert default_enabled_patterns(config) == ["anthropic/*"]
        config.brain = BrainConfig(native=NativeBrainConfig(provider="openrouter", model="z-ai/glm-5:high"))
        assert default_enabled_patterns(config) == ["anthropic/*", "openrouter/z-ai/glm-5"]


class TestBrainCanRun:
    def test_the_cli_brain_runs_anthropic_only(self):
        assert brain_can_run("claude_code", "anthropic/claude-opus-5")
        assert not brain_can_run("claude_code", "openrouter/z-ai/glm-5")

    def test_native_runs_its_own_provider(self):
        assert brain_can_run("native", "openrouter/x", native_provider="openrouter")
        assert not brain_can_run("native", "anthropic/x", native_provider="openrouter")
        assert brain_can_run("native", "anthropic/x", native_provider="anthropic")
        assert not brain_can_run("native", "anthropic/x")

    def test_an_unconfigured_provider_runs_nowhere(self):
        assert not brain_can_run("native", "local/x", native_provider="local")
        assert brain_can_run("native", "local/x", {"local": "openai_compat"}, native_provider="local")
        assert not brain_can_run("claude_code", "bare-id")


class TestTheBrainsDelegate:
    """The brains resolve through the resolver and stay lenient: what it cannot
    place is sent as written, as before."""

    def test_claude_code_reads_the_live_catalogue(self, monkeypatch):
        from istota.brain import _resolve
        from istota.brain.claude_code import ClaudeCodeBrain

        newer = build_catalogue(
            shipped=SHIPPED,
            discovered={"anthropic": (_m("claude-opus-6", "anthropic", "opus", "2026-12-01"),)},
        )
        monkeypatch.setattr(_resolve, "current_catalogue", lambda: newer)
        brain = ClaudeCodeBrain()
        assert brain.resolve_alias("smart:high") == ("claude-opus-6", "high")
        assert brain.resolve_model_name("anthropic/claude-sonnet-5") == "claude-sonnet-5"
        # Not listed, Claude-shaped: sent as written. Not runnable: unchanged too.
        assert brain.resolve_model_name("claude-opus-4-1-20250805") == "claude-opus-4-1-20250805"
        assert brain.resolve_alias("openrouter/z-ai/glm-5") is None

    def test_native_strips_only_its_own_providers_prefix(self, monkeypatch):
        from istota.brain import _resolve
        from istota.brain.native import NativeBrain
        from istota.config import NativeBrainConfig
        from istota.llm.providers import ProviderSpec

        monkeypatch.setattr(_resolve, "current_catalogue", lambda: CAT)
        spec = ProviderSpec(id="openrouter", kind="openrouter", base_url="https://openrouter.ai/api/v1")
        brain = NativeBrain(NativeBrainConfig(model="z-ai/glm-5"), provider=object(), spec=spec)
        assert brain.resolve_alias("openrouter/z-ai/glm-5:high") == ("z-ai/glm-5", "high")
        # OpenRouter's own id, and a model of another provider: as written.
        assert brain.resolve_model_name("anthropic/claude-opus-5") == "anthropic/claude-opus-5"
        assert brain.resolve_model_name("anthropic/claude-sonnet-5") == "anthropic/claude-sonnet-5"
        assert brain.resolve_model_name("general") == "z-ai/glm-5"

    def test_a_bare_claude_id_on_native_keeps_its_metadata(self):
        """The wire id is what native asks the catalogue about, so a bare
        Claude id on an `anthropic` native provider still gets the default
        record, and with it the wire features it had (no strict schemas,
        no reasoning_effort, no vision)."""
        from istota.brain.native import NativeBrain
        from istota.config import NativeBrainConfig
        from istota.llm.catalog import get_model_info
        from istota.llm.providers import ProviderSpec

        spec = ProviderSpec(id="anthropic", kind="anthropic", base_url="https://api.anthropic.com")
        brain = NativeBrain(NativeBrainConfig(model="claude-sonnet-5-5"), provider=object(), spec=spec)
        wire = brain.resolve_model_name("claude-sonnet-5-5")
        assert wire == "claude-sonnet-5-5"
        info = get_model_info(wire)
        assert not info.supports_structured_outputs and not info.supports_thinking
