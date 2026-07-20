"""Usage-limit detection for TmuxClaudeBrain (brain-fallback spec, Stage 1).

tmux is the subscription-billing brain, so a quota exhaustion is the exact case
a native fallback exists to cover. Two detection surfaces: the reconstructed
final-turn text (Stop payload / transcript) and a pane usage_limit_markers match
mid-turn. A usage_limit outcome must NOT feed the launch circuit breaker nor
tmux's own headless fallback — it returns up to the executor.
"""

import json

import pytest

from istota.brain._types import BrainRequest, BrainResult
from istota.brain.tmux_claude import (
    TmuxClaudeBrain,
    _CircuitBreaker,
    consume_circuit_open_alert,
    reset_circuit_breaker,
)
from istota.config import TmuxBrainConfig


@pytest.fixture(autouse=True)
def _reset_breaker(monkeypatch):
    import istota.brain.tmux_claude as mod
    monkeypatch.setattr(mod, "_VERSION_CHECKED", True)
    reset_circuit_breaker()
    yield
    reset_circuit_breaker()


def _req(tmp_path, **kw):
    base = dict(
        prompt="hi", allowed_tools=[], cwd=tmp_path,
        env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"}, timeout_seconds=60,
    )
    base.update(kw)
    return BrainRequest(**base)


class TestBuildResultUsageLimit:
    def test_stop_payload_usage_limit(self, tmp_path):
        brain = TmuxClaudeBrain()
        sentinel = tmp_path / "stop.json"
        sentinel.write_text(json.dumps({
            "transcript_path": "",
            "last_assistant_message": "Claude usage limit reached. Resets at 5pm.",
        }))
        result = brain._build_result(sentinel, _req(tmp_path), forward_progress=False)
        assert result.success is False
        assert result.stop_reason == "usage_limit"

    def test_normal_completion_unaffected(self, tmp_path):
        brain = TmuxClaudeBrain()
        sentinel = tmp_path / "stop.json"
        sentinel.write_text(json.dumps({
            "transcript_path": "",
            "last_assistant_message": "Here is your answer about API rate limits.",
        }))
        result = brain._build_result(sentinel, _req(tmp_path), forward_progress=False)
        assert result.success is True
        assert result.stop_reason == "completed"


class TestWaitForCompletionUsageLimit:
    def _classify(self, monkeypatch, tmp_path, pane_text):
        brain = TmuxClaudeBrain()
        monkeypatch.setattr(brain, "_capture", lambda name: pane_text)
        monkeypatch.setattr(brain, "_pane_alive", lambda name: True)
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
        s = tmp_path / "stop.json"  # never created
        import time as _t
        return brain._wait_for_completion("s", s, _t.monotonic() + 9999, None)

    def test_pane_marker_classified_usage_limit(self, monkeypatch, tmp_path):
        status, pane = self._classify(monkeypatch, tmp_path, "session limit reached")
        assert status == "usage_limit"
        assert "session limit reached" in pane

    def test_real_session_limit_text_classified(self, monkeypatch, tmp_path):
        # The actual interactive-TUI text — matched by the "session limit" marker.
        status, _ = self._classify(
            monkeypatch, tmp_path, "You've hit your session limit · resets 3:45pm"
        )
        assert status == "usage_limit"

    @pytest.mark.parametrize(
        "pane_text",
        [
            "You've hit your weekly limit · resets Mon 12:00am",
            "You've hit your Opus limit · resets 3:45pm",
        ],
    )
    def test_weekly_and_opus_limit_caught_by_detector_fallback(
        self, monkeypatch, tmp_path, pane_text
    ):
        # No marker matches these scopes; the is_usage_limit_error(pane) fallback
        # in _wait_for_completion catches them.
        status, _ = self._classify(monkeypatch, tmp_path, pane_text)
        assert status == "usage_limit"


class _ExecHarness:
    def __init__(self, monkeypatch, result, retryable=False):
        self.brain = TmuxClaudeBrain(TmuxBrainConfig(fallback_trip_threshold=3))
        self.calls = 0
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/tmux")
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
        monkeypatch.setattr(self.brain, "_cleanup_legacy_hook", lambda req: None)

        def run(req, attempt):
            self.calls += 1
            return (result, retryable)

        monkeypatch.setattr(self.brain, "_run_session", run)


class TestUsageLimitDoesNotFeedBreaker:
    def test_usage_limit_returned_no_breaker_no_retry(self, monkeypatch, tmp_path):
        h = _ExecHarness(monkeypatch, BrainResult(
            success=False, result_text="usage limit", stop_reason="usage_limit"))
        # Run more than the trip threshold — the breaker must never open.
        for _ in range(5):
            res = h.brain.execute(_req(tmp_path))
            assert res.stop_reason == "usage_limit"
        assert h.calls == 5  # each attempt actually ran tmux (not short-circuited)
        assert consume_circuit_open_alert() is False  # no alert armed
