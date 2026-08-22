"""``[brain.claude_code]`` — parse, defaults and loader validation.

The block exists for the subscription usage poll (subscription-usage-stats
spec, Stage 2). Every field is defaulted, so an absent block is the shipping
behaviour; the loader corrects a configuration that would make the feature
misbehave rather than refusing to load.

The last class here is the anti-drift pin. ``subscription_usage.py`` is a
stdlib-only leaf and reads these settings defensively via ``getattr``, so it
carries its own copy of the defaults it uses. That copy and this dataclass are
the same numbers, and this is what keeps them so.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import textwrap

from istota import subscription_usage as su
from istota.config import ClaudeCodeBrainConfig, load_config


def _load(tmp_path, body: str):
    cfg = tmp_path / "config.toml"
    cfg.write_text(textwrap.dedent(body))
    return load_config(cfg)


class TestDefaults:
    """An absent block is the documented shipping behaviour."""

    def test_absent_block_yields_the_documented_defaults(self, tmp_path):
        config = _load(tmp_path, '[brain]\nkind = "claude_code"\n')
        cc = config.brain.claude_code
        assert cc.subscription_usage is True
        assert cc.subscription_usage_cache_ttl_seconds == 300
        assert cc.subscription_usage_timeout_seconds == 10.0
        assert cc.subscription_usage_warn_percent == 80.0
        assert cc.subscription_usage_high_percent == 95.0
        assert cc.subscription_usage_stale_after_seconds == 3600

    def test_absent_block_equals_the_dataclass(self, tmp_path):
        """The loader must agree with the dataclass field by field.

        Same failure mode `fallback_on_transient` had: a key omitted from TOML
        resolving to something other than the dataclass default.
        """
        config = _load(tmp_path, '[brain]\nkind = "claude_code"\n')
        assert config.brain.claude_code == ClaudeCodeBrainConfig()

    def test_no_brain_section_at_all(self, tmp_path):
        config = _load(tmp_path, "[bot]\nname = \"Istota\"\n")
        assert config.brain.claude_code == ClaudeCodeBrainConfig()

    def test_empty_block(self, tmp_path):
        config = _load(tmp_path, '[brain]\nkind = "claude_code"\n\n[brain.claude_code]\n')
        assert config.brain.claude_code == ClaudeCodeBrainConfig()


class TestParse:
    def test_every_field_parses(self, tmp_path):
        config = _load(tmp_path, """
            [brain]
            kind = "claude_code"

            [brain.claude_code]
            subscription_usage = false
            subscription_usage_cache_ttl_seconds = 60
            subscription_usage_timeout_seconds = 4.5
            subscription_usage_warn_percent = 70
            subscription_usage_high_percent = 90
            subscription_usage_stale_after_seconds = 900
        """)
        cc = config.brain.claude_code
        assert cc.subscription_usage is False
        assert cc.subscription_usage_cache_ttl_seconds == 60
        assert cc.subscription_usage_timeout_seconds == 4.5
        assert cc.subscription_usage_warn_percent == 70.0
        assert cc.subscription_usage_high_percent == 90.0
        assert cc.subscription_usage_stale_after_seconds == 900

    def test_integer_percentages_become_floats(self, tmp_path):
        """TOML `70` and `70.0` must not produce different types."""
        config = _load(tmp_path, """
            [brain.claude_code]
            subscription_usage_warn_percent = 70
        """)
        assert isinstance(config.brain.claude_code.subscription_usage_warn_percent, float)

    def test_a_non_table_block_is_ignored(self, tmp_path):
        config = _load(tmp_path, '[brain]\nclaude_code = "yes"\n')
        assert config.brain.claude_code == ClaudeCodeBrainConfig()


class TestValidation:
    """The loader corrects, warns and carries on. It never refuses to load."""

    def test_percentages_clamp_to_the_top(self, tmp_path):
        config = _load(tmp_path, """
            [brain.claude_code]
            subscription_usage_warn_percent = 400
            subscription_usage_high_percent = 900
        """)
        cc = config.brain.claude_code
        assert cc.subscription_usage_warn_percent == 100.0
        assert cc.subscription_usage_high_percent == 100.0

    def test_percentages_clamp_to_the_bottom(self, tmp_path):
        config = _load(tmp_path, """
            [brain.claude_code]
            subscription_usage_warn_percent = -20
            subscription_usage_high_percent = -1
        """)
        cc = config.brain.claude_code
        assert cc.subscription_usage_warn_percent == 0.0
        assert cc.subscription_usage_high_percent == 0.0

    def test_an_inverted_pair_is_corrected_and_logged(self, tmp_path, caplog):
        """warn above high would make the amber band unreachable."""
        with caplog.at_level(logging.WARNING, logger="istota.config"):
            config = _load(tmp_path, """
                [brain.claude_code]
                subscription_usage_warn_percent = 95
                subscription_usage_high_percent = 80
            """)
        cc = config.brain.claude_code
        assert cc.subscription_usage_warn_percent == 80.0
        assert cc.subscription_usage_high_percent == 80.0
        messages = [r.getMessage() for r in caplog.records if r.name == "istota.config"]
        assert any("subscription_usage_warn_percent" in m for m in messages), messages

    def test_an_equal_pair_is_left_alone_and_silent(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="istota.config"):
            config = _load(tmp_path, """
                [brain.claude_code]
                subscription_usage_warn_percent = 90
                subscription_usage_high_percent = 90
            """)
        cc = config.brain.claude_code
        assert cc.subscription_usage_warn_percent == 90.0
        assert cc.subscription_usage_high_percent == 90.0
        assert not [
            r for r in caplog.records
            if r.name == "istota.config" and "subscription_usage" in r.getMessage()
        ]

    def test_a_valid_pair_is_left_alone_and_silent(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="istota.config"):
            config = _load(tmp_path, """
                [brain.claude_code]
                subscription_usage_warn_percent = 50
                subscription_usage_high_percent = 75
            """)
        cc = config.brain.claude_code
        assert cc.subscription_usage_warn_percent == 50.0
        assert cc.subscription_usage_high_percent == 75.0
        assert not [
            r for r in caplog.records
            if r.name == "istota.config" and "subscription_usage" in r.getMessage()
        ]

    def test_clamping_happens_before_the_inversion_check(self, tmp_path):
        """warn=150 against high=95 is an inversion once warn is clamped to 100."""
        config = _load(tmp_path, """
            [brain.claude_code]
            subscription_usage_warn_percent = 150
            subscription_usage_high_percent = 95
        """)
        cc = config.brain.claude_code
        assert cc.subscription_usage_warn_percent == 95.0
        assert cc.subscription_usage_high_percent == 95.0

    def test_non_finite_percentages_land_in_range(self, tmp_path):
        """TOML really does spell `inf` and `nan`, and both must land somewhere."""
        config = _load(tmp_path, """
            [brain.claude_code]
            subscription_usage_warn_percent = nan
            subscription_usage_high_percent = inf
        """)
        cc = config.brain.claude_code
        assert cc.subscription_usage_warn_percent == 0.0
        assert cc.subscription_usage_high_percent == 100.0

    def test_a_zero_ttl_is_floored(self, tmp_path):
        """A zero TTL would fetch on every dashboard poll."""
        config = _load(tmp_path, """
            [brain.claude_code]
            subscription_usage_cache_ttl_seconds = 0
        """)
        assert config.brain.claude_code.subscription_usage_cache_ttl_seconds == 1

    def test_a_negative_ttl_is_floored(self, tmp_path):
        config = _load(tmp_path, """
            [brain.claude_code]
            subscription_usage_cache_ttl_seconds = -300
        """)
        assert config.brain.claude_code.subscription_usage_cache_ttl_seconds == 1

    def test_a_zero_timeout_is_floored(self, tmp_path):
        config = _load(tmp_path, """
            [brain.claude_code]
            subscription_usage_timeout_seconds = 0
        """)
        assert config.brain.claude_code.subscription_usage_timeout_seconds == 1.0

    def test_a_sub_second_timeout_is_floored(self, tmp_path):
        config = _load(tmp_path, """
            [brain.claude_code]
            subscription_usage_timeout_seconds = 0.2
        """)
        assert config.brain.claude_code.subscription_usage_timeout_seconds == 1.0

    def test_disabling_the_poll_survives_validation(self, tmp_path):
        config = _load(tmp_path, """
            [brain.claude_code]
            subscription_usage = false
            subscription_usage_cache_ttl_seconds = 0
        """)
        cc = config.brain.claude_code
        assert cc.subscription_usage is False
        assert cc.subscription_usage_cache_ttl_seconds == 1


class TestOneSourceOfTruthForTheDefaults:
    """``subscription_usage.py`` keeps its own copy. It must be the same copy.

    The module is a stdlib-only leaf reached from the doctor/config-load side,
    so it must not import ``istota.config`` to learn these numbers, and it must
    behave as the shipping default against a ``Config`` that predates the block.
    That means two literals per value, and this is the test that makes them one
    value in practice.
    """

    def test_the_module_defaults_match_the_dataclass(self):
        defaults = ClaudeCodeBrainConfig()
        assert su.DEFAULT_SUBSCRIPTION_USAGE is defaults.subscription_usage
        assert su.DEFAULT_CACHE_TTL_SECONDS == defaults.subscription_usage_cache_ttl_seconds
        assert su.DEFAULT_TIMEOUT_SECONDS == defaults.subscription_usage_timeout_seconds

    def test_an_absent_block_reads_as_the_dataclass_defaults(self, tmp_path):
        """The defensive read and the typed read must agree on a real Config."""
        config = _load(tmp_path, '[brain]\nkind = "claude_code"\n')
        enabled, ttl, timeout = su._settings(config)
        defaults = ClaudeCodeBrainConfig()
        assert enabled is defaults.subscription_usage
        assert ttl == defaults.subscription_usage_cache_ttl_seconds
        assert timeout == defaults.subscription_usage_timeout_seconds

    def test_a_configured_block_reaches_the_module(self, tmp_path):
        config = _load(tmp_path, """
            [brain.claude_code]
            subscription_usage = false
            subscription_usage_cache_ttl_seconds = 60
            subscription_usage_timeout_seconds = 3
        """)
        assert su._settings(config) == (False, 60.0, 3.0)

    def test_an_infinite_timeout_never_reaches_the_fetch(self, tmp_path):
        """The loader floors at 1 but has no ceiling; the module's own guard has.

        `timeout = inf` is spellable in TOML and would otherwise become an
        unbounded socket timeout on the doctor path — the one thing this feature
        promises never to do.
        """
        config = _load(tmp_path, """
            [brain.claude_code]
            subscription_usage_timeout_seconds = inf
        """)
        assert config.brain.claude_code.subscription_usage_timeout_seconds == float("inf")
        _enabled, _ttl, timeout = su._settings(config)
        assert timeout == su.DEFAULT_TIMEOUT_SECONDS

    def test_config_does_not_import_the_leaf(self):
        """Keeping the copy is what keeps this true.

        ``istota.config`` is imported by every CLI invocation and every
        host-side skill CLI the proxy spawns per call. It must not grow an
        import of the usage module (and through it urllib/subprocess) just to
        read six default numbers. See ``TestConfigLoadPathStaysCheap`` in
        tests/test_doctor.py for the same rule stated for the forge checks.
        """
        code = (
            "import json, sys\n"
            "import istota.config\n"
            "print(json.dumps(sorted(sys.modules)))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        loaded = set(json.loads(out.stdout))
        assert "istota.subscription_usage" not in loaded
