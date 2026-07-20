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

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from istota.briefings import db as bdb
from istota.briefings._loader import UserNotFoundError, resolve_for_user
from istota.briefings._migrate import ensure_initialised
from istota.briefings.models import (
    SOURCE_KINDS,
    STRUCTURED_KINDS,
    ArchivedBriefing,
    BriefingBlock,
    BriefingsContext,
)
from istota.briefings.sources.browse import BROWSE_PRESETS


logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Auth dependencies — host overrides via app.dependency_overrides
# ---------------------------------------------------------------------------


def require_auth(request: Request) -> dict:
    user = None
    try:
        user = request.session.get("user")
    except (AssertionError, AttributeError):
        pass
    if not user:
        raise HTTPException(401, "unauthorized")
    return user


def verify_origin(request: Request) -> None:
    return None


def _app_config(request: Request):
    return getattr(request.app.state, "istota_config", None)


def get_user_context(
    request: Request,
    user: dict = Depends(require_auth),
) -> BriefingsContext:
    cfg = _app_config(request)
    try:
        ctx = resolve_for_user(user["username"], cfg)
    except UserNotFoundError as e:
        raise HTTPException(404, str(e))
    cache: set = getattr(request.app.state, "briefings_initialised_dbs", None)
    if cache is None:
        cache = set()
        request.app.state.briefings_initialised_dbs = cache
    if ctx.db_path not in cache:
        ensure_initialised(ctx, app_config=cfg)
        cache.add(ctx.db_path)
    else:
        ctx.ensure_dirs()
    return ctx


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
            if b.enabled:
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
        "items": [_map_archive(r) for r in rows],
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
    return _map_archive(row)


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
    * ``{"briefing_name", "title", ...}`` — create a new block.
    """
    with bdb.connect(ctx.db_path) as conn:
        if "reorder" in payload:
            r = payload["reorder"]
            name = (r.get("briefing_name") or "").strip()
            ordered = [int(x) for x in (r.get("ordered_ids") or [])]
            if not name:
                raise HTTPException(400, "briefing_name required")
            bdb.reorder_blocks(conn, name, ordered)
            conn.commit()
            return {"status": "ok", "reordered": len(ordered)}

        if payload.get("id"):
            block_id = int(payload["id"])
            existing = bdb.get_block(conn, block_id, with_sources=False)
            if not existing:
                raise HTTPException(404, "block not found")
            bdb.update_block(
                conn, block_id,
                title=payload.get("title"),
                directive=payload.get("directive"),
                render_mode=_valid_render_mode(payload.get("render_mode")),
                options=payload.get("options"),
            )
            conn.commit()
            block = bdb.get_block(conn, block_id)
            return {"status": "ok", "block": _map_block(block)}

        name = (payload.get("briefing_name") or "").strip()
        title = (payload.get("title") or "").strip()
        if not name or not title:
            raise HTTPException(400, "briefing_name and title required")
        block_id = bdb.add_block(
            conn, briefing_name=name, title=title,
            directive=payload.get("directive"),
            render_mode=_valid_render_mode(payload.get("render_mode")) or "synthesis",
            options=payload.get("options") or {},
        )
        conn.commit()
        block = bdb.get_block(conn, block_id)
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


def _valid_render_mode(value):
    from istota.briefings.models import RENDER_MODES
    if value is None:
        return None
    return value if value in RENDER_MODES else "synthesis"
