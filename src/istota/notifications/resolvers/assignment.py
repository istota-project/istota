"""Assignment notices: what an assignment needs the user to see, in the bell.

Six kinds, one source. Four are **object-backed**: a hand-off waiting on the
user (`handoff`), a pause the host set (`paused`), a proposed completion
(`completion`) and a budget past its warning line (`budget_low`). Each stays
open until its condition clears, which `is_open` decides from the assignment's
rows. Two close on being **seen**: a milestone the wake reported (`report`) and
the assignment's completion (`closed`). Routine wakes post nothing.

**Mixed close in one source.** `auto_resolve_on_seen` is per source, so the
source is fire-and-forget and the four object-backed kinds are exempted from
the seen close and the 14-day alert sweep through `kept_until_dismissed`, the
store's existing per-key exemption. What closes them is the producer side:
`settle` runs the same `is_open` predicate the resolver runs and resolves every
row whose condition cleared, from inside the lifecycle transition that cleared
it. The resolver returning `None` is the backstop.

**Keys** are `<kind>:<assignment id>`, plus `:<ref>` for the two kinds with
one row per object: `handoff:<id>:<action id>` and `report:<id>:<event id>`.
A pause, a proposal or a budget warning that comes back after the user cleared
it reopens its row, and a reopen delivers.

**What a push may carry** (`.claude/rules/notifications.md`, #638). The title
names the assignment, which the user set. The body may carry a wake's words (a
hand-off step, a report, a summary, the evidence for a completion) flattened
and capped at `MODEL_TEXT_CHARS`, and stays in the bell: every push is cut to
its title and `PUSH_POINTER` with `_common.pushing_only`, because the alert
route can be a room.

Producers: `wakes.finish_wake` (every kind a wake can cause), the pre-wake
budget pause in `wakes.dispatch_due`, the failed-finish pause, and
`lifecycle.close --completed`, which writes the `closed` row without alert
delivery, since the user is in the chat that closed it.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from . import _common

if TYPE_CHECKING:
    import sqlite3

    from istota.config import Config
    from istota.notifications.sources import NotificationRow, NotificationView
    from istota.notifications.store import RaiseResult

logger = logging.getLogger(__name__)

SOURCE = "assignment"
OBJECT_TYPE = "assignment"

OBJECT_BACKED_KINDS = ("handoff", "paused", "completion", "budget_low")
SEEN_KINDS = ("report", "closed")
KINDS = OBJECT_BACKED_KINDS + SEEN_KINDS
#: Kinds keyed per object as well as per assignment.
_REF_KINDS = ("handoff", "report")

_TITLE_CHARS = 80
MODEL_TEXT_CHARS = 500
_SUMMARY_CHARS = 160

PUSH_POINTER = "Details are in your notifications."

_TITLE_SUFFIX = {
    "handoff": "a step for you",
    "paused": "paused",
    "completion": "says it is done",
    "budget_low": "budget running low",
    "report": "progress report",
    "closed": "completed",
}
_SEVERITY = {
    "handoff": "info",
    "paused": "warning",
    "completion": "info",
    "budget_low": "warning",
    "report": "info",
    "closed": "success",
}
_STATUS_NOTE = {
    "handoff": "Clears when you report the step done in chat.",
    "paused": "Clears when the assignment is resumed, topped up or abandoned.",
    "completion": "Clears when you confirm the completion in chat, or say what is missing.",
    "budget_low": "Clears when the budget is topped up or the assignment ends.",
}
_PAUSE_TEXT = {
    "budget": "Its budget is spent. Top it up in chat to resume it.",
    "stalled": "Its last wakes made no progress that could be measured. Resume it, change the charter, or abandon it.",
    "failures": "Its last wakes failed. Resume it once the cause is fixed, or abandon it.",
    "thrash": "It kept failing on the same step. Resume it with a note on another approach, or abandon it.",
    "deadline": "Its deadline has passed. Set a later deadline and resume it, or abandon it.",
}
_TERMINAL_STATES = ("completed", "abandoned")
_SYSTEM_PAUSE_REASONS = tuple(_PAUSE_TEXT)


def dedup_key(assignment_id: int, kind: str, ref: object = None) -> str:
    if kind not in KINDS:
        raise ValueError(f"not an assignment notice kind: {kind!r}")
    key = _common.object_dedup_key(kind, int(assignment_id))
    if kind in _REF_KINDS:
        key = f"{key}:{int(ref)}"
    return key


def _parse_key(key: str) -> tuple[str, int | None]:
    """`(kind, ref)` from a stored key; `ref` is None for the per-assignment kinds."""
    parts = (key or "").split(":")
    kind = parts[0] if parts else ""
    ref = None
    if kind in _REF_KINDS and len(parts) == 3:
        try:
            ref = int(parts[2])
        except ValueError:
            ref = None
    return kind, ref


def _flat(text: object, limit: int = MODEL_TEXT_CHARS) -> str:
    from istota.confirmations import flatten

    return flatten(str(text or ""))[:limit]


def title_for(assignment_title: str, kind: str) -> str:
    name = _flat(assignment_title, _TITLE_CHARS)
    label = f"Assignment '{name}'" if name else "Assignment"
    return f"{label}: {_TITLE_SUFFIX[kind]}"


# ---------------------------------------------------------------------------
# Bodies, from the assignment's rows
# ---------------------------------------------------------------------------


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _handoff_body(conn, assignment, ref) -> str:
    from istota.assignments import store

    action = store.get_action(conn, assignment.id, int(ref)) if ref is not None else None
    if action is None:
        return "A step was handed to you."
    lines = [f"Step: {_flat(action.text)}"]
    if action.report_spec:
        lines.append(f"Report back: {_flat(action.report_spec)}")
    if action.due_at:
        lines.append(f"Due: {action.due_at[:10]}")
    lines.append("Say so in chat when it is done.")
    return "\n".join(lines)


def _last_pause(conn, assignment) -> dict:
    row = conn.execute(
        "SELECT payload FROM assignment_events WHERE assignment_id = ? AND kind = 'paused'"
        " ORDER BY id DESC LIMIT 1",
        (assignment.id,),
    ).fetchone()
    if row is None:
        return {}
    try:
        payload = json.loads(row[0] or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _paused_body(conn, assignment) -> str:
    from istota.assignments import store

    reason = assignment.state_reason or ""
    detail = _last_pause(conn, assignment)
    lines = [_PAUSE_TEXT.get(reason, "It was paused.")]
    if reason == "failures":
        if detail.get("finish_failed"):
            lines.append("One of its wakes could not be finished.")
        elif detail.get("error"):
            lines.append(f"Last error: {_flat(detail['error'])}")
    elif reason == "thrash" and detail.get("target"):
        lines.append(f"Step: {_flat(detail['target'], 200)}")
    elif reason == "stalled":
        for wake_id in list(detail.get("wake_ids") or [])[:3]:
            try:
                wake = store.get_wake(conn, int(wake_id))
            except (TypeError, ValueError):
                wake = None
            if wake is not None and wake.summary:
                lines.append(f"Wake {wake.seq}: {_flat(wake.summary, _SUMMARY_CHARS)}")
    return "\n".join(lines)


def _completion_body(conn, assignment) -> str:
    row = conn.execute(
        "SELECT complete_request FROM assignment_wakes WHERE assignment_id = ?"
        " AND complete_request IS NOT NULL ORDER BY seq DESC LIMIT 1",
        (assignment.id,),
    ).fetchone()
    lines = ["It reports that its goal is met."]
    if row is not None and row[0]:
        lines.append(f"Evidence: {_flat(row[0])}")
    lines.append("Confirm it in chat to close the assignment, or say what is missing.")
    return "\n".join(lines)


def _spend_line(conn, assignment) -> str:
    from istota.assignments import budget

    spent = budget.totals(conn, assignment.id)
    max_cost = float(assignment.budget.get("max_cost_usd") or 0.0)
    max_wakes = int(assignment.budget.get("max_wakes") or 0)
    return (
        f"It has spent {_money(spent.cost_usd)} of {_money(max_cost)} "
        f"and {spent.wakes} of {max_wakes} wakes."
    )


def _report_body(conn, assignment, ref) -> str:
    row = conn.execute(
        "SELECT payload FROM assignment_events WHERE id = ? AND assignment_id = ? AND kind = 'report'",
        (int(ref), assignment.id),
    ).fetchone() if ref is not None else None
    if row is None:
        return "A progress report."
    try:
        payload = json.loads(row[0] or "{}")
    except ValueError:
        payload = {}
    return _flat(payload.get("text") if isinstance(payload, dict) else "") or "A progress report."


def body_for(conn: "sqlite3.Connection", assignment, kind: str, ref: object = None) -> str:
    if kind == "handoff":
        return _handoff_body(conn, assignment, ref)
    if kind == "paused":
        return _paused_body(conn, assignment)
    if kind == "completion":
        return _completion_body(conn, assignment)
    if kind == "budget_low":
        return _spend_line(conn, assignment) + " Top it up in chat if it needs more."
    if kind == "report":
        return _report_body(conn, assignment, ref)
    if kind == "closed":
        return "The assignment is complete. " + _spend_line(conn, assignment)
    raise ValueError(f"not an assignment notice kind: {kind!r}")


# ---------------------------------------------------------------------------
# Producer
# ---------------------------------------------------------------------------


def write(
    conn: "sqlite3.Connection", assignment, kind: str, ref: object = None,
) -> "RaiseResult | None":
    """Write the row for one notice on the caller's connection.

    Returns the push-ready result (title and `PUSH_POINTER` only), for the
    caller to hand to `deliver_pending` after its transaction commits.
    """
    from istota.notifications.store import write_notification

    result = write_notification(
        conn, assignment.user_id,
        **_common.row_kwargs(
            source=SOURCE,
            dedup_key=dedup_key(assignment.id, kind, ref),
            title=title_for(assignment.title, kind),
            body=body_for(conn, assignment, kind, ref),
            severity=_SEVERITY[kind],
            actionable=kind in ("handoff", "completion"),
            object_type=OBJECT_TYPE,
            object_id=str(assignment.id),
            params={"kind": kind, "ref": ref},
        ),
    )
    return _common.pushing_only(result, PUSH_POINTER)


def write_notices(
    conn: "sqlite3.Connection", assignment_id: int, notices,
) -> list["RaiseResult | None"]:
    """Write every `(kind, ref)` in `notices` for one assignment. Never raises."""
    from istota.assignments import store

    results: list = []
    try:
        assignment = store.get_assignment(conn, assignment_id)
        if assignment is None:
            return results
        for kind, ref in notices:
            results.append(write(conn, assignment, kind, ref))
    except Exception:
        logger.warning("could not write the notices of assignment %s", assignment_id, exc_info=True)
    return results


# ---------------------------------------------------------------------------
# The close predicate, shared by the resolver and `settle`
# ---------------------------------------------------------------------------


def is_open(conn: "sqlite3.Connection", assignment, kind: str, ref: int | None) -> bool:
    """Whether the condition behind a notice still holds.

    The seen kinds always hold while the assignment exists: the seen close
    takes them, and a `None` here would close a report nobody has read.
    """
    if assignment is None:
        return False
    if kind in SEEN_KINDS:
        return True
    if assignment.state in _TERMINAL_STATES:
        return False
    if kind == "handoff":
        from istota.assignments import store

        action = store.get_action(conn, assignment.id, ref) if ref is not None else None
        return action is not None and action.status == "open"
    if kind == "paused":
        return assignment.state == "paused" and assignment.state_reason in _SYSTEM_PAUSE_REASONS
    if kind == "completion":
        return assignment.state == "waiting" and "completion" in (assignment.waiting_on or [])
    if kind == "budget_low":
        return bool(assignment.budget_low_sent)
    return False


def settle(conn: "sqlite3.Connection", assignment_id: int) -> int:
    """Resolve the open object-backed rows of one assignment whose condition
    cleared. Runs on the caller's connection, inside its transaction, so the
    close lands with the transition. Returns the rows closed. Never raises."""
    from istota.assignments import store
    from istota.notifications.store import resolve_notification

    closed = 0
    try:
        assignment = store.get_assignment(conn, assignment_id)
        if assignment is None:
            return 0
        rows = conn.execute(
            "SELECT dedup_key FROM notifications WHERE user_id = ? AND source = ?"
            " AND object_type = ? AND object_id = ? AND state = 'open'",
            (assignment.user_id, SOURCE, OBJECT_TYPE, str(assignment.id)),
        ).fetchall()
        for row in rows:
            kind, ref = _parse_key(row[0])
            if kind not in OBJECT_BACKED_KINDS:
                continue
            if not is_open(conn, assignment, kind, ref):
                closed += resolve_notification(
                    conn, assignment.user_id, SOURCE, row[0], by="system",
                )
    except Exception:
        logger.warning("could not settle the notices of assignment %s", assignment_id, exc_info=True)
    return closed


# ---------------------------------------------------------------------------
# Where a notice sends the user (#848)
# ---------------------------------------------------------------------------

#: Where Open in chat goes when no room of the user's can be named.
CHAT_FALLBACK_HREF = "/chat"


def folder_href(assignment_id: int) -> str:
    """The web app's redirect to the assignment's folder in Files.

    A path rather than the Files page's own `?place=&path=` form, which the
    URL allowlist refuses; the server looks the folder up from the id.
    """
    return f"/files/assignments/{int(assignment_id)}"


def _activation_room(conn, assignment) -> str | None:
    """The room the assignment's approved activation was asked in, while the
    user is a member and can still open it (not archived, not hidden). The
    assignment row records no room."""
    from istota import db
    from istota.rooms.scopes import canonical_token

    row = conn.execute(
        "SELECT origin FROM whatsapp_skill_requests WHERE kind = 'assignment_activation'"
        " AND requester_user_id = ? AND state = 'approved'"
        " AND json_extract(destination, '$.assignment_id') = ?"
        " ORDER BY approved_at DESC, rowid DESC LIMIT 1",
        (assignment.user_id, assignment.id),
    ).fetchone()
    if row is None:
        return None
    try:
        origin = json.loads(row[0] or "{}")
    except ValueError:
        return None
    token = origin.get("room_token") if isinstance(origin, dict) else None
    if not isinstance(token, str) or not token:
        return None
    token = canonical_token(conn, token)
    if token is None or not db.is_room_member(conn, token, assignment.user_id):
        return None
    if db.visible_room(conn, assignment.user_id, token) is None:
        return None
    return token


def chat_href(conn, assignment) -> str:
    """Open in chat: the activation room, else the user's default web room,
    else the chat page, through the `/chat/r/<token>` redirect."""
    from istota import db
    from istota.notifications.sources import is_safe_path

    token = _activation_room(conn, assignment)
    if token is None:
        room = db.default_web_room(conn, assignment.user_id)
        token = room.token if room is not None else None
    if token:
        href = f"/chat/r/{token}"
        if is_safe_path(href):
            return href
    return CHAT_FALLBACK_HREF


def _actions(conn, assignment) -> tuple:
    from istota.notifications.sources import NotificationAction

    return (
        NotificationAction(
            id="open-chat", label="Open in chat", kind="primary", method="LINK",
            href=chat_href(conn, assignment),
        ),
        NotificationAction(
            id="open-folder", label="Open folder", kind="default", method="LINK",
            href=folder_href(assignment.id),
        ),
    )


class AssignmentResolver:
    source = SOURCE
    auto_resolve_on_seen = True
    kept_until_dismissed = tuple(f"{kind}:" for kind in OBJECT_BACKED_KINDS)

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        from istota.assignments import store
        from istota.notifications.sources import NotificationView

        assignment_id = _common.coerce_object_id(row, noun="assignment", logger=logger, positive=True)
        if assignment_id is None:
            return None
        assignment = store.get_assignment(conn, assignment_id, user_id=row.user_id)
        kind, ref = _parse_key(row.dedup_key)
        if kind not in KINDS or not is_open(conn, assignment, kind, ref):
            return None
        body = f"Assignment {assignment.id} · {assignment.slug}"
        if row.body:
            body += "\n" + row.body
        return NotificationView(
            title=row.title,
            body=body,
            severity=row.severity,
            actions=_actions(conn, assignment),
            status_note=_STATUS_NOTE.get(kind),
        )


RESOLVER = AssignmentResolver()
