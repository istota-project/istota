"""Model resolution for the native brain.

The only provider is ``openai_compat``, whose endpoint may be anything (LM
Studio, Ollama, vLLM, OpenRouter, Anthropic), so Anthropic aliases like ``opus``
MUST NOT be translated to a ``claude-*`` id and shipped to a non-Anthropic
endpoint. Explicit ids pass through; only operator ``[models.roles]`` overrides
resolve.
"""

from istota.brain._roles import set_role_overrides
from istota.brain.native import NativeBrain
from istota.config import NativeBrainConfig


def _brain(provider, model=""):
    return NativeBrain(
        NativeBrainConfig(provider=provider, model=model), provider=object()
    )


class TestOpenAICompatResolution:
    def teardown_method(self):
        set_role_overrides({})

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
        set_role_overrides({"smart": "qwen/qwen3.6-35b-a3b"})
        b = _brain("openai_compat")
        assert b.resolve_model_name("smart") == "qwen/qwen3.6-35b-a3b"

    def test_no_anthropic_aliases_listed(self):
        b = _brain("openai_compat")
        names = [a[0] for a in b.list_aliases()]
        assert "opus" not in names
        assert "haiku" not in names

    def test_role_override_listed(self):
        set_role_overrides({"smart": "qwen-x"})
        b = _brain("openai_compat")
        assert ("smart", "qwen-x", None) in b.list_aliases()

    def test_resolve_alias_only_for_overrides(self):
        set_role_overrides({"fast": "tiny-model"})
        b = _brain("openai_compat")
        assert b.resolve_alias("fast") == ("tiny-model", None)
        assert b.resolve_alias("opus") is None


class TestNativeRoleDefaults:
    """NB-3: built-in role aliases (fast/general/smart) must resolve to the
    configured native model rather than reaching the wire as the literal string
    'general' — stock config sets extraction_model/curation_model='general'."""

    def teardown_method(self):
        set_role_overrides({})

    def test_general_resolves_to_native_model(self):
        b = _brain("openai_compat", model="qwen/qwen3.6-35b")
        assert b.resolve_model_name("general") == "qwen/qwen3.6-35b"

    def test_fast_and_smart_resolve_to_native_model(self):
        b = _brain("openai_compat", model="local-model")
        assert b.resolve_model_name("fast") == "local-model"
        assert b.resolve_model_name("smart") == "local-model"

    def test_role_override_still_wins_over_native_default(self):
        set_role_overrides({"general": "big-model"})
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
