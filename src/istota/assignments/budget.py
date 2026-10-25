"""An assignment's ledger and the ceilings its budget is held to.

The ledger is read from `task_usage` by `assignment_id`, a column the host
copies from the task row and the model cannot write. `cost_usd` is the
list-price equivalent the usage module computes on every brain, so one number
works across brains.

**What "unmeasured" means here.** `task_usage.cost_usd` is `NOT NULL DEFAULT
0.0`, so an unmeasured run cannot be told apart by its cost. A finished wake is
unmeasured when no usage row for its task has `has_totals = 1`: either the
brain wrote no row at all (`TmuxClaudeBrain`) or the run ended before a result
frame, which `usage.telemetry` records with `has_totals = 0`. Its cost counts
as zero; `max_wakes` still binds, so an unmeasured brain cannot run forever. A
wake that ended `not_active` never reached a brain and is not counted.

The pre-wake and post-wake enforcement steps that move state belong to `wakes`
and `lifecycle`; this module only reads and checks (`check_before_wake` is the
pre-wake check).
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from istota.assignments import store
from istota.assignments.charter import CharterError

if TYPE_CHECKING:
    from istota.config import AssignmentsConfig

#: States that hold one of the user's `max_active_per_user` places.
COUNTED_STATES = ("sleeping", "active", "waiting", "paused")


@dataclass(frozen=True)
class Totals:
    cost_usd: float
    wakes: int
    unmeasured_wakes: int
    billed_input_tokens: int
    output_tokens: int


def totals(conn: sqlite3.Connection, assignment_id: int) -> Totals:
    """What the assignment has spent, from `task_usage` and `assignment_wakes`."""
    usage = conn.execute(
        """
        SELECT COALESCE(SUM(cost_usd), 0.0) AS cost,
               COALESCE(SUM(CASE WHEN has_totals = 1 THEN billed_input_tokens ELSE 0 END), 0) AS input,
               COALESCE(SUM(CASE WHEN has_totals = 1 THEN output_tokens ELSE 0 END), 0) AS output
        FROM task_usage WHERE assignment_id = ?
        """,
        (assignment_id,),
    ).fetchone()
    unmeasured = conn.execute(
        """
        SELECT COUNT(*) FROM assignment_wakes w
        WHERE w.assignment_id = ? AND w.ended_at IS NOT NULL
          AND COALESCE(w.outcome, '') != 'not_active'
          AND NOT EXISTS (
              SELECT 1 FROM task_usage u
              WHERE u.assignment_id = w.assignment_id AND u.task_id = w.task_id
                AND u.has_totals = 1
          )
        """,
        (assignment_id,),
    ).fetchone()
    return Totals(
        cost_usd=float(usage["cost"]),
        wakes=store.count_wakes(conn, assignment_id),
        unmeasured_wakes=int(unmeasured[0]),
        billed_input_tokens=int(usage["input"]),
        output_tokens=int(usage["output"]),
    )


def remaining(assignment: store.Assignment, spent: Totals) -> dict[str, Any]:
    """Cost and wakes left under the charter's caps, floored at zero."""
    max_cost = float(assignment.budget.get("max_cost_usd") or 0.0)
    max_wakes = int(assignment.budget.get("max_wakes") or 0)
    return {
        "cost_usd": round(max(0.0, max_cost - spent.cost_usd), 4),
        "wakes": max(0, max_wakes - spent.wakes),
    }


def is_spent(assignment: store.Assignment, spent: Totals) -> bool:
    """Whether cost or wakes have reached the charter's cap."""
    max_cost = float(assignment.budget.get("max_cost_usd") or 0.0)
    max_wakes = int(assignment.budget.get("max_wakes") or 0)
    return spent.cost_usd >= max_cost or spent.wakes >= max_wakes


def check_before_wake(conn: sqlite3.Connection, assignment: store.Assignment) -> bool:
    """Whether the ledger leaves room for one more wake. The caller pauses on False."""
    return not is_spent(assignment, totals(conn, assignment.id))


def topped_up(
    cfg: "AssignmentsConfig",
    budget: dict[str, Any],
    *,
    usd: float | None = None,
    wakes: int | None = None,
) -> dict[str, Any]:
    """The budget with `usd` and `wakes` added to its caps, held to the ceilings.

    Raises `CharterError` (`invalid_charter`, `over_ceiling`). The caller
    passes exactly one of the two.
    """
    new = dict(budget)
    if usd is not None:
        if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not math.isfinite(usd) or usd <= 0:
            raise CharterError("invalid_charter", "a top-up must add more than zero dollars")
        new["max_cost_usd"] = round(float(budget.get("max_cost_usd") or 0.0) + float(usd), 4)
        if new["max_cost_usd"] > cfg.max_cost_usd_per_assignment:
            raise CharterError(
                "over_ceiling",
                f"the cost budget would pass this deployment's ceiling of "
                f"${cfg.max_cost_usd_per_assignment:.2f}",
            )
    if wakes is not None:
        if isinstance(wakes, bool) or not isinstance(wakes, int) or wakes < 1:
            raise CharterError("invalid_charter", "a top-up must add at least one wake")
        new["max_wakes"] = int(budget.get("max_wakes") or 0) + wakes
        if new["max_wakes"] > cfg.max_wakes_per_assignment:
            raise CharterError(
                "over_ceiling",
                f"max_wakes would pass this deployment's ceiling of {cfg.max_wakes_per_assignment}",
            )
    return new


def counted_assignments(
    conn: sqlite3.Connection, user_id: str, *, excluding: int | None = None,
) -> int:
    """The user's assignments holding a `max_active_per_user` place."""
    placeholders = ", ".join("?" for _ in COUNTED_STATES)
    sql = f"SELECT COUNT(*) FROM assignments WHERE user_id = ? AND state IN ({placeholders})"
    params: list[Any] = [user_id, *COUNTED_STATES]
    if excluding is not None:
        sql += " AND id != ?"
        params.append(excluding)
    return int(conn.execute(sql, params).fetchone()[0])


def check_active_limit(
    conn: sqlite3.Connection, cfg: "AssignmentsConfig", user_id: str, assignment_id: int,
) -> None:
    """Raise `CharterError("too_many_active")` when `assignment_id` would pass the cap."""
    others = counted_assignments(conn, user_id, excluding=assignment_id)
    if others >= cfg.max_active_per_user:
        raise CharterError(
            "too_many_active",
            f"this deployment allows {cfg.max_active_per_user} running assignments per user; "
            "pause is not enough, abandon or finish one first",
        )
