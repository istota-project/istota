"""FastAPI router for the briefings module.

Mounted by the host at ``/istota/api/briefings``. Reads/writes the per-user
briefings SQLite (blocks/sources/archive). Auth + CSRF + per-user resolution
mirror :mod:`istota.feeds.routes`: the host overrides ``require_auth`` /
``verify_origin`` via ``app.dependency_overrides`` and reads the istota config
off ``request.app.state.istota_config``.

Endpoints:

* Reader:   ``GET /archive`` (paged), ``GET /archive/{id}``.
* Content:  ``GET /config`` (blocks + schedule names),
            ``PUT /blocks`` (create/update/reorder), ``DELETE /blocks/{id}``,
            ``PUT /sources`` (create/update), ``DELETE /sources/{id}``.
* Pickers:  ``GET /feed-options`` (the user's Feeds subs/categories),
            ``GET /browse-presets``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from istota.briefings import db as bdb
from istota.briefings.document import edition_preview
from istota.briefings._loader import UserNotFoundError, resolve_for_user
from istota.briefings._migrate import ensure_initialised
from istota.briefings.models import (
    ALLOWED_SHARED_SOURCE_KINDS,
    BLOCK_TEMPLATES,
    RENDER_MODES,
    SOURCE_KINDS,
    STRUCTURED_KINDS,
    ArchivedBriefing,
    BriefingBlock,
    BriefingsContext,
    validate_shared_block_payload,
    validate_briefing_document,
)
from istota.briefings.sources.browse import BROWSE_PRESETS
from istota.briefings.sources.kv import SHARED_BLOCK_NAMESPACE
from istota.webui.request_guards import BodyError, read_bounded_body
from istota.webui.router_stubs import make_get_user_context, require_auth, verify_origin


logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Auth dependencies — host overrides via app.dependency_overrides
# ---------------------------------------------------------------------------


def _app_config(request: Request):
    return getattr(request.app.state, "istota_config", None)


def require_admin(
    request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """Gate shared-block admin routes on shared-content-writer authority.

    Uses ``Config.is_shared_kv_writer`` — the same fail-closed gate that governs
    every ``shared_kv`` write (an empty admins allowlist authorizes NOBODY). This
    is the correct authority for admin surfaces that create content flowing into
    other users' briefings. It matches ``web_app._user_is_web_admin`` except
    under ``auth = "none"``, where that gate admits the local user and this
    one still requires the admins file.
    """
    cfg = _app_config(request)
    username = user.get("username", "")
    if cfg is None or not cfg.is_shared_kv_writer(username):
        raise HTTPException(403, "admin only")
    return user


get_user_context = make_get_user_context(
    cache_attr="briefings_initialised_dbs",
    resolve=resolve_for_user,
    ensure=lambda ctx, cfg: ensure_initialised(ctx, app_config=cfg),
    not_found=UserNotFoundError,
)


# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------


def _map_block(block: BriefingBlock) -> dict:
    return {
        "id": block.id,
        "briefing_name": block.briefing_name,
        "position": block.position,
        "title": block.title,
        "directive": block.directive or "",
        "render_mode": block.render_mode,
        "options": block.options or {},
        "sources": [
            {
                "id": s.id,
                "position": s.position,
                "kind": s.kind,
                "config": s.config or {},
                "enabled": s.enabled,
            }
            for s in block.sources
        ],
    }


def _map_archive(row: ArchivedBriefing, *, with_body: bool = True) -> dict:
    out = {
        "id": row.id,
        "briefing_name": row.briefing_name,
        "subject": row.subject or "",
        "generated_at": row.generated_at,
        "task_id": row.task_id,
        "delivered_to": row.delivered_to,
        "read_at": row.read_at,
    }
    if with_body:
        out["body_md"] = row.body_md
    return out


def _schedule_names(cfg, username: str) -> list[str]:
    """The user's briefing names from the framework schedule (for the editor).

    Unions the in-memory config snapshot (``get_briefings_for_user`` — refreshed
    only at config load / SIGHUP) with the live ``briefing_configs`` DB rows, so
    a briefing just added through ``POST /settings/briefings`` shows up in the
    content-block editor's dropdown without waiting for a server reload.
    """
    names: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    try:
        from istota.skills.briefing import get_briefings_for_user
        for b in get_briefings_for_user(cfg, username):
            _add(b.name)
    except Exception:  # noqa: BLE001
        pass

    try:
        from istota.user_briefings import list_briefings
        for b in list_briefings(cfg.db_path, username):
            _add(b.name)
    except Exception:  # noqa: BLE001
        pass

    return names


# ---------------------------------------------------------------------------
# Reader — archive
# ---------------------------------------------------------------------------


@router.get("/archive")
def get_archive(
    request: Request,
    ctx: BriefingsContext = Depends(get_user_context),
    briefing_name: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    """Paged archive list (newest first), bodies included."""
    with bdb.connect(ctx.db_path) as conn:
        rows = bdb.list_archive(
            conn, briefing_name=briefing_name, limit=limit, offset=offset,
        )
        total = bdb.count_archive(conn, briefing_name=briefing_name)
        names = bdb.list_briefing_names(conn)
    return {
        "items": [
            {**_map_archive(r), "preview": edition_preview(r.body_md, r.document)}
            for r in rows
        ],
        "total": total,
        "briefing_names": names,
    }


@router.get("/archive/{archive_id}")
def get_archive_item(
    archive_id: int,
    ctx: BriefingsContext = Depends(get_user_context),
) -> dict:
    with bdb.connect(ctx.db_path) as conn:
        row = bdb.get_archived(conn, archive_id)
    if not row:
        raise HTTPException(404, "briefing not found")
    return {**_map_archive(row), "document": row.document}


@router.post("/archive/{archive_id}/read")
def mark_archive_item_read(
    archive_id: int,
    _: None = Depends(verify_origin),
    ctx: BriefingsContext = Depends(get_user_context),
) -> dict:
    with bdb.connect(ctx.db_path) as conn:
        row = bdb.mark_archived_read(conn, archive_id)
        conn.commit()
    if row is None:
        raise HTTPException(404, "briefing not found")
    return {"id": row.id, "read_at": row.read_at}


@router.post("/mark-as-read")
async def mark_archive_as_read(
    request: Request,
    _: None = Depends(verify_origin),
    ctx: BriefingsContext = Depends(get_user_context),
) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "expected a JSON object")
    if not isinstance(body, dict) or body.get("scope") not in ("all", "briefing"):
        raise HTTPException(400, "scope must be all or briefing")
    briefing_name = None
    if body["scope"] == "all":
        if set(body) != {"scope"}:
            raise HTTPException(400, "scope=all accepts only scope")
    else:
        briefing_name = body.get("briefing_name")
        if (
            set(body) != {"scope", "briefing_name"}
            or not isinstance(briefing_name, str)
            or not briefing_name
        ):
            raise HTTPException(400, "scope=briefing requires a non-empty briefing_name")

    def _update():
        with bdb.connect(ctx.db_path) as conn:
            updated = bdb.mark_archive_as_read(conn, briefing_name=briefing_name)
            conn.commit()
        return updated

    updated = await asyncio.to_thread(_update)
    return {"status": "ok", "updated": updated}


@router.delete("/archive/{archive_id}")
def delete_archive_item(
    archive_id: int,
    _: None = Depends(verify_origin),
    ctx: BriefingsContext = Depends(get_user_context),
) -> dict:
    """Remove a single archived briefing result from the reader/archive."""
    with bdb.connect(ctx.db_path) as conn:
        deleted = bdb.delete_archived(conn, archive_id)
        conn.commit()
    if not deleted:
        raise HTTPException(404, "briefing not found")
    return {"status": "ok"}


@router.post("/preview")
async def preview_document(
    request: Request,
    _: None = Depends(verify_origin),
    user: dict = Depends(require_auth),
) -> dict:
    """Render inline content without initialising or consulting the content DB."""
    from istota.briefings.document import document_to_markdown
    from istota.skills.briefing import render_briefing_document_html, render_briefing_document_plain

    cfg = _app_config(request)
    if cfg is not None and not cfg.is_module_enabled(user["username"], "briefings"):
        raise HTTPException(403, "briefings disabled")
    try:
        raw = await read_bounded_body(request, 2 * 1024 * 1024)
    except BodyError as error:
        raise HTTPException(error.status, "briefing preview body refused") from None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
        document = payload.get("document")
        if isinstance(document, dict) and isinstance(document.get("blocks"), list):
            if len(document["blocks"]) > 100:
                raise HTTPException(413, "too many preview blocks")
            for block in document["blocks"]:
                body = block.get("body_md") if isinstance(block, dict) else None
                if isinstance(body, str) and len(body.encode("utf-8")) > 64 * 1024:
                    raise HTTPException(413, "preview block too large")
        document = validate_briefing_document(document)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(400, "invalid briefing preview document") from None
    return {
        "body_md": document_to_markdown(document),
        "plain": render_briefing_document_plain(document),
        "html": render_briefing_document_html(document),
    }


# ---------------------------------------------------------------------------
# Content model — config / blocks / sources
# ---------------------------------------------------------------------------


@router.get("/config")
def get_config(
    request: Request,
    user: dict = Depends(require_auth),
    ctx: BriefingsContext = Depends(get_user_context),
) -> dict:
    """The content model grouped by briefing name + the schedule names."""
    cfg = _app_config(request)
    with bdb.connect(ctx.db_path) as conn:
        names = bdb.list_briefing_names(conn)
        briefings = [
            {
                "name": name,
                "blocks": [_map_block(b) for b in bdb.list_blocks(conn, name)],
            }
            for name in names
        ]
    return {
        "briefings": briefings,
        "schedule_names": _schedule_names(cfg, user["username"]),
        "source_kinds": list(SOURCE_KINDS),
        "structured_kinds": list(STRUCTURED_KINDS),
        "block_templates": BLOCK_TEMPLATES,
    }


@router.put("/blocks")
def put_block(
    request: Request,
    _: None = Depends(verify_origin),
    ctx: BriefingsContext = Depends(get_user_context),
    payload: dict = Body(...),
) -> dict:
    """Create, update, or reorder blocks.

    Modes (by payload shape):
    * ``{"reorder": {"briefing_name", "ordered_ids": [...]}}`` — reorder.
    * ``{"id": N, ...}`` — update an existing block.
    * Optional ``sources`` replaces the complete source list atomically.
    * ``{"briefing_name", "title", ...}`` — create a new block.
    """
    with bdb.connect(ctx.db_path) as conn:
        try:
            if "reorder" in payload:
                reorder = payload["reorder"]
                if not isinstance(reorder, dict):
                    raise ValueError("reorder must be an object")
                name = reorder.get("briefing_name")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("briefing_name required")
                ordered = reorder.get("ordered_ids")
                bdb.reorder_blocks(conn, name.strip(), ordered)
                conn.commit()
                return {"status": "ok", "reordered": len(ordered)}
            block = bdb.save_block_definition(conn, payload=payload)
            conn.commit()
        except LookupError:
            raise HTTPException(404, "block not found") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    return {"status": "ok", "block": _map_block(block)}


@router.delete("/blocks/{block_id}")
def delete_block(
    block_id: int,
    _: None = Depends(verify_origin),
    ctx: BriefingsContext = Depends(get_user_context),
) -> dict:
    with bdb.connect(ctx.db_path) as conn:
        bdb.delete_block(conn, block_id)
        conn.commit()
    return {"status": "ok"}


@router.put("/sources")
def put_source(
    request: Request,
    _: None = Depends(verify_origin),
    ctx: BriefingsContext = Depends(get_user_context),
    payload: dict = Body(...),
) -> dict:
    """Create or update a source on a block.

    * ``{"id": N, "config"?, "enabled"?}`` — update.
    * ``{"block_id", "kind", "config"?}`` — create.
    """
    with bdb.connect(ctx.db_path) as conn:
        if payload.get("id"):
            bdb.update_source(
                conn, int(payload["id"]),
                config=payload.get("config"),
                enabled=payload.get("enabled"),
            )
            conn.commit()
            return {"status": "ok"}

        block_id = payload.get("block_id")
        kind = payload.get("kind")
        if not block_id or kind not in SOURCE_KINDS:
            raise HTTPException(400, "block_id and a valid kind required")
        if not bdb.get_block(conn, int(block_id), with_sources=False):
            raise HTTPException(404, "block not found")
        source_id = bdb.add_source(
            conn, block_id=int(block_id), kind=kind,
            config=payload.get("config") or {},
        )
        conn.commit()
        return {"status": "ok", "id": source_id}


@router.delete("/sources/{source_id}")
def delete_source(
    source_id: int,
    _: None = Depends(verify_origin),
    ctx: BriefingsContext = Depends(get_user_context),
) -> dict:
    with bdb.connect(ctx.db_path) as conn:
        bdb.delete_source(conn, source_id)
        conn.commit()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Pickers
# ---------------------------------------------------------------------------


@router.get("/browse-presets")
def get_browse_presets() -> dict:
    return {
        "presets": [
            {"key": k, "name": v["name"], "url": v["url"]}
            for k, v in BROWSE_PRESETS.items()
        ]
    }


@router.get("/feed-options")
def get_feed_options(
    request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """The user's Feeds subscriptions + categories for the RSS source picker.

    Soft-degrades to empty lists when the Feeds module is off / unavailable.
    """
    cfg = _app_config(request)
    subscriptions: list[dict] = []
    categories: list[dict] = []
    try:
        from istota import feeds
        from istota.feeds import db as feeds_db

        fctx = feeds.resolve_for_user(user["username"], cfg)
        with feeds_db.connect(fctx.db_path) as conn:
            for f in feeds_db.list_feeds(conn):
                subscriptions.append({
                    "kind": "subscription", "value": f.id,
                    "label": f.title or f.url,
                })
            for c in feeds_db.list_categories(conn):
                categories.append({
                    "kind": "category", "value": c.id, "label": c.title,
                })
    except Exception:  # noqa: BLE001
        pass
    return {
        "available": bool(subscriptions or categories),
        "subscriptions": subscriptions,
        "categories": categories,
    }


# ---------------------------------------------------------------------------
# Shared briefing blocks (admin-editable; admin-shared-briefing-blocks spec)
# ---------------------------------------------------------------------------

def _shared_last_run_utc(value: str | None, tz_name: str) -> str | None:
    """The scheduler's naive last-run stamp, which is wall-clock time in the
    shared-block timezone, as ISO UTC: the clock ``run_answered.at`` is on."""
    if not value:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - the scheduler falls back the same way
        tz = ZoneInfo("UTC")
    try:
        stamped = datetime.fromisoformat(value)
    except ValueError:
        return value
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=tz)
    return stamped.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shared_block_status(conn, name: str, tz_name: str) -> dict:
    """Per-block runtime status: last generation, current shared_kv preview,
    and any Run now request with the daemon's last answer to one."""
    from istota import db as fdb
    from istota.briefings import shared_block_runs

    last_run_at = _shared_last_run_utc(
        fdb.get_briefing_shared_block_last_run(conn, name), tz_name,
    )
    val = fdb.shared_kv_get(conn, SHARED_BLOCK_NAMESPACE, name)
    preview = None
    updated_at = None
    stored_trusted = None
    if val:
        updated_at = val.get("updated_at")
        try:
            import json as _json
            parsed = _json.loads(val["value"])
            if isinstance(parsed, dict):
                stored_trusted = bool(parsed.get("trusted", False))
                text = parsed.get("text")
                if isinstance(text, str):
                    preview = text[:400]
        except (ValueError, TypeError):
            preview = (val.get("value") or "")[:400]
    runs = shared_block_runs.run_state(conn, name)
    return {
        "run_pending": runs["pending"],
        "run_answered": runs["answered"],
        "last_run_at": last_run_at,
        "value_updated_at": updated_at,
        "value_preview": preview,
        "stored_trusted": stored_trusted,
        "has_content": val is not None,
    }


def _map_shared_block(row, status: dict) -> dict:
    return {
        "name": row.name,
        "cron": row.cron,
        "title": row.title,
        "directive": row.directive or "",
        "render_mode": row.render_mode,
        "enabled": row.enabled,
        "trusted": row.trusted,
        "sources": row.sources,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "status": status,
    }


@router.get("/shared-blocks")
def list_shared_blocks(
    request: Request,
    _admin: dict = Depends(require_admin),
) -> dict:
    """List shared-block definitions + per-block status. Admin-gated."""
    from istota import db as fdb

    cfg = _app_config(request)
    with fdb.get_db(cfg.db_path) as conn:
        rows = fdb.list_shared_block_configs(conn)
        tz_name = (cfg.briefings.shared_block_timezone or "").strip() or "UTC"
        blocks = [
            _map_shared_block(r, _shared_block_status(conn, r.name, tz_name)) for r in rows
        ]
    return {
        "shared_blocks": blocks,
        "allowed_source_kinds": sorted(ALLOWED_SHARED_SOURCE_KINDS),
        "render_modes": list(RENDER_MODES),
        "shared_block_timezone": cfg.briefings.shared_block_timezone or "UTC",
    }


def _validate_shared_block_payload(payload: dict) -> dict:
    try:
        return validate_shared_block_payload(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.put("/shared-blocks")
def put_shared_block(
    request: Request,
    _admin: dict = Depends(require_admin),
    __: None = Depends(verify_origin),
    payload: dict = Body(...),
) -> dict:
    """Create or update a shared-block definition (keyed on name). Admin-gated."""
    from istota import db as fdb

    fields = _validate_shared_block_payload(payload)
    cfg = _app_config(request)
    with fdb.get_db(cfg.db_path) as conn:
        row = fdb.upsert_shared_block_config(conn, **fields)
        tz_name = (cfg.briefings.shared_block_timezone or "").strip() or "UTC"
        status = _shared_block_status(conn, row.name, tz_name)
    return {"status": "ok", "shared_block": _map_shared_block(row, status)}


@router.delete("/shared-blocks/{name}")
def delete_shared_block(
    name: str,
    request: Request,
    _admin: dict = Depends(require_admin),
    __: None = Depends(verify_origin),
    delete_value: bool = Query(False),
) -> dict:
    """Delete a shared-block definition. Admin-gated.

    By default the last ``shared_kv`` value is left in place (consuming briefings
    keep splicing it until it goes stale, then the freshness gate omits it). Pass
    ``?delete_value=true`` for a hard removal.
    """
    from istota import db as fdb
    from istota.briefings import shared_block_runs

    cfg = _app_config(request)
    with fdb.get_db(cfg.db_path) as conn:
        removed = fdb.delete_shared_block_config(conn, name)
        if removed:
            shared_block_runs.forget(conn, name)
        if delete_value:
            fdb.shared_kv_delete(conn, SHARED_BLOCK_NAMESPACE, name)
    if not removed:
        raise HTTPException(404, "shared block not found")
    return {"status": "ok", "deleted_value": bool(delete_value)}


@router.post("/shared-blocks/{name}/run", status_code=202)
def run_shared_block_now(
    name: str,
    request: Request,
    _admin: dict = Depends(require_admin),
    __: None = Depends(verify_origin),
) -> dict:
    """Run-now: ask the daemon to regenerate a shared block on its next tick.

    Never generates here (ISSUE-700). On the Docker stack this process is not on
    the browser's network, so a ``browse`` source came back empty and replaced
    the last good block, and its brain breaker is not the daemon's. Joins a
    request still pending; the page waits for ``status.run_answered`` to name
    the returned id.
    """
    from istota import db as fdb
    from istota.briefings import shared_block_runs

    cfg = _app_config(request)
    with fdb.get_db(cfg.db_path) as conn:
        row = fdb.get_shared_block_config(conn, name)
    if row is None:
        raise HTTPException(404, "shared block not found")
    made = shared_block_runs.request_run(cfg, name)
    return {"id": made["id"]}


@router.get("/email-accounts")
def get_email_accounts(
    request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """The signed-in user's mail accounts a ``my_email`` source can read.

    Always the bot plus-address; Gmail too when Google Workspace is on, marked
    unavailable until the user grants Gmail access.
    """
    from istota import db as fdb
    from istota.briefings.sources.my_email import list_accounts

    cfg = _app_config(request)
    with fdb.get_db(cfg.db_path) as conn:
        return {"accounts": list_accounts(cfg, conn, user["username"])}


@router.get("/shared-block-options")
def get_shared_block_options(
    request: Request,
    user: dict = Depends(require_auth),
) -> dict:
    """Live pickable shared blocks for the per-user Shared source dropdown.

    Available to any authenticated user (read-only discovery). Lists every live
    ``shared_kv`` key in the shared-block namespace, tagged ``config`` (has a
    definition row) or ``custom`` (published by a job, no definition). A defined
    block with no content yet is also surfaced so a user can reference it ahead
    of first generation.
    """
    from istota import db as fdb

    cfg = _app_config(request)
    options: dict[str, dict] = {}
    with fdb.get_db(cfg.db_path) as conn:
        defined = {r.name for r in fdb.list_shared_block_configs(conn)}
        for r in fdb.list_shared_block_configs(conn):
            options[r.name] = {
                "name": r.name, "updated_at": None, "has_content": False,
                "source": "config",
            }
        for row in fdb.shared_kv_list(conn, SHARED_BLOCK_NAMESPACE):
            key = row["key"]
            options[key] = {
                "name": key,
                "updated_at": row.get("updated_at"),
                "has_content": True,
                "source": "config" if key in defined else "custom",
            }
    return {"options": sorted(options.values(), key=lambda o: o["name"])}


# File-path picker for todos / reminders / notes sources. The path a user
# types is resolved relative to their own /Users/<uid>/ folder (see
# sources.builtins._resolve_user_path); these endpoints let the editor verify a
# path exists and offer a datalist of candidate text files.
_PATH_SUGGEST_EXTS = (".md", ".txt")
_PATH_SUGGEST_MAX = 50
_PATH_SUGGEST_DEPTH = 6
# Hard cap on candidate files inspected per request, so the query walk stays
# bounded even on a huge workspace where few paths match.
_PATH_SUGGEST_SCAN_MAX = 8000


def _walk_text_files(root: Path, query: str = "") -> list[str]:
    """Bounded walk of ``root`` for ``.md`` / ``.txt`` files matching ``query``.

    Returns paths relative to ``root`` (forward-slashed) — the same form the
    user types into the source path field. When ``query`` is given, only paths
    whose relative form contains it (case-insensitive substring) are returned,
    so a deep or late file still surfaces as the user types; matches whose
    basename hits the query rank ahead of directory-only hits. Bounded by
    depth, a total-scan cap, and the returned-count cap; hidden directories are
    skipped.
    """
    needle = query.strip().lower()
    root_str = str(root)
    scanned = 0
    name_hits: list[str] = []  # query matched the filename
    path_hits: list[str] = []  # query matched only a parent directory
    for dirpath, dirnames, filenames in os.walk(root_str):
        rel_dir = os.path.relpath(dirpath, root_str)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        kept = sorted(d for d in dirnames if not d.startswith("."))
        dirnames[:] = [] if depth >= _PATH_SUGGEST_DEPTH else kept
        for fn in sorted(filenames):
            if not fn.lower().endswith(_PATH_SUGGEST_EXTS):
                continue
            scanned += 1
            rel = os.path.relpath(os.path.join(dirpath, fn), root_str).replace(os.sep, "/")
            if not needle:
                name_hits.append(rel)
            elif needle in fn.lower():
                name_hits.append(rel)
            elif needle in rel.lower():
                path_hits.append(rel)
            if len(name_hits) >= _PATH_SUGGEST_MAX or scanned >= _PATH_SUGGEST_SCAN_MAX:
                return (name_hits + path_hits)[:_PATH_SUGGEST_MAX]
    return (name_hits + path_hits)[:_PATH_SUGGEST_MAX]


@router.get("/path-check")
def check_path(
    request: Request,
    path: str = Query("", description="Candidate source file path"),
    user: dict = Depends(require_auth),
) -> dict:
    """Verify a todos/reminders/notes source path resolves to an existing file.

    Resolves the candidate exactly as the resolver does (relative to the user's
    own folder) and checks existence. ``{ok: true, resolved}`` or
    ``{ok: false, error}``.
    """
    from istota.briefings.sources.builtins import _resolve_user_path
    from istota.skills.files import path_exists

    resolved = _resolve_user_path(user["username"], path)
    if not resolved:
        return {"ok": False, "error": "Enter a file path."}
    cfg = _app_config(request)
    exists = False
    try:
        if cfg is not None and cfg.has_workspace:
            # The resolver's own read (ISSUE-596), so this says yes exactly
            # when the briefing will read the file, and a symlink planted in
            # the user's tree cannot answer whether its target exists.
            from istota.storage import read_owner_text

            text, _reason, _definitive = read_owner_text(
                cfg, user["username"], resolved,
            )
            exists = text is not None
        elif cfg is not None:
            exists = path_exists(cfg, resolved)
    except Exception:  # noqa: BLE001
        exists = False
    if not exists:
        return {"ok": False, "error": "No file found at that path."}
    return {"ok": True, "resolved": resolved}


@router.get("/path-suggest")
def suggest_paths(
    request: Request,
    q: str = Query("", description="Filter suggestions by substring"),
    user: dict = Depends(require_auth),
) -> dict:
    """Candidate text-file paths under the user's folder, for the autocomplete.

    Bounded, ``.md`` / ``.txt`` only, returned relative to the user's own
    folder (the form the user types). ``q`` filters server-side (a substring of
    the relative path), so a deep or late-in-walk file still surfaces as the
    user types rather than being clipped by the bound. Empty without a
    workspace.
    """
    cfg = _app_config(request)
    root = cfg.workspace_root(user["username"]) if cfg is not None else None
    paths = _walk_text_files(root, q) if root is not None and root.is_dir() else []
    return {"paths": paths}
