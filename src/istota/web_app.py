"""Authenticated web interface for istota.

Run as: uvicorn istota.web_app:app --host 127.0.0.1 --port 8766

Provides an OIDC-authenticated web UI using Nextcloud as the identity provider.
SvelteKit frontend served as static files, Python handles auth and API.
"""

import asyncio
import logging
import os
import re
import signal
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import httpx
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import load_config

logger = logging.getLogger("istota.web_app")

# Module-level state
_config = None
_oauth = None

# Resolve static build directory (relative to this file or repo root)
_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "build"


def _reload_config():
    """Load config and register the Nextcloud OIDC client."""
    global _config, _oauth
    _config = load_config()
    _oauth = OAuth()
    if _config.web.oidc_issuer and _config.web.oidc_client_id:
        issuer = _config.web.oidc_issuer.rstrip("/")
        _oauth.register(
            name="nextcloud",
            client_id=_config.web.oidc_client_id,
            client_secret=_config.web.oidc_client_secret,
            server_metadata_url=f"{issuer}/index.php/.well-known/openid-configuration",
            client_kwargs={"scope": "openid profile"},
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _reload_config()
    signal.signal(signal.SIGHUP, lambda *_: _reload_config())
    yield


# Session secret must be available at import time for SessionMiddleware.
# Prefer env var (systemd EnvironmentFile=), fall back to a placeholder
# that will be replaced once lifespan loads the config.
_session_secret = os.environ.get("ISTOTA_WEB_SECRET_KEY", "change-me-insecure-default")

app = FastAPI(title="Istota Web", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    same_site="lax",
    https_only=True,
    max_age=7 * 24 * 60 * 60,  # 7 days
    session_cookie="istota_session",
    path="/istota/",
)


# ============================================================================
# Auth helpers
# ============================================================================

def _get_session_user(request: Request) -> dict | None:
    """Get user from session, or None."""
    return request.session.get("user")


def _require_api_auth(request: Request) -> dict:
    """Dependency for API routes: returns user or 401."""
    user = _get_session_user(request)
    if not user:
        raise _UnauthorizedException()
    return user


class _UnauthorizedException(Exception):
    pass


class _LoginRedirectException(Exception):
    pass


@app.exception_handler(_UnauthorizedException)
async def _handle_unauthorized(request: Request, exc: _UnauthorizedException):
    return JSONResponse({"error": "unauthorized"}, status_code=401)


@app.exception_handler(_LoginRedirectException)
async def _handle_login_redirect(request: Request, exc: _LoginRedirectException):
    return RedirectResponse(url="/istota/login", status_code=302)


# ============================================================================
# Auth routes (server-rendered, not SvelteKit)
# ============================================================================

auth_router = APIRouter(prefix="/istota")


@auth_router.get("/login")
async def login(request: Request):
    if _oauth is None or not hasattr(_oauth, "nextcloud"):
        return Response("OIDC not configured", status_code=500)
    if not request.query_params.get("go"):
        bot_name = escape(_config.bot_name) if _config else "Istota"
        return HTMLResponse(
            f'<!doctype html><html><head><meta charset="utf-8">'
            f'<title>{bot_name}</title>'
            f'<style>body{{background:#111;color:#e0e0e0;font-family:system-ui,-apple-system,sans-serif;'
            f'display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}'
            f'.box{{text-align:center}}h1{{font-size:1.2rem;margin:0 0 1rem}}'
            f'a{{color:#e0e0e0;padding:0.5rem 1.25rem;border:1px solid #333;border-radius:999px;'
            f'text-decoration:none;font-size:0.85rem;transition:all 0.15s}}'
            f'a:hover{{background:#e0e0e0;color:#111;border-color:#e0e0e0}}</style></head>'
            f'<body><div class="box"><h1>{bot_name}</h1>'
            f'<a href="/istota/login?go=1">Log in with Nextcloud</a></div></body></html>'
        )
    hostname = _config.site.hostname if _config and _config.site.hostname else request.headers.get("host", "localhost")
    scheme = request.headers.get("x-forwarded-proto", "https")
    redirect_uri = f"{scheme}://{hostname}/istota/callback"
    return await _oauth.nextcloud.authorize_redirect(request, redirect_uri)


@auth_router.get("/callback")
async def callback(request: Request):
    if _oauth is None or not hasattr(_oauth, "nextcloud"):
        return Response("OIDC not configured", status_code=500)
    token = await _oauth.nextcloud.authorize_access_token(request)
    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await _oauth.nextcloud.userinfo(request=request, token=token)
    username = userinfo.get("preferred_username", "")
    if not username or (_config and _config.users and username not in _config.users):
        return Response("Access denied: user not configured", status_code=403)
    request.session["user"] = {
        "username": username,
        "display_name": userinfo.get("name", username),
    }
    return RedirectResponse(url="/istota/", status_code=302)


@auth_router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/istota/login", status_code=302)


# ============================================================================
# API routes
# ============================================================================

api_router = APIRouter(prefix="/istota/api")


def _get_miniflux_creds(username: str) -> tuple[str, str] | None:
    """Get Miniflux base_url and api_key for a user, or None."""
    if not _config:
        return None
    uc = _config.get_user(username)
    if not uc:
        return None
    for r in uc.resources:
        if r.type == "miniflux" and r.base_url and r.api_key:
            return r.base_url, r.api_key
    return None


def _get_location_config(username: str) -> tuple[str, str, str] | None:
    """Get (db_path, user_id, timezone) for location queries, or None."""
    if not _config or not _config.location.enabled:
        return None
    uc = _config.get_user(username)
    if not uc:
        return None
    return str(_config.db_path), username, uc.timezone


@api_router.get("/me")
async def api_me(user: dict = Depends(_require_api_auth)):
    username = user["username"]
    features = {"feeds": False, "location": False}
    if _config:
        creds = _get_miniflux_creds(username)
        features["feeds"] = creds is not None
        features["location"] = _config.location.enabled
    return {
        "username": username,
        "display_name": user.get("display_name", username),
        "features": features,
    }


# Tags allowed in feed card excerpts
_ALLOWED_TAGS = {"a", "b", "strong", "i", "em", "br", "p", "ul", "ol", "li", "blockquote", "code", "pre", "img"}


def _sanitize_html(content: str, max_len: int = 600) -> str:
    """Sanitize HTML to allowed tags only, truncate by text length."""
    if not content:
        return ""
    import html as html_mod
    content = html_mod.unescape(content)
    result = []
    text_len = 0
    i = 0
    while i < len(content):
        if max_len and text_len >= max_len:
            break
        if content[i] == "<":
            end = content.find(">", i)
            if end == -1:
                break
            tag_str = content[i:end + 1]
            tag_match = re.match(r"</?(\w+)", tag_str)
            if tag_match and tag_match.group(1).lower() in _ALLOWED_TAGS:
                if tag_match.group(1).lower() == "img":
                    src_match = re.search(r'src="([^"]*)"', tag_str)
                    if src_match:
                        tag_str = f'<img src="{escape(html_mod.unescape(src_match.group(1)))}" loading="lazy">'
                    else:
                        tag_str = ""
                elif tag_match.group(1).lower() == "a" and not tag_str.startswith("</"):
                    href_match = re.search(r'href="([^"]*)"', tag_str)
                    if href_match:
                        tag_str = f'<a href="{escape(html_mod.unescape(href_match.group(1)))}">'
                    else:
                        tag_str = "<a>"
                result.append(tag_str)
            i = end + 1
        else:
            result.append(escape(content[i]))
            text_len += 1
            i += 1
    return "".join(result).strip()


def _extract_images(entry: dict) -> list[str]:
    """Extract image URLs from enclosures and content."""
    images = []
    for enc in entry.get("enclosures") or []:
        mime = enc.get("mime_type", "")
        url = enc.get("url", "")
        if mime.startswith("image/") and url:
            images.append(url)
    # Also extract from content if no enclosure images
    if not images:
        content = entry.get("content", "")
        for m in re.finditer(r'<img[^>]+src="([^"]+)"', content):
            images.append(m.group(1))
    return images


def _strip_image_from_content(content: str, images: list[str]) -> str:
    """Remove <img> tags from content that match the card images."""
    for img_url in images:
        content = re.sub(
            r'<p>\s*<img[^>]+src="' + re.escape(img_url) + r'"[^>]*>\s*</p>',
            '', content,
        )
        content = re.sub(
            r'<img[^>]+src="' + re.escape(img_url) + r'"[^>]*>',
            '', content,
        )
    return content.strip()


def _map_entry(entry: dict) -> dict:
    """Map a Miniflux entry to our API response format."""
    images = _extract_images(entry)
    content = entry.get("content", "")
    if images:
        content = _strip_image_from_content(content, images)
    content = _sanitize_html(content)
    return {
        "id": entry["id"],
        "title": entry.get("title", ""),
        "url": entry.get("url", ""),
        "content": content,
        "images": images,
        "feed": {
            "id": entry.get("feed", {}).get("id", 0),
            "title": entry.get("feed", {}).get("title", ""),
            "site_url": entry.get("feed", {}).get("site_url", ""),
        },
        "status": entry.get("status", ""),
        "published_at": entry.get("published_at", ""),
        "created_at": entry.get("created_at", ""),
    }


@api_router.get("/feeds")
async def api_feeds(
    user: dict = Depends(_require_api_auth),
    limit: int = Query(default=500, le=1000),
    offset: int = Query(default=0, ge=0),
    order: str = Query(default="published_at"),
    direction: str = Query(default="desc"),
    status: str = Query(default=""),
    category_id: int = Query(default=0),
):
    username = user["username"]
    creds = _get_miniflux_creds(username)
    if not creds:
        return JSONResponse({"error": "no miniflux resource configured"}, status_code=404)
    base_url, api_key = creds

    params: dict = {"limit": limit, "offset": offset, "order": order, "direction": direction}
    if status:
        params["status"] = status
    if category_id:
        params["category_id"] = category_id

    try:
        async with httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Auth-Token": api_key},
            timeout=30.0,
        ) as client:
            entries_resp = await client.get("/v1/entries", params=params)
            entries_resp.raise_for_status()
            feeds_resp = await client.get("/v1/feeds")
            feeds_resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.error("Miniflux API error for %s: %s", username, e)
        return JSONResponse({"error": "miniflux api error"}, status_code=502)

    entries_data = entries_resp.json()
    feeds_data = feeds_resp.json()

    entries = [_map_entry(e) for e in entries_data.get("entries", [])]
    feeds = [
        {"id": f["id"], "title": f.get("title", ""), "site_url": f.get("site_url", "")}
        for f in feeds_data
    ]

    return {
        "feeds": feeds,
        "entries": entries,
        "total": entries_data.get("total", len(entries)),
    }


@api_router.put("/feeds/entries/batch")
async def api_update_entries_batch(
    request: Request,
    user: dict = Depends(_require_api_auth),
):
    username = user["username"]
    creds = _get_miniflux_creds(username)
    if not creds:
        return JSONResponse({"error": "no miniflux resource configured"}, status_code=404)
    base_url, api_key = creds

    body = await request.json()
    entry_ids = body.get("entry_ids", [])
    if not entry_ids or not isinstance(entry_ids, list):
        return JSONResponse({"error": "entry_ids must be a non-empty list"}, status_code=400)

    try:
        async with httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Auth-Token": api_key},
            timeout=30.0,
        ) as client:
            resp = await client.put("/v1/entries", json={
                "entry_ids": entry_ids,
                "status": body.get("status", "read"),
            })
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.error("Miniflux API error batch-updating entries: %s", e)
        return JSONResponse({"error": "miniflux api error"}, status_code=502)

    return {"status": "ok"}


@api_router.put("/feeds/entries/{entry_id}")
async def api_update_entry(
    entry_id: int,
    request: Request,
    user: dict = Depends(_require_api_auth),
):
    username = user["username"]
    creds = _get_miniflux_creds(username)
    if not creds:
        return JSONResponse({"error": "no miniflux resource configured"}, status_code=404)
    base_url, api_key = creds

    body = await request.json()

    try:
        async with httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Auth-Token": api_key},
            timeout=30.0,
        ) as client:
            resp = await client.put("/v1/entries", json={
                "entry_ids": [entry_id],
                "status": body.get("status", "read"),
            })
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.error("Miniflux API error updating entry %d: %s", entry_id, e)
        return JSONResponse({"error": "miniflux api error"}, status_code=502)

    return {"status": "ok"}


# ============================================================================
# Location API
# ============================================================================


def _location_query_current(db_path: str, user_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT lp.timestamp, lp.lat, lp.lon, lp.accuracy,
                   lp.activity_type, lp.battery, lp.wifi,
                   p.name as place_name
            FROM location_pings lp
            LEFT JOIN places p ON lp.place_id = p.id
            WHERE lp.user_id = ?
            ORDER BY lp.timestamp DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if not row:
            return {"last_ping": None, "current_visit": None}

        last_ping = {
            "timestamp": row["timestamp"],
            "lat": row["lat"],
            "lon": row["lon"],
            "accuracy": row["accuracy"],
            "activity_type": row["activity_type"],
            "battery": row["battery"],
            "place": row["place_name"],
        }

        visit_row = conn.execute(
            """
            SELECT place_name, entered_at, ping_count
            FROM visits
            WHERE user_id = ? AND exited_at IS NULL
            ORDER BY entered_at DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        current_visit = None
        if visit_row:
            entered = visit_row["entered_at"]
            try:
                entered_dt = datetime.fromisoformat(entered)
                now = datetime.now(timezone.utc)
                if entered_dt.tzinfo is None:
                    entered_dt = entered_dt.replace(tzinfo=timezone.utc)
                duration_min = int((now - entered_dt).total_seconds() / 60)
            except (ValueError, TypeError):
                duration_min = None
            current_visit = {
                "place_name": visit_row["place_name"],
                "entered_at": entered,
                "duration_minutes": duration_min,
                "ping_count": visit_row["ping_count"],
            }

        return {"last_ping": last_ping, "current_visit": current_visit}
    finally:
        conn.close()


def _location_query_pings(
    db_path: str, user_id: str, tz_name: str,
    date: str | None, start: str | None, end: str | None, limit: int,
) -> dict:
    from zoneinfo import ZoneInfo

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("America/Los_Angeles")

        if date:
            day_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=tz)
            day_end = day_start + timedelta(days=1)
            since = day_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            until = day_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif start and end:
            s = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=tz)
            e = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=1)
            since = s.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            until = e.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            since = None
            until = None

        if since and until:
            query = """
                SELECT lp.timestamp, lp.lat, lp.lon, lp.accuracy,
                       lp.activity_type, lp.speed, lp.battery,
                       p.name as place_name
                FROM location_pings lp
                LEFT JOIN places p ON lp.place_id = p.id
                WHERE lp.user_id = ? AND lp.timestamp >= ? AND lp.timestamp < ?
                ORDER BY lp.timestamp ASC
            """
            params: list = [user_id, since, until]
            if limit:
                query += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(query, params).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT lp.timestamp, lp.lat, lp.lon, lp.accuracy,
                       lp.activity_type, lp.speed, lp.battery,
                       p.name as place_name
                FROM location_pings lp
                LEFT JOIN places p ON lp.place_id = p.id
                WHERE lp.user_id = ?
                ORDER BY lp.timestamp DESC LIMIT ?
                """,
                (user_id, limit or 100),
            ).fetchall()

        pings = [
            {
                "timestamp": r["timestamp"],
                "lat": r["lat"],
                "lon": r["lon"],
                "accuracy": r["accuracy"],
                "place": r["place_name"],
                "speed": r["speed"],
                "battery": r["battery"],
            }
            for r in rows
        ]
        return {"pings": pings, "count": len(pings)}
    finally:
        conn.close()


def _location_query_day_summary(db_path: str, user_id: str, tz_name: str, date: str | None) -> dict:
    from zoneinfo import ZoneInfo
    from .geo import cluster_pings, reverse_geocode, haversine

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")

    target_date = date or datetime.now(tz).strftime("%Y-%m-%d")

    day_start = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    since_utc = day_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    until_utc = day_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT lp.timestamp, lp.lat, lp.lon, lp.activity_type, lp.accuracy,
                   lp.place_id, p.name as place_name
            FROM location_pings lp
            LEFT JOIN places p ON lp.place_id = p.id
            WHERE lp.user_id = ? AND lp.timestamp >= ? AND lp.timestamp < ?
            ORDER BY lp.timestamp ASC
            """,
            (user_id, since_utc, until_utc),
        ).fetchall()

        if not rows:
            return {"date": target_date, "timezone": tz_name, "stops": [], "ping_count": 0, "transit_pings": 0}

        pings = [dict(r) for r in rows]
        clusters = cluster_pings(pings, radius_m=250)

        stops = []
        transit_pings = 0
        for c in clusters:
            if c["ping_count"] <= 2 and not c["place_name"]:
                transit_pings += c["ping_count"]
                continue
            stops.append(c)

        saved_places = conn.execute(
            "SELECT name, lat, lon, radius_meters FROM places WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        saved_places = [dict(r) for r in saved_places]

        for stop in stops:
            if stop["place_name"]:
                stop["location"] = stop["place_name"]
                stop["location_source"] = "saved_place"
            else:
                matched = False
                for sp in saved_places:
                    dist = haversine(stop["lat"], stop["lon"], sp["lat"], sp["lon"])
                    if dist <= max(sp["radius_meters"], 100):
                        stop["location"] = sp["name"]
                        stop["location_source"] = "saved_place_proximity"
                        matched = True
                        break
                if not matched:
                    geo = reverse_geocode(stop["lat"], stop["lon"], conn)
                    name = (
                        geo.get("suburb")
                        or geo.get("neighborhood")
                        or geo.get("road")
                        or geo.get("city")
                        or "unknown"
                    )
                    stop["location"] = name
                    stop["location_source"] = geo.get("source", "unknown")

            for key in ("first_ts", "last_ts"):
                try:
                    utc_dt = datetime.fromisoformat(stop[key]).replace(tzinfo=timezone.utc)
                    stop[key + "_local"] = utc_dt.astimezone(tz).strftime("%H:%M")
                except Exception:
                    stop[key + "_local"] = stop[key]

        # Merge consecutive stops at same location
        merged = []
        for stop in stops:
            if merged and merged[-1]["location"] == stop["location"]:
                prev = merged[-1]
                prev["last_ts"] = stop["last_ts"]
                prev["last_ts_local"] = stop.get("last_ts_local")
                prev["ping_count"] += stop["ping_count"]
            else:
                merged.append(stop)

        return {
            "date": target_date,
            "timezone": tz_name,
            "ping_count": len(pings),
            "transit_pings": transit_pings,
            "stops": [
                {
                    "location": s["location"],
                    "location_source": s.get("location_source"),
                    "arrived": s.get("first_ts_local"),
                    "departed": s.get("last_ts_local"),
                    "ping_count": s["ping_count"],
                    "lat": round(s["lat"], 5),
                    "lon": round(s["lon"], 5),
                }
                for s in merged
            ],
        }
    finally:
        conn.close()


def _location_query_places(db_path: str, user_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT name, lat, lon, radius_meters, category FROM places WHERE user_id = ? ORDER BY name",
            (user_id,),
        ).fetchall()
        return {
            "places": [
                {"name": r["name"], "lat": r["lat"], "lon": r["lon"],
                 "radius_meters": r["radius_meters"], "category": r["category"]}
                for r in rows
            ]
        }
    finally:
        conn.close()


@api_router.get("/location/current")
async def api_location_current(user: dict = Depends(_require_api_auth)):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, user_id, _ = loc
    return await asyncio.to_thread(_location_query_current, db_path, user_id)


@api_router.get("/location/pings")
async def api_location_pings(
    user: dict = Depends(_require_api_auth),
    date: str = Query(default=""),
    start: str = Query(default=""),
    end: str = Query(default=""),
    limit: int = Query(default=5000, le=50000),
):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, user_id, tz_name = loc
    return await asyncio.to_thread(
        _location_query_pings, db_path, user_id, tz_name,
        date or None, start or None, end or None, limit,
    )


@api_router.get("/location/day-summary")
async def api_location_day_summary(
    user: dict = Depends(_require_api_auth),
    date: str = Query(default=""),
):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, user_id, tz_name = loc
    return await asyncio.to_thread(
        _location_query_day_summary, db_path, user_id, tz_name, date or None,
    )


@api_router.get("/location/places")
async def api_location_places(user: dict = Depends(_require_api_auth)):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, user_id, _ = loc
    return await asyncio.to_thread(_location_query_places, db_path, user_id)


# ============================================================================
# App assembly — order matters: API > auth > static
# ============================================================================

app.include_router(api_router)
app.include_router(auth_router)

# Serve SvelteKit build as static files (catch-all for SPA routing)
if _STATIC_DIR.is_dir():
    app.mount("/istota", StaticFiles(directory=str(_STATIC_DIR), html=True), name="web-static")
