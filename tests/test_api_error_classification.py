"""Provider API-error classification and transient→fallback routing (ISSUE-212).

A capacity signal from the provider (529 overloaded, 429 rate limit, 5xx,
network-level failures) must be *detected*, retried against the primary, and —
when it persists — rerouted to the fallback brain. A request-shaped failure
(400/401/403/404/413, context length, content filter) must do none of that.

The detection half is the part that was broken: ``parse_api_error`` only matched
the JSON-bodied ``API Error: NNN {...}`` form, so a bare ``API Error: 529
Overloaded`` parsed as nothing, classified as a generic error, was never retried
and never reached the fallback.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from istota.brain._types import BrainRequest, BrainResult
from istota.brain.claude_code import (
    API_RETRY_DELAY_SECONDS,
    PERMANENT_STATUS_CODES,
    ClaudeCodeBrain,
    api_error_stop_reason,
    is_api_error_banner,
    is_permanent_api_error,
    is_transient_api_error,
    parse_api_error,
    parse_retry_after,
)


# ---------------------------------------------------------------------------
# parse_api_error — the bodyless / plain-text forms
# ---------------------------------------------------------------------------


class TestParseBodylessApiError:
    """The CLI does not always attach a JSON body to an API error."""

    def test_parses_bare_529_overloaded(self):
        # The exact string from the incident that filed ISSUE-212.
        parsed = parse_api_error("API Error: 529 Overloaded")
        assert parsed is not None
        assert parsed["status_code"] == 529
        assert parsed["message"] == "Overloaded"
        assert parsed["request_id"] is None

    def test_parses_status_with_no_tail(self):
        parsed = parse_api_error("API Error: 500")
        assert parsed is not None
        assert parsed["status_code"] == 500
        assert parsed["message"] == "Unknown error"

    def test_parses_unclosed_json_body_as_status_only(self):
        # Previously returned None (the JSON pattern needs a closing brace), so a
        # truncated 500 body meant "not an API error at all". A 500 is a 500.
        parsed = parse_api_error("API Error: 500 {broken json")
        assert parsed is not None
        assert parsed["status_code"] == 500

    def test_json_body_still_wins(self):
        parsed = parse_api_error(
            'API Error: 429 {"type":"error","error":{"message":"Rate limited"},'
            '"request_id":"req_1"}'
        )
        assert parsed["message"] == "Rate limited"
        assert parsed["request_id"] == "req_1"

    def test_tail_stops_at_newline(self):
        parsed = parse_api_error("API Error: 529 Overloaded\nStack trace here")
        assert parsed["message"] == "Overloaded"

    @pytest.mark.parametrize(
        "text",
        [
            "Task completed successfully",
            "Claude Code was killed (likely out of memory)",
            "",
            # A three-digit number with no API-error marker is not an API error.
            "The build produced 529 warnings",
        ],
    )
    def test_non_api_error_still_none(self, text):
        assert parse_api_error(text) is None


# ---------------------------------------------------------------------------
# is_transient_api_error — capacity + network signals
# ---------------------------------------------------------------------------


class TestTransientDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "API Error: 529 Overloaded",
            "API Error: 429 Too Many Requests",
            "API Error: 500 Internal Server Error",
            "API Error: 503",
            "API Error: 502 Bad Gateway",
            "API Error: 504",
        ],
    )
    def test_bodyless_capacity_errors_are_transient(self, text):
        assert is_transient_api_error(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "API Error: Connection error.",
            "API Error (Request timed out.)",
            "API Error: fetch failed",
            "API Error: socket hang up",
        ],
    )
    def test_network_level_api_errors_are_transient(self, text):
        # The issue lists connection reset / timeout / DNS as fallback-worthy.
        assert is_transient_api_error(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Error: connect ECONNRESET 160.79.104.10:443",
            "getaddrinfo EAI_AGAIN api.anthropic.com",
        ],
    )
    def test_node_network_errnos_are_transient_without_marker(self, text):
        # These errno strings are diagnostic enough to stand alone; the CLI does
        # not always wrap them in an "API Error:" prefix.
        assert is_transient_api_error(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "API Error: 400 Bad Request",
            "API Error: 401 Unauthorized",
            "API Error: 403 Forbidden",
            "API Error: 404 model not found",
            "API Error: 413 Payload Too Large",
        ],
    )
    def test_request_shaped_errors_are_not_transient(self, text):
        assert is_transient_api_error(text) is False

    @pytest.mark.parametrize(
        "text",
        [
            # Cloudflare's own 5xx family, which a fronted provider emits and
            # which an enumerated status set would have missed exactly the way
            # 529 was missed.
            "API Error: 520 Web Server Returned an Unknown Error",
            "API Error: 522 Connection Timed Out",
            "API Error: 524 A Timeout Occurred",
            "API Error: 408 Request Timeout",
            "API Error: 425 Too Early",
        ],
    )
    def test_every_5xx_and_timing_4xx_is_transient(self, text):
        assert is_transient_api_error(text) is True

    def test_documented_server_throttle_banner_is_transient(self):
        # The CLI's own "this is capacity, not your quota" banner must reach the
        # fallback rather than dead-ending as a generic error.
        throttle = (
            "API Error: Server is temporarily limiting requests "
            "(not your usage limit)"
        )
        assert is_transient_api_error(throttle) is True
        assert api_error_stop_reason(throttle) == "transient_api_error"

    @pytest.mark.parametrize(
        "text",
        [
            # No API-error marker: ordinary prose that happens to discuss the
            # words must not be dragged onto the retry path.
            "The deploy failed because the connection error was never handled.",
            "Task execution timed out after 30 minutes",
            "Cancelled by user",
            "Claude Code was killed (likely out of memory)",
        ],
    )
    def test_prose_is_not_transient(self, text):
        assert is_transient_api_error(text) is False


# ---------------------------------------------------------------------------
# is_permanent_api_error — the "do not retry, do not fall back" set
# ---------------------------------------------------------------------------


class TestPermanentDetection:
    @pytest.mark.parametrize("status", sorted(PERMANENT_STATUS_CODES))
    def test_status_codes_are_permanent(self, status):
        assert is_permanent_api_error(f"API Error: {status} something") is True

    @pytest.mark.parametrize(
        "text",
        [
            'API Error: 400 {"error":{"message":"prompt is too long: 250000 tokens"}}',
            'API Error: 400 {"error":{"type":"invalid_request_error"}}',
            "API Error: 400 The request exceeds the maximum context length",
        ],
    )
    def test_request_shaped_bodies_are_permanent(self, text):
        assert is_permanent_api_error(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "API Error: 529 Overloaded",
            "API Error: 500 Internal Server Error",
            "API Error: 429 Too Many Requests",
            "Here is a normal answer.",
            "",
            # Ordinary prose using the vocabulary. The text branch is gated on
            # the API-error marker for the same reason the transient one is.
            "The model's context window is 200k tokens.",
            "I applied a content filter to the results.",
            # A network signal wins over a request-shaped phrase in the message.
            "API Error: connection reset while building the context window",
        ],
    )
    def test_capacity_and_prose_are_not_permanent(self, text):
        assert is_permanent_api_error(text) is False

    def test_transient_status_wins_over_request_shaped_text(self):
        # A 529 whose body happens to quote a request-shaped phrase is still a
        # capacity error — the status code is authoritative.
        text = 'API Error: 529 {"error":{"message":"Overloaded (context length ok)"}}'
        assert is_permanent_api_error(text) is False
        assert is_transient_api_error(text) is True


# ---------------------------------------------------------------------------
# api_error_stop_reason — the single classifier the brains use
# ---------------------------------------------------------------------------


class TestApiErrorStopReason:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("API Error: 529 Overloaded", "transient_api_error"),
            ("API Error: 503", "transient_api_error"),
            ("API Error: Connection error.", "transient_api_error"),
            ("API Error: 400 Bad Request", "error"),
            ("API Error: 401 Unauthorized", "error"),
            ("You've hit your weekly limit · resets Mon 12:00am", "usage_limit"),
        ],
    )
    def test_classifies(self, text, expected):
        assert api_error_stop_reason(text) == expected

    def test_returns_none_for_non_api_error(self):
        assert api_error_stop_reason("Here is your answer.") is None
        assert api_error_stop_reason("") is None

    def test_usage_limit_wins_over_transient_429(self):
        # A quota 429 is persistent, not a retry candidate.
        quota = 'API Error: 429 {"error":{"message":"You have exceeded your usage limit"}}'
        assert api_error_stop_reason(quota) == "usage_limit"


# ---------------------------------------------------------------------------
# is_api_error_banner — the success-frame guard
# ---------------------------------------------------------------------------


class TestApiErrorBanner:
    @pytest.mark.parametrize(
        "text",
        [
            "API Error: 529 Overloaded",
            "  API Error: 500 Internal Server Error  ",
            "⚠️ API Error: 529 Overloaded",
            'API Error: 429 {"type":"error","error":{"message":"Rate limited"}}',
            "API Error: 500",
            "API Error: 400 Bad Request",
            "API Error: Connection error.",
            "API Error (Request timed out.)",
            "API Error: Server is temporarily limiting requests (not your usage limit)",
        ],
    )
    def test_matches_standalone_banner(self, text):
        assert is_api_error_banner(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Here is your completed answer.",
            # A real answer that *discusses* an API error must never be
            # reclassified as a failure — it is not banner-shaped.
            "The task failed earlier with API Error: 529 Overloaded, which I "
            "worked around by retrying against the cached response.",
            # Long text that merely opens with the phrase is not a bare banner.
            "API Error handling in this codebase " + ("x" * 500),
            # An answer that *starts* with the phrase and continues in prose.
            # A real banner's tail is a Title-cased reason phrase or JSON; a
            # sentence continues in lowercase. Without this the callers would
            # discard a completed answer (the scheduler guard fails the task).
            "API Error: 529 means the provider is overloaded; retry shortly.",
            "API Error 429 three times in yesterday's log, all recovered.",
            "API Error codes are documented in the provider's reference.",
        ],
    )
    def test_ignores_non_banner(self, text):
        assert is_api_error_banner(text) is False


# ---------------------------------------------------------------------------
# parse_retry_after
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ('API Error: 429 {"error":{"message":"rate limited"},"retry-after":30}', 30.0),
            ("API Error: 429 retry-after: 12", 12.0),
            ("API Error: 429 Retry-After 7", 7.0),
            ('API Error: 429 {"retry_after": "45"}', 45.0),
        ],
    )
    def test_parses(self, text, expected):
        assert parse_retry_after(text) == expected

    def test_returns_none_when_absent(self):
        assert parse_retry_after("API Error: 529 Overloaded") is None
        assert parse_retry_after("") is None

    def test_caps_absurd_values(self):
        # A provider asking us to wait an hour must not wedge the worker for an
        # hour — the task's own retry/backoff ladder takes over instead.
        assert parse_retry_after("retry-after: 3600") == 60.0

    def test_ignores_negative(self):
        assert parse_retry_after("retry-after: -5") is None


# ---------------------------------------------------------------------------
# ClaudeCodeBrain execution paths
# ---------------------------------------------------------------------------


def _req() -> BrainRequest:
    return BrainRequest(
        prompt="hi",
        allowed_tools=["Bash"],
        cwd=Path("/tmp"),
        env={},
        timeout_seconds=60,
    )


class _FakeStdin:
    def write(self, *_):
        pass

    def close(self):
        pass


class _FakeProc:
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


class TestSuccessFrameApiErrorReclassified:
    """`claude -p` can report a provider API error as a *successful* result frame
    with the error text as the answer — which is how a bare ``API Error: 529
    Overloaded`` reached the user verbatim as the final reply."""

    def _stream(self, result_text):
        brain = ClaudeCodeBrain()
        frame = json.dumps(
            {"type": "result", "subtype": "success", "result": result_text}
        ) + "\n"
        proc = _FakeProc([frame])
        with patch("istota.brain.claude_code.subprocess.Popen", return_value=proc):
            return brain._execute_streaming_once(["claude"], _req())

    def _simple(self, returncode, stdout, stderr=""):
        brain = ClaudeCodeBrain()
        fake = type(
            "R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr}
        )()
        with patch("istota.brain.claude_code.subprocess.run", return_value=fake):
            return brain._execute_simple_once(["claude"], _req())

    def test_streaming_success_frame_529_is_a_failure(self):
        result = self._stream("API Error: 529 Overloaded")
        assert result.success is False
        assert result.stop_reason == "transient_api_error"

    def test_streaming_success_frame_400_is_a_plain_error(self):
        result = self._stream("API Error: 400 Bad Request")
        assert result.success is False
        assert result.stop_reason == "error"

    def test_streaming_success_frame_normal_answer_unaffected(self):
        result = self._stream("Here is your completed answer.")
        assert result.success is True
        assert result.stop_reason == "completed"

    def test_streaming_answer_discussing_an_api_error_stays_success(self):
        text = (
            "The task failed earlier with API Error: 529 Overloaded, which I "
            "worked around by retrying against the cached response."
        )
        result = self._stream(text)
        assert result.success is True

    def test_simple_rc0_529_is_a_failure(self):
        result = self._simple(0, "API Error: 529 Overloaded")
        assert result.success is False
        assert result.stop_reason == "transient_api_error"

    def test_simple_rc0_normal_answer_unaffected(self):
        result = self._simple(0, "Here is your completed answer.")
        assert result.success is True

    def test_failure_frame_529_classifies_transient(self):
        result = self._simple(1, "API Error: 529 Overloaded")
        assert result.success is False
        assert result.stop_reason == "transient_api_error"

    def test_failure_frame_400_classifies_error(self):
        result = self._simple(1, "API Error: 400 Bad Request")
        assert result.stop_reason == "error"


class TestRetryLoopHonoursBodylessTransient:
    def test_bare_529_is_retried_then_reported_transient(self):
        brain = ClaudeCodeBrain()
        err = BrainResult(
            success=False,
            result_text="API Error: 529 Overloaded",
            stop_reason="transient_api_error",
        )
        with patch.object(brain, "_execute_simple_once", return_value=err) as once:
            with patch("istota.brain.claude_code.time.sleep") as sleep:
                out = brain._execute_simple(["claude"], _req())
        assert once.call_count == 3
        # Two backoffs, slept in slices (so !stop lands during one).
        assert sum(c.args[0] for c in sleep.call_args_list) == pytest.approx(
            API_RETRY_DELAY_SECONDS * 2
        )
        assert out.stop_reason == "transient_api_error"

    def test_permanent_error_is_not_retried(self):
        brain = ClaudeCodeBrain()
        err = BrainResult(
            success=False, result_text="API Error: 400 Bad Request", stop_reason="error",
        )
        with patch.object(brain, "_execute_simple_once", return_value=err) as once:
            with patch("istota.brain.claude_code.time.sleep") as sleep:
                out = brain._execute_simple(["claude"], _req())
        assert once.call_count == 1
        sleep.assert_not_called()
        assert out.stop_reason == "error"

    def test_retry_after_overrides_the_fixed_delay(self):
        brain = ClaudeCodeBrain()
        err = BrainResult(
            success=False,
            result_text="API Error: 429 retry-after: 12",
            stop_reason="transient_api_error",
        )
        with patch.object(brain, "_execute_simple_once", return_value=err):
            with patch("istota.brain.claude_code.time.sleep") as sleep:
                brain._execute_simple(["claude"], _req())
        # Sliced so !stop lands during the backoff; the total is what matters.
        assert sum(c.args[0] for c in sleep.call_args_list) == pytest.approx(24.0, abs=1.0)

    def test_backoff_is_interruptible_by_cancellation(self):
        brain = ClaudeCodeBrain()
        err = BrainResult(
            success=False,
            result_text="API Error: 429 retry-after: 60",
            stop_reason="transient_api_error",
        )
        req = BrainRequest(
            prompt="hi", allowed_tools=["Bash"], cwd=Path("/tmp"), env={},
            timeout_seconds=60, cancel_check=lambda: True,
        )
        with patch.object(brain, "_execute_simple_once", return_value=err) as once:
            with patch("istota.brain.claude_code.time.sleep") as sleep:
                out = brain._execute_simple(["claude"], req)
        # A 60s provider-requested wait must not hold a !stop for 60s.
        assert out.stop_reason == "cancelled"
        assert once.call_count == 1
        assert all(c.args[0] <= 0.5 for c in sleep.call_args_list)

    def test_success_frame_reclassification_is_not_retried(self):
        # The CLI ran to completion and may have executed tools; re-invoking the
        # same prompt would repeat those side effects. Reroute, don't retry.
        brain = ClaudeCodeBrain()
        err = BrainResult(
            success=False,
            result_text="API Error: 529 Overloaded",
            stop_reason="transient_api_error",
            work_committed=True,
        )
        with patch.object(brain, "_execute_simple_once", return_value=err) as once:
            with patch("istota.brain.claude_code.time.sleep") as sleep:
                out = brain._execute_simple(["claude"], _req())
        assert once.call_count == 1
        sleep.assert_not_called()
        # Still a fallback trigger.
        assert out.stop_reason == "transient_api_error"

    def test_success_frame_paths_set_work_committed(self):
        brain = ClaudeCodeBrain()
        fake = type(
            "R", (), {"returncode": 0, "stdout": "API Error: 529 Overloaded", "stderr": ""},
        )()
        with patch("istota.brain.claude_code.subprocess.run", return_value=fake):
            result = brain._execute_simple_once(["claude"], _req())
        assert result.work_committed is True

    def test_failure_frame_does_not_set_work_committed(self):
        brain = ClaudeCodeBrain()
        fake = type(
            "R", (), {"returncode": 1, "stdout": "API Error: 529 Overloaded", "stderr": ""},
        )()
        with patch("istota.brain.claude_code.subprocess.run", return_value=fake):
            result = brain._execute_simple_once(["claude"], _req())
        assert result.work_committed is False
