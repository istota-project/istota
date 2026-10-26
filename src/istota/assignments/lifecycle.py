"""State transitions, and the user's other writes to an assignment.

The only writer of `assignments.state` (through `store.set_state`). Every
function here runs inside `relay.requests.write_transaction`, which takes the
writer lock before it reads, re-reads the assignment by id and owner, checks the
source state, writes, and appends an `assignment_events` row. Two writers can
therefore not both move one assignment: the second sees the first's state and
raises `TransitionError`.

`TransitionError.code` is a CLI error code: `not_found` (the assignment, or an
item or hand-off that is not this assignment's), `bad_state` (with the current
state), `invalid_input` (an empty or overlong note or report),
`deadline_passed` and `confirmation_pending` (from `hold_activation`),
`not_a_wake` (a wake's write on a wake that is not this task's or has ended),
`not_met` and `items_changed` (from `close --completed` with no proposal), and
the `CharterError` codes passed through from the checks.

**The user's own "done" needs no wake** (ISSUE-853). Confirming the last open
confirmed item, or `close --completed` with no proposal, completes the
assignment when every mechanical item passes. The checks run before the
transaction opens (no file I/O under the writer lock), and the transaction
applies them only to the same done-when list (`validators.items_key`).

Trusted inbox events are written here for what the user did after activation,
so the next wake sees it. A draft has no wakes yet, and its charter is the
whole context of the first one, so a draft's charter edits write no
`charter_change` event.

**Activation is the one approval.** `hold_activation` stores the rendered
charter as an `assignment_activation` request in the relay request ledger, the
task parks on it through the scheduler's authorization branch, and
`relay.requests.approve_request` calls `activate_from_request` when the user
approves that exact preview. The request's `destination` carries the
`charter_digest` taken at hold time; approval recomputes it, and any charter
change while the request is held cancels it (`cancel_held_activation`, from
`edit_charter`), so an approval can never start a charter other than the one
shown. There is no clean-turn release.

The system's transitions around a wake (`wake_created`, `pause_for_budget`,
`wake_ended`, `wake_waits`, `pause_after_wake`, `complete_after_wake`) are here too, so this module stays the one writer of the state;
`wakes` decides when they happen and calls them inside its own transaction.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from istota import db
from istota.assignments import budget, charter, store, validators
from istota.notifications.resolvers import assignment as notices
from istota.relay.requests import write_transaction

if TYPE_CHECKING:
    from istota.config import AssignmentsConfig, Config

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
    by_task_id: int | None = None,
) -> tuple[store.Assignment, Any]:
    """Apply one charter change, and tell the next wake about it.

    `mutate(assignment)` writes the rows on this connection and may raise
    `charter.CharterError` or `TransitionError`, which roll the change back.
    After activation the before and after renders go into a trusted
    `charter_change` inbox event; nothing is written when the render did not
    move. Every change, a draft's included, is the one place a held
    activation request is cancelled (Stage 4). `by_task_id` is the turn
    making the change, whose own hold is closed without stopping it.
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
        cancel_held_activation(conn, assignment_id, reason="charter_changed", by_task_id=by_task_id)
        payload = dict(event_payload or {})
        store.insert_event(conn, assignment_id, kind=event, actor="user", payload=payload)
        if assignment.state != "draft" and before != after:
            store.insert_inbox_event(
                conn, assignment_id, kind="charter_change", trusted=True,
                payload={"before": before, "after": after},
            )
        return store.get_assignment(conn, assignment_id), result


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

ACTIVATION_KIND = "assignment_activation"
ACTIVATION_PREAMBLE = (
    "Start this assignment? Once approved it runs on its own, wake after wake, "
    "and spends up to the budget below."
)

_ACTIVATIONS_SQL = (
    "SELECT * FROM whatsapp_skill_requests WHERE kind = 'assignment_activation'"
    " AND json_extract(destination, '$.assignment_id') = ?"
)


def _deadline_passed(assignment: store.Assignment) -> bool:
    return bool(assignment.deadline) and assignment.deadline <= db.iso_utc_now()


def hold_activation(
    conn: sqlite3.Connection,
    cfg: "AssignmentsConfig",
    assignment_id: int,
    *,
    user_id: str,
    task_id: int,
) -> dict[str, Any]:
    """Hold the rendered charter for the user's approval. Returns the request row.

    A call from a task that already holds or had approved this assignment's
    activation returns that request, so the confirmed re-run calling
    `activate` again is told it is done. Raises `TransitionError` for the
    assignment's own refusals and `relay.requests.RequestError` for the
    ledger's (`confirmation_pending`, `task_unavailable`).
    """
    from istota.relay.requests import _store_request

    with write_transaction(conn):
        assignment = _load(conn, assignment_id, user_id)
        mine = conn.execute(
            _ACTIVATIONS_SQL + " AND requester_user_id = ? AND origin_task_id = ? ORDER BY rowid DESC",
            (assignment_id, user_id, task_id),
        ).fetchall()
        if mine and mine[0]["state"] in ("held", "approved"):
            return dict(mine[0])
        _require(assignment, ("draft",), "activate")
        if _deadline_passed(assignment):
            raise TransitionError("deadline_passed", "the deadline has passed; set a new one first")
        try:
            budget.check_active_limit(conn, cfg, user_id, assignment_id)
        except charter.CharterError as exc:
            raise _charter_error(exc) from None
        if conn.execute(_ACTIVATIONS_SQL + " AND state = 'held'", (assignment_id,)).fetchone():
            raise TransitionError(
                "confirmation_pending",
                "an activation of this assignment is already waiting for approval; "
                "approve or decline that one",
            )
        task = db.get_task(conn, task_id)
        if task is None:
            raise TransitionError("not_a_user_turn", "the calling task is gone")
        rendered = charter.render_current(conn, assignment_id)
        digest = charter.charter_digest(conn, assignment_id)
        preview = f"{ACTIVATION_PREAMBLE}\n\n{rendered}"
        summary = f"Activate assignment {assignment_id}"
        row = _store_request(
            conn, actor_user_id=user_id, task_id=task_id,
            # Numbered per task, so a turn whose earlier hold was cancelled by a
            # charter change can hold the new charter under a fresh key.
            request_key=f"assignment-{assignment_id}-{len(mine) + 1}",
            kind=ACTIVATION_KIND, recipient_user_id=user_id, text=summary,
            service_body=summary, template_body=None, provider="assignment",
            binding_fingerprint=digest, preview=preview,
            origin={"surface": task.source_type, "room_token": task.conversation_token},
            destination={"assignment_id": assignment_id, "charter_digest": digest},
        )
        store.insert_event(
            conn, assignment_id, kind="activation_held", actor="user", payload={"request_id": row["id"]},
        )
        return row


def activate_from_request(
    conn: sqlite3.Connection,
    config,
    *,
    request_id: str,
    preview_digest: str,
    actor_user_id: str,
) -> dict[str, Any]:
    """draft → sleeping, due now, on the approval of a held activation.

    Called by `relay.requests.approve_request` inside its write transaction,
    after it has matched the parked task, the request and the preview digest.
    Raises `RequestError("confirmation_unavailable")` for a request that is not
    this user's held activation. When the assignment has moved since the hold
    (no longer a draft, the charter re-rendered differently, the deadline
    passed, the active ceiling reached), the request closes as `failed` with
    that code and the parked task is cancelled; that is returned rather than
    raised, so the caller's savepoint keeps the close.
    """
    from istota.relay.requests import RequestError, text_hash

    with write_transaction(conn):
        row = conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id = ?", (request_id,)).fetchone()
        if (row is None or row["kind"] != ACTIVATION_KIND or row["state"] != "held"
                or row["requester_user_id"] != actor_user_id
                or row["recipient_user_id"] != actor_user_id
                or row["preview_digest"] != preview_digest
                or text_hash(row["preview"] or "") != preview_digest):
            raise RequestError("confirmation_unavailable")
        try:
            destination = json.loads(row["destination"] or "{}")
        except ValueError:
            destination = {}
        assignment_id = destination.get("assignment_id")
        held_digest = destination.get("charter_digest")
        if type(assignment_id) is not int or not isinstance(held_digest, str):
            raise RequestError("confirmation_unavailable")

        assignment = store.get_assignment(conn, assignment_id, user_id=actor_user_id)
        code = None
        if assignment is None:
            code = "not_found"
        elif config is None:
            code = "assignment_unavailable"
        elif assignment.state != "draft":
            code = "bad_state"
        elif charter.charter_digest(conn, assignment_id) != held_digest:
            code = "charter_changed"
        elif _deadline_passed(assignment):
            code = "deadline_passed"
        else:
            try:
                budget.check_active_limit(conn, config.assignments, actor_user_id, assignment_id)
            except charter.CharterError as exc:
                code = exc.code
        if code is not None:
            conn.execute(
                "UPDATE whatsapp_skill_requests SET state = 'failed', error_code = ?,"
                " closed_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
                (code, request_id),
            )
            if row["origin_task_id"] is not None:
                db.cancel_task(conn, row["origin_task_id"])
            if assignment is not None:
                store.insert_event(
                    conn, assignment_id, kind="activation_refused", actor="system",
                    payload={"request_id": request_id, "reason": code},
                )
            return {"error": code}

        now = db.iso_utc_now()
        store.set_state(conn, assignment_id, "sleeping")
        store.update_assignment(conn, assignment_id, next_wake_at=now, activated_at=now)
        store.insert_event(
            conn, assignment_id, kind="activated", actor="user", payload={"request_id": request_id},
        )
        conn.execute(
            "UPDATE whatsapp_skill_requests SET state = 'approved', approved_digest = ?,"
            " approved_at = datetime('now'), closed_at = datetime('now'), updated_at = datetime('now')"
            " WHERE id = ?",
            (preview_digest, request_id),
        )
        return {"status": "approved", "assignment_id": assignment_id}


#: The close code of a hold cancelled by its own turn's later charter edit.
#: The scheduler does not stop a turn whose hold closed with it.
CANCELLED_BY_OWN_TURN = "charter_changed_in_turn"


def cancel_held_activation(
    conn: sqlite3.Connection, assignment_id: int, *, reason: str, by_task_id: int | None = None,
) -> int:
    """Cancel any held activation of this assignment, and the task parked on it.

    Through the ledger's own close (`relay.relays.close_task_questions`), so
    the parked task is released the way every other closed hold releases it.
    A hold whose origin is `by_task_id`, the turn making the change, closes
    alone as `CANCELLED_BY_OWN_TURN`: that turn is still running and finishes
    with its reply. Returns how many were cancelled.
    """
    from istota.relay.relays import close_task_questions

    with write_transaction(conn):
        rows = conn.execute(_ACTIVATIONS_SQL + " AND state = 'held'", (assignment_id,)).fetchall()
        for row in rows:
            own_turn = by_task_id is not None and row["origin_task_id"] == by_task_id
            if row["origin_task_id"] is not None and not own_turn:
                close_task_questions(conn, row["origin_task_id"], reason=reason)
            else:
                conn.execute(
                    "UPDATE whatsapp_skill_requests SET state = 'cancelled', error_code = ?,"
                    " closed_at = datetime('now'), updated_at = datetime('now')"
                    " WHERE id = ? AND state = 'held'",
                    (CANCELLED_BY_OWN_TURN if own_turn else reason, row["id"]),
                )
        return len(rows)


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
        notices.settle(conn, assignment_id)
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
        notices.settle(conn, assignment_id)
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
        notices.settle(conn, assignment_id)
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
    by_task_id: int | None = None,
) -> store.Assignment:
    """Raise the cost or wake cap. A budget pause that is no longer spent → sleeping, due now."""

    def mutate(assignment: store.Assignment) -> None:
        new_budget = budget.topped_up(cfg, assignment.budget, usd=usd, wakes=wakes)
        store.update_assignment(conn, assignment_id, budget=new_budget, budget_low_sent=False)

    with write_transaction(conn):
        assignment, _ = edit_charter(
            conn, assignment_id, user_id=user_id, mutate=mutate, event="topped_up",
            event_payload={"usd": usd, "wakes": wakes}, by_task_id=by_task_id,
        )
        if (assignment.state == "paused" and assignment.state_reason == "budget"
                and not budget.is_spent(assignment, budget.totals(conn, assignment_id))):
            store.set_state(conn, assignment_id, "sleeping")
            store.update_assignment(conn, assignment_id, next_wake_at=db.iso_utc_now())
            store.insert_event(
                conn, assignment_id, kind="resumed", actor="user", payload={"reason": "budget"},
            )
        notices.settle(conn, assignment_id)
        return store.get_assignment(conn, assignment_id)


#: Where `close --completed` may complete an assignment that has not proposed it.
CLOSE_EARLY_STATES = ("sleeping", "waiting")


def _proposal_pending(assignment: store.Assignment) -> bool:
    return assignment.state == "waiting" and "completion" in assignment.waiting_on


class _Checked:
    """The mechanical items' results, run with no transaction open."""

    def __init__(self, key: tuple, results: dict):
        self.key = key
        self.results = results


def _check_mechanical(
    conn: sqlite3.Connection, config: "Config", assignment_id: int, user_id: str,
) -> _Checked | None:
    # `write_transaction` never commits, so a caller still in a transaction
    # holds the writer lock; reading the mount then would stall the scheduler.
    if conn.in_transaction:
        return None
    assignment = store.get_assignment(conn, assignment_id, user_id=user_id)
    if assignment is None:
        return None
    items = store.list_items(conn, assignment_id)
    return _Checked(validators.items_key(items), validators.run_mechanical(config, assignment, items))


def record_item_results(
    conn: sqlite3.Connection, assignment_id: int, results: dict, checked_at: str,
    *, wake_id: int | None = None,
) -> list[int]:
    """Record every mechanical item's result, with an `item_status` event on
    a change. Returns the ids that moved to `passing`."""
    newly_passing: list[int] = []
    for item in store.list_items(conn, assignment_id):
        if item.kind != "mechanical":
            continue
        result = results[item.id]
        store.set_item_status(conn, item.id, result.status, detail=result.detail, checked_at=checked_at)
        if result.status != item.status:
            store.insert_event(
                conn, assignment_id, kind="item_status", actor="system", wake_id=wake_id,
                payload={"item_id": item.id, "from": item.status, "to": result.status,
                         "detail": result.detail},
            )
        if result.status == "passing" and item.status != "passing":
            newly_passing.append(item.id)
    return newly_passing


def _failing(items: list[store.Item], checked: _Checked) -> list[int]:
    return [
        item.id for item in items
        if item.kind == "mechanical" and checked.results[item.id].status != "passing"
    ]


def _complete_by_user(
    conn: sqlite3.Connection, assignment_id: int, now: str, *, via: str | None,
) -> None:
    """→ completed on the user's word: every open confirmed item confirmed."""
    for item in store.list_items(conn, assignment_id):
        if item.kind == "confirmed" and item.status != "confirmed":
            store.set_item_status(conn, item.id, "confirmed", detail="confirmed at close", checked_at=now)
    store.set_state(conn, assignment_id, "completed", reason="user")
    store.update_assignment(conn, assignment_id, next_wake_at=None, closed_at=now)
    store.insert_event(
        conn, assignment_id, kind="completed", actor="user", payload={"via": via} if via else None,
    )
    # The bell row only, no alert delivery: the user is in the chat that closed it.
    notices.write_notices(conn, assignment_id, [("closed", None)])


def close(
    conn: sqlite3.Connection,
    config: "Config",
    assignment_id: int,
    *,
    user_id: str,
    completed: bool,
    missing: str | None = None,
) -> store.Assignment:
    """Answer a proposed completion: confirm it, or send it back with what is missing.

    Confirming marks every open confirmed item confirmed and → completed.
    Rejecting writes a trusted `completion_rejected` event and → sleeping, due now.

    `completed` also works with no proposal, from sleeping or a wait on the
    user, when every mechanical item passes now (`not_met` names the ones that
    do not): the user may stop once everything is met.
    """
    checked = None
    if completed:
        found = store.get_assignment(conn, assignment_id, user_id=user_id)
        if found is not None and not _proposal_pending(found) and found.state in CLOSE_EARLY_STATES:
            checked = _check_mechanical(conn, config, assignment_id, user_id)
    with write_transaction(conn):
        assignment = _load(conn, assignment_id, user_id)
        now = db.iso_utc_now()
        if _proposal_pending(assignment):
            pass
        elif completed and assignment.state in CLOSE_EARLY_STATES:
            items = store.list_items(conn, assignment_id)
            if checked is None or checked.key != validators.items_key(items):
                raise TransitionError(
                    "items_changed", "the assignment changed while its checks ran; try again",
                    state=assignment.state,
                )
            failing = _failing(items, checked)
            if failing:
                raise TransitionError(
                    "not_met",
                    "these mechanical items are not passing: " + ", ".join(str(i) for i in failing),
                    state=assignment.state,
                )
            record_item_results(conn, assignment_id, checked.results, now)
            _complete_by_user(conn, assignment_id, now, via="close")
            notices.settle(conn, assignment_id)
            return store.get_assignment(conn, assignment_id)
        else:
            raise TransitionError(
                "bad_state", "this assignment has not proposed completion", state=assignment.state,
            )
        if completed:
            _complete_by_user(conn, assignment_id, now, via=None)
        else:
            text = _text(missing, "what is missing")
            store.insert_inbox_event(
                conn, assignment_id, kind="completion_rejected", trusted=True, payload={"missing": text},
            )
            store.set_state(conn, assignment_id, "sleeping")
            store.update_assignment(conn, assignment_id, next_wake_at=now)
            store.insert_event(conn, assignment_id, kind="completion_rejected", actor="user")
        notices.settle(conn, assignment_id)
        return store.get_assignment(conn, assignment_id)


# ---------------------------------------------------------------------------
# The user's other inputs
# ---------------------------------------------------------------------------


def _last_open_confirmed(conn: sqlite3.Connection, assignment_id: int, item_id: int) -> bool:
    return not any(
        item.kind == "confirmed" and item.status != "confirmed" and item.id != item_id
        for item in store.list_items(conn, assignment_id)
    )


def confirm_item(
    conn: sqlite3.Connection, config: "Config", assignment_id: int, *, user_id: str, item_id: int,
) -> store.Item:
    """Mark a confirmed item confirmed. Mechanical items are the validators' alone.

    The last open confirmed item, on an activated assignment whose mechanical
    items all pass now, completes it: the user's confirmation is the answer a
    proposal would ask for, so no wake is spent proposing it (ISSUE-853).
    Otherwise the next wake reads an `item_confirmed` inbox event.
    """
    checked = None
    found = store.get_assignment(conn, assignment_id, user_id=user_id)
    if (found is not None and found.state not in ("draft", *TERMINAL_STATES)
            and _last_open_confirmed(conn, assignment_id, item_id)):
        checked = _check_mechanical(conn, config, assignment_id, user_id)
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
        now = db.iso_utc_now()
        store.set_item_status(conn, item.id, "confirmed", detail="confirmed by the user", checked_at=now)
        store.insert_event(conn, assignment_id, kind="item_confirmed", actor="user", payload={"item_id": item.id})
        if assignment.state == "draft":
            return store.get_item(conn, assignment_id, item_id)
        items = store.list_items(conn, assignment_id)
        if (checked is not None and checked.key == validators.items_key(items)
                and _last_open_confirmed(conn, assignment_id, item_id) and not _failing(items, checked)):
            record_item_results(conn, assignment_id, checked.results, now)
            _complete_by_user(conn, assignment_id, now, via="item_confirm")
            notices.settle(conn, assignment_id)
        else:
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
        notices.settle(conn, assignment_id)
        return store.get_action(conn, assignment_id, action_id)


# ---------------------------------------------------------------------------
# The system's transitions around a wake
# ---------------------------------------------------------------------------

WAKEABLE_STATES = ("sleeping", "waiting")


def _load_any(conn: sqlite3.Connection, assignment_id: int) -> store.Assignment:
    assignment = store.get_assignment(conn, assignment_id)
    if assignment is None:
        raise TransitionError("not_found", "no such assignment")
    return assignment


def _superseded(conn: sqlite3.Connection, assignment_id: int, wake_id: int) -> bool:
    """Whether a later wake of this assignment exists, so the `active` state
    belongs to it and an earlier wake ending must not move it."""
    row = conn.execute(
        "SELECT 1 FROM assignment_wakes WHERE assignment_id = ? AND seq >"
        " (SELECT seq FROM assignment_wakes WHERE id = ?) LIMIT 1",
        (assignment_id, wake_id),
    ).fetchone()
    return row is not None


def wake_created(
    conn: sqlite3.Connection, assignment_id: int, *, wake_id: int, task_id: int, fired_by: str,
) -> None:
    """sleeping or waiting → active, once the wake row and its task exist."""
    with write_transaction(conn):
        assignment = _load_any(conn, assignment_id)
        _require(assignment, WAKEABLE_STATES, "wake")
        store.set_state(conn, assignment_id, "active")
        store.update_assignment(conn, assignment_id, next_wake_at=None)
        store.insert_event(
            conn, assignment_id, kind="wake_created", actor="system", wake_id=wake_id,
            payload={"from": assignment.state, "fired_by": fired_by, "task_id": task_id},
        )


def pause_for_budget(conn: sqlite3.Connection, assignment_id: int) -> store.Assignment:
    """sleeping or waiting → paused (budget): the ledger has no room for another wake."""
    with write_transaction(conn):
        assignment = _load_any(conn, assignment_id)
        _require(assignment, WAKEABLE_STATES, "pause")
        store.set_state(conn, assignment_id, "paused", reason="budget")
        store.update_assignment(conn, assignment_id, next_wake_at=None)
        store.insert_event(
            conn, assignment_id, kind="paused", actor="system",
            payload={"from": assignment.state, "reason": "budget"},
        )
        return store.get_assignment(conn, assignment_id)


def wake_ended(
    conn: sqlite3.Connection, assignment_id: int, *, wake_id: int, next_wake_at: str,
) -> bool:
    """active → sleeping, due at `next_wake_at`.

    Returns False and changes nothing when the assignment has left `active`
    while the wake ran (the user paused or abandoned it), so the user's state
    is kept.
    """
    with write_transaction(conn):
        assignment = _load_any(conn, assignment_id)
        if assignment.state != "active" or _superseded(conn, assignment_id, wake_id):
            return False
        store.set_state(conn, assignment_id, "sleeping")
        store.update_assignment(conn, assignment_id, next_wake_at=next_wake_at)
        store.insert_event(
            conn, assignment_id, kind="wake_ended", actor="system", wake_id=wake_id,
            payload={"next_wake_at": next_wake_at},
        )
        return True


def wake_waits(
    conn: sqlite3.Connection, assignment_id: int, *, wake_id: int, waiting_on: list[str],
) -> bool:
    """active → waiting on the named kinds. False, and nothing written, once
    the assignment has left `active`."""
    with write_transaction(conn):
        assignment = _load_any(conn, assignment_id)
        if assignment.state != "active" or _superseded(conn, assignment_id, wake_id):
            return False
        store.set_state(conn, assignment_id, "waiting", waiting_on=list(waiting_on))
        store.update_assignment(conn, assignment_id, next_wake_at=None)
        store.insert_event(
            conn, assignment_id, kind="waiting", actor="system", wake_id=wake_id,
            payload={"waiting_on": list(waiting_on)},
        )
        return True


#: Why the host pauses an assignment after a wake (spec Design section 2).
SYSTEM_PAUSE_REASONS = ("budget", "stalled", "failures", "thrash", "deadline")


def pause_after_wake(
    conn: sqlite3.Connection,
    assignment_id: int,
    *,
    wake_id: int,
    reason: str,
    detail: dict[str, Any] | None = None,
    from_states: tuple[str, ...] = ("active",),
) -> bool:
    """active → paused (`reason`). False, and nothing written, when the
    assignment is in none of `from_states`: a pause or abandon the user set
    while the wake ran is kept."""
    if reason not in SYSTEM_PAUSE_REASONS:
        raise ValueError(f"not a system pause reason: {reason}")
    with write_transaction(conn):
        assignment = _load_any(conn, assignment_id)
        if assignment.state not in from_states or _superseded(conn, assignment_id, wake_id):
            return False
        store.set_state(conn, assignment_id, "paused", reason=reason)
        store.update_assignment(conn, assignment_id, next_wake_at=None)
        store.insert_event(
            conn, assignment_id, kind="paused", actor="system", wake_id=wake_id,
            payload={"from": assignment.state, "reason": reason, **(detail or {})},
        )
        return True


def complete_after_wake(conn: sqlite3.Connection, assignment_id: int, *, wake_id: int) -> bool:
    """active → completed, when a wake's completion met every item and the
    charter set `auto_complete`."""
    with write_transaction(conn):
        assignment = _load_any(conn, assignment_id)
        if assignment.state != "active" or _superseded(conn, assignment_id, wake_id):
            return False
        now = db.iso_utc_now()
        store.set_state(conn, assignment_id, "completed", reason="auto")
        store.update_assignment(conn, assignment_id, next_wake_at=None, closed_at=now)
        store.insert_event(conn, assignment_id, kind="completed", actor="system", wake_id=wake_id)
        return True


# ---------------------------------------------------------------------------
# A wake's requests
# ---------------------------------------------------------------------------
#
# What a wake writes through its own verbs. None of it moves the state: `next`
# and `complete` are requests on the wake row that `wakes.finish_wake` decides,
# after the validators have run on the final files. Hand-offs, reports and
# attempts are rows of their own. Each write re-reads the wake inside the
# transaction and refuses `not_a_wake` unless it is this task's wake of this
# assignment and has not ended, so a write cannot land on a finished wake.

NEXT_WAITS = ("user",)
ATTEMPT_OUTCOMES = ("ok", "failed", "partial")
STEP_MAX_CHARS = 1000
PROGRESS_MAX_CHARS = 1000
TARGET_MAX_CHARS = 200
TRIED_MAX_CHARS = 2000


def _open_wake_of(
    conn: sqlite3.Connection, assignment_id: int, *, user_id: str, wake_id: int, task_id: int,
) -> tuple[store.Assignment, store.Wake]:
    assignment = _load(conn, assignment_id, user_id)
    wake = store.get_wake(conn, wake_id)
    if (wake is None or wake.assignment_id != assignment_id or wake.task_id != task_id
            or wake.ended_at is not None):
        raise TransitionError("not_a_wake", "this task is not a running wake of that assignment")
    return assignment, wake


def request_next(
    conn: sqlite3.Connection,
    cfg: "AssignmentsConfig",
    assignment_id: int,
    *,
    user_id: str,
    wake_id: int,
    task_id: int,
    mode: str,
    requested: str,
    wake_at: datetime | None = None,
    wait: str | None = None,
    progress: object = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record the wake's `next` on its row, replacing any earlier one.

    `mode` is `in` or `at` with `wake_at`, or `wait` with `wait`. A time is
    clamped below by `min_wake_interval_minutes` from now and above by the
    deadline; `clamped` says which applied. Returns the stored request.
    """
    note = _text(progress, "the progress note", PROGRESS_MAX_CHARS) if progress is not None else None
    now = now or datetime.now(timezone.utc)
    with write_transaction(conn):
        assignment, wake = _open_wake_of(
            conn, assignment_id, user_id=user_id, wake_id=wake_id, task_id=task_id,
        )
        clamped = None
        when: str | None = None
        if mode == "wait":
            if wait not in NEXT_WAITS:
                raise TransitionError("invalid_input", f"--wait takes one of {', '.join(NEXT_WAITS)}")
        elif mode in ("in", "at") and wake_at is not None:
            floor = now + timedelta(minutes=cfg.min_wake_interval_minutes)
            if wake_at < floor:
                wake_at, clamped = floor, "floor"
            if assignment.deadline:
                deadline = datetime.fromisoformat(assignment.deadline.replace("Z", "+00:00"))
                if wake_at > deadline:
                    wake_at, clamped = deadline, "deadline"
            when = charter.iso_z(wake_at)
        else:
            raise TransitionError("invalid_input", "next needs --in, --at or --wait")
        request = {
            "mode": mode, "requested": requested, "wake_at": when,
            "wait": wait if mode == "wait" else None, "clamped": clamped,
            "recorded_at": charter.iso_z(now),
        }
        store.update_wake(conn, wake.id, next_request=request, progress_note=note)
        return request


def add_handoff(
    conn: sqlite3.Connection,
    assignment_id: int,
    *,
    user_id: str,
    wake_id: int,
    task_id: int,
    step: object,
    report: object,
    due_at: str | None = None,
) -> store.Action:
    """An open hand-off the wake gives the user, with what to report back.

    The `handoff_created` audit row (actor `assignment`, the wake's id, the
    action id) is what the notice for it hangs off.
    """
    clean_step = _text(step, "the step", STEP_MAX_CHARS)
    clean_report = _text(report, "what to report back", STEP_MAX_CHARS)
    with write_transaction(conn):
        assignment, wake = _open_wake_of(
            conn, assignment_id, user_id=user_id, wake_id=wake_id, task_id=task_id,
        )
        _require_live(assignment, "hand off a step of")
        action_id = store.insert_action(
            conn, assignment_id, text=clean_step, report_spec=clean_report, due_at=due_at,
            wake_id=wake.id,
        )
        store.insert_event(
            conn, assignment_id, kind="handoff_created", actor="assignment", wake_id=wake.id,
            payload={"action_id": action_id},
        )
        return store.get_action(conn, assignment_id, action_id)


def add_report(
    conn: sqlite3.Connection, assignment_id: int, *, user_id: str, wake_id: int, task_id: int,
    text: object,
) -> int:
    """A milestone the wake reports: a `report` audit row, which the user's notice reads."""
    clean = _text(text, "the report")
    with write_transaction(conn):
        assignment, wake = _open_wake_of(
            conn, assignment_id, user_id=user_id, wake_id=wake_id, task_id=task_id,
        )
        _require_live(assignment, "report on")
        return store.insert_event(
            conn, assignment_id, kind="report", actor="assignment", wake_id=wake.id,
            payload={"text": clean},
        )


def add_attempt(
    conn: sqlite3.Connection, assignment_id: int, *, user_id: str, wake_id: int, task_id: int,
    target: object, tried: object, outcome: str,
) -> int:
    """One attempt row; the host counts repeats per target for the thrash check."""
    clean_target = " ".join(_text(target, "the target", TARGET_MAX_CHARS).split())
    clean_tried = _text(tried, "what was tried", TRIED_MAX_CHARS)
    if outcome not in ATTEMPT_OUTCOMES:
        raise TransitionError("invalid_input", f"the outcome is one of {', '.join(ATTEMPT_OUTCOMES)}")
    with write_transaction(conn):
        _, wake = _open_wake_of(
            conn, assignment_id, user_id=user_id, wake_id=wake_id, task_id=task_id,
        )
        return store.insert_attempt(
            conn, assignment_id, target=clean_target, tried=clean_tried, outcome=outcome,
            wake_id=wake.id,
        )


def request_complete(
    conn: sqlite3.Connection, assignment_id: int, *, user_id: str, wake_id: int, task_id: int,
    evidence: object,
) -> None:
    """Record a proposed completion on the wake row, replacing any earlier one.

    `finish_wake` decides it once the validators have run on the final files.
    """
    clean = _text(evidence, "the evidence")
    with write_transaction(conn):
        _, wake = _open_wake_of(
            conn, assignment_id, user_id=user_id, wake_id=wake_id, task_id=task_id,
        )
        store.update_wake(conn, wake.id, complete_request=clean)
        store.insert_event(
            conn, assignment_id, kind="complete_requested", actor="assignment", wake_id=wake.id,
        )
