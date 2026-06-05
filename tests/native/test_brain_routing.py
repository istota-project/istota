"""Per-source-type brain routing — the gradual-rollout coexistence knob.

``[brain.source_type_overrides]`` maps a task's ``source_type`` to a brain
kind, overriding the instance default ``[brain] kind``. This lets an operator
move cron/heartbeat tasks to the native brain while interactive (talk/email)
tasks stay on ``claude_code`` — without touching the executor or the DB.
"""

import io
import logging

from istota.brain import KNOWN_BRAIN_KINDS, make_brain, resolve_brain_kind
from istota.brain.claude_code import ClaudeCodeBrain
from istota.brain.native import NativeBrain
from istota.config import BrainConfig, NativeBrainConfig, load_config


def _cfg(kind="claude_code", overrides=None):
    return BrainConfig(
        kind=kind,
        native=NativeBrainConfig(model="claude-sonnet-4-6"),
        source_type_overrides=overrides or {},
    )


class TestResolveBrainKind:
    def test_no_overrides_returns_base(self):
        base = _cfg("claude_code")
        assert resolve_brain_kind("scheduled", base) is base

    def test_override_swaps_kind(self):
        base = _cfg("claude_code", {"scheduled": "native"})
        resolved = resolve_brain_kind("scheduled", base)
        assert resolved.kind == "native"
        # The native sub-config is carried through untouched.
        assert resolved.native.model == "claude-sonnet-4-6"

    def test_non_matching_source_type_returns_base(self):
        base = _cfg("claude_code", {"scheduled": "native"})
        assert resolve_brain_kind("talk", base) is base

    def test_override_equal_to_base_is_noop(self):
        base = _cfg("native", {"scheduled": "native"})
        assert resolve_brain_kind("scheduled", base) is base

    def test_none_source_type_returns_base(self):
        base = _cfg("claude_code", {"scheduled": "native"})
        assert resolve_brain_kind(None, base) is base

    def test_source_type_is_stripped(self):
        base = _cfg("claude_code", {"scheduled": "native"})
        assert resolve_brain_kind("  scheduled  ", base).kind == "native"

    def test_unknown_target_kind_falls_back_and_warns(self):
        base = _cfg("claude_code", {"scheduled": "bogus"})
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("istota.brain")
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            resolved = resolve_brain_kind("scheduled", base)
        finally:
            logger.removeHandler(handler)
        assert resolved is base  # never wedge a task on a routing typo
        assert "bogus" in stream.getvalue()

    def test_known_kinds_registry(self):
        assert "claude_code" in KNOWN_BRAIN_KINDS
        assert "native" in KNOWN_BRAIN_KINDS


class TestRoutingThroughFactory:
    def test_routed_scheduled_builds_native(self):
        base = _cfg("claude_code", {"scheduled": "native"})
        brain = make_brain(resolve_brain_kind("scheduled", base))
        assert isinstance(brain, NativeBrain)

    def test_routed_interactive_stays_claude_code(self):
        base = _cfg("claude_code", {"scheduled": "native"})
        brain = make_brain(resolve_brain_kind("talk", base))
        assert isinstance(brain, ClaudeCodeBrain)


class TestConfigParsing:
    def test_source_type_overrides_parsed(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            "[brain]\n"
            'kind = "claude_code"\n'
            "[brain.source_type_overrides]\n"
            'scheduled = "native"\n'
            'heartbeat = "native"\n'
        )
        config = load_config(cfg_file)
        assert config.brain.source_type_overrides == {
            "scheduled": "native",
            "heartbeat": "native",
        }

    def test_missing_overrides_defaults_empty(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text('[brain]\nkind = "native"\n')
        config = load_config(cfg_file)
        assert config.brain.source_type_overrides == {}
