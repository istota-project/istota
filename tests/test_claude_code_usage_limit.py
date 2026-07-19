"""Usage-limit detection for ClaudeCodeBrain (brain-fallback spec, Stage 1).

``is_usage_limit_error`` classifies subscription/quota/billing exhaustion as a
persistent "brain unavailable" condition distinct from a transient overload,
and the execution paths carry it as ``stop_reason="usage_limit"`` (not retried
against the exhausted primary).
"""

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
