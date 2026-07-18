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

    def test_non_anthropic_overflow_phrasings(self):
        # NB-13(c): common non-Anthropic overflow phrasings must route to
        # compaction/recovery, not be treated as a permanent error.
        for msg in [
            "the request exceeds the available context size",
            "input exceeds the model's context window",
            "please reduce the length of the messages",
            "n_tokens exceeds context size",
        ]:
            c = classify_error(msg)
            assert c.is_context_overflow is True, msg
            assert c.category == "overflow", msg

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

    def test_permanent_400_quoting_a_5xx_code_is_not_retryable(self):
        # NB-13a: a permanent client error whose body happens to quote "503" or
        # "timeout" must not be misclassified as transient.
        c = classify_error("invalid request: field 'x' must be 503 or lower", status_code=400)
        assert c.retryable is False
        assert c.category == "permanent"
        c2 = classify_error("bad parameter: timeout must be positive", status_code=400)
        assert c2.retryable is False

    def test_rate_word_inside_another_word_is_not_a_rate_limit(self):
        # NB-13b: "generate" contains "rate" — must not read as a rate limit.
        c = classify_error("failed to generate a response")
        assert c.is_rate_limit is False
        assert c.category == "permanent"

    def test_token_rate_limit_429_is_transient_not_overflow(self):
        # NB-13: a tokens-per-minute 429 may read like overflow ("too many
        # tokens") but a 429 status is a rate limit, not a context overflow.
        c = classify_error("rate limit: too many tokens per minute", status_code=429)
        assert c.is_rate_limit is True
        assert c.is_context_overflow is False
        assert c.category == "transient"


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
    async def test_jitter_never_exceeds_max_delay(self, monkeypatch):
        # NB-13: jitter must be applied before the cap, so max_delay is a true
        # ceiling (not 1.5× it). Force the max jitter multiplier (1.5×).
        import istota.session.retry as retry_mod

        monkeypatch.setattr(retry_mod.random, "random", lambda: 1.0)
        seen: list[float] = []

        seq = [_Res(success=False, error_message="overloaded", status_code=503), _Res(success=True)]
        calls = 0

        async def run():
            nonlocal calls
            r = seq[calls]
            calls += 1
            return r

        await retry_with_backoff(
            run,
            max_retries=3,
            base_delay=100.0,
            max_delay=0.01,
            on_retry=lambda n, m, delay, err: seen.append(delay),
        )
        assert seen and all(d <= 0.01 for d in seen)

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
