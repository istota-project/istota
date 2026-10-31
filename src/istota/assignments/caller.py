"""Who is calling the assignment CLI: the user's own turn, a wake, or neither.

Decided from the task row named by `ISTOTA_TASK_ID`, read from the framework
DB, never from anything the model wrote. `ISTOTA_ASSIGNMENT_ID` is a
convenience default for the CLI's argument and never authority.

**A user turn** is a running task of this user, from web, Talk or the local
CLI, that answers a turn the user wrote in a room only they read: no guest, no
other author on the stored turn, no shared or group-owned room, no parent task,
no scheduled job, and not itself a wake. Email, SMS and WhatsApp are refused
because their prompts can carry other people's text; scheduled, briefing,
subtask and wake tasks are not the user speaking.

**A wake** is a running `source_type = 'assignment'` task of this user with
`assignment_id` set. Its wake row is found by task id.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from istota import db
from istota.assignments import store

USER_TURN_SOURCES = ("web", "talk", "cli")
#: Set in a wake's skill-proxy environment (`sandbox.task_env`); a default for
#: the CLI's argument, checked against the task row.
ASSIGNMENT_ID_VAR = "ISTOTA_ASSIGNMENT_ID"


@dataclass(frozen=True)
class UserTurn:
    task_id: int


@dataclass(frozen=True)
class Wake:
    task_id: int
    assignment_id: int
    wake_id: int | None


@dataclass(frozen=True)
class Other:
    task_id: int | None
    reason: str


Caller = UserTurn | Wake | Other


def _parse_task_id(raw: object) -> int | None:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def classify(conn: sqlite3.Connection, *, task_id: object, user_id: str) -> Caller:
    """Classify the calling task. `reason` on `Other` is for logs, not the model."""
    tid = _parse_task_id(task_id)
    if tid is None:
        return Other(None, "no_task")
    task = db.get_task(conn, tid)
    if task is None or not user_id or task.user_id != user_id:
        return Other(tid, "not_this_users_task")
    if task.status != "running":
        return Other(tid, "not_running")

    if task.source_type == "assignment" or task.assignment_id is not None:
        if task.source_type != "assignment" or task.assignment_id is None:
            return Other(tid, "inconsistent_wake_row")
        wake = store.get_wake_by_task(conn, tid)
        return Wake(task_id=tid, assignment_id=task.assignment_id, wake_id=wake.id if wake else None)

    if _is_user_turn(conn, task):
        return UserTurn(tid)
    return Other(tid, "not_a_user_turn")


def _is_user_turn(conn: sqlite3.Connection, task: db.Task) -> bool:
    from istota.rooms.scopes import canonical_token, turn_author

    if task.source_type not in USER_TURN_SOURCES:
        return False
    if (task.guest_participant_id is not None or task.is_group_chat
            or task.parent_task_id is not None or task.scheduled_job_id is not None
            or task.command or task.skill):
        return False
    if turn_author(conn, task, own_addresses=None) != "own":
        return False
    if task.conversation_token:
        room = canonical_token(conn, task.conversation_token)
        # An unregistered token is not a room we can prove private, so it is refused.
        if room is None or not db.is_private_room_of(conn, room, task.user_id):
            return False
    elif task.source_type != "cli":
        return False
    return True


def user_turn(conn: sqlite3.Connection, *, task_id: object, user_id: str) -> bool:
    return isinstance(classify(conn, task_id=task_id, user_id=user_id), UserTurn)
