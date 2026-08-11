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
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .. import db
    from ..config import Config
    from .registry import TransportRegistry

logger = logging.getLogger("istota.transport.routing")

# Latch for the missing-room-tables warning in `_talk_binding_for_task`. Once
# per process: the condition is static for the life of a deployment and the
# lookup runs on every delivery.
_WARNED_NO_ROOM_TABLES = False

# Surfaces whose outbound is the task_events log (no push delivery). Web chat
# rides the same substrate as the REPL: the client tails task_events over SSE,
# so there is nothing to push. This governs the planner's push-vs-stream
# short-circuit in `_resolve_one` and nothing else — the room fan-out asks
# `TransportCapabilities.room_view` instead (see `_expand_room_destinations`),
# which is a different question that happens to have the same answer today.
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

    ``mirror`` marks a destination produced by the ``room`` fan-out for a
    *non-origin* bound surface (e.g. a web-origin task mirrored to its bound
    Talk room). The scheduler suppresses the confirmation prompt on a mirror
    leg — confirmations stay on the originating surface (open question 7).
    """

    surface: str
    channel: str | None = None
    kind: str = "push"
    mirror: bool = False


def parse_output_target(spec: str | None) -> list[Destination]:
    """Parse an ``output_target`` string into destinations.

    Normalizes the legacy ``both`` / ``all`` aliases, splits on commas, and
    parses each ``surface[:channel]`` leaf. Returns ``[]`` for ``None`` / empty
    / ``"none"``. Surface validity is **not** checked here — that is the
    registry's job in ``resolve_delivery_plan``. Exact ``(surface, channel)``
    duplicates are collapsed, order preserved.

    ``room`` and ``room:<token>`` parse as an ordinary leaf and need no special
    case here, but they are not surfaces: ``room`` is a meta-destination that
    ``_expand_room_destinations`` replaces at resolve time with the room's live
    bindings. Bare ``room`` means the task's own channel; the token form names
    the room explicitly, which is what a stored origin descriptor carries.
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


def _room_descriptor(conn, surface: str, task: "db.Task") -> str | None:
    """``room:<canonical_token>`` when this task's channel is a registered live
    room, else None. Never raises — a descriptor is best-effort, and the
    surface-qualified fallback still routes.

    Two candidate tokens, tried in order: the task's own ``conversation_token``,
    then ``talk_delivery_token``. The second is there because a task whose
    channel is a synthetic email-thread hash can still carry the real Talk room
    separately, and stamping the surface form for it would leave exactly the
    single-leg descriptor this stage exists to stop writing. (Stage 4 retires
    that column; until then it is a real source of a room name.)

    Each candidate is resolved to a canonical token by
    ``_canonical_room_token``, because a raw ref is not a room id — comparing
    the two directly is the mistake this whole spec is cleaning up.

    A ``repl`` origin is excluded even when its token names a room. The terminal
    is gone by reply time, and the room expansion would deliver to it as a
    stream destination that no client is tailing.
    """
    if conn is None or surface == "repl":
        return None
    from ..email_support import is_synthetic_email_thread_token

    candidates = [task.conversation_token, task.talk_delivery_token]
    try:
        from .. import db

        for token in candidates:
            if not token or is_synthetic_email_thread_token(token):
                continue  # an email thread hash names no room
            # Cross-surface: an email continuation's token belongs to whichever
            # surface the originating send recorded, and its own surface owns no
            # bindings. A wrong answer here is a wrong *descriptor*, which live
            # bindings re-resolve at delivery — unlike a wrong channel.
            canonical = _canonical_room_token(
                conn, surface, token, cross_surface=True,
            )
            if canonical is None:
                continue
            room = db.get_room(conn, canonical)
            if room is None or getattr(room, "archived", 0):
                continue
            return f"room:{canonical}"
        return None
    except Exception as e:  # pragma: no cover - best-effort, never abort a send
        logger.warning("room descriptor lookup failed for task %s: %s",
                       getattr(task, "id", "?"), e)
        return None


def _canonical_room_token(
    conn, surface: str, token: str, *, cross_surface: bool,
) -> str | None:
    """The canonical room token a raw token names, or None if it names no room.

    Three tries, narrowest first: the token already *is* a canonical token; it
    is this surface's ref for one; it is *some other* surface's ref for one.

    The third is not hypothetical. An email continuation's
    ``conversation_token`` is whatever the originating send recorded — on a
    promoted room, the Talk ref — while the task's own surface is ``email``,
    which owns no bindings, so a surface-scoped lookup can only ever miss and
    the room reads as unregistered.

    ``cross_surface=False`` drops that third try, and delivery *must* pass it.
    A surface ref is unique only within its surface, so an unscoped match can
    resolve a token to a room that merely shares the string on another surface
    — and on the delivery path the consequence is posting the answer into a
    different conversation. For a talk-sourced task the destination is
    definitionally its ``conversation_token``; the binding rung is only an
    improvement on that while it is looking up the *same* room.

    The parameter has no default on purpose. Both answers are defensible and the
    difference is invisible at the call site, so each caller states which it
    wants rather than inheriting one. Stamping a descriptor passes True — the
    cross-surface case is the whole reason it can find a promoted room's token
    at all — and lives with the collision risk because a wrong descriptor is
    re-resolved by live bindings at delivery, where a wrong *channel* is not.
    """
    from .. import db

    if db.get_room(conn, token) is not None:
        return token
    scoped = db.resolve_room_token(conn, surface, token)
    if scoped is not None or not cross_surface:
        return scoped
    return db.find_room_token_by_ref(conn, token)


def origin_descriptor(task: "db.Task", conn=None) -> str | None:
    """The ``output_target`` descriptor that routes a follow-up back to the
    surface this task came from, stored on ``sent_emails`` at send time and read
    at inbound-reply time with zero re-resolution.

    **When the origin is a registered room, the descriptor names the room**
    (``room:<canonical_token>``) rather than one of its views. A room is one
    conversation that can be bound to several surfaces, so recording the leg the
    send happened to go out on throws away the fact that it was a room at all —
    and the reply then reaches that leg alone, leaving the other view of the
    conversation blank in a room where the user watched the question arrive.
    ``room`` re-expands by live bindings at delivery, so it also picks up a
    binding added *after* the send ("Also open in Talk" is exactly that).

    Requires ``conn`` to answer that. Without one it falls back to the
    surface-qualified form, which is what every caller emitted before rooms
    existed and is still correct for a destination that is not a room: a Talk DM
    with no registered room, or a genuine email-only thread.

    Otherwise resolves the task's primary surface via ``_surface_for_source_type``
    and emits ``surface:channel`` (or bare ``surface`` when no durable channel is
    known — delivery resolves it). A ``None`` return falls back to the legacy
    ``talk,email`` branch at the reply site. Never raises — an unexpected
    ``source_type`` resolves to the ``talk`` surface like any other.

    An ``email``-source task is the subtle case: it may be a *continuation* of a
    non-email origin (we are handling a reply to an email a web/Talk conversation
    asked us to send), in which case ``conversation_token`` still holds the origin
    room and we recover the origin from it so the *next* round routes back there
    too. A genuine email-only thread carries a synthetic thread token → no origin.
    ``repl`` is never a pushable origin (the terminal is gone by reply time).
    """
    from ..email_support import is_synthetic_email_thread_token
    from .registry import _surface_for_source_type

    surface = _surface_for_source_type(task.source_type)
    room = _room_descriptor(conn, surface, task)
    if room is not None:
        return room
    if surface == "web":
        tok = task.conversation_token
        return f"web:{tok}" if tok else "web"
    if surface == "talk":
        tok = task.talk_delivery_token or task.conversation_token
        # A synthetic email-thread token is not a real Talk room — don't echo it.
        if tok and not is_synthetic_email_thread_token(tok):
            return f"talk:{tok}"
        return "talk"  # bare talk → resolve_target / DM at delivery
    if surface == "email":
        # Recover the origin of an email continuation from its conversation_token.
        tok = task.conversation_token
        if not tok or is_synthetic_email_thread_token(tok):
            return None  # genuine email-only thread — no recoverable origin
        if tok.startswith("web-"):
            return f"web:{tok}"
        if tok.startswith("repl-"):
            return None  # a since-exited REPL terminal can't be pushed
        # A non-synthetic, non-web/repl token on an email task is a real Talk
        # room set by our own inbound continuation routing.
        return f"talk:{tok}"
    return None  # repl: no durable push target


def upgrade_legacy_origin(conn, origin: str) -> str | None:
    """``room:<canonical_token>`` for a stored descriptor that names one *view*
    of a multi-surface room; None to keep the descriptor exactly as stored.

    Back-compat only. `origin_descriptor` now stamps `room:<token>` itself, so
    nothing new needs this — but `sent_emails` rows written before that keep the
    surface-qualified form (`web:<token>`, `talk:<token>`) for the life of the
    thread, and reading one literally delivers the reply to the leg the original
    happened to go out on, leaving the other view of the same room blank. That
    is the defect `6244348e` fixed; deleting the widening along with the
    function that used to do it would reintroduce it for every thread already in
    flight at deploy time.

    **Not transitional, despite the name.** Legacy rows do age out — the next
    send in a thread re-stamps `room:<token>` — but `origin_descriptor` can only
    name a room it can find from the task, and a send whose room is reachable
    from neither `conversation_token` nor `talk_delivery_token` still stamps the
    surface form. Do not delete this on the assumption that it has become dead
    code; check `_room_descriptor` actually covers every writer first.

    It is deliberately expressed as an *upgrade to the new form* rather than as
    the old bare ``"room"``, which relied on the task's own
    `conversation_token` and so could not name a room the task was not already
    sitting in.

    Three cases keep the descriptor: a bare surface with no channel (nothing to
    look up), a token naming no live room, and a room with only the descriptor's
    own binding — where the room form would cost a lookup per delivery and
    expand to exactly what the descriptor already says.
    """
    from .. import db

    surface, _sep, channel = origin.partition(":")
    if not channel:
        return None
    # A promoted room's per-surface ref is not its canonical token, so resolve
    # the binding before asking whether the room exists.
    token = db.resolve_room_token(conn, surface, channel) or channel
    room = db.get_room(conn, token)
    if room is None or getattr(room, "archived", 0):
        return None
    bound = {b.surface for b in db.list_room_bindings(conn, token)}
    if not bound - {surface}:
        return None
    return f"room:{token}"


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


def _room_view(
    config: "Config", registry: "TransportRegistry | None", surface: str,
) -> str | None:
    """``TransportCapabilities.room_view`` for a surface name, or None when the
    surface is not a room view *or* is not resolvable.

    Those two answers deliberately collapse, and the caller skips only on
    ``"canonical"`` rather than keeping only ``"external"``. A binding whose
    surface has no live transport (Talk bound but ``talk.enabled = false``) has
    no capabilities to read, and the safe reading of an unresolvable surface is
    "not a canonical view": the destination survives expansion and resolves to a
    normal push against the binding's own ``surface_ref``, exactly as it did
    when this was a name check against ``_STREAM_SURFACES``. It then fails at
    delivery time, where a disabled surface is already handled. Treating
    unresolvable as "don't mirror" would instead make the mirror vanish at plan
    time with nothing logged — a behaviour change, and a silent one.

    Falls back to a config-built registry so a caller without one in scope still
    gets the real answer for the unconditionally-registered surfaces (web);
    ``make_registry`` does no I/O.
    """
    if registry is None:
        from .registry import make_registry
        try:
            registry = make_registry(config)
        except Exception as e:  # pragma: no cover - never abort delivery
            logger.warning("registry construction failed for room_view: %s", e)
            return None
    transport = registry.get(surface)
    if transport is None:
        return None
    return getattr(transport.capabilities, "room_view", None)


def is_canonical_room_view(
    config: "Config", registry: "TransportRegistry | None", surface: str,
) -> bool:
    """True when a surface renders a room *from* the canonical ``messages``
    store, so writing the row is already that surface's delivery.

    The public form of the ``room_view`` question, for callers outside this
    module that need to tell "this delivery is the canonical row" from "this
    delivery is a push". The scheduler asks it to decide whether a web
    destination aimed at the task's own room means an assistant bubble or an
    unsolicited ``role='system'`` note.
    """
    return _room_view(config, registry, surface) == "canonical"


def _expand_room_destinations(
    config: "Config", task: "db.Task",
    registry: "TransportRegistry | None" = None,
    token: str | None = None,
) -> list[Destination]:
    """Expand a ``room`` meta-destination by the room's live bindings: the
    origin delivery plus a push mirror to every *non-origin* binding whose view
    of the room lives in a store we don't own.

    The one rule: write the canonical ``messages`` row once, then push to every
    bound surface whose transcript lives somewhere else. A ``room_view`` of
    ``"canonical"`` (web) is already delivered by that row, so pushing to it
    would render the turn twice. A ``room_view`` of ``"external"`` (Talk, whose
    store is in Nextcloud) needs a real API call.

    So the mirror is asymmetric by design — a web-origin task mirrors to its
    bound Talk room, a Talk-origin task pushes nothing to its bound web room —
    and the asymmetry falls out of where each surface's transcript is stored
    rather than being asserted per surface. It used to key on
    ``_STREAM_SURFACES``, which gave the same answer only because web happens to
    be both the only canonical-transcript room view and the only stream room
    surface.

    ``token`` names the room explicitly — the ``room:<token>`` destination form.
    It matters when the room is not the task's own channel: an inbound email
    reply carries a stored ``room:`` origin descriptor while its own
    ``conversation_token`` may still be the synthetic thread hash. Omitting it
    falls back to the task's channel, which is the bare ``room`` form, unchanged.
    """
    from .. import db
    from .registry import _surface_for_source_type

    dests = list(_infer_default_plan(task))  # origin delivery
    token = token or task.conversation_token
    if not token or not config.db_path:
        return dests
    origin_surface = _surface_for_source_type(task.source_type)
    try:
        with db.get_db(config.db_path) as conn:
            # Resolve through the binding before listing them. A promoted room's
            # per-surface ref is not its canonical token, and looking bindings up
            # by the raw value is the mistake this whole spec is cleaning up. A
            # token that is already canonical resolves to itself.
            canonical = db.resolve_room_token(conn, origin_surface, token) or token
            # A room that went away between the send and the reply mirrors
            # nowhere — the bot has left it, or it never was one. The origin
            # delivery still stands, which is what keeps a reply from being
            # dropped because its room was archived underneath it.
            room = db.get_room(conn, canonical)
            if room is None:
                return dests  # names no room; listing bindings would be empty
            if getattr(room, "archived", 0):
                # Logged, never silent. `archive_orphaned_talk_rooms` archives
                # on a single missing conversation-list entry, and only a *Talk*
                # inbound un-archives — so a transient blip suppresses the
                # mirror for every web send into the room until someone posts
                # from Talk. That is recoverable but invisible, and an operator
                # looking for "why did my replies stop reaching Talk" needs this
                # line to exist.
                logger.warning(
                    "Not mirroring task %s into archived room %s",
                    getattr(task, "id", "?"), canonical,
                )
                return dests
            bindings = db.list_room_bindings(conn, canonical)
    except Exception as e:  # pragma: no cover - best-effort, never abort delivery
        logger.warning("room binding lookup failed for task %s: %s",
                       getattr(task, "id", "?"), e)
        return dests
    for b in bindings:
        if b.surface == origin_surface:
            continue
        if _room_view(config, registry, b.surface) == "canonical":
            continue
        dests.append(Destination(b.surface, b.surface_ref, mirror=True))
    return dests


def talk_channel_for_task(config: "Config", task: "db.Task") -> str | None:
    """The Talk room to deliver this task's notifications to.

    Replaces the ``tasks.talk_delivery_token`` column, which existed because in
    the Talk-only era an email task needed somewhere to record "the real Talk
    room" that was not its thread-grouping ``conversation_token``. A room's Talk
    room is now a property of the room — its ``talk`` binding — so the column
    was a denormalized copy of a fact the registry already holds, and one that
    could go stale: a room promoted to Talk *after* the task was created gained
    a binding the stored token never learned about.

    The ladder, in order:

    0. **``talk_delivery_token``, when set.** Still first, and still absolute.
       The column is on its way out but it is not gone, and while anything
       writes it, it carries information nothing else has: the legacy
       thread-match branch in ``transport/email/inbound.py`` (reached when
       ``sent_emails.origin_target`` is NULL) copies a Talk room onto the task
       that the room registry may never have heard of. Demoting this rung to "a
       hint for finding a room" silently reroutes those tasks to the alerts
       ladder — ISSUE-057's fix, undone, with a green test suite. Deleting the
       rung is the *last* step of retiring the column, not the first.
    1. **The task's room's Talk binding.** The replacement for the column, and
       the only rung that knows about a binding added after the task was
       created. Reached whenever the column is NULL, which is every talk- and
       web-sourced task.
    2. **``conversation_token`` itself**, when the task has one and is not
       email-sourced — the Talk-source case, where the token *is* the room, and
       the case of a DM with no registered room.
    3. **The user's resolved notification channel** (alerts → briefing → DM),
       for an email task whose token is a synthetic thread hash naming no Talk
       room at all. Posting to that hash silently no-ops.

    No token and no room resolves to ``None`` rather than falling through to the
    alerts ladder: a task with nothing to deliver to is not the same as an email
    whose thread hash needs redirecting, and conflating them would reroute every
    channel-less task into the user's alerts room.

    A synthetic token that resolves to nothing is returned **as-is** rather than
    as ``None``, preserving the pre-existing silent no-op at delivery instead of
    trading it for a different failure mode.
    """
    from ..email_support import is_synthetic_email_thread_token

    if task.talk_delivery_token:
        return task.talk_delivery_token
    room_talk = _talk_binding_for_task(config, task)
    if room_talk:
        return room_talk
    token = task.conversation_token
    if not token or task.source_type != "email":
        return token
    if not is_synthetic_email_thread_token(token):
        return token
    from ..notifications import resolve_conversation_token
    return resolve_conversation_token(config, task.user_id) or token


def transcript_room(
    conn,
    config: "Config",
    *,
    user_id: str,
    source_type: str | None,
    conversation_token: str | None,
    output_target: str | None,
    talk_delivery_token: str | None = None,
) -> str | None:
    """The room whose transcript this exchange belongs in, or None.

    One resolution, consulted by every writer of a room row, replacing three
    readings of ``tasks.conversation_token`` as if it named a room (ISSUE-247).
    On an email task that token is a *thread* identifier — a hash grouping
    ``References`` — so each of those writers correctly found no room and fell
    back to a different workaround: a ``role='system'`` note instead of a turn,
    a Talk-only mirror carrying a different body, and no inbound row at all.

    The ladder is two rungs, and neither invents a destination:

    1. ``conversation_token`` when it already **is** a registered room. Every
       talk- and web-sourced task, and an email threaded back into the room it
       came from. This rung is the whole answer for every surface but email.
    2. **Email only** — a room named by ``output_target``: the ``room:<token>``
       form, or an explicit ``talk:``/``web:``/bare ``talk`` leg. This is the
       room the plan delivers into, so the answer lands where it is being shown.

    What is deliberately **not** a rung is "the room this user's notifications
    would go to". That is :func:`routed_notification_room`, and only the email
    poller calls it, on the routes where the message names no conversation at
    all. Consulting it here would put an ungated `thread_match` reply — an
    external correspondent's verbatim body, which `_conversation_history_from_
    messages` re-pairs into that room's LLM context — into the user's alerts
    room whenever their reply-routing policy is `thread`, which is a room the
    thread had no relationship with. The poller resolves it once and writes the
    answer into ``output_target``, so every later reader sees rung 2.

    Existence, never creation, at both rungs: an email task naming no registered
    room (a cron mailing an external address) stays task-only with no
    transcript, which is the pre-existing behaviour and deliberately unchanged.
    Never raises — a failure to resolve a transcript room must not abort
    delivery.

    ``talk_delivery_token`` is rung 0 of `talk_channel_for_task`, so a bare
    ``talk`` leg has to see it too or the two ladders answer differently and the
    exchange splits across two rooms again — which is this issue, reintroduced
    one level down.
    """
    from .. import db

    try:
        if conversation_token and db.get_room(conn, conversation_token) is not None:
            return conversation_token
        if source_type != "email":
            return None
        for dest in parse_output_target(output_target):
            room = _room_for_destination(
                conn, config, user_id, dest,
                talk_delivery_token=talk_delivery_token,
            )
            if room:
                return room
    except Exception as e:  # pragma: no cover - never abort delivery
        logger.warning("transcript room resolution failed for %s: %s", user_id, e)
    return None


def routed_notification_room(
    conn, config: "Config", user_id: str,
) -> str | None:
    """The registered room this user's ``notification`` route resolves to.

    Where mail that names no conversation of its own surfaces. This routing
    already decided that; it was just being consulted *inside*
    ``send_notification``, i.e. after the content had been reduced to a system
    note, which is why the room could never hold the exchange (ISSUE-247). The
    email poller calls it before the task exists and writes the answer into
    ``output_target``, so the room is a delivery destination rather than
    something derived after the fact.

    Existence, never creation: `None` when the route names no registered room,
    and then the mail stays task-only exactly as it did.
    """
    try:
        from ..notifications import resolve_destinations
        for dest in resolve_destinations(config, user_id, "notification"):
            room = _room_for_destination(conn, config, user_id, dest)
            if room:
                return room
    except Exception as e:  # pragma: no cover - never abort ingest
        logger.warning("notification room resolution failed for %s: %s", user_id, e)
    return None


def _room_for_destination(
    conn, config: "Config", user_id: str, dest: Destination,
    *, talk_delivery_token: str | None = None,
) -> str | None:
    """The registered room a single destination names, or None.

    ``room:<token>`` names one outright. A ``talk``/``web`` leg names one of its
    *views*, so the ref is resolved to the canonical token before the registry
    is asked — a promoted room's Talk ref is not its own token, and looking the
    room up by the raw value is the conflation this whole change is undoing. A
    bare leg carries no channel and falls back to that surface's default for the
    user. Any other surface (email, ntfy, istota_file, stream) is a delivery
    target rather than a room view, and names no room.

    A bare ``talk`` leg reads ``talk_delivery_token`` first because
    `talk_channel_for_task` does: that column is rung 0 there, absolutely, and
    is the one thing that knows about a Talk room the registry may never have
    heard of (the legacy thread-match branch in `transport/email/inbound.py`
    copies one onto the task). Resolving the notification ladder here instead
    would name a different room from the one the Talk post lands in.
    """
    from .. import db

    surface, channel = dest.surface, dest.channel
    if surface == "room":
        candidate = channel
    elif surface == "talk":
        from ..notifications import resolve_conversation_token
        candidate = (
            channel
            or talk_delivery_token
            or resolve_conversation_token(config, user_id)
        )
    elif surface == "web":
        # Deliberately *not* `default_web_room_token`, which provisions a
        # `general` room when the user has none and opens its own connection to
        # do it. Resolving a transcript room must neither create one nor take a
        # second write lock on a database this caller already holds.
        if channel:
            candidate = channel
        else:
            rooms = db.list_web_chat_rooms(conn, user_id, include_archived=False)
            candidate = rooms[0].token if rooms else None
    else:
        return None
    if not candidate:
        return None
    if surface in ("talk", "web"):
        candidate = db.resolve_room_token(conn, surface, candidate) or candidate
    return candidate if db.get_room(conn, candidate) is not None else None


def transcript_room_for_task(conn, config: "Config", task: "db.Task") -> str | None:
    """The transcript room for a task that already exists.

    Asks the store first: a room already holding this task's question is the
    room its answer belongs in, whatever the routing would say now. That is what
    makes the two halves of an exchange agree by construction rather than by two
    derivations of the same rule staying in step. Falls through to
    :func:`transcript_room` when there is no question stored — a turn whose
    inbound row is still withheld behind the confirmation gate, or a task
    created without one.
    """
    from .. import db

    try:
        stored = db.room_for_task_turn(conn, task.id, "user")
    except Exception:  # pragma: no cover - never abort delivery
        stored = None
    if stored:
        return stored
    return transcript_room(
        conn, config,
        user_id=task.user_id,
        source_type=task.source_type,
        conversation_token=task.conversation_token,
        output_target=task.output_target,
        talk_delivery_token=task.talk_delivery_token,
    )


def _talk_binding_for_task(config: "Config", task: "db.Task") -> str | None:
    """The ``surface_ref`` of the Talk binding on this task's room, or None.

    Never raises and never blocks delivery on a database problem: an
    unresolvable room falls through to the rest of the ladder, which is where
    every pre-rooms deployment already lives.

    Room resolution is **surface-scoped** here (``cross_surface=False``), unlike
    descriptor stamping. A ref is unique only within its surface, so the
    unscoped fallback can match a room that merely shares the string — harmless
    when the answer is a stored descriptor, a misroute when it is where the
    answer gets posted.

    Only ``conversation_token`` is tried. ``talk_delivery_token`` is not a second
    candidate: rung 0 returns it outright before this is ever called, so a
    lookup keyed on it could only run when it is empty. When rung 0 finally
    goes, this is where it would come back — as a candidate rather than as an
    answer.

    ``rooms.archived`` is deliberately **not** checked, unlike the two sibling
    lookups in this module. Skipping an archived room here changes which token
    delivery attempts, not whether it succeeds: the fallback is
    ``conversation_token``, which for the only shape that can diverge (a
    *promoted* room, canonical token ≠ Talk ref) is not a Talk room either. Both
    answers fail at the API, so the guard would buy nothing and lose the last
    attempt at a room the bot may still be in.
    """
    if not config.db_path:
        return None
    from .. import db
    from .registry import _surface_for_source_type

    token = task.conversation_token
    if not token:
        return None
    try:
        with db.get_db(config.db_path) as conn:
            surface = _surface_for_source_type(task.source_type)
            canonical = _canonical_room_token(
                conn, surface, token, cross_surface=False,
            )
            if canonical is not None:
                binding = db.get_room_binding(conn, canonical, "talk")
                if binding is not None:
                    return binding.surface_ref
    except Exception as e:  # pragma: no cover - best-effort, never abort delivery
        # A missing room registry is a real fault, not a quiet fallback:
        # `init_db` creates these tables, so any database the daemon has opened
        # has them, and a deployment without them is a failed migration or a
        # partial restore. It also degrades delivery rather than merely
        # narrowing it, so it must be visible. Latched to once per process,
        # because this runs on every message and the alternative is a per-
        # delivery repeat of the same line.
        global _WARNED_NO_ROOM_TABLES
        if "no such table" in str(e).lower():
            if not _WARNED_NO_ROOM_TABLES:
                _WARNED_NO_ROOM_TABLES = True
                logger.warning(
                    "Room registry tables are missing (%s) — Talk delivery is "
                    "falling back to the pre-rooms ladder. This is logged once.",
                    e,
                )
        else:
            logger.warning("talk binding lookup failed for task %s: %s",
                           getattr(task, "id", "?"), e)
    return None


def _resolve_talk_channel(config: "Config", task: "db.Task") -> str | None:
    return talk_channel_for_task(config, task)


def _resolve_one(
    config: "Config", task: "db.Task",
    registry: "TransportRegistry | None", dest: Destination,
) -> Destination | None:
    surface = dest.surface

    if surface in _STREAM_SURFACES:
        from .registry import _surface_for_source_type
        is_origin = surface == _surface_for_source_type(task.source_type)
        if is_origin or surface == "stream":
            # Own-origin stream (a web/repl task's own result is the task_events
            # log the client tails), or the un-pushable bare REPL surface: the
            # result event covers it; deliver() is a no-op.
            return Destination(surface, dest.channel or "stream", "stream")
        # A *foreign* task routing INTO a stream surface (e.g. an email reply →
        # web room): there is no live SSE for this task in that room, so push via
        # the transport's deliver() (web → a `role='system'` messages row).
        transport = registry.get(surface) if registry is not None else None
        channel = dest.channel or (transport.resolve_target(task) if transport else None)
        if not channel:
            # Unresolvable channel → degrade to a stream no-op rather than drop a
            # reply outright (the interactive empty-plan fallback still covers it).
            return Destination(surface, "stream", "stream")
        return Destination(surface, channel, "push")

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
    channel is filled (Talk via ``talk_channel_for_task``'s
    column → binding → token → resolved-channel ladder) or the destination is
    dropped (logged at
    WARNING) when its surface is unregistered or its user-level channel resolves
    to ``None``. Never raises into the caller.
    """
    spec = task.output_target
    plan = parse_output_target(spec)
    if not plan and (spec is None or not spec.strip()):
        plan = _infer_default_plan(task)

    # Expand the `room` meta-destination by live bindings (not a static alias):
    # origin delivery + a push mirror to each non-origin push-bound surface.
    if any(d.surface == "room" for d in plan):
        expanded: list[Destination] = []
        for d in plan:
            if d.surface == "room":
                expanded.extend(_expand_room_destinations(
                    config, task, registry, d.channel))
            else:
                expanded.append(d)
        plan = expanded

    resolved: list[Destination] = []
    seen: set[tuple[str, str | None]] = set()
    for dest in plan:
        r = _resolve_one(config, task, registry, dest)
        if r is None:
            continue
        # Carry the mirror flag through resolution (it governs confirmation
        # suppression in the scheduler).
        if dest.mirror and not r.mirror:
            r = replace(r, mirror=True)
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
