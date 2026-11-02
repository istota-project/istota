"""Settings > Assignments: a user's own assignments, read and steered.

Own assignments only, taken from the session: no parameter names a user, every lookup
passes the user, and somebody else's id is the same 404 as a missing one. Bodies are read
bounded and parsed by hand, because a framework validation error echoes its input. No log
line carries a note, a report or a hand-off.

A page action is a signed-in person acting on their own record, which is what
`caller.UserTurn` establishes from a task row, so no `classify` runs and the session plus
the origin check are the authority. Each write copies its CLI verb's calls and order.
Nothing here writes a charter's fields or activates: a draft is a conversation, and
activation rides the relay ledger.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from istota import db
from istota.assignments import charter, lifecycle, store, view
from istota.notifications.resolvers import assignment as notices
from istota.rooms import egress
from istota.webui.request_guards import BodyError, read_bounded_body
from istota.webui.router_stubs import require_auth, verify_origin
from istota.webui.settings_http import NO_STORE, not_found, parse_id, refusal, request_config, usable_external_skills

logger = logging.getLogger(__name__)

router = APIRouter()

SKILL = "assignment"
_MAX_BODY_BYTES = 16 * 1024


def can_use(config, user_id: str) -> bool:
    """The one gate, for every route and `/me`: what `_guard_skill` plus the admin check
    amount to in the CLI. A page's feature flag only decides what is shown."""
    from istota.skills._loader import load_skill_index, user_skill_names

    try:
        index = load_skill_index(config.skills_dir, bundled_dir=config.bundled_skills_dir)
        return SKILL in user_skill_names(config, user_id, index, count_user_disabled=True)
    except Exception as exc:  # noqa: BLE001 — closed, and `/me` must still answer
        logger.warning("assignment gate unavailable: %s", type(exc).__name__)
        return False


def _invalid(message: str) -> lifecycle.TransitionError:
    return lifecycle.TransitionError("invalid_input", message)


def _refused(code: str, message: str, state: str | None = None) -> JSONResponse:
    # Which item a path named is not something to tell apart from no assignment at all.
    if code == "not_found":
        return not_found()
    return refusal(409 if code == "bad_state" else 400, code, message, state=state)


def _answer(work, config, user_id: str, assignment_id: int, body: dict) -> JSONResponse:
    """Run one action on the caller's own assignment, on its own connection."""
    try:
        with db.get_db(Path(config.db_path)) as conn:
            assignment = store.get_assignment(conn, assignment_id, user_id=user_id)
            if assignment is None:
                return not_found()
            result = work(conn, config, assignment, body)
    except lifecycle.TransitionError as exc:
        return _refused(exc.code, exc.message, exc.state)
    except charter.CharterError as exc:
        return _refused(exc.code, exc.message)
    except ValueError as exc:
        logger.warning("assignment action refused: %s", type(exc).__name__)
        return refusal(400, "invalid", "That could not be done.")
    except sqlite3.Error as exc:
        logger.error("assignment action failed: %s", type(exc).__name__)
        return refusal(500, "error", "That could not be done.")
    logger.info(
        "assignment_web operation=%s user=%s assignment_id=%s",
        getattr(work, "func", work).__name__.lstrip("_"), user_id, assignment_id,
    )
    return JSONResponse(result, headers=NO_STORE)


async def _read_body(request: Request) -> dict | JSONResponse:
    try:
        raw = await read_bounded_body(request, _MAX_BODY_BYTES)
        body = json.loads(raw)
    except BodyError as exc:
        if exc.status == 413:
            return refusal(413, "too_large", "That is too long.")
        return refusal(exc.status, "invalid_body", "The request could not be read.")
    except (ValueError, UnicodeDecodeError, RecursionError):
        return refusal(400, "invalid_body", "The request body is not JSON.")
    if not isinstance(body, dict):
        return refusal(400, "invalid_body", "The request body is not an object.")
    return body


async def _act(request: Request, user: dict, assignment_id: str, work, *, reads_body: bool = False):
    config = request_config(request)
    user_id = user["username"]
    # Before the body, so whoever the gate turns away reads the same 404 for any body.
    if not await asyncio.to_thread(can_use, config, user_id):
        return not_found()
    parsed = parse_id(assignment_id)
    if parsed is None:
        return not_found()
    body: dict = {}
    if reads_body:
        read = await _read_body(request)
        if isinstance(read, JSONResponse):
            return read
        body = read
    return await asyncio.to_thread(_answer, work, config, user_id, parsed, body)


def _text_field(body: dict, key: str, label: str) -> str | None:
    value = body.get(key)
    if value is None or isinstance(value, str):
        return value
    raise _invalid(f"{label} must be text.")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _list_row(conn, assignment) -> dict:
    return {
        **view.summary(assignment),
        "open_handoffs": len(store.list_actions(conn, assignment.id, status="open")),
        "ledger": view.ledger(conn, assignment),
    }


def _list(config, user_id: str) -> dict:
    with db.get_db(Path(config.db_path)) as conn:
        rows = store.list_assignments(conn, user_id)
        live = [row for row in rows if row.state not in lifecycle.TERMINAL_STATES]
        finished = [row for row in rows if row.state in lifecycle.TERMINAL_STATES]
        return {"assignments": [_list_row(conn, row) for row in live + finished]}


def _completion_evidence(conn, assignment) -> str | None:
    if assignment.state != "waiting" or "completion" not in assignment.waiting_on:
        return None
    return store.latest_complete_request(conn, assignment.id)


def _detail(config, user_id: str, assignment_id: int) -> dict | None:
    with db.get_db(Path(config.db_path)) as conn:
        assignment = store.get_assignment(conn, assignment_id, user_id=user_id)
        if assignment is None:
            return None
        network = None
        if assignment.state not in lifecycle.TERMINAL_STATES:
            network = egress.describe_subject(
                conn, user_id, f"assignment:{assignment.id}",
                ceiling=config.security.scheduled_job_max_unapproved,
            )
        return {
            **view.show(conn, config, assignment, wake_count=view.MAX_SHOW_WAKES),
            "goal": assignment.goal,
            "autonomy": assignment.autonomy,
            "completion_evidence": _completion_evidence(conn, assignment),
            "network": network,
            "external_skills": sorted(usable_external_skills(config, user_id)),
            "chat_href": notices.chat_href(conn, assignment),
            "folder_href": notices.folder_href(assignment.id),
        }


@router.get("/assignments")
async def assignments_list(request: Request, user: dict = Depends(require_auth)):
    config = request_config(request)
    user_id = user["username"]
    if not await asyncio.to_thread(can_use, config, user_id):
        return not_found()
    try:
        listing = await asyncio.to_thread(_list, config, user_id)
    except sqlite3.Error as exc:
        logger.error("assignment listing failed: %s", type(exc).__name__)
        return refusal(500, "error", "Assignments could not be loaded.")
    return JSONResponse(listing, headers=NO_STORE)


@router.get("/assignments/{assignment_id}")
async def assignment_detail(assignment_id: str, request: Request, user: dict = Depends(require_auth)):
    config = request_config(request)
    user_id = user["username"]
    if not await asyncio.to_thread(can_use, config, user_id):
        return not_found()
    parsed = parse_id(assignment_id)
    if parsed is None:
        return not_found()
    try:
        detail = await asyncio.to_thread(_detail, config, user_id, parsed)
    except sqlite3.Error as exc:
        logger.error("assignment read failed: %s", type(exc).__name__)
        return refusal(500, "error", "This assignment could not be loaded.")
    if detail is None:
        return not_found()
    return JSONResponse(detail, headers=NO_STORE)


# ---------------------------------------------------------------------------
# Steering. Each is its CLI verb's calls, in its order.
# ---------------------------------------------------------------------------


def _pause(conn, config, assignment, body: dict) -> dict:
    updated = lifecycle.pause(conn, assignment.id, user_id=assignment.user_id)
    return view.state_result(conn, config, updated)


def _resume(conn, config, assignment, body: dict) -> dict:
    updated = lifecycle.resume(conn, config.assignments, assignment.id, user_id=assignment.user_id)
    return view.state_result(conn, config, updated)


def _wake(conn, config, assignment, body: dict) -> dict:
    updated = lifecycle.request_wake(conn, assignment.id, user_id=assignment.user_id)
    return view.state_result(conn, config, updated)


def _abandon(conn, config, assignment, body: dict) -> dict:
    reason = _text_field(body, "reason", "The reason")
    updated = lifecycle.abandon(conn, assignment.id, user_id=assignment.user_id, reason=reason)
    return view.state_result(conn, config, updated)


def _topup_amount(body: dict) -> tuple[float | None, int | None]:
    """Exactly one of the two numbers, as the CLI's argument group requires."""
    usd = body.get("usd")
    wakes = body.get("wakes")
    if (usd is None) == (wakes is None):
        raise _invalid("A top-up takes either dollars or wakes.")
    if usd is not None:
        if isinstance(usd, bool) or not isinstance(usd, (int, float)):
            raise _invalid("A top-up's dollars must be a number.")
        try:
            return float(usd), None
        except OverflowError:
            raise _invalid("That top-up is too large.") from None
    if isinstance(wakes, bool) or not isinstance(wakes, int):
        raise _invalid("A top-up's wakes must be a whole number.")
    return None, wakes


def _topup(conn, config, assignment, body: dict) -> dict:
    usd, wakes = _topup_amount(body)
    updated = lifecycle.topup(
        conn, config.assignments, assignment.id, user_id=assignment.user_id, usd=usd, wakes=wakes,
    )
    return view.changed(conn, config, updated, **view.still_paused(conn, updated))


def _note(conn, config, assignment, body: dict) -> dict:
    text = _text_field(body, "text", "The note")
    inbox_id = lifecycle.add_note(conn, assignment.id, user_id=assignment.user_id, text=text)
    return view.state_result(conn, config, assignment, inbox_id=inbox_id)


def _close(conn, config, assignment, body: dict) -> dict:
    completed = body.get("completed")
    reject = _text_field(body, "reject", "What is missing")
    confirming = completed is True and reject is None
    sending_back = completed is None and reject is not None
    if not (confirming or sending_back):
        raise _invalid("Closing takes either completed or what is missing.")
    updated = lifecycle.close(
        conn, config, assignment.id, user_id=assignment.user_id,
        completed=confirming, missing=reject,
    )
    return view.state_result(conn, config, updated)


def _confirm_item(item_id: int, conn, config, assignment, body: dict) -> dict:
    item = lifecycle.confirm_item(
        conn, config, assignment.id, user_id=assignment.user_id, item_id=item_id,
    )
    return view.changed(conn, config, assignment, item={"id": item.id, "status": item.status})


def _handoff_done(action_id: int, conn, config, assignment, body: dict) -> dict:
    report = _text_field(body, "report", "The report")
    action = lifecycle.complete_handoff(
        conn, assignment.id, user_id=assignment.user_id, action_id=action_id, report=report,
    )
    return view.state_result(conn, config, assignment, handoff={"id": action.id, "status": action.status})


@router.post("/assignments/{assignment_id}/pause")
async def assignment_pause(
    assignment_id: str, request: Request,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    return await _act(request, user, assignment_id, _pause)


@router.post("/assignments/{assignment_id}/resume")
async def assignment_resume(
    assignment_id: str, request: Request,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    return await _act(request, user, assignment_id, _resume)


@router.post("/assignments/{assignment_id}/wake")
async def assignment_wake(
    assignment_id: str, request: Request,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    return await _act(request, user, assignment_id, _wake)


@router.post("/assignments/{assignment_id}/abandon")
async def assignment_abandon(
    assignment_id: str, request: Request,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    return await _act(request, user, assignment_id, _abandon, reads_body=True)


@router.post("/assignments/{assignment_id}/topup")
async def assignment_topup(
    assignment_id: str, request: Request,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    return await _act(request, user, assignment_id, _topup, reads_body=True)


@router.post("/assignments/{assignment_id}/note")
async def assignment_note(
    assignment_id: str, request: Request,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    return await _act(request, user, assignment_id, _note, reads_body=True)


@router.post("/assignments/{assignment_id}/close")
async def assignment_close(
    assignment_id: str, request: Request,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    return await _act(request, user, assignment_id, _close, reads_body=True)


@router.post("/assignments/{assignment_id}/items/{item_id}/confirm")
async def assignment_item_confirm(
    assignment_id: str, item_id: str, request: Request,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    parsed = parse_id(item_id)
    if parsed is None:
        return not_found()
    return await _act(request, user, assignment_id, functools.partial(_confirm_item, parsed))


@router.post("/assignments/{assignment_id}/handoffs/{action_id}/done")
async def assignment_handoff_done(
    assignment_id: str, action_id: str, request: Request,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    parsed = parse_id(action_id)
    if parsed is None:
        return not_found()
    return await _act(
        request, user, assignment_id, functools.partial(_handoff_done, parsed), reads_body=True,
    )
