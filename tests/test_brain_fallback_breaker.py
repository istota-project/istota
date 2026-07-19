"""PrimaryAvailabilityBreaker unit tests (brain-fallback spec, Stage 4)."""

import pytest

from istota.brain._fallback import (
    COOLDOWN_STOP_REASONS,
    PrimaryAvailabilityBreaker,
    TRIGGER_STOP_REASONS,
    effective_fallback_kind,
    get_availability_breaker,
    reset_availability_breaker,
)
from istota.config import BrainConfig


class TestBreaker:
    def test_open_returns_true_once(self, monkeypatch):
        import istota.brain._fallback as mod
        clock = [100.0]
        monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
        b = PrimaryAvailabilityBreaker()
        assert b.open("claude_code", 300) is True   # closed → open
        assert b.open("claude_code", 300) is False  # already open

    def test_should_skip_within_and_after_cooldown(self, monkeypatch):
        import istota.brain._fallback as mod
        clock = [0.0]
        monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
        b = PrimaryAvailabilityBreaker()
        b.open("claude_code", 300)
        assert b.should_skip("claude_code", 300) is True
        clock[0] = 301.0
        assert b.should_skip("claude_code", 300) is False

    def test_should_skip_false_when_never_opened(self):
        b = PrimaryAvailabilityBreaker()
        assert b.should_skip("native", 300) is False

    def test_record_success_closes(self):
        b = PrimaryAvailabilityBreaker()
        b.open("claude_code", 300)
        assert b.should_skip("claude_code", 300) is True
        b.record_success("claude_code")
        assert b.should_skip("claude_code", 300) is False
        # After close, open transitions again (arms a fresh alert).
        assert b.open("claude_code", 300) is True

    def test_keying_is_independent(self):
        b = PrimaryAvailabilityBreaker()
        b.open("claude_code", 300)
        assert b.should_skip("claude_code", 300) is True
        assert b.should_skip("tmux_claude", 300) is False

    def test_reopen_after_cooldown_arms_new_alert(self, monkeypatch):
        import istota.brain._fallback as mod
        clock = [0.0]
        monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
        b = PrimaryAvailabilityBreaker()
        assert b.open("x", 100) is True
        clock[0] = 101.0
        # cooldown elapsed → a fresh open is again a closed→open transition.
        assert b.open("x", 100) is True


class TestProcessGlobal:
    def test_reset_clears(self):
        get_availability_breaker().open("claude_code", 300)
        assert get_availability_breaker().should_skip("claude_code", 300) is True
        reset_availability_breaker()
        assert get_availability_breaker().should_skip("claude_code", 300) is False


class TestConstantsAndHelper:
    def test_trigger_and_cooldown_sets(self):
        assert TRIGGER_STOP_REASONS == frozenset({"usage_limit", "not_found", "fallback"})
        assert COOLDOWN_STOP_REASONS == frozenset({"usage_limit", "not_found"})
        # fallback triggers a reroute but never opens the breaker.
        assert "fallback" in TRIGGER_STOP_REASONS
        assert "fallback" not in COOLDOWN_STOP_REASONS

    def test_effective_fallback_kind(self):
        assert effective_fallback_kind(BrainConfig(kind="tmux_claude")) == "claude_code"
        assert effective_fallback_kind(BrainConfig(kind="claude_code")) is None
        assert effective_fallback_kind(BrainConfig(kind="claude_code", fallback="native")) == "native"
