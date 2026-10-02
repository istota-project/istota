"""The ``Retry-After`` parser, in one place.

RFC 9110 gives the header two spellings and a server is free to use either, so
both are read: ``delta-seconds`` (``Retry-After: 2327``) and an HTTP-date
(``Retry-After: Sat, 22 Aug 2026 23:42:17 GMT``).

Extracted from ``subscription_usage.py``, which paid for the hardening and
still re-exports it, when the feeds poller became a second caller (ISSUE-347).
Same reasoning as ``usage_render.py`` and ``git_hardening.py``: a second copy of
a parser whose whole job is to be right about malformed input is what makes the
next one a third, and the copies drift on exactly the inputs nobody thought of.
``brain/claude_code.py`` keeps its own ``parse_retry_after`` and is deliberately
not folded in here — it scrapes a number out of a CLI error *message*, which is
a different job from reading a header off a response.

stdlib-only leaf: imports nothing from the package, so a caller with a light
import graph can reach it without pulling in a heavy one.
"""

from __future__ import annotations

import math
import re
from datetime import timezone
from typing import Any, Mapping


def parse_retry_after(value: Any, *, now_ts: float) -> float | None:
    """``Retry-After`` as seconds from ``now_ts``, or ``None`` if unusable.

    A date already in the past floors at 0 rather than going negative — clock
    skew between two hosts is ordinary, and a negative backoff would read as
    "retry before now" to arithmetic that never expects one.

    ``None`` for anything else: absent, blank, a float (``delta-seconds`` is an
    integer, and a server sending ``1.5`` is not one this should guess for), a
    negative integer, a non-finite value, or a date that will not parse. The
    caller then falls back to its own interval — an unreadable hint costs
    nothing. ``None`` is deliberately distinct from ``0.0``: a server naming no
    time and a server naming now are different facts, and a caller that
    defaulted the first to a number could not tell them apart.

    Never raises. Both callers are on a failure path — a rate-limited response
    — where an exception would turn a throttle into a crash.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001 — a __str__ that raises is still just unusable
        return None
    if not text:
        return None

    # delta-seconds first: it is the common spelling and the cheap parse.
    #
    # `re.fullmatch(r"\d+")` rather than `str.isdigit()`, which is true for
    # characters `int()` then rejects — `"²".isdigit()` is True and `int("²")`
    # raises, which on a caller that catches broadly turns a 429 back into the
    # generic error this parser exists to avoid. `\d` matches the Nd category,
    # every member of which `int()` accepts.
    if re.fullmatch(r"\d+", text):
        try:
            seconds = float(int(text))
        except (ValueError, OverflowError):
            # A digit string long enough to overflow a float is not a delay.
            return None
        return seconds if math.isfinite(seconds) else None

    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(text)
    except Exception:  # noqa: BLE001 — a malformed date is a missing hint
        return None
    if when is None:
        return None
    try:
        if when.tzinfo is None:
            # RFC 9110 dates are GMT; a naive one means the sender omitted the
            # zone, not that it meant local time on *this* host.
            when = when.replace(tzinfo=timezone.utc)
        delta = when.timestamp() - now_ts
    except (ValueError, OverflowError, OSError):
        return None
    if not math.isfinite(delta):
        return None
    return max(0.0, delta)


def retry_after_from_headers(
    headers: Mapping[str, str] | None, *, now_ts: float,
) -> float | None:
    """``Retry-After`` out of a response's headers, case-insensitively.

    Header names are case-insensitive per RFC 9110, and neither a stub
    transport in a test nor a plain ``dict`` standing in for a real client's
    header mapping is under any obligation to match a particular
    capitalization, so the lookup folds case rather than trusting either.
    """
    if not headers:
        return None
    try:
        for name, value in headers.items():
            if str(name).strip().lower() == "retry-after":
                return parse_retry_after(value, now_ts=now_ts)
    except Exception:  # noqa: BLE001 — a mapping that will not iterate has no hint in it
        return None
    return None
