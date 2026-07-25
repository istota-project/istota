"""Cross-provider role contract (brain-fallback spec, Stage 2).

The enforceable form of "portable role tiers survive the provider boundary":
every registered brain MUST resolve every canonical role tier to a real,
non-empty model in its own namespace. If a new brain is added that can't, this
test fails — the fallback path relies on it.
"""

import pytest

from istota.brain import KNOWN_BRAIN_KINDS
from istota.brain._aliases import CANONICAL_ROLES
from istota.brain._roles import set_alias_overrides
from istota.brain.claude_code import ClaudeCodeBrain
from istota.brain.native import NativeBrain
from istota.brain.tmux_claude import TmuxClaudeBrain
from istota.config import NativeBrainConfig


def _make_brain(kind):
    """Construct each brain with the minimal config needed to resolve roles."""
    if kind == "claude_code":
        return ClaudeCodeBrain()
    if kind == "native":
        # Native speaks to one endpoint/model, so roles resolve to it.
        return NativeBrain(NativeBrainConfig(model="test-endpoint-model"))
    if kind == "tmux_claude":
        return TmuxClaudeBrain()
    raise AssertionError(f"unhandled brain kind {kind!r}")


class TestRoleContract:
    def test_all_kinds_covered(self):
        # Guard: if a new brain kind is registered, this test must construct it.
        assert set(KNOWN_BRAIN_KINDS) == {"claude_code", "native", "tmux_claude"}

    @pytest.mark.parametrize("kind", sorted(KNOWN_BRAIN_KINDS))
    @pytest.mark.parametrize("role", CANONICAL_ROLES)
    def test_every_brain_resolves_every_canonical_role(self, kind, role):
        brain = _make_brain(kind)
        resolved = brain.resolve_model_name(role)
        assert resolved, f"{kind} resolved role {role!r} to empty"
        # It resolved to a real model id, not the literal role string.
        assert resolved.lower() != role, (
            f"{kind} passed the literal role {role!r} through instead of resolving it"
        )


class TestModelNamespace:
    def test_each_brain_declares_a_namespace(self):
        assert ClaudeCodeBrain().model_namespace == "anthropic"
        assert TmuxClaudeBrain().model_namespace == "anthropic"
        assert NativeBrain(NativeBrainConfig(model="m")).model_namespace == "openai_compat"

    def test_cli_brains_share_a_namespace_native_differs(self):
        cli = ClaudeCodeBrain().model_namespace
        tmux = TmuxClaudeBrain().model_namespace
        native = NativeBrain(NativeBrainConfig(model="m")).model_namespace
        assert cli == tmux
        assert native != cli


class TestSharedDefaultBlockResolves:
    """Given the shipped per-namespace default block, every brain resolves every
    canonical role in its own namespace to a valid, non-role model string."""

    def teardown_method(self):
        set_alias_overrides({})

    def test_default_block_resolves_per_namespace(self):
        set_alias_overrides(
            {
                "smart": {
                    "anthropic": "opus:high",
                    "openai_compat": {"model": "anthropic/claude-opus-4.8", "effort": "high"},
                },
                "general": {
                    "anthropic": "claude-sonnet-5",
                    "openai_compat": "anthropic/claude-sonnet-4.6",
                },
                "fast": {
                    "anthropic": "claude-haiku-4-5",
                    "openai_compat": "anthropic/claude-haiku-4.5",
                },
            }
        )
        cli = ClaudeCodeBrain()
        native = NativeBrain(NativeBrainConfig(model="native-floor"))
        # claude_code resolves the anthropic slugs to canonical ids.
        assert cli.resolve_model_name("smart") == "claude-opus-4-8"
        assert cli.resolve_alias("smart")[1] == "high"
        # native resolves its own openai_compat slugs verbatim.
        assert native.resolve_model_name("smart") == "anthropic/claude-opus-4.8"
        assert native.resolve_alias("smart") == ("anthropic/claude-opus-4.8", "high")
        assert native.resolve_model_name("general") == "anthropic/claude-sonnet-4.6"
