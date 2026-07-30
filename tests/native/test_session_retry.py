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

    def test_status_recovered_from_http_prefix(self):
        # ISSUE-212 / NB-13: native calls classify_error with the message only,
        # but the provider layer stamps the status into it as "HTTP NNN: …".
        # Recovering it is what keeps a permanent 400 off the retry path and a
        # 429 on it, without threading a status through every call site.
        c = classify_error('HTTP 429: {"error":{"message":"slow down"}}')
        assert c.status_code == 429
        assert c.is_rate_limit is True
        assert c.retryable is True

        c2 = classify_error('HTTP 400: {"error":{"message":"connection error in field"}}')
        assert c2.status_code == 400
        assert c2.retryable is False
        assert c2.category == "permanent"

        c3 = classify_error('HTTP 503: {"error":{"message":"nope"}}')
        assert c3.retryable is True

    def test_explicit_status_wins_over_recovered_one(self):
        c = classify_error("HTTP 503: upstream said so", status_code=400)
        assert c.status_code == 400
        assert c.retryable is False

    def test_retry_after_parsed_on_rate_limit(self):
        c = classify_error('HTTP 429: {"error":{"message":"slow"},"retry-after":18}')
        assert c.retry_after == 18.0

    def test_retry_after_absent_is_none(self):
        assert classify_error("HTTP 429: slow down").retry_after is None

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

    @staticmethod
    async def _delays_for(monkeypatch, error_message, *, max_delay):
        """Run one retry cycle with sleeping stubbed out; return the delays used."""
        import istota.session.retry as retry_mod

        slept: list[float] = []

        async def fake_sleep(d):
            slept.append(d)

        monkeypatch.setattr(retry_mod.asyncio, "sleep", fake_sleep)

        seq = [
            _Res(success=False, error_message=error_message, status_code=429),
            _Res(success=True),
        ]
        calls = 0

        async def run():
            nonlocal calls
            r = seq[calls]
            calls += 1
            return r

        await retry_with_backoff(
            run, max_retries=3, base_delay=0.001, max_delay=max_delay,
        )
        return slept

    @pytest.mark.asyncio
    async def test_retry_after_overrides_the_backoff(self, monkeypatch):
        # ISSUE-212: honour the provider's Retry-After on a 429 instead of
        # guessing with exponential backoff.
        slept = await self._delays_for(
            monkeypatch,
            '{"error":{"message":"slow"},"retry-after":3}',
            max_delay=60.0,
        )
        assert slept == [3.0]

    @pytest.mark.asyncio
    async def test_retry_after_capped_by_max_delay(self, monkeypatch):
        slept = await self._delays_for(
            monkeypatch, "retry-after: 55", max_delay=2.0,
        )
        assert slept == [2.0]

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


# ---------------------------------------------------------------------------
# NativeBrain._RetryingProvider — the live retry path on the native side.
# `retry_with_backoff` above has no production caller; this wrapper is what
# actually runs for a native task, so the Retry-After honouring needs its own
# coverage here (ISSUE-212).
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, events_per_call):
        self._events = list(events_per_call)
        self.calls = 0

    async def stream(self, *a, **kw):
        batch = self._events[min(self.calls, len(self._events) - 1)]
        self.calls += 1
        for ev in batch:
            yield ev


class TestRetryingProviderBackoff:
    @staticmethod
    def _error(message):
        from istota.llm.provider import StreamError
        from istota.llm.types import AssistantMessage

        return StreamError(
            message=AssistantMessage(stop_reason="error", error_message=message)
        )

    @staticmethod
    def _done():
        from istota.llm.provider import StreamDone
        from istota.llm.types import AssistantMessage

        return StreamDone(message=AssistantMessage(stop_reason="end_turn"))

    async def _run(self, monkeypatch, message):
        import istota.brain.native as native_mod

        slept: list[float] = []

        async def fake_sleep(d):
            slept.append(d)

        monkeypatch.setattr(native_mod.asyncio, "sleep", fake_sleep)
        provider = _FakeProvider([[self._error(message)], [self._done()]])
        wrapped = native_mod._RetryingProvider(provider, None)
        events = [e async for e in wrapped.stream("sys", [], [])]
        return slept, provider.calls, events

    @pytest.mark.asyncio
    async def test_retry_after_is_honoured(self, monkeypatch):
        slept, calls, _ = await self._run(
            monkeypatch, 'HTTP 429: {"error":{"message":"slow"},"retry-after":7}',
        )
        assert calls == 2
        assert slept == [7.0]

    @pytest.mark.asyncio
    async def test_retry_after_capped_by_max_delay(self, monkeypatch):
        import istota.brain.native as native_mod

        slept, calls, _ = await self._run(monkeypatch, "HTTP 429: retry-after: 9999")
        assert calls == 2
        assert slept == [native_mod._API_RETRY_MAX_DELAY]

    @pytest.mark.asyncio
    async def test_exponential_backoff_without_retry_after(self, monkeypatch):
        slept, calls, _ = await self._run(monkeypatch, "HTTP 503: overloaded")
        assert calls == 2
        assert slept == [5.0]

    @pytest.mark.asyncio
    async def test_permanent_status_is_not_retried(self, monkeypatch):
        slept, calls, events = await self._run(
            monkeypatch, 'HTTP 400: {"error":{"message":"connection error in field"}}',
        )
        assert calls == 1
        assert slept == []
