"""One assignment as a reader sees it: the board `assignment show` prints and the
Assignments page draws, so the two cannot hold different copies of it.

Plain functions over the caller's connection. They import inside the function, as the
skill's handlers do, because the skills package imports every skill and this module with
it, and a skill CLI's start should not pay for the wake machinery.
"""

from __future__ import annotations

from typing import Any

DEFAULT_SHOW_WAKES = 3
MAX_SHOW_WAKES = 20
SUMMARY_SHOW_CHARS = 2000


def summary(assignment) -> dict:
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


def rewrite(conn, config, assignment_id: int) -> str:
    """Commit, then re-render `CHARTER.md`; the DB is the authority either way."""
    from istota.assignments import charter

    conn.commit()
    return charter.rewrite_charter_file(config, conn, assignment_id)


def wake_due(conn, config, assignment_id: int) -> str | None:
    """Whether the next gate tick wakes it, and why: a wait an inbox event has
    already ended reads `inbox`, not stuck (ISSUE-853)."""
    from istota.assignments import store, wakes

    current = store.get_assignment(conn, assignment_id)
    return wakes.wake_due(conn, config, current) if current is not None else None


def state_result(conn, config, assignment, **extra: Any) -> dict:
    conn.commit()
    return {
        "status": "ok", "assignment": summary(assignment),
        "wake_due": wake_due(conn, config, assignment.id), **extra,
    }


def changed(conn, config, assignment, **extra: Any) -> dict:
    from istota.assignments import store

    rendered = rewrite(conn, config, assignment.id)
    current = store.get_assignment(conn, assignment.id)
    return {
        "status": "ok", "assignment": summary(current),
        "wake_due": wake_due(conn, config, assignment.id),
        "charter": rendered, **extra,
    }


def still_paused(conn, assignment) -> dict:
    """What a top-up answers beside its result when a budget pause stays in place,
    because it left less than a wake costs (ISSUE-853). Empty otherwise."""
    from istota.assignments import budget

    if assignment.state != "paused" or assignment.state_reason != "budget":
        return {}
    spent = budget.totals(conn, assignment.id)
    return {
        "still_paused": {
            "remaining": budget.remaining(assignment, spent),
            "cheapest_wake_usd": spent.cheapest_wake_usd,
        },
    }


def ledger(conn, assignment) -> dict:
    from istota.assignments import budget

    spent = budget.totals(conn, assignment.id)
    return {
        "cost_usd": round(spent.cost_usd, 4),
        "wakes": spent.wakes,
        "unmeasured_wakes": spent.unmeasured_wakes,
        "billed_input_tokens": spent.billed_input_tokens,
        "output_tokens": spent.output_tokens,
        "remaining": budget.remaining(assignment, spent),
        "cheapest_wake_usd": spent.cheapest_wake_usd,
    }


def show(conn, config, assignment, *, wake_count: int | None = None) -> dict:
    from istota.assignments import charter, store
    from istota.assignments.prompt import UNTRUSTED_NOTICE

    limit = max(0, min(wake_count if wake_count is not None else DEFAULT_SHOW_WAKES, MAX_SHOW_WAKES))
    items = store.list_items(conn, assignment.id)
    frozen = store.list_frozen(conn, assignment.id)
    recent = store.list_wakes(conn, assignment.id, limit=limit) if limit else []
    return {
        "assignment": summary(assignment),
        "wake_due": wake_due(conn, config, assignment.id),
        "charter": charter.render(assignment, items, frozen),
        "ledger": ledger(conn, assignment),
        "items": [
            {"id": i.id, "position": i.position, "kind": i.kind, "text": i.text, "status": i.status,
             "detail": i.detail, "checked_at": i.checked_at}
            for i in items
        ],
        "frozen": [{"id": f.id, "name": f.name, "value": f.value} for f in frozen],
        "open_handoffs": [
            {"id": a.id, "text": a.text, "report": a.report_spec, "due_at": a.due_at, "created_at": a.created_at}
            for a in store.list_actions(conn, assignment.id, status="open")
        ],
        "wakes": [
            {"seq": w.seq, "fired_by": w.fired_by, "started_at": w.started_at, "ended_at": w.ended_at,
             "outcome": w.outcome, "cost_usd": w.cost_usd, "progress_signals": w.progress_signals,
             "summary": (w.summary or "")[:SUMMARY_SHOW_CHARS] or None,
             "summary_truncated": len(w.summary or "") > SUMMARY_SHOW_CHARS}
            for w in recent
        ],
        "notice": UNTRUSTED_NOTICE,
    }
