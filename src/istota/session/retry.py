"""Error classification and backoff retry (session layer).

Errors are classified here, not in the agent loop. The split (Pi's): context
overflow → compaction (not retryable), transient errors → retry with backoff,
everything else → fail. The retry counter resets on any success.

Prior art:
- Pi's AgentSession._handleRetryableError() — regex classification, exponential
  backoff, counter reset on success.
- Hermes's classify_api_error() — status-code classification, jittered backoff.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger("istota.session.retry")


@dataclass
class ErrorClassification:
    retryable: bool
    is_context_overflow: bool
    is_rate_limit: bool
    is_auth_error: bool
    category: str  # "transient" | "overflow" | "auth" | "permanent"
    status_code: int | None = None
    retry_after: float | None = None


_OVERFLOW_PATTERNS = re.compile(
    r"context.?length|context.?window|context.?size|too.?many.?tokens|"
    r"maximum.?context|prompt.?is.?too.?long|input.?too.?large|"
    # "exceeds the maximum/available/model's context", llama.cpp / vLLM / etc.
    r"exceeds.{0,40}context|reduce.?the.?(length|number.?of.?(messages|tokens))",
    re.IGNORECASE,
)

# Transient signals matched on the error *text* only (used when no status code
# is available). Deliberately phrase-based, not bare numbers/words: a permanent
# 400 whose body happens to quote "503" or "timeout" must not read as transient
# (NB-13a). Real 5xx are handled by the status-code branch, not this regex.
_OVERLOADED_PATTERNS = re.compile(
    r"overloaded|"
    r"rate.?limit|too.?many.?requests|"
    r"connection.?(error|reset|refused|timed?.?out)|"
    r"ECONNRESET|ECONNREFUSED|"
    r"network.?error|socket.?hang.?up|read.?timed?.?out|request.?timed?.?out",
    re.IGNORECASE,
)

# Rate-limit signals in error text (word-boundaried so "generate" doesn't match
# on a bare "rate", NB-13b).
_RATE_LIMIT_PATTERNS = re.compile(
    r"rate.?limit|too.?many.?requests|\b429\b",
    re.IGNORECASE,
)


def classify_error(error_message: str, status_code: int | None = None) -> ErrorClassification:
    """Classify an LLM error for retry / compaction decisions.

    Overflow is checked first so a ``400`` carrying a context-length message is
    routed to compaction rather than treated as a permanent client error.
    """
    error_message = error_message or ""

    # A hard rate-limit status wins over any overflow-looking text: a tokens-
    # per-minute 429 may read "too many tokens" but it's a rate limit, not a
    # context overflow (NB-13). Checked before the overflow-text match.
    if status_code == 429:
        return ErrorClassification(
            retryable=True,
            is_context_overflow=False,
            is_rate_limit=True,
            is_auth_error=False,
            category="transient",
            status_code=status_code,
        )

    if _OVERFLOW_PATTERNS.search(error_message):
        return ErrorClassification(
            retryable=False,
            is_context_overflow=True,
            is_rate_limit=False,
            is_auth_error=False,
            category="overflow",
            status_code=status_code,
        )

    if status_code in (401, 403):
        return ErrorClassification(
            retryable=False,
            is_context_overflow=False,
            is_rate_limit=False,
            is_auth_error=True,
            category="auth",
            status_code=status_code,
        )

    if status_code is None and _RATE_LIMIT_PATTERNS.search(error_message):
        return ErrorClassification(
            retryable=True,
            is_context_overflow=False,
            is_rate_limit=True,
            is_auth_error=False,
            category="transient",
            status_code=status_code,
        )

    if status_code is not None and status_code >= 500:
        return ErrorClassification(
            retryable=True,
            is_context_overflow=False,
            is_rate_limit=False,
            is_auth_error=False,
            category="transient",
            status_code=status_code,
        )

    if _OVERLOADED_PATTERNS.search(error_message):
        return ErrorClassification(
            retryable=True,
            is_context_overflow=False,
            is_rate_limit=False,
            is_auth_error=False,
            category="transient",
            status_code=status_code,
        )

    return ErrorClassification(
        retryable=False,
        is_context_overflow=False,
        is_rate_limit=False,
        is_auth_error=False,
        category="permanent",
        status_code=status_code,
    )


async def retry_with_backoff(
    run_fn: Callable[[], Awaitable],
    *,
    max_retries: int = 3,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    abort: asyncio.Event | None = None,
    on_retry: Callable[[int, int, float, str], None] | None = None,
):
    """Run ``run_fn`` with exponential backoff on retryable failures.

    ``run_fn`` returns a result object exposing ``.success`` (bool) and, on
    failure, ``.error_message`` / ``.status_code``. The retry sleep is
    interruptible via the ``abort`` event (``asyncio.wait_for`` on the event,
    no polling), so ``!stop`` lands during backoff without a poll interval.

    Failure modes:
    - abort set during the sleep → return the last error result
    - max_retries exhausted → return the last error result
    - non-retryable error → return immediately
    """
    retry_count = 0
    result = await run_fn()

    while True:
        if result.success:
            return result

        classification = classify_error(
            getattr(result, "error_message", "") or "",
            getattr(result, "status_code", None),
        )

        if not classification.retryable or retry_count >= max_retries:
            return result

        retry_count += 1
        # Jitter first, then cap — so max_delay is a true ceiling, not 1.5× it
        # (NB-13). Multiplier in [0.5, 1.5).
        delay = base_delay * (2 ** (retry_count - 1)) * (0.5 + random.random())
        delay = min(delay, max_delay)

        if on_retry:
            on_retry(retry_count, max_retries, delay, getattr(result, "error_message", "") or "")

        logger.warning(
            "Retryable error (attempt %d/%d), waiting %.1fs: %s",
            retry_count,
            max_retries,
            delay,
            (getattr(result, "error_message", "") or "")[:200],
        )

        if abort is not None and delay > 0:
            try:
                await asyncio.wait_for(abort.wait(), timeout=delay)
                return result  # abort tripped during the sleep
            except asyncio.TimeoutError:
                pass  # slept the full delay; retry
        elif delay > 0:
            await asyncio.sleep(delay)

        result = await run_fn()
