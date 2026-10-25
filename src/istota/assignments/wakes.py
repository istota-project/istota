"""When an assignment wakes, and the wake task that runs it.

`dispatch_due` is the body of the scheduler's `assignments` gate and the one
creator of wakes: `assignment wake` and the other user verbs only make an
assignment due, and the next tick creates the wake. Each assignment is decided
and acted on in its own `BEGIN IMMEDIATE` transaction that re-reads the row, so
two ticks racing cannot both wake one assignment, and one row that fails is
logged and skipped while the others proceed.

The rules, first match wins (spec Design section 7):

1. A deadline that has passed, with no deadline wake recorded for that
   deadline, wakes a sleeping or waiting assignment once (`fired_by =
   'deadline'`).
2. A sleeping assignment whose `next_wake_at` has come is woken (`time`).
3. A sleeping assignment with an unconsumed inbox event of a kind in
   `PULL_FORWARD_KINDS` is woken early (`inbox`), unless its last wake ended
   less than `min_wake_interval_minutes` ago; then `next_wake_at` moves to
   that floor.
4. A waiting assignment is woken (`inbox`) by an unconsumed event whose kind
   ends the wait it names (`WAIT_WAKERS`).

Every candidate passes `budget.check_before_wake` first; a spent budget pauses
it instead. The wake task is created only when the wake is due, never ahead
with `scheduled_for`, because `fail_ancient_pending_tasks` and `claim_task`'s
recovery key on `created_at`.

A wake task is an ordinary task in its own queue: `source_type` and `queue`
`assignment`, `max_attempts = 1` (the next wake is the retry), no conversation
token, no output target and no log channel, since its result goes nowhere but
the wake row.

`reap_orphans` finds wakes whose task ended without anyone finishing the wake
(a daemon restart, a task the stale recovery failed) and finishes them.
`finish_wake` is the one way a wake ends.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from istota import db
from istota.assignments import budget, lifecycle, store
from istota.assignments.charter import iso_z
from istota.relay.requests import write_transaction

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger(__name__)

FEATURE_FLAG = "skill_assignment"
SOURCE_TYPE = "assignment"
QUEUE = "assignment"
#: The task's stored prompt. The real prompt is built at pickup.
WAKE_PROMPT = "[assignment wake]"
SUMMARY_MAX_CHARS = 4000

#: The inbox kinds that end each wait. A proposed completion is answered by
#: `close`, which moves the state itself, so no inbox event ends that wait.
WAIT_WAKERS: dict[str, tuple[str, ...]] = {
    "user": ("user_note", "handoff_done", "charter_change"),
    "completion": (),
}

#: The inbox kinds that pull a sleeping assignment's wake forward: what the
#: user did since the last wake. `budget_low` and `user_edit` are written by the
#: host around a wake and are read by the next one whenever it comes.
PULL_FORWARD_KINDS = (
    "user_note", "handoff_done", "charter_change", "item_confirmed", "completion_rejected",
)

DEADLINE_EVENT = "deadline_wake_done"

#: How long a wake's task must have been terminal before the reaper finishes
#: the wake, so the worker that ran it gets there first.
ORPHAN_GRACE_SECONDS = 120

#: A task's terminal status, as the outcome `finish_wake` records.
_TASK_OUTCOMES = {"completed": "completed", "failed": "failed", "cancelled": "cancelled"}
#: The task outcome, as the wake's own outcome until the wake's `next` and
#: completion requests are read when it finishes.
_WAKE_OUTCOMES = {
    "completed": "no_next_step",
    "failed": "failed",
    "cancelled": "cancelled",
    "not_active": "not_active",
}


@dataclass(frozen=True)
class CreatedWake:
    assignment_id: int
    wake_id: int
    task_id: int
    fired_by: str


def feature_enabled(config: "Config") -> bool:
    return config.experimental.is_enabled(FEATURE_FLAG)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _wake_floor(conn: sqlite3.Connection, config: "Config", assignment_id: int) -> datetime | None:
    """When the next wake may start, by the floor after the last one ended."""
    row = conn.execute(
        "SELECT MAX(ended_at) FROM assignment_wakes WHERE assignment_id = ? AND ended_at IS NOT NULL",
        (assignment_id,),
    ).fetchone()
    ended = _parse(row[0]) if row else None
    if ended is None:
        return None
    return ended + timedelta(minutes=config.assignments.min_wake_interval_minutes)


def _deadline_wake_done(conn: sqlite3.Connection, assignment: store.Assignment) -> bool:
    """Whether the deadline as it stands now has already had its wake."""
    row = conn.execute(
        "SELECT 1 FROM assignment_events WHERE assignment_id = ? AND kind = ?"
        " AND json_extract(payload, '$.deadline') = ? LIMIT 1",
        (assignment.id, DEADLINE_EVENT, assignment.deadline),
    ).fetchone()
    return row is not None


def _decide(
    conn: sqlite3.Connection, config: "Config", assignment: store.Assignment, now: datetime,
) -> tuple[str | None, datetime | None]:
    """``(fired_by, None)`` to wake, ``(None, floor)`` to defer to a floor, else ``(None, None)``."""
    if assignment.state not in lifecycle.WAKEABLE_STATES:
        return None, None
    deadline = _parse(assignment.deadline)
    if deadline is not None and deadline <= now and not _deadline_wake_done(conn, assignment):
        return "deadline", None

    if assignment.state == "sleeping":
        due = _parse(assignment.next_wake_at)
        if due is not None and due <= now:
            return "time", None
        if store.has_unconsumed(conn, assignment.id, kinds=PULL_FORWARD_KINDS):
            floor = _wake_floor(conn, config, assignment.id)
            if floor is None or floor <= now:
                return "inbox", None
            if due is None or floor < due:
                return None, floor
        return None, None

    wakers: list[str] = []
    for waited in assignment.waiting_on:
        wakers.extend(WAIT_WAKERS.get(waited, ()))
    if wakers and store.has_unconsumed(conn, assignment.id, kinds=wakers):
        return "inbox", None
    return None, None


def _resolved_model_brain(
    config: "Config", assignment: store.Assignment,
) -> tuple[str | None, str | None, str | None]:
    """The wake task's model, effort and brain, as a cron job's are resolved:
    a brain pin only if `[brain] room_selectable` lists it, and the model
    against the brain that will run the wake."""
    from istota.brain import room_selectable_kinds
    from istota.scheduler import resolve_pinned_model_effort

    pinned = str(assignment.brain).strip() if assignment.brain else ""
    admitted = pinned if pinned in room_selectable_kinds(config.brain) else ""
    model, effort = "", assignment.effort or ""
    if assignment.model:
        model, effort = resolve_pinned_model_effort(
            config,
            source_type=SOURCE_TYPE,
            model=assignment.model,
            effort=effort,
            pinned_brain=pinned,
            label=f"Assignment {assignment.id} (user: {assignment.user_id})",
            warn_key=f"assignment_model:{assignment.id}",
        )
    return model or None, effort or None, admitted or None


def _dispatch_one(
    conn: sqlite3.Connection, config: "Config", assignment_id: int, now: datetime,
) -> CreatedWake | None:
    with write_transaction(conn):
        assignment = store.get_assignment(conn, assignment_id)
        if assignment is None:
            return None
        fired_by, floor = _decide(conn, config, assignment, now)
        if fired_by is None:
            if floor is not None:
                floor_iso = iso_z(floor)
                if assignment.next_wake_at != floor_iso:
                    store.update_assignment(conn, assignment_id, next_wake_at=floor_iso)
            return None
        if not budget.check_before_wake(conn, assignment):
            lifecycle.pause_for_budget(conn, assignment_id)
            logger.info("assignment %d paused: budget spent", assignment_id)
            return None

        model, effort, brain = _resolved_model_brain(config, assignment)
        task_id = db.create_task(
            conn,
            prompt=WAKE_PROMPT,
            user_id=assignment.user_id,
            source_type=SOURCE_TYPE,
            queue=QUEUE,
            assignment_id=assignment_id,
            max_attempts=1,
            skip_log_channel=True,
            model=model,
            effort=effort,
            brain=brain,
        )
        wake_id = store.insert_wake(conn, assignment_id, fired_by=fired_by, task_id=task_id)
        if fired_by == "deadline":
            store.insert_event(
                conn, assignment_id, kind=DEADLINE_EVENT, actor="system", wake_id=wake_id,
                payload={"deadline": assignment.deadline},
            )
        lifecycle.wake_created(
            conn, assignment_id, wake_id=wake_id, task_id=task_id, fired_by=fired_by,
        )
        return CreatedWake(assignment_id, wake_id, task_id, fired_by)


def dispatch_due(
    conn: sqlite3.Connection, config: "Config", *, now: datetime | None = None,
) -> list[CreatedWake]:
    """Create a wake for every assignment that is due. Returns what was created.

    One transaction per assignment, committed before the next; a failure is
    logged, rolled back and skipped. Does nothing with the flag off.
    """
    if not feature_enabled(config):
        return []
    moment = now or datetime.now(timezone.utc)
    candidates = [
        row[0] for row in conn.execute(
            "SELECT id FROM assignments WHERE state IN ('sleeping', 'waiting') ORDER BY id",
        ).fetchall()
    ]
    created: list[CreatedWake] = []
    for assignment_id in candidates:
        try:
            made = _dispatch_one(conn, config, assignment_id, moment)
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("assignment %d: could not create its wake; skipped", assignment_id)
            continue
        if made is not None:
            created.append(made)
    return created


# ---------------------------------------------------------------------------
# Ending a wake
# ---------------------------------------------------------------------------


def _task_outcome(task: db.Task | None) -> str:
    if task is None:
        return "failed"
    return _TASK_OUTCOMES.get(task.status, "failed")


def reap_orphans(conn: sqlite3.Connection, config: "Config") -> list[int]:
    """Finish every open wake whose task ended, or is gone, and nobody finished.

    A task counts once it has been terminal for `ORPHAN_GRACE_SECONDS`, so a
    worker finishing its own wake is not raced. Returns the wake ids finished.
    """
    rows = conn.execute(
        """
        SELECT w.id AS wake_id, w.task_id FROM assignment_wakes w
        LEFT JOIN tasks t ON t.id = w.task_id
        WHERE w.ended_at IS NULL
          AND (
              t.id IS NULL
              OR (t.status IN ('completed', 'failed', 'cancelled')
                  AND COALESCE(t.completed_at, t.updated_at) <= datetime('now', ?))
          )
        ORDER BY w.id
        """,
        (f"-{ORPHAN_GRACE_SECONDS} seconds",),
    ).fetchall()
    reaped: list[int] = []
    for row in rows:
        task = db.get_task(conn, row["task_id"]) if row["task_id"] is not None else None
        if finish_wake(config, task, _task_outcome(task), wake_id=row["wake_id"]):
            reaped.append(row["wake_id"])
    return reaped


def finish_wake(
    config: "Config", task: db.Task | None, outcome: str, *, wake_id: int | None = None,
) -> bool:
    """End one wake, in its own transaction. Never raises.

    `outcome` is how the task ended: `completed`, `failed`, `cancelled`, or
    `not_active` for a wake turned away at pickup. The wake is named by
    `wake_id`, or found from `task`. Records the end and returns an assignment
    still `active` to `sleeping` at its cadence (`not_active`: due at once, so
    turning the flag back on resumes it); a state the user set while the wake
    ran is kept. Returns True when this call ended the wake, False when it was
    already ended or not found.
    """
    now = datetime.now(timezone.utc)
    try:
        with db.get_db(config.db_path) as conn:
            with write_transaction(conn):
                wake = (
                    store.get_wake(conn, wake_id) if wake_id is not None
                    else store.get_wake_by_task(conn, task.id) if task is not None
                    else None
                )
                if wake is None or wake.ended_at is not None:
                    return False
                summary = (task.result or "")[:SUMMARY_MAX_CHARS] if task is not None else None
                store.update_wake(
                    conn, wake.id, ended_at=iso_z(now),
                    outcome=_WAKE_OUTCOMES.get(outcome, "failed"),
                    summary=summary or None,
                )
                assignment = store.get_assignment(conn, wake.assignment_id)
                if assignment is not None:
                    due = now if outcome == "not_active" else now + timedelta(
                        minutes=assignment.cadence_minutes,
                    )
                    lifecycle.wake_ended(
                        conn, assignment.id, wake_id=wake.id, next_wake_at=iso_z(due),
                    )
                return True
    except Exception:
        logger.exception("could not finish assignment wake %s (task %s)",
                         wake_id, getattr(task, "id", None))
        _record_finish_failed(config, wake_id=wake_id, task=task)
        return False


def _record_finish_failed(config: "Config", *, wake_id: int | None, task: db.Task | None) -> None:
    """End the wake as failed and leave a `finish_failed` event, so the reaper
    does not retry a broken finish every tick. Best-effort."""
    try:
        with db.get_db(config.db_path) as conn:
            wake = (
                store.get_wake(conn, wake_id) if wake_id is not None
                else store.get_wake_by_task(conn, task.id) if task is not None
                else None
            )
            if wake is None or wake.ended_at is not None:
                return
            store.update_wake(conn, wake.id, ended_at=db.iso_utc_now(), outcome="failed")
            store.insert_event(
                conn, wake.assignment_id, kind="finish_failed", actor="system", wake_id=wake.id,
            )
    except Exception:
        logger.exception("could not record the failed finish of assignment wake %s", wake_id)
