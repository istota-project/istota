"""State transitions, and the user's other writes to an assignment.

The only writer of `assignments.state` (through `store.set_state`). Every
function here runs inside `relay.requests.write_transaction`, which takes the
writer lock before it reads, re-reads the assignment by id and owner, checks the
source state, writes, and appends an `assignment_events` row. Two writers can
therefore not both move one assignment: the second sees the first's state and
raises `TransitionError`.

`TransitionError.code` is a CLI error code: `not_found` (the assignment, or an
item or hand-off that is not this assignment's), `bad_state` (with the current
state), `invalid_input` (an empty or overlong note or report), and the
`CharterError` codes passed through from the checks.

Trusted inbox events are written here for what the user did after activation,
so the next wake sees it. A draft has no wakes yet, and its charter is the
whole context of the first one, so a draft's charter edits write no
`charter_change` event.

The approval of an activation is Stage 4's (`activate_from_request`), and the
wake-side transitions are `wakes`'.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any, Callable

from istota import db
from istota.assignments import budget, charter, store
from istota.relay.requests import write_transaction

if TYPE_CHECKING:
    from istota.config import AssignmentsConfig

TERMINAL_STATES = ("completed", "abandoned")
NOTE_MAX_CHARS = 4000


class TransitionError(Exception):
    """A refused write. `code` is the CLI error code; `state` the state found."""

    def __init__(self, code: str, message: str, *, state: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.state = state


def _load(conn: sqlite3.Connection, assignment_id: int, user_id: str) -> store.Assignment:
    assignment = store.get_assignment(conn, assignment_id, user_id=user_id)
    if assignment is None:
        raise TransitionError("not_found", "no such assignment")
    return assignment


def _require(assignment: store.Assignment, allowed: tuple[str, ...], doing: str) -> None:
    if assignment.state not in allowed:
        raise TransitionError(
            "bad_state", f"cannot {doing} an assignment that is {assignment.state}",
            state=assignment.state,
        )


def _require_live(assignment: store.Assignment, doing: str) -> None:
    if assignment.state in TERMINAL_STATES:
        raise TransitionError(
            "bad_state", f"cannot {doing} an assignment that is {assignment.state}",
            state=assignment.state,
        )


def _text(value: object, field: str, limit: int = NOTE_MAX_CHARS) -> str:
    text = str(value or "").strip()
    if not text:
        raise TransitionError("invalid_input", f"{field} is required")
    if len(text) > limit:
        raise TransitionError("invalid_input", f"{field} is longer than {limit} characters")
    return text


def _charter_error(exc: charter.CharterError) -> TransitionError:
    return TransitionError(exc.code, exc.message)


# ---------------------------------------------------------------------------
# Creation and the charter
# ---------------------------------------------------------------------------


def create(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    title: str,
    goal: str,
    autonomy: str,
    budget_fields: dict[str, Any],
    deadline: str | None,
    cadence_minutes: int,
    auto_complete: bool,
    model: str | None,
    effort: str | None,
) -> store.Assignment:
    """Insert a draft from fields `charter` has already validated."""
    with write_transaction(conn):
        slug = charter.unique_slug(conn, user_id, title)
        assignment_id = store.insert_assignment(
            conn, user_id=user_id, slug=slug, title=title, goal=goal, autonomy=autonomy,
            budget=budget_fields, dir_relpath=charter.dir_relpath(slug), deadline=deadline,
            cadence_minutes=cadence_minutes, auto_complete=auto_complete, model=model, effort=effort,
        )
        store.insert_event(conn, assignment_id, kind="created", actor="user", payload={"slug": slug})
        return store.get_assignment(conn, assignment_id)


def edit_charter(
    conn: sqlite3.Connection,
    assignment_id: int,
    *,
    user_id: str,
    mutate: Callable[[store.Assignment], Any],
    event: str,
    event_payload: dict[str, Any] | None = None,
) -> tuple[store.Assignment, Any]:
    """Apply one charter change, and tell the next wake about it.

    `mutate(assignment)` writes the rows on this connection and may raise
    `charter.CharterError` or `TransitionError`, which roll the change back.
    After activation the before and after renders go into a trusted
    `charter_change` inbox event; nothing is written when the render did not
    move. Every change, a draft's included, is the one place a held
    activation request is cancelled (Stage 4).
    """
    with write_transaction(conn):
        assignment = _load(conn, assignment_id, user_id)
        _require_live(assignment, "change the charter of")
        before = charter.render_current(conn, assignment_id)
        try:
            result = mutate(assignment)
        except charter.CharterError as exc:
            raise _charter_error(exc) from None
        after = charter.render_current(conn, assignment_id)
        payload = dict(event_payload or {})
        store.insert_event(conn, assignment_id, kind=event, actor="user", payload=payload)
        if assignment.state != "draft" and before != after:
            store.insert_inbox_event(
                conn, assignment_id, kind="charter_change", trusted=True,
                payload={"before": before, "after": after},
            )
        return store.get_assignment(conn, assignment_id), result


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def pause(conn: sqlite3.Connection, assignment_id: int, *, user_id: str) -> store.Assignment:
    """sleeping, active or waiting → paused (user). A running wake finishes."""
    with write_transaction(conn):
        assignment = _load(conn, assignment_id, user_id)
        _require(assignment, ("sleeping", "active", "waiting"), "pause")
        store.set_state(conn, assignment_id, "paused", reason="user")
        store.update_assignment(conn, assignment_id, next_wake_at=None)
        store.insert_event(
            conn, assignment_id, kind="paused", actor="user", payload={"from": assignment.state},
        )
        return store.get_assignment(conn, assignment_id)


def resume(
    conn: sqlite3.Connection, cfg: "AssignmentsConfig", assignment_id: int, *, user_id: str,
) -> store.Assignment:
    """paused → sleeping, due now. Refused past `max_active_per_user`, or while
    the budget is spent (a top-up is what resumes a budget pause)."""
    with write_transaction(conn):
        assignment = _load(conn, assignment_id, user_id)
        _require(assignment, ("paused",), "resume")
        try:
            budget.check_active_limit(conn, cfg, user_id, assignment_id)
        except charter.CharterError as exc:
            raise _charter_error(exc) from None
        if budget.is_spent(assignment, budget.totals(conn, assignment_id)):
            raise TransitionError(
                "bad_state", "the budget is spent; top it up to resume", state=assignment.state,
            )
        store.set_state(conn, assignment_id, "sleeping")
        store.update_assignment(conn, assignment_id, next_wake_at=db.iso_utc_now())
        store.insert_event(
            conn, assignment_id, kind="resumed", actor="user",
            payload={"reason": assignment.state_reason},
        )
        return store.get_assignment(conn, assignment_id)


def abandon(
    conn: sqlite3.Connection, assignment_id: int, *, user_id: str, reason: str | None = None,
) -> store.Assignment:
    """Any non-terminal state → abandoned. Always allowed from a user turn."""
    clean = " ".join(str(reason or "").split())[:NOTE_MAX_CHARS] or None
    with write_transaction(conn):
        assignment = _load(conn, assignment_id, user_id)
        _require_live(assignment, "abandon")
        store.set_state(conn, assignment_id, "abandoned", reason=clean)
        store.update_assignment(conn, assignment_id, next_wake_at=None, closed_at=db.iso_utc_now())
        store.insert_event(
            conn, assignment_id, kind="abandoned", actor="user",
            payload={"from": assignment.state, "reason": clean},
        )
        return store.get_assignment(conn, assignment_id)


def request_wake(conn: sqlite3.Connection, assignment_id: int, *, user_id: str) -> store.Assignment:
    """Make a sleeping or waiting-on-the-user assignment due now.

    The wake itself is created by the scheduler's gate, so there is one
    creator. A wait on a proposed completion is answered with `close`, not a wake.
    """
    with write_transaction(conn):
        assignment = _load(conn, assignment_id, user_id)
        _require(assignment, ("sleeping", "waiting"), "wake")
        if assignment.state == "waiting" and "completion" in assignment.waiting_on:
            raise TransitionError(
                "bad_state",
                "this assignment proposed completion; answer it with close --completed or --reject",
                state=assignment.state,
            )
        store.update_assignment(conn, assignment_id, next_wake_at=db.iso_utc_now())
        store.insert_inbox_event(
            conn, assignment_id, kind="user_note", trusted=True,
            payload={"text": "wake requested by the user", "wake_requested": True},
        )
        store.insert_event(conn, assignment_id, kind="wake_requested", actor="user")
        return store.get_assignment(conn, assignment_id)


def topup(
    conn: sqlite3.Connection,
    cfg: "AssignmentsConfig",
    assignment_id: int,
    *,
    user_id: str,
    usd: float | None = None,
    wakes: int | None = None,
) -> store.Assignment:
    """Raise the cost or wake cap. A budget pause that is no longer spent → sleeping, due now."""

    def mutate(assignment: store.Assignment) -> None:
        new_budget = budget.topped_up(cfg, assignment.budget, usd=usd, wakes=wakes)
        store.update_assignment(conn, assignment_id, budget=new_budget, budget_low_sent=False)

    with write_transaction(conn):
        assignment, _ = edit_charter(
            conn, assignment_id, user_id=user_id, mutate=mutate, event="topped_up",
            event_payload={"usd": usd, "wakes": wakes},
        )
        if (assignment.state == "paused" and assignment.state_reason == "budget"
                and not budget.is_spent(assignment, budget.totals(conn, assignment_id))):
            store.set_state(conn, assignment_id, "sleeping")
            store.update_assignment(conn, assignment_id, next_wake_at=db.iso_utc_now())
            store.insert_event(
                conn, assignment_id, kind="resumed", actor="user", payload={"reason": "budget"},
            )
        return store.get_assignment(conn, assignment_id)


def close(
    conn: sqlite3.Connection,
    assignment_id: int,
    *,
    user_id: str,
    completed: bool,
    missing: str | None = None,
) -> store.Assignment:
    """Answer a proposed completion: confirm it, or send it back with what is missing.

    Confirming marks every open confirmed item confirmed and → completed.
    Rejecting writes a trusted `completion_rejected` event and → sleeping, due now.
    """
    with write_transaction(conn):
        assignment = _load(conn, assignment_id, user_id)
        if assignment.state != "waiting" or "completion" not in assignment.waiting_on:
            raise TransitionError(
                "bad_state", "this assignment has not proposed completion", state=assignment.state,
            )
        now = db.iso_utc_now()
        if completed:
            for item in store.list_items(conn, assignment_id):
                if item.kind == "confirmed" and item.status != "confirmed":
                    store.set_item_status(conn, item.id, "confirmed", detail="confirmed at close", checked_at=now)
            store.set_state(conn, assignment_id, "completed", reason="user")
            store.update_assignment(conn, assignment_id, next_wake_at=None, closed_at=now)
            store.insert_event(conn, assignment_id, kind="completed", actor="user")
        else:
            text = _text(missing, "what is missing")
            store.insert_inbox_event(
                conn, assignment_id, kind="completion_rejected", trusted=True, payload={"missing": text},
            )
            store.set_state(conn, assignment_id, "sleeping")
            store.update_assignment(conn, assignment_id, next_wake_at=now)
            store.insert_event(conn, assignment_id, kind="completion_rejected", actor="user")
        return store.get_assignment(conn, assignment_id)


# ---------------------------------------------------------------------------
# The user's other inputs
# ---------------------------------------------------------------------------


def confirm_item(
    conn: sqlite3.Connection, assignment_id: int, *, user_id: str, item_id: int,
) -> store.Item:
    """Mark a confirmed item confirmed. Mechanical items are the validators' alone."""
    with write_transaction(conn):
        assignment = _load(conn, assignment_id, user_id)
        _require_live(assignment, "confirm an item of")
        item = store.get_item(conn, assignment_id, item_id)
        if item is None:
            raise TransitionError("not_found", "no such item on this assignment")
        if item.kind != "confirmed":
            raise TransitionError(
                "bad_state", "a mechanical item passes only when its check does", state=assignment.state,
            )
        if item.status == "confirmed":
            return item
        store.set_item_status(conn, item.id, "confirmed", detail="confirmed by the user", checked_at=db.iso_utc_now())
        store.insert_event(conn, assignment_id, kind="item_confirmed", actor="user", payload={"item_id": item.id})
        if assignment.state != "draft":
            store.insert_inbox_event(
                conn, assignment_id, kind="item_confirmed", trusted=True,
                payload={"item_id": item.id, "text": item.text},
            )
        return store.get_item(conn, assignment_id, item_id)


def add_note(conn: sqlite3.Connection, assignment_id: int, *, user_id: str, text: object) -> int:
    """A trusted `user_note` for the next wake. Returns the inbox event id."""
    clean = _text(text, "the note")
    with write_transaction(conn):
        assignment = _load(conn, assignment_id, user_id)
        _require_live(assignment, "leave a note on")
        event_id = store.insert_inbox_event(
            conn, assignment_id, kind="user_note", trusted=True, payload={"text": clean},
        )
        store.insert_event(conn, assignment_id, kind="note", actor="user", payload={"inbox_id": event_id})
        return event_id


def complete_handoff(
    conn: sqlite3.Connection, assignment_id: int, *, user_id: str, action_id: int, report: object,
) -> store.Action:
    """Mark an open hand-off done with the user's report, for the next wake."""
    clean = _text(report, "the report")
    with write_transaction(conn):
        assignment = _load(conn, assignment_id, user_id)
        _require_live(assignment, "report a hand-off on")
        action = store.get_action(conn, assignment_id, action_id)
        if action is None:
            raise TransitionError("not_found", "no such hand-off on this assignment")
        if not store.resolve_action(conn, assignment_id, action_id, response=clean):
            raise TransitionError("bad_state", "that hand-off is already done", state=assignment.state)
        store.insert_inbox_event(
            conn, assignment_id, kind="handoff_done", trusted=True,
            payload={"action_id": action_id, "step": action.text, "report": clean},
        )
        store.insert_event(conn, assignment_id, kind="handoff_done", actor="user", payload={"action_id": action_id})
        return store.get_action(conn, assignment_id, action_id)
