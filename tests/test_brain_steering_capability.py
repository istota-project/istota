"""Stage 2 of the !steer spec: every brain declares `supports_steering`."""

from istota.brain.claude_code import ClaudeCodeBrain
from istota.brain.native import NativeBrain
from istota.brain.tmux_claude import TmuxClaudeBrain
from istota.commands import _STEERABLE_KINDS


class TestSupportsSteering:
    def test_claude_code_false(self):
        assert ClaudeCodeBrain().supports_steering is False

    def test_tmux_true(self):
        # Truthful even though the paste wiring is post-v1.
        assert TmuxClaudeBrain().supports_steering is True

    def test_native_true(self):
        from istota.config import BrainConfig, NativeBrainConfig

        cfg = BrainConfig(kind="native", native=NativeBrainConfig())
        brain = NativeBrain(cfg, provider=object())
        assert brain.supports_steering is True

    def test_every_brain_declares_the_property(self):
        # Contract: the property must exist and be a bool on each brain.
        from istota.config import BrainConfig, NativeBrainConfig

        native = NativeBrain(
            BrainConfig(kind="native", native=NativeBrainConfig()), provider=object()
        )
        for brain in (ClaudeCodeBrain(), TmuxClaudeBrain(), native):
            assert isinstance(brain.supports_steering, bool)


class TestSteerableKinds:
    def test_v1_native_only(self):
        assert _STEERABLE_KINDS == frozenset({"native"})
