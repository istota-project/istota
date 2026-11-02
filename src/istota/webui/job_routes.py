"""Settings > Scheduled jobs: a user's own jobs, read and written.

Own jobs only, taken from the session: no parameter names a user, and a job of somebody
else's is the same 404 as one that does not exist. Bodies are read bounded and parsed by
hand, because a framework validation error echoes its input. No line this module logs names a
job, which a limited task can name; the shared helpers it calls still do.

CRON.md is the authority. A save writes the file with no transaction open, since that
can be a slow mount, and leaves the row to the daemon's next sync. A rewrite is built
from the jobs that parsed, so a file that did not parse whole refuses every save, and so
does a missing or re-seeded file with rows still in the table: a file holding only the
new job would orphan them on the next sync, over what may be a passing mount fault.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from istota import cron_loader, db
from istota.lib.date_parse import iso_utc
from istota.notifications.resolvers import cron_job as cron_job_source
from istota.notifications.resolvers import egress_refused
from istota.rooms import egress
from istota.storage import read_user_config_file
from istota.webui.request_guards import BodyError, read_bounded_body
from istota.webui.router_stubs import require_auth, verify_origin
from istota.webui.settings_http import NO_STORE, not_found, parse_id, refusal, request_config, usable_external_skills

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_BODY_BYTES = 64 * 1024
_KEEPABLE = ("open", "public")
_NEXT_SYNC = "next_sync"
_FILE_UNREADABLE = "CRON.md cannot be changed until every job in it loads."
_NOT_IN_FILE = "CRON.md does not hold this job, so it cannot be changed here."
_COMMAND_DROPPED = (
    "Command jobs are admin-only, so this job does not run. Use a prompt, or remove it."
)
_MODULE_REFUSED = "A module owns this job, and it cannot be changed here."


class _Refused(Exception):
    """A refusal to answer, raised from a worker thread."""

    def __init__(self, response: JSONResponse) -> None:
        super().__init__("refused")
        self.response = response


def _file_refused() -> _Refused:
    return _Refused(refusal(409, "file_unreadable", _FILE_UNREADABLE))


def _has_own_rows(conn: sqlite3.Connection, user_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM scheduled_jobs WHERE user_id = ? AND substr(name, 1, 8) != '_module.' LIMIT 1",
        (user_id,),
    ).fetchone() is not None


def _file_readable(config, user_id: str, doc, has_rows: bool) -> bool:
    if not config.has_workspace or not cron_loader.job_file_writable(config, user_id, doc):
        return False
    # The sync restores rows into a seeded template. A job added first would stop it
    # being a template, and the sweep would take them.
    return not (has_rows and (doc is None or doc.is_template))


def _load_writable_doc(config, user_id: str):
    """This user's CRON.md, or a refusal if a job cannot be written over it."""
    doc = cron_loader.load_cron_document(config, user_id) if config.has_workspace else None
    with db.get_db(Path(config.db_path)) as conn:
        has_rows = _has_own_rows(conn, user_id)
    if not _file_readable(config, user_id, doc, has_rows):
        raise _file_refused()
    return doc


def _definition_from_file(job: cron_loader.CronJob) -> dict:
    return {
        "kind": "command" if job.command else "prompt",
        "prompt": job.prompt,
        "prompt_file": job.prompt_file,
        "command": job.command,
        "target": job.target,
        "room": job.room,
        "silent_unless_action": job.silent_unless_action,
        "skip_log_channel": job.skip_log_channel,
        "model": job.model,
        "effort": job.effort,
        "brain": job.brain,
        "provider": job.provider,
        "publish_shared_kv": job.publish_shared_kv,
        "publish_shared_kv_trusted": job.publish_shared_kv_trusted,
        "egress": list(job.egress),
        "egress_kept": list(job.egress_kept),
    }


def _row_command(row: db.ScheduledJob) -> str:
    if not row.skill:
        return row.command or ""
    try:
        args = json.loads(row.skill_args or "[]")
    except ValueError:
        args = []
    if not isinstance(args, list):
        args = []
    return shlex.join(["istota-skill", row.skill, *(str(arg) for arg in args)])


def _row_egress(row: db.ScheduledJob) -> list[str]:
    try:
        entries = json.loads(row.egress_request or "[]")
    except ValueError:
        return []
    return [entry for entry in entries if isinstance(entry, str)] if isinstance(entries, list) else []


def _definition_from_row(row: db.ScheduledJob) -> dict:
    command = _row_command(row)
    return {
        "kind": "command" if command else "prompt",
        "prompt": row.prompt or "",
        "prompt_file": "",
        "command": command,
        "target": row.output_target or "",
        "room": row.conversation_token or "",
        "silent_unless_action": bool(row.silent_unless_action),
        "skip_log_channel": bool(row.skip_log_channel),
        "model": row.model or "",
        "effort": row.effort or "",
        "brain": row.brain or "",
        "provider": row.provider or "",
        "publish_shared_kv": row.publish_shared_kv or "",
        "publish_shared_kv_trusted": bool(row.publish_shared_kv_trusted),
        "egress": _row_egress(row),
        "egress_kept": [],
    }


def _row_state(row: db.ScheduledJob) -> str:
    return egress.job_run_state({"enabled": row.enabled, "auto_disabled_at": row.auto_disabled_at})


def _job_view(config, conn, user_id: str, row, file_job, *, readable: bool, ceiling: str) -> dict:
    is_module = (row.name if row is not None else file_job.name).startswith(egress.MODULE_JOB_PREFIX)
    in_file = file_job is not None and not is_module
    dropped = (
        row is None and in_file
        and cron_loader.fj_is_disallowed_command(file_job, config.is_admin(user_id))
    )
    if row is not None:
        state = _row_state(row)
    else:
        state = egress.JOB_RUNNING if file_job.enabled else egress.JOB_DISABLED
    # No route can save a row with no id, so a dropped job is fixed in CRON.md.
    editable = in_file and readable and not dropped
    view = {
        "id": row.id if row is not None else None,
        "pending": row is None and not dropped,
        "name": file_job.name if in_file else row.name,
        "cron": file_job.cron if in_file else row.cron_expression,
        "enabled": file_job.enabled if in_file else bool(row.enabled),
        "once": file_job.once if in_file else bool(row.once),
        "state": state,
        "last_run_at": iso_utc(row.last_run_at) if row is not None else None,
        "last_success_at": iso_utc(row.last_success_at) if row is not None else None,
        "next_run_at": (
            cron_loader.next_fire(row.cron_expression, config.resolve_user_timezone(user_id))
            if row is not None and state == egress.JOB_RUNNING else None
        ),
        "consecutive_failures": row.consecutive_failures if row is not None else 0,
        "last_error": row.last_error if row is not None else None,
        "definition": _definition_from_file(file_job) if in_file else _definition_from_row(row),
        "revision": cron_loader.job_revision(file_job) if editable else None,
        "editable": editable,
        "network": (
            egress.describe_subject(conn, user_id, egress.cron_subject(row.name), ceiling=ceiling)
            if row is not None else None
        ),
    }
    if dropped:
        view["file_error"] = _COMMAND_DROPPED
    return view


def _options(config, user_id: str) -> dict:
    # app.py holds these and includes this router, so the import waits for a request.
    from istota.webui import app as web_app

    return {
        "outputs": web_app._registered_delivery_surfaces(),
        "rooms": {"talk": web_app._user_talk_rooms(user_id), "web": web_app._user_web_rooms(user_id)},
        "selectable_brains": web_app._brain_catalogue(user_id)["selectable_brains"],
        "can_publish": config.is_shared_kv_writer(user_id),
    }


def _listing(config, user_id: str) -> dict:
    content = read_user_config_file(config, user_id, "CRON.md") if config.has_workspace else None
    doc = cron_loader.parse_cron_document(content, config, user_id) if content else None
    errors = cron_loader.cron_document_errors(content, config, user_id) if content else []
    file_jobs = {job.name: job for job in (doc.jobs if doc is not None else [])}
    ceiling = config.security.scheduled_job_max_unapproved
    with db.get_db(Path(config.db_path)) as conn:
        readable = _file_readable(config, user_id, doc, _has_own_rows(conn, user_id))
        subjects = set(egress.list_subject_names(conn, user_id))
        rows = {
            row.name: row for row in db.get_user_scheduled_jobs(conn, user_id)
            if egress.cron_subject(row.name) in subjects
        }
        names = set(rows) | {
            name for name in file_jobs if not name.startswith(egress.MODULE_JOB_PREFIX)
        }
        jobs = [
            _job_view(
                config, conn, user_id, rows.get(name), file_jobs.get(name),
                readable=readable, ceiling=ceiling,
            )
            for name in sorted(names, key=str.casefold)
        ]
    return {
        "jobs": jobs,
        "file": {"readable": readable, "errors": errors},
        "options": _options(config, user_id),
        "external_skills": sorted(usable_external_skills(config, user_id)),
        "timezone": config.resolve_user_timezone(user_id),
    }


@router.get("/settings/jobs")
async def settings_jobs(request: Request, user: dict = Depends(require_auth)):
    config = request_config(request)
    try:
        listing = await asyncio.to_thread(_listing, config, user["username"])
    except sqlite3.Error as exc:
        logger.error("job listing failed: %s", type(exc).__name__)
        return refusal(500, "error", "Scheduled jobs could not be loaded.")
    return JSONResponse(listing, headers=NO_STORE)


async def _read_job_body(request: Request) -> dict | JSONResponse:
    try:
        raw = await read_bounded_body(request, _MAX_BODY_BYTES)
        body = json.loads(raw)
    except BodyError as exc:
        if exc.status == 413:
            return refusal(413, "too_large", "That job is too large.")
        return refusal(exc.status, "invalid_body", "The request could not be read.")
    except (ValueError, UnicodeDecodeError, RecursionError):
        return refusal(400, "invalid_body", "The request body is not JSON.")
    if not isinstance(body, dict):
        return refusal(400, "invalid_field", "This is not a job.", "body")
    return body


def _save_refusal(outcome: cron_loader.JobSaveOutcome, name: str) -> JSONResponse:
    status = outcome.status
    if status == "invalid":
        # The parser's sentences open with the job's name, which came from the body.
        errors = [error.replace(f"Job '{name}'", "This job") for error in outcome.errors]
        return JSONResponse(
            {"error": "invalid", "errors": errors}, status_code=422, headers=NO_STORE,
        )
    if status == "name_taken":
        return refusal(409, "name_taken", "Another job has that name.", "name")
    if status in ("changed", "not_found"):
        return refusal(409, "job_changed", "This job changed since the page read it.")
    if status == "unreadable":
        return refusal(409, "file_unreadable", _FILE_UNREADABLE)
    if status == "locked":
        return refusal(409, "locked", "CRON.md is being changed. Try again in a moment.")
    if status == "too_large":
        return refusal(413, "too_large", "CRON.md would be too large with this job.")
    return refusal(500, "failed", "CRON.md could not be written. Nothing changed.")


def _validated(config, user_id: str, payload: dict, stored) -> cron_loader.CronJob:
    try:
        return cron_loader.validate_job_input(config, user_id, payload, stored)
    except cron_loader.JobFieldError as exc:
        raise _Refused(refusal(400, "invalid_field", exc.message, exc.field)) from None


def _create(config, user_id: str, payload: dict) -> JSONResponse:
    _load_writable_doc(config, user_id)
    job = _validated(config, user_id, payload, None)
    outcome = cron_loader.save_job_definition(
        config, user_id, job, original_name=None, expected_revision=None,
    )
    if outcome.status != "ok":
        return _save_refusal(outcome, job.name)
    logger.info("job_web operation=create user=%s", user_id)
    return JSONResponse({"applies": _NEXT_SYNC}, status_code=201, headers=NO_STORE)


async def _run_work(work, *args) -> JSONResponse:
    try:
        return await asyncio.to_thread(work, *args)
    except _Refused as exc:
        return exc.response
    except sqlite3.Error as exc:
        logger.error("job write failed: %s", type(exc).__name__)
        return refusal(500, "error", "That could not be saved.")


@router.post("/settings/jobs")
async def settings_job_create(
    request: Request, user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = request_config(request)
    body = await _read_job_body(request)
    if isinstance(body, JSONResponse):
        return body
    return await _run_work(_create, config, user["username"], body)


def _own_row(config, user_id: str, job_id: str) -> db.ScheduledJob:
    """The caller's own job, not a module's, or a refusal."""
    parsed = parse_id(job_id)
    if parsed is None:
        raise _Refused(not_found())
    with db.get_db(Path(config.db_path)) as conn:
        row = db.get_scheduled_job(conn, parsed)
    if row is None or row.user_id != user_id:
        raise _Refused(not_found())
    if row.name.startswith(egress.MODULE_JOB_PREFIX):
        raise _Refused(refusal(409, "module_job", _MODULE_REFUSED))
    return row


def _keep_access_kind(payload: dict) -> str | None:
    kind = payload.pop("keep_access", None)
    if kind is None or (isinstance(kind, str) and kind in _KEEPABLE):
        return kind
    raise _Refused(refusal(400, "invalid_field", "That is not a kind of access.", "keep_access"))


def _holds_approval(config, user_id: str, row: db.ScheduledJob, kind: str) -> bool:
    subject = egress.cron_subject(row.name)
    with db.get_db(Path(config.db_path)) as conn:
        current = egress.job_row_by_subject(conn, user_id, subject)
        if current is None:
            return False
        return f"{kind}:{egress.job_digest(current)}" in egress.grants_for(conn, user_id, subject)


def _keep_approval(config, user_id: str, subject: str, kind: str, digest: str) -> bool:
    """The grant for the text just saved. The save has landed either way, so a failure
    here is reported and not raised, and the sync then asks again, the safe direction."""
    try:
        with db.get_db(Path(config.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            egress.approve_saved_text(conn, user_id, subject, kind, digest, decided_via="web")
            egress_refused.resolve_if_settled(conn, user_id, subject, by="web")
            conn.commit()
    except (sqlite3.Error, egress.EntryRefused) as exc:
        logger.error("job approval not kept: %s", type(exc).__name__)
        return False
    return True


def _edit(config, user_id: str, job_id: str, payload: dict) -> JSONResponse:
    row = _own_row(config, user_id, job_id)
    fields = dict(payload)
    revision = fields.pop("revision", None)
    if not isinstance(revision, str):
        return refusal(400, "invalid_field", "The page's copy of this job is missing.", "revision")
    keep = _keep_access_kind(fields)

    doc = _load_writable_doc(config, user_id)
    stored = next((job for job in (doc.jobs if doc else []) if job.name == row.name), None)
    if stored is None:
        return refusal(409, "file_unreadable", _NOT_IN_FILE)
    # Before the field checks: what they call unchanged is judged against this revision.
    if cron_loader.job_revision(stored) != revision:
        return refusal(409, "job_changed", "This job changed since the page read it.")
    job = _validated(config, user_id, fields, stored)

    # Asked before the write: a sync landing first drops the old grant.
    holds = keep is not None and job.name == row.name and _holds_approval(config, user_id, row, keep)
    outcome = cron_loader.save_job_definition(
        config, user_id, job, original_name=stored.name, expected_revision=revision,
    )
    if outcome.status != "ok":
        return _save_refusal(outcome, job.name)

    access_kept = None
    if keep is not None:
        access_kept = holds and _keep_approval(
            config, user_id, egress.cron_subject(row.name), keep,
            cron_loader.job_text_digest(job, is_admin=config.is_admin(user_id)),
        )
    logger.info("job_web operation=edit user=%s job_id=%s", user_id, row.id)
    return JSONResponse({"applies": _NEXT_SYNC, "access_kept": access_kept}, headers=NO_STORE)


@router.put("/settings/jobs/{job_id}")
async def settings_job_edit(
    job_id: str, request: Request,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = request_config(request)
    body = await _read_job_body(request)
    if isinstance(body, JSONResponse):
        return body
    return await _run_work(_edit, config, user["username"], job_id, body)


def _set_enabled(config, user_id: str, job_id: str, enabled: bool) -> JSONResponse:
    row = _own_row(config, user_id, job_id)
    with db.get_db(Path(config.db_path)) as conn:
        outcome = cron_loader.set_job_enabled(config, conn, row, enabled, by="web")
    logger.info(
        "job_web operation=%s user=%s job_id=%s outcome=%s",
        "enable" if enabled else "disable", user_id, row.id, outcome,
    )
    return JSONResponse({"outcome": outcome}, headers=NO_STORE)


@router.post("/settings/jobs/{job_id}/enable")
async def settings_job_enable(
    job_id: str, request: Request,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    return await _run_work(_set_enabled, request_config(request), user["username"], job_id, True)


@router.post("/settings/jobs/{job_id}/disable")
async def settings_job_disable(
    job_id: str, request: Request,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    return await _run_work(_set_enabled, request_config(request), user["username"], job_id, False)


def _run_now(config, user_id: str, job_id: str) -> JSONResponse:
    from istota import scheduler

    row = _own_row(config, user_id, job_id)
    with db.get_db(Path(config.db_path)) as conn:
        task_id, refused = scheduler.run_scheduled_job_now(conn, config, row)
    if task_id is None:
        return refusal(409, "run_refused", refused)
    logger.info("job_web operation=run user=%s job_id=%s task_id=%s", user_id, row.id, task_id)
    return JSONResponse({"task_id": task_id}, headers=NO_STORE)


@router.post("/settings/jobs/{job_id}/run")
async def settings_job_run(
    job_id: str, request: Request,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    return await _run_work(_run_now, request_config(request), user["username"], job_id)


def _delete(config, user_id: str, job_id: str) -> JSONResponse:
    row = _own_row(config, user_id, job_id)
    doc = cron_loader.load_cron_document(config, user_id) if config.has_workspace else None
    if doc is not None and cron_loader.document_hides_jobs(doc):
        return refusal(409, "file_unreadable", _FILE_UNREADABLE)
    # The file first and outside any transaction (ISSUE-387); a row whose definition is
    # still in CRON.md would be re-inserted by the next sync.
    if not cron_loader.remove_job_definition(config, user_id, row.name):
        return refusal(
            409, "cron_md_unwritable",
            "The job is still in CRON.md, which could not be rewritten. Nothing was deleted.",
        )
    with db.get_db(Path(config.db_path)) as conn:
        # By name: a sync between the file write and here may have replaced the row.
        current = db.get_scheduled_job_by_name(conn, user_id, row.name)
        if current is not None:
            cron_job_source.resolve_for_job(conn, user_id, current.id, by="web")
            db.delete_scheduled_job(conn, current.id)
    logger.info("job_web operation=delete user=%s job_id=%s", user_id, row.id)
    return JSONResponse({"deleted": True, "id": row.id}, headers=NO_STORE)


@router.delete("/settings/jobs/{job_id}")
async def settings_job_delete(
    job_id: str, request: Request,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    return await _run_work(_delete, request_config(request), user["username"], job_id)
