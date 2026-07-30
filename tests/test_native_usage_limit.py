"""Native-brain usage-limit vs transient classification (brain-fallback Stage 1).

Native talks to arbitrary OpenAI-compatible endpoints, so classification is a
best-effort heuristic over the provider error body: a quota/billing exhaustion
→ ``usage_limit`` (reroute), a plain overload/rate-limit → ``transient_api_error``.
"""

import pytest

from istota.brain.native import NativeBrain, _classify_native_error
from istota.session.usage import TaskUsage


class TestClassifyNativeError:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ('HTTP 429: {"error": {"type": "insufficient_quota", "message": "You have exceeded your quota"}}', "usage_limit"),
            ('HTTP 429: {"error": {"message": "Your credit balance is too low"}}', "usage_limit"),
            ("billing hard limit reached", "usage_limit"),
            ('HTTP 429: {"error": {"message": "Rate limit exceeded, retry"}}', "transient_api_error"),
            ('HTTP 503: {"error": {"message": "Service overloaded"}}', "transient_api_error"),
            # ISSUE-212: a network-level failure is a capacity/connectivity
            # signal, so it reroutes to the fallback rather than dead-ending.
            ("Connection error: timed out", "transient_api_error"),
            ("Connection error: [Errno 104] ECONNRESET", "transient_api_error"),
            ("", "error"),
            # Request-shaped failures must stay permanent — a fallback attempt
            # would fail identically and double-charge.
            ('HTTP 400: {"error": {"message": "Invalid request"}}', "error"),
            ('HTTP 401: {"error": {"message": "Incorrect API key"}}', "error"),
            ('HTTP 404: {"error": {"message": "Unknown model"}}', "error"),
            ('HTTP 413: {"error": {"message": "Payload too large"}}', "error"),
        ],
    )
    def test_classification(self, text, expected):
        assert _classify_native_error(text) == expected


class TestBuildResultStopReason:
    def _build(self, error_message):
        return NativeBrain._build_result(
            "error", "", error_message, [], [], TaskUsage(), "model-x",
        )

    def test_quota_429_becomes_usage_limit(self):
        r = self._build('HTTP 429: {"error": {"type": "insufficient_quota"}}')
        assert r.success is False
        assert r.stop_reason == "usage_limit"

    def test_plain_rate_limit_429_stays_transient(self):
        r = self._build('HTTP 429: {"error": {"message": "Rate limit exceeded"}}')
        assert r.stop_reason == "transient_api_error"

    def test_connection_error_is_transient(self):
        # ISSUE-212: a network-level failure is a capacity/connectivity signal,
        # so it retries and then reroutes to the fallback instead of surfacing
        # as an anonymous error the trigger set can't match.
        r = self._build("Connection error: timed out")
        assert r.stop_reason == "transient_api_error"

    def test_request_shaped_status_stays_error(self):
        r = self._build('HTTP 400: {"error": {"message": "Invalid request"}}')
        assert r.stop_reason == "error"
