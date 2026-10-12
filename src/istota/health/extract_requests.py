"""Health document extraction, handed from the web process to the daemon.

The three extract routes (a lab panel, a visit document, an immunization list)
call the model with the ``Read`` tool on an uploaded document, and on the two
Claude brains that grant is confined by bubblewrap and nothing else
(``_brain_call``). The web container cannot build a namespace: the stack gives
it Docker's default seccomp and AppArmor profiles and no ``systempaths`` grant,
so ``build_bwrap_cmd`` handed the command back unchanged and the CLI ran
unconfined in a container that mounts all of ``/data`` (ISSUE-697). So a route
leaves a request here and the daemon's ``health-extract-request`` gate runs the
extraction on its next tick, the shape shared-block Run now has
(``briefings/shared_block_runs.py``): a request stays pending until it is
answered, so a daemon restarted mid-run answers it on the next tick rather than
leaving the page waiting on a request nobody holds.

A reserved ``shared_kv`` namespace, written by the web route and the daemon,
never by a task. Keys are ``request:<id>`` and ``answered:<id>``. The request
names its user. A panel is read from that user's own uploads directory. An
upload is staged in ``{temp_dir}/.health-extract/``, which both containers see
through the ``/data`` volume and which no task binds: the user's own work
directory ``{temp_dir}/{user_id}`` is bound read-write into their tasks, so a
task could swap the staged file for a symlink while the daemon, which is not
sandboxed, was about to read it. The request carries the staged file's bare
name, never a path, and the daemon opens it once without following a link and
reads the bytes it keeps from that one open.

The daemon also keeps the encounter and immunization uploads as documents, as
the routes did, because the staged copy is the only copy and the daemon holds
it: a storage failure still returns the extracted rows with ``document_id``
null and a warning.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger(__name__)

EXTRACT_NAMESPACE = "_health_extract"
_REQUEST_PREFIX = "request:"
_ANSWERED_PREFIX = "answered:"
# The poll runs on the scheduler's dispatch loop every tick, so a held write
# lock skips the tick rather than stalling it.
_POLL_BUSY_MS = 2000
# An answer is removed when its owner reads it; one never collected is pruned
# after this long, when the next answer is recorded.
_ANSWER_RETENTION = "-1 day"

PANEL = "panel"
ENCOUNTER = "encounter"
IMMUNIZATION = "immunization"
KINDS = (PANEL, ENCOUNTER, IMMUNIZATION)

PENDING = "pending"
ANSWERED = "answered"
UNKNOWN = "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: The staging directory, under ``config.temp_dir``. A leading dot keeps it off
#: every user's own directory name, and ``_staging_dir`` refuses a configured
#: user who has it anyway.
_STAGING_DIR_NAME = ".health-extract"
#: Pending requests one user may hold; one queue serves every user.
MAX_PENDING_PER_USER = 3


class TooManyRequests(Exception):
    """The user already has ``MAX_PENDING_PER_USER`` extractions waiting."""


def _staging_dir(config: "Config") -> Path:
    if _STAGING_DIR_NAME in (getattr(config, "users", None) or {}):
        raise RuntimeError(f"a user is named {_STAGING_DIR_NAME!r}; refusing to stage")
    root = Path(getattr(config, "temp_dir", None) or tempfile.gettempdir()).resolve()
    staging = root / _STAGING_DIR_NAME
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    return staging


def stage_upload(config: "Config", raw: bytes, suffix: str) -> str:
    """Write an upload where the daemon's extractor can read it, and return its
    bare file name."""
    with tempfile.NamedTemporaryFile(
        dir=_staging_dir(config), prefix="upload-", suffix=suffix or ".bin",
        delete=False,
    ) as tmp:
        tmp.write(raw)
    return Path(tmp.name).name


def discard_staged(config: "Config", name: str) -> None:
    """Remove a staged upload no request will name. Never raises."""
    with contextlib.suppress(OSError, RuntimeError):
        path = _staged_path(config, name)
        if path is not None:
            path.unlink()


def _staged_path(config: "Config", name: object) -> Path | None:
    """The staged upload a request names, or None when the name is not a bare
    file name in the staging directory."""
    if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
        return None
    staging = _staging_dir(config)
    path = staging / name
    if path.parent != staging:
        return None
    return path


def _read_staged(path: Path) -> bytes | None:
    """The staged file's bytes, from one open that follows no link, or None
    when it is missing or not a regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return None
        return fh.read()


def request_extract(
    config: "Config",
    user_id: str,
    kind: str,
    *,
    panel_id: int | None = None,
    staged: str | None = None,
    mime: str | None = None,
    filename: str | None = None,
    document_mime: str | None = None,
) -> str:
    """Ask the daemon to run one extraction on its next tick. Returns its id.

    ``mime`` is what the extractor reads the file as; ``document_mime`` is the
    declared type the kept document is stored with, which the routes passed
    separately and still do.
    """
    from istota import db

    if kind not in KINDS:
        raise ValueError(f"unknown extraction kind {kind!r}")
    request_id = uuid.uuid4().hex[:16]
    request = {
        "id": request_id, "user_id": user_id, "kind": kind, "at": _now(),
        "panel_id": panel_id, "staged": staged, "mime": mime,
        "filename": filename, "document_mime": document_mime,
    }
    with db.get_db(config.db_path) as conn:
        # One write transaction from the count, so two uploads cannot both
        # take the last slot.
        conn.execute("BEGIN IMMEDIATE")
        pending = 0
        for row in conn.execute(
            "SELECT value FROM shared_kv WHERE namespace = ? AND key LIKE ?",
            (EXTRACT_NAMESPACE, _REQUEST_PREFIX + "%"),
        ):
            try:
                owner = json.loads(row["value"]).get("user_id")
            except (ValueError, AttributeError):
                continue
            if owner == user_id:
                pending += 1
        if pending >= MAX_PENDING_PER_USER:
            raise TooManyRequests(user_id)
        db.shared_kv_set(conn, EXTRACT_NAMESPACE, _REQUEST_PREFIX + request_id,
                         json.dumps(request), "web")
    return request_id


def requests_pending(config: "Config") -> bool:
    """Whether any extraction is waiting. One indexed read, for the dispatch loop."""
    if not Path(config.db_path).exists():
        return False
    from istota import db

    with db.get_db(config.db_path, busy_timeout_ms=_POLL_BUSY_MS) as conn:
        row = conn.execute(
            "SELECT 1 FROM shared_kv WHERE namespace = ? AND key LIKE ? LIMIT 1",
            (EXTRACT_NAMESPACE, _REQUEST_PREFIX + "%"),
        ).fetchone()
    return row is not None


def next_request(config: "Config") -> dict | None:
    """The oldest pending request, left in place until it is answered.

    A value that is not a JSON object is removed rather than returned, so one
    bad row cannot block the queue behind it.
    """
    if not Path(config.db_path).exists():
        return None
    from istota import db

    with db.get_db(config.db_path) as conn:
        while True:
            row = conn.execute(
                "SELECT key, value FROM shared_kv WHERE namespace = ? AND key LIKE ? "
                "ORDER BY updated_at, key LIMIT 1",
                (EXTRACT_NAMESPACE, _REQUEST_PREFIX + "%"),
            ).fetchone()
            if row is None:
                return None
            try:
                request = json.loads(row["value"])
            except ValueError:
                request = None
            if isinstance(request, dict) and request.get("id"):
                return request
            logger.warning("health_extract_request_dropped key=%s", row["key"])
            db.shared_kv_delete(conn, EXTRACT_NAMESPACE, row["key"])


def record_answered(config: "Config", request: dict, result: dict) -> None:
    """Record a request's result and remove the request, in one transaction,
    pruning answers nobody collected. Never raises."""
    from istota import db

    request_id = request["id"]
    value = json.dumps({
        "id": request_id, "user_id": request.get("user_id", ""),
        "at": _now(), "result": result,
    })
    try:
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM shared_kv WHERE namespace = ? AND key LIKE ? "
                "AND updated_at < datetime('now', ?)",
                (EXTRACT_NAMESPACE, _ANSWERED_PREFIX + "%", _ANSWER_RETENTION),
            )
            db.shared_kv_set(conn, EXTRACT_NAMESPACE, _ANSWERED_PREFIX + request_id,
                             value, "daemon")
            db.shared_kv_delete(conn, EXTRACT_NAMESPACE, _REQUEST_PREFIX + request_id)
    except Exception as exc:  # noqa: BLE001 - a record of the run, not the run
        logger.warning("health_extract_answer_unwritten id=%s err=%s", request_id, exc)


def extract_state(config: "Config", user_id: str, request_id: str) -> dict:
    """Where one of ``user_id``'s requests stands: ``pending``, ``answered``
    with its result, or ``unknown``. Another user's request is ``unknown``."""
    from istota import db

    with db.get_db(config.db_path) as conn:
        for state, prefix in ((ANSWERED, _ANSWERED_PREFIX), (PENDING, _REQUEST_PREFIX)):
            row = db.shared_kv_get(conn, EXTRACT_NAMESPACE, prefix + request_id)
            if row is None:
                continue
            try:
                value = json.loads(row["value"])
            except ValueError:
                continue
            if not isinstance(value, dict) or value.get("user_id") != user_id:
                continue
            if state == ANSWERED:
                # Collected: the result is medical data, and the framework
                # database is not where that lives.
                db.shared_kv_delete(conn, EXTRACT_NAMESPACE, prefix + request_id)
                return {"state": ANSWERED, "result": value.get("result")}
            return {"state": PENDING}
    return {"state": UNKNOWN}


def keep_import_document(
    ctx, result, *, raw: bytes, filename: str, mime: str | None, max_bytes: int,
):
    """Persist an import upload and stamp ``document_id`` on its result.

    A storage failure must not cost the user the extraction they just waited
    for — losing the file is bad, losing both is worse. So the rows come back
    with ``document_id: null`` and a warning appended instead.
    """
    import sqlite3

    from istota.health import db as health_db
    from istota.health import documents as health_documents

    if not isinstance(result, dict):
        return result
    ocr_text = result.get("ocr_text") or result.get("text") or None
    try:
        with health_db.connect(ctx.db_path) as conn:
            doc, _created = health_documents.store_document(
                conn, ctx, raw=raw, filename=filename, mime=mime,
                source="import", ocr_text=ocr_text, max_bytes=max_bytes,
            )
            conn.commit()
    except (health_documents.DocumentError, OSError, sqlite3.Error) as e:
        logger.error(
            "health_import_document_store_failed user=%s filename=%s error=%s",
            ctx.user_id, filename, e,
        )
        warnings = list(result.get("warnings") or [])
        warnings.append(f"The uploaded file could not be kept: {e}")
        return {**result, "document_id": None, "warnings": warnings}
    return {**result, "document_id": doc.id}


def _health_context(config: "Config", user_id: str):
    from istota.health._loader import resolve_for_user
    from istota.health._migrate import ensure_initialised

    ctx = resolve_for_user(user_id, config)
    ensure_initialised(ctx)
    return ctx


def run_extract(config: "Config", request: dict) -> dict:
    """Run one request's extraction, daemon-side, and return what the page gets.

    An ``error`` key is a failure the page shows instead of a review table. The
    staged upload is removed whatever happens.
    """
    from istota.health import db as health_db
    from istota.health import documents as health_documents
    from istota.health._loader import UserNotFoundError

    user_id = request.get("user_id")
    kind = request.get("kind")
    staged = None
    if not isinstance(user_id, str) or not user_id:
        return {"error": "extraction request names no user"}
    if kind != PANEL:
        staged = _staged_path(config, request.get("staged"))
    try:
        try:
            ctx = _health_context(config, user_id)
        except UserNotFoundError as e:
            return {"error": str(e)}

        if kind == PANEL:
            from istota.health.ocr import extract_from_panel

            with health_db.connect(ctx.db_path) as conn:
                panel = health_db.get_panel(conn, request.get("panel_id"))
            if panel is None:
                return {"error": "panel not found"}
            return extract_from_panel(ctx, panel, config=config)

        if kind not in (ENCOUNTER, IMMUNIZATION):
            return {"error": f"unknown extraction kind {kind!r}"}
        raw = _read_staged(staged) if staged is not None else None
        if raw is None:
            return {"error": "the uploaded file is no longer available"}
        mime = request.get("mime") or "application/octet-stream"
        if kind == ENCOUNTER:
            from istota.health.encounter_ocr import extract_from_file

            result = extract_from_file(staged, mime, config=config, user_id=user_id)
        else:
            from istota.health.immunization_ocr import extract_from_file

            with health_db.connect(ctx.db_path) as conn:
                refs = health_db.list_immunization_refs(conn)
            result = extract_from_file(staged, mime, refs, config=config, user_id=user_id)
        return keep_import_document(
            ctx, result, raw=raw,
            filename=request.get("filename") or "",
            mime=request.get("document_mime"),
            max_bytes=health_documents.max_document_bytes(config),
        )
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def answer_pending(config: "Config") -> None:
    """Answer every pending request, oldest first (ISSUE-697).

    Here rather than in the web process because the daemon is the process
    that can build the extractor's namespace.
    """
    answered: set[str] = set()
    while True:
        request = next_request(config)
        # A request seen twice is one whose answer could not be written; leave
        # it for the next tick rather than spinning on it.
        if request is None or request["id"] in answered:
            return
        answered.add(request["id"])
        try:
            result = run_extract(config, request)
        except Exception:  # noqa: BLE001 - the next request must still be answered
            logger.error(
                "health_extract_failed id=%s kind=%s", request["id"],
                request.get("kind"), exc_info=True,
            )
            result = {"error": "extraction failed"}
        record_answered(config, request, result)
