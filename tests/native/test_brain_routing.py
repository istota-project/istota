"""Per-source-type brain routing — the gradual-rollout coexistence knob.

``[brain.source_type_overrides]`` maps a task's ``source_type`` to a brain
kind, overriding the instance default ``[brain] kind``. This lets an operator
move cron/heartbeat tasks to the native brain while interactive (talk/email)
tasks stay on ``claude_code`` — without touching the executor or the DB.
"""

import io
import logging

from istota import brain as brain_mod
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


class TestRefusalWarningsSayItOnce:
    """A refused pin is a static fact, so it is logged once per process.

    The condition depends on the stored pin and the operator's config, neither
    of which changes between calls, so an undeduped WARNING repeats at caller
    rate: roughly 1440 lines a day for a ``* * * * *`` cron job with a refused
    pin, times however many times one task resolves its brain (ISSUE-422). The
    *first* line is the point and is kept; only the repeat is the bug.
    """

    def test_an_unlisted_pin_warns_once_however_often_it_is_resolved(self, caplog):
        base = _cfg("claude_code")  # room_selectable is empty
        with caplog.at_level(logging.WARNING, logger="istota.brain"):
            for _ in range(5):
                assert resolve_brain_kind("scheduled", base, override="native") is base
        refusals = [r for r in caplog.records if "room_selectable" in r.getMessage()]
        assert len(refusals) == 1

    def test_an_unknown_pin_warns_once(self, caplog):
        base = _cfg("claude_code")
        with caplog.at_level(logging.WARNING, logger="istota.brain"):
            for _ in range(5):
                assert resolve_brain_kind("talk", base, override="claude-kode") is base
        refusals = [r for r in caplog.records if "unknown kind" in r.getMessage()]
        assert len(refusals) == 1

    def test_an_unknown_override_target_warns_once(self, caplog):
        base = _cfg("claude_code", {"scheduled": "bogus"})
        with caplog.at_level(logging.WARNING, logger="istota.brain"):
            for _ in range(5):
                assert resolve_brain_kind("scheduled", base) is base
        assert len([r for r in caplog.records if "bogus" in r.getMessage()]) == 1

    def test_each_distinct_refusal_still_gets_its_own_line(self, caplog):
        base = _cfg("claude_code")
        with caplog.at_level(logging.WARNING, logger="istota.brain"):
            resolve_brain_kind("scheduled", base, override="native")
            resolve_brain_kind("talk", base, override="native")
            resolve_brain_kind("scheduled", base, override="tmux_claude")
        refusals = [r for r in caplog.records if "room_selectable" in r.getMessage()]
        assert len(refusals) == 3

    def test_a_pin_and_an_override_target_of_the_same_name_both_warn(self, caplog):
        """One call, two refusals, one name: neither may shadow the other.

        A refused pin falls through to the source-type layer, so a job pinned
        to ``bogus`` on a lane whose override is also ``bogus`` refuses twice
        in a single call — two conditions with two different remedies, which is
        why which arm refused is part of what is remembered.
        """
        base = _cfg("claude_code", {"scheduled": "bogus"})
        with caplog.at_level(logging.WARNING, logger="istota.brain"):
            assert resolve_brain_kind("scheduled", base, override="bogus") is base
        assert len(caplog.records) == 2

    def test_the_key_set_is_bounded(self, caplog):
        """``pinned`` comes off a row CRON.md can write.

        An unbounded map is one durable entry per value anybody cares to write,
        so the set stops growing — and says so, rather than going quiet with no
        explanation.
        """
        base = _cfg("claude_code")
        with caplog.at_level(logging.WARNING, logger="istota.brain"):
            for i in range(brain_mod._WARNED_REFUSAL_CAP + 20):
                resolve_brain_kind("scheduled", base, override=f"kind-{i}")
        assert len(brain_mod._WARNED_REFUSALS) <= brain_mod._WARNED_REFUSAL_CAP + 1
        notices = [r for r in caplog.records if "suppressing" in r.getMessage()]
        assert len(notices) == 1

    def test_a_long_pin_is_bounded_in_the_key_and_in_the_line(self, caplog):
        """The count is not a memory bound on its own: each key has to be small.

        ``scheduled_jobs.brain`` is a plain string field with no length cap, so
        a bounded number of unbounded keys is still unbounded, and a bounded
        number of unbounded log lines still fills a disk.
        """
        base = _cfg("claude_code")
        with caplog.at_level(logging.WARNING, logger="istota.brain"):
            assert resolve_brain_kind("scheduled", base, override="n" * 5000) is base
        widest = max(len(part) for key in brain_mod._WARNED_REFUSALS for part in key)
        assert widest <= brain_mod._REFUSAL_SHOWN_CHARS
        assert len(caplog.records) == 1
        assert len(caplog.records[0].getMessage()) < 400

    def test_a_non_string_override_target_warns_rather_than_raising(self, caplog):
        """A routing typo must not become a raised exception.

        `[brain.source_type_overrides]` values are stringified by the config
        hook, but only on the `load_config` path — a `BrainConfig` built any
        other way carries whatever TOML can spell, and `execute_task` calls
        this unguarded. An unhashable target used to raise `TypeError` out of
        the membership test, which is the same concession
        `reachable_brain_kinds` already makes for its own read of this mapping.
        """
        base = _cfg("claude_code", {"scheduled": 5, "talk": ["native"]})
        with caplog.at_level(logging.WARNING, logger="istota.brain"):
            for _ in range(3):
                assert resolve_brain_kind("scheduled", base) is base
                assert resolve_brain_kind("talk", base) is base
        assert len(caplog.records) == 2

    def test_a_non_string_source_type_warns_rather_than_raising(self, caplog):
        """`source_type` came off a task row and was coerced by nothing here.

        It reached `(source_type or "").strip()` as whatever it was, so a
        non-string raised `AttributeError` — after the pin refusal above it
        had already warned, so the operator got a warning for a call that then
        failed anyway.
        """
        base = _cfg("claude_code")
        with caplog.at_level(logging.WARNING, logger="istota.brain"):
            for _ in range(3):
                assert resolve_brain_kind(7, base, override="native") is base
        assert len(caplog.records) == 1

    def test_the_key_truncation_does_not_reach_the_override_lookup(self):
        """A lookup key is matched whole, never cut down to the key bound.

        The override is deliberately keyed on the *truncated* spelling, which
        is the only name a truncating lookup could match and a name the
        `source_type` does not carry.
        """
        long_source = "s" * 200
        base = _cfg("claude_code", {brain_mod._shown(long_source): "native"})
        assert resolve_brain_kind(long_source, base) is base
