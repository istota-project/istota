"""Row access for the assignment tables. Plain functions on a caller's connection.

No policy lives here: which state may follow which, who may write the charter,
and when a wake is due belong to `lifecycle`, `caller` and `wakes`. This module
reads and writes rows, decodes the JSON columns, and commits nothing; the
caller owns the transaction.

`set_state` is the one function that writes `assignments.state`, and only
`lifecycle` calls it. `update_assignment` refuses the column, so the rule that
one module moves an assignment between states is visible at every call site.

Timestamps are ISO-Z (`db.iso_utc_now`), the format the tables' defaults use.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from istota import db


@dataclass(frozen=True)
class Assignment:
    id: int
    user_id: str
    slug: str
    title: str
    goal: str
    autonomy: str
    state: str
    state_reason: str | None
    waiting_on: list[str]
    budget: dict[str, Any]
    budget_low_sent: bool
    deadline: str | None
    cadence_minutes: int
    auto_complete: bool
    model: str | None
    effort: str | None
    brain: str | None
    settings: dict[str, Any]
    dir_relpath: str
    next_wake_at: str | None
    created_at: str
    updated_at: str
    activated_at: str | None
    closed_at: str | None


@dataclass(frozen=True)
class Item:
    id: int
    assignment_id: int
    position: int
    text: str
    kind: str
    validator: dict[str, Any] | None
    status: str
    checked_at: str | None
    detail: str | None


@dataclass(frozen=True)
class Frozen:
    id: int
    assignment_id: int
    name: str
    value: str


@dataclass(frozen=True)
class Wake:
    id: int
    assignment_id: int
    seq: int
    task_id: int | None
    kind: str
    fired_by: str
    created_at: str
    started_at: str | None
    ended_at: str | None
    outcome: str | None
    next_request: dict[str, Any] | None
    complete_request: str | None
    progress_note: str | None
    summary: str | None
    progress_signals: list[str]
    cost_usd: float | None


@dataclass(frozen=True)
class Snapshot:
    id: int
    assignment_id: int
    wake_id: int | None
    phase: str
    sha256: str
    content: str
    truncated: bool
    created_at: str


@dataclass(frozen=True)
class InboxEvent:
    id: int
    assignment_id: int
    kind: str
    trusted: bool
    payload: Any
    created_at: str
    consumed_by_wake: int | None


@dataclass(frozen=True)
class Action:
    id: int
    assignment_id: int
    wake_id: int | None
    kind: str
    text: str
    report_spec: str | None
    status: str
    due_at: str | None
    created_at: str
    resolved_at: str | None
    response: str | None


@dataclass(frozen=True)
class Attempt:
    id: int
    assignment_id: int
    wake_id: int | None
    target: str
    tried: str
    outcome: str
    created_at: str


@dataclass(frozen=True)
class Event:
    id: int
    assignment_id: int
    wake_id: int | None
    kind: str
    actor: str
    payload: Any
    created_at: str


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    return json.loads(text)


def _placeholders(values: Iterable[Any]) -> str:
    return ", ".join("?" for _ in values)


# ---------------------------------------------------------------------------
# assignments
# ---------------------------------------------------------------------------


def _assignment(row: sqlite3.Row) -> Assignment:
    return Assignment(
        id=row["id"],
        user_id=row["user_id"],
        slug=row["slug"],
        title=row["title"],
        goal=row["goal"],
        autonomy=row["autonomy"],
        state=row["state"],
        state_reason=row["state_reason"],
        waiting_on=_loads(row["waiting_on"], []),
        budget=_loads(row["budget"], {}),
        budget_low_sent=bool(row["budget_low_sent"]),
        deadline=row["deadline"],
        cadence_minutes=row["cadence_minutes"],
        auto_complete=bool(row["auto_complete"]),
        model=row["model"],
        effort=row["effort"],
        brain=row["brain"],
        settings=_loads(row["settings"], {}),
        dir_relpath=row["dir_relpath"],
        next_wake_at=row["next_wake_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        activated_at=row["activated_at"],
        closed_at=row["closed_at"],
    )


def insert_assignment(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    slug: str,
    title: str,
    goal: str,
    budget: dict[str, Any],
    dir_relpath: str,
    autonomy: str = "",
    deadline: str | None = None,
    cadence_minutes: int = 360,
    auto_complete: bool = False,
    model: str | None = None,
    effort: str | None = None,
    brain: str | None = None,
    settings: dict[str, Any] | None = None,
) -> int:
    """Insert a `draft` assignment and return its id."""
    cursor = conn.execute(
        """
        INSERT INTO assignments (
            user_id, slug, title, goal, autonomy, budget, deadline,
            cadence_minutes, auto_complete, model, effort, brain, settings,
            dir_relpath
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id, slug, title, goal, autonomy, _dumps(budget), deadline,
            cadence_minutes, 1 if auto_complete else 0, model or None,
            effort or None, brain or None, _dumps(settings or {}), dir_relpath,
        ),
    )
    return int(cursor.lastrowid)


def get_assignment(
    conn: sqlite3.Connection, assignment_id: int, *, user_id: str | None = None,
) -> Assignment | None:
    """One assignment by id; with `user_id`, only if that user owns it."""
    sql = "SELECT * FROM assignments WHERE id = ?"
    params: list[Any] = [assignment_id]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    row = conn.execute(sql, params).fetchone()
    return _assignment(row) if row else None


def get_assignment_by_slug(
    conn: sqlite3.Connection, user_id: str, slug: str,
) -> Assignment | None:
    row = conn.execute(
        "SELECT * FROM assignments WHERE user_id = ? AND slug = ?", (user_id, slug),
    ).fetchone()
    return _assignment(row) if row else None


def list_assignments(
    conn: sqlite3.Connection,
    user_id: str | None = None,
    *,
    states: Iterable[str] | None = None,
) -> list[Assignment]:
    """Assignments in id order, for one user or all, optionally by state."""
    clauses: list[str] = []
    params: list[Any] = []
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if states is not None:
        states = list(states)
        clauses.append(f"state IN ({_placeholders(states)})")
        params.extend(states)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM assignments{where} ORDER BY id", params).fetchall()
    return [_assignment(row) for row in rows]


#: Columns `update_assignment` writes, and how each is encoded. `state`,
#: `state_reason` and `waiting_on` move together through `set_state`, except
#: that `waiting_on` may also be set alone; identity columns are never updated.
_ASSIGNMENT_UPDATABLE: dict[str, Any] = {
    "title": None,
    "goal": None,
    "autonomy": None,
    "budget": _dumps,
    "budget_low_sent": lambda v: 1 if v else 0,
    "deadline": None,
    "cadence_minutes": None,
    "auto_complete": lambda v: 1 if v else 0,
    "model": None,
    "effort": None,
    "brain": None,
    "settings": _dumps,
    "waiting_on": _dumps,
    "next_wake_at": None,
    "activated_at": None,
    "closed_at": None,
}


def update_assignment(conn: sqlite3.Connection, assignment_id: int, **fields: Any) -> None:
    """Write the named columns and `updated_at`. Raises ValueError on any other name."""
    unknown = sorted(set(fields) - set(_ASSIGNMENT_UPDATABLE))
    if unknown:
        raise ValueError(f"update_assignment cannot write {unknown}")
    if not fields:
        return
    columns: list[str] = []
    params: list[Any] = []
    for name, value in fields.items():
        encode = _ASSIGNMENT_UPDATABLE[name]
        columns.append(f"{name} = ?")
        params.append(encode(value) if encode is not None and value is not None else value)
    columns.append("updated_at = ?")
    params.append(db.iso_utc_now())
    params.append(assignment_id)
    conn.execute(f"UPDATE assignments SET {', '.join(columns)} WHERE id = ?", params)


def set_state(
    conn: sqlite3.Connection,
    assignment_id: int,
    state: str,
    *,
    reason: str | None = None,
    waiting_on: list[str] | None = None,
) -> None:
    """Write `state`, `state_reason` and `waiting_on` together. `lifecycle` only."""
    conn.execute(
        "UPDATE assignments SET state = ?, state_reason = ?, waiting_on = ?, updated_at = ?"
        " WHERE id = ?",
        (state, reason, _dumps(waiting_on) if waiting_on else None, db.iso_utc_now(), assignment_id),
    )


# ---------------------------------------------------------------------------
# assignment_items
# ---------------------------------------------------------------------------


def _item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        assignment_id=row["assignment_id"],
        position=row["position"],
        text=row["text"],
        kind=row["kind"],
        validator=_loads(row["validator"]),
        status=row["status"],
        checked_at=row["checked_at"],
        detail=row["detail"],
    )


def insert_item(
    conn: sqlite3.Connection,
    assignment_id: int,
    *,
    text: str,
    kind: str,
    validator: dict[str, Any] | None = None,
) -> int:
    """Append a done-when item after the last one and return its id."""
    cursor = conn.execute(
        """
        INSERT INTO assignment_items (assignment_id, position, text, kind, validator)
        SELECT ?, COALESCE(MAX(position), 0) + 1, ?, ?, ?
        FROM assignment_items WHERE assignment_id = ?
        """,
        (assignment_id, text, kind, _dumps(validator) if validator is not None else None, assignment_id),
    )
    return int(cursor.lastrowid)


def get_item(conn: sqlite3.Connection, assignment_id: int, item_id: int) -> Item | None:
    row = conn.execute(
        "SELECT * FROM assignment_items WHERE id = ? AND assignment_id = ?", (item_id, assignment_id),
    ).fetchone()
    return _item(row) if row else None


def list_items(conn: sqlite3.Connection, assignment_id: int) -> list[Item]:
    rows = conn.execute(
        "SELECT * FROM assignment_items WHERE assignment_id = ? ORDER BY position, id", (assignment_id,),
    ).fetchall()
    return [_item(row) for row in rows]


def delete_item(conn: sqlite3.Connection, assignment_id: int, item_id: int) -> bool:
    cursor = conn.execute(
        "DELETE FROM assignment_items WHERE id = ? AND assignment_id = ?", (item_id, assignment_id),
    )
    return cursor.rowcount > 0


def set_item_status(
    conn: sqlite3.Connection,
    item_id: int,
    status: str,
    *,
    detail: str | None = None,
    checked_at: str | None = None,
) -> None:
    conn.execute(
        "UPDATE assignment_items SET status = ?, detail = ?, checked_at = ? WHERE id = ?",
        (status, detail, checked_at, item_id),
    )


# ---------------------------------------------------------------------------
# assignment_frozen
# ---------------------------------------------------------------------------


def insert_frozen(conn: sqlite3.Connection, assignment_id: int, *, name: str, value: str) -> int:
    cursor = conn.execute(
        "INSERT INTO assignment_frozen (assignment_id, name, value) VALUES (?, ?, ?)",
        (assignment_id, name, value),
    )
    return int(cursor.lastrowid)


def list_frozen(conn: sqlite3.Connection, assignment_id: int) -> list[Frozen]:
    rows = conn.execute(
        "SELECT * FROM assignment_frozen WHERE assignment_id = ? ORDER BY id", (assignment_id,),
    ).fetchall()
    return [Frozen(id=r["id"], assignment_id=r["assignment_id"], name=r["name"], value=r["value"]) for r in rows]


def delete_frozen(conn: sqlite3.Connection, assignment_id: int, frozen_id: int) -> bool:
    cursor = conn.execute(
        "DELETE FROM assignment_frozen WHERE id = ? AND assignment_id = ?", (frozen_id, assignment_id),
    )
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# assignment_wakes
# ---------------------------------------------------------------------------


def _wake(row: sqlite3.Row) -> Wake:
    return Wake(
        id=row["id"],
        assignment_id=row["assignment_id"],
        seq=row["seq"],
        task_id=row["task_id"],
        kind=row["kind"],
        fired_by=row["fired_by"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        outcome=row["outcome"],
        next_request=_loads(row["next_request"]),
        complete_request=row["complete_request"],
        progress_note=row["progress_note"],
        summary=row["summary"],
        progress_signals=_loads(row["progress_signals"], []),
        cost_usd=row["cost_usd"],
    )


def insert_wake(
    conn: sqlite3.Connection,
    assignment_id: int,
    *,
    fired_by: str,
    task_id: int | None = None,
    kind: str = "work",
) -> int:
    """Insert the assignment's next wake; `seq` is assigned in the same statement."""
    cursor = conn.execute(
        """
        INSERT INTO assignment_wakes (assignment_id, seq, task_id, kind, fired_by)
        SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?
        FROM assignment_wakes WHERE assignment_id = ?
        """,
        (assignment_id, task_id, kind, fired_by, assignment_id),
    )
    return int(cursor.lastrowid)


def get_wake(conn: sqlite3.Connection, wake_id: int) -> Wake | None:
    row = conn.execute("SELECT * FROM assignment_wakes WHERE id = ?", (wake_id,)).fetchone()
    return _wake(row) if row else None


def get_wake_by_task(conn: sqlite3.Connection, task_id: int) -> Wake | None:
    row = conn.execute(
        "SELECT * FROM assignment_wakes WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return _wake(row) if row else None


def open_wake(conn: sqlite3.Connection, assignment_id: int) -> Wake | None:
    """The assignment's wake that has not ended, if any."""
    row = conn.execute(
        "SELECT * FROM assignment_wakes WHERE assignment_id = ? AND ended_at IS NULL"
        " ORDER BY seq DESC LIMIT 1",
        (assignment_id,),
    ).fetchone()
    return _wake(row) if row else None


def list_wakes(conn: sqlite3.Connection, assignment_id: int, *, limit: int | None = None) -> list[Wake]:
    """Newest first."""
    sql = "SELECT * FROM assignment_wakes WHERE assignment_id = ? ORDER BY seq DESC"
    params: list[Any] = [assignment_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_wake(row) for row in conn.execute(sql, params).fetchall()]


def count_wakes(conn: sqlite3.Connection, assignment_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM assignment_wakes WHERE assignment_id = ?", (assignment_id,),
    ).fetchone()
    return int(row[0])


_WAKE_UPDATABLE: dict[str, Any] = {
    "task_id": None,
    "started_at": None,
    "ended_at": None,
    "outcome": None,
    "next_request": _dumps,
    "complete_request": None,
    "progress_note": None,
    "summary": None,
    "progress_signals": _dumps,
    "cost_usd": None,
}


def update_wake(conn: sqlite3.Connection, wake_id: int, **fields: Any) -> None:
    """Write the named columns. Raises ValueError on any other name."""
    unknown = sorted(set(fields) - set(_WAKE_UPDATABLE))
    if unknown:
        raise ValueError(f"update_wake cannot write {unknown}")
    if not fields:
        return
    columns: list[str] = []
    params: list[Any] = []
    for name, value in fields.items():
        encode = _WAKE_UPDATABLE[name]
        columns.append(f"{name} = ?")
        params.append(encode(value) if encode is not None and value is not None else value)
    params.append(wake_id)
    conn.execute(f"UPDATE assignment_wakes SET {', '.join(columns)} WHERE id = ?", params)


# ---------------------------------------------------------------------------
# assignment_snapshots
# ---------------------------------------------------------------------------


def insert_snapshot(
    conn: sqlite3.Connection,
    assignment_id: int,
    *,
    phase: str,
    content: str,
    sha256: str,
    wake_id: int | None = None,
    truncated: bool = False,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO assignment_snapshots (assignment_id, wake_id, phase, sha256, content, truncated)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (assignment_id, wake_id, phase, sha256, content, 1 if truncated else 0),
    )
    return int(cursor.lastrowid)


def latest_snapshot(
    conn: sqlite3.Connection, assignment_id: int, *, phases: Iterable[str] | None = None,
) -> Snapshot | None:
    """The most recent snapshot, optionally restricted to some phases."""
    sql = "SELECT * FROM assignment_snapshots WHERE assignment_id = ?"
    params: list[Any] = [assignment_id]
    if phases is not None:
        phases = list(phases)
        sql += f" AND phase IN ({_placeholders(phases)})"
        params.extend(phases)
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
    if row is None:
        return None
    return Snapshot(
        id=row["id"],
        assignment_id=row["assignment_id"],
        wake_id=row["wake_id"],
        phase=row["phase"],
        sha256=row["sha256"],
        content=row["content"],
        truncated=bool(row["truncated"]),
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# assignment_inbox
# ---------------------------------------------------------------------------


def _inbox_event(row: sqlite3.Row) -> InboxEvent:
    return InboxEvent(
        id=row["id"],
        assignment_id=row["assignment_id"],
        kind=row["kind"],
        trusted=bool(row["trusted"]),
        payload=_loads(row["payload"]),
        created_at=row["created_at"],
        consumed_by_wake=row["consumed_by_wake"],
    )


def insert_inbox_event(
    conn: sqlite3.Connection,
    assignment_id: int,
    *,
    kind: str,
    payload: Any,
    trusted: bool = False,
) -> int:
    """Queue an event for the next wake. Untrusted unless the caller says so."""
    cursor = conn.execute(
        "INSERT INTO assignment_inbox (assignment_id, kind, trusted, payload) VALUES (?, ?, ?, ?)",
        (assignment_id, kind, 1 if trusted else 0, _dumps(payload)),
    )
    return int(cursor.lastrowid)


def list_inbox(
    conn: sqlite3.Connection,
    assignment_id: int,
    *,
    unconsumed: bool = False,
    consumed_by_wake: int | None = None,
) -> list[InboxEvent]:
    """Events in arrival order: all, only the unconsumed, or those one wake claimed."""
    sql = "SELECT * FROM assignment_inbox WHERE assignment_id = ?"
    params: list[Any] = [assignment_id]
    if unconsumed:
        sql += " AND consumed_by_wake IS NULL"
    if consumed_by_wake is not None:
        sql += " AND consumed_by_wake = ?"
        params.append(consumed_by_wake)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [_inbox_event(row) for row in rows]


def has_unconsumed(
    conn: sqlite3.Connection, assignment_id: int, *, kinds: Iterable[str] | None = None,
) -> bool:
    sql = "SELECT 1 FROM assignment_inbox WHERE assignment_id = ? AND consumed_by_wake IS NULL"
    params: list[Any] = [assignment_id]
    if kinds is not None:
        kinds = list(kinds)
        sql += f" AND kind IN ({_placeholders(kinds)})"
        params.extend(kinds)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def consume_inbox(conn: sqlite3.Connection, assignment_id: int, wake_id: int) -> int:
    """Claim every unconsumed event for a wake. Returns how many it claimed."""
    cursor = conn.execute(
        "UPDATE assignment_inbox SET consumed_by_wake = ?"
        " WHERE assignment_id = ? AND consumed_by_wake IS NULL",
        (wake_id, assignment_id),
    )
    return cursor.rowcount


# ---------------------------------------------------------------------------
# assignment_actions
# ---------------------------------------------------------------------------


def _action(row: sqlite3.Row) -> Action:
    return Action(
        id=row["id"],
        assignment_id=row["assignment_id"],
        wake_id=row["wake_id"],
        kind=row["kind"],
        text=row["text"],
        report_spec=row["report_spec"],
        status=row["status"],
        due_at=row["due_at"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
        response=row["response"],
    )


def insert_action(
    conn: sqlite3.Connection,
    assignment_id: int,
    *,
    text: str,
    report_spec: str | None = None,
    due_at: str | None = None,
    wake_id: int | None = None,
    kind: str = "handoff",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO assignment_actions (assignment_id, wake_id, kind, text, report_spec, due_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (assignment_id, wake_id, kind, text, report_spec, due_at),
    )
    return int(cursor.lastrowid)


def get_action(conn: sqlite3.Connection, assignment_id: int, action_id: int) -> Action | None:
    row = conn.execute(
        "SELECT * FROM assignment_actions WHERE id = ? AND assignment_id = ?", (action_id, assignment_id),
    ).fetchone()
    return _action(row) if row else None


def list_actions(
    conn: sqlite3.Connection, assignment_id: int, *, status: str | None = None,
) -> list[Action]:
    sql = "SELECT * FROM assignment_actions WHERE assignment_id = ?"
    params: list[Any] = [assignment_id]
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    return [_action(row) for row in conn.execute(sql + " ORDER BY id", params).fetchall()]


def resolve_action(
    conn: sqlite3.Connection, assignment_id: int, action_id: int, *, response: str,
) -> bool:
    """Mark an open action done. False when it was not open (or not this assignment's)."""
    cursor = conn.execute(
        "UPDATE assignment_actions SET status = 'done', response = ?, resolved_at = ?"
        " WHERE id = ? AND assignment_id = ? AND status = 'open'",
        (response, db.iso_utc_now(), action_id, assignment_id),
    )
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# assignment_attempts
# ---------------------------------------------------------------------------


def insert_attempt(
    conn: sqlite3.Connection,
    assignment_id: int,
    *,
    target: str,
    tried: str,
    outcome: str,
    wake_id: int | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO assignment_attempts (assignment_id, wake_id, target, tried, outcome)"
        " VALUES (?, ?, ?, ?, ?)",
        (assignment_id, wake_id, target, tried, outcome),
    )
    return int(cursor.lastrowid)


def list_attempts(
    conn: sqlite3.Connection,
    assignment_id: int,
    *,
    target: str | None = None,
    limit: int | None = None,
) -> list[Attempt]:
    """Newest first, optionally for one target."""
    sql = "SELECT * FROM assignment_attempts WHERE assignment_id = ?"
    params: list[Any] = [assignment_id]
    if target is not None:
        sql += " AND target = ?"
        params.append(target)
    sql += " ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [
        Attempt(
            id=r["id"], assignment_id=r["assignment_id"], wake_id=r["wake_id"],
            target=r["target"], tried=r["tried"], outcome=r["outcome"], created_at=r["created_at"],
        )
        for r in conn.execute(sql, params).fetchall()
    ]


# ---------------------------------------------------------------------------
# assignment_events
# ---------------------------------------------------------------------------


def insert_event(
    conn: sqlite3.Connection,
    assignment_id: int,
    *,
    kind: str,
    actor: str,
    payload: Any = None,
    wake_id: int | None = None,
) -> int:
    """Append an audit row. The table is append-only; nothing here updates it."""
    cursor = conn.execute(
        "INSERT INTO assignment_events (assignment_id, wake_id, kind, actor, payload)"
        " VALUES (?, ?, ?, ?, ?)",
        (assignment_id, wake_id, kind, actor, _dumps(payload if payload is not None else {})),
    )
    return int(cursor.lastrowid)


def list_events(conn: sqlite3.Connection, assignment_id: int) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM assignment_events WHERE assignment_id = ? ORDER BY id", (assignment_id,),
    ).fetchall()
    return [
        Event(
            id=r["id"], assignment_id=r["assignment_id"], wake_id=r["wake_id"], kind=r["kind"],
            actor=r["actor"], payload=_loads(r["payload"], {}), created_at=r["created_at"],
        )
        for r in rows
    ]
