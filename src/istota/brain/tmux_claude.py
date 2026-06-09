"""TmuxClaudeBrain — drives the *interactive* `claude` TUI via a detached tmux
session, instead of the headless `claude -p` subprocess `ClaudeCodeBrain` uses.

Why this exists: starting 2026-06-15, Anthropic meters headless/SDK usage
(`claude -p`, the Agent SDK, GitHub Actions) against a separate monthly Agent
SDK credit, while *interactive* terminal use keeps drawing from the normal
subscription limits. This brain is a feasibility prototype testing whether
driving the interactive TUI programmatically (prompt injection + a `Stop` hook
sentinel + transcript parsing) keeps Istota on subscription billing. See
``Specs/Active/tmux-subscription-brain-feasibility.md``.

Stage 0 (this file's current state) is scaffold only: it satisfies the Brain
protocol by delegating the four model-resolution methods wholesale to a composed
``ClaudeCodeBrain`` (same CLI, same Anthropic model namespace), and stubs
``execute`` with NotImplementedError. The real tmux flow lands in Stage 2.
"""

from ._types import BrainRequest, BrainResult
from .claude_code import ClaudeCodeBrain


class TmuxClaudeBrain:
    """Brain that drives the interactive `claude` TUI inside a tmux session.

    Model resolution is delegated to an internal ``ClaudeCodeBrain``: this brain
    runs the same `claude` CLI binary against the same Anthropic model namespace,
    so duplicating ``MODEL_ALIASES`` / ``DEFAULT_ROLE_TARGETS`` would only invite
    drift. Only ``execute`` is genuinely new.
    """

    def __init__(self) -> None:
        # Composed, not inherited: we forward the four resolution methods and
        # own execute. The CLI brain holds no per-instance state, so a fresh
        # one here is free.
        self._cli = ClaudeCodeBrain()

    # --- Model resolution (delegated to ClaudeCodeBrain) -------------------

    def resolve_alias(self, alias):
        return self._cli.resolve_alias(alias)

    def resolve_model_name(self, name):
        return self._cli.resolve_model_name(name)

    def list_aliases(self):
        return self._cli.list_aliases()

    def validate_role_override(self, role, target):
        return self._cli.validate_role_override(role, target)

    # --- Execution --------------------------------------------------------

    def execute(self, req: BrainRequest) -> BrainResult:
        raise NotImplementedError(
            "TmuxClaudeBrain.execute lands in Stage 2 of the "
            "tmux-subscription-brain feasibility study"
        )
