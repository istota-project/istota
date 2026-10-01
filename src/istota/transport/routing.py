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

# The static room-model table. A stdlib-only leaf that imports nothing, so a
# module-level import here costs nothing and introduces no cycle — unlike
# `db`, which this module deliberately imports per function.
from ..surfaces import (
    UserTurnMirror as UserTurnMirrorMode,
    is_room_member,
    origin_surface_for_source_type,
    room_view,
    user_turn_mirror,
)

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
_INTERACTIVE_SOURCE_TYPES = frozenset(
    {"talk", "email", "repl", "web", "sms", "whatsapp"}
)

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


@dataclass(frozen=True)
class UserTurnMirror:
    """A bound external view that needs a copy of a non-native user turn.

    ``surface_ref`` is the destination's address, never the canonical room
    token. ``mode`` describes authorship, not content policy: callers must
    still choose content by origin. In particular, email reposts carry sender
    and subject only, never the wrapped untrusted body. ``as_user`` degrades
    to an attributed repost when no author credential is available.
    """

    surface: str
    surface_ref: str
    mode: UserTurnMirrorMode


def plan_user_turn_mirrors(
    conn, config: "Config", room_token: str, origin_surface: str,
) -> list[UserTurnMirror]:
    """Plan user-turn copies from static facts and one binding lookup.

    Config does not gate the room model: a disabled transport still has its
    declared role, and its caller handles post failures. Missing, archived or
    unbound rooms have no targets. A DB failure must not fail the original send.
    """
    from .. import db

    try:
        room_token = db._canonical_room_token(conn, room_token, cross_surface=False)
        room = db.get_room(conn, room_token)
        if room is None or room.archived:
            return []
        mirrors = []
        for binding in db.list_room_bindings(conn, room_token):
            if binding.surface == origin_surface:
                continue
            if room_view(binding.surface) != "external":
                continue
            mode = user_turn_mirror(binding.surface)
            if mode is not None:
                mirrors.append(UserTurnMirror(binding.surface, binding.surface_ref, mode))
        return mirrors
    except Exception as e:
        logger.warning("user-turn mirror planning failed for room %s: %s", room_token, e)
        return []


def parse_output_target(
    spec: str | None, *, task_id: int | None = None,
) -> list[Destination]:
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

    A ``group`` leaf, bare or ``group:<id>``, is dropped with a WARNING (groups
    spec D8): a group is never a delivery target, and refusing it here rather
    than in the registry means every validator that reads an empty parse as
    "names nowhere" refuses it too. ``task_id`` only names the task in that
    warning.
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
        if surface == "group":
            logger.warning(
                "output target leaf %r dropped%s: a group is never a "
                "delivery target",
                token, f" for task {task_id}" if task_id is not None else "",
            )
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

    Three live tries, narrowest first: the token already *is* a canonical token; it
    is this surface's ref for one; it is *some other* surface's ref for one.

    A final permanent-mapping lookup forwards a migrated identity when the
    live tries miss, including when cross-surface lookup is disabled.

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

    token = db._canonical_room_token(
        conn, token, surface=surface, cross_surface=cross_surface,
    )
    return token if db.get_room(conn, token) is not None else None


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
    from ..db import is_canonical_room_token

    surface = _surface_for_source_type(task.source_type)
    room = _room_descriptor(conn, surface, task)
    if room is not None:
        return room
    if is_canonical_room_token(task.conversation_token) and not task.talk_delivery_token:
        return f"room:{task.conversation_token}"
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
    if surface == "sms":
        return "sms"
    if surface == "whatsapp":
        # Bare, always. A descriptor is stored and read back later, and the
        # only destination a WhatsApp route may name is the user's own current
        # binding — a channel here would be a Meta identifier written into a
        # durable row and re-sent to hours later.
        return "whatsapp"
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
    token = db._canonical_room_token(conn, channel, surface=surface, cross_surface=False)
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
    if st == "sms":
        return [Destination("sms")]
    if st == "whatsapp":
        return [Destination("whatsapp")]
    return []


def _room_view(
    config: "Config", registry: "TransportRegistry | None", surface: str,
) -> str | None:
    """``TransportCapabilities.room_view`` for a surface name, or None when the
    surface is not a room view *or* is not resolvable.

    **The second of two readers of `room_view`, and the config-gated one.**
    `surfaces.room_view()` answers what role a surface plays in the room model
    — a fact about the code, the same on every deployment — and is what the
    converted room-model sites read. This one answers whether a live transport
    for the surface exists on *this* deployment right now, which is what a
    delivery planner wants and what nothing outside this module should read for
    the model question: `web_app._user_row_display` would start rendering every
    historic Talk turn as an external message on a deployment with
    ``talk.enabled = false``.

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


def canonical_room_token(conn, token: str, surface: str = "") -> str | None:
    """The canonical room token a raw ``token`` names, or None if it names no
    room. The public form of ``_canonical_room_token``'s cross-surface reading.

    For callers holding a token and no reliable surface for it — the ``rooms``
    skill, which has only ``ISTOTA_CONVERSATION_TOKEN``, and the prompt header —
    a surface-scoped lookup can only ever miss on a promoted room reached from
    Talk, where the task's token is the Talk ref and the room's id is the
    ``web-…`` one. Both callers are *describing* a room rather than choosing a
    delivery channel, so the collision risk the delivery path refuses (a ref
    that happens to be another surface's ref for a different room) costs a wrong
    label here rather than an answer posted into the wrong conversation.

    Never raises: both callers sit on a path where an exception means no task at
    all, so an unreadable registry is "names no room".
    """
    if not token:
        return None
    try:
        return _canonical_room_token(conn, surface, token, cross_surface=True)
    except Exception:  # pragma: no cover - best-effort labelling
        return None


def room_target_descriptor(
    token: str, origin: str, talk_ref: str | None = None,
) -> str:
    """The ``output_target`` a *scheduled* job should carry to deliver into this
    room — the string `istota-skill rooms list` hands the model and the
    `talk create` guard's refusal points at (ISSUE-509).

    Deliberately **not** ``room:<token>``, and the reason changed with
    ISSUE-511. That spelling used to deliver nowhere from a cron job — the
    expansion skipped both of the room's bindings as legs an origin that does
    not exist was assumed to have covered — and it now delivers correctly, so
    what remains is a preference rather than a defect. Two reasons to keep
    naming the surfaces here. The descriptor is *read* as well as written: it
    goes into `istota-skill rooms list`, the prompt header's `Room:` line and
    the `talk create` refusal, where naming the surfaces tells a reader which
    ones a room is on. And it degrades legibly — a job whose room is later
    unbound from a surface still shows which legs it was written for, where
    ``room:`` shows nothing and silently narrows.

    What ``room:<token>`` buys that this does not is re-expansion by *live*
    bindings, so a room promoted to Talk after the job was written picks the
    Talk leg up on its own. Prefer it where that matters; prefer this where the
    descriptor is something a person reads.

    So the descriptor names the surfaces explicitly:

    - a Talk-origin room is ``talk:<token>`` — its token *is* the conversation.
    - a web room is ``web:<token>``, which ``_resolve_one`` sends down the
      foreign-push branch (a scheduled task's origin surface is ``talk``, so the
      web destination is never short-circuited to a stream no-op).
    - a **promoted** web room is ``web:<token>,talk:<talk_ref>``, because the
      web leg writes the canonical row and pushes nothing to Talk. Naming only
      the web half is the ISSUE-400 shape: correct on the surface the author was
      looking at, invisible to everyone reading the room from the other one.

    Pure — the caller supplies the binding it already read.
    """
    if origin == "talk":
        return f"talk:{token}"
    descriptor = f"web:{token}"
    if talk_ref:
        descriptor += f",talk:{talk_ref}"
    return descriptor


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

    **Both skips are compensations for an origin leg, so a task that originated
    on no surface takes neither** (ISSUE-511). The origin surface is
    ``surfaces.origin_surface_for_source_type`` and never
    ``registry._surface_for_source_type``: that one answers "where do I deliver
    this result" and maps everything it does not recognise to ``"talk"``, so a
    scheduled job carrying ``room:<token>`` had the room's Talk binding skipped
    as an origin leg that does not exist, and its web binding skipped as a
    canonical row an origin leg was assumed to have written. Rooms bind only
    ``talk`` and ``web``, so the plan came out empty — and ``scheduled`` is not
    in ``_INTERACTIVE_SOURCE_TYPES``, so nothing caught it and the answer went
    nowhere while the job reported success. ``None`` reads as "no origin leg
    delivered anything", which is the correct reading for `briefing`, `cli`,
    `doctor`, `heartbeat`, `scheduled` and `subtask`, and every binding is
    emitted.

    Emitting the *canonical* binding is the half that is easy to leave out and
    is what the transcript depends on. `scheduler._room_turn_belongs_here`'s
    first rung reads `own_room_canonical_dests`, which is empty unless the web
    binding is in the plan; its second rung is the room already holding the
    question, and a cron task deposits no ``role='user'`` row. So on a web-only
    room that leg is the only thing that can produce an assistant turn — and it
    produces a turn rather than an unsolicited ``role='system'`` note precisely
    because the scheduler recognises it as the task's own room and suppresses
    the push in favour of the row (ISSUE-164). On a Talk-bound room it is
    harmless: the same partition puts it in ``own_room_canonical_dests`` beside
    the Talk leg's own ``_talk_lands_here``, so there is still one row and no
    note.

    **The token is resolved through a binding only when there is a surface to
    scope the lookup by.** A ref is unique only within its surface, so the
    unscoped lookup `_canonical_room_token(..., cross_surface=True)` performs is
    refused on this path for the reason that function's own docstring gives: a
    wrong *descriptor* is re-resolved by live bindings at delivery, a wrong
    *channel* posts the answer into somebody else's conversation. Nothing is
    lost by it. `origin_descriptor` is the only shipped producer of a ``room:``
    descriptor and it stamps a canonical token — `_room_descriptor` confirms the
    room with `db.get_room` before returning — and the token a person copies out
    of `istota-skill rooms list` is canonical too. A canonical token needs no
    resolution.

    **The seed still emits a source-type default for `briefing`, which reads as
    a contradiction of the paragraph above and is a knowingly-kept one.**
    `_infer_default_plan` answers `[]` for five of the six originless source
    types; `briefing` is the exception, and there the bare `talk` leg is not an
    origin delivery but the default the task would have had anyway. Dropping it
    is not free in either available spelling: the missing-room and archived
    arms below hand `dests` back precisely so a briefing whose room went away
    still falls through to the user's notification channel, and an email
    continuation's stored `room:<token>` descriptor leans on the same prepend
    for its own email leg. So a briefing targeting a room other than its own
    channel reaches both, which is one leg more than asked for and one more
    than it reached before — closer to correct rather than further, since the
    named room is the one that was missing. Pinned by test; narrowing it is its
    own change (ISSUE-511 review).

    ``token`` names the room explicitly — the ``room:<token>`` destination form.
    **It must be the room's canonical token**, which is what
    `istota-skill rooms list` reports as `token` and what the prompt header's
    `Room:` line carries — not a per-surface ref. With no origin surface there
    is nothing to scope a binding lookup by, so a promoted room's Talk ref
    written here resolves to no room; the missing-room arm logs that rather
    than dropping it in silence.
    It matters when the room is not the task's own channel: an inbound email
    reply carries a stored ``room:`` origin descriptor while its own
    ``conversation_token`` may still be the synthetic thread hash. Omitting it
    falls back to the task's channel, which is the bare ``room`` form, unchanged.
    """
    from .. import db

    dests = list(_infer_default_plan(task))  # origin delivery
    token = token or task.conversation_token
    if not token or not config.db_path:
        return dests
    origin_surface = origin_surface_for_source_type(task.source_type)
    try:
        with db.get_db(config.db_path) as conn:
            # Resolve through the binding before listing them. A promoted room's
            # per-surface ref is not its canonical token, and looking bindings up
            # by the raw value is the mistake this whole spec is cleaning up. A
            # token that is already canonical resolves to itself.
            canonical = db._canonical_room_token(
                conn, token, surface=origin_surface or "", cross_surface=False,
            )
            # A room that went away between the send and the reply mirrors
            # nowhere — the bot has left it, or it never was one. The origin
            # delivery still stands, which is what keeps a reply from being
            # dropped because its room was archived underneath it.
            room = db.get_room(conn, canonical)
            if room is None:
                # Logged for the reason the archived arm below is, and more
                # urgently since ISSUE-511: for a task that originates nowhere
                # `dests` is empty, nothing downstream fills it, and `room` is
                # no longer an unknown surface at cron-load time — so a
                # mistyped or non-canonical token now reproduces the exact
                # symptom this issue was filed about with no signal at all.
                logger.warning(
                    "Task %s targets room %r, which names no registered room; "
                    "nothing will be delivered to it. The token must be the "
                    "room's canonical one, which `istota-skill rooms list` "
                    "reports as `token`.",
                    getattr(task, "id", "?"), canonical,
                )
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
        # Static absence means this surface never shows the room transcript.
        # A disabled Talk transport also has no *live* view, but retains its
        # existing resolution/fallback path. Phone bindings must not imply sends.
        if room_view(b.surface) is None:
            continue
        # Both skips compensate for what an origin leg already delivered, so
        # neither applies to a task that has no origin leg. See above.
        if origin_surface is not None:
            if b.surface == origin_surface:
                continue
            if _room_view(config, registry, b.surface) == "canonical":
                continue
        # `mirror` is a *relation to an origin leg*, so a task with no origin
        # has no mirror legs — it is not a synonym for "produced by the
        # fan-out". All three readers treat the flag as "this duplicates a
        # delivery that happened somewhere else", and the one that bites is
        # `scheduler.py`'s undelivered-result arm: it suppresses the inbox row
        # and the alert for a Talk post that came back `None`, which is right
        # for a web-origin task whose answer already streamed, and drops the
        # only copy of the answer for an originless `room:` expansion where the
        # Talk leg is the whole plan.
        dests.append(Destination(
            b.surface, b.surface_ref, mirror=origin_surface is not None,
        ))
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
    2. **A legacy surface token**, never a minted room identity. An
       unregistered Talk DM can still deliver by its native address.
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
    from ..db import is_canonical_room_token
    from ..email_support import is_synthetic_email_thread_token

    if task.talk_delivery_token:
        return task.talk_delivery_token
    room_talk = _talk_binding_for_task(config, task)
    if room_talk:
        return room_talk
    token = task.conversation_token
    if is_canonical_room_token(token):
        return None
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
    all. Consulting it here would put an ungated `thread_match` reply — the
    correspondent's verbatim body, which `_conversation_history_from_messages`
    re-pairs into that room's LLM context — into the user's alerts room whenever
    their reply-routing policy is `thread`, which is a room the thread had no
    relationship with. (Since ISSUE-234 "ungated" on that route means the reply
    came from an address the bot wrote to, which narrows who can do this without
    changing that they can.) The poller resolves it once and writes the
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

    try:
        if conversation_token:
            canonical = _canonical_room_token(conn, "", conversation_token, cross_surface=False)
            if canonical is not None:
                return canonical
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

    A room more than one human reads is skipped, as `refuse_shared_rooms`
    refuses it for the notification itself: the mail is the user's, and naming
    the room here records it there before any delivery rule runs.
    """
    try:
        from .. import db
        from ..notifications import resolve_destinations
        for dest in resolve_destinations(config, user_id, "notification"):
            room = _room_for_destination(conn, config, user_id, dest)
            if room and not db.room_is_shared(conn, room):
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
        # `conn` is passed, not left to be reopened: since ISSUE-477 that
        # resolver reads the user's configured default room, and the web branch
        # below states the rule — resolving a transcript room must not take a
        # second connection on a database this caller already holds.
        candidate = (
            channel
            or talk_delivery_token
            or resolve_conversation_token(config, user_id, conn)
        )
    elif surface == "web":
        # `db.default_web_room` rather than `default_web_room_token`, which
        # provisions a `general` room when the user has none and opens its own
        # connection to do it. Resolving a transcript room must neither create
        # one nor take a second write lock on a database this caller already
        # holds. Since ISSUE-473 the two agree on which rooms *qualify* — this
        # used to keep an unfiltered copy of the rule and could name a room the
        # delivery resolver would refuse — but only on that. Having no fallback,
        # this answers None where delivery would invent or resurface a room, so
        # a user with no qualifying room gets no transcript room here.
        if channel:
            candidate = channel
        else:
            room = db.default_web_room(conn, user_id)
            candidate = room.token if room else None
    else:
        return None
    if not candidate:
        return None
    # A room-owning surface is one that appears in `room_bindings`, so its
    # candidate is a `surface_ref` to be resolved to the canonical token first.
    # Email is a `guest` and binds nothing, which is why it never reaches here
    # — the dispatch above returns None for it — and why the predicate is the
    # ownership one rather than the room-view one. It sits *after* that
    # dispatch deliberately: `surface == "room"` is a name in the destination
    # grammar rather than a surface, and a member check placed ahead of it
    # would drop every `room:<token>` descriptor (ISSUE-247).
    return _canonical_room_token(
        conn, surface if is_room_member(surface) else "", candidate, cross_surface=False,
    )


def private_phone_room(
    conn, surface: str, user_id: str, channel: str | None = None,
) -> str | None:
    """The user's own SMS or WhatsApp room, if one was minted, else None.

    The room a phone push lands in as a transcript row. Existence, never
    creation: a push is the system talking, so a miss writes nothing and the
    send goes ahead exactly as before. A room another human reads is refused
    like any other personal delivery. ``channel`` is the planned destination; a
    WhatsApp channel that is not the user's private chat (a group's room) has
    no private transcript to land in. SMS has no such alternative, since its
    send always resolves the user's own binding.
    """
    from .. import db
    from .sms import sms_conversation_token
    from .whatsapp import whatsapp_conversation_token

    if surface == "sms":
        surface_ref = sms_conversation_token(user_id)
    elif surface == "whatsapp":
        surface_ref = whatsapp_conversation_token(user_id)
        if channel is not None and channel != surface_ref:
            return None
    else:
        return None
    token = db.resolve_room_token(conn, surface, surface_ref)
    if not token or db.get_room(conn, token) is None:
        return None
    if user_id not in db.list_room_members(conn, token) or db.room_is_shared(conn, token):
        return None
    return token


def is_private_phone_room(conn, surface: str, user_id: str, room_token) -> bool:
    """Whether ``room_token`` is this user's own SMS or WhatsApp room.

    The one test for "the phone webhook has already stored this turn here":
    both webhooks record every accepted private turn, a typed ``!command`` and
    a confirmation answer included, before acting on it, while a WhatsApp
    group's command records nothing. A writer that would add the same turn
    again asks this first.
    """
    return bool(room_token) and room_token == private_phone_room(conn, surface, user_id)


def private_phone_ref(surface: str, user_id: str) -> str | None:
    """The ``surface_ref`` of ``user_id``'s own SMS or WhatsApp thread, else None."""
    from .sms import sms_conversation_token
    from .whatsapp import whatsapp_conversation_token

    if not user_id:
        return None
    if surface == "sms":
        return sms_conversation_token(user_id)
    if surface == "whatsapp":
        return whatsapp_conversation_token(user_id)
    return None


def phone_transcript_surface(conn, room_token) -> str | None:
    """``'sms'`` or ``'whatsapp'`` when the room is a private phone thread's
    transcript, else None.

    The read-only test (decided 2026-10-01): web reads such a room and may not
    write into it, so the send route refuses and the client renders no
    composer. Asked of the room, not of a reader, so it answers the same for
    every member: the binding's ref has to be the room creator's own private
    thread token, the rule `whatsapp.outbound.is_group_task` uses. A WhatsApp
    group room is bound by its group JID and keeps its composer. Unlike
    `private_phone_room` this does not drop a room another member was added to;
    adding a reader does not make a phone thread writable from web.
    """
    from .. import db

    if not room_token:
        return None
    # A task created before the mint still carries the hash token, which the
    # room keeps as a permanent alias.
    room = db.get_room(
        conn, db._canonical_room_token(conn, room_token, cross_surface=False),
    )
    if room is None:
        return None
    for binding in db.list_room_bindings(conn, room.token):
        ref = private_phone_ref(binding.surface, room.user_id)
        if ref is not None and binding.surface_ref == ref:
            return binding.surface
    return None


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

    if surface == "sms":
        # The channel is advisory, as email's is: `deliver_sms` resolves the
        # binding itself immediately before the send. A destination with no
        # channel is kept rather than dropped, because dropping it empties the
        # plan — and an empty plan discards a finished answer with nothing but
        # a daemon WARNING, and makes an SMS-origin confirmation *complete*
        # instead of parking. Kept, `deliver_sms` records `unconfigured` and
        # raises the task alert the spec asks for.
        transport = registry.get("sms") if registry is not None else None
        if transport is None:
            logger.warning(
                "Dropping SMS destination for task %s: transport not registered",
                getattr(task, "id", "?"),
            )
            return None
        channel = transport.resolve_target(task)
        if dest.channel and dest.channel != channel:
            # A descriptor carrying a number is never sent to. The binding
            # wins, always — the route grammar must not become a way to send
            # to an arbitrary number — but it is said out loud rather than
            # rewritten in silence.
            logger.warning(
                "Ignoring the phone number in an sms: destination for task %s; "
                "SMS always sends to the user's own binding",
                getattr(task, "id", "?"),
            )
        if not channel:
            logger.warning(
                "SMS destination for task %s has no current user binding; "
                "delivery will record `unconfigured`",
                getattr(task, "id", "?"),
            )
        return Destination("sms", channel, "push")

    if surface == "whatsapp":
        # The same shape as SMS's branch above and for the same two reasons.
        # The channel is advisory — `deliver_whatsapp` resolves the binding
        # itself immediately before the call — and a destination with no
        # channel is kept rather than dropped, because dropping it empties the
        # plan, which discards a finished answer with nothing but a WARNING and
        # makes a WhatsApp-origin confirmation *complete* instead of parking.
        transport = registry.get("whatsapp") if registry is not None else None
        if transport is None:
            logger.warning(
                "Dropping WhatsApp destination for task %s: transport not registered",
                getattr(task, "id", "?"),
            )
            return None
        channel = transport.resolve_target(task)
        if dest.channel and dest.channel != channel:
            from .whatsapp.identity import normalize_group_jid

            if normalize_group_jid(dest.channel):
                # A room's WhatsApp group binding, reached by fan-out from a
                # task that is not that group's own turn — a web turn in the
                # group's room, a scheduled job naming it. A group is answered
                # only from its own turns (multiplayer D6); falling through to
                # the user's own chat would send it somewhere nobody asked.
                logger.info(
                    "Not delivering task %s into a WhatsApp group it was not "
                    "asked in", getattr(task, "id", "?"),
                )
                return None
            # `whatsapp:<phone-or-id>` is refused rather than obeyed. The route
            # grammar must not become a way to send to an arbitrary contact, so
            # the binding wins always — said out loud rather than rewritten in
            # silence, and without echoing the identifier the caller supplied.
            logger.warning(
                "Ignoring the explicit destination in a whatsapp: target for "
                "task %s; WhatsApp always sends to the user's own binding",
                getattr(task, "id", "?"),
            )
        if not channel:
            logger.warning(
                "WhatsApp destination for task %s has no current user binding; "
                "delivery will record `unconfigured`",
                getattr(task, "id", "?"),
            )
        return Destination("whatsapp", channel, "push")

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
    if st == "sms":
        try:
            from .registry import make_registry
            transport = make_registry(config).get("sms")
        except Exception:
            transport = None
        if transport is None:
            return None
        # Channel-less is a live destination here too; see `_resolve_one`.
        return Destination("sms", transport.resolve_target(task), "push")
    if st == "whatsapp":
        try:
            from .registry import make_registry
            transport = make_registry(config).get("whatsapp")
        except Exception:
            transport = None
        if transport is None:
            return None
        # Channel-less is a live destination here too; see `_resolve_one`.
        return Destination("whatsapp", transport.resolve_target(task), "push")
    channel = _resolve_talk_channel(config, task)
    if not channel:
        return None
    return Destination("talk", channel, "push")


def refuse_shared_rooms(
    config: "Config", user_id: str, dests: list[Destination], *,
    purpose: str, conversation_token: str | None = None,
) -> list[Destination]:
    """``dests`` without any leg that posts into a room more than one human
    reads (multiplayer Stage 15, SG 10).

    A shared room is not a destination for a user's personal content, however
    it came to be named: an ``output_target``, a routing descriptor, or an
    ``alerts_channel`` / ``log_channel`` that gained a second reader after it
    was set. Each refusal is one WARNING naming the room and the purpose, and
    the rest of the list survives. Nothing is redirected: the caller's own
    fallback, if it has one, decides what happens to an emptied list.

    ``conversation_token`` is the room the caller's task ran in. A leg into
    that room is a conversational reply and is kept, because the disclosure
    gate (`room_scopes.task_withheld_scopes`) keys on exactly that room, so the
    answer was produced at the reach the room allows. Any other shared room
    would receive output produced at the reach of somewhere else.

    Fails toward refusal: a registry that cannot be read refuses every room
    leg, since a room that cannot be checked cannot be shown private. A
    database with no registry at all is not that case: it holds no room.
    """
    from pathlib import Path

    from .. import db

    legs = [
        d for d in dests
        if d.kind != "stream" and d.channel and d.channel != "stream"
        and is_room_member(d.surface)
    ]
    if not legs or not config.db_path or not Path(config.db_path).exists():
        return list(dests)
    refused: dict[tuple[str, str | None], str] = {}
    try:
        with db.get_db(config.db_path) as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rooms'"
            ).fetchone() is None:
                # A database with no room registry holds no room to refuse.
                return list(dests)
            own = None
            if conversation_token:
                own = canonical_room_token(conn, conversation_token)
            for d in legs:
                room = _canonical_room_token(
                    conn, d.surface, d.channel, cross_surface=False,
                )
                if room and room != own and db.room_is_shared(conn, room):
                    refused[(d.surface, d.channel)] = room
    except Exception as e:
        logger.warning(
            "could not read the room registry for %s delivery (user %s); "
            "refusing every room leg: %s", purpose, user_id, e,
        )
        refused = {(d.surface, d.channel): d.channel for d in legs}
    kept = []
    for d in dests:
        room = refused.get((d.surface, d.channel))
        if room is None:
            kept.append(d)
            continue
        logger.warning(
            "Refusing %s delivery for user %s into shared room %s (%s:%s): "
            "more than one human reads it",
            purpose, user_id, room, d.surface, d.channel,
        )
    return kept


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
    plan = parse_output_target(spec, task_id=task.id)
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

    # A side room's output never reaches its parent (multiplayer D4); posting
    # there is the held `room post` verb. Before the shared-room refusal, which
    # would otherwise drop the parent first and leave the pin nothing to
    # substitute the side room for.
    from ..side_rooms import pin_plan
    resolved = pin_plan(config, task, resolved)

    # Only the room the task ran in may receive it if that room is shared. A
    # briefing has no such room: its blocks are assembled daemon-side from the
    # user's own sources, which no room gate reaches.
    resolved = refuse_shared_rooms(
        config, task.user_id, resolved,
        purpose=task.source_type or "task",
        conversation_token=(
            None if task.source_type == "briefing" else task.conversation_token
        ),
    )

    # Last, so an interactive reply is never eaten. It names the task's own
    # origin, which neither rule above refuses: not a side room's parent, and
    # the room the task ran in.
    if not resolved and task.source_type in _INTERACTIVE_SOURCE_TYPES:
        fb = _reply_origin_destination(config, task)
        if fb is not None:
            resolved.append(fb)
    return resolved
