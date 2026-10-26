"""`istota-skill assignment`: the user's verbs on an assignment, and the read verbs.

Runs host-side through the skill proxy. Every verb refuses unless the caller
is an admin (`admin_only` gates only selection), and refuses while the calling task has scopes withheld, since
an assignment is the user's personal record. Every assignment lookup is scoped
by `ISTOTA_USER_ID`, so another user's id or slug answers `not_found`.

Verbs that change the charter, the state or the budget, or that write a
trusted inbox event, need the user's own turn (`assignments.caller`), decided
from the task row named by `ISTOTA_TASK_ID`. `list` and `show` answer any task
of the user. Writes go straight to the framework DB in this process, not
through deferred files.

Errors are `{"status": "error", "error": "<code>", "message": "..."}`, exit 1,
built by `run_skill_cli`'s `on_exception` from the three refusal types, so a
refused write rolls back with the connection rather than being printed from
inside it.

`activate` holds the rendered charter in the relay request ledger
(`assignments.lifecycle.hold_activation`); the task parks on it and the user's
approval of that exact preview starts the assignment.

The wake verbs (`next`, `handoff "<step>"`, `report`, `attempt`, `complete`)
need the caller to be a wake (`caller.Wake` with its wake row) and the named
assignment to be that wake's own: `not_a_wake` otherwise, `wrong_assignment`
for another assignment. `--assignment` defaults to `ISTOTA_ASSIGNMENT_ID`, then
to the task row's assignment, and is always checked against the task row.
`next` and `complete` are requests recorded on the wake row; `finish_wake`
decides them.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

from istota.skills._cli import error_envelope, parse_and_resolve, run_skill_cli
from istota.skills._hostpath import NOT_A_PATH, host_path

STATES = ("draft", "active", "sleeping", "waiting", "paused", "completed", "abandoned")
DEFAULT_SHOW_WAKES = 3
MAX_SHOW_WAKES = 20
SUMMARY_SHOW_CHARS = 2000


class Refusal(Exception):
    """A refusal with a CLI error code, turned into the error envelope."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def _envelope(code: str, message: str, **extra: Any) -> dict:
    # `error_envelope`'s first parameter is named `message`, so the text is set after.
    envelope = error_envelope(code, **extra)
    envelope["message"] = message
    return envelope


_LEDGER_MESSAGES = {
    "confirmation_pending": "this turn is already waiting on another approval; finish that first",
    "task_unavailable": "the calling task is not running",
}


def _on_exception(exc: BaseException) -> dict:
    from istota.assignments.charter import CharterError
    from istota.assignments.lifecycle import TransitionError
    from istota.relay.requests import RequestError

    if isinstance(exc, RequestError):
        return _envelope(str(exc), _LEDGER_MESSAGES.get(str(exc), "the approval could not be held"))
    if isinstance(exc, Refusal):
        return _envelope(exc.code, exc.message, **exc.extra)
    if isinstance(exc, TransitionError):
        extra = {"state": exc.state} if exc.state else {}
        return _envelope(exc.code, exc.message, **extra)
    if isinstance(exc, CharterError):
        return _envelope(exc.code, exc.message)
    return _envelope("internal_error", type(exc).__name__)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, config, user_id: str, conn: sqlite3.Connection):
        self.config = config
        self.user_id = user_id
        self.conn = conn

    @property
    def cfg(self):
        return self.config.assignments


def _gate() -> dict:
    """The admin check and the skill guard, before any DB is opened."""
    from istota.skills.skills import _guard_skill, _load_context

    ctx = _load_context()
    if not ctx["is_admin"]:
        raise Refusal("not_admin", "assignments are available to admins only")
    refusal = _guard_skill("assignment", ctx)
    if refusal:
        raise Refusal("feature_disabled", refusal)
    if ctx["withheld"]:
        raise Refusal(
            "not_a_user_turn", "assignments are unavailable in a task that runs with scopes withheld",
        )
    return ctx


def _db_path() -> str:
    path = os.environ.get("ISTOTA_DB_PATH", "")
    if not path:
        raise Refusal("internal_error", "the framework database path is not available to this task")
    return path


def _require_user_turn(ctx: _Ctx) -> None:
    from istota.assignments import caller

    found = caller.classify(ctx.conn, task_id=os.environ.get("ISTOTA_TASK_ID"), user_id=ctx.user_id)
    if not isinstance(found, caller.UserTurn):
        raise Refusal(
            "not_a_user_turn",
            "only the user's own turn in their private web or Talk chat can do this; "
            "ask them to say it there",
        )


def _task_id() -> int | None:
    """The calling task, which `_require_user_turn` has already checked."""
    value = os.environ.get("ISTOTA_TASK_ID")
    return int(value) if value and value.isdigit() else None


def _user_tz(ctx: _Ctx) -> tzinfo:
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(ctx.config.resolve_user_timezone(ctx.user_id, conn=ctx.conn))
    except Exception:
        return timezone.utc


def _resolve(ctx: _Ctx, ident: str):
    from istota.assignments import store

    text = str(ident or "").strip()
    found = None
    if text.isdigit():
        found = store.get_assignment(ctx.conn, int(text), user_id=ctx.user_id)
    elif text:
        found = store.get_assignment_by_slug(ctx.conn, ctx.user_id, text)
    if found is None:
        raise Refusal("not_found", f"no assignment {text!r}")
    return found


def _summary(assignment) -> dict:
    return {
        "id": assignment.id,
        "slug": assignment.slug,
        "title": assignment.title,
        "state": assignment.state,
        "state_reason": assignment.state_reason,
        "waiting_on": assignment.waiting_on,
        "deadline": assignment.deadline,
        "next_wake_at": assignment.next_wake_at,
        "cadence_minutes": assignment.cadence_minutes,
        "budget": assignment.budget,
        "auto_complete": assignment.auto_complete,
        "model": assignment.model,
        "effort": assignment.effort,
        "directory": assignment.dir_relpath,
        "created_at": assignment.created_at,
        "activated_at": assignment.activated_at,
        "closed_at": assignment.closed_at,
    }


def _rewrite(ctx: _Ctx, assignment_id: int) -> str:
    """Commit, then re-render `CHARTER.md`; the DB is the authority either way."""
    from istota.assignments import charter

    ctx.conn.commit()
    return charter.rewrite_charter_file(ctx.config, ctx.conn, assignment_id)


def _changed(ctx: _Ctx, assignment, **extra: Any) -> dict:
    rendered = _rewrite(ctx, assignment.id)
    from istota.assignments import store

    current = store.get_assignment(ctx.conn, assignment.id)
    return {"status": "ok", "assignment": _summary(current), "charter": rendered, **extra}


# ---------------------------------------------------------------------------
# Charter verbs
# ---------------------------------------------------------------------------


def _budget_fields(ctx: _Ctx, base: dict, args) -> dict:
    from istota.assignments import charter

    merged = dict(base)
    for key, attr in (("max_cost_usd", "budget_usd"), ("max_wakes", "max_wakes"),
                      ("per_wake_minutes", "per_wake_minutes"), ("per_wake_turns", "per_wake_turns")):
        value = getattr(args, attr, None)
        if value is not None:
            merged[key] = value
    return charter.validate_budget(
        ctx.cfg,
        max_cost_usd=merged.get("max_cost_usd"),
        max_wakes=merged.get("max_wakes"),
        per_wake_minutes=merged.get("per_wake_minutes"),
        per_wake_turns=merged.get("per_wake_turns"),
    )


def cmd_create(ctx: _Ctx, args) -> dict:
    from istota.assignments import charter, lifecycle

    _require_user_turn(ctx)
    title = charter.validate_title(args.title)
    goal = charter.validate_goal(args.goal)
    autonomy = charter.validate_autonomy(args.autonomy)
    budget_fields = _budget_fields(ctx, {}, args)
    deadline = (
        charter.validate_deadline(args.deadline, tz=_user_tz(ctx)) if args.deadline else None
    )
    cadence = charter.validate_cadence(
        ctx.cfg, charter.parse_duration_minutes(args.cadence) if args.cadence else 360,
    )
    assignment = lifecycle.create(
        ctx.conn,
        user_id=ctx.user_id,
        title=title,
        goal=goal,
        autonomy=autonomy,
        budget_fields=budget_fields,
        deadline=deadline,
        cadence_minutes=cadence,
        auto_complete=bool(args.auto_complete),
        model=charter.validate_model(args.model),
        effort=charter.validate_effort(args.effort),
    )
    ctx.conn.commit()
    directory = charter.create_directory(ctx.config, ctx.conn, assignment)
    rendered = charter.render_current(ctx.conn, assignment.id)
    return {
        "status": "ok", "assignment": _summary(assignment), "charter": rendered,
        "directory_written": directory,
    }


def cmd_set(ctx: _Ctx, args) -> dict:
    from istota.assignments import charter, lifecycle, store

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)
    tz = _user_tz(ctx)

    def mutate(current) -> list[str]:
        fields: dict[str, Any] = {}
        if args.title is not None:
            fields["title"] = charter.validate_title(args.title)
        if args.goal is not None:
            fields["goal"] = charter.validate_goal(args.goal)
        if args.autonomy is not None:
            fields["autonomy"] = charter.validate_autonomy(args.autonomy)
        if any(getattr(args, a) is not None
               for a in ("budget_usd", "max_wakes", "per_wake_minutes", "per_wake_turns")):
            fields["budget"] = _budget_fields(ctx, current.budget, args)
        if args.clear_deadline:
            fields["deadline"] = None
        elif args.deadline is not None:
            fields["deadline"] = charter.validate_deadline(args.deadline, tz=tz)
        if args.cadence is not None:
            fields["cadence_minutes"] = charter.validate_cadence(
                ctx.cfg, charter.parse_duration_minutes(args.cadence),
            )
        if args.auto_complete is not None:
            fields["auto_complete"] = bool(args.auto_complete)
        if args.model is not None:
            fields["model"] = charter.validate_model(args.model)
        if args.effort is not None:
            fields["effort"] = charter.validate_effort(args.effort)
        if not fields:
            raise Refusal("invalid_input", "set needs at least one option to change")
        store.update_assignment(ctx.conn, current.id, **fields)
        return sorted(fields)

    updated, changed = lifecycle.edit_charter(
        ctx.conn, assignment.id, user_id=ctx.user_id, mutate=mutate, by_task_id=_task_id(), event="charter_set",
    )
    return _changed(ctx, updated, changed=changed)


def _validator_spec(args) -> dict | None:
    if args.check is None:
        extras = [flag for flag, value in (
            ("--file", args.file), ("--format", args.format), ("--columns", args.columns),
            ("--min-rows", args.min_rows), ("--text-match", args.text_match),
        ) if value is not None] + (["--absent"] if args.absent else [])
        if extras:
            raise Refusal("invalid_validator", f"{', '.join(extras)} need --check")
        return None
    spec: dict[str, Any] = {"check": args.check, "file": args.file}
    if args.format is not None:
        spec["format"] = args.format
    if args.columns is not None:
        spec["columns"] = [c.strip() for c in args.columns.split(",")]
    if args.min_rows is not None:
        spec["min_rows"] = args.min_rows
    if args.text_match is not None:
        spec["text"] = args.text_match
    if args.absent:
        spec["present"] = False
    return spec


def cmd_item_add(ctx: _Ctx, args) -> dict:
    from istota.assignments import charter, lifecycle, store

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)
    text, spec = charter.validate_item(args.text, args.kind, _validator_spec(args))

    def mutate(current) -> int:
        return store.insert_item(ctx.conn, current.id, text=text, kind=args.kind, validator=spec)

    updated, item_id = lifecycle.edit_charter(
        ctx.conn, assignment.id, user_id=ctx.user_id, mutate=mutate, by_task_id=_task_id(), event="item_added",
        event_payload={"kind": args.kind},
    )
    return _changed(ctx, updated, item_id=item_id)


def cmd_item_remove(ctx: _Ctx, args) -> dict:
    from istota.assignments import lifecycle, store

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)

    def mutate(current) -> None:
        if not store.delete_item(ctx.conn, current.id, args.item_id):
            raise lifecycle.TransitionError("not_found", "no such item on this assignment")

    updated, _ = lifecycle.edit_charter(
        ctx.conn, assignment.id, user_id=ctx.user_id, mutate=mutate, by_task_id=_task_id(), event="item_removed",
        event_payload={"item_id": args.item_id},
    )
    return _changed(ctx, updated, removed_item_id=args.item_id)


def cmd_item_confirm(ctx: _Ctx, args) -> dict:
    from istota.assignments import lifecycle

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)
    item = lifecycle.confirm_item(ctx.conn, assignment.id, user_id=ctx.user_id, item_id=args.item_id)
    return _changed(ctx, assignment, item={"id": item.id, "status": item.status})


def cmd_frozen_add(ctx: _Ctx, args) -> dict:
    from istota.assignments import charter, lifecycle, store

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)
    name, value = charter.validate_frozen(args.name, args.value)

    def mutate(current) -> int:
        try:
            return store.insert_frozen(ctx.conn, current.id, name=name, value=value)
        except sqlite3.IntegrityError:
            raise charter.CharterError(
                "invalid_charter", f"there is already a frozen commitment named {name!r}; remove it first",
            ) from None

    updated, frozen_id = lifecycle.edit_charter(
        ctx.conn, assignment.id, user_id=ctx.user_id, mutate=mutate, by_task_id=_task_id(), event="frozen_added",
        event_payload={"name": name},
    )
    return _changed(ctx, updated, frozen_id=frozen_id)


def cmd_frozen_remove(ctx: _Ctx, args) -> dict:
    from istota.assignments import lifecycle, store

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)

    def mutate(current) -> None:
        if not store.delete_frozen(ctx.conn, current.id, args.frozen_id):
            raise lifecycle.TransitionError("not_found", "no such frozen commitment on this assignment")

    updated, _ = lifecycle.edit_charter(
        ctx.conn, assignment.id, user_id=ctx.user_id, mutate=mutate, by_task_id=_task_id(), event="frozen_removed",
        event_payload={"frozen_id": args.frozen_id},
    )
    return _changed(ctx, updated, removed_frozen_id=args.frozen_id)


# ---------------------------------------------------------------------------
# State verbs
# ---------------------------------------------------------------------------


def _state_result(ctx: _Ctx, assignment, **extra: Any) -> dict:
    ctx.conn.commit()
    return {"status": "ok", "assignment": _summary(assignment), **extra}


def cmd_activate(ctx: _Ctx, args) -> dict:
    """Hold the rendered charter for the user's approval; the task parks on it."""
    from istota.assignments import lifecycle, store

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)
    row = lifecycle.hold_activation(
        ctx.conn, ctx.cfg, assignment.id, user_id=ctx.user_id,
        task_id=int(os.environ["ISTOTA_TASK_ID"]),
    )
    ctx.conn.commit()
    current = store.get_assignment(ctx.conn, assignment.id)
    result = {
        "status": row["state"], "request_id": row["id"],
        "needs_confirmation": row["state"] == "held", "assignment": _summary(current),
    }
    if row["state"] == "held":
        result["preview"] = row["preview"]
    return result


def cmd_pause(ctx: _Ctx, args) -> dict:
    from istota.assignments import lifecycle

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)
    return _state_result(ctx, lifecycle.pause(ctx.conn, assignment.id, user_id=ctx.user_id))


def cmd_resume(ctx: _Ctx, args) -> dict:
    from istota.assignments import lifecycle

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)
    return _state_result(ctx, lifecycle.resume(ctx.conn, ctx.cfg, assignment.id, user_id=ctx.user_id))


def cmd_abandon(ctx: _Ctx, args) -> dict:
    from istota.assignments import lifecycle

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)
    return _state_result(
        ctx, lifecycle.abandon(ctx.conn, assignment.id, user_id=ctx.user_id, reason=args.reason),
    )


def cmd_wake(ctx: _Ctx, args) -> dict:
    from istota.assignments import lifecycle

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)
    return _state_result(ctx, lifecycle.request_wake(ctx.conn, assignment.id, user_id=ctx.user_id))


def cmd_topup(ctx: _Ctx, args) -> dict:
    from istota.assignments import lifecycle

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)
    updated = lifecycle.topup(
        ctx.conn, ctx.cfg, assignment.id, user_id=ctx.user_id, usd=args.usd, wakes=args.wakes,
        by_task_id=_task_id(),
    )
    return _changed(ctx, updated)


def cmd_note(ctx: _Ctx, args) -> dict:
    from istota.assignments import lifecycle

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)
    inbox_id = lifecycle.add_note(ctx.conn, assignment.id, user_id=ctx.user_id, text=args.text)
    return _state_result(ctx, assignment, inbox_id=inbox_id)


def cmd_handoff_done(ctx: _Ctx, args) -> dict:
    from istota.assignments import lifecycle

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)
    action = lifecycle.complete_handoff(
        ctx.conn, assignment.id, user_id=ctx.user_id, action_id=args.action_id, report=args.report,
    )
    return _state_result(ctx, assignment, handoff={"id": action.id, "status": action.status})


def cmd_close(ctx: _Ctx, args) -> dict:
    from istota.assignments import lifecycle

    _require_user_turn(ctx)
    assignment = _resolve(ctx, args.id)
    updated = lifecycle.close(
        ctx.conn, assignment.id, user_id=ctx.user_id,
        completed=bool(args.completed), missing=args.reject,
    )
    return _state_result(ctx, updated)


# ---------------------------------------------------------------------------
# Wake verbs
# ---------------------------------------------------------------------------


def _require_wake(ctx: _Ctx, ident: str | None):
    """The calling wake, and the assignment the verb names, which must be its own.

    The assignment defaults to `ISTOTA_ASSIGNMENT_ID`, then to the task row's;
    either way it is checked against the task row, which is the authority.
    """
    from istota.assignments import caller

    found = caller.classify(ctx.conn, task_id=os.environ.get("ISTOTA_TASK_ID"), user_id=ctx.user_id)
    if not isinstance(found, caller.Wake) or found.wake_id is None:
        raise Refusal("not_a_wake", "only a wake of an assignment can do this")
    text = (ident or "").strip() or os.environ.get(caller.ASSIGNMENT_ID_VAR, "").strip()
    assignment = _resolve(ctx, text or str(found.assignment_id))
    if assignment.id != found.assignment_id:
        raise Refusal("wrong_assignment", "this wake belongs to a different assignment")
    return found, assignment


def _wake_ids(ctx: _Ctx, found) -> dict:
    return {"user_id": ctx.user_id, "wake_id": found.wake_id, "task_id": found.task_id}


def cmd_next(ctx: _Ctx, args) -> dict:
    from istota.assignments import charter, lifecycle

    found, assignment = _require_wake(ctx, args.assignment)
    now = datetime.now(timezone.utc)
    wake_at = None
    if args.in_ is not None:
        mode, requested = "in", args.in_
        wake_at = now + timedelta(minutes=charter.parse_duration_minutes(args.in_))
    elif args.at is not None:
        mode, requested = "at", args.at
        try:
            wake_at = charter.parse_time(args.at, tz=_user_tz(ctx))
        except ValueError:
            raise Refusal(
                "invalid_input", "--at takes an ISO 8601 date and time, such as 2026-11-02T09:00",
            ) from None
    else:
        mode, requested = "wait", args.wait
    request = lifecycle.request_next(
        ctx.conn, ctx.cfg, assignment.id, **_wake_ids(ctx, found), mode=mode, requested=requested,
        wake_at=wake_at, wait=args.wait, progress=args.progress, now=now,
    )
    return _state_result(ctx, assignment, next=request)


def cmd_handoff(ctx: _Ctx, args) -> dict:
    from istota.assignments import charter, lifecycle

    found, assignment = _require_wake(ctx, args.assignment)
    due_at = None
    if args.due is not None:
        try:
            due_at = charter.validate_deadline(args.due, tz=_user_tz(ctx))
        except charter.CharterError as exc:
            message = ("the due date is in the past" if exc.code == "deadline_passed"
                       else "--due takes a date (YYYY-MM-DD) or an ISO 8601 time")
            raise Refusal("invalid_input", message) from None
    action = lifecycle.add_handoff(
        ctx.conn, assignment.id, **_wake_ids(ctx, found), step=args.step, report=args.report,
        due_at=due_at,
    )
    return _state_result(
        ctx, assignment,
        handoff={"id": action.id, "text": action.text, "report": action.report_spec,
                 "due_at": action.due_at, "status": action.status},
    )


def cmd_report(ctx: _Ctx, args) -> dict:
    from istota.assignments import lifecycle

    found, assignment = _require_wake(ctx, args.assignment)
    event_id = lifecycle.add_report(ctx.conn, assignment.id, **_wake_ids(ctx, found), text=args.text)
    return _state_result(ctx, assignment, event_id=event_id)


def cmd_attempt(ctx: _Ctx, args) -> dict:
    from istota.assignments import lifecycle

    found, assignment = _require_wake(ctx, args.assignment)
    attempt_id = lifecycle.add_attempt(
        ctx.conn, assignment.id, **_wake_ids(ctx, found), target=args.target, tried=args.tried,
        outcome=args.outcome,
    )
    return _state_result(ctx, assignment, attempt_id=attempt_id)


def cmd_complete(ctx: _Ctx, args) -> dict:
    from istota.assignments import lifecycle

    found, assignment = _require_wake(ctx, args.assignment)
    lifecycle.request_complete(ctx.conn, assignment.id, **_wake_ids(ctx, found), evidence=args.evidence)
    return _state_result(ctx, assignment, complete_requested=True)


# ---------------------------------------------------------------------------
# Read verbs
# ---------------------------------------------------------------------------


def cmd_list(ctx: _Ctx, args) -> dict:
    from istota.assignments import store

    states = [args.state] if args.state else None
    rows = store.list_assignments(ctx.conn, ctx.user_id, states=states)
    return {"status": "ok", "state": args.state, "assignments": [_summary(a) for a in rows]}


def cmd_show(ctx: _Ctx, args) -> dict:
    from istota.assignments import budget, charter, store
    from istota.assignments.prompt import UNTRUSTED_NOTICE

    assignment = _resolve(ctx, args.id)
    limit = max(0, min(args.wakes if args.wakes is not None else DEFAULT_SHOW_WAKES, MAX_SHOW_WAKES))
    spent = budget.totals(ctx.conn, assignment.id)
    items = store.list_items(ctx.conn, assignment.id)
    frozen = store.list_frozen(ctx.conn, assignment.id)
    wakes = store.list_wakes(ctx.conn, assignment.id, limit=limit) if limit else []
    return {
        "status": "ok",
        "assignment": _summary(assignment),
        "charter": charter.render(assignment, items, frozen),
        "ledger": {
            "cost_usd": round(spent.cost_usd, 4),
            "wakes": spent.wakes,
            "unmeasured_wakes": spent.unmeasured_wakes,
            "billed_input_tokens": spent.billed_input_tokens,
            "output_tokens": spent.output_tokens,
            "remaining": budget.remaining(assignment, spent),
        },
        "items": [
            {"id": i.id, "position": i.position, "kind": i.kind, "text": i.text, "status": i.status,
             "detail": i.detail, "checked_at": i.checked_at}
            for i in items
        ],
        "frozen": [{"id": f.id, "name": f.name, "value": f.value} for f in frozen],
        "open_handoffs": [
            {"id": a.id, "text": a.text, "report": a.report_spec, "due_at": a.due_at, "created_at": a.created_at}
            for a in store.list_actions(ctx.conn, assignment.id, status="open")
        ],
        "wakes": [
            {"seq": w.seq, "fired_by": w.fired_by, "started_at": w.started_at, "ended_at": w.ended_at,
             "outcome": w.outcome, "cost_usd": w.cost_usd, "progress_signals": w.progress_signals,
             "summary": (w.summary or "")[:SUMMARY_SHOW_CHARS] or None,
             "summary_truncated": len(w.summary or "") > SUMMARY_SHOW_CHARS}
            for w in wakes
        ],
        "notice": UNTRUSTED_NOTICE,
    }


# ---------------------------------------------------------------------------
# Parser and dispatch
# ---------------------------------------------------------------------------


def _add_charter_options(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument("--title", required=required)
    parser.add_argument("--goal", required=required)
    parser.add_argument("--autonomy", default="" if required else None,
                        help="What Istota may do without asking")
    parser.add_argument("--budget-usd", dest="budget_usd", type=float, required=required)
    parser.add_argument("--max-wakes", dest="max_wakes", type=int, required=required)
    parser.add_argument("--deadline", help="YYYY-MM-DD, or an ISO 8601 time")
    parser.add_argument("--cadence", help="Default check-in interval: 30m, 6h, 2d")
    parser.add_argument("--per-wake-minutes", dest="per_wake_minutes", type=int)
    parser.add_argument("--per-wake-turns", dest="per_wake_turns", type=int)
    parser.add_argument("--model")
    parser.add_argument("--effort")


def _add_wake_assignment(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--assignment", help="The assignment's id or slug; defaults to the wake's own",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="istota-skill assignment", description="Goals Istota carries across days",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="Draft a new assignment")
    _add_charter_options(p, required=True)
    p.add_argument("--auto-complete", dest="auto_complete", action="store_true")

    p = sub.add_parser("set", help="Change the charter")
    p.add_argument("id")
    _add_charter_options(p, required=False)
    p.add_argument("--clear-deadline", dest="clear_deadline", action="store_true")
    p.add_argument("--auto-complete", dest="auto_complete", action=argparse.BooleanOptionalAction,
                   default=None)

    p_item = sub.add_parser("item", help="Done-when items")
    item_sub = p_item.add_subparsers(dest="item_command", required=True)
    p = item_sub.add_parser("add")
    p.add_argument("id")
    p.add_argument("--text", required=True)
    p.add_argument("--kind", required=True, choices=["mechanical", "confirmed"])
    p.add_argument("--check", choices=["file_parses", "csv_columns", "contains"])
    host_path(
        p, "--file", mode=NOT_A_PATH,
        note="stored, never opened here: a name relative to the assignment's own directory, "
             "held by validators.parse_spec to at most four plain parts and opened later by the "
             "daemon through sandbox.confined from the directory it composes",
    )
    p.add_argument("--format", choices=["csv", "json", "toml", "markdown"])
    p.add_argument("--columns", help="Comma-separated column names")
    p.add_argument("--min-rows", dest="min_rows", type=int)
    host_path(
        p, "--text-match", dest="text_match", mode=NOT_A_PATH,
        note="literal text a contains check looks for, not a path",
        help="Literal text the file must contain",
    )
    p.add_argument("--absent", action="store_true", help="The text must not occur")
    p = item_sub.add_parser("remove")
    p.add_argument("id")
    p.add_argument("item_id", type=int)
    p = item_sub.add_parser("confirm")
    p.add_argument("id")
    p.add_argument("item_id", type=int)

    p_frozen = sub.add_parser("frozen", help="Frozen commitments")
    frozen_sub = p_frozen.add_subparsers(dest="frozen_command", required=True)
    p = frozen_sub.add_parser("add")
    p.add_argument("id")
    p.add_argument("--name", required=True)
    p.add_argument("--value", required=True)
    p = frozen_sub.add_parser("remove")
    p.add_argument("id")
    p.add_argument("frozen_id", type=int)

    p = sub.add_parser("activate", help="Ask the user to approve the charter and start it")
    p.add_argument("id")

    for name in ("pause", "resume", "wake"):
        p = sub.add_parser(name)
        p.add_argument("id")
    p = sub.add_parser("abandon")
    p.add_argument("id")
    p.add_argument("--reason")

    p = sub.add_parser("topup", help="Raise the cost or wake cap")
    p.add_argument("id")
    amount = p.add_mutually_exclusive_group(required=True)
    amount.add_argument("--usd", type=float)
    amount.add_argument("--wakes", type=int)

    p = sub.add_parser("note", help="Leave a note for the next wake")
    p.add_argument("id")
    p.add_argument("text")

    # Two verbs share the word: a wake's `handoff "<step>"` and the user's
    # `handoff done <id> <action-id>`, told apart by shape in `_handoff_shape`.
    p = sub.add_parser(
        "handoff",
        help='A wake hands a step to the user: handoff "<step>" --report "<what to bring back>"; '
             "the user reports one done: handoff done <id> <action-id> --report ...",
    )
    p.add_argument("words", nargs="+", metavar="STEP")
    p.add_argument("--report", required=True)
    p.add_argument("--due", help="YYYY-MM-DD, or an ISO 8601 time")
    _add_wake_assignment(p)

    p = sub.add_parser("next", help="A wake says when the assignment wakes next")
    when = p.add_mutually_exclusive_group(required=True)
    when.add_argument("--in", dest="in_", metavar="DURATION", help="30m, 6h, 2d")
    when.add_argument("--at", help="An ISO 8601 time; no offset is the user's timezone")
    when.add_argument("--wait", choices=["user"], help="Wait until the user acts")
    p.add_argument("--progress", help="A note on progress; recorded, not evidence")
    _add_wake_assignment(p)

    p = sub.add_parser("report", help="A wake tells the user about a milestone")
    p.add_argument("text")
    _add_wake_assignment(p)

    p = sub.add_parser("attempt", help="A wake records what it tried")
    host_path(
        p, "--target", mode=NOT_A_PATH, required=True,
        note="a label the host counts repeats by and stores as text; nothing opens it",
        help="What it was tried on: a URL, a file name, a reference",
    )
    p.add_argument("--tried", required=True)
    p.add_argument("--outcome", required=True, choices=["ok", "failed", "partial"])
    _add_wake_assignment(p)

    p = sub.add_parser("complete", help="A wake proposes completion")
    p.add_argument("--evidence", required=True)
    _add_wake_assignment(p)

    p = sub.add_parser("close", help="Answer a proposed completion")
    p.add_argument("id")
    answer = p.add_mutually_exclusive_group(required=True)
    answer.add_argument("--completed", action="store_true")
    answer.add_argument("--reject", metavar="WHAT_IS_MISSING")

    p = sub.add_parser("list", help="Your assignments")
    p.add_argument("--state", choices=STATES)

    p = sub.add_parser("show", help="One assignment in full")
    p.add_argument("id")
    p.add_argument("--wakes", type=int, help=f"Recent wakes to include (default {DEFAULT_SHOW_WAKES})")

    return parser


COMMANDS = {
    "create": cmd_create,
    "set": cmd_set,
    "item add": cmd_item_add,
    "item remove": cmd_item_remove,
    "item confirm": cmd_item_confirm,
    "frozen add": cmd_frozen_add,
    "frozen remove": cmd_frozen_remove,
    "activate": cmd_activate,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "abandon": cmd_abandon,
    "wake": cmd_wake,
    "topup": cmd_topup,
    "note": cmd_note,
    "handoff done": cmd_handoff_done,
    "close": cmd_close,
    "next": cmd_next,
    "handoff": cmd_handoff,
    "report": cmd_report,
    "attempt": cmd_attempt,
    "complete": cmd_complete,
    "list": cmd_list,
    "show": cmd_show,
}


def _handoff_shape(args) -> str:
    """`handoff done <id> <action-id>` as three separate words is the user's
    verb; anything else is a wake's step. A quoted step is one word, so
    `handoff "done 4 7"` stays a step."""
    words = args.words
    if len(words) == 3 and words[0] == "done" and words[2].isdigit():
        args.id, args.action_id = words[1], int(words[2])
        return "handoff done"
    args.step = " ".join(words)
    return "handoff"


def _command_key(args) -> str:
    nested = {"item": "item_command", "frozen": "frozen_command"}
    if args.command in nested:
        return f"{args.command} {getattr(args, nested[args.command])}"
    if args.command == "handoff":
        return _handoff_shape(args)
    return args.command


def _with_db(handler):
    def run(args):
        from istota import db

        gate = _gate()
        with db.get_db(_db_path()) as conn:
            return handler(_Ctx(gate["config"], gate["user_id"], conn), args)

    return run


def main(argv=None):
    args = parse_and_resolve(build_parser(), argv)
    commands = {name: _with_db(handler) for name, handler in COMMANDS.items()}
    run_skill_cli(commands, args, command=_command_key(args), on_exception=_on_exception)
