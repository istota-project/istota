"""Tests for TmuxClaudeBrain — the tmux-driven interactive-TUI brain.

Wiring + routing + delegated model resolution (Stage 0). The full execute()
flow is covered in tests/test_tmux_execute.py; transcript parsing in
tests/test_tmux_transcript.py. See
``Specs/Active/tmux-subscription-brain-feasibility.md``.
"""


import pytest

from istota.brain import (
    Brain,
    BrainRequest,
    KNOWN_BRAIN_KINDS,
    make_brain,
    resolve_brain_kind,
)
from istota.brain.claude_code import ClaudeCodeBrain
from istota.brain.tmux_claude import TmuxClaudeBrain
from istota.config import BrainConfig, NativeBrainConfig


@pytest.fixture(autouse=True)
def _skip_version_probe(monkeypatch):
    import istota.brain.tmux_claude as mod
    monkeypatch.setattr(mod, "_VERSION_CHECKED", True)


def _cfg(kind="claude_code", overrides=None):
    return BrainConfig(
        kind=kind,
        native=NativeBrainConfig(model="claude-sonnet-4-6"),
        source_type_overrides=overrides or {},
    )


class TestRegistration:
    def test_tmux_claude_in_known_kinds(self):
        assert "tmux_claude" in KNOWN_BRAIN_KINDS

    def test_make_brain_builds_tmux_claude(self):
        brain = make_brain(_cfg(kind="tmux_claude"))
        assert isinstance(brain, TmuxClaudeBrain)

    def test_satisfies_brain_protocol(self):
        brain: Brain = TmuxClaudeBrain()
        for method in (
            "execute",
            "resolve_alias",
            "resolve_model_name",
            "list_aliases",
            "validate_role_override",
        ):
            assert callable(getattr(brain, method))


class TestRouting:
    def test_override_routes_source_type_to_tmux(self):
        base = _cfg("claude_code", {"cron": "tmux_claude"})
        resolved = resolve_brain_kind("cron", base)
        assert resolved.kind == "tmux_claude"

    def test_routed_build_produces_tmux_brain(self):
        base = _cfg("claude_code", {"cron": "tmux_claude"})
        brain = make_brain(resolve_brain_kind("cron", base))
        assert isinstance(brain, TmuxClaudeBrain)

    def test_non_matching_source_type_stays_base(self):
        base = _cfg("claude_code", {"cron": "tmux_claude"})
        assert resolve_brain_kind("talk", base) is base


class TestModelResolutionDelegation:
    """Resolution is delegated wholesale to a composed ClaudeCodeBrain — same
    CLI, same Anthropic model namespace. Results must match exactly."""

    def setup_method(self):
        self.brain = TmuxClaudeBrain()
        self.reference = ClaudeCodeBrain()

    def test_resolve_alias_matches_claude_code(self):
        for alias in ("opus", "opus-high", "sonnet", "haiku", "smart", "bogus"):
            assert self.brain.resolve_alias(alias) == self.reference.resolve_alias(alias)

    def test_resolve_model_name_matches_claude_code(self):
        for name in ("opus", "smart", "claude-opus-4-8", "", None):
            assert self.brain.resolve_model_name(name) == self.reference.resolve_model_name(name)

    def test_list_aliases_matches_claude_code(self):
        assert self.brain.list_aliases() == self.reference.list_aliases()

    def test_validate_role_override_matches_claude_code(self):
        assert self.brain.validate_role_override("smart", "opus-high") == (
            self.reference.validate_role_override("smart", "opus-high")
        )


class TestExecuteImplemented:
    """execute() is implemented as of Stage 2 — full flow coverage lives in
    tests/test_tmux_execute.py. Here we only assert it no longer stubs out."""

    def test_execute_does_not_raise_not_implemented(self, monkeypatch):
        import istota.brain.tmux_claude as mod
        # No tmux on PATH → graceful not_found result, never NotImplementedError.
        monkeypatch.setattr(mod.shutil, "which", lambda _: None)
        req = BrainRequest(
            prompt="hi",
            allowed_tools=[],
            cwd=__import__("pathlib").Path("/tmp"),
            env={},
            timeout_seconds=60,
        )
        res = TmuxClaudeBrain().execute(req)
        assert res.stop_reason == "not_found"
        assert res.success is False
