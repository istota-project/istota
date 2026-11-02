"""A subject's network access: the writes behind the job and assignment pages.

Own subjects only, taken from the session: no parameter names a user, so an admin
cannot approve another user's entries here. Bodies are read bounded and parsed by
hand, because a framework validation error echoes its input and a refused hostname
can carry data. For the same reason no log line names a host or a job.

An approval of `open:` or `public:` carries the digest of the text the page showed,
compared with the row under the write lock, so it cannot reach text nobody read. A
job's access is one choice, which writes its `reach` to CRON.md and any approval it needs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from istota import cron_loader, db
from istota.notifications.resolvers import egress_refused
from istota.rooms import egress
from istota.webui.request_guards import BodyError, read_bounded_body
from istota.webui.router_stubs import require_auth, verify_origin
from istota.webui.settings_http import NO_STORE, not_found, refusal, request_config, usable_external_skills

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_BODY_BYTES = 8 * 1024


class _FileUnwritten(Exception):
    """CRON.md does not say what the person chose."""


async def _read_body(request: Request, fields: set[str]) -> dict | JSONResponse:
    """The body as exactly these string fields, or a refusal that echoes none of it."""
    try:
        raw = await read_bounded_body(request, _MAX_BODY_BYTES)
        body = json.loads(raw)
    except BodyError as exc:
        return refusal(exc.status, "invalid_body", "That request is too large.")
    except (ValueError, UnicodeDecodeError, RecursionError):
        return refusal(400, "invalid_body", "The request body is not JSON.")
    if not isinstance(body, dict) or set(body) != fields:
        return refusal(400, "invalid_body", "The request does not have the expected fields.")
    if not all(isinstance(value, str) for value in body.values()):
        return refusal(400, "invalid_body", "The request does not have the expected fields.")
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
    return await _run(lambda: _write(config, user_id, subject, change), user_id, operation)


async def _run(work, user_id: str, operation: str) -> JSONResponse:
    try:
        await asyncio.to_thread(work)
    except LookupError:
        return not_found()
    except _FileUnwritten as exc:
        return refusal(409, "file_unwritten", str(exc))
    except egress.StaleJob as exc:
        return refusal(409, "stale_job", str(exc))
    except egress.EntryRefused as exc:
        return refusal(400, "invalid_entry", str(exc), "entry")
    except sqlite3.Error as exc:
        logger.error("egress write failed: %s", type(exc).__name__)
        return refusal(500, "error", "That could not be saved.")
    logger.info("egress_web operation=%s user=%s", operation, user_id)
    return JSONResponse({"ok": True}, headers=NO_STORE)


@router.post("/settings/network/grants")
async def settings_network_grant(
    request: Request, user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = request_config(request)
    user_id = user["username"]
    body = await _read_body(request, {"subject", "entry"})
    if isinstance(body, JSONResponse):
        return body
    skills = await asyncio.to_thread(usable_external_skills, config, user_id)

    def change(conn):
        parsed = egress.parse_entry(body["entry"])
        if parsed.kind in egress.JOB_REQUESTS:
            raise egress.EntryRefused("A job's text is approved from its own button.")
        egress.add_grant(
            conn, user_id, body["subject"], parsed.text, decided_via="web", skill_index=skills,
            operator=egress.operator_endpoints(conn, config, user_id),
        )
        egress.clear_refused(conn, user_id, body["subject"], parsed.text)

    return await _apply(config, user_id, body["subject"], change, "grant")


def _set_access(config, user_id: str, subject: str, access: str, digest: str) -> None:
    """One job's access, chosen on the page.

    CRON.md authors `reach`, so the sync undoes a choice that did not reach it, and no
    database transaction is held across the write, which can be a slow mount. Limited
    takes the approvals off first, so a file that cannot be written cannot keep a job on
    full access. A wider choice writes the file first and refuses if it cannot: a stale
    text or a crash after that leaves a `reach` with no approval, which grants nothing
    the file could not already ask for.
    """
    ceiling = config.security.scheduled_job_max_unapproved
    with db.get_db(Path(config.db_path)) as conn:
        row = egress.job_row_by_subject(conn, user_id, subject)
    if row is None:
        raise LookupError
    if row["name"].startswith(egress.MODULE_JOB_PREFIX):
        raise egress.EntryRefused("A module's job is not set here.")
    writes_file = egress.access_writes_file(row, access, ceiling)
    wanted = egress.ACCESS_REACH[access]

    def write_file(outcome: str) -> None:
        if not cron_loader.update_job_reach_in_cron_md(config, user_id, row["name"], wanted):
            raise _FileUnwritten(
                f"CRON.md could not be changed, so {outcome} Check that every job in it loads."
            )

    def store(reach: str | None) -> None:
        _write(config, user_id, subject, lambda conn: egress.set_job_access(
            conn, user_id, subject, access, digest,
            reach=reach, ceiling=ceiling, decided_via="web",
        ))

    if access == egress.REACH_LIMITED:
        store(None)
        if writes_file:
            write_file("any approval is gone but the file still asks for more.")
            store(wanted)
        return
    egress.check_job_text(row, digest)
    if writes_file:
        write_file("nothing changed.")
    store(wanted if writes_file else None)


@router.post("/settings/network/access")
async def settings_network_access(
    request: Request, user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = request_config(request)
    user_id = user["username"]
    body = await _read_body(request, {"subject", "access", "digest"})
    if isinstance(body, JSONResponse):
        return body
    if not body["subject"].startswith("cron:"):
        return not_found()
    if body["access"] not in egress.ACCESS_REACH:
        return refusal(400, "invalid_body", "That is not a kind of access.")
    return await _run(
        lambda: _set_access(config, user_id, body["subject"], body["access"], body["digest"]),
        user_id, "access",
    )


@router.post("/settings/network/grants/remove")
async def settings_network_revoke(
    request: Request, user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = request_config(request)
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
    config = request_config(request)
    user_id = user["username"]
    body = await _read_body(request, {"subject", "entry"})
    if isinstance(body, JSONResponse):
        return body

    def change(conn):
        egress.dismiss_refused(conn, user_id, body["subject"], body["entry"])

    return await _apply(config, user_id, body["subject"], change, "dismiss")
