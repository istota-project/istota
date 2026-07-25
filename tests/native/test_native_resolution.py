"""Model resolution for the native brain.

The only provider is ``openai_compat``, whose endpoint may be anything (LM
Studio, Ollama, vLLM, OpenRouter, Anthropic), so Anthropic aliases like ``opus``
MUST NOT be translated to a ``claude-*`` id and shipped to a non-Anthropic
endpoint. Explicit ids pass through; only operator ``[models.aliases]`` overrides
resolve.
"""

from istota.brain._roles import set_alias_overrides
from istota.brain.native import NativeBrain
from istota.config import NativeBrainConfig


def _brain(provider, model=""):
    return NativeBrain(
        NativeBrainConfig(provider=provider, model=model), provider=object()
    )


class TestOpenAICompatResolution:
    def teardown_method(self):
        set_alias_overrides({})

    def test_alias_not_translated_to_anthropic(self):
        b = _brain("openai_compat")
        # "opus" must NOT become "claude-opus-4-8" for a non-Anthropic endpoint.
        assert b.resolve_model_name("opus") == "opus"

    def test_explicit_id_passes_through(self):
        b = _brain("openai_compat")
        assert b.resolve_model_name("qwen/qwen3.6-35b-a3b") == "qwen/qwen3.6-35b-a3b"

    def test_empty_returns_empty(self):
        b = _brain("openai_compat")
        assert b.resolve_model_name("") == ""
        assert b.resolve_model_name(None) == ""

    def test_role_override_resolves(self):
        set_alias_overrides({"smart": "qwen/qwen3.6-35b-a3b"})
        b = _brain("openai_compat")
        assert b.resolve_model_name("smart") == "qwen/qwen3.6-35b-a3b"

    def test_no_anthropic_aliases_listed(self):
        b = _brain("openai_compat")
        names = [a[0] for a in b.list_aliases()]
        assert "opus" not in names
        assert "haiku" not in names

    def test_role_override_listed(self):
        set_alias_overrides({"smart": "qwen-x"})
        b = _brain("openai_compat")
        assert ("smart", "qwen-x", None) in b.list_aliases()

    def test_resolve_alias_only_for_overrides(self):
        set_alias_overrides({"fast": "tiny-model"})
        b = _brain("openai_compat")
        assert b.resolve_alias("fast") == ("tiny-model", None)
        assert b.resolve_alias("opus") is None


class TestPerNamespaceOverrides:
    """The core cross-namespace bug fix: native reads its OWN namespace value,
    never the anthropic one, so a foreign-namespace string never hits the wire."""

    def teardown_method(self):
        set_alias_overrides({})

    def test_native_reads_openai_compat_key_not_anthropic(self):
        # Operator defines smart once, per namespace. Native must resolve to the
        # openai_compat slug, NOT the anthropic alias (the shipped-feature bug).
        set_alias_overrides(
            {
                "smart": {
                    "anthropic": "opus-46-high",
                    "openai_compat": "anthropic/claude-opus-4.8",
                }
            }
        )
        b = _brain("openai_compat", model="fallback-model")
        assert b.resolve_model_name("smart") == "anthropic/claude-opus-4.8"
        assert b.resolve_alias("smart") == ("anthropic/claude-opus-4.8", None)

    def test_native_openai_compat_carries_effort(self):
        set_alias_overrides(
            {"smart": {"openai_compat": {"model": "anthropic/claude-opus-4.8", "effort": "high"}}}
        )
        b = _brain("openai_compat", model="m")
        assert b.resolve_alias("smart") == ("anthropic/claude-opus-4.8", "high")

    def test_anthropic_only_override_falls_to_native_default(self):
        # A per-namespace table with only the anthropic key, running native →
        # native misses its namespace, no legacy, falls to its own model floor.
        set_alias_overrides({"smart": {"anthropic": "opus-high"}})
        b = _brain("openai_compat", model="the-native-model")
        assert b.resolve_model_name("smart") == "the-native-model"

    def test_legacy_flat_under_native_unchanged(self):
        # Documented no-regression edge: a flat value is namespace-agnostic, so
        # native returns it verbatim (correct here — operators set native slugs).
        set_alias_overrides({"smart": "qwen/qwen3.6-35b-a3b"})
        b = _brain("openai_compat", model="m")
        assert b.resolve_alias("smart") == ("qwen/qwen3.6-35b-a3b", None)

    def test_list_aliases_reflects_openai_compat_value(self):
        set_alias_overrides(
            {"smart": {"anthropic": "opus-high", "openai_compat": "slug/x"}}
        )
        b = _brain("openai_compat", model="m")
        listed = {a[0]: (a[1], a[2]) for a in b.list_aliases()}
        assert listed["smart"] == ("slug/x", None)


class TestNativeRoleDefaults:
    """NB-3: built-in role aliases (fast/general/smart) must resolve to the
    configured native model rather than reaching the wire as the literal string
    'general' — stock config sets extraction_model/curation_model='general'."""

    def teardown_method(self):
        set_alias_overrides({})

    def test_general_resolves_to_native_model(self):
        b = _brain("openai_compat", model="qwen/qwen3.6-35b")
        assert b.resolve_model_name("general") == "qwen/qwen3.6-35b"

    def test_fast_and_smart_resolve_to_native_model(self):
        b = _brain("openai_compat", model="local-model")
        assert b.resolve_model_name("fast") == "local-model"
        assert b.resolve_model_name("smart") == "local-model"

    def test_role_override_still_wins_over_native_default(self):
        set_alias_overrides({"general": "big-model"})
        b = _brain("openai_compat", model="small-model")
        assert b.resolve_model_name("general") == "big-model"

    def test_role_name_never_reaches_wire_even_with_empty_model(self):
        # A misconfigured (empty) native model must not leak 'general' as an id.
        b = _brain("openai_compat", model="")
        assert b.resolve_model_name("general") == ""

    def test_explicit_id_still_passes_through(self):
        b = _brain("openai_compat", model="qwen/qwen3.6-35b")
        assert b.resolve_model_name("some/other-model") == "some/other-model"
        # And a provider alias like opus still is NOT translated.
        assert b.resolve_model_name("opus") == "opus"

    def test_role_defaults_listed(self):
        b = _brain("openai_compat", model="local-model")
        listed = {a[0]: a[1] for a in b.list_aliases()}
        assert listed.get("general") == "local-model"
        assert listed.get("fast") == "local-model"
        assert listed.get("smart") == "local-model"
