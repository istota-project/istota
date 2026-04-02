"""Authenticated web interface for istota.

Run as: uvicorn istota.web_app:app --host 127.0.0.1 --port 8766

Provides an OIDC-authenticated web UI using Nextcloud as the identity provider.
SvelteKit frontend served as static files, Python handles auth and API.
"""

import logging
import os
import re
import signal
from contextlib import asynccontextmanager
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


@api_router.get("/me")
async def api_me(user: dict = Depends(_require_api_auth)):
    username = user["username"]
    features = {"feeds": False}
    if _config:
        creds = _get_miniflux_creds(username)
        features["feeds"] = creds is not None
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
        },
        "status": entry.get("status", ""),
        "published_at": entry.get("published_at", ""),
        "created_at": entry.get("created_at", ""),
    }


@api_router.get("/feeds")
async def api_feeds(
    user: dict = Depends(_require_api_auth),
    limit: int = Query(default=500, le=1000),
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

    params: dict = {"limit": limit, "order": order, "direction": direction}
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
# App assembly — order matters: API > auth > static
# ============================================================================

app.include_router(api_router)
app.include_router(auth_router)

# Serve SvelteKit build as static files (catch-all for SPA routing)
if _STATIC_DIR.is_dir():
    app.mount("/istota", StaticFiles(directory=str(_STATIC_DIR), html=True), name="web-static")
