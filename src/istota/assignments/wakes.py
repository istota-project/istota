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

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from istota import db
from istota.assignments import budget, charter, lifecycle, scratchpad, store, validators
from istota.assignments import prompt as assignment_prompt
from istota.assignments.charter import iso_z
from istota.notifications.resolvers import assignment as assignment_notices
from istota.relay.requests import write_transaction

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger(__name__)

FEATURE_FLAG = "skill_assignment"
SOURCE_TYPE = "assignment"
QUEUE = "assignment"
#: The task's stored prompt. The real prompt is built at pickup.
WAKE_PROMPT = "[assignment wake]"
#: How a wake turned away at pickup ends its task.
NOT_ACTIVE_ERROR = "assignment_not_active"
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
    raised: list,
) -> CreatedWake | None:
    """Decide one assignment and wake it. A budget pause's bell row goes into
    `raised`, for the caller to deliver once this transaction has committed."""
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
            raised.extend(assignment_notices.write_notices(conn, assignment_id, [("paused", "budget")]))
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
    from istota.notifications.store import deliver_pending

    created: list[CreatedWake] = []
    for assignment_id in candidates:
        raised: list = []
        try:
            made = _dispatch_one(conn, config, assignment_id, moment, raised)
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("assignment %d: could not create its wake; skipped", assignment_id)
            continue
        deliver_pending(config, raised)
        if made is not None:
            created.append(made)
    return created


# ---------------------------------------------------------------------------
# Picking a wake up
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WakeStart:
    """What `execute_task` runs a wake with."""

    assignment: store.Assignment
    wake: store.Wake
    prompt: assignment_prompt.WakePrompt
    #: The brain's timeout: `per_wake_minutes`, never past `task_timeout_minutes`.
    timeout_seconds: int
    #: `per_wake_turns`; the native brain applies its own cap below this.
    max_turns: int


def wake_limits(config: "Config", assignment: store.Assignment) -> tuple[int, int]:
    """`(timeout_seconds, max_turns)` from the charter's per-wake budget."""
    minutes = int(assignment.budget.get("per_wake_minutes") or charter.DEFAULT_PER_WAKE_MINUTES)
    minutes = max(1, min(minutes, config.scheduler.task_timeout_minutes))
    turns = int(assignment.budget.get("per_wake_turns") or charter.DEFAULT_PER_WAKE_TURNS)
    return minutes * 60, max(1, turns)


def _pickup_target(
    conn: sqlite3.Connection, config: "Config", task: db.Task,
) -> tuple[store.Assignment, store.Wake] | None:
    """The assignment and open wake this task runs, or None when it must not run."""
    if not feature_enabled(config) or task.assignment_id is None:
        return None
    assignment = store.get_assignment(conn, task.assignment_id, user_id=task.user_id)
    wake = store.get_wake_by_task(conn, task.id)
    if assignment is None or wake is None or wake.assignment_id != assignment.id:
        return None
    if assignment.state != "active" or wake.ended_at is not None:
        return None
    return assignment, wake


def start_wake(config: "Config", task: db.Task, *, dry_run: bool = False) -> WakeStart | None:
    """Pick a wake up (spec Design section 7): check, snapshot, claim, build.

    None means the wake must not run: the assignment is not `active`, the flag
    is off, or the task names no open wake of the user's. The wake is then
    finished as `not_active` here, so it is not counted as a failure, and the
    caller ends the task with `NOT_ACTIVE_ERROR`.

    The scratchpad's `before` snapshot, the user-edit event and the inbox claim
    are written once, in one transaction; a re-run of a wake already started
    builds the prompt from what it claimed then. A dry run writes nothing and
    renders the inbox as it stands.
    """
    now = datetime.now(timezone.utc)
    with db.get_db(config.db_path) as conn:
        if dry_run:
            target = _pickup_target(conn, config, task)
            if target is None:
                return None
            assignment, wake = target
            state = scratchpad.read_file(config, assignment)
            text = state.text if state.text is not None else scratchpad.restore_text(conn, assignment.id)
            inbox = [
                e for e in store.list_inbox(conn, assignment.id)
                if e.consumed_by_wake in (None, wake.id)
            ]
        else:
            with write_transaction(conn):
                target = _pickup_target(conn, config, task)
                if target is not None:
                    assignment, wake = target
                    if wake.started_at is None:
                        text = scratchpad.begin(config, conn, assignment, wake).text
                        store.consume_inbox(conn, assignment.id, wake.id)
                        store.update_wake(conn, wake.id, started_at=iso_z(now))
                        # The deliverables as the wake found them, for
                        # finish_wake's `deliverable_changed` signal.
                        digests = validators.deliverable_digests(
                            config, assignment, store.list_items(conn, assignment.id),
                        )
                        store.insert_event(
                            conn, assignment.id, kind="wake_started", actor="system",
                            wake_id=wake.id, payload={"task_id": task.id, "deliverables": digests},
                        )
                    else:
                        before = store.wake_snapshot(conn, wake.id, "before")
                        text = before.content if before is not None else scratchpad.restore_text(
                            conn, assignment.id,
                        )
            conn.commit()
            if target is None:
                logger.info("assignment wake task %s turned away: not active", task.id)
                finish_wake(config, task, "not_active")
                return None
            wake = store.get_wake(conn, wake.id)
            inbox = store.list_inbox(conn, assignment.id, consumed_by_wake=wake.id)

        built = assignment_prompt.build(
            conn, config, assignment, wake, scratchpad_text=text, inbox=inbox, now=now,
        )
    timeout_seconds, max_turns = wake_limits(config, assignment)
    return WakeStart(
        assignment=assignment, wake=wake, prompt=built,
        timeout_seconds=timeout_seconds, max_turns=max_turns,
    )


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


#: Consumed inbox kinds that count as the user's input (spec Design section 11).
USER_INPUT_KINDS = ("user_note", "handoff_done", "charter_change", "user_edit")
#: Wake outcomes that are not a run of the wake at all, or that the user
#: stopped: neither counts toward a stall or the failure pause.
_UNCOUNTED_OUTCOMES = ("not_active", "cancelled")


@dataclass(frozen=True)
class Finished:
    """What `finish_wake` decided for one wake."""

    wake_id: int
    assignment_id: int
    outcome: str
    signals: list[str]
    #: The assignment's state once the wake ended.
    state: str | None
    #: `(kind, ref)` for every notice the user must see: `handoff` (action id),
    #: `report` (event id), `budget_low`, `paused` (reason), `completion`,
    #: `closed`. Recorded on the `wake_finished` event and written to the bell
    #: on the same transaction.
    notices: list[tuple[str, object]]
    #: The bell rows those notices wrote, for `deliver_pending` after commit.
    raised: tuple = ()


def finish_wake(
    config: "Config", task: db.Task | None, outcome: str, *, wake_id: int | None = None,
) -> bool:
    """End one wake, in its own transaction. Never raises.

    `outcome` is how the task ended: `completed`, `failed`, `cancelled`, or
    `not_active` for a wake turned away at pickup. The wake is named by
    `wake_id`, or found from `task`. Runs the spec's sequence (Design section
    7, "After the task ends") in `_finish`. Returns True when this call ended
    the wake, False when it was already ended, not found, or the finish failed;
    a failed finish ends the wake `failed` and pauses the assignment
    (`failures`), so it does not wake again on a broken path.
    """
    from istota.notifications.store import deliver_pending

    now = datetime.now(timezone.utc)
    try:
        with db.get_db(config.db_path) as conn:
            with write_transaction(conn):
                wake = _wake_for(conn, task, wake_id)
                if wake is None or wake.ended_at is not None:
                    return False
                finished = _finish(conn, config, wake, task, outcome, now)
        deliver_pending(config, finished.raised)
        return True
    except Exception:
        logger.exception("could not finish assignment wake %s (task %s)",
                         wake_id, getattr(task, "id", None))
        _record_finish_failed(config, wake_id=wake_id, task=task)
        return False


def _wake_for(conn: sqlite3.Connection, task: db.Task | None, wake_id: int | None) -> store.Wake | None:
    if wake_id is not None:
        return store.get_wake(conn, wake_id)
    if task is not None:
        return store.get_wake_by_task(conn, task.id)
    return None


def _summary(task: db.Task | None) -> str | None:
    """The task's answer, or for a failed one its error, cut to the cap."""
    if task is None:
        return None
    text = task.result or task.error or ""
    return text[:SUMMARY_MAX_CHARS] or None


def _finish(
    conn: sqlite3.Connection,
    config: "Config",
    wake: store.Wake,
    task: db.Task | None,
    outcome: str,
    now: datetime,
) -> Finished:
    """The finishing sequence, in the spec's order. Runs inside the caller's transaction."""
    assignment = store.get_assignment(conn, wake.assignment_id)
    if assignment is None or outcome == "not_active":
        store.update_wake(conn, wake.id, ended_at=iso_z(now), outcome=_WAKE_OUTCOMES.get(outcome, "failed"),
                          summary=_summary(task) if outcome != "not_active" else None)
        if assignment is not None:
            lifecycle.wake_ended(conn, assignment.id, wake_id=wake.id, next_wake_at=iso_z(now))
        return Finished(wake.id, wake.assignment_id, _WAKE_OUTCOMES.get(outcome, "failed"), [],
                        None, [])

    cfg = config.assignments
    task_outcome = outcome if outcome in _TASK_OUTCOMES else "failed"

    # 1. The scratchpad. A wake that never started has no `before` snapshot,
    # and an `after` taken now would swallow the user's edit as a baseline.
    if wake.started_at is not None:
        scratchpad.end(config, conn, assignment, wake)

    # 2. Validators.
    newly_passing = _run_validators(conn, config, assignment, wake, now)
    items = store.list_items(conn, assignment.id)

    # 3. Progress signals.
    signals = _progress_signals(conn, config, assignment, wake, items, newly_passing)

    # 4. Attempts: the thrash check.
    thrash_target = _thrash_target(conn, assignment.id, wake.id)

    # 5. The ledger, budget_low and the spent check.
    notices: list[tuple[str, object]] = []
    spent = budget.totals(conn, assignment.id)
    if _raise_budget_low(conn, cfg, assignment, spent, wake.id):
        notices.append(("budget_low", None))
    budget_spent = budget.is_spent(assignment, spent)

    # 6 and 7. Completion first, then the pauses, then `next` or the default.
    completion = None
    if wake.complete_request:
        completion = _decide_completion(conn, assignment, wake, items, task_outcome)
    pause = None
    if completion not in ("completed", "proposed"):
        pause = _decide_pause(
            conn, cfg, assignment, wake, task, task_outcome, signals, thrash_target, budget_spent,
        )

    if completion == "completed":
        wake_outcome = "completed"
    elif completion == "proposed":
        wake_outcome = "complete_proposed"
    elif task_outcome != "completed":
        wake_outcome = task_outcome
    elif completion == "refused":
        wake_outcome = "complete_refused"
    else:
        wake_outcome = "next" if wake.next_request else "no_next_step"

    # 8. End the wake row.
    store.update_wake(
        conn, wake.id, ended_at=iso_z(now), outcome=wake_outcome, summary=_summary(task),
        progress_signals=signals, cost_usd=_wake_cost(conn, assignment.id, wake.task_id),
    )

    if completion == "completed":
        if lifecycle.complete_after_wake(conn, assignment.id, wake_id=wake.id):
            notices.append(("closed", None))
    elif completion == "proposed":
        if lifecycle.wake_waits(conn, assignment.id, wake_id=wake.id, waiting_on=["completion"]):
            notices.append(("completion", None))
    elif pause is not None:
        reason, detail = pause
        if lifecycle.pause_after_wake(
            conn, assignment.id, wake_id=wake.id, reason=reason, detail=detail,
        ):
            notices.append(("paused", reason))
    else:
        _apply_next(conn, config, assignment, wake, now)

    # 9. Notices. What this wake handed off or reported is listed with them.
    for event in store.list_events(conn, assignment.id):
        if event.wake_id != wake.id or event.actor != "assignment":
            continue
        if event.kind == "handoff_created":
            notices.append(("handoff", (event.payload or {}).get("action_id")))
        elif event.kind == "report":
            notices.append(("report", event.id))

    after = store.get_assignment(conn, assignment.id)
    state = after.state if after is not None else None
    store.insert_event(
        conn, assignment.id, kind="wake_finished", actor="system", wake_id=wake.id,
        payload={
            "task_outcome": task_outcome, "outcome": wake_outcome, "signals": signals,
            "state": state, "notices": [list(n) for n in notices],
        },
    )
    raised = assignment_notices.write_notices(conn, assignment.id, notices)
    assignment_notices.settle(conn, assignment.id)
    return Finished(wake.id, assignment.id, wake_outcome, signals, state, notices, tuple(raised))


def _run_validators(
    conn: sqlite3.Connection, config: "Config", assignment: store.Assignment, wake: store.Wake,
    now: datetime,
) -> list[int]:
    """Run every mechanical item and record it. Returns the ids that moved to `passing`."""
    checked_at = iso_z(now)
    newly_passing: list[int] = []
    for item in store.list_items(conn, assignment.id):
        if item.kind != "mechanical":
            continue
        result = validators.run_item(config, assignment, item)
        store.set_item_status(conn, item.id, result.status, detail=result.detail, checked_at=checked_at)
        if result.status != item.status:
            store.insert_event(
                conn, assignment.id, kind="item_status", actor="system", wake_id=wake.id,
                payload={"item_id": item.id, "from": item.status, "to": result.status,
                         "detail": result.detail},
            )
        if result.status == "passing" and item.status != "passing":
            newly_passing.append(item.id)
    return newly_passing


def _progress_signals(
    conn: sqlite3.Connection,
    config: "Config",
    assignment: store.Assignment,
    wake: store.Wake,
    items: list[store.Item],
    newly_passing: list[int],
) -> list[str]:
    """The host-visible facts that say the wake moved the assignment (section 11).

    Scratchpad edits and `--progress` never count.
    """
    signals: list[str] = []
    if newly_passing:
        signals.append("item_passing")
    consumed = {event.kind for event in store.list_inbox(conn, assignment.id, consumed_by_wake=wake.id)}
    if "item_confirmed" in consumed:
        signals.append("item_confirmed")
    if consumed & set(USER_INPUT_KINDS):
        signals.append("user_input")
    if "handoff_done" in consumed:
        signals.append("handoff_done")
    new_pair = conn.execute(
        """
        SELECT 1 FROM assignment_attempts a
        WHERE a.assignment_id = ? AND a.wake_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM assignment_attempts b
              WHERE b.assignment_id = a.assignment_id AND b.target = a.target
                AND b.outcome = a.outcome AND b.id < a.id
          )
        LIMIT 1
        """,
        (assignment.id, wake.id),
    ).fetchone()
    if new_pair is not None:
        signals.append("attempt_new_outcome")
    if _deliverable_changed(conn, config, assignment, wake, items):
        signals.append("deliverable_changed")
    return signals


def _deliverable_changed(
    conn: sqlite3.Connection,
    config: "Config",
    assignment: store.Assignment,
    wake: store.Wake,
    items: list[store.Item],
) -> bool:
    """A file a mechanical item names changed during the wake, and every item
    checking that file now passes. Compared with the digests pickup recorded."""
    row = conn.execute(
        "SELECT payload FROM assignment_events WHERE wake_id = ? AND kind = 'wake_started'"
        " ORDER BY id LIMIT 1",
        (wake.id,),
    ).fetchone()
    if row is None:
        return False
    try:
        before = json.loads(row[0]).get("deliverables")
    except (TypeError, ValueError, AttributeError):
        return False
    if not isinstance(before, dict):
        return False
    after = validators.deliverable_digests(config, assignment, items)
    for file, digest in after.items():
        if digest is None or digest == before.get(file):
            continue
        checking = [item for item in items if validators.item_file(item) == file]
        if checking and all(item.status == "passing" for item in checking):
            return True
    return False


def _thrash_target(conn: sqlite3.Connection, assignment_id: int, wake_id: int) -> str | None:
    """A target this wake failed on again after `THRASH_REPEATS` failures in a row."""
    repeats = assignment_prompt.THRASH_REPEATS
    targets = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT target FROM assignment_attempts WHERE assignment_id = ? AND wake_id = ?"
            " ORDER BY target",
            (assignment_id, wake_id),
        ).fetchall()
    ]
    for target in targets:
        last = store.list_attempts(conn, assignment_id, target=target, limit=repeats + 1)
        if (len(last) == repeats + 1 and last[0].wake_id == wake_id
                and all(attempt.outcome == "failed" for attempt in last)):
            return target
    return None


def _wake_cost(conn: sqlite3.Connection, assignment_id: int, task_id: int | None) -> float | None:
    """What this wake's task spent, copied onto the wake row for the timeline."""
    if task_id is None:
        return None
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0.0) FROM task_usage"
        " WHERE assignment_id = ? AND task_id = ?",
        (assignment_id, task_id),
    ).fetchone()
    return float(row[1]) if row[0] else None


def _raise_budget_low(
    conn: sqlite3.Connection, cfg, assignment: store.Assignment, spent: budget.Totals, wake_id: int,
) -> bool:
    """The first crossing of `budget_low_fraction` of cost or wakes: a trusted
    `budget_low` inbox event for the next wake, once until a top-up."""
    if assignment.budget_low_sent or assignment.state in lifecycle.TERMINAL_STATES:
        return False
    max_cost = float(assignment.budget.get("max_cost_usd") or 0.0)
    max_wakes = int(assignment.budget.get("max_wakes") or 0)
    fraction = cfg.budget_low_fraction
    crossed = (
        (max_cost > 0 and spent.cost_usd >= fraction * max_cost)
        or (max_wakes > 0 and spent.wakes >= fraction * max_wakes)
    )
    if not crossed:
        return False
    payload = {
        "cost_usd": round(spent.cost_usd, 4), "max_cost_usd": max_cost,
        "wakes": spent.wakes, "max_wakes": max_wakes,
    }
    store.insert_inbox_event(conn, assignment.id, kind="budget_low", trusted=True, payload=payload)
    store.update_assignment(conn, assignment.id, budget_low_sent=True)
    store.insert_event(
        conn, assignment.id, kind="budget_low", actor="system", wake_id=wake_id, payload=payload,
    )
    return True


def _decide_completion(
    conn: sqlite3.Connection,
    assignment: store.Assignment,
    wake: store.Wake,
    items: list[store.Item],
    task_outcome: str,
) -> str | None:
    """`completed`, `proposed`, `refused`, or None when the request is not
    weighed: a wake whose task failed or was stopped may have left its files
    half-written, so its completion is recorded as ignored."""
    if task_outcome != "completed":
        store.insert_event(
            conn, assignment.id, kind="complete_ignored", actor="system", wake_id=wake.id,
            payload={"task_outcome": task_outcome},
        )
        return None
    failing = [item.id for item in items if item.kind == "mechanical" and item.status != "passing"]
    if failing:
        store.insert_event(
            conn, assignment.id, kind="complete_refused", actor="system", wake_id=wake.id,
            payload={"failing_items": failing},
        )
        return "refused"
    open_confirmed = [item for item in items if item.kind == "confirmed" and item.status != "confirmed"]
    if assignment.auto_complete and not open_confirmed:
        return "completed"
    return "proposed"


def _counted_wakes(conn: sqlite3.Connection, assignment_id: int, wake_id: int, limit: int) -> list[store.Wake]:
    """The newest `limit` ended wakes before this one that count toward a pause."""
    if limit <= 0:
        return []
    placeholders = ", ".join("?" for _ in _UNCOUNTED_OUTCOMES)
    rows = conn.execute(
        f"SELECT id FROM assignment_wakes WHERE assignment_id = ? AND id != ?"
        f" AND ended_at IS NOT NULL AND COALESCE(outcome, '') NOT IN ({placeholders})"
        f" ORDER BY seq DESC LIMIT ?",
        (assignment_id, wake_id, *_UNCOUNTED_OUTCOMES, limit),
    ).fetchall()
    return [store.get_wake(conn, row[0]) for row in rows]


def _decide_pause(
    conn: sqlite3.Connection,
    cfg,
    assignment: store.Assignment,
    wake: store.Wake,
    task: db.Task | None,
    task_outcome: str,
    signals: list[str],
    thrash_target: str | None,
    budget_spent: bool,
) -> tuple[str, dict] | None:
    """The pause this wake ends in, if any, as `(reason, detail)`. A pause
    overrides the wake's `next`."""
    if budget_spent:
        return "budget", {}
    if thrash_target is not None:
        return "thrash", {"target": thrash_target}
    if task_outcome == "failed" and cfg.failure_wakes > 0:
        earlier = _counted_wakes(conn, assignment.id, wake.id, cfg.failure_wakes - 1)
        if len(earlier) == cfg.failure_wakes - 1 and all(w.outcome == "failed" for w in earlier):
            error = (task.error or "") if task is not None else "the wake's task is gone"
            return "failures", {"error": error[:500]}
    if task_outcome != "cancelled" and not signals and cfg.stall_wakes > 0:
        earlier = _counted_wakes(conn, assignment.id, wake.id, cfg.stall_wakes - 1)
        if len(earlier) == cfg.stall_wakes - 1 and all(not w.progress_signals for w in earlier):
            return "stalled", {"wake_ids": [wake.id, *[w.id for w in earlier]]}
    if wake.fired_by == "deadline":
        return "deadline", {}
    return None


def _apply_next(
    conn: sqlite3.Connection, config: "Config", assignment: store.Assignment, wake: store.Wake,
    now: datetime,
) -> None:
    """The wake's recorded `next`, or the default sleep for `cadence_minutes`.

    A time was clamped when the wake asked; it is clamped again from now, the
    wake's end, so the floor between wakes holds. A deadline still ahead caps it.
    """
    request = wake.next_request or {}
    if request.get("mode") == "wait":
        lifecycle.wake_waits(conn, assignment.id, wake_id=wake.id, waiting_on=[request.get("wait") or "user"])
        return
    wake_at = _parse(request.get("wake_at")) if request.get("mode") in ("in", "at") else None
    if wake_at is None:
        wake_at = now + timedelta(minutes=assignment.cadence_minutes)
    floor = now + timedelta(minutes=config.assignments.min_wake_interval_minutes)
    if wake_at < floor:
        wake_at = floor
    deadline = _parse(assignment.deadline)
    if deadline is not None and now < deadline < wake_at:
        wake_at = deadline
    lifecycle.wake_ended(conn, assignment.id, wake_id=wake.id, next_wake_at=iso_z(wake_at))


def _record_finish_failed(config: "Config", *, wake_id: int | None, task: db.Task | None) -> None:
    """End the wake as failed, leave a `finish_failed` event and pause the
    assignment (`failures`), so neither the reaper nor the gate retries a
    broken finish every tick. Best-effort."""
    from istota.notifications.store import deliver_pending

    raised: list = []
    try:
        with db.get_db(config.db_path) as conn:
            with write_transaction(conn):
                wake = _wake_for(conn, task, wake_id)
                if wake is None or wake.ended_at is not None:
                    return
                store.update_wake(conn, wake.id, ended_at=db.iso_utc_now(), outcome="failed")
                store.insert_event(
                    conn, wake.assignment_id, kind="finish_failed", actor="system", wake_id=wake.id,
                )
                if lifecycle.pause_after_wake(
                    conn, wake.assignment_id, wake_id=wake.id, reason="failures",
                    detail={"finish_failed": True},
                    from_states=("active", "sleeping", "waiting"),
                ):
                    raised = assignment_notices.write_notices(
                        conn, wake.assignment_id, [("paused", "failures")],
                    )
        deliver_pending(config, raised)
    except Exception:
        logger.exception("could not record the failed finish of assignment wake %s", wake_id)
