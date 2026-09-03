"""PrimaryAvailabilityBreaker unit tests (brain-fallback spec, Stage 4)."""


import pytest

from istota.brain._fallback import (
    COOLDOWN_STOP_REASONS,
    MIN_COOLDOWN_SECONDS,
    PrimaryAvailabilityBreaker,
    TRIGGER_STOP_REASONS,
    effective_fallback_kind,
    get_availability_breaker,
    reset_availability_breaker,
)
from istota.config import BrainConfig
from tests.support.monotonic_spy import monotonic_spy


class TestBreaker:
    def test_open_returns_true_once(self, monkeypatch):
        import istota.brain._fallback as mod
        clock = [100.0]
        monotonic_spy(monkeypatch, mod, lambda: clock[0])
        b = PrimaryAvailabilityBreaker()
        assert b.open("claude_code", 300) is True   # closed → open
        assert b.open("claude_code", 300) is False  # already open

    def test_should_skip_within_and_after_cooldown(self, monkeypatch):
        import istota.brain._fallback as mod
        clock = [0.0]
        monotonic_spy(monkeypatch, mod, lambda: clock[0])
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
        monotonic_spy(monkeypatch, mod, lambda: clock[0])
        b = PrimaryAvailabilityBreaker()
        assert b.open("x", 100) is True
        clock[0] = 101.0
        # cooldown elapsed → a fresh open is again a closed→open transition.
        assert b.open("x", 100) is True

    def test_repeat_open_does_not_extend_window(self, monkeypatch):
        # An already-open breaker re-opened within the window keeps its ORIGINAL
        # deadline: the cooldown is anchored to the first failure, so a caller
        # that keeps re-reporting the same unavailability can't hold it open
        # forever. Window is [0, 100); a re-open at t=90 must not push it out.
        import istota.brain._fallback as mod
        clock = [0.0]
        monotonic_spy(monkeypatch, mod, lambda: clock[0])
        b = PrimaryAvailabilityBreaker()
        assert b.open("x", 100) is True
        clock[0] = 90.0
        assert b.open("x", 100) is False           # already open, no re-arm
        assert b.should_skip("x", 100) is True      # still within original window
        clock[0] = 101.0
        # Deadline stayed at 100 (not pushed to 190) → breaker has reopened.
        assert b.should_skip("x", 100) is False

    def test_success_does_not_close_a_breaker_opened_after_its_probe(self, monkeypatch):
        import istota.brain._fallback as mod
        clock = [0.0]
        monotonic_spy(monkeypatch, mod, lambda: clock[0])
        b = PrimaryAvailabilityBreaker()
        b.open("x", 100)
        clock[0] = 101.0
        primary_started_at = clock[0]
        clock[0] = 102.0
        b.open("x", 100)

        b.record_success("x", started_at=primary_started_at)

        assert b.should_skip("x", 100) is True


class TestDeadline:
    """ISSUE-374: the window ends at a deadline, and a caller may name it."""

    def _clocked(self, monkeypatch, start=0.0):
        import istota.brain._fallback as mod
        clock = [start]
        monotonic_spy(monkeypatch, mod, lambda: clock[0])
        return clock, PrimaryAvailabilityBreaker()

    def test_until_shortens_the_window(self, monkeypatch):
        clock, b = self._clocked(monkeypatch)
        assert b.open("x", 3600, until=660.0) is True
        clock[0] = 659.0
        assert b.should_skip("x", 3600) is True
        clock[0] = 661.0
        assert b.should_skip("x", 3600) is False

    def test_until_is_capped_by_the_cooldown(self, monkeypatch):
        clock, b = self._clocked(monkeypatch)
        b.open("x", 100, until=10_000.0)
        assert b.remaining("x") == 100
        clock[0] = 101.0
        assert b.should_skip("x", 100) is False

    def test_until_is_floored(self, monkeypatch):
        _clock, b = self._clocked(monkeypatch)
        b.open("x", 3600, until=1.0)
        assert b.remaining("x") == MIN_COOLDOWN_SECONDS

    def test_the_floor_never_outgrows_the_cooldown(self, monkeypatch):
        _clock, b = self._clocked(monkeypatch)
        b.open("x", 10, until=1.0)
        assert b.remaining("x") == 10

    def test_a_repeat_open_cannot_extend_the_deadline(self, monkeypatch):
        clock, b = self._clocked(monkeypatch)
        assert b.open("x", 3600, until=660.0) is True
        clock[0] = 600.0
        assert b.open("x", 3600, until=100_000.0) is False
        assert b.remaining("x") == 60
        clock[0] = 661.0
        assert b.should_skip("x", 3600) is False

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_until_falls_back_to_the_cooldown(self, monkeypatch, bad):
        _clock, b = self._clocked(monkeypatch)
        b.open("x", 300, until=bad)
        assert b.remaining("x") == 300
        assert b.should_skip("x", 300) is True

    def test_remaining_is_none_when_closed(self):
        assert PrimaryAvailabilityBreaker().remaining("x") is None

    def test_remaining_is_none_once_the_deadline_passes(self, monkeypatch):
        clock, b = self._clocked(monkeypatch)
        b.open("x", 100)
        clock[0] = 101.0
        assert b.remaining("x") is None

    def test_success_still_closes_a_deadline_window(self, monkeypatch):
        _clock, b = self._clocked(monkeypatch)
        b.open("x", 3600, until=660.0)
        b.record_success("x")
        assert b.should_skip("x", 3600) is False

    def test_a_zero_cooldown_never_skips(self, monkeypatch):
        _clock, b = self._clocked(monkeypatch)
        b.open("x", 0, until=660.0)
        assert b.should_skip("x", 0) is False


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
        # ISSUE-362: no implicit target for any kind, tmux_claude included.
        assert effective_fallback_kind(BrainConfig(kind="tmux_claude")) is None
        assert effective_fallback_kind(BrainConfig(kind="claude_code")) is None
        assert effective_fallback_kind(BrainConfig(kind="claude_code", fallback="native")) == "native"
