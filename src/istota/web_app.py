"""Authenticated web interface for istota.

Run as: uvicorn istota.web_app:app --host 127.0.0.1 --port 8766

Provides an OIDC-authenticated web UI using Nextcloud as the identity provider.
Currently serves: dashboard, feed pages.
"""

import logging
import os
import signal
from contextlib import asynccontextmanager
from html import escape

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from .config import load_config

logger = logging.getLogger("istota.web_app")

# Module-level state
_config = None
_oauth = None


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

router = APIRouter(prefix="/istota")


def _require_auth(request: Request):
    """Dependency that checks for a valid session. Raises redirect if not logged in."""
    user = request.session.get("user")
    if not user:
        return None
    return user


def _auth_or_redirect(request: Request):
    """Dependency: returns user dict or redirects to login."""
    user = _require_auth(request)
    if user is None:
        raise _login_redirect()
    return user


class _LoginRedirectException(Exception):
    pass


def _login_redirect():
    return _LoginRedirectException()


@app.exception_handler(_LoginRedirectException)
async def _handle_login_redirect(request: Request, exc: _LoginRedirectException):
    return RedirectResponse(url="/istota/login", status_code=302)


@router.get("/login")
async def login(request: Request):
    if _oauth is None or not hasattr(_oauth, "nextcloud"):
        return Response("OIDC not configured", status_code=500)
    # If no "go" param, show a landing page instead of auto-redirecting.
    # This prevents logout from immediately re-authenticating via OIDC
    # (Nextcloud session is still active, so the flow auto-completes).
    if not request.query_params.get("go"):
        bot_name = escape(_config.bot_name) if _config else "Istota"
        return HTMLResponse(f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>{bot_name}</title></head>
<body>
<h1>{bot_name}</h1>
<p><a href="/istota/login?go=1">Log in with Nextcloud</a></p>
</body>
</html>""")
    hostname = _config.site.hostname if _config and _config.site.hostname else request.headers.get("host", "localhost")
    scheme = request.headers.get("x-forwarded-proto", "https")
    redirect_uri = f"{scheme}://{hostname}/istota/callback"
    return await _oauth.nextcloud.authorize_redirect(request, redirect_uri)


@router.get("/callback")
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


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/istota/login", status_code=302)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: dict = Depends(_auth_or_redirect)):
    username = user["username"]
    display_name = escape(user.get("display_name", username))

    links = []
    # Check if user has miniflux resource → show feeds link
    if _config:
        uc = _config.get_user(username)
        if uc:
            has_feeds = any(r.type == "miniflux" for r in uc.resources)
            if has_feeds:
                links.append('<li><a href="/istota/feeds">Feeds</a></li>')

    links_html = "\n".join(links) if links else "<li>No features available</li>"

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Istota</title></head>
<body>
<h1>Istota</h1>
<p>Logged in as {display_name} ({escape(username)})</p>
<ul>
{links_html}
</ul>
<p><a href="/istota/logout">Log out</a></p>
</body>
</html>"""
    return HTMLResponse(html)


@router.get("/feeds", response_class=HTMLResponse)
async def feeds(request: Request, user: dict = Depends(_auth_or_redirect)):
    username = user["username"]
    if not _config or not _config.nextcloud_mount_path:
        return Response("Mount not configured", status_code=500)

    feeds_path = (
        _config.nextcloud_mount_path
        / "Users"
        / username
        / _config.bot_dir_name
        / "html"
        / "feeds"
        / "index.html"
    )
    if not feeds_path.is_file():
        return Response("Feed page not found", status_code=404)

    return HTMLResponse(feeds_path.read_text())


app.include_router(router)
