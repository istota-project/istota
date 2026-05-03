"""FastAPI router for the native feeds backend.

Mounted by the host application at ``/istota/api/feeds`` when
``[feeds] backend = "native"`` is set. Reads/writes the per-user workspace
SQLite populated by the native poller, returning the same JSON shape as
the legacy Miniflux proxy in ``web_app.py`` so the SvelteKit reader is
backend-agnostic.

Auth and per-user resolution mirror :mod:`istota.money.routes`: the host
overrides ``require_auth`` via ``app.dependency_overrides`` and the istota
config is read off ``request.app.state.istota_config``.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi import File as FastAPIFile
from fastapi.responses import JSONResponse, PlainTextResponse

from istota.feeds import db as feeds_db
from istota.feeds._config_io import read_feeds_config, write_feeds_config
from istota.feeds._loader import UserNotFoundError, resolve_for_user
from istota.feeds.models import (
    DEFAULT_POLL_INTERVAL_MINUTES,
    FeedsContext,
    default_poll_interval_for,
    detect_source_type,
)


# ---------------------------------------------------------------------------
# Auth dependency — host app overrides via app.dependency_overrides
# ---------------------------------------------------------------------------


def require_auth(request: Request) -> dict:
    """Return ``{"username": ..., "display_name": ...}`` or raise 401.

    Default reads ``request.session["user"]`` (Starlette SessionMiddleware).
    The host overrides this with its own auth dependency.
    """
    user = None
    try:
        user = request.session.get("user")
    except (AssertionError, AttributeError):
        # No SessionMiddleware installed.
        pass
    if not user:
        raise HTTPException(401, "unauthorized")
    return user


def get_user_context(
    request: Request,
    user: dict = Depends(require_auth),
) -> FeedsContext:
    istota_config = getattr(request.app.state, "istota_config", None)
    try:
        ctx = resolve_for_user(user["username"], istota_config)
    except UserNotFoundError as e:
        raise HTTPException(404, str(e))
    ctx.ensure_dirs()
    # init_db sets WAL and runs CREATE TABLE IF NOT EXISTS. WAL is persistent
    # in the SQLite file header, so we only need to run it once per DB per
    # process. Caching by db_path also prevents concurrent requests from
    # racing on the journal_mode transition lock.
    cache: set = getattr(request.app.state, "feeds_initialised_dbs", None)
    if cache is None:
        cache = set()
        request.app.state.feeds_initialised_dbs = cache
    if ctx.db_path not in cache:
        feeds_db.init_db(ctx.db_path)
        cache.add(ctx.db_path)
    return ctx


# ---------------------------------------------------------------------------
# Mappers — keep wire-format identical to the Miniflux proxy
# ---------------------------------------------------------------------------


def _map_feed(feed, cat_by_id: dict) -> dict:
    cat = cat_by_id.get(feed.category_id)
    return {
        "id": feed.id,
        "title": feed.title or feed.url,
        "site_url": feed.site_url or "",
        "category": {
            "id": cat.id if cat else 0,
            "title": cat.title if cat else "",
        },
    }


def _map_entry(entry, feed_by_id: dict, cat_by_id: dict) -> dict:
    feed = feed_by_id.get(entry.feed_id)
    cat = cat_by_id.get(feed.category_id) if feed else None
    return {
        "id": entry.id,
        "title": entry.title or "",
        "url": entry.url or "",
        "content": entry.content_html or "",
        "images": list(entry.image_urls or []),
        "feed": {
            "id": feed.id if feed else 0,
            "title": (feed.title or feed.url) if feed else "",
            "site_url": (feed.site_url or "") if feed else "",
            "category": {
                "id": cat.id if cat else 0,
                "title": cat.title if cat else "",
            },
        },
        "status": entry.status,
        "published_at": entry.published_at or "",
        "created_at": entry.fetched_at or "",
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter()


@router.get("")
async def api_feeds(
    ctx: FeedsContext = Depends(get_user_context),
    limit: int = Query(default=500, le=1000),
    offset: int = Query(default=0, ge=0),
    order: str = Query(default="published_at"),
    direction: str = Query(default="desc"),
    status: str = Query(default=""),
    category_id: int = Query(default=0),
    feed_id: int = Query(default=0),
    before: int = Query(default=0, ge=0),
):
    """List feeds and entries — mirrors Miniflux ``GET /v1/entries`` + ``/v1/feeds``."""

    def _query():
        with feeds_db.connect(ctx.db_path) as conn:
            cats = feeds_db.list_categories(conn)
            feeds = feeds_db.list_feeds(conn)
            entries = feeds_db.list_entries(
                conn,
                limit=limit,
                offset=offset,
                status=status or None,
                feed_id=feed_id or None,
                category_id=category_id or None,
                before_published_ts=before or None,
                order=order,
                direction=direction,
            )
            total = feeds_db.count_entries(
                conn,
                status=status or None,
                feed_id=feed_id or None,
                category_id=category_id or None,
            )
        return cats, feeds, entries, total

    cats, feeds, entries, total = await asyncio.to_thread(_query)
    cat_by_id = {c.id: c for c in cats}
    feed_by_id = {f.id: f for f in feeds}

    return {
        "feeds": [_map_feed(f, cat_by_id) for f in feeds],
        "entries": [_map_entry(e, feed_by_id, cat_by_id) for e in entries],
        "total": total,
    }


@router.put("/entries/batch")
async def api_update_entries_batch(
    request: Request,
    ctx: FeedsContext = Depends(get_user_context),
):
    body = await request.json()
    entry_ids = body.get("entry_ids", [])
    if not entry_ids or not isinstance(entry_ids, list):
        return JSONResponse(
            {"error": "entry_ids must be a non-empty list"}, status_code=400,
        )
    new_status = body.get("status", "read")
    if new_status not in ("read", "unread", "removed"):
        return JSONResponse(
            {"error": "status must be one of: read, unread, removed"}, status_code=400,
        )

    def _update():
        with feeds_db.connect(ctx.db_path) as conn:
            count = feeds_db.update_entry_status(conn, list(entry_ids), new_status)
            conn.commit()
        return count

    updated = await asyncio.to_thread(_update)
    return {"status": "ok", "updated": updated}


@router.put("/entries/{entry_id}")
async def api_update_entry(
    entry_id: int,
    request: Request,
    ctx: FeedsContext = Depends(get_user_context),
):
    body = await request.json()
    new_status = body.get("status", "read")
    if new_status not in ("read", "unread", "removed"):
        return JSONResponse(
            {"error": "status must be one of: read, unread, removed"}, status_code=400,
        )

    def _update():
        with feeds_db.connect(ctx.db_path) as conn:
            count = feeds_db.update_entry_status(conn, [entry_id], new_status)
            conn.commit()
        return count

    updated = await asyncio.to_thread(_update)
    return {"status": "ok", "updated": updated}


# ---------------------------------------------------------------------------
# Settings — feeds.toml CRUD + OPML
# ---------------------------------------------------------------------------


@router.get("/config")
async def api_get_config(ctx: FeedsContext = Depends(get_user_context)):
    """Return the parsed feeds.toml plus runtime diagnostics."""

    def _read():
        cfg = read_feeds_config(ctx.config_path)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds = feeds_db.list_feeds(conn)
            total_entries = feeds_db.count_entries(conn)
            unread = feeds_db.count_entries(conn, status="unread")
        diagnostics = {
            "total_feeds": len(feeds),
            "total_entries": total_entries,
            "unread_entries": unread,
            "error_feeds": sum(1 for f in feeds if f.error_count > 0),
            "last_poll_at": max(
                (f.last_fetched_at for f in feeds if f.last_fetched_at),
                default=None,
            ),
        }
        feed_state = [
            {
                "url": f.url,
                "last_fetched_at": f.last_fetched_at,
                "last_error": f.last_error,
                "error_count": f.error_count,
            }
            for f in feeds
        ]
        return cfg, diagnostics, feed_state

    cfg, diagnostics, feed_state = await asyncio.to_thread(_read)
    return {
        "config": cfg,
        "diagnostics": diagnostics,
        "feed_state": feed_state,
    }


@router.put("/config")
async def api_put_config(
    request: Request,
    ctx: FeedsContext = Depends(get_user_context),
):
    """Write feeds.toml from the request body, then resync into the DB."""
    body = await request.json()
    config_payload = body.get("config")
    if not isinstance(config_payload, dict):
        return JSONResponse(
            {"error": "body must be {'config': {...}}"}, status_code=400,
        )

    err = _validate_feeds_config(config_payload)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    def _save():
        write_feeds_config(ctx.config_path, config_payload)
        return _sync_config_to_db(ctx)

    summary = await asyncio.to_thread(_save)
    return {"status": "ok", "sync": summary}


@router.get("/export-opml", response_class=PlainTextResponse)
async def api_export_opml(ctx: FeedsContext = Depends(get_user_context)):
    from istota.feeds.opml import export_opml

    text = await asyncio.to_thread(export_opml, ctx)
    return PlainTextResponse(
        text,
        media_type="text/x-opml",
        headers={"Content-Disposition": 'attachment; filename="feeds.opml"'},
    )


@router.post("/refresh")
async def api_refresh(
    ctx: FeedsContext = Depends(get_user_context),
):
    """Mark every feed due now. The scheduled job ``_module.feeds.run_scheduled``
    (cron ``*/5``) picks them up out-of-process — keeping the long sequential
    poll out of the web request lifecycle so shutdown is fast and one user's
    refresh can't tie up uvicorn workers.
    """

    def _reset() -> int:
        with feeds_db.connect(ctx.db_path) as conn:
            cur = conn.execute("UPDATE feeds SET next_poll_at = NULL")
            conn.commit()
            return cur.rowcount or 0

    queued = await asyncio.to_thread(_reset)
    return {"status": "queued", "feeds_queued": queued}


@router.post("/import-opml")
async def api_import_opml(
    ctx: FeedsContext = Depends(get_user_context),
    file: UploadFile = FastAPIFile(...),
):
    from istota.feeds.opml import import_opml

    raw = await file.read()
    if not raw:
        return JSONResponse({"error": "empty upload"}, status_code=400)
    if len(raw) > 5 * 1024 * 1024:
        return JSONResponse({"error": "OPML too large (>5MB)"}, status_code=413)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return JSONResponse({"error": "OPML must be UTF-8"}, status_code=400)

    try:
        result = await asyncio.to_thread(import_opml, ctx, text)
    except Exception as e:
        return JSONResponse({"error": f"OPML parse failed: {e}"}, status_code=400)

    # Project the DB back to feeds.toml so the settings UI sees the new state.
    await asyncio.to_thread(_dump_db_to_config, ctx)

    return {
        "status": "ok",
        "feeds_added": result.feeds_added,
        "feeds_updated": result.feeds_updated,
        "categories_added": result.categories_added,
        "rewritten_bridger_urls": result.rewritten_bridger_urls,
    }


# ---------------------------------------------------------------------------
# Helpers — config validation + DB sync (parallel to cli._sync_config_to_db)
# ---------------------------------------------------------------------------


def _validate_feeds_config(cfg: dict) -> str | None:
    """Return an error string if ``cfg`` is malformed; ``None`` if OK."""
    if "feeds" in cfg and not isinstance(cfg["feeds"], list):
        return "feeds must be a list"
    if "categories" in cfg and not isinstance(cfg["categories"], list):
        return "categories must be a list"
    for f in cfg.get("feeds") or []:
        if not isinstance(f, dict):
            return "each feed must be an object"
        if not str(f.get("url") or "").strip():
            return "every feed needs a non-empty url"
    for c in cfg.get("categories") or []:
        if not isinstance(c, dict):
            return "each category must be an object"
        if not str(c.get("slug") or "").strip():
            return "every category needs a non-empty slug"
    settings = cfg.get("settings") or {}
    if settings:
        if not isinstance(settings, dict):
            return "settings must be an object"
        interval = settings.get("default_poll_interval_minutes")
        if interval is not None and not isinstance(interval, int):
            return "settings.default_poll_interval_minutes must be int"
    return None


def _sync_config_to_db(ctx: FeedsContext) -> dict:
    """Push categories + feeds from feeds.toml into the feeds DB.

    Mirrors :func:`istota.feeds.cli._sync_config_to_db`. Kept duplicate-free
    by import — we *could* import the CLI helper, but the CLI module pulls
    in Click and is a heavier import for the web app to take. The body is
    small and identical in behaviour.
    """
    cfg = read_feeds_config(ctx.config_path)
    feeds_db.init_db(ctx.db_path)

    cats_added = 0
    feeds_added = 0
    feeds_updated = 0
    with feeds_db.connect(ctx.db_path) as conn:
        slug_to_id: dict[str, int] = {}
        for c in cfg.get("categories") or []:
            slug = str(c.get("slug") or "").strip()
            title = str(c.get("title") or slug or "").strip()
            if not slug:
                continue
            existing = feeds_db.get_category_by_slug(conn, slug)
            cat_id = feeds_db.upsert_category(conn, slug, title)
            slug_to_id[slug] = cat_id
            if existing is None:
                cats_added += 1

        explicit_default = cfg.get("settings", {}).get("default_poll_interval_minutes")
        explicit_default = int(explicit_default) if explicit_default else None

        for f in cfg.get("feeds") or []:
            url = str(f.get("url") or "").strip()
            if not url:
                continue
            cat_slug = f.get("category")
            cat_id = slug_to_id.get(cat_slug) if cat_slug else None
            if cat_slug and cat_id is None:
                cat_id = feeds_db.upsert_category(conn, cat_slug, cat_slug)
                slug_to_id[cat_slug] = cat_id
                cats_added += 1
            source_type = detect_source_type(url)
            per_feed = f.get("poll_interval_minutes")
            if per_feed:
                interval = int(per_feed)
            elif explicit_default is not None:
                interval = explicit_default
            else:
                interval = default_poll_interval_for(source_type)
            existing = feeds_db.get_feed_by_url(conn, url)
            feeds_db.upsert_feed(
                conn,
                url=url,
                title=f.get("title"),
                site_url=f.get("site_url"),
                source_type=source_type,
                category_id=cat_id,
                poll_interval_minutes=interval,
            )
            if existing is None:
                feeds_added += 1
            else:
                feeds_updated += 1
        conn.commit()

    return {
        "categories_added": cats_added,
        "feeds_added": feeds_added,
        "feeds_updated": feeds_updated,
    }


def _dump_db_to_config(ctx: FeedsContext) -> None:
    """Project the DB back to feeds.toml after an OPML import."""
    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        cats = feeds_db.list_categories(conn)
        feeds = feeds_db.list_feeds(conn)
    cat_by_id = {c.id: c for c in cats}
    data: dict = {
        "settings": {"default_poll_interval_minutes": DEFAULT_POLL_INTERVAL_MINUTES},
        "categories": [{"slug": c.slug, "title": c.title} for c in cats],
        "feeds": [],
    }
    for f in feeds:
        entry: dict = {"url": f.url}
        if f.title:
            entry["title"] = f.title
        if f.category_id and f.category_id in cat_by_id:
            entry["category"] = cat_by_id[f.category_id].slug
        if f.poll_interval_minutes != default_poll_interval_for(f.source_type):
            entry["poll_interval_minutes"] = f.poll_interval_minutes
        data["feeds"].append(entry)
    write_feeds_config(ctx.config_path, data)
