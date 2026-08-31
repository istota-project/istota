"""Availability failover between brains (brain-fallback spec).

Two pieces live here:

- ``effective_fallback_kind`` — resolves the configured ``[brain] fallback`` into
  the brain kind to fall back to. Explicit config only: no brain kind has an
  implicit failover target (ISSUE-362).
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

import logging
import math
import threading
import time
from typing import NamedTuple, TYPE_CHECKING

if TYPE_CHECKING:
    from ._types import BrainResult, BrainConfig

logger = logging.getLogger(__name__)

# The stop_reasons that reroute *this attempt* to the fallback brain.
# ``transient_api_error`` is added conditionally (fallback_on_transient).
TRIGGER_STOP_REASONS: frozenset[str] = frozenset({"usage_limit", "not_found", "fallback"})

# The stop_reasons that open the availability breaker (skip the primary on
# subsequent tasks). Only genuinely persistent conditions — a quota window is
# hours; a missing binary won't reappear mid-run. ``fallback`` is excluded so
# tmux's own probing cadence (its launch _CircuitBreaker) is preserved;
# ``transient_api_error`` is excluded (transient by definition).
COOLDOWN_STOP_REASONS: frozenset[str] = frozenset({"usage_limit", "not_found"})

# The shortest window a caller-supplied deadline may produce. A quota that
# resets in four seconds would otherwise open a breaker that does nothing: the
# next task probes the primary, fails again because the reset has not quite
# landed, and reopens — a failed attempt per task for as long as it takes. One
# minute is short enough that a genuine reset is barely delayed and long enough
# that the breaker is worth having. Clamped down to ``cooldown`` when an
# operator configured something shorter than the floor.
MIN_COOLDOWN_SECONDS: float = 60.0

# The brain kinds whose ``usage_limit`` is the Claude subscription's. The reset
# hint comes from Anthropic's own usage endpoint, so it describes these two and
# nothing else — a ``native`` brain's provider has its own quota on its own
# clock, and reading this one for it would be a guess wearing a number.
SUBSCRIPTION_BRAIN_KINDS: frozenset[str] = frozenset({"claude_code", "tmux_claude"})


def effective_fallback_kind(brain_config) -> str | None:
    """The configured ``[brain] fallback`` for ``brain_config``, or None.

    Failover happens only where an operator named a kind; every brain kind is
    treated alike. ``tmux_claude`` used to resolve to ``claude_code`` here with
    nothing configured — a shim from before ``[brain] fallback`` existed — which
    inverted what an empty setting meant (there was no value of ``fallback``
    that turned failover off on a tmux deployment) and made
    ``_validate_brain_fallback``'s two "disabling fallback" warnings false:
    blanking the field is what activated the implicit target. Removed in
    ISSUE-362. Kept out of ``config.py`` so brain-kind logic doesn't leak into
    config.

    A fallback equal to this config's own ``kind`` resolves to None, because
    rerunning the same brain cannot help. That test belongs **here** rather than
    only at config load, because ``brain_config`` may be a *routed* config:
    ``resolve_brain_kind`` returns ``replace(brain_config, kind=target)`` for a
    ``source_type_overrides`` entry, inheriting ``fallback``. So
    ``kind = "claude_code"`` routing ``scheduled`` to tmux with
    ``fallback = "claude_code"`` is a self-fallback for an interactive task and a
    real target for a scheduled one, and only the resolved config can tell them
    apart. Config load keeps its own warning for the case where *no* kind the
    deployment can run would benefit.
    """
    configured = (getattr(brain_config, "fallback", "") or "").strip()
    if not configured or configured == getattr(brain_config, "kind", ""):
        return None
    return configured


class _OpenWindow(NamedTuple):
    """When the breaker opened, and when it stops skipping. Both monotonic."""

    opened_at: float
    deadline: float


class PrimaryAvailabilityBreaker:
    """Process-global availability breaker keyed by primary brain kind.

    Simpler than a consecutive-failure counter: a usage limit is authoritative on
    the first hit, so ``open`` marks the kind unavailable immediately. Thread-safe
    (the daemon runs a worker pool).

    The state is a **deadline**, not a duration (ISSUE-374). The two differ
    whenever a caller knows when the condition actually ends — a quota window's
    reset — and the difference is the whole failure the flat cooldown produced:
    a limit hit eleven minutes before the reset held every task on the fallback
    brain for the remaining forty-nine, with the primary idle and available.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # kind -> the window it is open for.
        self._open: dict[str, _OpenWindow] = {}

    def open(self, kind: str, cooldown: float, *, until: float | None = None) -> bool:
        """Mark ``kind`` unavailable until ``until``, or for ``cooldown`` seconds.

        ``until`` is an absolute ``time.monotonic`` deadline for callers that know
        when the condition ends. It is clamped in both directions, inside the
        lock where ``opened_at`` is known: never past ``opened_at + cooldown``, so
        a wrong reset time cannot pin the deployment to its fallback, and never
        below :data:`MIN_COOLDOWN_SECONDS` from now (itself capped by ``cooldown``),
        so a reset moments away does not produce a breaker that does nothing. A
        non-finite ``until`` is discarded rather than propagated — ``NaN`` loses
        every comparison, which would leave a breaker that never skips.

        Returns True iff this call transitioned the breaker from closed→open for
        ``kind`` (so the caller arms exactly one operator alert). A call while it
        is already open returns False and **leaves the existing deadline
        untouched** — a repeated failure within the window does not push it out,
        and neither does a later ``until``. So the window is anchored to the first
        failure and the breaker reliably reopens for a fresh probe, rather than
        being held open indefinitely by a caller that keeps re-reporting the same
        unavailability.
        """
        with self._lock:
            now = time.monotonic()
            window = self._open.get(kind)
            if window is not None and now < window.deadline:
                return False
            ceiling = now + cooldown
            deadline = ceiling
            if until is not None and math.isfinite(until):
                floor = now + min(MIN_COOLDOWN_SECONDS, cooldown)
                deadline = min(max(until, floor), ceiling)
            self._open[kind] = _OpenWindow(opened_at=now, deadline=deadline)
            return True

    def should_skip(self, kind: str, cooldown: float) -> bool:
        """True while ``kind`` is open and its deadline hasn't passed.

        ``cooldown`` is read only as the stickiness switch it has always also
        been: ``0`` means every caller probes the primary. The length of an open
        window is the deadline recorded at ``open``, so changing the setting no
        longer retroactively resizes a window already in force — an operator who
        lowers it to unstick a deployment waits out the armed window or restarts
        the daemon, which clears this state.
        """
        if cooldown <= 0:
            return False
        with self._lock:
            window = self._open.get(kind)
            return window is not None and time.monotonic() < window.deadline

    def remaining(self, kind: str) -> float | None:
        """Seconds left on ``kind``'s open window, or None if it isn't open.

        The number the caller publishes to sibling processes, so the file's
        ``expires_at`` and the breaker in force describe one window rather than
        two.
        """
        with self._lock:
            window = self._open.get(kind)
            if window is None:
                return None
            left = window.deadline - time.monotonic()
            return left if left > 0 else None

    def record_success(self, kind: str, *, started_at: float | None = None) -> None:
        """A primary probe for ``kind`` succeeded — close its older breaker state."""
        with self._lock:
            window = self._open.get(kind)
            if started_at is None or window is None or window.opened_at <= started_at:
                self._open.pop(kind, None)

    def reset(self) -> None:
        """Clear all state (test/teardown)."""
        with self._lock:
            self._open.clear()


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


def usage_reset_deadline(config, kind: str, reason: str) -> float | None:
    """A ``time.monotonic`` deadline for ``kind``'s cooldown, or None.

    Only ``usage_limit`` gets one, and only for a brain running on the Claude
    subscription. ``not_found`` is a missing binary — a quota reset says nothing
    about when one reappears — and a ``native`` primary's limit belongs to a
    different provider on a different clock.

    ``cached_reset_seconds`` reads the disk cache and nothing else, so this costs
    no request, no credential resolution and no socket on a path a failing task
    is already standing on. No cache, a disabled endpoint or a window that has
    already reset all give None, which leaves the flat cooldown in force.
    """
    if config is None or reason != "usage_limit" or kind not in SUBSCRIPTION_BRAIN_KINDS:
        return None
    try:
        from ..subscription_usage import cached_reset_seconds

        seconds = cached_reset_seconds(config, now_ts=time.time())
    except Exception:  # noqa: BLE001 — a missing hint costs the hint, nothing else
        # Logged rather than swallowed in silence: None here is
        # indistinguishable from "no cache", and both restore the flat
        # cooldown, so without a line here a deployment cannot tell a working
        # fix from an inert one.
        logger.debug("brain fallback: quota reset lookup failed", exc_info=True)
        return None
    if seconds is None:
        return None
    return time.monotonic() + seconds


def open_primary_breaker(
    kind: str, cooldown: float, reason: str, *, config=None
) -> float | None:
    """Open the availability breaker for ``kind`` and publish the same window.

    The one place the deadline is computed, so nothing downstream can describe a
    different window than the one in force (ISSUE-374). Both openers — the
    executor's task path and :func:`report_brain_result` for the direct
    callers — go through here.

    Returns the seconds actually armed iff this call transitioned the breaker
    closed→open, else None. The caller reads that both to arm exactly one
    operator alert and to say in it when the primary comes back; reading
    ``fallback_cooldown_seconds`` for the second is what made the alert name a
    window nothing was holding.

    The ``should_skip`` pre-check is not the guard (``open`` decides that, under
    its lock); it only keeps the cache read off the path of every repeat failure
    inside an already-open window. Losing the race costs one disk read.

    A ``remaining`` of None after a successful ``open`` means a concurrent
    ``record_success`` closed the breaker in between — the primary answered, so
    there is nothing to publish and nobody to alert. Substituting ``cooldown``
    there was the one place the replaced value could still reach the status
    file, and for a breaker that is no longer open at all.

    **It does not shorten a window already in force**, only refuse to extend
    one. A better deadline can only arrive on a second failure, and once the
    breaker is open every caller skips the primary — the executor's
    ``_skip_primary``, and ``primary_brain_unavailable`` for the direct
    callers — so on a deployment with a fallback configured there is no second
    failure to carry one. Where there is (no fallback configured, so tasks keep
    calling the primary), shortening would mean republishing the status file,
    which rewrites ``opened_at`` and would break ``clear_unavailable``'s
    ``started_at`` guard. The window self-corrects at its own expiry instead,
    for the cost of one failed probe.
    """
    if cooldown <= 0:
        return None
    until = None
    if not _BREAKER.should_skip(kind, cooldown):
        until = usage_reset_deadline(config, kind, reason)
    if not _BREAKER.open(kind, cooldown, until=until):
        return None
    armed = _BREAKER.remaining(kind)
    if armed is None:
        return None
    logger.info(
        "brain availability: %s unavailable (%s), skipping it for %ds (%s)",
        kind, reason, round(armed),
        "quota reset" if until is not None else "flat cooldown",
    )
    if config is not None:
        from ..brain_availability import record_unavailable

        record_unavailable(config, kind, reason, cooldown_seconds=armed)
    return armed


def report_brain_result(
    brain_result: "BrainResult", brain_config: "BrainConfig", *, config=None,
    started_at: float | None = None, started_monotonic: float | None = None,
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
        _BREAKER.record_success(kind, started_at=started_monotonic)
        if config is not None:
            from ..brain_availability import clear_unavailable

            clear_unavailable(config, kind, started_at=started_at)
        return None
    stop_reason = getattr(brain_result, "stop_reason", "")
    if stop_reason not in COOLDOWN_STOP_REASONS:
        return None
    armed = open_primary_breaker(kind, cooldown, stop_reason, config=config)
    return stop_reason if armed is not None else None


__all__ = [
    "COOLDOWN_STOP_REASONS",
    "MIN_COOLDOWN_SECONDS",
    "PrimaryAvailabilityBreaker",
    "SUBSCRIPTION_BRAIN_KINDS",
    "TRIGGER_STOP_REASONS",
    "effective_fallback_kind",
    "get_availability_breaker",
    "open_primary_breaker",
    "primary_brain_unavailable",
    "report_brain_result",
    "reset_availability_breaker",
    "usage_reset_deadline",
]
