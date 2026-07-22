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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._types import BrainResult, BrainConfig

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


# ---------------------------------------------------------------------------
# Direct-caller helpers (sleep cycle, shared-block generation, …)
# ---------------------------------------------------------------------------
# These callers invoke the primary brain *directly* (``make_brain(
# config.brain).execute(req)``) rather than through the executor's fallback-
# wrapped path. ISSUE-181: they must (a) not grind through every channel/block
# when the primary is in a ``usage_limit`` state, and (b) not re-attempt every
# cycle while it stays down. They consult the same process-global breaker the
# executor arms, and feed their own failures back into it so the breaker is a
# single shared signal across *all* brain callers (not just the task path).


def primary_brain_unavailable(brain_config: "BrainConfig") -> tuple[bool, str | None]:
    """Whether non-essential direct callers should skip the primary brain.

    Returns ``(False, reason)`` when the availability breaker is open for the
    primary brain kind — i.e. a persistent unavailability (``usage_limit`` /
    ``not_found``) was reported and the cooldown hasn't elapsed. Returns
    ``(True, None)`` when the primary should be probed.

    Direct (non-executor) brain callers — the sleep cycle, shared-block
    generation — should consult this before each call (or before a batch) so a
    degraded primary doesn't grind through every channel/block and re-attempt
    every cycle. The breaker is opened either by the executor (when a real task
    hits ``usage_limit``/``not_found``) or by :func:`report_brain_result` below
    when one of these direct callers hits it itself, so the signal is shared.

    Honours ``fallback_cooldown_seconds``: ``0`` disables stickiness (every
    caller probes the primary first, matching the executor's contract).
    """
    kind = getattr(brain_config, "kind", "")
    cooldown = getattr(brain_config, "fallback_cooldown_seconds", 0) or 0
    if cooldown <= 0:
        return True, None
    if _BREAKER.should_skip(kind, cooldown):
        return False, "unavailable"
    return True, None


def report_brain_result(
    brain_result: "BrainResult", brain_config: "BrainConfig"
) -> str | None:
    """Feed a direct caller's ``BrainResult`` into the shared availability breaker.

    Opens the breaker (one-shot alert semantics) when the primary reported a
    persistent unavailability (``usage_limit`` / ``not_found``) and closes it on
    a successful completion — mirroring what the executor does for the task
    path, so the breaker stays a single shared signal across every brain caller.

    Returns the ``stop_reason`` when this call *transitioned* the breaker from
    closed→open (so the caller arms exactly one operator alert), else ``None``.
    A call while it is already open (within cooldown) returns ``None`` — the
    first opener owns the alert, preventing per-channel/per-block spam.
    """
    kind = getattr(brain_config, "kind", "")
    cooldown = getattr(brain_config, "fallback_cooldown_seconds", 0) or 0
    if cooldown <= 0:
        return None
    if getattr(brain_result, "success", False):
        _BREAKER.record_success(kind)
        return None
    stop_reason = getattr(brain_result, "stop_reason", "")
    if stop_reason in COOLDOWN_STOP_REASONS and _BREAKER.open(kind, cooldown):
        return stop_reason
    return None


__all__ = [
    "COOLDOWN_STOP_REASONS",
    "PrimaryAvailabilityBreaker",
    "TRIGGER_STOP_REASONS",
    "effective_fallback_kind",
    "get_availability_breaker",
    "primary_brain_unavailable",
    "report_brain_result",
    "reset_availability_breaker",
]
