"""Provider-aware model resolution for the native brain.

When the native provider is ``claude_code`` (the CLI as a bare inference
endpoint) the Anthropic alias table applies. When it's ``openai_compat`` the
endpoint may be anything (LM Studio, Ollama, vLLM, OpenRouter), so Anthropic
aliases like ``opus`` MUST NOT be translated to a ``claude-*`` id and shipped to
a non-Anthropic endpoint. Explicit ids pass through; only operator
``[models.roles]`` overrides resolve.
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


class TestClaudeCodeProviderResolution:
    def test_aliases_still_resolve_under_claude_code_provider(self):
        b = _brain("claude_code")
        # The claude CLI inference path keeps the Anthropic alias table.
        assert b.resolve_model_name("opus").startswith("claude-opus")
        names = [a[0] for a in b.list_aliases()]
        assert "opus" in names
