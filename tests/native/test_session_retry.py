"""Error classification + backoff retry (session layer)."""

import asyncio
from dataclasses import dataclass

import pytest

from istota.session.retry import classify_error, retry_with_backoff


class TestClassifyError:
    def test_context_overflow_not_retryable(self):
        c = classify_error("prompt is too long: 250000 tokens")
        assert c.is_context_overflow is True
        assert c.retryable is False
        assert c.category == "overflow"

    def test_auth_error(self):
        c = classify_error("forbidden", status_code=403)
        assert c.is_auth_error is True
        assert c.retryable is False
        assert c.category == "auth"

    def test_rate_limit_by_status(self):
        c = classify_error("slow down", status_code=429)
        assert c.is_rate_limit is True
        assert c.retryable is True
        assert c.category == "transient"

    def test_server_error_retryable(self):
        c = classify_error("internal", status_code=503)
        assert c.retryable is True
        assert c.category == "transient"

    def test_overloaded_text_retryable(self):
        c = classify_error("Overloaded")
        assert c.retryable is True

    def test_unknown_is_permanent(self):
        c = classify_error("some weird thing", status_code=418)
        assert c.retryable is False
        assert c.category == "permanent"

    def test_overflow_wins_over_status(self):
        c = classify_error("maximum context length exceeded", status_code=400)
        assert c.is_context_overflow is True


@dataclass
class _Res:
    success: bool
    error_message: str = ""
    status_code: int | None = None


class TestRetryWithBackoff:
    @pytest.mark.asyncio
    async def test_returns_immediately_on_success(self):
        calls = 0

        async def run():
            nonlocal calls
            calls += 1
            return _Res(success=True)

        res = await retry_with_backoff(run, max_retries=3, base_delay=0)
        assert res.success
        assert calls == 1

    @pytest.mark.asyncio
    async def test_no_retry_for_permanent_error(self):
        calls = 0

        async def run():
            nonlocal calls
            calls += 1
            return _Res(success=False, error_message="bad request", status_code=400)

        res = await retry_with_backoff(run, max_retries=3, base_delay=0)
        assert res.success is False
        assert calls == 1

    @pytest.mark.asyncio
    async def test_retries_transient_then_succeeds(self):
        seq = [
            _Res(success=False, error_message="overloaded", status_code=503),
            _Res(success=False, error_message="overloaded", status_code=503),
            _Res(success=True),
        ]
        calls = 0

        async def run():
            nonlocal calls
            r = seq[calls]
            calls += 1
            return r

        res = await retry_with_backoff(run, max_retries=3, base_delay=0)
        assert res.success
        assert calls == 3

    @pytest.mark.asyncio
    async def test_exhausts_retries(self):
        calls = 0

        async def run():
            nonlocal calls
            calls += 1
            return _Res(success=False, error_message="overloaded", status_code=503)

        res = await retry_with_backoff(run, max_retries=2, base_delay=0)
        assert res.success is False
        assert calls == 3  # initial + 2 retries

    @pytest.mark.asyncio
    async def test_abort_during_sleep_returns_last_error(self):
        abort = asyncio.Event()
        calls = 0

        async def run():
            nonlocal calls
            calls += 1
            abort.set()  # trip abort so the backoff sleep returns early
            return _Res(success=False, error_message="overloaded", status_code=503)

        res = await retry_with_backoff(
            run, max_retries=5, base_delay=10, abort=abort
        )
        assert res.success is False
        assert calls == 1
