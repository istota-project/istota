"""Usage-limit detection for ClaudeCodeBrain (brain-fallback spec, Stage 1).

``is_usage_limit_error`` classifies subscription/quota/billing exhaustion as a
persistent "brain unavailable" condition distinct from a transient overload,
and the execution paths carry it as ``stop_reason="usage_limit"`` (not retried
against the exhausted primary).
"""

import json
from unittest.mock import patch

import pytest

from istota.brain._types import BrainRequest, BrainResult
from istota.brain.claude_code import (
    ClaudeCodeBrain,
    is_transient_api_error,
    is_usage_limit_error,
)


class TestIsUsageLimitError:
    @pytest.mark.parametrize(
        "text",
        [
            "Claude usage limit reached. Your limit will reset at 3pm.",
            "session limit reached",
            "You have exceeded your current quota",
            '{"error": {"type": "insufficient_quota", "message": "quota exceeded"}}',
            "Your credit balance is too low to run this request",
            "billing hard limit has been reached",
            "You've hit your monthly limit",
            "API Error: 429 {\"error\": {\"message\": \"usage limit reached\"}}",
            # The three real Claude Code subscription-limit messages (per the
            # docs) — session/weekly/Opus. All three must classify as usage_limit.
            "You've hit your session limit · resets 3:45pm",
            "You've hit your weekly limit · resets Mon 12:00am",
            "You've hit your Opus limit · resets 3:45pm",
        ],
    )
    def test_matches_usage_limit_strings(self, text):
        assert is_usage_limit_error(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "API Error: 500 {\"error\": {\"message\": \"Internal server error\"}}",
            "API Error: 529 {\"error\": {\"message\": \"Overloaded\"}}",
            "Rate limit exceeded, please retry after 1s",  # transient, not quota
            "Connection error: timed out",
            "Some normal completion text about limits of a design",
        ],
    )
    def test_ignores_non_usage_limit_strings(self, text):
        assert is_usage_limit_error(text) is False

    def test_overload_429_is_transient_not_usage_limit(self):
        overload = 'API Error: 429 {"error": {"message": "Overloaded, please retry"}}'
        assert is_transient_api_error(overload) is True
        assert is_usage_limit_error(overload) is False

    def test_quota_429_is_usage_limit(self):
        quota = 'API Error: 429 {"error": {"message": "You have exceeded your usage limit"}}'
        # It matches the transient predicate (429), but usage-limit classification
        # takes precedence at every call site.
        assert is_usage_limit_error(quota) is True


def _req() -> BrainRequest:
    return BrainRequest(
        prompt="hi",
        allowed_tools=["Bash"],
        cwd=__import__("pathlib").Path("/tmp"),
        env={},
        timeout_seconds=60,
    )


class TestSimplePathClassification:
    def _run(self, returncode, stdout, stderr=""):
        brain = ClaudeCodeBrain()
        fake = type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()
        with patch("istota.brain.claude_code.subprocess.run", return_value=fake):
            return brain._execute_simple_once(["claude"], _req())

    def test_usage_limit_stdout_classified(self):
        result = self._run(1, "Claude usage limit reached. Resets at 5pm.")
        assert result.success is False
        assert result.stop_reason == "usage_limit"

    def test_usage_limit_stderr_classified(self):
        result = self._run(1, "", stderr="session limit reached")
        assert result.stop_reason == "usage_limit"

    def test_generic_error_stays_error(self):
        result = self._run(1, "some random failure")
        assert result.stop_reason == "error"

    def test_rc0_limit_on_stdout_reclassified(self):
        # `claude -p` reports a limit as a SUCCESS (rc 0, limit text on stdout).
        # It must not be delivered as a successful answer.
        result = self._run(0, "You've hit your weekly limit · resets Mon 12:00am")
        assert result.success is False
        assert result.stop_reason == "usage_limit"

    def test_rc0_normal_output_stays_success(self):
        result = self._run(0, "Here is your completed answer.")
        assert result.success is True
        assert result.stop_reason == "completed"


class _FakeStdin:
    def write(self, *_):
        pass

    def close(self):
        pass


class _FakeProc:
    """Minimal Popen stand-in for the streaming path: an iterable stdout of
    stream-json lines, an empty stderr, rc 0."""

    def __init__(self, stdout_lines):
        self.stdout = iter(stdout_lines)
        self.stderr = iter([])
        self.stdin = _FakeStdin()
        self.returncode = 0
        self.pid = 4321

    def wait(self):
        pass

    def kill(self):
        self.returncode = -9


class TestStreamingSuccessBranchClassification:
    def _run(self, result_text):
        brain = ClaudeCodeBrain()
        frame = json.dumps(
            {"type": "result", "subtype": "success", "result": result_text}
        ) + "\n"
        proc = _FakeProc([frame])
        with patch("istota.brain.claude_code.subprocess.Popen", return_value=proc):
            return brain._execute_streaming_once(["claude"], _req())

    def test_success_frame_with_limit_reclassified(self):
        # The CLI emits a session-limit hit as subtype:"success" with the limit
        # text as `result`. It must classify usage_limit, not completed.
        result = self._run("You've hit your session limit · resets 9pm (UTC)")
        assert result.success is False
        assert result.stop_reason == "usage_limit"

    def test_success_frame_normal_stays_success(self):
        result = self._run("Here is your completed answer.")
        assert result.success is True
        assert result.stop_reason == "completed"


class TestSimpleRetryShortCircuit:
    def test_usage_limit_not_retried(self):
        brain = ClaudeCodeBrain()
        ul = BrainResult(success=False, result_text="usage limit reached", stop_reason="usage_limit")
        with patch.object(brain, "_execute_simple_once", return_value=ul) as once:
            with patch("istota.brain.claude_code.time.sleep") as sleep:
                out = brain._execute_simple(["claude"], _req())
        assert out.stop_reason == "usage_limit"
        assert once.call_count == 1  # not retried
        sleep.assert_not_called()
