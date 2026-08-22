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
import math
import subprocess
import sys
import textwrap

import pytest

from istota import subscription_usage as su
from istota.config import (
    ClaudeCodeBrainConfig,
    Config,
    _validate_claude_code_brain,
    load_config,
)


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

    @pytest.mark.parametrize("literal,expected", [
        ("true", True), ("false", False),
        ('"true"', True), ('"false"', False),
        ('"yes"', True), ('"no"', False),
        ('"on"', True), ('"off"', False),
        ('"TRUE"', True), ('" False "', False),
    ])
    def test_the_switch_accepts_a_string_boolean(self, tmp_path, literal, expected):
        """`bool("false")` is True, and a rendered config can quote a boolean.

        This is the field that decides whether the deployment makes an
        unsolicited outbound request, so "operator wrote false, poll stayed on"
        is the one failure this parse must not have.
        """
        config = _load(tmp_path, f"""
            [brain.claude_code]
            subscription_usage = {literal}
        """)
        assert config.brain.claude_code.subscription_usage is expected

    def test_an_uninterpretable_switch_falls_back_and_warns(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="istota.config"):
            config = _load(tmp_path, """
                [brain.claude_code]
                subscription_usage = "maybe"
            """)
        assert config.brain.claude_code.subscription_usage is True
        assert any(
            "subscription_usage=" in r.getMessage() and "not a boolean" in r.getMessage()
            for r in caplog.records if r.name == "istota.config"
        )


class TestABadValueNeverStopsTheDaemon:
    """`load_config` runs in the scheduler, the web app, the webhook receiver
    and every host-side skill CLI the proxy spawns per call. A typo on a knob
    that only draws a dashboard tile must not stop any of them from starting.

    `int(float("inf"))` raises OverflowError and `int(float("nan"))` raises
    ValueError, and TOML spells both — so this is reachable from a config file,
    not just from a hand-built dataclass.
    """

    @pytest.mark.parametrize("key", [
        "subscription_usage_cache_ttl_seconds",
        "subscription_usage_timeout_seconds",
        "subscription_usage_warn_percent",
        "subscription_usage_high_percent",
        "subscription_usage_stale_after_seconds",
    ])
    @pytest.mark.parametrize("literal", ["inf", "-inf", "nan", '"5m"', "[1, 2]", "true"])
    def test_a_bad_numeric_value_loads_as_the_default(self, tmp_path, key, literal):
        config = _load(tmp_path, f"""
            [brain.claude_code]
            {key} = {literal}
        """)
        assert getattr(config.brain.claude_code, key) == getattr(ClaudeCodeBrainConfig(), key)

    @pytest.mark.parametrize("literal", ["inf", "nan", '"5m"', "true"])
    def test_a_bad_numeric_value_is_logged(self, tmp_path, caplog, literal):
        with caplog.at_level(logging.WARNING, logger="istota.config"):
            _load(tmp_path, f"""
                [brain.claude_code]
                subscription_usage_cache_ttl_seconds = {literal}
            """)
        assert any(
            "subscription_usage_cache_ttl_seconds" in r.getMessage()
            for r in caplog.records if r.name == "istota.config"
        )

    def test_a_table_where_a_number_belongs(self, tmp_path):
        config = _load(tmp_path, """
            [brain.claude_code.subscription_usage_warn_percent]
            oops = 1
        """)
        assert config.brain.claude_code.subscription_usage_warn_percent == 80.0

    def test_a_non_finite_timeout_never_reaches_the_dataclass(self, tmp_path):
        """`inf` here is an unbounded socket read on a diagnostic path, and a
        value the admin config pane cannot serialize — starlette renders JSON
        with `allow_nan=False`, so one of these 500s `GET /api/admin/config`
        for the whole instance."""
        config = _load(tmp_path, """
            [brain.claude_code]
            subscription_usage_timeout_seconds = inf
        """)
        timeout = config.brain.claude_code.subscription_usage_timeout_seconds
        assert math.isfinite(timeout)
        assert timeout == ClaudeCodeBrainConfig().subscription_usage_timeout_seconds

    @pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
    def test_a_hand_built_config_is_corrected_too(self, value):
        """The parse guards the file; this guards a Config assembled some other
        way, which is the population the validator's own docstring claims."""
        config = Config()
        config.brain.claude_code = ClaudeCodeBrainConfig(
            subscription_usage_timeout_seconds=value,
            subscription_usage_warn_percent=value,
            subscription_usage_high_percent=value,
        )
        _validate_claude_code_brain(config)
        cc = config.brain.claude_code
        assert math.isfinite(cc.subscription_usage_timeout_seconds)
        assert math.isfinite(cc.subscription_usage_warn_percent)
        assert math.isfinite(cc.subscription_usage_high_percent)
        assert 0.0 <= cc.subscription_usage_warn_percent <= 100.0
        assert 0.0 <= cc.subscription_usage_high_percent <= 100.0

    def test_a_nan_percentage_does_not_become_a_permanent_warning(self):
        """Clamping NaN would land it at 0.0 — WARN at every utilization, for
        ever, on a check whose whole point is that it does not cry wolf."""
        config = Config()
        config.brain.claude_code = ClaudeCodeBrainConfig(
            subscription_usage_warn_percent=float("nan"),
        )
        _validate_claude_code_brain(config)
        warn = config.brain.claude_code.subscription_usage_warn_percent
        assert warn == ClaudeCodeBrainConfig().subscription_usage_warn_percent
        assert warn > 0.0

    def test_a_nan_ttl_does_not_sail_past_the_floor(self):
        """`nan < 1` is False. A floor written the obvious way lets it through."""
        config = Config()
        config.brain.claude_code = ClaudeCodeBrainConfig(
            subscription_usage_cache_ttl_seconds=float("nan"),
        )
        _validate_claude_code_brain(config)
        assert config.brain.claude_code.subscription_usage_cache_ttl_seconds == 1


class TestValidation:
    """The loader corrects, warns and carries on. It never refuses to load."""

    def test_percentages_clamp_to_the_top(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="istota.config"):
            config = _load(tmp_path, """
                [brain.claude_code]
                subscription_usage_warn_percent = 400
                subscription_usage_high_percent = 900
            """)
        cc = config.brain.claude_code
        assert cc.subscription_usage_warn_percent == 100.0
        assert cc.subscription_usage_high_percent == 100.0
        # A silent correction is the failure here: an operator who set 400 and
        # got 100 must be able to find out why from the log.
        messages = [r.getMessage() for r in caplog.records if r.name == "istota.config"]
        assert any("subscription_usage_warn_percent" in m for m in messages), messages
        assert any("subscription_usage_high_percent" in m for m in messages), messages

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

    def test_non_finite_percentages_take_the_default_not_a_bound(self, tmp_path):
        """TOML spells `inf` and `nan`, and neither is "out of range".

        Clamping would put `nan` at 0.0, which means amber at every utilization
        for ever. A value that is not a number carries no intent to preserve,
        so it takes the shipping default instead.
        """
        config = _load(tmp_path, """
            [brain.claude_code]
            subscription_usage_warn_percent = nan
            subscription_usage_high_percent = inf
        """)
        cc = config.brain.claude_code
        assert cc.subscription_usage_warn_percent == 80.0
        assert cc.subscription_usage_high_percent == 95.0

    def test_a_zero_ttl_is_floored_and_logged(self, tmp_path, caplog):
        """A zero TTL would fetch on every dashboard poll."""
        with caplog.at_level(logging.WARNING, logger="istota.config"):
            config = _load(tmp_path, """
                [brain.claude_code]
                subscription_usage_cache_ttl_seconds = 0
            """)
        assert config.brain.claude_code.subscription_usage_cache_ttl_seconds == 1
        assert any(
            "subscription_usage_cache_ttl_seconds" in r.getMessage()
            for r in caplog.records if r.name == "istota.config"
        )

    @pytest.mark.parametrize("value", [0, -1, -3600])
    def test_stale_after_seconds_is_deliberately_not_floored(self, tmp_path, value):
        """Documented as a decision in `_validate_claude_code_brain`, so pinned.

        Zero there coherently means "treat any stale reading as too old". A
        later change that floors it "for consistency" with the other two
        integers would otherwise leave the whole suite green.
        """
        config = _load(tmp_path, f"""
            [brain.claude_code]
            subscription_usage_stale_after_seconds = {value}
        """)
        assert config.brain.claude_code.subscription_usage_stale_after_seconds == value

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

    def test_the_module_guard_substitutes_where_the_loader_floors(self):
        """The two guards refuse the same values and substitute different ones.

        Deliberate, and documented on `_settings`: the loader floors a below-1
        value at 1 because an operator asked for something small, while the
        module substitutes the shipping default, because a value arriving past
        the loader carries no intent worth preserving. Pinned so the difference
        stays a decision rather than becoming a surprise.
        """
        config = Config()
        config.brain.claude_code = ClaudeCodeBrainConfig(
            subscription_usage_cache_ttl_seconds=0,
            subscription_usage_timeout_seconds=0.0,
        )
        _enabled, ttl, timeout = su._settings(config)
        assert ttl == su.DEFAULT_CACHE_TTL_SECONDS
        assert timeout == su.DEFAULT_TIMEOUT_SECONDS

        _validate_claude_code_brain(config)
        assert config.brain.claude_code.subscription_usage_cache_ttl_seconds == 1
        assert config.brain.claude_code.subscription_usage_timeout_seconds == 1.0

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
