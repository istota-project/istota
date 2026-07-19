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
            ("Connection error: timed out", "error"),
            ("", "error"),
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

    def test_connection_error_stays_error(self):
        r = self._build("Connection error: timed out")
        assert r.stop_reason == "error"
