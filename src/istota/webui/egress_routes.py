"""Settings > Network access: what a user's scheduled work may reach.

Own subjects only, taken from the session: no parameter names a user, so an admin
cannot approve another user's entries here. Bodies are read bounded and parsed by
hand, because a framework validation error echoes its input and a refused hostname
can carry data. For the same reason no log line names a host or a job.

An approval of `open:` or `public:` carries the digest of the text the page showed,
compared with the row under the write lock, so it cannot reach text nobody read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from istota import db
from istota.notifications.resolvers import egress_refused
from istota.rooms import egress
from istota.webui.request_guards import BodyError, read_bounded_body
from istota.webui.router_stubs import require_auth, verify_origin

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store"}
_MAX_BODY_BYTES = 8 * 1024


def _refusal(status: int, error: str, detail: str, field: str | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": error, "detail": detail, "field": field}, status_code=status, headers=_NO_STORE,
    )


def _not_found() -> JSONResponse:
    return _refusal(404, "not_found", "There is no such item.")


def _config(request: Request):
    config = getattr(request.app.state, "istota_config", None)
    if config is None or not getattr(config, "db_path", None):
        raise HTTPException(503, "Config not loaded")
    return config


def _usable_external_skills(config, user_id: str) -> dict:
    """The `external` skills this user could use at all, name to metadata."""
    from istota.skills._loader import load_skill_index, user_skill_names

    try:
        index = load_skill_index(config.skills_dir, bundled_dir=config.bundled_skills_dir)
        names = user_skill_names(config, user_id, index, count_user_disabled=False)
    except Exception as exc:  # noqa: BLE001 — a page with no skills to offer still works
        logger.warning("egress_skills_unavailable: %s", type(exc).__name__)
        return {}
    return {name: index[name] for name in names if getattr(index[name], "egress", "") != "local"}


def _listing(config, user_id: str) -> dict:
    with db.get_db(Path(config.db_path)) as conn:
        subjects = [
            egress.describe_subject(conn, user_id, subject)
            for subject in egress.list_subject_names(conn, user_id)
        ]
    return {
        "subjects": subjects,
        "external_skills": sorted(_usable_external_skills(config, user_id)),
    }


@router.get("/settings/network")
async def settings_network(request: Request, user: dict = Depends(require_auth)):
    config = _config(request)
    try:
        listing = await asyncio.to_thread(_listing, config, user["username"])
    except sqlite3.Error as exc:
        logger.error("egress listing failed: %s", type(exc).__name__)
        return _refusal(500, "error", "Network access could not be loaded.")
    return JSONResponse(listing, headers=_NO_STORE)


async def _read_body(request: Request, fields: set[str]) -> dict | JSONResponse:
    """The body as exactly these string fields, or a refusal that echoes none of it."""
    try:
        raw = await read_bounded_body(request, _MAX_BODY_BYTES)
        body = json.loads(raw)
    except BodyError as exc:
        return _refusal(exc.status, "invalid_body", "That request is too large.")
    except (ValueError, UnicodeDecodeError, RecursionError):
        return _refusal(400, "invalid_body", "The request body is not JSON.")
    if not isinstance(body, dict) or set(body) != fields:
        return _refusal(400, "invalid_body", "The request does not have the expected fields.")
    if not all(isinstance(value, str) for value in body.values()):
        return _refusal(400, "invalid_body", "The request does not have the expected fields.")
    return body


def _write(config, user_id: str, subject: str, change) -> None:
    """Run `change(conn)` for a subject this user owns, then close the bell row if
    nothing is left to ask. One write transaction. A subject of nobody's, or of
    somebody else's, raises `LookupError`, so the two read alike."""
    with db.get_db(Path(config.db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not egress.subject_exists(conn, user_id, subject):
            raise LookupError
        change(conn)
        egress_refused.resolve_if_settled(conn, user_id, subject, by="web")
        conn.commit()


async def _apply(config, user_id: str, subject: str, change, operation: str) -> JSONResponse:
    try:
        await asyncio.to_thread(_write, config, user_id, subject, change)
    except LookupError:
        return _not_found()
    except egress.StaleJob as exc:
        return _refusal(409, "stale_job", str(exc))
    except egress.EntryRefused as exc:
        return _refusal(400, "invalid_entry", str(exc), "entry")
    except sqlite3.Error as exc:
        logger.error("egress write failed: %s", type(exc).__name__)
        return _refusal(500, "error", "That could not be saved.")
    logger.info("egress_web operation=%s user=%s", operation, user_id)
    return JSONResponse({"ok": True}, headers=_NO_STORE)


@router.post("/settings/network/grants")
async def settings_network_grant(
    request: Request, user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = _config(request)
    user_id = user["username"]
    body = await _read_body(request, {"subject", "entry"})
    if isinstance(body, JSONResponse):
        return body
    skills = await asyncio.to_thread(_usable_external_skills, config, user_id)

    def change(conn):
        parsed = egress.parse_entry(body["entry"])
        if parsed.kind in egress.JOB_REQUESTS:
            raise egress.EntryRefused("A job's text is approved from its own button.")
        egress.add_grant(
            conn, user_id, body["subject"], parsed.text, decided_via="web", skill_index=skills,
        )
        egress.clear_refused(conn, user_id, body["subject"], parsed.text)

    return await _apply(config, user_id, body["subject"], change, "grant")


@router.post("/settings/network/approvals")
async def settings_network_approve(
    request: Request, user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = _config(request)
    user_id = user["username"]
    body = await _read_body(request, {"subject", "kind", "digest"})
    if isinstance(body, JSONResponse):
        return body
    if not body["subject"].startswith("cron:"):
        return _not_found()

    def change(conn):
        egress.approve_job(
            conn, user_id, body["subject"], body["kind"], body["digest"], decided_via="web",
        )

    return await _apply(config, user_id, body["subject"], change, "approve")


@router.post("/settings/network/grants/remove")
async def settings_network_revoke(
    request: Request, user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = _config(request)
    user_id = user["username"]
    body = await _read_body(request, {"subject", "entry"})
    if isinstance(body, JSONResponse):
        return body

    def change(conn):
        egress.remove_grant(conn, user_id, body["subject"], body["entry"])

    return await _apply(config, user_id, body["subject"], change, "revoke")


@router.post("/settings/network/refused/dismiss")
async def settings_network_dismiss(
    request: Request, user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = _config(request)
    user_id = user["username"]
    body = await _read_body(request, {"subject", "entry"})
    if isinstance(body, JSONResponse):
        return body

    def change(conn):
        egress.dismiss_refused(conn, user_id, body["subject"], body["entry"])

    return await _apply(config, user_id, body["subject"], change, "dismiss")
