"""What a task in a shared room may reach, and the room-scoped answers beside it.

A turn runs with its sender's reach (ISSUE-576). A member's turn in a shared
room reaches everything it would in their private room: asking in a room they
know others read is the decision that the answer may be read there, so there is
no per-room grant to make first. A guest's turn (emissary mode, multiplayer D2)
runs as the room's host at room-safe reach: the guest has no data of their own
and the host asked nothing, so every scope is withheld. So does a task nobody
asked in the room, such as a subtask whose conversation is a shared room. A
member's own cron job or briefing aimed at a room they are in counts as that
member asking (ISSUE-594). A scope is a skill
whose manifest says ``shared_room: private`` (the default), or one of the two
synthetic scopes that are not skills:

- ``files``: the workspace, ``{mount}/Users/{user_id}``, and the per-resource
  mounts, which the sandbox binds only when this is not withheld.
- ``memory``: ``USER.md``, dated memories, recalled memories, playbooks,
  knowledge-graph facts and per-skill overlays.

A ``shared_room: safe`` skill is never a scope, and the room's own
``CHANNEL.md`` is shared by construction. What is withheld is enforced by what
the task can reach, at the seams ``execute_task`` and ``task_env`` apply it to.

What a member's turn in a shared room loses is the *ambient* part of their
memory, which reaches the prompt without being asked for (`ambient_memory_off`):
the memory skill stays, so a member who asks for a note gets it.

A correspondent's turn (`turn_author`: mail somebody else wrote, on an email
thread room or roomless) is a third posture. It runs as the host and withholds
every scope except a short allowlist (`correspondent_scopes`), so a skill added
later starts out of reach. The host's standing instructions, `USER.md` and
per-skill overlays, still load although ``memory`` is withheld
(`Reach.standing_memory_loads`), since they are fixed text the host wrote. The
rest of their memory does not: the correspondent's mail would be the search
query and the reply a way out. The native web tools go too unless the user
allowed `web` (`Reach.web_tools_load`). The reply itself goes through the
unchanged outbound gate. The host's own tasks on a thread room withhold nothing.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal

from istota import db
from istota.browser.owner import BRIEFINGS_IDENTITY

logger = logging.getLogger(__name__)

SYNTHETIC_SCOPES = ("files", "memory")

TurnAuthor = Literal["own", "guest", "correspondent"]

# Far above `scheduler.max_subtask_depth`; a chain this long is not trusted.
_TURN_CHAIN_LIMIT = 64

#: What a correspondent's turn keeps. `email` for its body, the reply format;
#: `skills` so a widened menu skill can be opened; `transcribe` and `whisper`
#: read the mail's own staged attachments, and `markets` public data, so none
#: makes anything private readable by the sender who reads the reply.
CORRESPONDENT_DEFAULT_SCOPES = frozenset({"email", "skills", "transcribe", "whisper", "markets"})


@dataclass(frozen=True)
class CorrespondentAllowance:
    """What a correspondent's turn may reach. ``web`` names the native web
    tools, which are not a scope."""

    scopes: frozenset[str] = frozenset()
    web: bool = False


@dataclass(frozen=True)
class Reach:
    """One task's withheld set and the posture it was derived under.

    The set alone cannot say "`memory` withheld, but `USER.md` and overlays
    load", nor carry the allowance's ``web``.
    """

    withheld: frozenset[str]
    author: TurnAuthor
    allowance: CorrespondentAllowance = CorrespondentAllowance()

    @property
    def standing_memory_loads(self) -> bool:
        """Whether `USER.md` and per-skill overlays may reach this task."""
        return "memory" not in self.withheld or self.author == "correspondent"

    @property
    def web_tools_load(self) -> bool:
        """Whether the native web tools reach this task. A query or a URL is an
        outbound action, which a guest's turn never takes and a correspondent's
        takes only where the user allowed `web`."""
        if self.author == "correspondent":
            return self.allowance.web
        return self.author != "guest"


#: Research tasks run as `__briefings__` behind the executor's research gate:
#: a group edition's research, and an agent shared block's whole run.
BRIEFING_RESEARCH_SOURCE_TYPES = frozenset({"group_briefing_research", "shared_block_research"})


def is_briefing_research(task: "db.Task") -> bool:
    return task.source_type in BRIEFING_RESEARCH_SOURCE_TYPES or task.user_id == BRIEFINGS_IDENTITY


def scope_names(skill_index: Mapping[str, object]) -> list[str]:
    """Every scope: private skills, then the two synthetic."""
    private = sorted(
        name for name, meta in skill_index.items()
        if getattr(meta, "shared_room", "private") != "safe"
        and name not in SYNTHETIC_SCOPES
    )
    return [*private, *SYNTHETIC_SCOPES]


def all_scopes(skill_index: Mapping[str, object]) -> frozenset[str]:
    """Every scope, which is what a restricted task withholds."""
    return frozenset(scope_names(skill_index))


def correspondent_scopes(
    conn: sqlite3.Connection | None,
    user_id: str,
    *,
    skill_index: Mapping[str, object],
) -> CorrespondentAllowance:
    """What ``user_id``'s correspondents' turns keep: the default set, of the
    scopes this index has."""
    return CorrespondentAllowance(
        scopes=CORRESPONDENT_DEFAULT_SCOPES & all_scopes(skill_index),
    )


def withheld_for_task(
    conn: sqlite3.Connection | None,
    task: "db.Task",
    *,
    skill_index: Mapping[str, object],
    own_addresses: Collection[str] | None = None,
) -> frozenset[str]:
    """What one task may not reach: `reach_for_task`'s set, for the callers
    that read nothing else."""
    return reach_for_task(
        conn, task, skill_index=skill_index, own_addresses=own_addresses,
    ).withheld


def reach_for_task(
    conn: sqlite3.Connection | None,
    task: "db.Task",
    *,
    skill_index: Mapping[str, object],
    own_addresses: Collection[str] | None = None,
) -> Reach:
    """What one task may not reach and who wrote its turn, from its own row.
    The one derivation the executor's reach seams and the `skills` CLI's guard
    both read.

    Every scope on a guest's turn. All but the allowlist on a correspondent's
    (`correspondent_scopes`), with a room or without one. Every scope, too, on
    a task in a room more than one human reads that no member asked there: one
    with no origin surface (a subtask, a CLI task, a heartbeat) unless it is
    the user's own cron job or briefing and the user is a current member. Such
    a task's answer lands in the room with no member asking, so the consent a
    member's turn carries does not reach it. Nothing otherwise: a member's turn
    runs at full reach in every room (ISSUE-576), and the host's own tasks in
    an email thread room withhold nothing (`is_email_thread_room`).

    ``conn`` is ``None`` for a database that does not exist, which holds no
    room. ``own_addresses`` is `turn_author`'s, and the default can only misread
    the user's own mail as a correspondent's. Any error reading the turn or the
    room withholds every scope, and an unreadable author loads no standing
    memory.
    """
    if is_briefing_research(task):
        withheld = frozenset(set(skill_index) - {"browse", "skills", "untrusted_input"})
        return Reach(withheld | frozenset(SYNTHETIC_SCOPES), "own")
    if task.guest_participant_id is not None:
        return Reach(all_scopes(skill_index), "guest")
    try:
        author = turn_author(conn, task, own_addresses=own_addresses)
    except Exception as exc:
        logger.warning(
            "room_scopes: could not read who wrote task %s, withholding every "
            "scope: %s", task.id, exc,
        )
        return Reach(all_scopes(skill_index), "guest")
    if author == "guest":
        return Reach(all_scopes(skill_index), "guest")
    if author == "correspondent":
        allowance = correspondent_scopes(conn, task.user_id, skill_index=skill_index)
        return Reach(all_scopes(skill_index) - allowance.scopes, author, allowance)
    return Reach(_withheld_from_own_task(conn, task, skill_index), "own")


def _withheld_from_own_task(
    conn: sqlite3.Connection | None,
    task: "db.Task",
    skill_index: Mapping[str, object],
) -> frozenset[str]:
    """`reach_for_task` for a task whose turn is the user's own: what its room
    says. A thread room is asked first, since the poller marks its turns group
    chats and the host's subtask there has no origin surface."""
    from .surfaces import origin_surface_for_source_type

    if not task.conversation_token:
        return frozenset()
    if conn is not None:
        try:
            if is_email_thread_room(conn, task.conversation_token):
                return frozenset()
        except Exception as exc:
            logger.warning(
                "room_scopes: could not read the room %s, withholding every "
                "scope: %s", task.conversation_token, exc,
            )
            return all_scopes(skill_index)
    member_surface = origin_surface_for_source_type(task.source_type) is not None
    if not member_surface and (task.is_group_chat or task.audience == "mixed"):
        return all_scopes(skill_index)
    if conn is None:
        return frozenset()
    try:
        token = canonical_token(conn, task.conversation_token)
        if token is None or not (
            db.room_is_shared(conn, token) or task.is_group_chat
            or task.audience == "mixed"
        ):
            return frozenset()
        if not member_surface and not _members_own_schedule(conn, task, token):
            return all_scopes(skill_index)
    except Exception as exc:
        logger.warning(
            "room_scopes: could not read the room %s, withholding every "
            "scope: %s", task.conversation_token, exc,
        )
        return all_scopes(skill_index)
    return frozenset()


def _members_own_schedule(
    conn: sqlite3.Connection, task: "db.Task", room_token: str,
) -> bool:
    """Whether this task is the task's user's own CRON.md job or briefing,
    aimed at a room they are a current member of (ISSUE-594).

    The job lives in the user's workspace and names this room as where its
    answer is read, which is the member asking in advance. Under bwrap a
    guest's turn and a restricted task cannot write that file. Without it
    (Docker, macOS, a checkout running ``istota serve``) a guest's tools can, but such a job could
    already be aimed at the host's private room at full reach, so this adds
    nothing a guest could not reach before. A ``scheduled`` task must point at
    a job row owned by its user; a briefing is only ever created by the
    scheduler from the user's own config.

    Membership is `is_current_member`.
    """
    if task.source_type == "scheduled":
        if task.scheduled_job_id is None:
            return False
        row = conn.execute(
            "SELECT user_id FROM scheduled_jobs WHERE id = ?",
            (task.scheduled_job_id,),
        ).fetchone()
        if row is None or row[0] != task.user_id:
            return False
    elif task.source_type == "briefing":
        if not task.briefing_name:
            return False
    else:
        return False
    return is_current_member(conn, room_token, task.user_id)


def is_current_member(conn: sqlite3.Connection, room_token: str, user_id: str) -> bool:
    """Whether ``user_id`` is in ``room_token`` now, not only once.

    A ``room_members`` row and, where the user has any principal participant
    row, a present one: Talk records a departure only as ``left_at`` and never
    removes the member row.
    """
    if not db.is_room_member(conn, room_token, user_id):
        return False
    present = conn.execute(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE left_at IS NULL) "
        "FROM room_participants WHERE room_token = ? AND user_id = ? "
        "AND kind = 'principal'",
        (room_token, user_id),
    ).fetchone()
    return present[0] == 0 or present[1] > 0


def turn_author(
    conn: sqlite3.Connection | None,
    task: "db.Task",
    *,
    own_addresses: Collection[str] | None,
) -> TurnAuthor:
    """Who wrote the turn this task answers: the one authorship predicate.

    Walks ``parent_task_id`` to the root, because a subtask or a `!retry` copy
    has its parent's user and room but no turn of its own. The chain is
    ``guest`` if any link is, else ``correspondent`` if any link is, else
    ``own``. A missing ancestor, a cycle or a chain past the limit is
    ``correspondent``, and so is a link belonging to another user.

    ``own_addresses`` is the user's own addresses (`mail.support.own_addresses`)
    and is required so a caller has to decide: ``None`` falls back to
    `db.own_addresses_without_config`, which misses TOML-only addresses and so
    can only misread the user's own mail as a correspondent's.

    With no database (``conn`` is ``None``) nothing can be walked or attributed:
    an email task is not trusted and any other task is the user's own. Raises
    what the reads raise; each caller chooses its failure direction.
    """
    if conn is None:
        if task.guest_participant_id is not None:
            return "guest"
        return "correspondent" if task.source_type == "email" else "own"

    someone_else = False
    seen: set[int] = set()
    current = task
    while True:
        if current.id in seen or len(seen) >= _TURN_CHAIN_LIMIT:
            someone_else = True
            break
        seen.add(current.id)
        link = _link_author(conn, current, task.user_id, own_addresses)
        if link == "guest":
            return "guest"
        if link == "correspondent":
            someone_else = True
        if current.parent_task_id is None:
            break
        parent = db.get_task(conn, current.parent_task_id)
        if parent is None:
            someone_else = True
            break
        current = parent
    return "correspondent" if someone_else else "own"


def _link_author(
    conn: sqlite3.Connection,
    task: "db.Task",
    user_id: str,
    own_addresses: Collection[str] | None,
) -> TurnAuthor:
    """One link of `turn_author`'s chain, read off its own rows."""
    if task.user_id != user_id:
        return "correspondent"
    if task.guest_participant_id is not None:
        return "guest"
    stored = _stored_turn_author(conn, task)
    if stored is not None:
        return stored
    if task.source_type == "email":
        return _mail_author(conn, task, own_addresses)
    return "own"


def _stored_turn_author(conn: sqlite3.Connection, task: "db.Task") -> TurnAuthor | None:
    """What the task's stored user row says, or None where it says nothing: no
    row, or an email row with neither column set, which must not read as the
    host's by default. On any other surface such a row predates attribution
    and is the user's, as every history reader reads it.
    """
    row = conn.execute(
        "SELECT author_user_id, author_label FROM messages "
        "WHERE task_id = ? AND role = 'user' ORDER BY id LIMIT 1",
        (task.id,),
    ).fetchone()
    if row is None:
        return None
    author_user_id, author_label = row[0], row[1]
    if author_user_id is not None:
        return "own" if author_user_id == task.user_id else "correspondent"
    if author_label:
        return "correspondent"
    return None if task.source_type == "email" else "own"


def _mail_author(
    conn: sqlite3.Connection,
    task: "db.Task",
    own_addresses: Collection[str] | None,
) -> TurnAuthor:
    """An email task with no attributed row, read from its `processed_emails`
    row (ISSUE-874): an approved held mail no room admitted keeps no stored row
    and still runs as the host on the sender's text.
    """
    ledger = conn.execute(
        "SELECT sender_email, routing_method FROM processed_emails "
        "WHERE task_id = ? ORDER BY id LIMIT 1",
        (task.id,),
    ).fetchone()
    if ledger is None:
        # A `!retry` copy has neither row and its parent decides on the chain.
        # Any other such task has a sender nobody can read, which is not proof
        # the user wrote it; `author_for_email_task` answers "the user's" here.
        return "own" if task.parent_task_id is not None else "correspondent"
    if not ledger[0] and ledger[1] != "sender_match":
        # `external_email_sender` reads no sender as a non-email turn.
        return "correspondent"
    own = None if own_addresses is None else sorted(own_addresses)
    author_user_id, author_label = db.author_for_email_task(conn, task.id, task.user_id, own)
    if author_label or author_user_id != task.user_id:
        return "correspondent"
    return "own"


def ambient_memory_off(conn: sqlite3.Connection | None, task: "db.Task") -> bool:
    """Whether this task's prompt leaves out the sender's ambient memory.

    True in a room more than one human reads now: a guest's turn, a turn whose
    stored audience is ``mixed``, a surface roster saying "group", or a
    registered room with more than one member. True as well in a group-owned
    room at any headcount (`db.is_group_owned_room`), one member included,
    without the turn being a group chat: `task_group_ids` still loads that
    group's notebook and the member's reach is unchanged. `USER.md`, dated and recalled
    memories, knowledge-graph facts and playbooks reach the prompt without the
    member asking for them, so a question about lunch could come back carrying
    a health note; that is the one thing asking in the room did not consent to.
    A fixed rule, not a setting. Fails toward leaving the memory out: a room
    whose audience cannot be read counts as shared.

    Not in an email thread room, asked ahead of the audience and the roster
    (the poller marks every thread turn a group chat): the thread is the
    host's correspondence. Their memory loads on their own turn there; on a
    correspondent's, the executor loads `USER.md` and overlays alone, from
    the withheld ``memory`` scope and the posture.
    """
    if is_briefing_research(task) or task.guest_participant_id is not None:
        return True
    if task.conversation_token and conn is not None:
        try:
            if is_email_thread_room(conn, task.conversation_token):
                return False
        except Exception as exc:
            logger.warning(
                "room_scopes: could not read the room %s, leaving ambient "
                "memory out: %s", task.conversation_token, exc,
            )
            return True
    if task.audience == "mixed" or task.is_group_chat:
        return True
    if not task.conversation_token or conn is None:
        return False
    try:
        token = canonical_token(conn, task.conversation_token)
        if token is None:
            return False
        # Headcount is not the rule here: a group room's history is read by
        # every future member, so a lone member's turn is no more private.
        if db.is_group_owned_room(db.get_room(conn, token)):
            return True
        return db.room_is_shared(conn, token)
    except Exception as exc:
        logger.warning(
            "room_scopes: could not read the audience of %s, leaving ambient "
            "memory out: %s", task.conversation_token, exc,
        )
        return True


def is_email_thread_room(conn, token: str | None) -> bool:
    """Whether ``token`` names a room bound to an email thread.

    The one rule the three seams ask (the guest branch at ingest, the withheld
    scopes, the ambient memory): such a room is the host's correspondence, so
    an admitted turn on it runs as the host, not as a guest, at whatever reach
    its author has (`reach_for_task`). The token form of
    `transport.email.threads.thread_room_for_task`. Raises what the reads
    raise; each caller chooses its failure direction.

    The user's private email room is bound to email too, under its creator's
    own token rather than a Message-ID, and is not a thread: its turns are the
    user's own (`transport.email.private_room`). The rule itself is
    `thread_binding`'s; this adds the canonicalisation.
    """
    from istota.transport.email.threads import thread_binding

    return thread_binding(conn, canonical_token(conn, token)) is not None


def canonical_token(conn, token: str | None) -> str | None:
    """The registry token a conversation token names, or None for no room.

    The one copy: `private_replies` re-exports it, and the memory skill CLI
    reaches it here without importing `private_replies`.
    """
    if not token:
        return None
    token = db._canonical_room_token(conn, token)
    return token if db.get_room(conn, token) is not None else None


#: The fence label a shared room's `CHANNEL.md` carries, in the prompt block,
#: in recall and in `memory show --channel` (multiplayer D24).
CHANNEL_NOTES_LABEL = "room notes"


def channel_notes_shared(
    conn: sqlite3.Connection,
    conversation_token: str | None,
    *,
    guest_turn: bool = False,
    is_group_chat: bool = False,
) -> bool:
    """Whether a room's `CHANNEL.md` may have several authors (D24).

    A guest's turn, a surface roster saying "group", or a registered room more
    than one human has ever been in. Ever, not now: notes a member wrote stay
    theirs after they leave. Raises what the reads raise; each caller chooses
    its failure direction, and both choose the fence.
    """
    if guest_turn or is_group_chat:
        return True
    token = canonical_token(conn, conversation_token)
    return token is not None and db.room_was_ever_shared(conn, token)


def _is_own_push_token(user_id: str, token: str) -> bool:
    """Whether ``token`` is ``user_id``'s own SMS or WhatsApp 1:1 token."""
    from istota.transport.sms import sms_conversation_token
    from istota.transport.whatsapp import whatsapp_conversation_token

    return token in (
        sms_conversation_token(user_id), whatsapp_conversation_token(user_id),
    )


def task_group_ids(
    conn: sqlite3.Connection,
    task: "db.Task",
    *,
    own_addresses: Collection[str] | None = None,
) -> list[str]:
    """The groups whose material this task may carry, sorted; the one answer
    the prompt's ``## Group memory``, the ``Groups/<id>`` binds and the
    ``kv --group`` gate (multiplayer D21) all read.

    A group is in it when ``task.user_id`` is a current member and everyone
    who reads the answer is too (groups spec D6). Off a room that is every
    group of the user, since the answer goes to the user alone (a shared room
    is refused as a delivery target, multiplayer Stage 15). In a room, only the
    groups whose current members cover the room's members and present
    principal participants; in a room linked to a group (``rooms.group_id``),
    that group alone, under the same rule.

    Nothing at all on a turn the user did not write (`turn_author`: a guest's,
    or a correspondent's, which off a room would otherwise be every group),
    on a turn whose stored
    audience is ``mixed``, or while any guest or agent is present: the charter
    is "may be said in front of every member", and a guest or another bot is
    not one. Nothing either where the audience cannot be read: a room with no
    recorded readers, or a surface roster saying "group" over a room the
    registry records one person in. A token naming no registered room is the
    first case, so an email thread with no thread room loads nothing.

    The exception is the user's own SMS or WhatsApp 1:1 (multiplayer D23):
    its token is derived from ``task.user_id`` alone and names no room, and
    the conversation is as private as a room-less task, so it loads the same
    set. Recognised by deriving the token, never by its prefix, so another
    user's push token is still an unknown room.

    The audience rule here is the only gate on group material; a member's
    full reach in a shared room is their own data and never widens it. Raises
    on a database error; the caller treats that as the empty set.
    """
    if is_briefing_research(task) or task.guest_participant_id is not None or task.audience == "mixed":
        return []
    if turn_author(conn, task, own_addresses=own_addresses) != "own":
        return []
    groups = db.list_user_groups(conn, task.user_id)
    automatic = {g for g in groups if db.get_group(conn, g)["system_key"] == "all_users"}
    groups = [g for g in groups if g not in automatic]
    if not task.conversation_token:
        return groups

    from istota.rooms import policy as room_policy

    room_token = canonical_token(conn, task.conversation_token)
    if room_token is None:
        if _is_own_push_token(task.user_id, task.conversation_token):
            return groups
        room_token = task.conversation_token
    room = room_policy.room_readers(conn, room_token)
    if room.guests or room.others:
        logger.debug(
            "group_memory_skipped reason=non_member_reader token=%s",
            task.conversation_token,
        )
        return []
    readers = set(room.members)
    if not readers or (task.is_group_chat and len(readers) < 2):
        logger.debug(
            "group_memory_skipped reason=room_members_unknown token=%s",
            task.conversation_token,
        )
        return []
    # A room linked to a group (multiplayer Stage 27) carries that group and no
    # other, still under the rule below: the link narrows the candidates, it
    # never makes a reader a member.
    registered = db.get_room(conn, room_token)
    if registered is not None and registered.group_id:
        groups = [g for g in [*groups, *automatic] if g == registered.group_id]
    return [
        g for g in groups if readers <= set(db.list_group_members(conn, g))
    ]
