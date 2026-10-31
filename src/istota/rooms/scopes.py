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
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal

from istota import db
from istota.browser.owner import BRIEFINGS_IDENTITY
from istota.rooms import egress
from istota.sandbox.user_scope import ROOM_NOTES_WITHHELD

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

#: (user, name) already warned about, so a stale setting is one line per process
#: rather than one per seam per task.
_warned_correspondent_names: set[tuple[str, str]] = set()


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
    #: False only for an own task that holds the user's private data and is
    #: attended or runs an approved `open` job: nothing else may drive a browser
    #: signed in to their accounts (#899).
    isolated_browser: bool = True
    #: Where this task may send bytes. None: any public host. Otherwise the hosts
    #: its owner approved (`rooms.egress`). Every egress seam reads this.
    egress_hosts: frozenset[str] | None = None
    #: The part of `withheld` that is there only because this task is limited to
    #: those hosts: each `external` skill with no `skill:` grant.
    egress_withheld: frozenset[str] = frozenset()
    #: An approved public job, or a subtask or `!retry` copy of one: the research
    #: posture on the job's own identity and room. It holds nothing private, which
    #: is why `egress_hosts` is None. Decided once per occurrence and kept on its
    #: row (`job_posture`).
    public: bool = False

    @property
    def restricted(self) -> bool:
        """Whether this task takes the restricted posture: its own temp dir, no
        vault. That is for a task whose input somebody else wrote or nobody asked
        for. A limited job loses named skills and is otherwise the user's own."""
        return bool(self.withheld - self.egress_withheld)

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

#: All research and a public job keep. `browse` is what they are for, `skills` opens
#: its body and `untrusted_input` is its companion.
PUBLIC_KEPT_SKILLS = frozenset({"browse", "skills", "untrusted_input"})


#: Off the menu and off the proxy in an incognito room (ISSUE-892), and their
#: CLIs refuse there as well. The `memory` skill stays, for the room's own
#: CHANNEL.md. Not scopes: withholding one would give the task a restricted
#: temp dir, which is for input somebody else wrote.
INCOGNITO_DISABLED_SKILLS = frozenset(
    {"memory_search", "kv", "relay", "room", "rooms", "assignment"}
)


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
    """What ``user_id``'s correspondents' turns keep: the default set and the
    user's widening, of the scopes this index has.

    The writers refuse the names no setting may widen; this drops them too, since
    a hand-edited row reaches here unchecked. An unknown name is a typo or a
    removed skill. `wallet.policy` passes an empty index, against which every
    name looks unknown, so that case is not worth a warning.
    """
    from istota.user_profiles import (
        CORRESPONDENT_REFUSED_SCOPES,
        stored_correspondent_scopes,
    )

    widening = set(stored_correspondent_scopes(conn, user_id)) if conn is not None else set()
    web = "web" in widening
    widening.discard("web")
    known = all_scopes(skill_index)
    refused = widening & set(CORRESPONDENT_REFUSED_SCOPES)
    widening -= refused
    for name in sorted(refused):
        _warn_about_correspondent_name(user_id, name, "may not be widened")
    if skill_index:
        for name in sorted(widening - known):
            _warn_about_correspondent_name(user_id, name, "is not a scope")
    return CorrespondentAllowance(
        scopes=(CORRESPONDENT_DEFAULT_SCOPES | widening) & known,
        web=web,
    )


def _warn_about_correspondent_name(user_id: str, name: str, why: str) -> None:
    if (user_id, name) in _warned_correspondent_names:
        return
    _warned_correspondent_names.add((user_id, name))
    logger.warning(
        "room_scopes: correspondent_scopes for %s names %r, which %s; dropped",
        user_id, name, why,
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

    An own task that is unattended and holds private data is limited as well
    (`_egress_for`): `egress_hosts` is what its owner approved, and each `external`
    skill with no `skill:` grant joins the set (`Reach.egress_withheld`). A job its
    owner approved as public has open web and holds nothing (`Reach.public`), asked
    first, so an owner who has approved both `public` and `open` gets the narrower.

    ``conn`` is ``None`` for a database that does not exist, which holds no
    room. ``own_addresses`` is `turn_author`'s, and the default can only misread
    the user's own mail as a correspondent's. Any error reading the turn or the
    room withholds every scope, and an unreadable author loads no standing
    memory.
    """
    if is_briefing_research(task):
        return Reach(_public_withheld(skill_index), "own")
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
    posture = _job_posture_narrowing(conn, task)
    if posture == "public":
        return Reach(
            _public_withheld(skill_index) | {ROOM_NOTES_WITHHELD}, "own", public=True,
        )
    if posture == "unknown" and _is_descendant(conn, task):
        return Reach(all_scopes(skill_index), "own", egress_hosts=frozenset())
    withheld = _withheld_from_own_task(conn, task, skill_index)
    egress_hosts, granted_skills = _egress_for(conn, task)
    if egress_hosts is None:
        return Reach(withheld, "own", isolated_browser=bool(withheld))
    egress_withheld = _external_skills(skill_index) - granted_skills - withheld
    return Reach(
        withheld | egress_withheld, "own", isolated_browser=True,
        egress_hosts=egress_hosts, egress_withheld=egress_withheld,
    )


def _public_withheld(skill_index: Mapping[str, object]) -> frozenset[str]:
    return frozenset(set(skill_index) - PUBLIC_KEPT_SKILLS) | frozenset(SYNTHETIC_SCOPES)


JobPosture = Literal["public", "private", "unknown"]


def job_posture(conn: sqlite3.Connection, task: "db.Task") -> JobPosture:
    """What this task's occurrence was decided as, from its row, never asking the
    job again once it has answered: the row can change or go while the task runs,
    and a `once` job's goes ahead of the drain.

    `private` is what makes a missing job row mean something. A `once` job that ran
    as itself recorded `private`, and keeps its writes. `unknown` is an occurrence
    nobody decided whose job has gone, so no one can say what it held or read, and
    each reader narrows. Raises what the reads raise; each caller chooses its
    failure direction.
    """
    root = _decision_root(conn, task)
    stored = egress.stored_posture(conn, root.id)
    if stored == egress.POSTURE_PUBLIC:
        return "public"
    if stored == egress.POSTURE_PRIVATE:
        return "private"
    if is_occurrence(root) and egress.job_row(conn, root) is None:
        return "unknown"
    return "public" if egress.public_job_approved(conn, root) else "private"


def is_public_job(conn: sqlite3.Connection, task: "db.Task") -> bool:
    """Whether this task's occurrence is a public job. Raises what the reads raise."""
    return job_posture(conn, task) == "public"


def _job_posture_narrowing(conn: sqlite3.Connection | None, task: "db.Task") -> JobPosture:
    """`job_posture`, where a failure narrows: the task falls through to
    `_egress_for`, which limits it."""
    if conn is None:
        return "private"
    try:
        return job_posture(conn, task)
    except Exception as exc:  # noqa: BLE001 — a read failure narrows
        logger.warning("egress_decide_failed task=%s: %s", task.id, type(exc).__name__)
        return "private"


def _is_descendant(conn: sqlite3.Connection, task: "db.Task") -> bool:
    return _decision_root(conn, task).id != task.id


def _external_skills(skill_index: Mapping[str, object]) -> frozenset[str]:
    return frozenset(
        name for name, meta in skill_index.items()
        if getattr(meta, "egress", "external") != "local"
    )


def _root_is_attended(root: "db.Task") -> bool:
    """Whether a person is there for this root task. TASKS.md is a surface and is
    not attended, because a limited task can write a line into it."""
    from .surfaces import origin_surface_for_source_type

    if root.source_type == "cli":
        return True
    surface = origin_surface_for_source_type(root.source_type)
    return surface is not None and surface != "istota_file"


def _egress_for(
    conn: sqlite3.Connection | None, task: "db.Task",
) -> tuple[frozenset[str] | None, frozenset[str]]:
    """Where an own task may send bytes: ``(hosts, granted skills)``, with None
    hosts for any public host. A subtask or `!retry` copy has its root's answer.

    `reach_for_task` has already asked about a public job, whose posture is more
    than an answer here. Reading the grants can fail only toward the empty set.
    """
    if conn is None:
        return frozenset(), frozenset()
    try:
        root = _decision_root(conn, task)
        if is_briefing_research(root) or _root_is_attended(root):
            return None, frozenset()
        if egress.job_approved(conn, root, "open"):
            return None, frozenset()
        subject = egress.subject_for(conn, root)
        return (
            egress.approved_hosts(conn, task.user_id, subject),
            egress.approved_skills(conn, task.user_id, subject),
        )
    except Exception as exc:  # noqa: BLE001 — a read failure narrows
        logger.warning("egress_decide_failed task=%s: %s", task.id, type(exc).__name__)
        return frozenset(), frozenset()


def egress_subject(conn: sqlite3.Connection, task: "db.Task") -> str | None:
    """What this task's refusals and grants hang on: its root's subject."""
    return egress.subject_for(conn, _decision_root(conn, task))


def is_occurrence(task: "db.Task") -> bool:
    """Whether this task is one run of a job, made from the job's row."""
    return task.source_type == "scheduled" and task.scheduled_job_id is not None


def _decision_root(conn: sqlite3.Connection, task: "db.Task") -> "db.Task":
    """The task whose answer this one takes: the nearest job occurrence up the
    `parent_task_id` walk, else the task at its end.

    A job re-run links to the failed occurrence it replaces and runs the job as it
    stands now, so it decides for itself. A subtask or a `!retry` copy, which has
    no job of its own, takes the answer of what it descends from. `turn_author` has
    just read this chain as whole, so a break here is a race, and raises.
    """
    root = task
    for link in _turn_chain(conn, task):
        if link is None:
            raise LookupError("broken task chain")
        root = link
        if is_occurrence(link):
            break
    return root


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
    for current in _turn_chain(conn, task):
        if current is None:
            someone_else = True
            break
        link = _link_author(conn, current, task.user_id, own_addresses)
        if link == "guest":
            return "guest"
        if link == "correspondent":
            someone_else = True
    return "correspondent" if someone_else else "own"


def _turn_chain(conn: sqlite3.Connection, task: "db.Task") -> "Iterator[db.Task | None]":
    """``task``, then each ancestor up `parent_task_id`. Ends in None where the
    chain breaks: a missing ancestor, a cycle or a chain past the limit."""
    seen: set[int] = set()
    current = task
    while True:
        if current.id in seen or len(seen) >= _TURN_CHAIN_LIMIT:
            yield None
            return
        seen.add(current.id)
        yield current
        if current.parent_task_id is None:
            return
        parent = db.get_task(conn, current.parent_task_id)
        if parent is None:
            yield None
            return
        current = parent


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
    # An incognito room, at any headcount, by the task's own flag (ISSUE-892).
    if task.incognito or is_briefing_research(task) or task.guest_participant_id is not None:
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
    if task.incognito:
        return []
    if is_briefing_research(task) or task.guest_participant_id is not None or task.audience == "mixed":
        return []
    if turn_author(conn, task, own_addresses=own_addresses) != "own":
        return []
    if job_posture(conn, task) != "private":
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
