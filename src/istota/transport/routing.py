"""Outbound delivery routing — the single source of truth for "where does a
task's result go".

A **destination** is ``surface[:channel]``; an **``output_target``** value is a
comma-separated list of destinations stored in the free-text ``tasks.output_target``
column. ``parse_output_target`` turns the string into ``Destination``s (pure,
no I/O); ``resolve_delivery_plan`` turns a task into the ordered, deduplicated,
channel-resolved set of destinations the scheduler delivers to, reproducing the
hardcoded ``output_target`` fan-out that ``process_one_task`` used to do inline.

Surface validity is the planner's job (registry lookup); the parser only parses.
Unknown / unconfigured destinations are dropped with a warning, never raised —
plan resolution must never abort task finalization. For interactive source types
an empty post-drop plan falls back to reply-to-origin so a misconfigured
``output_target`` can never silently eat a reply.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .. import db
    from ..config import Config
    from .registry import TransportRegistry

logger = logging.getLogger("istota.transport.routing")

# Surfaces whose outbound is the task_events log (no push delivery). Web chat
# rides the same substrate as the REPL: the client tails task_events over SSE,
# so there is nothing to push.
_STREAM_SURFACES = frozenset({"stream", "web"})
# Source types that must never silently drop a reply (interactive surfaces).
_INTERACTIVE_SOURCE_TYPES = frozenset({"talk", "email", "repl", "web"})

# Legacy compound aliases, normalized in exactly one place.
_ALIASES: dict[str, list[str]] = {
    "both": ["talk", "email"],
    "all": ["talk", "email", "ntfy"],
}


@dataclass(frozen=True)
class Destination:
    """One resolved (or to-be-resolved) delivery target.

    ``channel`` is ``None`` from the parser when the descriptor had no explicit
    ``:channel`` (resolve at delivery); ``resolve_delivery_plan`` fills it for
    push surfaces that need a durable target (Talk). ``kind`` mirrors the
    transport's ``surface_class`` — ``"push"`` or ``"stream"``.
    """

    surface: str
    channel: str | None = None
    kind: str = "push"


def parse_output_target(spec: str | None) -> list[Destination]:
    """Parse an ``output_target`` string into destinations.

    Normalizes the legacy ``both`` / ``all`` aliases, splits on commas, and
    parses each ``surface[:channel]`` leaf. Returns ``[]`` for ``None`` / empty
    / ``"none"``. Surface validity is **not** checked here — that is the
    registry's job in ``resolve_delivery_plan``. Exact ``(surface, channel)``
    duplicates are collapsed, order preserved.
    """
    if spec is None:
        return []
    text = spec.strip()
    if not text or text.lower() == "none":
        return []

    out: list[Destination] = []
    seen: set[tuple[str, str | None]] = set()
    for raw in text.split(","):
        token = raw.strip()
        if not token:
            continue
        surface_raw, sep, channel_raw = token.partition(":")
        surface = surface_raw.strip().lower()
        if not surface:
            continue
        # `none` is the explicit "deliver nowhere" sentinel — valid both as the
        # whole spec (handled above) and as a list leaf (e.g. a typo'd
        # "talk,none"); drop the leaf rather than emit an unknown-surface warning.
        if surface == "none":
            continue
        channel = channel_raw.strip() if sep else None
        if channel == "":
            channel = None
        # Expand compound aliases (only meaningful with no explicit channel).
        if surface in _ALIASES and channel is None:
            leaves = _ALIASES[surface]
        else:
            leaves = [surface]
        for leaf in leaves:
            chan = None if leaf in _ALIASES else channel
            # An aliased leaf carries no channel; a real surface keeps its own.
            key = (leaf, chan)
            if key in seen:
                continue
            seen.add(key)
            out.append(Destination(leaf, chan))
    return out


def origin_descriptor(task: "db.Task") -> str | None:
    """The ``output_target`` descriptor that routes a follow-up back to the
    surface this task came from, stored on ``sent_emails`` at send time and read
    at inbound-reply time with zero re-resolution.

    Resolves the task's primary surface via ``_surface_for_source_type`` and
    emits ``surface:channel`` (or bare ``surface`` when no durable channel is
    known — delivery resolves it, e.g. a Talk DM). Returns ``None`` for surfaces
    that cannot anchor a pushed reply: ``repl`` (the terminal is gone by reply
    time) and ``email`` (email is the mirror leg, not an origin). A ``None``
    caller falls back to the legacy ``talk,email`` branch. Never raises — an
    unexpected ``source_type`` resolves to the ``talk`` surface like any other.
    """
    from ..email_support import is_synthetic_email_thread_token
    from .registry import _surface_for_source_type

    surface = _surface_for_source_type(task.source_type)
    if surface == "web":
        tok = task.conversation_token
        return f"web:{tok}" if tok else "web"
    if surface == "talk":
        tok = task.talk_delivery_token or task.conversation_token
        # A synthetic email-thread token is not a real Talk room — don't echo it.
        if tok and not is_synthetic_email_thread_token(tok):
            return f"talk:{tok}"
        return "talk"  # bare talk → resolve_target / DM at delivery
    # repl (no durable push target) and email (the mirror leg) are not origins.
    return None


def plan_has_surface(plan: list[Destination], surface: str) -> bool:
    """True if any destination in ``plan`` targets ``surface``. The replacement
    for the old ``target in ("talk", "both", "all")`` string checks."""
    return any(d.surface == surface for d in plan)


def _infer_default_plan(task: "db.Task") -> list[Destination]:
    """Reproduce process_one_task's source_type → default target inference for
    tasks with no explicit ``output_target``."""
    st = task.source_type
    if st in ("talk", "briefing"):
        return [Destination("talk")]
    if st == "email":
        return [Destination("email")]
    if st == "istota_file":
        return [Destination("istota_file")]
    if st == "repl":
        return [Destination("stream", "stream", "stream")]
    if st == "web":
        return [Destination("web", "stream", "stream")]
    return []


def _resolve_talk_channel(config: "Config", task: "db.Task") -> str | None:
    # Lazy import: scheduler imports the transport package at module load.
    from ..scheduler import _talk_target_for_delivery
    return _talk_target_for_delivery(config, task)


def _resolve_one(
    config: "Config", task: "db.Task",
    registry: "TransportRegistry | None", dest: Destination,
) -> Destination | None:
    surface = dest.surface

    if surface in _STREAM_SURFACES:
        return Destination(surface, dest.channel or "stream", "stream")

    if surface == "talk":
        channel = dest.channel or _resolve_talk_channel(config, task)
        if not channel:
            logger.warning(
                "Dropping talk destination for task %s: no resolvable Talk channel",
                getattr(task, "id", "?"),
            )
            return None
        return Destination("talk", channel, "push")

    if surface == "email":
        # Recipient is resolved at delivery from the task's email thread; the
        # channel is advisory. Mirrors today's unconditional post_email.
        return Destination("email", dest.channel, "push")

    if surface in ("ntfy", "istota_file"):
        # Resolved at delivery (Stage 1: inline; Stage 2: their transports).
        return Destination(surface, dest.channel, "push")

    # Any other surface must be a registered transport (Matrix, web chat,
    # future). Unknown / unconfigured-at-user-level → drop with a warning.
    transport = registry.get(surface) if registry is not None else None
    if transport is None:
        logger.warning(
            "Dropping unknown delivery surface %r for task %s",
            surface, getattr(task, "id", "?"),
        )
        return None
    surface_class = getattr(transport.capabilities, "surface_class", "push")
    if surface_class == "stream":
        return Destination(surface, dest.channel or "stream", "stream")
    channel = dest.channel or transport.resolve_target(task)
    if not channel:
        logger.warning(
            "Dropping %s destination for task %s: surface configured but no "
            "user-level channel resolved",
            surface, getattr(task, "id", "?"),
        )
        return None
    return Destination(surface, channel, "push")


def _reply_origin_destination(
    config: "Config", task: "db.Task",
) -> Destination | None:
    """The reply-to-origin fallback for interactive tasks whose plan resolved
    empty — never eat an interactive reply."""
    st = task.source_type
    if st == "email":
        return Destination("email", None, "push")
    if st == "repl":
        return Destination("stream", "stream", "stream")
    if st == "web":
        return Destination("web", "stream", "stream")
    channel = _resolve_talk_channel(config, task)
    if not channel:
        return None
    return Destination("talk", channel, "push")


def resolve_delivery_plan(
    config: "Config", task: "db.Task", registry: "TransportRegistry | None",
) -> list[Destination]:
    """Resolve the ordered, deduplicated set of destinations for a task result.

    Precedence: explicit ``task.output_target`` > reply-to-origin (interactive
    source types) > source-type default > drop. For each destination the
    channel is filled (Talk via the synthetic-email-token fallback that
    ``_talk_target_for_delivery`` uses) or the destination is dropped (logged at
    WARNING) when its surface is unregistered or its user-level channel resolves
    to ``None``. Never raises into the caller.
    """
    spec = task.output_target
    plan = parse_output_target(spec)
    if not plan and (spec is None or not spec.strip()):
        plan = _infer_default_plan(task)

    resolved: list[Destination] = []
    seen: set[tuple[str, str | None]] = set()
    for dest in plan:
        r = _resolve_one(config, task, registry, dest)
        if r is None:
            continue
        key = (r.surface, r.channel)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(r)

    if not resolved and task.source_type in _INTERACTIVE_SOURCE_TYPES:
        fb = _reply_origin_destination(config, task)
        if fb is not None:
            resolved.append(fb)

    return resolved
