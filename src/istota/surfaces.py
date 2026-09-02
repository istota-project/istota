"""What role each surface plays in the room model, in one table.

A room is one conversation bound to several surfaces. Which surfaces those can
be, and what each may do with a room, was decided by ten hardcoded declarations
of surface names across five files in four spellings. They agreed only because
there are exactly two room surfaces today and one surface that can post into a
room without being one, and three genuinely different questions shared the same
literals:

1. **Does this surface create and own rooms?** ``room_role``. ``member`` — an
   inbound message registers the room, binds the surface and adds membership
   (talk, web). ``guest`` — an inbound message can join an existing room's
   transcript but never mints one (email); ISSUE-136's "existence, never
   creation" rule is this value. ``None`` — never a room turn at all.
2. **Does this surface have a view of the room, so a turn written into the room
   is already in front of its users?** ``room_view``, mirroring
   ``TransportCapabilities.room_view``: ``canonical`` for a view rendered from
   the ``messages`` store we own (web), ``external`` for one whose transcript
   lives in a store we do not (Talk's is in Nextcloud), ``None`` for a surface
   that is not a room view. The test that makes email's ``None`` principled
   rather than a special case: is there a durable, addressable place a person
   opens to read the whole conversation, that we can write into? A Talk room,
   yes. An email thread, no — it is reconstructed from messages scattered
   across mailboxes we cannot write into.
3. **How does an external room view show a turn it did not receive natively?**
   ``user_turn_mirror``. A refinement of ``room_view == "external"`` rather than
   an independent axis: a surface that is not an external room view is never a
   fan-out target, so the field means nothing there.

Bidirectional sync is not a field, because it is not a property a surface has:
a surface is bidirectional for a room when (1) is not ``None`` and (2) is not
``None``. A ``sync`` flag would let an operator ask for states no surface can
implement.

**Two questions that look like these and are not, so a converter does not
reach for the wrong reader.** ``db._CONVERSATIONAL_SOURCE_TYPES`` is
``("talk", "web")`` — the same members as ``room_role == "member"`` — and gates
the caught-up dual-read. Its own comment says email is excluded *on purpose*
and that "Mirroring is not the criterion; guaranteed completeness is": the
store holds email turns only from ISSUE-136 forward, so counting them would pin
a room to the legacy path forever. Collapsing it into a reader here would be
this module's own mistake committed one question further along. And
``db.TRANSCRIPT_SURFACE_FILTER`` asks which surfaces' user rows the transcript
renders — ``('web', 'talk', 'email')``, a set whose domain is ``source_type``
values rather than surface names, and which must track a migration DELETE
holding a fourth value no surface table will ever have. Both stay literals.

**The key space is registry surface names only.** ``room`` and ``stream`` are
names in the *destination grammar* (``parse_output_target`` yields
``Destination("room", <token>)``), not surfaces, so they have no record here and
every reader answers False for them. That matters at exactly one converted
site: ``routing._room_for_destination`` dispatches on ``surface == "room"``
first, and a member check placed ahead of that dispatch would drop every
``room:<token>`` destination — the descriptor ISSUE-247 exists for. The leaf
can replace that function's terminal ``else`` arm, never its dispatch.

**Why the table is static, and why that is not a cosmetic detail.**
``routing._room_view`` reads the same fact through a config-built registry and
deliberately collapses "not a room view" with "surface not resolvable" — a
``talk`` binding on a deployment with ``talk.enabled = false`` has no transport
to read capabilities from. That collapse is safe for its own callers, which
branch only on ``"canonical"``. It is not safe for the sites reading this
module: ``web_app._user_row_display`` asks whether a stored row's
``origin_surface`` is foreign to the room, and if ``talk`` were unresolvable in
the web process every historic Talk turn would start rendering as an external
message; the scheduler's confirmation gate would start putting the prompt on
the mirror Talk leg. A surface's role in the room model is a fact about the
code and must not vary with an operator's switch. So there are two readers of
``room_view`` answering two questions and both stay: ``surfaces.room_view()``
for "what role does this surface play", ``routing._room_view()`` for "is there
a live transport for this surface right now".

Each transport keeps its own literal ``room_view`` in ``TransportCapabilities``
rather than reading it from here, because somebody adding a surface is looking
at the transport class; a test holds the two in agreement. Same arrangement as
``sandbox_cache_sweeper`` restating ``executor``'s cache directory names.

Every reader takes ``object`` rather than ``str``, because callers pass values
straight off database rows, and answers the conservative value for anything it
does not recognise: an unknown surface is not a member, not a view and has no
mirror mode. Nothing here raises.

"Conservative" means *not a room surface*, and the lookup is exact — no
trimming, no case folding — so ``"Talk"`` and ``"talk "`` answer the same as a
name nobody has heard of. That is the safe direction at six of the seven sites
this feeds. It is not at the seventh: ``web_app._user_row_display`` reads the
predicate **negated**, to mark a row as foreign to the room, so a damaged
``messages.origin_surface`` there would render a genuine Talk turn as an
external message. The column is written verbatim from ``task.source_type`` and
no writer transforms it, which is why this is a note rather than a ``strip()``
— normalizing would answer for a value the product cannot currently produce,
and quietly accept one it should not.

stdlib-only leaf: it imports nothing from the package. ``transport`` imports
``db`` at module level, so anything both ``transport`` and ``db``'s callers
might read has to sit below them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RoomRole = Literal["member", "guest"]
RoomView = Literal["canonical", "external"]
UserTurnMirror = Literal["as_user", "attributed"]


@dataclass(frozen=True)
class SurfaceRoomFacts:
    """One surface's answers to the three questions above.

    ``user_turn_mirror`` says how an external room view carries a turn authored
    on another surface: ``as_user`` posts it as the author where an author
    credential is available and degrades to an attributed repost where it is
    not (Talk, today's ladder exactly — ``web_app._mirror_web_turn_as_user`` at
    ingest, ``scheduler._format_mirror_user_repost`` at delivery when the
    external-id stamp is absent); ``attributed`` is a surface that can only ever
    repost under the bot's own identity.

    **What this field does not carry.** The shipped behaviour is keyed on the
    ``(origin, destination)`` pair, not on the destination alone: a web-origin
    repost carries the user's body, while an email-origin one carries sender and
    subject and **never the body — that is the wrapped untrusted prompt** (see
    `.claude/rules/transport.md`). This field cannot express that difference and
    does not try to; the body policy stays with the caller. A third-surface
    implementer reading ``as_user`` off this table and posting an external
    correspondent's raw body into a room would be committing a content-safety
    failure, not a routing one.
    """

    room_role: RoomRole | None
    room_view: RoomView | None
    user_turn_mirror: UserTurnMirror | None


# Every surface name `transport.make_registry` can produce, and only those: the
# keys are transport names, never the destination-grammar names noted above.
# Two tests hold the table against the registry — one over the names a
# fully-enabled registry produces, one over the assignments in `make_registry`'s
# own source, because the first cannot see a transport added behind a new config
# flag. `SurfaceRoomFacts` takes no defaults so a record cannot be half-filled:
# an empty one satisfies a key check while answering "not a room surface" at
# every converted site, which is the failure the coverage tests exist to stop.
#
# Deliberately a plain mutable dict rather than a `MappingProxyType`, because
# the model conversion's negative control removes `talk`'s row to prove the
# converted sites read this table rather than a stale literal. Mutate it only
# through `monkeypatch`: every reader resolves this global per call, so a direct
# mutation leaks into every later test in the same worker.
SURFACES: dict[str, SurfaceRoomFacts] = {
    "talk": SurfaceRoomFacts(
        room_role="member", room_view="external", user_turn_mirror="as_user",
    ),
    "web": SurfaceRoomFacts(
        room_role="member", room_view="canonical", user_turn_mirror=None,
    ),
    # Guest: an email threaded back into a room joins that room's transcript and
    # never mints one. No room view — see the durable-place test above.
    #
    # Two literals in `db.py` cover the same names for a different question, and
    # neither is derived from this row: `TRANSCRIPT_SURFACE_FILTER`'s
    # `('web', 'talk', 'email')` asks which surfaces' user rows the transcript
    # renders, and the DELETE in `_migrate_nonconversational_transcript_cleanup`
    # has to stay in step with it. That question's domain is `source_type`
    # values rather than surface names, so it stays where it is. The two sets
    # being equal today is a coincidence, not a derivation.
    "email": SurfaceRoomFacts(
        room_role="guest", room_view=None, user_turn_mirror=None,
    ),
    "ntfy": SurfaceRoomFacts(
        room_role=None, room_view=None, user_turn_mirror=None,
    ),
    "istota_file": SurfaceRoomFacts(
        room_role=None, room_view=None, user_turn_mirror=None,
    ),
    "repl": SurfaceRoomFacts(
        room_role=None, room_view=None, user_turn_mirror=None,
    ),
}

_NO_FACTS = SurfaceRoomFacts(
    room_role=None, room_view=None, user_turn_mirror=None,
)


def _facts(surface: object) -> SurfaceRoomFacts:
    if not isinstance(surface, str):
        return _NO_FACTS
    return SURFACES.get(surface, _NO_FACTS)


def room_role(surface: object) -> str | None:
    """``"member"``, ``"guest"``, or None for a surface that is never a room
    turn or that has no record here."""
    return _facts(surface).room_role


def room_view(surface: object) -> str | None:
    """``"canonical"``, ``"external"``, or None.

    The static answer. ``routing._room_view`` answers the *other* question —
    whether a live transport for this surface exists on this deployment right
    now — and a delivery planner wants that one. See the module docstring.
    """
    return _facts(surface).room_view


def is_room_member(surface: object) -> bool:
    """True when this surface creates and owns rooms.

    The workhorse, but **not for every site that gates on a surface name**, and
    the difference is the one a converter has to get right. A ``guest`` surface
    answers False, which is the point at the ownership sites — email writes into
    a room it finds and never registers, binds, renames or joins one.

    It is the wrong reader wherever the question is "may this surface put a
    ``role='user'`` row in a room at all", which admits email:
    ``commands._TRANSCRIPT_SURFACES`` is ``("web", "talk", "email")`` and gates
    ``_record_confirm_exchange``, so reading this predicate there would stop an
    email ``!confirm`` recording its exchange — and that row is a durable
    authorization record. The neighbouring ``!steer`` write really is
    ``("talk", "web")`` and really does ask this. Two writes in one file, two
    questions; the spec's conversion table calls both of them ownership and is
    wrong about the first.
    """
    return _facts(surface).room_role == "member"


def is_room_view(surface: object) -> bool:
    """True when this surface shows its users the room's transcript.

    Distinct from `is_room_member` at exactly one site — the scheduler's
    confirmation-prompt mirror gate, which asks whether a task's own origin
    surface shows it the question itself. The two coincide for every surface
    that exists today and are separate questions; keep them apart.
    """
    return _facts(surface).room_view is not None


def user_turn_mirror(surface: object) -> str | None:
    """``"as_user"``, ``"attributed"``, or None. Meaningful only where
    ``room_view`` is ``"external"``; see `SurfaceRoomFacts`."""
    return _facts(surface).user_turn_mirror


def origin_surface_for_source_type(source_type: object) -> str | None:
    """The surface a task with this ``source_type`` originated on, or None.

    **This is not `transport.registry._surface_for_source_type` and must not be
    routed through it.** That one answers "where do I deliver this task's
    result", and to do so maps everything that is not ``email`` / ``repl`` /
    ``web`` to ``"talk"`` — ``briefing``, ``scheduled``, ``subtask``,
    ``heartbeat``, ``cli``, ``istota_file``, ``doctor``, ``playbook`` and the
    empty string included. Asking it *this* question flips False to True for
    eight of the twelve shipped source types (the spec that called for this
    module says six; measured, ``istota_file`` and ``doctor`` flip too, and so
    does the empty string), and at the scheduler's confirmation gate the
    predicate is negated: the prompt would be suppressed on the mirror leg for
    cron, briefing and heartbeat tasks, which parks them with the question
    delivered nowhere until `expire_stale_confirmations` kills them two hours
    later. Two questions, two mappings.

    A scheduled task originates on no surface at all, so the answer is None
    and both room predicates answer False for it. Across all twelve shipped
    source types that pair answers exactly what the `source_type in
    ROOM_SURFACES` literal at the two scheduler gates answers today. The five
    that are also surface names (``talk``, ``web``, ``email``, ``repl``,
    ``istota_file``) pass through; the other seven (``briefing``, ``cli``,
    ``doctor``, ``heartbeat``, ``playbook``, ``scheduled``, ``subtask``) do not.

    Derived from `SURFACES` rather than from a second list so the two cannot
    drift. A surface name that is never a source type (``ntfy``) would pass
    through if one ever arrived, which changes no answer: its record makes it
    neither a member nor a view.
    """
    if not isinstance(source_type, str):
        return None
    return source_type if source_type in SURFACES else None
