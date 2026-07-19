"""Availability failover between brains (brain-fallback spec).

Two pieces live here:

- ``effective_fallback_kind`` — resolves the configured ``[brain] fallback`` into
  the brain kind to fall back to, encoding the tmux back-compat default
  (a ``tmux_claude`` primary falls back to ``claude_code`` unless overridden).
- ``PrimaryAvailabilityBreaker`` — a process-global, thread-safe breaker keyed by
  primary brain kind. Once a primary reports a *persistent* unavailability
  (``usage_limit`` / ``not_found``), subsequent tasks skip it for a cooldown
  instead of paying a failed primary attempt each time; the cooldown auto-resets
  when a primary probe succeeds.

This breaker is deliberately distinct from ``tmux_claude._BREAKER`` (which
governs tmux's launch-failure fast-fail). The two compose: tmux fails fast →
executor sees ``fallback`` (not a cooldown reason) → keeps probing tmux;
a ``usage_limit`` from *any* primary opens *this* breaker → skips the primary
for the cooldown. Kept executor-agnostic (no ``Config``) so the executor owns
alert dispatch.
"""

from __future__ import annotations

import threading
import time

# The stop_reasons that reroute *this attempt* to the fallback brain.
# ``transient_api_error`` is added conditionally (fallback_on_transient).
TRIGGER_STOP_REASONS: frozenset[str] = frozenset({"usage_limit", "not_found", "fallback"})

# The stop_reasons that open the availability breaker (skip the primary on
# subsequent tasks). Only genuinely persistent conditions — a quota window is
# hours; a missing binary won't reappear mid-run. ``fallback`` is excluded so
# tmux's own probing cadence (its launch _CircuitBreaker) is preserved;
# ``transient_api_error`` is excluded (transient by definition).
COOLDOWN_STOP_REASONS: frozenset[str] = frozenset({"usage_limit", "not_found"})


def effective_fallback_kind(brain_config) -> str | None:
    """The brain kind to fall back to for ``brain_config``, or None.

    Precedence: the configured ``fallback`` if set; else the tmux back-compat
    default (``claude_code`` for a ``tmux_claude`` primary — preserving the old
    hardcoded behaviour); else None. Kept out of ``config.py`` so brain-kind
    logic doesn't leak into config.
    """
    configured = (getattr(brain_config, "fallback", "") or "").strip()
    if configured:
        return configured
    if getattr(brain_config, "kind", "") == "tmux_claude":
        return "claude_code"
    return None


class PrimaryAvailabilityBreaker:
    """Process-global availability breaker keyed by primary brain kind.

    Simpler than a consecutive-failure counter: a usage limit is authoritative on
    the first hit, so ``open`` marks the kind unavailable immediately for a
    cooldown. Thread-safe (the daemon runs a worker pool).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # kind -> monotonic timestamp when the breaker opened.
        self._opened_at: dict[str, float] = {}

    def open(self, kind: str, cooldown: float) -> bool:
        """Mark ``kind`` unavailable for ``cooldown`` seconds.

        Returns True iff this call transitioned the breaker from closed→open for
        ``kind`` (so the caller arms exactly one operator alert). A call while it
        is already open (within cooldown) returns False.
        """
        with self._lock:
            now = time.monotonic()
            opened = self._opened_at.get(kind)
            already_open = opened is not None and (now - opened) < cooldown
            self._opened_at[kind] = now
            return not already_open

    def should_skip(self, kind: str, cooldown: float) -> bool:
        """True while ``kind`` is open and its cooldown hasn't elapsed."""
        with self._lock:
            opened = self._opened_at.get(kind)
            if opened is None:
                return False
            return (time.monotonic() - opened) < cooldown

    def record_success(self, kind: str) -> None:
        """A primary probe for ``kind`` succeeded — close the breaker."""
        with self._lock:
            self._opened_at.pop(kind, None)

    def reset(self) -> None:
        """Clear all state (test/teardown)."""
        with self._lock:
            self._opened_at.clear()


# Process-global instance (per daemon; a restart resets it, which also matches a
# fresh quota window).
_BREAKER = PrimaryAvailabilityBreaker()


def get_availability_breaker() -> PrimaryAvailabilityBreaker:
    return _BREAKER


def reset_availability_breaker() -> None:
    """Reset the process-global breaker (test/teardown helper)."""
    _BREAKER.reset()


__all__ = [
    "COOLDOWN_STOP_REASONS",
    "PrimaryAvailabilityBreaker",
    "TRIGGER_STOP_REASONS",
    "effective_fallback_kind",
    "get_availability_breaker",
    "reset_availability_breaker",
]
