"""Authenticated web interface for istota.

Run as: uvicorn istota.web_app:app --host 127.0.0.1 --port 8766

Provides an OIDC-authenticated web UI using Nextcloud as the identity provider.
SvelteKit frontend served as static files, Python handles auth and API.
"""

import asyncio
import importlib
import json
import logging
import os
import platform
import re
import secrets
import shutil
import signal
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import httpx
from authlib.integrations.base_client.errors import MismatchingStateError, OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import db as _db
from .brain import make_brain
from .config import load_config
from .location_logic import (
    _location_discover_places,
    _location_dismiss_cluster,
    _location_list_dismissed,
    _location_place_stats,
    _location_restore_dismissed,
)

logger = logging.getLogger("istota.web_app")

# Module-level state
_config = None
_oauth = None
_WEB_START_TIME = time.time()

# Sentinel for "key absent from the PATCH body" — distinguishes "clear this
# field" (explicit null) from "leave it untouched" in _chat_update_room.
_UNSET = object()

# Resolve static build directory. Precedence:
#   1. ISTOTA_WEB_STATIC_DIR env override (Docker runtime / explicit).
#   2. Repo-relative web/build (editable installs from the repo root).
#   3. Packaged static tree at istota/web_static (non-editable wheel installs —
#      the release build copies web/build there; see pyproject packaging).
def _pick_static_dir(env_dir: str, repo_build: Path, packaged: Path) -> Path:
    """Pick the static dir from candidates (pure — unit-testable).

    Precedence: env override > repo-relative build > packaged. Falls back to
    the repo-relative path when neither build exists, preserving the existing
    "missing build" behaviour (StaticFiles mount is guarded on ``.is_dir()``).
    """
    if env_dir.strip():
        return Path(env_dir.strip())
    if repo_build.is_dir():
        return repo_build
    if packaged.is_dir():
        return packaged
    return repo_build


def _resolve_static_dir() -> Path:
    here = Path(__file__).resolve()
    return _pick_static_dir(
        os.environ.get("ISTOTA_WEB_STATIC_DIR", ""),
        here.parent.parent.parent / "web" / "build",
        here.parent / "web_static",
    )


# SvelteKit emits two classes of asset and they need opposite caching. Bare
# `StaticFiles` sends neither header, leaving both to heuristic freshness — and
# an iOS home-screen PWA takes that as licence to pin the app shell more or less
# forever. That is how a device ends up running a months-old bundle against a
# current API: the shell it cached still names hashed chunks the server has
# since deleted, so nothing 404s loudly, it just never picks up new code.
def _static_cache_control(path: str) -> str:
    """The `Cache-Control` for a built asset, by SvelteKit's own two classes."""
    # `_app/immutable/*` is content-addressed — the hash changes when the bytes
    # do, so it can be cached indefinitely and never revalidated.
    if "/_app/immutable/" in f"/{path.lstrip('/')}":
        return "public, max-age=31536000, immutable"
    # Everything else is a stable name with changing content: the HTML shell
    # (which names the hashed chunks), version.json, the manifest, icons.
    # `no-cache` still allows caching — it requires revalidation, which the
    # ETag answers with a 304 in the common case.
    return "no-cache"


class _CacheHeaderStatics(StaticFiles):
    """`StaticFiles` that stamps the cache policy above onto every response."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = _static_cache_control(scope.get("path", ""))
        return response


_STATIC_DIR = _resolve_static_dir()


def _reload_config():
    """Load config and register OAuth clients.

    Web auth uses NC's built-in OAuth2 provider (auth-only). Google is
    a separate, unrelated OAuth client used only by the google_workspace skill.
    """
    global _config, _oauth
    _config = load_config()
    _oauth = OAuth()
    if _config.web.oauth2_client_id:
        # NC built-in OAuth2 — no metadata discovery, register endpoints directly.
        provider = _config.web.oauth2_provider.rstrip("/")
        _oauth.register(
            name="nextcloud",
            client_id=_config.web.oauth2_client_id,
            client_secret=_config.web.oauth2_client_secret,
            authorize_url=f"{provider}/index.php/apps/oauth2/authorize",
            access_token_url=(
                _config.web.oauth2_token_endpoint
                or f"{provider}/index.php/apps/oauth2/api/v1/token"
            ),
            client_kwargs={"scope": ""},  # NC built-in OAuth2 ignores scope
        )
    if _config.google_workspace.enabled and _config.google_workspace.client_id:
        _oauth.register(
            name="google",
            client_id=_config.google_workspace.client_id,
            client_secret=_config.google_workspace.client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": " ".join(_config.google_workspace.scopes)},
            authorize_params={"access_type": "offline", "prompt": "consent"},
        )
    # token_storage = "encrypted" without the web-only key is a deploy
    # misconfiguration: fail loud once, then run as ephemeral (the
    # web_tokens.feature_enabled gate is False everywhere downstream).
    if _config.web.token_storage == "encrypted":
        from . import web_tokens as _wt  # noqa: PLC0415
        if not _wt.token_key_available():
            logger.error(
                "[web] token_storage = \"encrypted\" is configured but "
                "ISTOTA_WEB_TOKEN_KEY is missing or too short — running as "
                "\"ephemeral\" (no post-as-user mirroring, no read sync). "
                "Provision the key for the web unit only.",
            )


def _publish_config(app: FastAPI) -> None:
    """Expose the loaded istota config to mounted routers via app.state."""
    app.state.istota_config = _config


def _reload_config_on_signal(app: FastAPI) -> None:
    """SIGHUP reload that keeps the running config when the new one won't load.

    `load_config` raises on a malformed config — `[email]
    outbound_approval_floor` is a deliberate hard failure, since no fallback
    value for a security floor is safe to pick. At startup that is what we want:
    the lifespan fails loudly and the process doesn't serve. On SIGHUP it is
    not. The exception would propagate out of the signal handler into whatever
    main-thread bytecode uvicorn happened to be executing, so an operator
    typo'ing a value and reloading would take down a running web process rather
    than being told the reload failed. Keep serving the config we have and say
    so — the same shape `webhook_receiver._maybe_reload_for_signal` already uses.
    """
    try:
        _reload_config()
        _publish_config(app)
    except Exception as e:
        logger.error(
            "SIGHUP config reload failed, keeping the previously loaded "
            "config: %s", e,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _reload_config()
    _publish_config(app)
    signal.signal(signal.SIGHUP, lambda *_: _reload_config_on_signal(app))
    yield


# Starlette's SessionMiddleware keeps the whole session in a *signed* cookie
# (no server-side store), so the signing key is the only thing between a forged
# cookie and an authenticated session — a shared/guessable key is a full auth
# bypass (ISSUE-124). It must be resolved before the middleware is constructed
# (import time). Resolution order:
#   1. ISTOTA_WEB_SESSION_SECRET_KEY env var (Ansible EnvironmentFile path).
#   2. config.web.session_secret_key — the Docker entrypoint generates this on
#      first boot and persists it into config.toml on the data volume (and
#      load_config folds the env var from (1) in too), so it's the single merged
#      source of truth across both deploy paths.
#   3. No real secret found → fail closed. There is deliberately no constant
#      fallback. For local dev/test, ISTOTA_WEB_ALLOW_INSECURE_SESSION=1 opts
#      into a random per-process key (sessions don't survive a restart).
_ALLOW_INSECURE_SESSION_ENV = "ISTOTA_WEB_ALLOW_INSECURE_SESSION"


def _resolve_session_secret() -> str:
    env_secret = os.environ.get("ISTOTA_WEB_SESSION_SECRET_KEY", "").strip()
    if env_secret:
        return env_secret

    # config.toml (Docker-persisted) secret. Best-effort: a missing or
    # unreadable config must not crash import — it just means none was found.
    try:
        _cfg = load_config()
        config_secret = (_cfg.web.session_secret_key or "").strip()
    except Exception:  # pragma: no cover - defensive
        _cfg = None
        config_secret = ""
    if config_secret:
        return config_secret

    # No-auth (standalone local) mode never reads the session — the middleware
    # is still constructed, so it needs *a* key, but a random per-process one is
    # fine (there is nothing to forge without an auth flow). Do not crash import.
    if _cfg is not None and getattr(_cfg.web, "auth", "nextcloud") == "none":
        return secrets.token_hex(32)

    if os.environ.get(_ALLOW_INSECURE_SESSION_ENV, "").strip().lower() in ("1", "true", "yes"):
        logger.warning(
            "No web session secret configured; signing with a random per-process "
            "key because %s is set. Sessions will not survive a restart. Do not "
            "use this in production.",
            _ALLOW_INSECURE_SESSION_ENV,
        )
        return secrets.token_hex(32)

    raise RuntimeError(
        "No web session signing secret configured. Set "
        "ISTOTA_WEB_SESSION_SECRET_KEY (or web.session_secret_key in config.toml) "
        "to a long random value. Refusing to start with an insecure default — a "
        "shared signing key allows forged session cookies and auth bypass "
        f"(ISSUE-124). For local development set {_ALLOW_INSECURE_SESSION_ENV}=1."
    )


_session_secret = _resolve_session_secret()

# `https_only` defaults to True so production cookies carry `Secure`. Browsers
# refuse Secure cookies on plaintext origins, which kills the whole auth flow
# on local dev (Docker default = http://localhost:8766). Operators flip
# `ISTOTA_WEB_INSECURE_COOKIES=1` for those setups.
_https_only = os.environ.get("ISTOTA_WEB_INSECURE_COOKIES", "").strip() not in ("1", "true", "yes")

app = FastAPI(title="Istota Web", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    same_site="lax",
    https_only=_https_only,
    max_age=7 * 24 * 60 * 60,  # 7 days
    session_cookie="istota_session",
    path="/istota/",
)


@app.get("/", include_in_schema=False)
async def _root_redirect() -> RedirectResponse:
    """Send the bare root to the app.

    The whole UI lives under ``/istota`` (the base path is baked into the
    SvelteKit build and shared with the server deployment, where nginx routes
    ``/istota/`` to this service). In a standalone / direct-uvicorn run there is
    no nginx in front, so opening ``http://host:port/`` would otherwise 404 —
    redirect it so the printed bare-port URL just works.
    """
    return RedirectResponse(url="/istota/", status_code=307)


# ============================================================================
# Auth helpers
# ============================================================================

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0"})


def is_loopback_host(host: str) -> bool:
    """Whether a bind host is loopback-only (safe for no-auth).

    ``0.0.0.0`` is deliberately NOT loopback — it binds all interfaces. Only
    ``127.0.0.1`` / ``::1`` / ``localhost`` count. (Listed here for clarity;
    the guard below rejects everything not in the accept set.)
    """
    return host.strip().lower() in ("127.0.0.1", "::1", "localhost")


def assert_no_auth_bind_safe(auth: str, host: str) -> None:
    """Refuse to serve no-auth on a non-loopback bind.

    Raises ``RuntimeError`` when ``auth == "none"`` and ``host`` is not a
    loopback address — structurally prevents an unauthenticated instance from
    being exposed on the network. A no-op for ``auth == "nextcloud"``.
    """
    if auth != "none":
        return
    if not is_loopback_host(host):
        raise RuntimeError(
            f"[web] auth = \"none\" (no authentication) refuses to bind to a "
            f"non-loopback host {host!r}. no-auth mode is only safe on "
            f"127.0.0.1/::1/localhost. Either bind loopback-only or set "
            f"[web] auth = \"nextcloud\"."
        )


def _no_auth_mode() -> bool:
    """Whether the web app is running with authentication bypassed.

    Single-user local (standalone) shape: ``[web] auth = "none"``. Server
    deployments leave the default ``"nextcloud"`` and this is always False.
    """
    return bool(_config) and getattr(_config.web, "auth", "nextcloud") == "none"


def _local_user() -> dict:
    """The fixed local user dict for no-auth mode.

    Shape mirrors the session user (``{"username", "display_name"}``). The id
    is the single configured local user; the display name comes from its
    profile when present.
    """
    uid = _config.local_user_id if _config else "local"
    uc = _config.users.get(uid) if _config else None
    display = (uc.display_name if uc and uc.display_name else uid)
    return {"username": uid, "display_name": display}


def _get_session_user(request: Request) -> dict | None:
    """Get user from session, or None."""
    return request.session.get("user")


def _require_api_auth(request: Request) -> dict:
    """Dependency for API routes: returns user or 401.

    In no-auth mode (``[web] auth = "none"``) every request is the fixed local
    user — an early return before any session read, so it holds for every route
    without override wiring and survives a SIGHUP config reload.
    """
    if _no_auth_mode():
        return _local_user()
    user = _get_session_user(request)
    if not user:
        raise _UnauthorizedException()
    return user


def _user_is_web_admin(username: str) -> bool:
    """Web dashboard admin check — fails closed.

    Distinct from ``Config.is_admin``, which treats an empty ``admin_users``
    set as "all users are admin" for sandbox/skill/command back-compat. The
    web admin dashboard requires an explicit allowlist: a missing or empty
    ``/etc/istota/admins`` means no admin access via the web UI.

    Exception: in no-auth mode the single local user is always admin (the
    install is single-user and trusted by construction), so a local instance
    doesn't need an ``/etc/istota/admins`` entry.
    """
    if _no_auth_mode() and _config and username == _config.local_user_id:
        return True
    if not _config or not _config.admin_users:
        return False
    return username in _config.admin_users


def _require_admin(user: dict = Depends(_require_api_auth)) -> dict:
    """Dependency for admin API routes: returns user or 403."""
    if not _user_is_web_admin(user["username"]):
        raise _ForbiddenException("admin only")
    return user


def _verify_origin(request: Request) -> None:
    """Check Origin/Referer header against configured hostname for CSRF protection.

    No-op in no-auth mode: the loopback-only bind (enforced at startup) means
    there is no cross-site attacker, and requiring an Origin header would 403
    tools/curl calls with none.
    """
    if _no_auth_mode():
        return
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        raise _ForbiddenException("missing origin")
    if not _config or not _config.site.hostname:
        raise _ForbiddenException("site.hostname not configured")
    hostname = _config.site.hostname
    from urllib.parse import urlparse
    parsed = urlparse(origin)
    if parsed.hostname != hostname.split(":")[0]:
        raise _ForbiddenException("origin mismatch")


def _get_external_origin() -> tuple[str, str]:
    """Get the external hostname and scheme for OAuth redirect URIs.

    Requires site.hostname to be configured — does not fall back to
    request headers, which can be forged. Scheme is `http` when hostname is
    a literal localhost / loopback (Docker dev path); otherwise `https`.
    """
    if not _config or not _config.site.hostname:
        raise ValueError("site.hostname must be configured when web app is enabled")
    host = _config.site.hostname
    bare = host.split(":")[0]
    scheme = "http" if bare in ("localhost", "127.0.0.1", "::1") else "https"
    return host, scheme


class _ForbiddenException(Exception):
    pass


class _UnauthorizedException(Exception):
    pass


class _LoginRedirectException(Exception):
    pass


@app.exception_handler(_ForbiddenException)
async def _handle_forbidden(request: Request, exc: _ForbiddenException):
    return JSONResponse({"error": "forbidden"}, status_code=403)


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


def _nc_redirect_uri(request: Request) -> str:
    """Compute the OAuth redirect URI for the NC flow.

    Precedence: explicit ``web.oauth2_redirect_uri`` > derived from ``site.hostname``.
    Must match the URI registered with the NC OAuth2 client exactly.
    """
    if _config and _config.web.oauth2_redirect_uri:
        return _config.web.oauth2_redirect_uri
    hostname, scheme = _get_external_origin()
    return f"{scheme}://{hostname}/istota/callback"


async def _nc_oauth2_userinfo(token: dict) -> dict:
    """Fetch identity from NC's OCS endpoint with a bearer token, then drop the token.

    The endpoint returns `{ocs: {data: {id, displayname, email, ...}}}`.
    Token is not stored — it lives only in this function's stack frame.
    """
    access_token = token.get("access_token")
    if not access_token:
        raise ValueError("token response missing access_token")
    endpoint = (
        _config.web.oauth2_userinfo_endpoint
        or f"{_config.web.oauth2_provider.rstrip('/')}/ocs/v2.php/cloud/user?format=json"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            endpoint,
            headers={
                "Authorization": f"Bearer {access_token}",
                "OCS-APIRequest": "true",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        body = resp.json()
    inner = body.get("ocs", {}).get("data") or {}
    if not isinstance(inner, dict):
        raise ValueError("unexpected OCS userinfo shape")
    return inner


# Project homepage, linked from the login footer. Names the software, not the
# deployment — a rebranded `bot_name` still runs Istota.
ISTOTA_SITE_URL = "https://istota.cynium.com"

# Tokens mirror web/src/app.css. Dark is the default (as in the app); an explicit
# `data-theme="light"` from the stored preference flips it, so a user who picked
# light in the app doesn't get flashed a dark login page.
_LOGIN_PAGE_CSS = """
:root {
  color-scheme: dark;
  --surface-base: #111;
  --surface-card: #1a1a1a;
  --surface-raised: #222;
  --text-primary: #e0e0e0;
  --text-muted: #888;
  --text-dim: #666;
  --border-default: #333;
  --border-subtle: #222;
  --accent-amber: #f5a623;
  --accent-blue: #7aa3d8;
  --glow: rgba(245, 166, 35, 0.09);
}
:root[data-theme='light'] {
  color-scheme: light;
  --surface-base: #ffffff;
  --surface-card: #f4f4f5;
  --surface-raised: #e8e8ea;
  --text-primary: #1a1a1a;
  --text-muted: #6b6b70;
  --text-dim: #8a8a90;
  --border-default: #d4d4d8;
  --border-subtle: #e4e4e7;
  --accent-amber: #b9740a;
  --accent-blue: #2563b0;
  --glow: rgba(185, 116, 10, 0.07);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  padding: 2rem 1.25rem;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 1.5rem;
  background: var(--surface-base);
  background-image: radial-gradient(60rem 32rem at 50% -8rem, var(--glow), transparent 70%);
  color: var(--text-primary);
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
  -webkit-font-smoothing: antialiased;
}
.card {
  width: 100%;
  max-width: 22rem;
  padding: 2.25rem 1.75rem 1.75rem;
  background: var(--surface-card);
  border: 1px solid var(--border-subtle);
  border-radius: 1rem;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2), 0 12px 32px rgba(0, 0, 0, 0.25);
  text-align: center;
}
/* The sigil is a flat near-white silhouette on transparency, so it needs no
   badge chrome — and light theme inverts it to near-black rather than shipping
   a second asset. Mirrors .app-nav .app-name .sigil in app.css. */
.mark {
  height: 4.5rem;
  width: auto;
  margin: 0 auto 1.25rem;
  display: block;
}
:root[data-theme='light'] .mark { filter: invert(1); }
h1 {
  margin: 0;
  font-size: 1.35rem;
  font-weight: 600;
  letter-spacing: -0.01em;
}
.tagline {
  margin: 0.4rem 0 1.75rem;
  font-size: 0.85rem;
  color: var(--text-muted);
}
.btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.6rem;
  width: 100%;
  padding: 0.7rem 1rem;
  border-radius: 0.6rem;
  border: 1px solid var(--border-default);
  font-size: 0.9rem;
  font-weight: 500;
  text-decoration: none;
  color: var(--text-primary);
  background: var(--surface-raised);
  transition: border-color 0.15s, background 0.15s, transform 0.15s;
}
a.btn:hover {
  border-color: var(--accent-amber);
  transform: translateY(-1px);
}
a.btn:focus-visible {
  outline: 2px solid var(--accent-amber);
  outline-offset: 2px;
}
.btn svg { flex: none; }
.lucide-cloud { color: var(--accent-blue); }
.btn-disabled {
  margin-top: 0.75rem;
  color: var(--text-dim);
  background: transparent;
  border-style: dashed;
  cursor: not-allowed;
}
.soon {
  padding: 0.1rem 0.45rem;
  border-radius: 999px;
  background: var(--surface-raised);
  border: 1px solid var(--border-subtle);
  font-size: 0.65rem;
  font-weight: 500;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  color: var(--text-muted);
}
.divider {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin: 1rem 0 0.25rem;
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-dim);
}
.divider::before, .divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border-subtle);
}
footer {
  font-size: 0.72rem;
  color: var(--text-dim);
}
footer a {
  color: var(--text-muted);
  text-decoration: none;
  border-bottom: 1px solid var(--border-default);
}
footer a:hover { color: var(--accent-amber); border-bottom-color: var(--accent-amber); }
@media (prefers-reduced-motion: reduce) {
  .btn { transition: none; }
  a.btn:hover { transform: none; }
}
"""

# Reads the same localStorage key the SvelteKit theme store writes (a JSON
# string), so the login page matches the app the user is about to enter. Runs
# before first paint — keep it in <head>.
_LOGIN_PAGE_THEME_SCRIPT = (
    "try{if(JSON.parse(localStorage.getItem('theme'))==='light')"
    "document.documentElement.setAttribute('data-theme','light')}catch(e){}"
)

# Lucide icons, inlined. This page is server-rendered FastAPI HTML, so it can't
# import `lucide-svelte` the way the SvelteKit app does — the path data and the
# default attributes are copied verbatim from the package so the two surfaces
# draw the same glyphs.
def _lucide(name: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" '
        f'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
        f'class="lucide lucide-{name}">{body}</svg>'
    )


_CLOUD_ICON = _lucide("cloud", '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>')

_MAIL_ICON = _lucide(
    "mail",
    '<path d="m22 7-8.991 5.727a2 2 0 0 1-2.009 0L2 7"/>'
    '<rect x="2" y="4" width="20" height="16" rx="2"/>',
)


def _render_login_page(bot_name: str, version: str) -> str:
    """The unauthenticated landing page: one card, one working way in."""
    name = escape(bot_name)
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Sign in &middot; {name}</title>'
        f'<link rel="icon" href="/istota/favicon.png">'
        f'<script>{_LOGIN_PAGE_THEME_SCRIPT}</script>'
        f'<style>{_LOGIN_PAGE_CSS}</style></head><body>'
        f'<main class="card">'
        f'<img class="mark" src="/istota/octopus-sigil.webp" alt="" width="68" height="72">'
        f'<h1>{name}</h1>'
        f'<p class="tagline">Sign in to continue</p>'
        f'<a class="btn" href="/istota/login?go=1">{_CLOUD_ICON}'
        f'Log in with Nextcloud</a>'
        f'<div class="divider">or</div>'
        f'<div class="btn btn-disabled" aria-disabled="true">{_MAIL_ICON}'
        f'Log in with email <span class="soon">Coming soon</span></div>'
        f'</main>'
        f'<footer>Running <a href="{ISTOTA_SITE_URL}" target="_blank" '
        f'rel="noopener">Istota</a> v{escape(version)}</footer>'
        f'</body></html>'
    )


def _render_login_error_page(
    bot_name: str, version: str, headline: str, detail: str
) -> str:
    """A login failure the user can act on, in the same card as the login page.

    ``headline`` and ``detail`` are always caller-supplied fixed strings —
    never provider- or exception-derived text, which is attacker-influenceable
    and would be reflected straight into a browser response.
    """
    name = escape(bot_name)
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Sign-in failed &middot; {name}</title>'
        f'<link rel="icon" href="/istota/favicon.png">'
        f'<script>{_LOGIN_PAGE_THEME_SCRIPT}</script>'
        f'<style>{_LOGIN_PAGE_CSS}</style></head><body>'
        f'<main class="card">'
        f'<img class="mark" src="/istota/octopus-sigil.webp" alt="" width="68" height="72">'
        f'<h1>{name}</h1>'
        f'<p class="tagline">{escape(headline)}</p>'
        f'<p class="tagline">{escape(detail)}</p>'
        f'<a class="btn" href="/istota/login">Try signing in again</a>'
        f'</main>'
        f'<footer>Running <a href="{ISTOTA_SITE_URL}" target="_blank" '
        f'rel="noopener">Istota</a> v{escape(version)}</footer>'
        f'</body></html>'
    )


@auth_router.get("/login")
async def login(request: Request):
    if _oauth is None or not hasattr(_oauth, "nextcloud"):
        return Response("Auth not configured", status_code=500)
    if not request.query_params.get("go"):
        from . import __version__

        bot_name = _config.bot_name if _config else "Istota"
        return HTMLResponse(_render_login_page(bot_name, __version__))
    return await _oauth.nextcloud.authorize_redirect(request, _nc_redirect_uri(request))


@auth_router.get("/callback")
async def callback(request: Request):
    if _oauth is None or not hasattr(_oauth, "nextcloud"):
        return Response("Auth not configured", status_code=500)

    from . import __version__  # noqa: PLC0415

    _bot_name = _config.bot_name if _config else "Istota"

    def _login_error(status: int, headline: str, detail: str) -> HTMLResponse:
        return HTMLResponse(
            _render_login_error_page(_bot_name, __version__, headline, detail),
            status_code=status,
        )

    # A failure here is not a server fault and must not surface as a bare 500.
    # The common causes are all recoverable by starting again: the session
    # cookie carrying the OAuth ``state`` was cleared or never sent, a stale or
    # bookmarked callback URL was opened, or the authorize hop happened in a
    # different cookie jar than the callback (an embedded WebView handing off
    # to the system browser does exactly this).
    try:
        token = await _oauth.nextcloud.authorize_access_token(request)
    except MismatchingStateError:
        logger.warning(
            "OAuth2 callback state mismatch — session cookie missing or stale "
            "(client=%s)", request.client.host if request.client else "?",
        )
        return _login_error(
            400,
            "Sign-in could not be completed",
            "This sign-in link has expired, or your browser did not send the "
            "cookie that started it. Please sign in again.",
        )
    except OAuthError as e:
        # The provider declined — a cancelled consent, a revoked client.
        logger.warning("OAuth2 callback rejected by provider: %s", e)
        return _login_error(
            400,
            "Sign-in was declined",
            "The identity provider did not authorise this sign-in.",
        )
    except Exception:
        # Token-endpoint unreachable or misbehaving. Distinct from the above:
        # retrying may work, but nothing the user did caused it.
        logger.exception("OAuth2 token exchange failed")
        return _login_error(
            502,
            "Sign-in is temporarily unavailable",
            "Could not reach the identity provider. Please try again shortly.",
        )

    # NC's built-in OAuth2 returns the resource owner's username inline in
    # the token response (`user_id`), so we don't need a second HTTP round-trip.
    # The token is dropped after we extract user_id — the OCS userinfo path
    # is kept as a fallback for older NC versions or custom auth backends
    # that don't include `user_id`.
    username = token.get("user_id") or ""
    display_name = ""
    if not username:
        try:
            data = await _nc_oauth2_userinfo(token)
        except Exception as e:
            logger.warning("OAuth2 userinfo fetch failed: %s", e)
            return Response("identity verification failed", status_code=502)
        username = data.get("id") or data.get("user_id") or ""
        display_name = data.get("displayname") or data.get("display-name") or ""
    if not display_name:
        display_name = username

    if not username or (_config and _config.users and username not in _config.users):
        return Response("Access denied: user not configured", status_code=403)

    # Phase 6: auto-seed the user_profiles row on first login.
    # Idempotent — existing rows are not overwritten on subsequent logins.
    # The TOML UserConfig is passed as ``seed_from`` so the row carries the
    # full operator-supplied profile (emails, channels, …) the
    # moment it's created, even if the scheduler's startup migration hasn't
    # run yet (web service may boot first). The ``created`` signal gates
    # the NC display_name refresh to first-login only — any subsequent
    # web-UI edit to display_name is preserved across logins.
    if _config and _config.db_path and Path(_config.db_path).exists():
        try:
            from . import user_profiles as _up  # noqa: PLC0415

            uc = _config.get_user(username) if _config else None
            seeded, created = _up.ensure_profile_with_status(
                _config.db_path, username,
                display_name=display_name or username,
                seed_from=uc,
            )
            if (
                created
                and display_name
                and seeded.display_name == username
                and display_name != username
            ):
                _up.update_profile(_config.db_path, username, display_name=display_name)
        except Exception as e:  # noqa: BLE001
            logger.warning("user_profile auto-seed failed user=%s: %s", username, e)

    # Retain the user-scoped OAuth pair when the operator opted in
    # ([web] token_storage = "encrypted" + ISTOTA_WEB_TOKEN_KEY provisioned).
    # Every successful login overwrites the stored pair, so a dead refresh
    # token self-heals here. Best-effort: a storage failure must not break
    # login (the feature degrades to today's behaviour).
    if _config and _config.db_path:
        try:
            from . import web_tokens as _wt  # noqa: PLC0415

            if _wt.feature_enabled(_config) and token.get("refresh_token"):
                _wt.store_tokens(
                    _config.db_path,
                    username,
                    token.get("access_token", ""),
                    token["refresh_token"],
                    token.get("expires_in", 3600),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("web token persistence failed user=%s: %s", username, e)

    request.session.clear()
    request.session["user"] = {
        "username": username,
        "display_name": display_name,
    }
    return RedirectResponse(url="/istota/", status_code=302)


@auth_router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/istota/login", status_code=302)


# ============================================================================
# Google OAuth routes (auth_router only — API routes added after api_router definition)
# ============================================================================


@auth_router.get("/google/connect")
async def google_connect(request: Request):
    """Initiate Google OAuth flow. User must be logged in."""
    user = _get_session_user(request)
    if not user:
        return RedirectResponse(url="/istota/login", status_code=302)
    if not _oauth or not hasattr(_oauth, "google"):
        return Response("Google Workspace not configured", status_code=500)
    hostname, scheme = _get_external_origin()
    redirect_uri = f"{scheme}://{hostname}/istota/google/callback"

    # The registered client carries the operator's whole ceiling as its
    # default scope. Pass the *user's* resolved subset per request instead —
    # authlib's create_authorization_url takes `scope` in kwargs and prefers
    # it over client.scope, so this is an override, not an addition.
    requested = _google_requested_scopes(user["username"])
    if not requested:
        # An empty scope string would send the user to a consent screen that
        # grants nothing. Say why instead.
        logger.info(
            "Google connect declined for user %s: nothing selected within the "
            "instance scope ceiling", user["username"],
        )
        return RedirectResponse(url="/istota/settings?google=no_scopes", status_code=302)

    # ``access_type=offline`` is what makes Google issue a refresh token at
    # all, and ``prompt=consent`` is what makes it issue one *again* on a
    # reconnect — otherwise it returns only an access token for a grant the
    # user has already consented to. ``google_callback`` refuses to store a
    # response without a refresh token (it could not refresh an hour later),
    # so without both, reconnecting after a scope change or a revoke fails
    # with a bare ``?google=error`` and no indication of the cause. Changing
    # the per-user selection is exactly such a reconnect.
    return await _oauth.google.authorize_redirect(
        request, redirect_uri,
        scope=" ".join(requested),
        access_type="offline", prompt="consent",
    )


@auth_router.get("/google/callback")
async def google_callback(request: Request):
    """Handle Google OAuth callback — store tokens in DB."""
    user = _get_session_user(request)
    if not user:
        return RedirectResponse(url="/istota/login", status_code=302)
    if not _oauth or not hasattr(_oauth, "google"):
        return Response("Google Workspace not configured", status_code=500)
    try:
        token = await _oauth.google.authorize_access_token(request)
    except Exception as e:
        logger.error("Google OAuth callback failed: %s", e)
        return RedirectResponse(url="/istota/settings?google=error", status_code=302)

    access_token = token.get("access_token", "")
    refresh_token = token.get("refresh_token", "")
    expires_in = token.get("expires_in", 3600)
    scopes = token.get("scope", "")

    if not access_token or not refresh_token:
        logger.error("Google OAuth: missing tokens (access=%s, refresh=%s)",
                      bool(access_token), bool(refresh_token))
        return RedirectResponse(url="/istota/settings?google=error", status_code=302)

    import json
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    scopes_json = json.dumps(scopes.split()) if isinstance(scopes, str) else json.dumps(scopes)

    from . import db
    with db.get_db(_config.db_path) as conn:
        db.upsert_google_token(
            conn, user["username"], access_token, refresh_token, expiry, scopes_json,
        )
    logger.info("Google account connected for user %s", user["username"])
    return RedirectResponse(url="/istota/settings?google=connected", status_code=302)


# ============================================================================
# API routes
# ============================================================================

api_router = APIRouter(prefix="/istota/api")


def _user_has_feeds(username: str) -> bool:
    """True if the feeds module is enabled for the user (default-on)."""
    if not _config:
        return False
    return _config.is_module_enabled(username, "feeds")


def _user_has_money(username: str) -> bool:
    """True if the money module is enabled for the user (default-on)."""
    if not _config:
        return False
    return _config.is_module_enabled(username, "money")


def _user_has_location(username: str) -> bool:
    """True if the location module is enabled for the user (default-on)."""
    if not _config:
        return False
    if not _config.location.enabled:
        return False
    return _config.is_module_enabled(username, "location")


def _user_has_health(username: str) -> bool:
    """True if the health module is enabled for the user (default-on)."""
    if not _config:
        return False
    return _config.is_module_enabled(username, "health")


def _user_has_briefings(username: str) -> bool:
    """True if the briefings module is enabled for the user (default-on)."""
    if not _config:
        return False
    return _config.is_module_enabled(username, "briefings")


def _has_google_token(username: str) -> bool:
    """Check if a user has connected their Google account."""
    if not _config:
        return False
    try:
        from . import db
        with db.get_db(_config.db_path) as conn:
            return db.has_google_token(conn, username)
    except Exception:
        return False


def _google_granted_scopes(username: str) -> list[str] | None:
    """Scopes Google granted this user, or None if never connected.

    Decryption-free (``db.get_google_scopes``): the display must survive a
    rotated ``ISTOTA_SECRET_KEY`` on a row that is still present.

    Deliberately **not** wrapped in a swallow, unlike ``_has_google_token``.
    ``None`` here means "no row", and the caller turns that into "Not
    connected" — so folding a transient DB failure into the same value would
    invite the user to redo a consent flow they do not need. A read failure
    is a 500 the card reports as a failure to load, which is what happened.
    """
    if not _config:
        return None
    from . import db
    with db.get_db(_config.db_path) as conn:
        return db.get_google_scopes(conn, username)


def _google_scope_selection(username: str) -> dict[str, str]:
    """The user's stored per-service selection ({} when they never chose).

    Fails soft: a DB that is unreachable here must not block a connect, and
    the empty selection resolves to the operator's whole ceiling, which is
    what the user got before the picker existed.
    """
    if not _config:
        return {}
    try:
        from . import google_scopes, user_profiles
        profile = user_profiles.get_profile(_config.db_path, username)
        if profile is None:
            return {}
        return google_scopes.normalize_selection(profile.google_scopes)
    except Exception:
        logger.warning(
            "google_workspace: could not read the scope selection for %s; "
            "falling back to the instance ceiling", username, exc_info=True,
        )
        return {}


def _google_requested_scopes(username: str) -> list[str]:
    """What a connect for this user would ask Google for, ceiling-clamped."""
    if not _config or not _config.google_workspace:
        return []
    from . import google_scopes
    return google_scopes.resolve_selection(
        _google_scope_selection(username), _config.google_workspace.scopes,
    )


def _get_location_config(username: str) -> tuple[str, str, str] | None:
    """Resolve (per-user location.db path, user_id, timezone), or None.

    Per-user split: ``db_path`` now points at
    ``{workspace}/location/data/location.db`` rather than the framework
    ``istota.db``. Callers that also need the framework-side geocode
    cache open a second connection to ``_config.db_path``.
    """
    if not _config or not _config.location.enabled:
        return None
    uc = _config.get_user(username)
    if not uc:
        return None
    from . import location as _location  # noqa: PLC0415
    try:
        loc_ctx = _location.resolve_for_user(username, _config)
    except _location.UserNotFoundError:
        return None
    # Lazy init so /location/* endpoints work even before a ping arrives.
    _location.init_db(loc_ctx.db_path)
    # Live DB timezone so a just-saved web-UI change is reflected (ISSUE-099).
    return str(loc_ctx.db_path), username, _config.resolve_user_timezone(username)


def _resolve_tz(client_tz: str, fallback: str) -> str:
    """Accept a client-supplied IANA timezone only if zoneinfo validates it."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not client_tz:
        return fallback
    try:
        ZoneInfo(client_tz)
        return client_tz
    except (ZoneInfoNotFoundError, ValueError):
        return fallback


@api_router.get("/me")
async def api_me(user: dict = Depends(_require_api_auth)):
    username = user["username"]
    is_admin = _user_is_web_admin(username)
    features: dict = {
        "chat": True,  # web chat is always-on
        "feeds": False,
        "location": False,
        "money": False,
        "health": False,
        "briefings": False,
        "google_workspace": False,
        "google_workspace_enabled": False,
        "admin": is_admin,
    }
    if _config:
        features["feeds"] = _user_has_feeds(username)
        features["location"] = _user_has_location(username)
        features["money"] = _user_has_money(username)
        features["health"] = _user_has_health(username)
        features["briefings"] = _user_has_briefings(username)
        features["google_workspace_enabled"] = _config.google_workspace.enabled
        if _config.google_workspace.enabled:
            features["google_workspace"] = _has_google_token(username)
    # Nextcloud user-token status: null when the feature is off (ephemeral /
    # keyless), {connected: false} when on but nothing stored (user hasn't
    # logged in since enablement, or disconnected), {connected: true,
    # expires_at} when a pair is held. Drives the settings card.
    nextcloud_token = None
    if _config:
        from . import web_tokens as _wt  # noqa: PLC0415
        if _wt.feature_enabled(_config):
            nextcloud_token = (
                _wt.token_status(_config.db_path, username)
                or {"connected": False, "expires_at": None}
            )
    # Ways to reach the bot outside the web UI. Surfaced so the dashboard can
    # tell the user their actual plus-address rather than a generic "you can
    # email me"; null/false when the surface isn't deployed.
    contact = {"email": None, "talk": False}
    if _config:
        from .email_support import per_user_address  # noqa: PLC0415
        contact["email"] = per_user_address(_config, username)
        contact["talk"] = bool(_config.talk.enabled and _config.nextcloud.url)
    return {
        "username": username,
        "display_name": user.get("display_name", username),
        "bot_name": _config.bot_name if _config else "Istota",
        "is_admin": is_admin,
        "features": features,
        "contact": contact,
        "nextcloud_token": nextcloud_token,
    }


# ---- Admin dashboard ----


def _iso_utc(ts: str | None) -> str | None:
    """Normalize a heterogeneous timestamp string to ISO 8601 UTC.

    Inputs come from three writers with different conventions:
    - SQLite ``datetime('now')`` and ``strftime`` — naive, space-separated,
      documented to be UTC.
    - Python ``datetime.now(timezone.utc).isoformat()`` — offset-aware,
      ``T`` separator, ``+00:00`` suffix.
    - Python ``datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")`` — naive.

    Naive timestamps are treated as UTC. Output is always ``YYYY-MM-DDTHH:MM:SSZ``
    so the frontend can pass it straight to ``new Date()``.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace(" ", "T"))
    except (ValueError, TypeError):
        return ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gather_admin_stats() -> dict:
    """Aggregate read-only system stats for the admin dashboard.

    Single payload — every section is best-effort: a failure in one
    sub-aggregator is captured as an error string rather than failing the
    whole request.
    """
    from . import __version__, db

    db_path = _config.db_path
    now = datetime.now(timezone.utc)

    payload: dict = {
        "system": _admin_system_section(__version__, db_path),
        "users": [],
        "scheduler": {"jobs_total": 0, "jobs_active": 0, "jobs_paused": 0, "last_errors": []},
        "modules": {},
        "tasks": {},
        "storage": _admin_storage_section(db_path),
    }

    try:
        with db.get_db(db_path) as conn:
            payload["users"] = _admin_users_section(conn, now)
            payload["scheduler"] = _admin_scheduler_section(conn)
            payload["tasks"] = _admin_tasks_section(conn, now)
            last_run, healthy = _admin_scheduler_health(conn, now)
            payload["system"]["last_scheduler_run"] = last_run
            payload["system"]["scheduler_healthy"] = healthy
    except Exception as exc:
        logger.exception("admin stats DB aggregation failed")
        payload["error"] = str(exc)

    payload["modules"] = _admin_modules_section()
    payload["chat"] = _admin_chat_section()
    payload["runtime"] = _admin_runtime_section()
    payload["models"] = _admin_models_section()
    payload["brain_status"] = _admin_brain_status_section()
    return payload


def _admin_chat_section() -> dict:
    """Web-chat surface gauges. `room_stream_connections` is the live count of
    open room-event streams (one per open /chat tab): the metric that decides
    whether the deferred shared per-user broker — one poller per user fanning
    out in-process instead of one per connection — is ever needed."""
    return {
        "room_stream_connections": _room_stream_conn_delta(0),
        "room_stream_poll_interval_ms": int(
            _chat_knob("room_stream_poll_interval_ms", 1000),
        ),
    }


def _admin_brain_status_section() -> dict:
    """Live brain posture for the admin dashboard (ISSUE-188).

    Distinct from :func:`_admin_models_section`, which reports the *configured*
    brain: this consults the process-global availability breaker so the operator
    can see when the primary brain is down and the fallback is serving traffic.
    When the primary breaker is open, reports ``degraded: true`` with the
    configured fallback kind as ``active``; otherwise ``degraded: false`` on the
    primary. The breaker is process-global in-memory, so a stats call inside the
    daemon's own web process sees the real state (single-daemon deployment).
    Best-effort: any failure returns an ``error`` string rather than aborting the
    whole stats payload.
    """
    if not _config:
        return {}

    try:
        from .brain._fallback import (
            effective_fallback_kind,
            primary_brain_unavailable,
        )

        brain_config = _config.brain
        primary_kind = brain_config.kind
        available, reason = primary_brain_unavailable(brain_config)
        if available:
            return {
                "degraded": False,
                "active": primary_kind,
                "primary": primary_kind,
            }
        # Breaker open: the primary is skipped and the fallback (if any) serves.
        return {
            "degraded": True,
            "primary": primary_kind,
            "active": effective_fallback_kind(brain_config),
            "reason": reason,
        }
    except Exception as exc:  # noqa: BLE001 — never fail the stats payload
        logger.exception("admin brain status section failed")
        return {"error": str(exc)}


def _admin_models_section() -> dict:
    """Configured model + brain-backend block for the admin dashboard.

    Shows the active brain kind, the effective default model/effort, and how the
    portable role aliases (``fast``/``general``/``smart``) resolve right now
    (reflecting any operator ``[models.aliases]`` overrides). ``endpoint`` /
    ``provider`` are only populated for the native brain, which talks to a
    configurable OpenAI-compatible endpoint. Best-effort: any failure returns an
    ``error`` string rather than aborting the whole stats payload.
    """
    if not _config:
        return {}

    from .brain import CANONICAL_ROLES, make_brain

    try:
        brain_config = _config.brain
        brain = make_brain(brain_config)

        # Effective default model: resolve the top-level `model` (may be an
        # alias) to a canonical id. Empty = the brain's own default, which for
        # the native brain is the endpoint's configured model.
        default_model = brain.resolve_model_name(_config.model)
        if not default_model:
            if brain_config.kind == "native":
                default_model = brain_config.native.model or "endpoint default"
            else:
                default_model = "CLI default"

        roles: list[dict] = []
        for role in CANONICAL_ROLES:
            resolved = brain.resolve_alias(role)
            target = resolved[0] if resolved and resolved[0] else None
            roles.append({"role": role, "resolved": target or "brain default"})

        section: dict = {
            "brain_kind": brain_config.kind,
            "default_model": default_model,
            "default_effort": _config.effort or None,
            "roles": roles,
        }

        if brain_config.kind == "native":
            section["endpoint"] = brain_config.native.base_url
            section["provider"] = brain_config.native.provider

        if brain_config.source_type_overrides:
            section["source_type_overrides"] = dict(brain_config.source_type_overrides)

        return section
    except Exception as exc:  # noqa: BLE001 — never fail the stats payload
        logger.exception("admin models section failed")
        return {"error": str(exc)}


def _admin_runtime_section() -> dict:
    """Runtime posture block for the admin dashboard.

    ``mode`` is config-derived (``Config.is_standalone``). ``caveats`` is
    derived from what is *actually* disabled, so it stays accurate as the user
    opts features back in — a caveat whose feature is enabled is omitted. Each
    caveat is ``{"title", "detail"}``. In server mode the block is minimal
    (``mode == "server"``, empty caveats) so the frontend renders nothing.
    """
    if not _config or not _config.is_standalone:
        return {"mode": "server", "caveats": []}

    caveats: list[dict] = []

    # Security caveat is always present in standalone mode. Standalone is a
    # trusted single-user posture without bwrap isolation, so the agent runs
    # with the user's full privileges regardless of the sandbox_enabled flag.
    caveats.append({
        "title": "No sandbox isolation",
        "detail": (
            "The agent runs with your user account's full privileges. Only "
            "give this instance content and instructions you trust."
        ),
    })

    if not _config.nextcloud.url:
        workspace = str(_config.nextcloud_mount_path or "a local folder")
        caveats.append({
            "title": "No Nextcloud",
            "detail": (
                f"The workspace is a local folder ({workspace}); file sharing "
                "and CalDAV-from-Nextcloud are unavailable."
            ),
        })

    if not _config.location.enabled:
        caveats.append({
            "title": "GPS location tracking is off",
            "detail": (
                "The Overland webhook receiver isn't running under `istota "
                "serve` by default."
            ),
        })

    if not _config.talk.enabled:
        caveats.append({
            "title": "Nextcloud Talk is disabled",
            "detail": "Chat is the web UI and REPL only.",
        })

    if not _config.email.enabled:
        caveats.append({
            "title": "Email polling is off",
            "detail": "Inbound/outbound email is disabled.",
        })

    return {"mode": "standalone", "caveats": caveats}


def _admin_system_section(version: str, db_path: Path) -> dict:
    db_size = 0
    try:
        if db_path.exists():
            db_size = db_path.stat().st_size
    except OSError:
        db_size = 0
    return {
        "version": version,
        "uptime_seconds": int(time.time() - _WEB_START_TIME),
        "db_size_bytes": db_size,
        "python_version": platform.python_version(),
        "last_scheduler_run": None,
        "scheduler_healthy": False,
    }


def _admin_storage_section(db_path: Path) -> dict:
    db_size = 0
    try:
        if db_path.exists():
            db_size = db_path.stat().st_size
    except OSError:
        db_size = 0
    mount_healthy = False
    if _config and _config.nextcloud_mount_path:
        try:
            mount_healthy = Path(_config.nextcloud_mount_path).is_dir()
        except OSError:
            mount_healthy = False
    backups_count, last_backup = _scan_db_backups(db_path.parent / "backups")
    return {
        "db_size_bytes": db_size,
        "backups_count": backups_count,
        "last_backup": last_backup,
        # Only meaningful when a Nextcloud server backs the workspace; a local
        # (standalone) install has no mount, so the frontend hides the row.
        "nextcloud_configured": bool(_config and _config.storage_is_nextcloud),
        "nextcloud_mount_healthy": mount_healthy,
    }


def _scan_db_backups(backups_dir: Path) -> tuple[int, str | None]:
    """Count *.db.gz files under daily/ and weekly/, return latest mtime as ISO Z.

    Mirrors the layout produced by deploy/ansible/templates/istota-backup.sh.j2.
    """
    count = 0
    latest: float | None = None
    try:
        for sub in ("daily", "weekly"):
            d = backups_dir / sub
            if not d.is_dir():
                continue
            for p in d.glob("*.db.gz"):
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                count += 1
                if latest is None or mtime > latest:
                    latest = mtime
    except OSError:
        return 0, None
    if latest is None:
        return count, None
    iso = datetime.fromtimestamp(latest, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return count, iso


_INTERACTIVE_SOURCES = frozenset({"talk", "email", "tasks_file", "cli", "web"})
_AUTOMATED_SOURCES = frozenset({"scheduled", "briefing", "heartbeat", "subtask"})


def _classify_source(source_type: str | None) -> str:
    """Classify a ``source_type`` as ``interactive``/``automated``.

    Used to keep the headline numbers honest when module pollers
    (``_module.feeds.run_scheduled`` etc., source_type=``scheduled``) dwarf
    real user-driven traffic. Unknown / NULL source_types fall into
    ``automated`` so the headline split never silently undercounts —
    ``interactive_24h + automated_24h`` always equals ``last_24h``. The
    risk of misclassifying a future interactive type is preferred to
    silent drift.
    """
    if source_type in _INTERACTIVE_SOURCES:
        return "interactive"
    return "automated"


def _admin_users_section(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    """Per-user task counts, joined with config metadata.

    ``last_active`` reflects the user's most recent task creation, not
    ``updated_at`` — the latter bumps on background retries and would show
    "active 30s ago" for users who logged off hours earlier.
    """
    cutoff_24h = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        """
        SELECT user_id,
               COUNT(*) AS total,
               SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS last_24h,
               SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS last_30d,
               MAX(created_at) AS last_active
        FROM tasks
        GROUP BY user_id
        """,
        (cutoff_24h, cutoff_30d),
    ).fetchall()
    by_user = {r["user_id"]: r for r in rows}

    breakdown_rows = conn.execute(
        """
        SELECT user_id, source_type,
               COUNT(*) AS n,
               SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
               AVG(CASE WHEN status = 'completed'
                         AND started_at IS NOT NULL
                         AND completed_at IS NOT NULL
                        THEN (julianday(completed_at) - julianday(started_at)) * 86400
                   END) AS avg_sec
        FROM tasks
        WHERE created_at >= ?
        GROUP BY user_id, source_type
        """,
        (cutoff_24h,),
    ).fetchall()
    breakdown: dict[str, dict[str, dict]] = {}
    for r in breakdown_rows:
        src = r["source_type"] or "unknown"
        entry = breakdown.setdefault(r["user_id"], {})
        entry[src] = {
            "count": int(r["n"]),
            "failed": int(r["failed"] or 0),
            "avg_duration_seconds": (
                round(float(r["avg_sec"]), 2) if r["avg_sec"] is not None else None
            ),
        }

    out = []
    user_ids = set(_config.users.keys()) | set(by_user.keys()) if _config else set(by_user.keys())
    for user_id in sorted(user_ids):
        uc = _config.users.get(user_id) if _config else None
        row = by_user.get(user_id)
        total = int(row["total"]) if row else 0
        last_24h = int(row["last_24h"] or 0) if row else 0
        last_30d = int(row["last_30d"] or 0) if row else 0
        avg_per_day = round(last_30d / 30.0, 2) if last_30d else 0.0
        per_source = breakdown.get(user_id, {})
        interactive_24h = sum(
            v["count"] for s, v in per_source.items() if _classify_source(s) == "interactive"
        )
        automated_24h = sum(
            v["count"] for s, v in per_source.items() if _classify_source(s) == "automated"
        )
        failed_24h = sum(v["failed"] for v in per_source.values())
        out.append({
            "username": user_id,
            "display_name": uc.display_name if uc else user_id,
            "is_admin": _user_is_web_admin(user_id),
            "tasks_total": total,
            "tasks_last_24h": last_24h,
            "tasks_avg_per_day": avg_per_day,
            "tasks_by_source_24h": per_source,
            "tasks_interactive_24h": interactive_24h,
            "tasks_automated_24h": automated_24h,
            "tasks_failed_24h": failed_24h,
            "last_active": _iso_utc(row["last_active"]) if row else None,
        })
    return out


def _admin_scheduler_section(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT id, user_id, name, cron_expression, enabled, last_run_at,
               last_success_at, consecutive_failures, last_error
        FROM scheduled_jobs
        ORDER BY user_id, name
        """,
    ).fetchall()
    jobs = []
    last_errors = []
    active = paused = 0
    for r in rows:
        enabled = bool(r["enabled"])
        if enabled:
            active += 1
        else:
            paused += 1
        jobs.append({
            "id": r["id"],
            "user_id": r["user_id"],
            "name": r["name"],
            "cron": r["cron_expression"],
            "enabled": enabled,
            "last_run_at": _iso_utc(r["last_run_at"]),
            "last_success_at": _iso_utc(r["last_success_at"]),
            "consecutive_failures": r["consecutive_failures"] or 0,
            "last_error": r["last_error"],
        })
        if r["last_error"] and (r["consecutive_failures"] or 0) > 0:
            last_errors.append({
                "job_name": f"{r['user_id']}/{r['name']}",
                "error": r["last_error"],
                "timestamp": _iso_utc(r["last_run_at"]),
            })
    return {
        "jobs_total": len(jobs),
        "jobs_active": active,
        "jobs_paused": paused,
        "jobs": jobs,
        "last_errors": last_errors[:10],
    }


def _admin_scheduler_health(conn: sqlite3.Connection, now: datetime) -> tuple[str | None, bool]:
    row = conn.execute("SELECT MAX(updated_at) AS last_run FROM tasks").fetchone()
    last_run_raw = row["last_run"] if row else None
    last_run = _iso_utc(last_run_raw)
    if not last_run_raw:
        return None, False
    try:
        ts = datetime.fromisoformat(last_run_raw.replace(" ", "T"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        healthy = (now - ts) < timedelta(minutes=5)
    except ValueError:
        healthy = False
    return last_run, healthy


def _admin_tasks_section(conn: sqlite3.Connection, now: datetime) -> dict:
    cutoff_24h = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    total = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]

    by_source_rows = conn.execute(
        """
        SELECT source_type,
               SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS n_24h,
               SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS n_30d,
               SUM(CASE WHEN created_at >= ? AND status = 'failed' THEN 1 ELSE 0 END) AS failed_24h
        FROM tasks
        WHERE created_at >= ?
        GROUP BY source_type
        """,
        (cutoff_24h, cutoff_30d, cutoff_24h, cutoff_30d),
    ).fetchall()
    by_source: dict[str, int] = {}
    failed_by_source: dict[str, int] = {}
    last_24h = 0
    last_30d = 0
    interactive_24h = automated_24h = 0
    interactive_30d = automated_30d = 0
    for r in by_source_rows:
        src = r["source_type"] or "unknown"
        n24 = int(r["n_24h"] or 0)
        n30 = int(r["n_30d"] or 0)
        f24 = int(r["failed_24h"] or 0)
        last_24h += n24
        last_30d += n30
        if n24:
            by_source[src] = n24
        if f24:
            failed_by_source[src] = f24
        bucket = _classify_source(src)
        if bucket == "interactive":
            interactive_24h += n24
            interactive_30d += n30
        elif bucket == "automated":
            automated_24h += n24
            automated_30d += n30

    duration_row = conn.execute(
        """
        SELECT AVG((julianday(completed_at) - julianday(started_at)) * 86400) AS avg_sec
        FROM tasks
        WHERE created_at >= ?
          AND status = 'completed'
          AND started_at IS NOT NULL
          AND completed_at IS NOT NULL
        """,
        (cutoff_24h,),
    ).fetchone()
    avg_duration = float(duration_row["avg_sec"]) if duration_row["avg_sec"] else 0.0

    # Error rate over terminal states only — including pending/locked/running
    # in the denominator would spike the rate to 100% on a quiet day with one
    # failure and a few in-flight tasks.
    terminals = conn.execute(
        """
        SELECT
          SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
          SUM(CASE WHEN status IN ('completed', 'failed') THEN 1 ELSE 0 END) AS terminal
        FROM tasks
        WHERE created_at >= ?
        """,
        (cutoff_24h,),
    ).fetchone()
    failed_24h = int(terminals["failed"] or 0)
    terminal_24h = int(terminals["terminal"] or 0)
    error_rate = (failed_24h / terminal_24h) if terminal_24h else 0.0

    return {
        "total": total,
        "last_24h": last_24h,
        "avg_per_day_30d": round(last_30d / 30.0, 2) if last_30d else 0.0,
        "by_source": by_source,
        "failed_by_source_24h": failed_by_source,
        "avg_duration_seconds": round(avg_duration, 2),
        "error_rate_24h": round(error_rate, 4),
        "failed_24h": failed_24h,
        "interactive_24h": interactive_24h,
        "automated_24h": automated_24h,
        "interactive_avg_per_day_30d": round(interactive_30d / 30.0, 2) if interactive_30d else 0.0,
        "automated_avg_per_day_30d": round(automated_30d / 30.0, 2) if automated_30d else 0.0,
    }


def _admin_modules_section() -> dict:
    """Per-module health snapshot. Each sub-aggregator is best-effort."""
    modules: dict = {}
    if not _config:
        return modules

    # feeds + money keep bespoke aggregators: feeds carries richer
    # unreachable/resolve-error status semantics, money has no SQLite to count
    # (beancount files). The remaining SQLite-backed modules share one driver.
    feeds = _admin_module_feeds()
    if feeds is not None:
        modules["feeds"] = feeds

    money = _admin_module_money()
    if money is not None:
        modules["money"] = money

    for spec in _MODULE_DB_STATS:
        stats = _aggregate_module_db(spec)
        if stats is not None:
            modules[spec.name] = stats

    return modules


def _admin_module_feeds() -> dict | None:
    # Count users with the feeds module enabled even if we can't resolve
    # their workspace (e.g. ``nextcloud_mount_path`` unset on docker-compose
    # deploys). Returning ``None`` would silently hide a configured-but-
    # unreachable subsystem from admins.
    configured = sum(
        1 for uid in _config.users
        if _config.is_module_enabled(uid, "feeds")
    )
    if not configured:
        return None

    feeds_total = entries_total = entries_unread = 0
    last_poll = None
    poll_errors = 0
    users_resolved = 0
    resolve_errors = 0
    try:
        from istota.feeds._loader import UserNotFoundError, resolve_for_user
    except Exception:  # pragma: no cover
        return {
            "users_configured": configured,
            "status": "unreachable",
        }

    for user_id in _config.users:
        try:
            ctx = resolve_for_user(user_id, _config)
        except UserNotFoundError:
            continue
        except Exception:
            logger.exception("feeds resolve failed for %s", user_id)
            resolve_errors += 1
            continue
        users_resolved += 1
        try:
            with sqlite3.connect(str(ctx.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                feeds_total += conn.execute("SELECT COUNT(*) AS n FROM feeds").fetchone()["n"]
                entries_total += conn.execute("SELECT COUNT(*) AS n FROM feed_entries").fetchone()["n"]
                entries_unread += conn.execute(
                    "SELECT COUNT(*) AS n FROM feed_entries WHERE status = 'unread'",
                ).fetchone()["n"]
                row = conn.execute(
                    "SELECT MAX(last_fetched_at) AS lp FROM feeds",
                ).fetchone()
                if row["lp"] and (last_poll is None or row["lp"] > last_poll):
                    last_poll = row["lp"]
                poll_errors += conn.execute(
                    "SELECT COUNT(*) AS n FROM feeds WHERE error_count > 0",
                ).fetchone()["n"]
        except sqlite3.Error:
            logger.exception("feeds db read failed for %s", user_id)
            resolve_errors += 1
            continue

    out = {
        "users_configured": configured,
        "users_resolved": users_resolved,
        "feeds_total": feeds_total,
        "entries_total": entries_total,
        "entries_unread": entries_unread,
        "last_poll": _iso_utc(last_poll),
        "poll_errors_24h": poll_errors,
    }
    if users_resolved == 0:
        out["status"] = "unreachable"
    elif resolve_errors:
        out["resolve_errors"] = resolve_errors
    return out


def _admin_module_money() -> dict | None:
    users_with = sum(
        1 for uid in _config.users
        if _config.is_module_enabled(uid, "money")
    )
    if not users_with:
        return None
    return {"users_configured": users_with}


@dataclass(frozen=True)
class _ModuleDbStats:
    """Declarative admin snapshot for a per-user SQLite-backed module.

    Only three things vary between modules: the ``COUNT(*)`` queries, the
    timestamp queries feeding a single "last activity" field, and the module
    handle. The driver (`_aggregate_module_db`) supplies the shared loop:
    enumerate enabled users, resolve + connect each user's DB, sum the counts,
    take the max timestamp, and skip a broken DB without blanking the row.

    The module must expose the standard loader protocol — ``list_users`` /
    ``resolve_for_user`` / a ``connect`` context manager / ``UserNotFoundError``
    and a context object with ``.db_path`` (see `_module_loader`).

    - ``counts``: ``(output_field, "SELECT COUNT(*) AS n FROM …")`` pairs.
    - ``timestamp_field`` / ``timestamp_queries``: the queries (each aliasing
      the value ``AS ts``) whose max fills that one field.
    """

    name: str
    counts: tuple[tuple[str, str], ...]
    timestamp_field: str
    timestamp_queries: tuple[str, ...]


_MODULE_DB_STATS: tuple[_ModuleDbStats, ...] = (
    _ModuleDbStats(
        name="location",
        counts=(
            ("visits_total", "SELECT COUNT(*) AS n FROM visits"),
            ("places_total", "SELECT COUNT(*) AS n FROM places"),
        ),
        timestamp_field="last_update",
        timestamp_queries=("SELECT MAX(timestamp) AS ts FROM location_pings",),
    ),
    _ModuleDbStats(
        name="health",
        counts=(
            ("panels_total", "SELECT COUNT(*) AS n FROM panels"),
            ("biomarkers_total", "SELECT COUNT(*) AS n FROM biomarkers"),
            ("encounters_total", "SELECT COUNT(*) AS n FROM encounters"),
            ("immunizations_total", "SELECT COUNT(*) AS n FROM immunizations"),
        ),
        timestamp_field="last_update",
        timestamp_queries=(
            "SELECT MAX(measured_at) AS ts FROM stats",
            "SELECT MAX(drawn_at) AS ts FROM panels",
        ),
    ),
    _ModuleDbStats(
        name="briefings",
        counts=(
            ("blocks_total", "SELECT COUNT(*) AS n FROM briefing_blocks"),
            ("sources_total", "SELECT COUNT(*) AS n FROM briefing_block_sources"),
            ("archived_total", "SELECT COUNT(*) AS n FROM briefing_archive"),
        ),
        timestamp_field="last_generated",
        timestamp_queries=("SELECT MAX(generated_at) AS ts FROM briefing_archive",),
    ),
)


def _module_loader(name: str):
    """Resolve a module's loader protocol via lazy import.

    Returns ``(list_users, resolve_for_user, connect, UserNotFoundError)``.
    ``connect`` lives on the package for most modules but on the ``.db``
    submodule for briefings — one fallback handles that without a per-module
    special case. Lazy so importing ``web_app`` doesn't pull every module in.
    """
    mod = importlib.import_module(f"istota.{name}")
    connect = getattr(mod, "connect", None)
    if connect is None:
        connect = importlib.import_module(f"istota.{name}.db").connect
    return mod.list_users, mod.resolve_for_user, connect, mod.UserNotFoundError


def _aggregate_module_db(spec: _ModuleDbStats) -> dict | None:
    """Shared driver for a per-user SQLite-backed module's admin snapshot.

    Returns ``None`` (module hidden) when no user has it enabled; otherwise a
    dict of ``users_configured`` + the spec's counts + its single timestamp
    field. Per-user try/except so one broken DB doesn't blank the row; an
    unresolvable user (e.g. no mount) is skipped quietly.
    """
    try:
        list_users, resolve_for_user, connect, not_found = _module_loader(spec.name)
    except Exception:  # pragma: no cover - module import failure
        logger.exception("%s module loader import failed", spec.name)
        return None

    users = list_users(_config)
    if not users:
        return None

    out: dict = {"users_configured": len(users)}
    for field, _sql in spec.counts:
        out[field] = 0
    out[spec.timestamp_field] = None

    latest: str | None = None
    for uid in users:
        try:
            ctx = resolve_for_user(uid, _config)
            if not ctx.db_path.exists():
                continue
            with connect(ctx.db_path) as conn:
                for field, sql in spec.counts:
                    out[field] += conn.execute(sql).fetchone()["n"]
                for query in spec.timestamp_queries:
                    row = conn.execute(query).fetchone()
                    ts = row["ts"] if row else None
                    if ts and (latest is None or ts > latest):
                        latest = ts
        except not_found:
            continue
        except Exception:
            logger.exception("%s module stats failed for user=%s", spec.name, uid)
    out[spec.timestamp_field] = _iso_utc(latest)
    return out


@api_router.get("/admin/stats")
async def admin_stats(_: dict = Depends(_require_admin)):
    """Single payload backing the admin dashboard. Read-only."""
    return await asyncio.to_thread(_gather_admin_stats)


# ---- Task event stream (task-event-streaming spec) ----
#
# SSE and snapshot consumers read the task_events table from the web process —
# the table is the bus (WAL handles concurrent reads from scheduler writes).
# No live subscriber, no IPC.

_SSE_POLL_SECONDS = 0.2


def _sse_poll_seconds() -> float:
    """The SSE generator's table-poll cadence, from ``[web.chat]
    sse_poll_interval_ms`` (falls back to the module default if unset)."""
    ms = getattr(_config.web.chat, "sse_poll_interval_ms", None)
    return (ms / 1000.0) if ms else _SSE_POLL_SECONDS


def _task_owner(task_id: int) -> str | None:
    from . import db
    with db.get_db(_config.db_path) as conn:
        task = db.get_task(conn, task_id)
        return task.user_id if task else None


def _load_task_events(task_id: int, since_seq: int) -> list[dict]:
    from . import db
    with db.get_db(_config.db_path) as conn:
        return db.get_task_events(conn, task_id, since_seq)


_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _synthetic_terminal_events(task_id: int, after_seq: int) -> list[dict]:
    """Terminal backstop for the web chat stream.

    A web task's event log is the bus, but it can be emptied out from under a
    watching client: ``set_task_pending_retry`` deletes every row and resets the
    per-task ``seq`` on each retry-eligible failure, so the final attempt's
    ``error``/``done`` land at a ``seq`` *below* the client's resume cursor and
    never reach it — the UI hangs on "Working…" though the task is terminal. A
    crash that skips ``EventWriter.finish()`` leaves the same gap.

    When the task is terminal but no ``done`` is deliverable to a client parked
    at ``after_seq``, synthesize the terminal frames from the task row, numbered
    *above* ``after_seq`` so the client's monotonic-seq guard accepts them.
    Returns ``[]`` while the task is still running (incl. ``pending`` between
    retries) or when a real ``done`` is still deliverable normally.
    """
    from . import db
    synth_msg_id: int | None = None
    with db.get_db(_config.db_path) as conn:
        task = db.get_task(conn, task_id)
        if task is None or task.status not in _TERMINAL_TASK_STATUSES:
            return []
        pending = db.get_task_events(conn, task_id, after_seq)
        # The durable star key for a completed room turn, so a synthesized
        # terminal frame makes the turn starrable just like the live `done`
        # event does (ISSUE-172).
        if task.status == "completed" and task.conversation_token:
            synth_msg_id = db.get_turn_message_id(
                conn, task.conversation_token, task_id, "assistant",
            )
    if any(e["kind"] == "done" for e in pending):
        return []  # a real terminal frame is still on its way to this client
    seq = max([after_seq, *(e["seq"] for e in pending)]) + 1
    frames: list[dict] = []
    if task.status == "completed":
        # Full answer from the durable task row — the resume/backstop frame
        # must not re-clip what the live path now delivers whole (ISSUE-178).
        frames.append({"seq": seq, "kind": "result",
                       "payload": {"text": task.result or ""}})
    elif task.status == "cancelled":
        frames.append({"seq": seq, "kind": "cancelled", "payload": {}})
    else:  # failed — mirror the live error frame's raw-ish message
        frames.append({"seq": seq, "kind": "error",
                       "payload": {"message": (task.error or "Task failed.")[:500],
                                   "stop_reason": "error"}})
    done_payload: dict = {
        "stop_reason": "completed" if task.status == "completed" else "error",
    }
    if task.model_used:
        done_payload["model"] = task.model_used
    if synth_msg_id is not None:
        done_payload["msg_id"] = synth_msg_id
    frames.append({"seq": seq + 1, "kind": "done", "payload": done_payload})
    return frames


async def _authorize_task_access(task_id: int, user: dict) -> None:
    """404 if the task is unknown, 403 if it isn't the caller's (admins exempt)."""
    from fastapi import HTTPException
    owner = await asyncio.to_thread(_task_owner, task_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="task not found")
    if owner != user["username"] and not _user_is_web_admin(user["username"]):
        raise HTTPException(status_code=403, detail="not your task")


@api_router.get("/chat/tasks/{task_id}/events")
async def chat_task_events(
    task_id: int, since_seq: int = 0, user: dict = Depends(_require_api_auth),
):
    """Snapshot of a task's events (web chat reconnect / late connect)."""
    await _authorize_task_access(task_id, user)
    events = await asyncio.to_thread(_load_task_events, task_id, since_seq)
    # Polling-fallback backstop: a terminal task whose `done` the client can't
    # reach (retry wiped the log / crash skipped finish()) gets a synthesized
    # terminal frame so the poll loop settles instead of spinning forever.
    if not any(e["kind"] == "done" for e in events):
        last = max([since_seq, *(e["seq"] for e in events)])
        events = events + await asyncio.to_thread(
            _synthetic_terminal_events, task_id, last,
        )
    return {"events": events}


@api_router.get("/chat/tasks/{task_id}/stream")
async def chat_task_stream(
    task_id: int, request: Request, since_seq: int = 0,
    user: dict = Depends(_require_api_auth),
):
    """SSE stream of a task's events.

    Resumes from ``Last-Event-ID`` (browser EventSource) or ``?since_seq=``.
    A late connect (task already finished) dumps the full history and closes.
    The stream ends after the terminal ``done`` event.
    """
    await _authorize_task_access(task_id, user)

    header_id = request.headers.get("last-event-id")
    if header_id:
        try:
            since_seq = max(since_seq, int(header_id))
        except ValueError:
            pass

    async def _generate():
        last = since_seq
        while True:
            if await request.is_disconnected():
                return
            events = await asyncio.to_thread(_load_task_events, task_id, last)
            for ev in events:
                last = ev["seq"]
                payload = json.dumps(ev["payload"])
                yield f"id: {ev['seq']}\nevent: {ev['kind']}\ndata: {payload}\n\n"
                if ev["kind"] == "done":
                    return
            if not events:
                # No new rows. If the task is terminal but this client will never
                # get a `done` (retry deleted + seq-reset the log, or a crash
                # skipped finish()), synthesize one so the stream ends instead of
                # polling forever. No-op while the task is still running/pending.
                synth = await asyncio.to_thread(
                    _synthetic_terminal_events, task_id, last,
                )
                for ev in synth:
                    last = ev["seq"]
                    yield (f"id: {ev['seq']}\nevent: {ev['kind']}\n"
                           f"data: {json.dumps(ev['payload'])}\n\n")
                    if ev["kind"] == "done":
                        return
            await asyncio.sleep(_sse_poll_seconds())

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- Room event stream (live-web-chat-room-stream spec) ----
#
# Sibling of the task stream above, and the same principle: the table is the
# bus. Where `chat_task_stream` tails `task_events` for ONE task the client
# started, this tails the canonical `messages` store for EVERY room the user is
# a member of — so a Talk turn, a routed alert, or a background-room message
# reaches the browser without the client polling a full history page per room.
#
# One connection per session, not one per room: room switching becomes a
# client-side filter, background rooms get real content, and a tab holds two
# connections total (this, plus a task stream while a turn is in flight).
#
# `messages.id` is the whole cursor — one monotonic integer over user turns,
# assistant turns and system messages, because the unified-room-sync work
# already consolidated them into one table. That is what closes the fast-turn
# hole structurally: a Talk turn that starts and finishes inside 200ms still
# writes two `messages` rows, and both are tailed.

_ROOM_STREAM_POLL_SECONDS = 1.0
_ROOM_STREAM_KEEPALIVE_SECONDS = 20.0
_ROOM_STREAM_ROOM_CHECK_SECONDS = 10.0
# Read-only tail against a DB the scheduler is writing (co-located under
# `istota serve`). WAL handles concurrent readers, but bound the wait the same
# way the scheduler's main loop does rather than blocking 30s on a lock.
_ROOM_STREAM_BUSY_TIMEOUT_MS = 2000

_room_stream_connections = 0
_room_stream_lock = threading.Lock()


def _room_stream_conn_delta(delta: int) -> int:
    """Track live room-stream connections (admin-stats gauge). The metric that
    decides whether the deferred shared per-user broker is ever needed."""
    global _room_stream_connections
    with _room_stream_lock:
        _room_stream_connections = max(0, _room_stream_connections + delta)
        return _room_stream_connections


def _chat_knob(name: str, default: float) -> float:
    """A ``[web.chat]`` numeric knob, falling back to the module default when
    absent or zero — same pattern as `_sse_poll_seconds`. Use `_chat_knob_opt`
    for a knob whose 0 means 'off' rather than 'unset'."""
    chat = getattr(getattr(_config, "web", None), "chat", None)
    value = getattr(chat, name, None)
    return float(value) if value else float(default)


def _chat_knob_opt(name: str, default: float) -> float:
    """As `_chat_knob`, but an explicit ``0`` is honoured as 0 (disabled) rather
    than folded onto the default. Only the attribute being absent falls back."""
    chat = getattr(getattr(_config, "web", None), "chat", None)
    value = getattr(chat, name, None)
    return float(default) if value is None else float(value)


def _room_events_batch(
    username: str, since_id: int, limit: int | None = None,
) -> dict:
    """One tail batch for ``username``: ``{events, cursor, gap}``.

    ``cursor`` is what the client should adopt. On the happy path it is the last
    delivered row's id. On truncation it is the maximum id the server **scanned**
    — not the last one sent, which would silently strand the truncated rows —
    and ``gap`` is True with no events, telling the client to reload (rooms +
    active room) rather than replay a backlog.

    Truncation has two independent triggers, because row count is a poor proxy
    for cost: a joined assistant row carries `execution_trace`, so a flat row cap
    can mean anything from a few hundred KB to several MB. `room_stream_max_batch`
    is the outer LIMIT that stops the query pulling megabytes before a byte
    budget could measure them; `room_stream_max_bytes` is the accumulate-and-
    truncate budget on serialize.
    """
    from . import db
    max_batch = int(_chat_knob("room_stream_max_batch", 500))
    max_bytes = int(_chat_knob("room_stream_max_bytes", 2_000_000))
    want = max_batch if limit is None else max(1, min(int(limit), max_batch))
    with db.get_db(
        _config.db_path, busy_timeout_ms=_ROOM_STREAM_BUSY_TIMEOUT_MS,
    ) as conn:
        # Cheap gate: O(1) against the primary key. Only a cursor that has
        # actually fallen behind pays for the per-user visibility join, so an
        # idle deployment costs one trivial query per connection per tick.
        max_id = db.max_message_id(conn)
        if max_id <= since_id:
            return {"events": [], "cursor": since_id, "gap": False}
        rows = db.list_room_events_since(
            conn, username, since_id=since_id, limit=want + 1,
        )
    truncated = len(rows) > want
    events: list[dict] = []
    total = 0
    for r in rows[:want]:
        d = _cross_room_message_dict(r, username)
        total += len(json.dumps(d))
        if total > max_bytes:
            truncated = True
            break
        events.append(d)
    if truncated:
        return {"events": [], "cursor": max_id, "gap": True}
    # Not truncated → every row above `since_id` was scanned, so the cursor
    # advances to the max id scanned even when nothing was *visible* to this
    # user. Advancing only to the last delivered row would make a user with few
    # visible messages on a busy instance re-scan the same range every tick.
    # `max()` guards the (harmless) case of a row landing between the gate read
    # and the tail read.
    cursor = max_id
    if events:
        cursor = max(cursor, int(events[-1]["msg_id"]))
    return {"events": events, "cursor": cursor, "gap": False}


def _room_deletions_batch(username: str, since_id: int) -> dict:
    """Deletions visible to ``username`` with ledger id > ``since_id``.

    A second cursor alongside the message tail, and it has to be: a hard delete
    leaves no `messages` row for the id-ordered message tail to carry, so
    without its own ledger the news reaches another open tab only on the next
    full reload. Same O(1)-gate shape as `_room_events_batch` — on a deployment
    where nobody has ever deleted anything, this costs one trivial MAX() per
    connection per tick and never runs the visibility join.
    """
    from . import db
    max_batch = int(_chat_knob("room_stream_max_batch", 500))
    with db.get_db(
        _config.db_path, busy_timeout_ms=_ROOM_STREAM_BUSY_TIMEOUT_MS,
    ) as conn:
        max_id = db.max_message_deletion_id(conn)
        if max_id <= since_id:
            return {"deletions": [], "cursor": since_id}
        rows = db.list_message_deletions_since(
            conn, username, since_id=since_id, limit=max_batch,
        )
    return {
        "deletions": [
            {"msg_id": int(r["message_id"]), "room_token": r["room_token"]}
            for r in rows
        ],
        # The max id *scanned*, not the last one delivered — the same reason
        # the message tail advances past invisible rows: otherwise a cursor
        # behind someone else's deletions never catches up and the gate never
        # short-circuits again.
        "cursor": max_id,
    }


def _room_snapshot(username: str) -> dict[str, dict]:
    """Room metadata the sidebar renders, keyed by canonical token.

    Read-only (unlike `_chat_list_rooms`, which seeds handles / bindings / read
    cursors), because it runs on every stream tick-interval. A registry room
    with no `web_chat_rooms` handle yet has no frontend id, so it is skipped —
    the 30s rooms poll creates the handle and the next diff picks it up.

    Unread counts are deliberately NOT part of the snapshot: they change on
    every message, which is exactly what the `message` frames already carry.
    """
    from . import db
    with db.get_db(
        _config.db_path, busy_timeout_ms=_ROOM_STREAM_BUSY_TIMEOUT_MS,
    ) as conn:
        handles = {
            h.token: h
            for h in db.list_web_chat_rooms(conn, username, include_archived=True)
        }
        out: dict[str, dict] = {}
        for r in db.list_member_rooms(conn, username, include_archived=False):
            handle = handles.get(r.token)
            if handle is None:
                continue
            out[r.token] = {
                "id": handle.id,
                "token": r.token,
                "name": r.name or handle.name,
                "origin": r.origin,
                "model": r.model,
                "effort": r.effort,
            }
    return out


def _room_delta_frames(before: dict[str, dict], after: dict[str, dict]) -> list[dict]:
    """`room` frame payloads for what changed between two snapshots — a rename,
    a model/effort change, or a room appearing / disappearing on another device
    or surface. Closes the "renamed or deleted elsewhere never propagates" gap
    without another full room-list fetch."""
    frames: list[dict] = []
    for token, room in after.items():
        if before.get(token) != room:
            frames.append({"action": "upsert", "room": room})
    for token, room in before.items():
        if token not in after:
            frames.append({"action": "remove", "token": token, "id": room["id"]})
    return frames


@api_router.get("/chat/events")
async def chat_room_events(
    since_id: int = 0,
    since_deletion_id: int = 0,
    limit: int = 0,
    user: dict = Depends(_require_api_auth),
):
    """Snapshot of the room event tail — the polling fallback behind
    ``/chat/stream``, mirroring how ``/chat/tasks/{id}/events`` backs
    ``/chat/tasks/{id}/stream``. ``limit=0`` means "the server's own cap"; the
    client passes ``limit=1`` when it only wants a fresh cursor after a
    reload.

    Carries the deletion tail on the same response so the fallback path is not
    a downgrade — a deletion would otherwise be invisible to a client that had
    dropped to polling until its next full reload."""
    username = user["username"]
    batch = await asyncio.to_thread(
        _room_events_batch, username, max(0, since_id),
        limit if limit > 0 else None,
    )
    deletions = await asyncio.to_thread(
        _room_deletions_batch, username, max(0, since_deletion_id),
    )
    batch["deletions"] = deletions["deletions"]
    batch["deletion_cursor"] = deletions["cursor"]
    return batch


@api_router.get("/chat/stream")
async def chat_room_stream(
    request: Request,
    since_id: int = 0,
    since_deletion_id: int = 0,
    user: dict = Depends(_require_api_auth),
):
    """SSE stream of every message visible to the caller, across all their rooms.

    Resumes from ``Last-Event-ID`` (EventSource sends it automatically) or
    ``?since_id=``. Unlike the task stream this NEVER terminates on its own —
    it is session-lived, which is why it emits a keepalive comment frame.

    Only message-bearing frames carry an SSE ``id:``. EventSource retains the
    last id it saw, so an auxiliary frame (keepalive, room metadata, deletions)
    carrying an unrelated id would move the resume cursor to the wrong place on
    reconnect. The deletion tail therefore carries its own cursor *inside* the
    frame payload, and the client passes it back as ``since_deletion_id`` — a
    reconnect after a delete must not silently resurrect the message.
    """
    username = user["username"]
    header_id = request.headers.get("last-event-id")
    if header_id:
        try:
            since_id = max(since_id, int(header_id))
        except ValueError:
            pass
    since_id = max(0, since_id)

    poll = _chat_knob("room_stream_poll_interval_ms", 1000) / 1000.0
    keepalive = _chat_knob(
        "room_stream_keepalive_seconds", _ROOM_STREAM_KEEPALIVE_SECONDS,
    )
    room_check = _chat_knob_opt(
        "room_stream_room_check_seconds", _ROOM_STREAM_ROOM_CHECK_SECONDS,
    )

    async def _generate():
        cursor = since_id
        del_cursor = since_deletion_id
        last_frame = time.monotonic()
        last_room_check = 0.0
        snapshot: dict[str, dict] | None = None
        _room_stream_conn_delta(1)
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    batch = await asyncio.to_thread(
                        _room_events_batch, username, cursor,
                    )
                except sqlite3.OperationalError:
                    batch = None  # lock held past the budget — skip this tick
                except Exception:  # noqa: BLE001 — a bad row must not kill the stream
                    logger.warning("room stream: tail failed", exc_info=True)
                    batch = None
                if batch and batch["gap"]:
                    cursor = batch["cursor"]
                    yield (f"id: {cursor}\nevent: gap\n"
                           f"data: {json.dumps({'cursor': cursor})}\n\n")
                    last_frame = time.monotonic()
                elif batch:
                    for ev in batch["events"]:
                        yield (f"id: {ev['msg_id']}\nevent: message\n"
                               f"data: {json.dumps(ev)}\n\n")
                        last_frame = time.monotonic()
                    # Adopt the batch's own cursor, not the last delivered id.
                    # `_room_events_batch` deliberately advances past rows this
                    # user cannot see; taking `events[-1]` instead would leave
                    # `max_id > cursor` permanently true on any instance where
                    # someone else is writing, so the MAX(id) gate would never
                    # short-circuit and the per-user visibility join would run
                    # every tick. The client's `Last-Event-ID` stays on the last
                    # *message* id (auxiliary advances carry no frame), so a
                    # resume merely re-scans a range — harmless.
                    cursor = max(cursor, int(batch["cursor"]))

                try:
                    dels = await asyncio.to_thread(
                        _room_deletions_batch, username, del_cursor,
                    )
                except sqlite3.OperationalError:
                    dels = None  # lock held past the budget — skip this tick
                except Exception:  # noqa: BLE001 — must not kill the stream
                    logger.warning("room stream: deletions failed", exc_info=True)
                    dels = None
                if dels and dels["deletions"]:
                    # No `id:` — this is an auxiliary frame, and moving
                    # EventSource's resume cursor onto a ledger id would strand
                    # the message tail. The payload carries its own cursor.
                    yield ("event: message_deleted\n"
                           f"data: {json.dumps(dels)}\n\n")
                    last_frame = time.monotonic()
                if dels:
                    del_cursor = max(del_cursor, int(dels["cursor"]))

                now = time.monotonic()
                if room_check and now - last_room_check >= room_check:
                    last_room_check = now
                    try:
                        fresh = await asyncio.to_thread(_room_snapshot, username)
                    except Exception:  # noqa: BLE001 — metadata is best-effort
                        fresh = None
                    if fresh is not None:
                        # The first pass establishes the baseline the client
                        # already has from its own room-list load.
                        if snapshot is not None:
                            for frame in _room_delta_frames(snapshot, fresh):
                                yield f"event: room\ndata: {json.dumps(frame)}\n\n"
                        snapshot = fresh

                if time.monotonic() - last_frame >= keepalive:
                    last_frame = time.monotonic()
                    yield ": ping\n\n"
                await asyncio.sleep(poll)
        finally:
            _room_stream_conn_delta(-1)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@api_router.get("/admin/tasks/{task_id}/events")
async def admin_task_events(
    task_id: int, since_seq: int = 0, _: dict = Depends(_require_admin),
):
    """All events for a task — backs the admin in-flight task-detail view."""
    events = await asyncio.to_thread(_load_task_events, task_id, since_seq)
    return {"events": events}


# ---- Admin logs (ISSUE-203) ----
#
# A read path over operationally sensitive data — the app log carries whatever
# the daemon logged, and `task_logs` embeds truncated task results, so this sees
# across users. Three properties carry the safety:
#
#   1. Every route is `_require_admin`, which fails closed on a blank
#      /etc/istota/admins (unlike `Config.is_admin`'s permissive empty rule).
#   2. A request names a *source id*, never a path. The only place a path is
#      derived is `admin_logs.resolve_app_log_chain`, which confines every
#      candidate to the resolved log directory. Traversal is not reachable.
#   3. Records are returned as JSON data and rendered as text nodes. Nothing in
#      the pipeline treats log content as markup.

_LOG_PAGE_DEFAULT = 200
_LOG_PAGE_MAX = 1000

# Deliberately slower than the chat streams' ~200ms: a log tail is read at human
# pace and each poll is a file stat or an indexed `id >` scan.
_LOG_STREAM_POLL_SECONDS = 1.0
# ~15s of silence before a keepalive, so an idle tail behind a proxy with a
# short read timeout does not look like a dropped connection.
_LOG_STREAM_KEEPALIVE_TICKS = 15


@dataclass(frozen=True)
class _LogQuery:
    """Filters shared by the page and stream routes."""

    min_level: str | None = None
    q: str | None = None
    logger: str | None = None
    user_id: str | None = None
    task_id: int | None = None


def _log_query(
    level: str | None, q: str | None, logger_name: str | None,
    user_id: str | None, task_id: int | None,
) -> _LogQuery:
    from . import admin_logs

    def _clean(value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed[:200] or None

    # An unmodelled level is rejected rather than passed through: it means
    # opposite things on the two sources (the file reader ranks an unknown level
    # above CRITICAL and would hide everything; the DB reader's IN-list goes
    # empty and returns *only* unmodelled rows), and neither is an error the
    # caller could tell from a genuinely empty log.
    min_level = _clean(level)
    if min_level is not None:
        min_level = min_level.upper()
        if min_level not in admin_logs.LEVELS:
            raise HTTPException(
                status_code=400,
                detail=f"unknown level {min_level!r}; expected one of "
                       f"{', '.join(admin_logs.LEVELS)}",
            )

    return _LogQuery(
        min_level=min_level,
        q=_clean(q),
        logger=_clean(logger_name),
        user_id=_clean(user_id),
        task_id=task_id,
    )


def _require_log_source(source_id: str):
    """Resolve a source id, or raise. Blocking — call via ``asyncio.to_thread``.

    ``list_sources`` stats the whole rotation chain and iterates the log
    directory, so this is real filesystem I/O and must not run on the loop.
    """
    from . import admin_logs

    source = admin_logs.get_source(_config, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="unknown log source")
    if not source.available:
        raise HTTPException(status_code=409, detail=source.detail)
    return source


def _read_log_page(source_id: str, limit: int, before: str | None, query: _LogQuery) -> dict:
    from . import admin_logs, db

    if source_id == "app":
        chain = admin_logs.resolve_app_log_chain(_config)
        page = admin_logs.read_file_page(
            chain, limit=limit, before=before,
            min_level=query.min_level, q=query.q, logger=query.logger,
        )
        return page.to_dict()

    with db.get_db(_config.db_path) as conn:
        page = admin_logs.read_task_log_page(
            conn, limit=limit, before=before,
            min_level=query.min_level, q=query.q,
            user_id=query.user_id, task_id=query.task_id,
        )
    return page.to_dict()


def _validate_log_cursor(source_id: str, cursor: str) -> None:
    """Raise ``ValueError`` if ``cursor`` is not well-formed for the source.

    Shape only — no read. Used by the stream route, which must reject a bad
    cursor with a 400 before the response body starts.
    """
    from . import admin_logs

    if source_id == "app":
        chain = admin_logs.resolve_app_log_chain(_config)
        if not chain:
            return
        admin_logs.parse_file_cursor(chain, cursor)
        if not cursor.startswith(chain[0].name + ":"):
            raise ValueError("tail cursor must name the live log file")
        return

    try:
        if int(cursor) < 0:
            raise ValueError("malformed log cursor")
    except (TypeError, ValueError) as exc:
        raise ValueError("malformed log cursor") from exc


def _read_log_tail(source_id: str, cursor: str, query: _LogQuery) -> dict:
    from . import admin_logs, db

    if source_id == "app":
        chain = admin_logs.resolve_app_log_chain(_config)
        tail = admin_logs.read_file_tail(
            chain, cursor,
            min_level=query.min_level, q=query.q, logger=query.logger,
        )
        return tail.to_dict()

    with db.get_db(_config.db_path) as conn:
        tail = admin_logs.read_task_log_tail(
            conn, cursor,
            min_level=query.min_level, q=query.q,
            user_id=query.user_id, task_id=query.task_id,
        )
    return tail.to_dict()


@api_router.get("/admin/logs/sources")
async def admin_log_sources(_: dict = Depends(_require_admin)):
    """The readable log sources for this deployment, available or not.

    An unavailable source is listed *with its reason* rather than hidden — "file
    logging is off" is the answer to "why is there nothing here", and omitting
    the row leaves the admin to guess.
    """
    from . import admin_logs

    sources = await asyncio.to_thread(admin_logs.list_sources, _config)
    return {"sources": [s.to_dict() for s in sources]}


@api_router.get("/admin/logs/{source_id}")
async def admin_log_page(
    source_id: str,
    limit: int = _LOG_PAGE_DEFAULT,
    before: str | None = None,
    level: str | None = None,
    q: str | None = None,
    logger_name: str | None = Query(None, alias="logger"),
    user_id: str | None = None,
    task_id: int | None = None,
    _: dict = Depends(_require_admin),
):
    """A page of records from ``source_id``, oldest-first."""
    await asyncio.to_thread(_require_log_source, source_id)
    limit = max(1, min(limit, _LOG_PAGE_MAX))
    query = _log_query(level, q, logger_name, user_id, task_id)
    try:
        return await asyncio.to_thread(_read_log_page, source_id, limit, before, query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@api_router.get("/admin/logs/{source_id}/stream")
async def admin_log_stream(
    source_id: str,
    request: Request,
    cursor: str,
    level: str | None = None,
    q: str | None = None,
    logger_name: str | None = Query(None, alias="logger"),
    user_id: str | None = None,
    task_id: int | None = None,
    _: dict = Depends(_require_admin),
):
    """Live tail of ``source_id`` from ``cursor``.

    Polls the source rather than pushing, for the same reason the task and room
    streams do: the writer is a different process (the scheduler unit), so there
    is no in-process bus to subscribe to. Cadence is deliberately slower than the
    chat streams — a log tail is read at human pace and each poll is a file stat
    or an indexed `id >` scan.

    The frame carries its cursor *inside* the payload and sends no SSE ``id:``.
    A log cursor is not an integer for the file source (it is ``name:offset``),
    and EventSource's ``Last-Event-ID`` resume would otherwise hand back a value
    the client also has to reconcile with a ``reset``. The client re-opens with
    the cursor it last saw instead.
    """
    await asyncio.to_thread(_require_log_source, source_id)
    query = _log_query(level, q, logger_name, user_id, task_id)

    # Validate the cursor's *shape* up front: a StreamingResponse body cannot
    # turn a later exception into a 400, so a malformed cursor must fail before
    # the response starts rather than as a silently-dead stream. Deliberately a
    # parse rather than a read — a full read here would duplicate up to a
    # megabyte of file I/O (or a LIKE scan) whose result is then discarded.
    try:
        await asyncio.to_thread(_validate_log_cursor, source_id, cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def _generate():
        current = cursor
        idle = 0
        while True:
            if await request.is_disconnected():
                return
            try:
                tail = await asyncio.to_thread(_read_log_tail, source_id, current, query)
            except ValueError:
                # The live file rotated out from under a cursor naming a file
                # that has since left the chain. Tell the client to re-seed.
                yield f"event: reset\ndata: {json.dumps({'reason': 'cursor expired'})}\n\n"
                return
            except Exception:  # noqa: BLE001 - a read error must not 500 mid-stream
                # Named `stream_error`, not `error`: EventSource already has a
                # built-in `error` event for connection failures, so a frame by
                # that name lands on the same listener and is indistinguishable
                # from a dropped socket — the payload would never be seen.
                logger.exception("admin log stream read failed (source=%s)", source_id)
                yield (
                    "event: stream_error\n"
                    f"data: {json.dumps({'error': 'log read failed'})}\n\n"
                )
                return

            current = tail["cursor"] or current
            if tail["records"] or tail["reset"]:
                idle = 0
                yield f"event: records\ndata: {json.dumps(tail)}\n\n"
            else:
                idle += 1
                if idle >= _LOG_STREAM_KEEPALIVE_TICKS:
                    idle = 0
                    yield ": ping\n\n"
            await asyncio.sleep(_LOG_STREAM_POLL_SECONDS)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@api_router.get("/admin/config")
async def admin_config(_: dict = Depends(_require_admin)):
    """The loaded config, sectioned and credential-redacted. Read-only.

    Shaped field-by-field (dotted key + type + secret flag) rather than as a
    TOML dump so the same payload can back an editable form later without the
    frontend changing shape.
    """
    from . import admin_config_view

    return await asyncio.to_thread(admin_config_view.build_config_view, _config)


# ---- Web chat surface ----
#
# Always-on in-app companion to Talk. Rooms are per-user channel tokens (each
# carries its own CHANNEL.md + sleep-cycle handling). A sent message becomes a
# source_type="web" / output_target="web" task; the result and progress live in
# the task_events table the existing /chat/tasks/{id}/stream SSE endpoint tails.


def _room_to_dict(room) -> dict:
    return {
        "id": room.id,
        "token": room.token,
        "name": room.name,
        "archived": room.archived,
        "created_at": room.created_at,
        "updated_at": room.updated_at,
    }


def _known_room_models() -> set[str]:
    """Canonical model ids a room default may be set to — the distinct targets
    the active brain exposes via its alias table. Used to validate the PATCH."""
    try:
        return {
            model for _alias, model, _effort in make_brain(_config.brain).list_aliases()
            if model
        }
    except Exception:  # noqa: BLE001 — validation degrades to "reject all" safely
        logger.warning("known_room_models: brain aliases unavailable", exc_info=True)
        return set()


def _chat_list_rooms(username: str) -> list[dict]:
    """The user's non-archived rooms from the unified registry — both web- and
    Talk-origin. A Talk room the bot joined surfaces here automatically (it was
    lazily registered on its first inbound message). Each registry room is given
    a ``web_chat_rooms`` handle (the frontend's integer room id) and a ``web``
    binding on first listing — that handle/binding *is* the room's web presence.
    Each entry carries ``origin`` so the UI can badge Talk rooms and gate the
    promote action, and ``last_activity`` so the sidebar can hold its
    most-recently-active-first order without refetching the list.

    The order is `list_member_rooms`' — newest activity first — and the client
    renders it as given, so this is where the sidebar's order is decided."""
    from . import db
    with db.get_db(_config.db_path) as conn:
        db.ensure_default_web_chat_room(conn, username)
        registry = db.list_member_rooms(conn, username, include_archived=False)
        out: list[dict] = []
        for r in registry:
            handle = db.ensure_web_chat_handle(
                conn, username, r.token, r.name or "Talk room",
            )
            # Membership says this room is visible to the user, so a leftover
            # per-user archived flag (set when they previously hid the room, then
            # were re-added by a new inbound) is stale — clear it so the payload
            # doesn't report a shown room as archived (ISSUE-134).
            if handle.archived:
                handle = db.update_web_chat_room(conn, handle.id, archived=False) or handle
            db.add_room_binding(conn, r.token, "web", r.token)
            d = _room_to_dict(handle)
            d["name"] = r.name or handle.name
            d["origin"] = r.origin
            # Standing per-room model/effort default lives on the shared registry
            # room (canonical), not the per-user web handle.
            d["model"] = r.model
            d["effort"] = r.effort
            # The sidebar renders this list in order and re-sorts on this stamp
            # as messages stream in, so it is normalized the same way a message
            # row's `created_at` is — the client compares the two directly.
            d["last_activity"] = _iso_utc(r.last_activity or r.created_at)
            # Unread badge. Seed the web read cursor on first surface so a
            # pre-existing backlog doesn't read as unread, then count messages
            # past it. Per-room try/except so one bad count can't abort the
            # whole listing.
            try:
                db.initialize_room_read_state(conn, r.token, "web", username)
                d["unread_count"] = db.count_unread_messages(
                    conn, r.token, "web", username,
                )
            except Exception:
                logger.warning(
                    "unread count failed for room %s", r.token, exc_info=True,
                )
                d["unread_count"] = 0
            out.append(d)
    return out


def _chat_create_room(username: str, name: str) -> dict:
    from . import db
    with db.get_db(_config.db_path) as conn:
        room = db.create_web_chat_room(conn, username, name)
    d = _room_to_dict(room)
    # Nothing has been said in it yet, so its activity stamp is its birth — but
    # it has to carry one, or the sidebar's activity sort would file the room
    # the user just created below every room they haven't touched in weeks.
    # Server-clocked rather than minted client-side, so it can't disagree with
    # the stamps the room list sends.
    d["last_activity"] = _iso_utc(room.created_at)
    return d


def _chat_owned_room(username: str, room_id: int):
    """Return the room if it belongs to ``username``, else None."""
    from . import db
    with db.get_db(_config.db_path) as conn:
        room = db.get_web_chat_room(conn, room_id)
    if room is None or room.user_id != username:
        return None
    return room


def _chat_answer_confirmation(
    username: str,
    token: str,
    text: str,
    answer,
    reply_to_msg_id: int | None = None,
    client_msg_id: str | None = None,
) -> dict | None:
    """Answer the held task this reply lands on. None = nothing to answer.

    The web half of ISSUE-243. Returning None rather than a string when nothing
    is parked is what keeps the fall-through rule intact: "yes" is a perfectly
    ordinary reply to a question the bot asked in prose, so matching the word
    must not by itself suppress task creation — only finding a parked question
    may.

    Path A works here because ISSUE-242's mirror stamps the prompt's Talk id
    onto the mirrored ``messages`` row, so a cited web reply can be walked back
    to ``tasks.talk_response_id``. The citation is checked against this room
    first, so the endpoint does not become an id oracle for another's rooms.
    """
    from . import confirmations, db

    with db.get_db(_config.db_path) as conn:
        # A retry of a send we accepted but never got to report must resolve to
        # the answer it already gave, not answer again. Re-resolving is not
        # merely wasteful: the first attempt consumed the question, so a second
        # gate parked in between would be the single open one and get approved
        # on a "yes" the user typed at a different question entirely. The
        # ordinary send path gets this from `_is_own_replay`, which cannot see
        # this exchange — its lookup inner-joins `tasks` and these rows carry no
        # task id.
        prior = db.find_confirmation_exchange(conn, token, client_msg_id)
        if prior is not None:
            user_msg_id, system_msg_id, ack = prior
            return {
                "ack": ack,
                "user_msg_id": user_msg_id,
                "system_msg_id": system_msg_id,
            }

        talk_response_id: int | None = None
        if reply_to_msg_id is not None:
            target = db.get_reply_target(conn, reply_to_msg_id)
            if target is not None and target[0] == token:
                external = db.get_message_external_id(conn, reply_to_msg_id, "talk")
                if external and external.isdecimal():
                    talk_response_id = int(external)

        res = confirmations.resolve(
            conn, username, conversation_token=token,
            talk_response_id=talk_response_id,
        )
        if res.ambiguous:
            # Nothing was decided, so nothing is recorded — the listing is a
            # "say which", and the `!command` inline-only precedent fits it
            # where the decision precedent does not. It is also the one branch
            # that consumes no question, so recording it would let a client
            # loop append transcript rows without bound (this path returns
            # before `_chat_create_web_task`, where the rate limit lives).
            return {
                "ack": confirmations.ambiguity_listing(conn, res.ambiguous),
                "user_msg_id": None,
                "system_msg_id": None,
            }
        if res.task is None:
            return None

        ack = confirmations.apply_answer(conn, res.task, answer, _config)
        user_msg_id, system_msg_id = confirmations.record_exchange(
            conn, token, answer_text=text, ack=ack, origin_surface="web",
            client_msg_id=client_msg_id, answered_by=username,
        )
        conn.commit()

    return {
        "ack": ack,
        "user_msg_id": user_msg_id,
        "system_msg_id": system_msg_id,
    }


def _chat_mark_room_read(username: str, room_id: int) -> dict | None:
    """Advance the user's web read cursor for a room to its current newest
    message. Returns ``{cursor, advanced, room_token}``, or None if the room
    isn't the user's. ``advanced`` gates the web→Talk read push — the UI fires
    mark-read on every visibilitychange, so no-op calls are common and must
    not hit Nextcloud."""
    from . import db
    with db.get_db(_config.db_path) as conn:
        room = db.get_web_chat_room(conn, room_id)
        if room is None or room.user_id != username:
            return None
        old = db.get_room_read_state(conn, room.token, "web", username)
        max_id = db.room_max_message_id(conn, room.token)
        db.set_room_read_state(conn, room.token, "web", max_id, username)
    return {"cursor": max_id, "advanced": max_id > old, "room_token": room.token}


def _chat_update_room(
    username: str, room_id: int, name: str | None, archived: bool | None,
    model=_UNSET, effort=_UNSET,
) -> dict | None:
    from . import db
    with db.get_db(_config.db_path) as conn:
        room = db.get_web_chat_room(conn, room_id)
        if room is None or room.user_id != username:
            return None
        updated = db.update_web_chat_room(
            conn, room_id, name=name, archived=archived,
        )
        # Keep the unified room registry in sync (the cross-surface room list /
        # future sidebar reads it, not web_chat_rooms).
        if updated is not None:
            # Per-room model/effort default (canonical). Only touch a column
            # when its key was present in the request, so a name-only edit
            # doesn't clobber the model default. Merge against current state.
            if model is not _UNSET or effort is not _UNSET:
                reg = db.get_room(conn, updated.token)
                new_model = model if model is not _UNSET else (reg.model if reg else None)
                new_effort = effort if effort is not _UNSET else (reg.effort if reg else None)
                db.set_room_model_effort(conn, updated.token, new_model, new_effort)
            if name is not None:
                db.rename_room(conn, updated.token, updated.name)
            if archived is not None:
                reg = db.get_room(conn, updated.token)
                if reg is not None and reg.origin == "talk":
                    # Shared Talk room: hide per-user via membership, never via
                    # the global archived flag (ISSUE-134) — that would hide it
                    # from the other participants too. Mirror _chat_delete_room:
                    # the dismissal tombstone is what makes the hide durable
                    # against the poll's membership re-seed, so write/clear it
                    # alongside the membership change.
                    if archived:
                        db.remove_room_member(conn, updated.token, username)
                        db.dismiss_room(conn, updated.token, username)
                    else:
                        db.add_room_member(conn, updated.token, username)
                        db.undismiss_room(conn, updated.token, username)
                else:
                    db.set_room_archived(conn, updated.token, bool(archived))
        if updated is None:
            return None
        d = _room_to_dict(updated)
        reg = db.get_room(conn, updated.token)
        d["model"] = reg.model if reg else None
        d["effort"] = reg.effort if reg else None
    return d


def _chat_delete_room(username: str, room_id: int) -> str:
    """Hard-delete a room and its token-scoped rows. Returns a status string:
    ``"not_found"`` (unknown / not owned), ``"busy"`` (a task is in flight), or
    ``"ok"``. The DB cascade is one transaction; the ``CHANNEL.md`` removal is
    best-effort and never fails the delete."""
    from . import db
    with db.get_db(_config.db_path) as conn:
        room = db.get_web_chat_room(conn, room_id)
        if room is None or room.user_id != username:
            return "not_found"
        if db.count_active_web_tasks(conn, room.token, username) > 0:
            return "busy"
        # A Talk-origin room is hidden per-user, not destroyed: deleting from web
        # must not wipe a Nextcloud Talk conversation's mirrored history, and —
        # because the room is shared (ISSUE-134) — must not hide it from the
        # other participants. Drop only this user's membership + archive their own
        # handle; the global `rooms.archived` flag stays reserved for "the bot
        # left the Nextcloud room" (archive_orphaned_talk_rooms).
        reg = db.get_room(conn, room.token)
        if reg is not None and reg.origin == "talk":
            db.remove_room_member(conn, room.token, username)
            # Durable hide tombstone: the poll-time Talk-room registration
            # backfill re-adds membership for every participant, so dropping the
            # membership row alone wouldn't keep the room hidden. The tombstone
            # excludes it from the web list until the user re-engages (posts in
            # the room), which clears it via `record_inbound`.
            db.dismiss_room(conn, room.token, username)
            db.update_web_chat_room(conn, room_id, archived=True)
            return "ok"
        db.delete_web_chat_room(conn, room_id, username)
        token = room.token
    # Best-effort: drop the channel's CHANNEL.md directory. Outside the DB
    # transaction; a filesystem failure leaves the dir but doesn't fail the API.
    if _config.nextcloud_mount_path:
        channel_dir = _config.nextcloud_mount_path / "Channels" / token
        try:
            shutil.rmtree(channel_dir, ignore_errors=True)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("chat room delete: CHANNEL.md cleanup failed: %s", exc)
    return "ok"


def _room_talk_binding(username: str, room_id: int) -> str | None:
    """The Talk room token a web room is bound to, or None. Owner-scoped."""
    from . import db
    with db.get_db(_config.db_path) as conn:
        handle = db.get_web_chat_room(conn, room_id)
        if handle is None or handle.user_id != username:
            return None
        binding = db.get_room_binding(conn, handle.token, "talk")
    return binding.surface_ref if binding else None


async def _chat_promote_to_talk(username: str, room_id: int) -> dict | None:
    """"Also open in Talk": create a real Nextcloud Talk conversation for a
    web-origin room, add the requesting user, bind it, and seed a single pointer
    post (older history stays in web — open question 4's lean). Returns the
    updated room dict, or None if the room is unknown / not owned / not a
    web-origin room / already bound to Talk / Talk is unconfigured."""
    from . import db
    from .talk import TalkClient

    with db.get_db(_config.db_path) as conn:
        handle = db.get_web_chat_room(conn, room_id)
        if handle is None or handle.user_id != username:
            return None
        token = handle.token
        reg = db.get_room(conn, token)
        if reg is None or reg.origin != "web":
            return None  # only web-origin rooms promote
        if db.get_room_binding(conn, token, "talk") is not None:
            return None  # already bound
        name = reg.name or handle.name
    if not _config.nextcloud.url:
        return None

    # One-off OCS calls from the web process (not the scheduler delivery path),
    # so a dedicated short-lived client is fine here.
    client = TalkClient(_config)
    try:
        room = await client.create_conversation(name)
        talk_token = room.get("token")
        if not talk_token:
            return None
        # Persist the binding *immediately* — before the best-effort
        # add_participant / seed-post steps, which can hang or crash. Otherwise a
        # failure between the OCS create and a binding write that trailed those
        # slow calls would leave an orphaned Talk room with no binding, and a
        # re-promote (which only checks for a missing binding) would create a
        # *second* Talk room. The write is its own short transaction; the
        # subsequent steps are recoverable, the binding is not. Idempotent
        # (INSERT OR IGNORE), so the trailing re-read block is safe too.
        with db.get_db(_config.db_path) as conn:
            db.add_room_binding(conn, token, "talk", talk_token)
        try:
            await client.add_participant(talk_token, username)
        except Exception as e:
            logger.warning("promote: add_participant failed for %s: %s", username, e)
        try:
            await client.send_message(
                talk_token,
                "Continued from the web chat — earlier history lives in the web app.",
            )
        except Exception as e:  # seed post is best-effort
            logger.debug("promote: seed post failed: %s", e)
    finally:
        await client.aclose()

    with db.get_db(_config.db_path) as conn:
        handle = db.get_web_chat_room(conn, room_id)
        reg = db.get_room(conn, token)
    d = _room_to_dict(handle)
    d["origin"] = reg.origin if reg else "web"
    d["talk_token"] = talk_token
    return d


def _trace_tool_descriptions(execution_trace: str | None, actions_taken: str | None) -> list[str]:
    """Tool-use descriptions for a finished task, in order, so the client can
    rebuild the action strip as a persisted "done" trace (ISSUE-122). Prefers
    the ordered ``execution_trace`` (tool entries only), falling back to the
    flat ``actions_taken`` list. Malformed JSON degrades to an empty list."""
    if execution_trace:
        try:
            entries = json.loads(execution_trace)
            tools = [
                e.get("text") or ""
                for e in entries
                if isinstance(e, dict) and e.get("type") == "tool"
            ]
            if tools:
                return tools
        except (ValueError, TypeError):
            pass
    if actions_taken:
        try:
            actions = json.loads(actions_taken)
            if isinstance(actions, list):
                return [str(a) for a in actions]
        except (ValueError, TypeError):
            pass
    return []


def _trace_segments(
    execution_trace: str | None,
    actions_taken: str | None,
    result: str | None,
    *,
    status: str = "completed",
) -> list[dict]:
    """Ordered, interleaved ``text`` / ``tool`` segments for a finished task, so
    the web client reconstructs the same in-order layout as the live stream.

    Prefers the ordered ``execution_trace`` (``type`` of ``text`` / ``tool`` /
    ``cm_boundary``; the boundary is skipped). The canonical answer is the
    ``result``: when non-empty it overwrites the trailing text segment (or is
    appended when the trace ends on a tool). Falls back to the flat
    ``actions_taken`` tool descriptions plus a result text segment when the
    trace is absent or malformed. Never raises.

    For an *interrupted* task (``status`` of ``failed`` / ``cancelled``) the
    terminal ``result`` is the error / cancel notice — a new message appended
    *after* the trace's intermediate content, not a replacement for the draft
    answer. Overwriting (the completed-task path) would discard the last
    intermediate text block the model produced before the interruption
    (ISSUE-183).
    """
    result = result or ""
    segments: list[dict] = []
    parsed_trace = False
    if execution_trace:
        try:
            entries = json.loads(execution_trace)
            if isinstance(entries, list):
                parsed_trace = True
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    etype = e.get("type")
                    if etype == "text":
                        segments.append({"kind": "text", "text": e.get("text") or ""})
                    elif etype == "tool":
                        segments.append({"kind": "tool", "text": e.get("text") or ""})
                    # cm_boundary (and anything else) is skipped.
        except (ValueError, TypeError):
            parsed_trace = False
    if not parsed_trace:
        # Fallback: ordered tool descriptions, then the answer.
        for desc in _trace_tool_descriptions(None, actions_taken):
            segments.append({"kind": "tool", "text": desc})
    if result:
        if status in ("failed", "cancelled"):
            # Interrupted task: the terminal error / cancel notice is a new
            # message, not the canonical answer replacing a draft. Append it
            # so the trace's trailing intermediate text survives.
            segments.append({"kind": "text", "text": result})
        elif segments and segments[-1]["kind"] == "text":
            segments[-1]["text"] = result
        else:
            segments.append({"kind": "text", "text": result})
    return segments


def _task_duration_seconds(started_at: str | None, completed_at: str | None) -> float | None:
    """Wall-clock seconds between a task's ``started_at`` and ``completed_at``
    (both SQLite ``datetime('now')`` strings), rounded to match the live `done`
    event's ``duration_seconds``. ``None`` if either is missing/unparseable."""
    if not started_at or not completed_at:
        return None
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        start = datetime.strptime(started_at[:19], fmt)
        end = datetime.strptime(completed_at[:19], fmt)
    except ValueError:
        return None
    delta = (end - start).total_seconds()
    return round(delta, 1) if delta >= 0 else None


def _assistant_message_dict(row, text: str, status: str, *, confirmation: bool = False) -> dict:
    """Build a transcript assistant-message dict from a row that carries the
    enrichment columns (status / actions_taken / execution_trace / started_at /
    completed_at / model_used) — a `messages`⋈`tasks` row or a `tasks` row. When
    the task has been retention-deleted those columns are NULL and the turn
    degrades to a plain `text` bubble. `_row_get` tolerates either source."""
    created_at = _turn_created_at(row)
    if confirmation:
        return {
            "role": "assistant", "text": text, "task_id": _row_get(row, "task_id") or _row_get(row, "id"),
            "status": status, "confirmation": True, "created_at": created_at,
        }
    trace = _row_get(row, "execution_trace")
    actions = _row_get(row, "actions_taken")
    out = {
        "role": "assistant", "text": text,
        "task_id": _row_get(row, "task_id") or _row_get(row, "id"),
        "status": status, "created_at": created_at,
        "tools": _trace_tool_descriptions(trace, actions),
        "segments": _trace_segments(trace, actions, text, status=status),
        "duration_seconds": _task_duration_seconds(
            _row_get(row, "started_at"), _row_get(row, "completed_at"),
        ),
        "model": _row_get(row, "model_used") or None,
    }
    # Store-sourced rows carry the message's stable id + the requesting user's
    # star flag; aux (`tasks`-only) rows have neither — such turns aren't
    # starrable until mirrored to the durable store.
    msg_id = _row_get(row, "msg_id")
    if msg_id is not None:
        out["msg_id"] = msg_id
        out["starred"] = bool(_row_get(row, "starred"))
    return out


def _row_get(row, key: str):
    """sqlite3.Row.get() equivalent — returns None for a column absent from the
    row's keys instead of raising (the two source queries differ in columns)."""
    return row[key] if key in row.keys() else None


def _turn_created_at(row):
    """When a row places a turn on the timeline, prefer the turn's `turn_ts`.

    Only the aux (`tasks`) gap-fill query selects `turn_ts` (see `_AUX_TURN_TS`);
    a spine row has no such column and falls back to its own `created_at`. The
    distinction matters because a `tasks` row is stamped up to a clock second
    before the user row it answers, and the transcript's final sort is on
    `created_at` — so a gap-filled error bubble rendered at the raw task stamp
    sorts *above* the question it is answering."""
    return _row_get(row, "turn_ts") or _row_get(row, "created_at")


# Surface filter shared by the spine query and its `has_more` probe: web/talk
# turns render both halves; scheduled posts render the assistant only (the
# synthetic cron prompt was never user-authored). Canonical definition lives in
# db.py so the cross-room aggregate query (`db.list_messages_across_rooms`)
# can't drift from the per-room spine.
_SPINE_SURFACE = _db.TRANSCRIPT_SURFACE_FILTER
# Columns the spine query selects: the durable turn + the LEFT JOIN tasks
# enrichment (trace / timing / model). `m.id AS msg_id` is the raw keyset
# tiebreaker for the cursor AND the message's stable star key; `starred` is the
# requesting user's star flag (the join takes the username as the query's FIRST
# positional parameter).
_SPINE_COLUMNS = (
    "SELECT m.role AS role, m.body AS body, m.task_id AS task_id, "
    "  m.id AS msg_id, m.created_at AS created_at, t.status AS status, "
    "  t.actions_taken AS actions_taken, t.execution_trace AS execution_trace, "
    "  t.started_at AS started_at, t.completed_at AS completed_at, "
    "  t.model_used AS model_used, (s.message_id IS NOT NULL) AS starred, "
    "  m.attachments AS attachments, t.attachments AS task_attachments, "
    "  m.attachment_paths AS attachment_paths, "
    "  m.reply_to_message_id AS reply_to_message_id, "
    # Who wrote a `role='user'` row the reader did not — see the matching
    # fragment in `db._CROSS_ROOM_COLUMNS`.
    "  m.author_user_id AS author_user_id, m.author_label AS author_label, "
    # Truncated here rather than in the dict builder, matching the cross-room
    # fragment: no read path needs more of the parent than the excerpt cap.
    # The literal must track `_REPLY_EXCERPT_CHARS` below.
    "  p.role AS reply_role, substr(p.body, 1, 200) AS reply_body "
    "FROM messages m LEFT JOIN tasks t ON t.id = m.task_id "
    "LEFT JOIN message_stars s ON s.message_id = m.id AND s.user_id = ? "
    # The cited parent, joined live rather than snapshotted: nothing in the
    # stack edits `messages.body`, so the join can't drift. A primary-key
    # lookup, and a NULL result against a non-NULL id is the deleted case.
    "LEFT JOIN messages p ON p.id = m.reply_to_message_id "
)
# Where a gap-filled turn sits on the timeline: its **user spine row's**
# `created_at`, falling back to the `tasks` row's own stamp for a turn the store
# doesn't hold (spineless legacy / failed-only rooms).
#
# The two stamps are not the same instant. `record_inbound` creates the task and
# *then* writes the user row, each defaulting to its own `datetime('now')` — a
# second-granularity clock — so a turn whose two inserts straddle a clock second
# has a `tasks.created_at` up to a second *before* the spine row it belongs to.
# Banding the aux fill on the raw task stamp then drops the answer whenever that
# turn is the page's oldest (`created_at >= t1` excludes it by one second),
# which for a room shorter than one page is every load: the user sees their
# question with no error bubble under it, permanently. Banding on the coalesced
# stamp uses the same key the spine is ordered by, so a turn lands on the page
# its user row is on — no gap at the boundary, and no double-render onto the
# neighbouring page. Only the timestamp is coalesced, not the `id` tiebreaker,
# so a turn tying the page floor second but sorting below it by id can still
# split across two pages; that is the pre-existing limit of a timestamp-only
# band, unchanged here.
_AUX_TURN_TS = (
    "COALESCE((SELECT m3.created_at FROM messages m3 "
    "WHERE m3.task_id = tasks.id AND m3.room_token = tasks.conversation_token "
    "AND m3.role = 'user' LIMIT 1), tasks.created_at)"
)
# Wrapping the column in a COALESCE costs the range bound `idx_tasks_user_created`
# gave the raw `created_at`, so every band and probe degrades to a scan of all of
# this user's tasks with one correlated lookup per row — measured at 0.003 ms →
# 3.3 ms for one probe over 10k tasks, growing linearly, twice per page load.
# Each bound therefore carries a redundant *sargable* companion on the raw
# column, which restores the index range (measured back at 0.003 ms).
#
# The companion is safe because it is deliberately looser than the exact test it
# accompanies, so it can never exclude a row the exact test would keep:
# `record_inbound` writes the task before its user row, making
# `turn_ts >= tasks.created_at`, and an hour of slack absorbs a backwards clock
# step that would invert them. The one writer that stamps a user row far from
# its task — `backfill_room_messages_from_talk_cache`, which uses the historical
# Talk timestamp — recovers *completed* turns only, and every band below is
# scoped to failed/cancelled, so the two cannot meet.
_AUX_TS_BELOW = "tasks.created_at < datetime(?, '+1 hour')"
_AUX_TS_ABOVE = "tasks.created_at >= datetime(?, '-1 hour')"
# Columns the aux (`tasks`) gap-fill query selects. `turn_ts` is the banding,
# cursor AND rendered key for these rows; the raw `created_at` is selected only
# as the COALESCE fallback source and for `_task_duration_seconds`.
_AUX_COLUMNS = (
    "SELECT id, prompt, result, status, error, confirmation_prompt, "
    "created_at, actions_taken, execution_trace, started_at, completed_at, "
    f"model_used, attachments, {_AUX_TURN_TS} AS turn_ts FROM tasks "
)

# Which `tasks` rows may gap-fill a room transcript (failed/cancelled answers,
# in-flight slots, legacy turns). web/talk are room surfaces by construction.
#
# `email` is admitted only for a turn whose question was actually mirrored into
# this room (ISSUE-136). Without it, an email turn that fails, is cancelled, or
# is still running renders as a question with no answer, no error and no
# streaming slot — the mirror image of the orphaned reply the issue fixed, since
# the scheduler only stores an assistant row on the success path. Keying on the
# mirrored user row rather than on `source_type` alone is what keeps a
# confirmation-gated email out: its mirror is deliberately withheld until the
# user approves it, and these rows render `tasks.prompt`, so a bare source-type
# widening would publish the untrusted content the gate exists to hold back.
_AUX_SOURCE_SCOPE = (
    "(source_type IN ('web', 'talk') OR (source_type = 'email' AND EXISTS ("
    "SELECT 1 FROM messages m2 WHERE m2.room_token = tasks.conversation_token "
    "AND m2.task_id = tasks.id AND m2.role = 'user')))"
)


def _row_reply_to(row) -> dict | None:
    """The rendered citation for a history row, or None when it isn't a reply.

    Two shapes, and the client distinguishes them on `deleted`: a live parent
    carries its role and a display excerpt, a hard-deleted one carries only the
    id it used to name. The excerpt cap is a *display* bound, distinct from the
    1000-char prompt snapshot on the task — without it every reply in a page
    would carry a whole assistant answer to render as two lines.
    """
    keys = row.keys()
    if "reply_to_message_id" not in keys:
        return None
    parent_id = row["reply_to_message_id"]
    if parent_id is None:
        return None
    if row["reply_role"] is None:
        return {"msg_id": parent_id, "deleted": True}
    return {
        "msg_id": parent_id,
        "role": row["reply_role"],
        "excerpt": (row["reply_body"] or "")[:_REPLY_EXCERPT_CHARS],
        "deleted": False,
    }


def _row_attachment_names(row, *, message_column: bool = True) -> list[str] | None:
    """The attachment chip labels for a history row, or None for a turn that
    carried no files.

    Prefers the display names stored on the canonical `messages` row (what the
    user actually picked). Falls back to basenames of the joined `tasks` paths,
    which covers turns predating the message-side column — and only those, since
    retention deletes the task row not long after.
    """
    keys = row.keys()
    if message_column and "attachments" in keys and row["attachments"]:
        try:
            names = json.loads(row["attachments"])
        except (TypeError, ValueError):
            names = None
        if isinstance(names, list) and names:
            return [str(n) for n in names]
    raw_paths = None
    if not message_column and "attachments" in keys:
        raw_paths = row["attachments"]
    elif "task_attachments" in keys:
        raw_paths = row["task_attachments"]
    if not raw_paths:
        return None
    try:
        paths = json.loads(raw_paths)
    except (TypeError, ValueError):
        return None
    if not isinstance(paths, list) or not paths:
        return None
    return [os.path.basename(str(p)) for p in paths]


def _row_attachment_paths(row, username: str, *, message_column: bool = True):
    """Workspace paths the caller's attachment chips can be linked at, parallel
    to `_row_attachment_names`, or None when none of them can be.

    Two sources, in order: the paths stored on the canonical `messages` row at
    ingest (which outlive the `tasks` row retention deletes), then — for turns
    predating that column, and for the `tasks` gap-fill rows — a derivation
    from the host paths still on the task.

    Both are re-scoped to **the caller's own** workspace, because a room is
    shared: a co-member sees the chip, but `/chat/files` serves only the
    caller's own files and would refuse the path. Offering it as a link would
    promise a download the endpoint then 403s.
    """
    from .transport.ingest import workspace_attachment_paths

    keys = row.keys()
    if message_column and "attachment_paths" in keys and row["attachment_paths"]:
        try:
            stored = json.loads(row["attachment_paths"])
        except (TypeError, ValueError):
            stored = None
        if isinstance(stored, list) and stored:
            prefix = f"/Users/{username}/"
            scoped = [
                p if isinstance(p, str) and p.startswith(prefix) else None
                for p in stored
            ]
            return scoped if any(scoped) else None
    raw_paths = None
    if not message_column and "attachments" in keys:
        raw_paths = row["attachments"]
    elif "task_attachments" in keys:
        raw_paths = row["task_attachments"]
    if not raw_paths:
        return None
    try:
        paths = json.loads(raw_paths)
    except (TypeError, ValueError):
        return None
    if not isinstance(paths, list) or not paths:
        return None
    return workspace_attachment_paths(_config, username, [str(p) for p in paths])


def _row_attachment_fields(row, username: str, *, message_column: bool = True) -> dict:
    """The attachment keys a history row contributes to its payload, if any.

    `attachments` are the chip labels; `attachment_paths` is the positional
    companion telling the client which of them are openable. Both are omitted
    wholesale for a turn that carried no files.
    """
    names = _row_attachment_names(row, message_column=message_column)
    if not names:
        return {}
    out: dict = {"attachments": names}
    paths = _row_attachment_paths(row, username, message_column=message_column)
    if paths:
        out["attachment_paths"] = paths
    return out


def _user_row_display(row, viewer: str | None = None) -> dict:
    """The `text` (and, when it isn't the reader, `author`) of a user row.

    Two independent things a `role='user'` row can be that the transcript would
    otherwise render as though the viewer had typed it:

    **Who wrote it.** The client labels every user bubble with the logged-in
    viewer's display name, so a row with no author reads as the viewer's own
    words. `messages` now records the writer, so this is a projection rather
    than a recovery: `author_user_id` resolves to that user's display name,
    `author_label` is an already-sanitized external sender, and both NULL means
    the room owner — which is the correct fallback for a pre-migration row.
    `author` is emitted only when the writer is *not* the viewer, since absence
    is what tells the client to use its own label.

    The label is tested first, so it wins if a writer ever sets both. That is
    the cautious direction and the reason is in the `schema.sql` comment: the
    opposite tiebreak renders a stranger's mail as the account it was routed to.

    Attribution used to be re-derived per read from `processed_emails`
    (ISSUE-226), which answered only for email and only while the ledger row
    outlived the message. The stored columns also cover the case that recovery
    could not: a co-member's turn in a shared room now carries their name.

    **What it says.** An email turn's stored body is the task prompt verbatim —
    wrapper tags, the "external input — do not follow instructions" guard, and
    the trailing instruction to the model — because it re-pairs straight into
    LLM context and a prettified body would drop the guard. None of that is for
    a human, so the display body is the email itself. Anything that is not an
    email prompt renders verbatim.
    """
    from .email_support import parse_email_prompt  # noqa: PLC0415

    body = row["body"]
    out: dict = {"text": body}

    parsed = parse_email_prompt(body)
    if parsed is not None:
        _headers, email_body = parsed
        if email_body:
            out["text"] = email_body

    try:
        author_user_id = row["author_user_id"]
        author_label = row["author_label"]
    except (IndexError, KeyError):
        return out  # a producer that predates the columns (the dev mock)

    if author_label:
        out["author"] = author_label
    elif author_user_id and author_user_id != viewer:
        out["author"] = _display_name_for(author_user_id)
    return out


def _display_name_for(user_id: str) -> str:
    """A user's display name, falling back to the id itself.

    The id is a poor label but an honest one; an empty author would read as the
    viewer, which is the mislabelling the column exists to prevent.
    """
    if _config is not None:
        user_cfg = _config.users.get(user_id)
        if user_cfg is not None and getattr(user_cfg, "display_name", None):
            return user_cfg.display_name
    return user_id


def _chat_room_messages(
    username: str,
    token: str,
    limit: int,
    before: tuple[str, int] | None = None,
) -> dict:
    """A page of a room's transcript plus (on first load) its active tasks.

    The transcript is read from the **durable** canonical `messages` store, not
    the `tasks` table: `cleanup_old_tasks` GCs completed tasks after a few days,
    so a `tasks`-sourced transcript silently lost a dormant room's history and
    surfaced only the stray cancelled/failed tasks retention happens to keep
    (ISSUE-126). Surviving `tasks` rows are joined in only to *enrich* a stored
    turn (trace / timing / model) and to *fill* turns the store doesn't hold —
    failed/cancelled answers (the scheduler stores only successful turns), the
    in-flight assistant slot, and any legacy turn not yet backfilled. Dedup is
    keyed on `(role, task_id)`: the store is authoritative, `tasks` fills gaps.

    Paging (ISSUE-131). The `messages` store is the **spine** — it holds every
    successful turn and every system message — so it drives keyset pagination.
    `before` is the `(created_at, id)` of the oldest spine row the client already
    holds (its *raw* stored `created_at`, NOT the `_iso_utc`-normalized display
    value — see the cursor note below). `None` → first load (the most-recent
    window). Each page's spine defines a half-open time band `[page_lo, before)`;
    the aux `tasks` gap-fill and system rows for an older page are filtered to
    that band so the timeline tiles with no gap and no overlap. `active_tasks`
    is returned only on the first load — an older page never carries an in-flight
    slot.

    The aux fill bands on `_AUX_TURN_TS`, not on the `tasks` row's own
    `created_at`: the two are written by separate statements against a
    second-granularity `datetime('now')`, so the task stamp can fall a second
    short of the spine row it belongs to and carry the turn off its own page.
    Coalescing puts every aux band on the key the spine is ordered by. Only the
    *timestamp* is coalesced, not the id tiebreaker, so a turn tying the page
    floor second but sorting below it by id can still split across two pages —
    the pre-existing limit of a timestamp-only band. On an aux-only page
    (`msg_rows` empty) the cursor's `ts` is therefore a `turn_ts` paired with a
    `tasks.id`; both the notes band and the `has_more` probe read the same value,
    or the sliver between the two keys renders on both pages.

    ISSUE-130: the window is ordered `created_at DESC, id DESC` (not `id DESC`),
    so a backfilled room whose `id` order inverts `created_at` order keeps its
    most-recent-by-time turns instead of admitting stale-but-high-id rows.

    Cursor format (load-bearing): `oldest_cursor.ts` is the **raw** stored
    `created_at` (`YYYY-MM-DD HH:MM:SS`), kept separate from the `_iso_utc`
    display value (`…T…Z`). The two are not byte-comparable — `'T' > ' '` — so a
    cursor shipped in display format sorts as *newer* than its own row and the
    keyset predicate re-returns page 1 forever. The client passes `ts` back
    verbatim as `before_ts`.
    """
    from . import db
    with db.get_db(_config.db_path) as conn:
        # 1. Spine: durable turns, keyset-paginated. limit*2 because a turn is
        #    two rows (user + assistant); scheduled posts contribute one, so this
        #    over-fetches a little for scheduled-heavy rooms, which is harmless.
        if before is None:
            msg_rows = conn.execute(
                _SPINE_COLUMNS + f"WHERE m.room_token = ? AND {_SPINE_SURFACE} "
                "ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
                (username, token, limit * 2),
            ).fetchall()
        else:
            before_ts, before_id = before
            msg_rows = conn.execute(
                _SPINE_COLUMNS + f"WHERE m.room_token = ? AND {_SPINE_SURFACE} "
                "AND (m.created_at, m.id) < (?, ?) "
                "ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
                (username, token, before_ts, before_id, limit * 2),
            ).fetchall()

        # 2. Aux gap-fill (failed/cancelled answers, in-flight slots, legacy
        #    turns). The read shape depends on the page:
        if before is None:
            if msg_rows:
                # First load with a spine: failed/cancelled banded to >= the
                # page's oldest spine `created_at` (so a failed turn at the
                # window boundary isn't dropped — flaw #2), plus active/in-flight
                # slots unconditionally (they're the newest and must always show).
                # Completed turns are NOT read here — they live wholly in the
                # spine; pulling them by `created_at >= t1` would re-render a
                # completed turn whose spine rows the LIMIT cut at the t1 second.
                t1 = msg_rows[-1]["created_at"]
                task_rows = conn.execute(
                    _AUX_COLUMNS
                    + "WHERE conversation_token = ? AND user_id = ? "
                    "AND " + _AUX_SOURCE_SCOPE + " "
                    "AND (status IN ('pending', 'locked', 'running', 'pending_confirmation') "
                    "     OR (status IN ('failed', 'cancelled') "
                    f"         AND {_AUX_TS_ABOVE} AND {_AUX_TURN_TS} >= ?)) "
                    "ORDER BY turn_ts DESC, id DESC",
                    (token, username, t1, t1),
                ).fetchall()
            else:
                # Empty-spine fallback (un-backfilled legacy / failed-only room):
                # no spine to page, so keep today's behavior exactly — the most
                # recent tasks window, no cursor offered.
                task_rows = conn.execute(
                    _AUX_COLUMNS
                    + "WHERE conversation_token = ? AND user_id = ? "
                    "AND " + _AUX_SOURCE_SCOPE + " "
                    "ORDER BY turn_ts DESC, id DESC LIMIT ?",
                    (token, username, limit),
                ).fetchall()
        else:
            before_ts, before_id = before
            if msg_rows:
                # Older page with a spine: failed/cancelled tasks banded to the
                # page's [page_lo, before) window. Failed/cancelled only — a
                # completed turn lives wholly in the spine, so reading it here
                # would re-render a turn whose spine rows the LIMIT split across
                # this page's boundary. An older page never carries an in-flight
                # slot.
                page_lo = msg_rows[-1]["created_at"]
                task_rows = conn.execute(
                    _AUX_COLUMNS
                    + "WHERE conversation_token = ? AND user_id = ? "
                    "AND " + _AUX_SOURCE_SCOPE + " "
                    "AND status IN ('failed', 'cancelled') "
                    f"AND {_AUX_TS_ABOVE} AND {_AUX_TS_BELOW} "
                    f"AND {_AUX_TURN_TS} >= ? AND {_AUX_TURN_TS} < ? "
                    "ORDER BY turn_ts DESC, id DESC",
                    (token, username, page_lo, before_ts, page_lo, before_ts),
                ).fetchall()
            else:
                # Aux-only tail (flaw #3): the spine is exhausted but failed/
                # cancelled tasks older than the cursor remain (spineless, legacy
                # rows). Page them directly by keyset so none are stranded.
                task_rows = conn.execute(
                    _AUX_COLUMNS
                    + "WHERE conversation_token = ? AND user_id = ? "
                    "AND " + _AUX_SOURCE_SCOPE + " "
                    "AND status IN ('failed', 'cancelled') "
                    f"AND {_AUX_TS_BELOW} AND ({_AUX_TURN_TS}, id) < (?, ?) "
                    "ORDER BY turn_ts DESC, id DESC LIMIT ?",
                    (token, username, before_ts, before_ts, before_id, limit),
                ).fetchall()

        # 3. Bot-delivered system messages (alerts / logs / notifications routed
        #    to web) live in the canonical store (role='system'). First load uses
        #    the existing most-recent window; an older page bands them into
        #    [page_lo, before) so they ride along with their turns.
        if before is None:
            notes = db.list_system_messages(conn, token, limit)
        else:
            # `turn_ts` on the aux fallback, matching the cursor this page hands
            # back: band the notes on a *lower* floor than the cursor and the
            # sliver between the two is read on both pages, duplicating any
            # system row in it (nothing dedups them — `seen` is keyed on
            # (role, task_id) and a system row has no task_id).
            page_lo = (
                msg_rows[-1]["created_at"] if msg_rows
                else (task_rows[-1]["turn_ts"] if task_rows else before[0])
            )
            notes = db.list_system_messages_in_band(
                conn, token, lo_ts=page_lo, hi_ts=before[0],
            )
        # Star flags for the system rows (the spine rows carry theirs via the
        # message_stars join; notes come from a separate read).
        note_star_ids = db.get_starred_message_ids(
            conn, username, [n.id for n in notes],
        )

        # 4. Paging metadata: the page's oldest spine (or aux-only) row gives the
        #    next cursor; `has_more` ORs a spine probe with a band-eligible
        #    failed/cancelled aux probe (flaw #3 — an aux-only tail must keep
        #    has_more true until it's paged through).
        has_more = False
        oldest_cursor: dict | None = None
        if msg_rows:
            page_lo_ts = msg_rows[-1]["created_at"]
            page_lo_id = msg_rows[-1]["msg_id"]
            oldest_cursor = {"ts": page_lo_ts, "id": page_lo_id}
            spine_more = conn.execute(
                f"SELECT 1 FROM messages m WHERE m.room_token = ? AND {_SPINE_SURFACE} "
                "AND (m.created_at, m.id) < (?, ?) LIMIT 1",
                (token, page_lo_ts, page_lo_id),
            ).fetchone() is not None
            aux_more = conn.execute(
                "SELECT 1 FROM tasks WHERE conversation_token = ? AND user_id = ? "
                "AND " + _AUX_SOURCE_SCOPE + " AND status IN ('failed', 'cancelled') "
                f"AND {_AUX_TS_BELOW} AND {_AUX_TURN_TS} < ? LIMIT 1",
                (token, username, page_lo_ts, page_lo_ts),
            ).fetchone() is not None
            has_more = spine_more or aux_more
        elif before is not None and task_rows:
            # `turn_ts`, not `created_at` — the band this page was read with, so
            # the cursor it hands back can't skip or re-return a row.
            page_lo_ts = task_rows[-1]["turn_ts"]
            page_lo_id = task_rows[-1]["id"]
            oldest_cursor = {"ts": page_lo_ts, "id": page_lo_id}
            has_more = conn.execute(
                "SELECT 1 FROM tasks WHERE conversation_token = ? AND user_id = ? "
                "AND " + _AUX_SOURCE_SCOPE + " AND status IN ('failed', 'cancelled') "
                f"AND {_AUX_TS_BELOW} AND ({_AUX_TURN_TS}, id) < (?, ?) LIMIT 1",
                (token, username, page_lo_ts, page_lo_ts, page_lo_id),
            ).fetchone() is not None

    messages: list[dict] = []
    seen: set[tuple[str, object]] = set()  # (role, task_id) already rendered

    # 1. Durable store turns (authoritative).
    for r in reversed(msg_rows):  # oldest-first
        tid = r["task_id"]
        if r["role"] == "user":
            d = {
                "role": "user", "task_id": tid,
                "created_at": r["created_at"],
                "msg_id": r["msg_id"], "starred": bool(r["starred"]),
                **_user_row_display(r, username),
            }
            d.update(_row_attachment_fields(r, username))
        else:  # assistant — a stored assistant row is by definition a completed turn
            d = _assistant_message_dict(r, r["body"], r["status"] or "completed")
        cited = _row_reply_to(r)
        if cited is not None:
            d["reply_to"] = cited
        messages.append(d)
        if tid is not None:
            seen.add((r["role"], tid))

    # 2. Tasks fill the gaps, oldest-first. The room runs tasks one at a time
    #    (the per-channel claim gate serializes them), so in-flight ones stream
    #    in this order. `active_task` is kept as the oldest for back-compat. An
    #    older page (before set) carries terminal statuses only, so active_tasks
    #    stays empty there.
    active_tasks: list[dict] = []
    for r in reversed(task_rows):
        tid = r["id"]
        if ("user", tid) not in seen:
            d = {
                "role": "user", "text": r["prompt"], "task_id": tid,
                "created_at": _turn_created_at(r),
            }
            d.update(_row_attachment_fields(r, username, message_column=False))
            messages.append(d)
            seen.add(("user", tid))
        status = r["status"]
        if ("assistant", tid) in seen:
            continue  # the store already rendered (and enriched) this answer
        if status == "completed":
            messages.append(_assistant_message_dict(r, r["result"] or "", status))
        elif status == "pending_confirmation":
            messages.append(_assistant_message_dict(
                r, r["confirmation_prompt"] or r["result"] or "", status, confirmation=True,
            ))
            active_tasks.append({"id": tid, "status": status})
        elif status in ("failed", "cancelled"):
            messages.append(_assistant_message_dict(r, r["result"] or r["error"] or "", status))
        else:  # pending / locked / running — placeholder slot to stream into
            messages.append({
                "role": "assistant", "text": "", "task_id": tid,
                "status": status, "created_at": _turn_created_at(r),
            })
            active_tasks.append({"id": tid, "status": status})

    # Merge bot-delivered messages in by time. `notif_id` gives the client a
    # stable key so an idle poll appends only ones that arrived later.
    for n in notes:
        text = f"**{n.title}**\n\n{n.body}" if n.title else n.body
        messages.append({
            "role": n.role, "text": text, "notif_id": n.id,
            "created_at": n.created_at,
            # `notif_id` kept for back-compat; `msg_id` is the uniform star key.
            "msg_id": n.id, "starred": n.id in note_star_ids,
        })
    # Normalize every turn's created_at to explicit ISO 8601 UTC. The stored
    # values are naive UTC (SQLite datetime('now') / strftime, and the Talk-cache
    # backfill), which the browser's new Date() parses as *local* time — the
    # imported-from-Talk turns then render hours ahead. Doing it before the sort
    # also keeps the sort key in one uniform format.
    for m in messages:
        m["created_at"] = _iso_utc(m.get("created_at"))
    # Order chronologically, but break created_at ties by (task_id, role) so a
    # turn's user→assistant pair stays adjacent even when several rapid in-flight
    # sends share a timestamp (the store and tasks contribute the two halves
    # separately now). Notes (no task_id) sort after task turns at equal time.
    _role_rank = {"user": 0, "assistant": 1, "system": 2}
    messages.sort(key=lambda m: (
        m.get("created_at") or "",
        m.get("task_id") if m.get("task_id") is not None else float("inf"),
        _role_rank.get(m["role"], 3),
    ))
    return {
        "messages": messages,
        # Active tasks resume only on the first load. An older page never carries
        # an in-flight slot (it reads terminal statuses only), so the client must
        # not re-resume anything from it.
        "active_task": active_tasks[0] if (before is None and active_tasks) else None,
        "active_tasks": active_tasks if before is None else [],
        # Paging metadata: older history exists, and the cursor to fetch it (raw
        # stored created_at + id — NOT the normalized display value).
        "has_more": has_more,
        "oldest_cursor": oldest_cursor,
    }


def _chat_upload_roots(username: str) -> list[Path]:
    """Directories a web-chat upload for this user may legitimately live under
    (mount inbox + temp fallback). Both are listed regardless of mount config so
    a path saved under either still validates."""
    return [
        _config.nextcloud_mount_path / "Users" / username / "inbox" / "web-chat",
        _config.temp_dir / username / "web-chat-uploads",
    ] if _config.nextcloud_mount_path else [
        _config.temp_dir / username / "web-chat-uploads",
    ]


def _validate_chat_attachments(username: str, paths: list) -> list[str] | None:
    """Keep only attachment paths that resolve inside the user's web-chat upload
    roots. Returns the cleaned list, or ``None`` if any path is foreign — a
    client must not point the brain at arbitrary host paths or escape via
    symlink / ``..``. ``realpath`` collapses both."""
    if not paths:
        return []
    roots = [os.path.realpath(r) for r in _chat_upload_roots(username)]
    out: list[str] = []
    for p in paths:
        if not isinstance(p, str) or not p:
            return None
        real = os.path.realpath(p)
        if not any(real == r or real.startswith(r + os.sep) for r in roots):
            return None
        out.append(p)
    return out


def _describe_attachment_only_message(attachments: list[str]) -> str:
    """Stand-in prompt for a send that carried attachments but no typed text.

    Voice memos are the motivating case: the recording is the message. The
    descriptor names what arrived so the turn is legible everywhere the raw
    prompt is read (transcript, conversation context, the Talk mirror repost),
    and it keeps the prompt useful when transcription is unavailable — the
    model still sees "there is audio here" plus the attachment path, and can
    reach for the whisper skill itself.
    """
    from .executor import _AUDIO_EXTENSIONS

    names = [os.path.basename(p) for p in attachments]
    audio = [
        n for n in names
        if os.path.splitext(n)[1].lstrip(".").lower() in _AUDIO_EXTENSIONS
    ]
    if audio and len(audio) == len(names):
        label = "Voice message" if len(audio) == 1 else "Voice messages"
        return f"{label} (see attached audio)."
    joined = ", ".join(names)
    return f"(Sent without a message — see attached: {joined})"


def _is_own_replay(conn, token: str, client_msg_id: str, username: str) -> bool:
    """Whether this key already names a turn *this* sender created in this room.

    The same test `record_inbound` applies before it replays, asked one step
    earlier so the citation check can stand down for a retry. Scoped to the
    sender for the same reason the replay is: a co-member's reused key resolves
    to a task this caller is not authorized to read, and that send gives the
    key up rather than claiming it.
    """
    from . import db

    prior = db.find_send_by_client_msg_id(conn, token, client_msg_id)
    return prior is not None and prior[1] == username


def _chat_create_web_task(
    username: str, token: str, text: str,
    attachments: list[str] | None = None,
    model: str | None = None,
    effort: str | None = None,
    apply_room_default: bool = True,
    attachment_names: list[str] | None = None,
    client_msg_id: str | None = None,
    reply_to_msg_id: int | None = None,
) -> tuple[str, int]:
    """Rate-limited web-task creation. Returns ``("ok", task_id)``,
    ``("rate_limited", window_seconds)`` or ``("reply_target_gone", 0)``.

    A cited parent is resolved here, inside the same transaction as the create:
    it must be a message in *this* room, and its body — never a client-supplied
    quote — is what gets snapshotted onto the task.
    """
    from . import db
    from .transport import record_inbound
    chat = _config.web.chat
    with db.get_db(_config.db_path) as conn:
        # Take the write lock up front so the count and the insert are one
        # critical section — a plain SELECT takes no lock under WAL, so two
        # concurrent sends could both read under-limit and both insert,
        # overshooting the cap (TOCTOU). BEGIN IMMEDIATE serializes them.
        conn.execute("BEGIN IMMEDIATE")
        recent = db.count_recent_web_tasks(conn, username, chat.rate_limit_window_seconds)
        if recent >= chat.rate_limit_messages:
            return ("rate_limited", chat.rate_limit_window_seconds)
        # A retry of a send we already accepted replays that turn, and the
        # citation question does not arise for it: the parent was valid when
        # the turn was created, and the turn exists. Checked *before* the
        # citation, because a parent deleted in the meantime would otherwise
        # refuse the replay — the client would then be told its message is
        # gone, hand the text back, and the user's natural re-send (a fresh
        # key) would create a second task for a message the server already has.
        # `record_inbound` does the replay itself; this only decides whether to
        # ask about the citation at all.
        replaying = bool(client_msg_id) and _is_own_replay(
            conn, token, client_msg_id, username,
        )
        # Resolve the citation before anything is created. A reply body
        # routinely depends on its referent, so ingesting the message with the
        # citation quietly dropped would deliver a message the user did not
        # write — a visible refusal is the better failure. The caller has
        # already proved membership of this room, so a mismatch is a deleted
        # parent or a client bug, and both answer the same way.
        reply_to_content: str | None = None
        if reply_to_msg_id is not None and not replaying:
            target = db.get_reply_target(conn, reply_to_msg_id)
            if target is None or target[0] != token:
                return ("reply_target_gone", 0)
            reply_to_content = target[1][:_REPLY_SNAPSHOT_CHARS]
        # Sending a new message in a room means the user has moved on from any
        # question parked in it — the rule the Talk poller has always applied
        # (`transport/talk/inbound.py`, before its own `ingest_message`). Web
        # never did, and a `pending_confirmation` task holds its room under
        # `_CLAIM_CHANNEL_GATE_SQL`, so an unanswered gate froze the room until
        # `confirmation_timeout_minutes` (ISSUE-241, via ISSUE-227).
        #
        # Two things make it safe. It is **room-scoped**, so an email gate
        # parked under its own synthetic thread token is untouched — only a
        # question the user can actually see. And it is skipped on a **replay**:
        # a retry returns the prior task without creating anything, so
        # cancelling here would discard the confirmation that very send
        # produced, which is the durability path this key exists to serve.
        # Ordered before `record_inbound` for the same reason Talk orders it
        # that way — the new task must not be a candidate for its own cancel.
        if not replaying:
            cancelled = db.cancel_pending_confirmations(conn, token, username)
            if cancelled:
                logger.info(
                    "Cancelled %d pending confirmation(s) in %s for %s (new message)",
                    cancelled, token, username,
                )
        # Route through the shared inbound helper so the web user turn lands in
        # the canonical `messages` store (and the room is registered) exactly
        # like Talk — instead of living only in tasks.prompt.
        # output_target="room" fans out by the room's live bindings: the web
        # origin (streamed over SSE) plus a push mirror to a bound Talk room, if
        # any. For a web-only room it resolves to just the web stream (same as
        # the old "web").
        _room_token, task_id = record_inbound(
            conn, _config, surface="web", surface_ref=token, user_id=username,
            text=text, source_type="web", output_target="room", priority=5,
            attachments=attachments or None, model=model, effort=effort,
            apply_room_default=apply_room_default,
            attachment_names=attachment_names or None,
            client_msg_id=client_msg_id,
            reply_to_canonical_id=reply_to_msg_id,
            reply_to_content=reply_to_content,
        )
    return ("ok", task_id)


# Fire-and-forget background tasks (web→Talk read pushes). Held in a set so
# they aren't garbage-collected mid-flight; every coroutine passed in catches
# its own exceptions, so a failure can only ever log.
_bg_tasks: set = set()


def _fire_and_forget(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _room_talk_ref(room_token: str) -> str | None:
    """The Talk surface_ref bound to a room, or None (web-only room)."""
    from . import db
    with db.get_db(_config.db_path) as conn:
        bindings = db.list_room_bindings(conn, room_token)
    return next((b.surface_ref for b in bindings if b.surface == "talk"), None)


async def _push_read_to_talk(username: str, room_token: str) -> None:
    """Web→Talk read sync: mark the bound Talk conversation read as the user.
    Called only when the web cursor actually advanced. Best-effort — the one
    user-visible artifact of failure is a badge that doesn't auto-clear."""
    try:
        from . import web_tokens

        if not _config or not web_tokens.feature_enabled(_config):
            return
        talk_ref = await asyncio.to_thread(_room_talk_ref, room_token)
        if not talk_ref:
            return
        access = await asyncio.to_thread(
            web_tokens.get_access_token, _config.db_path, _config, username,
        )
        if not access:
            return
        from .talk import TalkClient

        client = TalkClient(_config, bearer_token=access, timeout=5)
        try:
            # mark_conversation_read logs + returns False on failure.
            await client.mark_conversation_read(talk_ref)
        finally:
            await client.aclose()
    except Exception as e:  # noqa: BLE001 — never propagate into the request
        logger.warning(
            "read push to Talk failed user=%s room=%s: %s",
            username, room_token, e,
        )


# Talk→web read-state pull throttle: user -> monotonic timestamp of the last
# pull. Process-local by design (a web restart just pulls once immediately).
_talk_read_pull_state: dict[str, float] = {}


async def _pull_talk_read_state(username: str) -> None:
    """Talk→web read sync, piggybacked on the web rooms poll (at most one NC
    conversation-list fetch per user per `talk_read_sync_interval`).

    For each web-visible room bound to a Talk conversation the user has fully
    read (`unreadMessages == 0` under their own bearer token), advance their
    web cursor — but only up to the newest canonical message that actually
    exists in Talk (`room_max_talk_synced_message_id`), so Talk read-state
    can't swallow web-only system messages the user never saw there."""
    try:
        from . import db, web_tokens

        if not _config or not web_tokens.feature_enabled(_config):
            return
        interval = _config.web.chat.talk_read_sync_interval
        if interval <= 0:
            return
        now = time.monotonic()
        last = _talk_read_pull_state.get(username)
        if last is not None and now - last < interval:
            return
        # Stamp before the fetch so a failing NC isn't hammered every poll.
        _talk_read_pull_state[username] = now

        access = await asyncio.to_thread(
            web_tokens.get_access_token, _config.db_path, _config, username,
        )
        if not access:
            return
        from .talk import TalkClient

        client = TalkClient(_config, bearer_token=access, timeout=5)
        try:
            conversations = await client.list_conversations()
        finally:
            await client.aclose()
        fully_read = {
            c.get("token")
            for c in conversations
            if c.get("token") and c.get("unreadMessages") == 0
        }
        if not fully_read:
            return

        def _advance() -> int:
            advanced = 0
            with db.get_db(_config.db_path) as conn:
                for room in db.list_member_rooms(conn, username):
                    bindings = db.list_room_bindings(conn, room.token)
                    talk_ref = next(
                        (b.surface_ref for b in bindings if b.surface == "talk"),
                        None,
                    )
                    if not talk_ref or talk_ref not in fully_read:
                        continue
                    cap = db.room_max_talk_synced_message_id(conn, room.token)
                    if cap <= 0:
                        continue  # nothing Talk-synced yet (pre-deploy rows)
                    current = db.get_room_read_state(
                        conn, room.token, "web", username,
                    )
                    if cap > current:
                        db.set_room_read_state(
                            conn, room.token, "web", cap, username,
                        )
                        advanced += 1
            return advanced

        advanced = await asyncio.to_thread(_advance)
        if advanced:
            logger.debug(
                "talk read pull user=%s advanced %d room cursor(s)",
                username, advanced,
            )
    except Exception as e:  # noqa: BLE001 — the rooms poll must never fail
        logger.warning("talk read pull failed user=%s: %s", username, e)


async def _post_as_user(
    access: str, talk_ref: str, text: str, message_id: int, username: str,
    reply_to_talk_id: int | None = None,
) -> int | None:
    """One post-as-user attempt against the bound Talk room, with a single
    forced-refresh retry on 401 (clock skew / early revocation on a
    supposedly-live token). Returns the posted Talk message id, or None.

    `reply_to_talk_id` makes the mirrored turn a real Talk reply. It is None
    whenever the cited parent was never mirrored into Talk — a web-only turn,
    or one predating the mirror — and the post then degrades to a plain one
    rather than being withheld: the message still belongs in the room.
    """
    from . import web_tokens
    from .talk import TalkClient
    from .transport import WEBMIRROR_REF_PREFIX

    reference_id = f"{WEBMIRROR_REF_PREFIX}{message_id}"
    for attempt in (0, 1):
        client = TalkClient(_config, bearer_token=access, timeout=5)
        try:
            resp = await client.send_message(
                talk_ref, text, reply_to=reply_to_talk_id,
                reference_id=reference_id,
            )
            posted = resp.get("ocs", {}).get("data", {}).get("id")
            return int(posted) if posted else None
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else 0
            if attempt == 0 and status == 401:
                access = await asyncio.to_thread(
                    lambda: web_tokens.get_access_token(
                        _config.db_path, _config, username, force_refresh=True,
                    ),
                )
                if access:
                    continue
            logger.warning(
                "post-as-user Talk post failed user=%s room=%s status=%s",
                username, talk_ref, status,
            )
            return None
        except Exception as e:
            logger.warning(
                "post-as-user Talk post failed user=%s room=%s: %s",
                username, talk_ref, e,
            )
            return None
        finally:
            await client.aclose()
    return None


async def _mirror_web_turn_as_user(
    username: str, room_token: str, text: str, task_id: int,
    reply_to_msg_id: int | None = None,
) -> None:
    """Post a just-ingested web user turn into the room's bound Talk
    conversation *as the user*, at send time — so the message appears in Talk
    instantly, authored by the user, instead of as the bot's attributed repost
    at task completion.

    On success the posted Talk message id is stamped onto the canonical user
    row (`set_message_external_id`): that stamp is both the echo-check ledger
    entry and the scheduler's repost-suppression signal. On any failure —
    feature off, no binding, no live token, post error — nothing is stamped
    and the scheduler's legacy attributed repost covers the mirror leg exactly
    as before. Never raises into the request path.
    """
    from . import db, web_tokens

    if not _config or not web_tokens.feature_enabled(_config):
        return

    def _lookup() -> tuple[str | None, int | None, int | None]:
        with db.get_db(_config.db_path) as conn:
            bindings = db.list_room_bindings(conn, room_token)
            talk_ref = next(
                (b.surface_ref for b in bindings if b.surface == "talk"), None,
            )
            if talk_ref is None:
                return None, None, None
            # A retry carrying the same `client_msg_id` resolves to the turn
            # the first attempt created, and that attempt already mirrored it.
            # The stamp is the record of that, so it is also the guard: without
            # it a client-side timeout would put the message in Talk twice.
            if db.user_turn_has_external_id(conn, task_id, "talk"):
                return None, None, None
            row = conn.execute(
                "SELECT id FROM messages WHERE room_token = ? AND task_id = ? "
                "AND role = 'user' LIMIT 1",
                (room_token, task_id),
            ).fetchone()
            # The cited parent's own Talk id, when it has one. A parent that
            # never reached Talk (web-only, or predating the mirror) leaves
            # this None and the post degrades to a plain one.
            parent_talk_id = None
            if reply_to_msg_id is not None:
                raw = db.get_message_external_id(conn, reply_to_msg_id, "talk")
                if raw is not None:
                    try:
                        parent_talk_id = int(raw)
                    except ValueError:
                        parent_talk_id = None
            return talk_ref, (int(row["id"]) if row else None), parent_talk_id

    try:
        talk_ref, message_id, parent_talk_id = await asyncio.to_thread(_lookup)
        if not talk_ref or message_id is None:
            return  # web-only room (or turn not stored) — nothing to mirror

        access = await asyncio.to_thread(
            web_tokens.get_access_token, _config.db_path, _config, username,
        )
        if not access:
            logger.debug(
                "post-as-user skipped user=%s room=%s (no live token)",
                username, room_token,
            )
            return

        posted_id = await _post_as_user(
            access, talk_ref, text, message_id, username,
            reply_to_talk_id=parent_talk_id,
        )
        if posted_id is None:
            return

        def _stamp():
            with db.get_db(_config.db_path) as conn:
                db.set_message_external_id(
                    conn, message_id, "talk", str(posted_id),
                )

        await asyncio.to_thread(_stamp)
    except Exception as e:  # noqa: BLE001 — never fail the send request
        logger.warning(
            "post-as-user mirror failed user=%s room=%s: %s",
            username, room_token, e,
        )


@api_router.get("/chat/config")
async def chat_config(user: dict = Depends(_require_api_auth)):
    """Client-facing chat knobs."""
    chat = _config.web.chat
    return {
        "max_prompt_chars": chat.max_prompt_chars,
        "max_attachment_mb": chat.max_attachment_mb,
        "attachment_extensions": chat.attachment_extensions,
        "client_poll_interval_ms": chat.client_poll_interval_ms,
    }


@api_router.get("/chat/commands")
async def chat_commands(user: dict = Depends(_require_api_auth)):
    """Command + model-alias catalogue that powers the composer autocomplete.

    Derived at request time from the in-memory command registry and the active
    brain's alias table — no storage. Model aliases degrade to an empty list if
    the brain can't resolve them, so the primary (command) feature still works.
    """
    from . import commands

    cmds = [
        {"name": name, "help": help_text}
        for name, (_handler, help_text) in sorted(commands.COMMANDS.items())
    ]
    aliases: list[dict] = []
    try:
        aliases = [
            {"alias": alias, "target": model, "effort": effort}
            for alias, model, effort in make_brain(_config.brain).list_aliases()
        ]
    except Exception as e:  # noqa: BLE001 — aliases degrade independently
        logger.warning("chat_commands: model aliases unavailable: %s", e)
    return {"commands": cmds, "model_aliases": aliases}


@api_router.get("/chat/rooms")
async def chat_list_rooms(user: dict = Depends(_require_api_auth)):
    # Talk→web read sync rides the rooms poll (throttled server-side), and
    # runs BEFORE the listing so freshly-cleared badges show in this payload.
    await _pull_talk_read_state(user["username"])
    rooms = await asyncio.to_thread(_chat_list_rooms, user["username"])
    return {"rooms": rooms}


@api_router.post("/chat/rooms")
async def chat_create_room(
    request: Request,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    if len(name) > 80:
        name = name[:80]
    room = await asyncio.to_thread(_chat_create_room, user["username"], name)
    return room


@api_router.patch("/chat/rooms/{room_id}")
async def chat_update_room(
    room_id: int,
    request: Request,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    data = await request.json()
    name = data.get("name")
    archived = data.get("archived")
    if name is not None:
        name = str(name).strip()[:80] or None
    if archived is not None:
        archived = bool(archived)
    # Per-room model/effort default (canonical model id + effort level). A key's
    # presence signals intent: absent → leave untouched, "" / null → clear.
    model = _UNSET
    if "model" in data:
        model = str(data["model"] or "").strip() or None
        if model is not None and model not in _known_room_models():
            return JSONResponse({"error": "unknown model"}, status_code=400)
    effort = _UNSET
    if "effort" in data:
        from .commands import _EFFORT_LEVELS
        effort = str(data["effort"] or "").strip().lower() or None
        if effort is not None and effort not in _EFFORT_LEVELS:
            return JSONResponse({"error": "invalid effort"}, status_code=400)
    updated = await asyncio.to_thread(
        _chat_update_room, user["username"], room_id, name, archived, model, effort,
    )
    if updated is None:
        return JSONResponse({"error": "room not found"}, status_code=404)
    # Propagate a rename to the bound Talk conversation, if any (best-effort).
    if name is not None and _config.nextcloud.url:
        talk_token = await asyncio.to_thread(
            _room_talk_binding, user["username"], room_id,
        )
        if talk_token:
            from .talk import TalkClient
            client = TalkClient(_config)
            try:
                await client.rename_conversation(talk_token, updated["name"])
            except Exception as e:  # best-effort; web rename already persisted
                logger.warning("rename propagate to Talk failed: %s", e)
            finally:
                await client.aclose()
    return updated


@api_router.post("/chat/rooms/{room_id}/promote")
async def chat_promote_room(
    room_id: int,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    """Create a real Nextcloud Talk conversation for a web-origin room and bind
    it, so the conversation is reachable from the Talk mobile clients too."""
    result = await _chat_promote_to_talk(user["username"], room_id)
    if result is None:
        return JSONResponse(
            {"error": "room not found or not eligible for promotion"},
            status_code=404,
        )
    return result


@api_router.delete("/chat/rooms/{room_id}")
async def chat_delete_room(
    room_id: int,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    result = await asyncio.to_thread(
        _chat_delete_room, user["username"], room_id,
    )
    if result == "not_found":
        return JSONResponse({"error": "room not found"}, status_code=404)
    if result == "busy":
        return JSONResponse(
            {"error": "room has a task in progress"}, status_code=409,
        )
    return {"status": "ok"}


@api_router.get("/chat/rooms/{room_id}/messages")
async def chat_room_messages(
    room_id: int,
    limit: int = 50,
    before_ts: str | None = None,
    before_id: int | None = None,
    user: dict = Depends(_require_api_auth),
):
    room = await asyncio.to_thread(_chat_owned_room, user["username"], room_id)
    if room is None:
        return JSONResponse({"error": "room not found"}, status_code=404)
    # The keyset cursor is two params that must travel together: both present →
    # an older page, both absent → first load. One without the other is a client
    # bug, not a half-cursor we can guess at.
    if (before_ts is None) != (before_id is None):
        return JSONResponse(
            {"error": "before_ts and before_id must be supplied together"},
            status_code=400,
        )
    before = (before_ts, before_id) if before_ts is not None else None
    limit = max(1, min(limit, 200))
    return await asyncio.to_thread(
        _chat_room_messages, user["username"], room.token, limit, before,
    )


@api_router.post("/chat/rooms/{room_id}/read")
async def chat_mark_room_read(
    room_id: int,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    """Mark a room read on the web surface — advances the per-user web read
    cursor to the room's newest message so the sidebar unread badge clears.
    When the cursor actually advanced, the read state is also pushed to a
    bound Talk conversation as the user (fire-and-forget, feature-gated)."""
    result = await asyncio.to_thread(
        _chat_mark_room_read, user["username"], room_id,
    )
    if result is None:
        return JSONResponse({"error": "room not found"}, status_code=404)
    if result["advanced"]:
        _fire_and_forget(
            _push_read_to_talk(user["username"], result["room_token"]),
        )
    return {"ok": True, "last_read_message_id": result["cursor"]}


def _chat_set_message_star(username: str, message_id: int, starred: bool) -> bool:
    """Star/unstar a message for ``username``. Returns False (→ 404) when the
    message doesn't exist or the user isn't a member of its room — the two are
    deliberately indistinguishable so the endpoint can't be used to probe which
    message ids exist in foreign rooms."""
    from . import db
    with db.get_db(_config.db_path) as conn:
        token = db.get_message_room(conn, message_id)
        if token is None or not db.is_room_member(conn, token, username):
            return False
        db.set_message_starred(conn, message_id, username, starred)
    logger.debug(
        "message star user=%s msg_id=%s starred=%s", username, message_id, starred,
    )
    return True


_ACTIVE_TASK_STATUSES = ("pending", "locked", "running", "pending_confirmation")


def _chat_delete_message(username: str, message_id: int) -> str | dict:
    """Hard-delete one transcript row for ``username``.

    Returns ``"not_found"`` (unknown id, or the caller isn't a member of its
    room — deliberately indistinguishable, same as the star endpoint, so the
    route can't be used to probe foreign message ids), ``"busy"`` when the
    turn's task is still in flight, or a dict describing what to propagate to
    Talk.

    The busy guard mirrors the room delete's: a running turn's assistant row is
    still being written, and deleting it would have the scheduler recreate it
    at completion — the delete would silently undo itself.

    The Talk mirror ids are read *here*, inside the same transaction, because
    after the delete the `external_ids` ledger no longer exists.
    """
    from . import db
    with db.get_db(_config.db_path) as conn:
        token = db.get_message_room(conn, message_id)
        if token is None or not db.is_room_member(conn, token, username):
            return "not_found"
        row = conn.execute(
            "SELECT task_id FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        task_id = row["task_id"] if row else None
        if task_id is not None:
            task = db.get_task(conn, int(task_id))
            if task is not None and task.status in _ACTIVE_TASK_STATUSES:
                return "busy"
        talk_message_id = db.get_message_external_ids(conn, message_id).get("talk")
        talk_ref = next(
            (b.surface_ref for b in db.list_room_bindings(conn, token)
             if b.surface == "talk"),
            None,
        )
        db.delete_message(conn, message_id, username)
    logger.info(
        "message delete user=%s msg_id=%s room=%s", username, message_id, token,
    )
    return {
        "room_token": token,
        "talk_ref": talk_ref,
        "talk_message_id": talk_message_id,
    }


async def _delete_from_talk(
    username: str, talk_ref: str, talk_message_id: str,
) -> None:
    """Best-effort removal of a deleted message's Talk counterpart.

    Which credential can do this depends on who Talk thinks wrote the message:
    a user turn mirrored by post-as-user is authored by the *user*, an
    assistant reply by the bot, and Talk lets only the author (or a moderator)
    delete. So both are tried — the user's own OAuth token first when the
    feature is on and a live token exists, then the bot account. A `403` from
    one is the ordinary "not yours" answer, not an error, which is why the
    fallback exists rather than a single hardcoded credential.

    Never raises into the request path: the web-side delete has already
    committed, and a Talk that is down or a message past Talk's own deletion
    window must not turn a successful delete into a failed one. The divergence
    it can leave (gone in web, still in Talk) is bounded and visible.
    """
    from .talk import TalkClient

    try:
        msg_id = int(talk_message_id)
    except (TypeError, ValueError):
        return

    async def _attempt(client: "TalkClient", who: str) -> bool:
        try:
            await client.delete_message(talk_ref, msg_id)
            logger.debug(
                "talk delete ok as=%s room=%s msg=%s", who, talk_ref, msg_id,
            )
            return True
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else 0
            logger.debug(
                "talk delete refused as=%s room=%s msg=%s status=%s",
                who, talk_ref, msg_id, status,
            )
            return False
        except Exception as e:  # noqa: BLE001
            logger.debug("talk delete failed as=%s: %s", who, e)
            return False

    from . import web_tokens

    if _config and web_tokens.feature_enabled(_config):
        access = await asyncio.to_thread(
            web_tokens.get_access_token, _config.db_path, _config, username,
        )
        if access and await _attempt(
            TalkClient(_config, bearer_token=access, timeout=5), "user",
        ):
            return

    if not _config or not _config.nextcloud.url:
        return
    from .async_runtime import get_talk_client
    if not await _attempt(get_talk_client(_config), "bot"):
        logger.info(
            "talk delete not propagated room=%s msg=%s (no credential could)",
            talk_ref, msg_id,
        )


def _cross_room_message_dict(r, username: str) -> dict:
    """One `db._CROSS_ROOM_COLUMNS` row → the history payload shape.

    Shared by the paginated aggregate views and the live room-event stream, so
    a streamed row and a reloaded row are byte-identical and the client can
    build both through the same `buildHistoryMessage`. Every row additionally
    carries `room_token` / `room_name` (the stream needs the token to route a
    frame; the aggregate panes render the label)."""
    base = {
        "msg_id": r["msg_id"], "starred": bool(r["starred"]),
        "room_token": r["room_token"], "room_name": r["room_name"] or "",
    }
    if r["role"] == "user":
        # `status` is the owning task's — the stream reads it to decide whether
        # a user turn from another surface is still in flight (and so needs a
        # task stream opened for it). Harmless on the aggregate panes.
        d = {
            "role": "user", "task_id": r["task_id"],
            "status": r["status"], "created_at": r["created_at"], **base,
            **_user_row_display(r, username),
        }
        d.update(_row_attachment_fields(r, username))
    elif r["role"] == "assistant":
        d = _assistant_message_dict(r, r["body"], r["status"] or "completed")
        d.update(base)
    else:  # system — same shape as the per-room notes merge
        text = f"**{r['title']}**\n\n{r['body']}" if r["title"] else r["body"]
        d = {
            "role": r["role"], "text": text, "notif_id": r["msg_id"],
            "created_at": r["created_at"], **base,
        }
    cited = _row_reply_to(r)
    if cited is not None:
        d["reply_to"] = cited
    d["created_at"] = _iso_utc(d.get("created_at"))
    return d


def _chat_aggregate_messages(
    username: str,
    view: str,
    limit: int,
    before: tuple[str, int] | None,
) -> dict:
    """One page of the cross-room All / Unread / Starred stream, oldest-first,
    in the same message shape as the per-room endpoint plus `room_token` /
    `room_name`. Durable store only — no aux gap-fill, no in-flight slots (the
    aggregate panes are reading surfaces; the room view is the live console)."""
    from . import db
    before_ts, before_id = before if before is not None else (None, None)
    with db.get_db(_config.db_path) as conn:
        # limit+1 → the extra row is the has_more probe.
        rows = db.list_messages_across_rooms(
            conn, username, view=view, limit=limit + 1,
            before_ts=before_ts, before_id=before_id,
        )
    has_more = len(rows) > limit
    rows = rows[:limit]
    # Rows arrive newest-first; the page's last row is its oldest → the cursor
    # (raw stored created_at + id, NOT the display value — same contract as the
    # per-room endpoint).
    oldest_cursor = (
        {"ts": rows[-1]["created_at"], "id": rows[-1]["msg_id"]} if rows else None
    )
    messages = [_cross_room_message_dict(r, username) for r in reversed(rows)]
    return {
        "messages": messages,
        "has_more": has_more,
        "oldest_cursor": oldest_cursor,
    }


def _chat_mark_all_read(username: str) -> list[str]:
    """Advance every visible room's web cursor. Returns the tokens of the
    rooms whose cursor actually moved (they get the Talk read push)."""
    from . import db
    with db.get_db(_config.db_path) as conn:
        moved = db.mark_all_rooms_read_tokens(conn, username)
    logger.info("mark_all_rooms_read user=%s rooms_updated=%d", username, len(moved))
    return moved


@api_router.get("/chat/messages")
async def chat_messages_view(
    view: str = "all",
    limit: int = 50,
    before_ts: str | None = None,
    before_id: int | None = None,
    user: dict = Depends(_require_api_auth),
):
    """Cross-room message stream for the All / Unread / Starred views."""
    if view not in ("all", "unread", "starred"):
        return JSONResponse({"error": "unknown view"}, status_code=400)
    # Same both-or-neither cursor contract as the per-room endpoint.
    if (before_ts is None) != (before_id is None):
        return JSONResponse(
            {"error": "before_ts and before_id must be supplied together"},
            status_code=400,
        )
    before = (before_ts, before_id) if before_ts is not None else None
    limit = max(1, min(limit, 200))
    return await asyncio.to_thread(
        _chat_aggregate_messages, user["username"], view, limit, before,
    )


@api_router.put("/chat/messages/{message_id}/star")
async def chat_star_message(
    message_id: int,
    request: Request,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    """Star or unstar a durable message for the requesting user."""
    try:
        data = await request.json()
    except Exception:
        data = None
    starred = data.get("starred") if isinstance(data, dict) else None
    if not isinstance(starred, bool):
        return JSONResponse(
            {"error": "body must be {\"starred\": true|false}"}, status_code=422,
        )
    ok = await asyncio.to_thread(
        _chat_set_message_star, user["username"], message_id, starred,
    )
    if not ok:
        return JSONResponse({"error": "message not found"}, status_code=404)
    return {"ok": True, "starred": starred}


@api_router.delete("/chat/messages/{message_id}")
async def chat_delete_message(
    message_id: int,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    """Hard-delete one transcript row.

    Gone from every read path at once — the room view, the All/Unread/Starred
    panes, and the LLM's own conversation context. Other open clients learn of
    it through the `message_deleted` frame on `/chat/stream`, which tails the
    `message_deletions` ledger (the `messages` row itself is gone, so the
    ordinary message tail has nothing left to carry).

    A Talk-bound room additionally gets a best-effort delete of the mirrored
    Talk message, fire-and-forget: the web-side delete has already committed
    and must not be reported as failed because Talk was unreachable.
    """
    username = user["username"]
    result = await asyncio.to_thread(_chat_delete_message, username, message_id)
    if result == "not_found":
        return JSONResponse({"error": "message not found"}, status_code=404)
    if result == "busy":
        return JSONResponse(
            {"error": "message belongs to a task in progress"}, status_code=409,
        )
    if result["talk_ref"] and result["talk_message_id"]:
        _fire_and_forget(_delete_from_talk(
            username, result["talk_ref"], result["talk_message_id"],
        ))
    return {"ok": True, "message_id": message_id}


@api_router.post("/chat/rooms/read-all")
async def chat_read_all_rooms(
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    """Mark every visible room read on the web surface in one action (the
    header's mark-all chip). Returns how many rooms' cursors moved. Rooms
    whose cursor actually moved get the Talk read push too."""
    moved = await asyncio.to_thread(_chat_mark_all_read, user["username"])
    for room_token in moved:
        _fire_and_forget(_push_read_to_talk(user["username"], room_token))
    return {"ok": True, "updated": len(moved)}


@api_router.post("/chat/rooms/{room_id}/messages")
async def chat_send_message(
    room_id: int,
    request: Request,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    username = user["username"]
    room = await asyncio.to_thread(_chat_owned_room, username, room_id)
    if room is None:
        return JSONResponse({"error": "room not found"}, status_code=404)
    if room.archived:
        # Archived rooms are hidden in the UI; reject sends so they don't keep
        # spawning tasks and churning their channel memory behind your back.
        return JSONResponse({"error": "room is archived"}, status_code=409)

    data = await request.json()
    text = (data.get("text") or "").strip()
    if len(text) > _config.web.chat.max_prompt_chars:
        return JSONResponse({"error": "message too long"}, status_code=400)

    attachments = _validate_chat_attachments(username, data.get("attachments") or [])
    if attachments is None:
        return JSONResponse({"error": "invalid attachment path"}, status_code=400)
    # Display labels for the transcript's attachment chips. The stored filename
    # carries a collision-avoiding random suffix, so the name the user picked is
    # only knowable from the client. Display-only and never a path, so it needs
    # no path validation — just a bound on what a client can persist.
    raw_names = data.get("attachment_names") or []
    attachment_names = (
        [str(n)[:_MAX_ATTACHMENT_NAME_CHARS] for n in raw_names]
        if isinstance(raw_names, list) else []
    )
    # The client's own identity for this message, carried by every attempt at
    # it, so a retry of a send we accepted but never got to report resolves to
    # the first turn. Opaque, so it is bounded rather than validated — but the
    # bound *rejects* rather than truncating: truncating changes the identity,
    # so two distinct keys sharing a prefix would silently resolve to one
    # another's task and the second message would be swallowed. Anything that
    # is not a non-empty string within the bound is treated as absent, which is
    # exactly the pre-feature behaviour.
    raw_client_id = data.get("client_msg_id")
    client_msg_id = (
        raw_client_id
        if isinstance(raw_client_id, str)
        and 0 < len(raw_client_id) <= _MAX_CLIENT_MSG_ID_CHARS
        else None
    )
    # The canonical `messages.id` this send replies to. Only the id is accepted
    # — the parent's text is read server-side from the row we already hold, so a
    # client cannot dictate what the model is told it previously said. Anything
    # that is not a positive integer is treated as absent (`bool` is an `int`
    # subclass, hence the explicit exclusion).
    raw_reply_to = data.get("reply_to_msg_id")
    reply_to_msg_id = (
        raw_reply_to
        if isinstance(raw_reply_to, int)
        and not isinstance(raw_reply_to, bool)
        and raw_reply_to > 0
        else None
    )

    # An attachment-only send is a real message — a voice memo recorded in the
    # composer is the whole message, with nothing typed alongside it. The
    # attachment *is* the content, so stand a short descriptor in for the empty
    # text rather than rejecting the send (or storing a blank user turn that
    # reads as nothing in history, in LLM context, and on a Talk mirror leg).
    # The executor's audio pre-transcription then folds the spoken words in.
    if not text:
        if not attachments:
            return JSONResponse({"error": "text or attachment required"}, status_code=400)
        text = _describe_attachment_only_message(attachments)

    # A leading "!" is either a `!model` prefix (strip + carry overrides into the
    # task) or a `!command` (run synchronously, return inline — no task row, no
    # events). Mirrors the Talk inbound order so the command set is identical
    # across surfaces.
    model_override: str | None = None
    effort_override: str | None = None
    # An explicit `!model` prefix (any alias, incl. `default`) suppresses the
    # per-room model default so a per-message choice always wins.
    model_prefix_used = False
    if text.startswith("!"):
        from . import commands
        from .async_runtime import run_coro
        from .brain import make_brain
        from .transport import make_registry

        brain = make_brain(_config.brain)
        prefix = commands.resolve_model_prefix(
            text, brain, has_attachments=bool(attachments),
        )
        if prefix.usage is not None:
            return {"task_id": None, "inline_result": prefix.usage}
        if prefix.matched:
            model_prefix_used = True
            model_override = prefix.model
            effort_override = prefix.effort
            text = prefix.content

        if text.startswith("!"):
            registry = make_registry(_config)

            def _run_cmd():
                return run_coro(commands.dispatch(
                    _config, username, room.token, text,
                    surface="web", registry=registry,
                ))

            result = await asyncio.to_thread(_run_cmd)
            if result.handled:
                return {
                    "task_id": None,
                    "inline_result": result.text or "",
                    "command_data": result.data,
                }

    # A bare "yes"/"no" answers a parked confirmation, as it does in Talk
    # (`transport/talk/inbound.py`, before its own `ingest_message`). The
    # ordering is not cosmetic: `_chat_create_web_task` cancels this room's
    # pending confirmations on any new message, so an answer that reached it
    # would cancel the question it is answering. Attachment-bearing sends are
    # excluded — the file is the message, whatever the caption says.
    if not attachments:
        from . import confirmations

        answer = confirmations.parse_answer(text)
        if answer is not None:
            answered = await asyncio.to_thread(
                _chat_answer_confirmation, username, room.token, text, answer,
                reply_to_msg_id, client_msg_id,
            )
            if answered is not None:
                return {
                    "task_id": None,
                    "inline_result": answered["ack"],
                    "command_data": {
                        "kind": "confirmation_answered",
                        "user_msg_id": answered["user_msg_id"],
                        "system_msg_id": answered["system_msg_id"],
                    },
                }
            # Nothing was parked — "yes" is just a message. Fall through.

    outcome, value = await asyncio.to_thread(
        _chat_create_web_task, username, room.token, text, attachments,
        model_override, effort_override, not model_prefix_used,
        attachment_names, client_msg_id, reply_to_msg_id,
    )
    if outcome == "rate_limited":
        return JSONResponse(
            {"error": "rate limit exceeded"},
            status_code=429,
            headers={"Retry-After": str(value)},
        )
    if outcome == "reply_target_gone":
        # 404 for both "unknown id" and "a message in another room",
        # indistinguishable — the rule the star and delete endpoints already
        # set, so no endpoint becomes an id oracle for the others.
        return JSONResponse(
            {"error": "the message you replied to is no longer available"},
            status_code=404,
        )
    task_id = value
    # Post-as-user mirror into a bound Talk room, at send time (bounded ~5s,
    # best-effort). When it succeeds the scheduler suppresses its completion-
    # time attributed repost; when it doesn't, the repost covers the mirror.
    await _mirror_web_turn_as_user(
        username, room.token, text, task_id, reply_to_msg_id,
    )
    return {
        "task_id": task_id,
        "status": "pending",
        "stream_url": f"/istota/api/chat/tasks/{task_id}/stream",
        "snapshot_url": f"/istota/api/chat/tasks/{task_id}/events",
    }


def _chat_confirm_task(task_id: int) -> None:
    from . import confirmations, db
    with db.get_db(_config.db_path) as conn:
        task = db.get_task(conn, task_id)
        if task is None or task.status != "pending_confirmation":
            # Only a parked confirmation is confirmable. Returning early keeps a
            # stray confirm (a duplicate click, a running re-run) from wiping a
            # live task's event log — delete_task_events is unconditional, so
            # the status gate must live here, not just in db.confirm_task.
            return
        # Clear prior events so the confirmed re-run's reset seq counter can't
        # collide on UNIQUE(task_id, seq) — the client already captured them.
        db.delete_task_events(conn, task_id)
        # Shared with the Talk poller and `!confirm` so all three restore the
        # transcript mirror the gate withheld (ISSUE-241).
        confirmations.approve(conn, task, config=_config)


def _chat_pending_confirmations(username: str) -> list[dict]:
    """Every question waiting on this user, oldest first.

    The API equivalent of `handle_confirmation_reply`'s Path C, and the reason
    it is its own endpoint rather than a widening of `_AUX_SOURCE_SCOPE`: a
    first-contact email's `conversation_token` is the synthetic thread hash, so
    there is no room whose history query could ever surface it — and the aux
    query renders `tasks.prompt`, which for a gated email is exactly the
    untrusted body the gate is holding back.

    What ships is the bot-composed prompt plus the sender / subject / routing
    method off `processed_emails`. Sender and subject are still attacker
    supplied, so the client renders them as text, never as markup.
    """
    from . import confirmations, db
    from .transport.ingest import ROOM_SURFACES
    out: list[dict] = []
    with db.get_db(_config.db_path) as conn:
        for task in confirmations.pending_for_user(conn, username):
            # A turn that started on a room surface already renders its own
            # `ConfirmationCard` in the transcript (its `confirmation` event
            # replays on the task stream). Listing it here too would show one
            # question twice with two answer paths, and answering from the
            # banner leaves the card stale. `ROOM_SURFACES` is the same set
            # `record_inbound` uses to decide who owns a room.
            if (task.source_type or "") in ROOM_SURFACES:
                continue
            email: dict | None = None
            if task.source_type == "email":
                record = db.get_email_for_task(conn, task.id)
                if record is not None:
                    email = {
                        "sender": record.sender_email,
                        "subject": record.subject,
                        "routing_method": record.routing_method,
                    }
            out.append({
                "task_id": task.id,
                "source_type": task.source_type,
                "created_at": _iso_utc(task.created_at),
                "prompt": task.confirmation_prompt or "",
                "summary": confirmations.describe(conn, task),
                "room_token": (
                    task.conversation_token
                    if db.get_room(conn, task.conversation_token or "") is not None
                    else None
                ),
                "email": email,
            })
    return out


@api_router.get("/chat/confirmations")
async def chat_pending_confirmations(user: dict = Depends(_require_api_auth)):
    """Questions the user has been asked but not answered, on any surface.

    Unconditionally reachable from web chat — it needs no routing
    configuration, which is what makes a web-only user able to see an email
    gate at all (ISSUE-241).
    """
    items = await asyncio.to_thread(_chat_pending_confirmations, user["username"])
    return {"confirmations": items}


def _chat_cancel_task(task_id: int) -> None:
    from . import confirmations, db
    with db.get_db(_config.db_path) as conn:
        row = conn.execute(
            "SELECT worker_pid, status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        status = row["status"] if row else None
        if status == "pending_confirmation":
            # A parked confirmation isn't running — reject it outright rather
            # than flagging a worker that will never see the flag. Through the
            # shared verb, so this path records the same `task_logs` row the
            # Talk poller and `!confirm` do.
            task = db.get_task(conn, task_id)
            if task is not None:
                confirmations.decline(conn, task)
            return
        conn.execute(
            "UPDATE tasks SET cancel_requested = 1 WHERE id = ?", (task_id,)
        )
    # Best-effort subprocess kill; the scheduler's cancel_check ends the task
    # and emits cancelled/done so the SSE stream closes cleanly.
    if row and row["worker_pid"]:
        try:
            os.kill(row["worker_pid"], signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


@api_router.post("/chat/tasks/{task_id}/confirm")
async def chat_confirm_task(
    task_id: int,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    await _authorize_task_access(task_id, user)
    await asyncio.to_thread(_chat_confirm_task, task_id)
    return {"status": "ok"}


@api_router.post("/chat/tasks/{task_id}/cancel")
async def chat_cancel_task(
    task_id: int,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    await _authorize_task_access(task_id, user)
    await asyncio.to_thread(_chat_cancel_task, task_id)
    return {"status": "cancelling"}


def _chat_attachment_dir(username: str, day: str) -> Path:
    """Where a web-chat upload lands: the user's inbox under the mount when one
    is configured (so the brain reads it via the sandboxed workspace), else the
    user temp dir (always RW inside the sandbox). The first upload root is the
    write target; the validator (`_validate_chat_attachments`) accepts both."""
    return _chat_upload_roots(username)[0] / day


_ATTACHMENT_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")
# A chip label is display-only, so it's bounded rather than sanitized (the
# renderer escapes it); this just keeps a client from persisting an essay.
_MAX_ATTACHMENT_NAME_CHARS = 200
# The idempotency key is opaque to us — a UUID today — so it is bounded rather
# than validated. Long enough for any sane identity scheme, short enough that
# it cannot be used as storage.
_MAX_CLIENT_MSG_ID_CHARS = 64
# Prompt snapshot of a cited parent, stored on `tasks.reply_to_content`. Same
# cap Talk has always used (`transport/talk/inbound.py`). Distinct from the
# display excerpt below: this one is read by the model.
_REPLY_SNAPSHOT_CHARS = 1000
# Display excerpt on a rendered citation. Without it every reply in a page
# carries a full assistant answer to render as two lines.
_REPLY_EXCERPT_CHARS = 200


def _attachment_stem(filename: str, limit: int = 48) -> str:
    """Filesystem-safe leading part of a stored attachment's name.

    ``Path.stem`` drops any directory component, and the substitution keeps
    only characters that can't traverse or confuse a path, so a hostile
    ``filename`` (it comes from the client) can only shorten to ``""``.
    """
    stem = _ATTACHMENT_STEM_RE.sub("-", Path(filename).stem).strip("-._")
    return stem[:limit]


def _save_chat_attachment(username: str, filename: str, data: bytes) -> str:
    import uuid
    from datetime import date
    ext = Path(filename).suffix.lower()
    day = date.today().isoformat()
    dest_dir = _chat_attachment_dir(username, day)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Lead with the uploaded name so the inbox is browsable (a voice message
    # reads as `voice-20260726-131512-a3f91c02.webm`, not a bare UUID), and
    # keep a random suffix so two same-named uploads in one day can't collide.
    stem = _attachment_stem(filename)
    suffix = uuid.uuid4().hex[:8] if stem else uuid.uuid4().hex
    dest = dest_dir / f"{stem}-{suffix}{ext}" if stem else dest_dir / f"{suffix}{ext}"
    dest.write_bytes(data)
    return str(dest)


@api_router.post("/chat/attachments")
async def chat_upload_attachment(
    file: UploadFile = File(...),
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    """Upload one file for a chat message. Lands in the user's
    ``inbox/web-chat/YYYY-MM-DD/`` and returns the path the next message
    should reference."""
    chat = _config.web.chat
    name = file.filename or "upload"
    ext = Path(name).suffix.lower().lstrip(".")
    if chat.attachment_extensions and ext not in chat.attachment_extensions:
        return JSONResponse(
            {"error": f"file type .{ext} not allowed"}, status_code=400,
        )
    data = await file.read()
    if len(data) > chat.max_attachment_mb * 1024 * 1024:
        return JSONResponse(
            {"error": f"file exceeds {chat.max_attachment_mb} MB"}, status_code=413,
        )
    username = user["username"]
    path = await asyncio.to_thread(_save_chat_attachment, username, name, data)
    # `workspace_path` is what `/chat/files` takes, so the composer can link the
    # chip it renders optimistically instead of waiting for the turn to come
    # back from history. None on a mountless deployment, where nothing is
    # servable and the chip stays inert.
    from .transport.ingest import workspace_attachment_paths
    resolved = workspace_attachment_paths(_config, username, [path])
    return {
        "path": path,
        "name": name,
        "size": len(data),
        "workspace_path": resolved[0] if resolved else None,
    }


# ---- Chat file download (authenticated handover) ----
#
# Web chat has no outbound attachment channel, so a file a task produces has no
# way to reach the user. The alternative was minting a public Nextcloud link to
# hand someone a file they already own — turning an authenticated-only file into
# a bearer-URL grant. This serves it inside the session instead; link shares go
# back to being for giving a file to *someone else*.


class ChatFileError(Exception):
    """Refusal to serve a path, carrying the status the caller should see."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _chat_file_workspace(username: str) -> Path:
    """On-disk root the caller's downloads are confined to.

    Deliberately the user's own workspace with no admin bypass. The endpoint
    exists to hand someone their own files; an admin who needs to read
    elsewhere has the sandbox and the CLI, and widening this would make the
    single most directly-reachable read path on the web app the widest one.
    """
    root = _config.workspace_root(username) if _config else None
    if root is None:
        raise ChatFileError(
            503,
            "This deployment has no local workspace mount, so files cannot be "
            "served directly. Use a Nextcloud share link instead.",
        )
    return root


def _resolve_chat_file(username: str, path: str) -> Path:
    """Map a caller-supplied workspace path to a real file, or refuse.

    Two independent checks, because they catch different escapes: the lexical
    scope check (shared with the skill CLI, so the browser and the model are
    held to one rule) rejects ``..`` and absolute paths outside the workspace,
    and the realpath check afterwards rejects a symlink *inside* the workspace
    that points out of it — which no amount of string normalization can see.
    """
    from .nextcloud._http import (
        PathScopeError,
        resolve_scoped_path,
        workspace_root as nc_workspace_root,
    )

    raw = (path or "").strip()
    if not raw:
        raise ChatFileError(400, "path is required")
    if "\x00" in raw:
        raise ChatFileError(400, "path is not a valid filename")

    try:
        # is_admin=False always — see _chat_file_workspace.
        scoped = resolve_scoped_path(raw, username, is_admin=False)
    except PathScopeError as e:
        raise ChatFileError(403, str(e)) from e

    root = _chat_file_workspace(username)
    # Same helper the scope check anchors on, so the Nextcloud-path prefix and
    # the on-disk root can't drift apart.
    relative = scoped[len(nc_workspace_root(username)):].lstrip("/")
    if not relative:
        raise ChatFileError(400, "path names the workspace itself, not a file")

    real_root = os.path.realpath(root)
    real = os.path.realpath(os.path.join(real_root, relative))
    if real != real_root and not real.startswith(real_root + os.sep):
        raise ChatFileError(403, "path resolves outside your workspace")

    target = Path(real)
    if not target.exists():
        raise ChatFileError(404, "file not found")
    if target.is_dir():
        raise ChatFileError(400, "path is a directory, not a file")
    if not target.is_file():
        raise ChatFileError(400, "path is not a regular file")
    return target


@api_router.get("/chat/files")
async def chat_download_file(
    path: str = Query(..., description="Workspace path of the file to download"),
    user: dict = Depends(_require_api_auth),
):
    """Serve one file out of the caller's own workspace, inside their session.

    This is how a task hands over a file it produced. No share is created, so
    nothing becomes reachable outside the authenticated session.
    """
    username = user["username"]
    try:
        target = await asyncio.to_thread(_resolve_chat_file, username, path)
    except ChatFileError as e:
        return JSONResponse({"error": e.message}, status_code=e.status)

    return FileResponse(
        target,
        filename=target.name,
        # Always an attachment: the workspace holds user-authored HTML and SVG,
        # and rendering those inline would execute them on the app's own origin
        # against the session cookie that just authorized the read.
        content_disposition_type="attachment",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---- Google Workspace API routes ----


def _google_status_payload(username: str) -> dict:
    """Everything the settings card renders, in one read.

    Four distinct states the card has to keep apart, and which the old
    ``{enabled, connected}`` pair could not express:

    - the instance does not offer a service (not in the operator's ceiling);
    - the user chose not to grant it;
    - they granted it, at read-only or at full;
    - they granted it *before* the request changed, so the grant is narrower
      (``missing_scopes``) or wider (``extra_scopes``) than what a reconnect
      would now ask for. Nothing revalidates a grant at startup, so this is
      the state behind "the bot can't see my calendar" and it was invisible.
    """
    from . import google_scopes

    if not _config or not _config.google_workspace.enabled:
        return {
            "enabled": False,
            "connected": False,
            "offered": [],
            "granted": [],
            "unrecognized_scopes": [],
            "unoffered_scopes": [],
            "selection": {},
            "selection_set": False,
            "requested_scopes": [],
            "missing_scopes": [],
            "extra_scopes": [],
        }

    ceiling = _config.google_workspace.scopes
    stored = _google_scope_selection(username)
    granted = _google_granted_scopes(username)
    requested = google_scopes.resolve_selection(stored, ceiling)
    summary = google_scopes.summarize_granted(granted or [])

    # Report the selection clamped to the current ceiling rather than as
    # stored. An operator who narrows the ceiling after a user picked "full"
    # leaves a stored value nothing will ever honour, and rendering it puts a
    # level in the picker that its own option list no longer contains. The
    # stored value is left alone — it is only overwritten if the user saves.
    effective = google_scopes.normalize_selection(
        stored or google_scopes.default_selection(ceiling),
    )
    offered = google_scopes.offered_services(ceiling)
    max_levels = {o["service"]: o["max_level"] for o in offered}
    for key, level in list(effective.items()):
        ceiling_level = max_levels.get(key, google_scopes.LEVEL_OFF)
        if google_scopes.LEVELS.index(level) > google_scopes.LEVELS.index(ceiling_level):
            effective[key] = ceiling_level

    return {
        "enabled": True,
        "connected": granted is not None,
        "offered": offered,
        "granted": summary["services"],
        "unrecognized_scopes": summary["unrecognized"],
        # Ceiling scopes with no service row. They are requested regardless —
        # no picker can turn one off — so the card names them rather than
        # asking for something it never mentioned.
        "unoffered_scopes": google_scopes.unoffered_scopes(ceiling),
        "selection": effective,
        "selection_set": bool(stored),
        "requested_scopes": requested,
        # Only meaningful once connected: comparing a request against an
        # absent grant would report the whole request as missing.
        "missing_scopes": (
            google_scopes.missing_scopes(requested, granted) if granted is not None else []
        ),
        # The boilerplate Google appends to any OIDC-discovered grant is never
        # in `requested`, so without excluding it here every such user carries
        # a permanent "your grant is wider" banner that reconnecting can't clear.
        "extra_scopes": (
            [
                s for s in google_scopes.missing_scopes(granted, requested)
                if s not in google_scopes.BOILERPLATE_SCOPES
            ]
            if granted is not None else []
        ),
    }


@api_router.get("/google/status")
async def google_status(user: dict = Depends(_require_api_auth)):
    """Connection state, granted scopes, and the per-user scope selection."""
    return _google_status_payload(user["username"])


@api_router.put("/google/scopes")
async def google_set_scopes(
    payload: dict,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    """Store the user's per-service scope selection.

    Writing this does **not** change what Google has already granted — the
    selection only takes effect on the next connect, which re-consents. The
    response carries the resolved request so the card can say what that
    reconnect would ask for.
    """
    from fastapi import HTTPException

    from . import google_scopes, user_profiles

    if _config is None:
        raise HTTPException(status_code=503, detail="config not loaded")
    if not _config.google_workspace.enabled:
        # Nothing would ever read the value: connect is unreachable and the
        # card renders the "not configured" state. Refuse rather than store a
        # selection against a service this instance does not have.
        raise HTTPException(
            status_code=409, detail="Google Workspace is not enabled on this instance",
        )
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    raw = payload.get("selection")
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="selection must be an object")

    # Unknown services and levels are dropped rather than rejected: this map
    # outlives any one client build, and a stale UI must not lock a user out
    # of changing the services it does know about.
    selection = google_scopes.normalize_selection(raw)

    username = user["username"]
    user_profiles.ensure_profile(
        _config.db_path, username,
        display_name=user.get("display_name") or username,
    )
    user_profiles.update_profile(_config.db_path, username, google_scopes=selection)
    logger.info(
        "google_workspace scope selection updated user=%s selection=%s",
        username, selection,
    )

    requested = google_scopes.resolve_selection(
        selection, _config.google_workspace.scopes,
    )
    # Advisory only, and the write has already committed — so a read failure
    # here must not turn a successful save into a 500. (The status endpoint
    # makes the opposite call: there the grant *is* the answer.)
    try:
        granted = _google_granted_scopes(username)
    except Exception:  # noqa: BLE001
        logger.warning(
            "google_workspace: could not read the grant for %s after saving the "
            "selection; reporting no reconnect requirement", username, exc_info=True,
        )
        granted = None
    return {
        "ok": True,
        "selection": selection,
        "requested_scopes": requested,
        # True when the stored grant no longer covers the new request, i.e.
        # the user has to reconnect for the change to mean anything.
        "reconnect_required": bool(
            granted is not None and google_scopes.missing_scopes(requested, granted)
        ),
    }


@api_router.delete("/settings/nextcloud-token")
async def nextcloud_token_disconnect(
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    """Delete the user's stored Nextcloud OAuth pair (settings "Disconnect").
    The web session is untouched — only the retained token goes away; the next
    login re-mints a pair if the feature is still enabled."""
    from . import web_tokens as _wt
    deleted = await asyncio.to_thread(
        _wt.delete_tokens, _config.db_path, user["username"],
    )
    if deleted:
        logger.info("Nextcloud token disconnected for user %s", user["username"])
    return {"ok": True, "was_connected": deleted}


@api_router.delete("/google/disconnect")
async def google_disconnect(
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    """Remove Google OAuth tokens for the current user."""
    from . import db
    with db.get_db(_config.db_path) as conn:
        deleted = db.delete_google_token(conn, user["username"])
    if deleted:
        logger.info("Google account disconnected for user %s", user["username"])
    return {"ok": True, "was_connected": deleted}


# ---- Settings: per-service credential management (Phase 5) ----
#
# Service cards are computed from the user's resource declarations + the
# set of secrets currently stored in the encrypted DB table. Plaintext
# values are never returned — the UI only sees a "configured" badge per
# (service, key) pair.

from .secret_schema import (
    CONNECTED_SERVICE_SCHEMA as _CONNECTED_SERVICE_SCHEMA,
    MODULE_SERVICE_SCHEMA as _MODULE_SERVICE_SCHEMA,
    all_known_services as _all_known_services,
)


def _service_status(schema: dict, configured_keys: set[str]) -> str:
    """Compute card status: configured / partial / missing.

    A field is "required" unless ``optional: True`` is set on the field
    spec, or — for back-compat — the label contains the word "optional".
    Status:
    * ``configured`` — every required key is set.
    * ``partial``    — some but not all required keys set.
    * ``missing``    — no required keys set.
    """
    required = {
        f["key"] for f in schema["fields"]
        if not f.get("optional")
        and "optional" not in f.get("label", "").lower()
    }
    if not required:
        # All-optional services: any key set → configured, else missing.
        return "configured" if configured_keys else "missing"
    if required.issubset(configured_keys):
        return "configured"
    if configured_keys & required:
        return "partial"
    return "missing"


def _build_service_card(
    service: str,
    schema: dict,
    stored: dict[str, list[dict]],
    *,
    extra: dict | None = None,
) -> dict:
    configured = {entry["key"] for entry in stored.get(service, [])}
    last_updated = max(
        (entry["updated_at"] or "" for entry in stored.get(service, [])),
        default="",
    ) or None
    card = {
        "service": service,
        "label": schema["label"],
        "status": _service_status(schema, configured),
        "fields": schema["fields"],
        "configured_keys": sorted(configured),
        "last_updated": last_updated,
        "used_by": list(schema.get("used_by", ())),
        "oauth": bool(schema.get("oauth", False)),
        "custom_ui": bool(schema.get("custom_ui", False)),
    }
    if extra:
        card.update(extra)
    return card


@api_router.get("/settings/services")
async def settings_services(user: dict = Depends(_require_api_auth)) -> dict:
    """Connected services for the current user.

    Returns only services in ``_CONNECTED_SERVICE_SCHEMA`` — module-specific
    services live on their per-module settings pages and are reachable via
    ``/settings/module-services/{module}``.
    """
    from . import secrets_store

    if not _config:
        return {"services": []}

    username = user["username"]
    stored = secrets_store.list_user_services(_config.db_path, username)

    cards: list[dict] = []
    for service, schema in _CONNECTED_SERVICE_SCHEMA.items():
        if schema.get("cli_only"):
            # Operator-provisioned via `istota secret`; no web surface.
            continue
        extra: dict = {}
        if service == "google_workspace":
            extra["connected"] = _has_google_token(username)
            extra["enabled"] = bool(
                _config.google_workspace and _config.google_workspace.enabled
            )
        cards.append(_build_service_card(service, schema, stored, extra=extra))
    return {"services": cards}


@api_router.get("/settings/modules")
async def settings_modules(user: dict = Depends(_require_api_auth)) -> dict:
    """Module registry + per-user enabled state.

    Modules are on by default. The web UI uses this to render the
    "Disabled modules" multiselect in /settings → Preferences and to gate
    each module's settings page with a banner.

    Experimental modules (entries in ``EXPERIMENTAL_MODULES``) are hidden
    unless the operator has enabled the matching ``module_<name>`` flag
    via ``[experimental] features`` — they shouldn't appear in the
    settings UI on standard installs.
    """
    from .modules import EXPERIMENTAL_MODULES, MODULE_NAMES

    def _visible(cfg) -> list[str]:
        out = []
        for name in sorted(MODULE_NAMES):
            flag = EXPERIMENTAL_MODULES.get(name)
            if flag and (cfg is None or not cfg.experimental.is_enabled(flag)):
                continue
            out.append(name)
        return out

    if not _config:
        modules = _visible(None)
        return {
            "modules": modules,
            "disabled": [],
            "enabled_for_user": {m: True for m in modules},
        }

    username = user["username"]
    modules = _visible(_config)
    uc = _config.get_user(username)
    disabled = list(uc.disabled_modules) if uc else []
    return {
        "modules": modules,
        "disabled": [m for m in disabled if m in modules],
        "enabled_for_user": {
            m: _config.is_module_enabled(username, m) for m in modules
        },
    }


@api_router.get("/settings/module-services/{module}")
async def settings_module_services(
    module: str,
    user: dict = Depends(_require_api_auth),
) -> dict:
    """Service cards belonging to a single module's settings page.

    Returns ``{"module": ..., "module_enabled": bool, "services": [...]}``.
    Unknown module names return 404. The status pills here use the same
    rules as /settings/services; ``module_enabled=false`` is the signal for
    the module page to render its "module disabled" banner instead of the
    config UI.
    """
    from fastapi import HTTPException
    from . import secrets_store
    from .modules import MODULE_NAMES

    if module not in MODULE_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown module: {module}")

    schemas = _MODULE_SERVICE_SCHEMA.get(module, {})
    if not _config:
        return {
            "module": module,
            "module_enabled": True,
            "services": [],
        }

    username = user["username"]
    enabled = _config.is_module_enabled(username, module)
    stored = secrets_store.list_user_services(_config.db_path, username)
    cards = [
        _build_service_card(service, schema, stored)
        for service, schema in schemas.items()
    ]
    return {
        "module": module,
        "module_enabled": enabled,
        "services": cards,
    }


@api_router.put("/settings/secrets/{service}/{key}")
async def settings_set_secret(
    service: str,
    key: str,
    payload: dict,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
) -> dict:
    """Set or clear a single (service, key) secret for the current user.

    Body: ``{"value": "<plaintext>"}``. Empty value deletes the row.
    Service + key must match the schema (rejects typos and unknown services).
    """
    from . import secrets_store
    from fastapi import HTTPException

    schema = _all_known_services().get(service)
    if not schema:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service}")
    valid_keys = {f["key"] for f in schema["fields"]}
    if key not in valid_keys:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown key '{key}' for service '{service}'",
        )

    value = (payload.get("value") or "").strip() if isinstance(payload, dict) else ""

    try:
        secrets_store.set_secret(_config.db_path, user["username"], service, key, value)
    except secrets_store.SecretKeyMissingError:
        raise HTTPException(
            status_code=503,
            detail="ISTOTA_SECRET_KEY is not set; cannot store secrets.",
        )

    _signal_ingest_reload_if_needed(service, key)

    logger.info(
        "settings: %s %s/%s for user=%s",
        "cleared" if not value else "stored",
        service, key, user["username"],
    )
    return {"ok": True, "service": service, "key": key, "configured": bool(value)}


def _signal_ingest_reload_if_needed(service: str, key: str) -> None:
    """Tell the webhook receiver its token map is stale, if this was a token.

    The receiver is a different process (its own systemd unit on the
    server, its own container under Docker), so a token written here is
    invisible to it until it reloads. Without this the first ping from a
    just-provisioned device 403s, which reads as a bad token rather than as
    a stale cache.
    """
    if service != "overland" or key != "ingest_token" or not _config:
        return
    from .location import ingest_signal

    ingest_signal.signal_reload(_config.db_path)


@api_router.post("/settings/secrets/overland/ingest_token/generate")
async def settings_generate_ingest_token(
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
) -> dict:
    """Mint a fresh location ingest token and return it **once**.

    The only endpoint that puts a secret in a response body. That is the
    point: the token has to reach a phone, and the alternative is the user
    transcribing 43 random characters by hand. Returning it here lets the
    settings page render it as a QR the device scans, after which it is
    write-only like every other secret — no read path exists.

    Generating rotates: any previous token stops working immediately. That
    is the revocation mechanism, and with one token per user it also
    revokes the user's other devices, which is why the UI has to say so.
    """
    import secrets as _secrets

    from . import secrets_store
    from fastapi import HTTPException

    if not _config:
        raise HTTPException(status_code=503, detail="config not loaded")

    if not _user_has_location(user["username"]):
        # The receiver only builds a token map for users with the module on,
        # so a token issued here would be refused on first use. A 409 that
        # says why beats a token that silently never works.
        raise HTTPException(
            status_code=409,
            detail="The location module is off for this user, so an ingest "
                   "token would not be accepted. Enable it in Settings first.",
        )

    if not _config.site.hostname:
        # Checked before minting, not after: rotating the user's token and
        # then refusing would cut off their working devices for nothing.
        #
        # With no hostname the URL below is *relative*, which the QR payload's
        # decoder rejects for not being https — so the phone reports "not an
        # Istota provisioning code" and blames the code rather than the
        # missing config. A tracker cannot post to a relative URL either, so
        # there is no token worth issuing here. Hits the standalone local
        # install, where hostname is routinely blank.
        raise HTTPException(
            status_code=409,
            detail="This deployment has no public hostname configured, so the "
                   "webhook URL a device posts to cannot be built. Set "
                   "[site] hostname and try again.",
        )

    token = _secrets.token_urlsafe(32)
    try:
        secrets_store.set_secret(
            _config.db_path, user["username"], "overland", "ingest_token", token,
        )
    except secrets_store.SecretKeyMissingError:
        raise HTTPException(
            status_code=503,
            detail="ISTOTA_SECRET_KEY is not set; cannot store secrets.",
        )

    _signal_ingest_reload_if_needed("overland", "ingest_token")

    logger.info(
        "settings: generated location ingest token for user=%s", user["username"],
    )
    return {
        "ok": True,
        "token": token,
        "webhook_url": _location_webhook_url(token),
    }


def _location_webhook_url(token: str = "<token>") -> str:
    """The ingest URL a device posts to.

    Shared by the generate endpoint (which fills in the real token so the
    QR carries a working URL) and ``/location/settings-info`` (which leaves
    the placeholder, since it must never echo a stored token).
    """
    hostname = _config.site.hostname if _config else ""
    if not hostname:
        return f"/webhooks/location?token={token}"
    return f"https://{hostname}/webhooks/location?token={token}"


@api_router.post("/money/monarch/login")
async def money_monarch_login(
    payload: dict,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
) -> dict:
    """Derive Monarch session cookies from email+password and store them.

    Body: ``{"email", "password", "mfa_totp", "email_otp"}``. Only ``email``
    and ``password`` are required. ``mfa_totp`` is the *current* 6-digit code
    from an authenticator (we never store the TOTP secret); ``email_otp`` is
    the code Monarch emails when it doesn't recognise the device.

    On success: persists ``session_id`` + ``csrftoken`` to the encrypted
    secrets table and returns ``{"ok": True}``. The plaintext credentials
    are never written to disk — they exist only for the duration of the
    /auth/login/ call.

    Failure modes map to status codes so the UI can render specific
    messages:
    - 400: invalid input (missing email/password, etc.)
    - 401: Monarch rejected the credentials
    - 412: a *challenge*, not a failure — the credentials were accepted and a
      code is needed. ``detail.code`` is ``email_otp_required`` (emailed code)
      or ``mfa_required`` (authenticator code, or one spent on a retry)
    - 503: three distinct conditions the user can't act on — Cloudflare blocked
      the server IP, Monarch's CAPTCHA gate is up, or the client-version
      contract moved. Only the message text tells them apart.
    """
    from fastapi import HTTPException

    from . import secrets_store
    from .money._vendor.monarch_client import (
        MonarchAuthError, MonarchCaptchaRequired, MonarchClient,
        MonarchClientOutdated, MonarchCloudflareBlocked,
        MonarchEmailOTPRequired, MonarchMFARequired,
    )

    email = (payload.get("email") or "").strip() if isinstance(payload, dict) else ""
    password = payload.get("password") or "" if isinstance(payload, dict) else ""
    mfa_totp = (payload.get("mfa_totp") or "").strip() if isinstance(payload, dict) else ""
    email_otp = (payload.get("email_otp") or "").strip() if isinstance(payload, dict) else ""
    if not (email and password):
        raise HTTPException(status_code=400, detail="email and password required")

    try:
        auth = await MonarchClient.login_with_credentials(
            email=email, password=password, mfa_totp=mfa_totp or None,
            email_otp=email_otp or None,
        )
    except MonarchEmailOTPRequired as exc:
        # A challenge, not a failure: the credentials were accepted and Monarch
        # has emailed a code. The detail is structured so the client can tell
        # this from a wrong password without matching on prose — the previous
        # string-matching never worked, because the generic fetch wrapper
        # surfaces only the status code.
        raise HTTPException(
            status_code=412,
            detail={
                "code": "email_otp_required",
                "message": str(exc),
            },
        )
    except MonarchMFARequired as exc:
        raise HTTPException(
            status_code=412,
            detail={
                "code": "mfa_required",
                "message": str(exc),
            },
        )
    except MonarchClientOutdated as exc:
        # 503 because the user can't fix this. The client already re-read the
        # live version and retried once, so reaching here means Monarch's login
        # contract moved — an operator, not the user, has to act. The message
        # deliberately doesn't name the version as the cause: Monarch's own
        # wording says "outdated" for any client identity it won't accept, and
        # taking that at face value has cost two misdiagnoses.
        logger.error("monarch_login_client_rejected msg=%s", exc)
        raise HTTPException(
            status_code=503,
            detail=f"Monarch refused this app's login request. Your email and "
                   f"password were not the problem. This needs an "
                   f"operator-side fix. {exc}",
        )
    except MonarchCaptchaRequired as exc:
        # Monarch's bot-protection gate is sticky once tripped. There is no
        # programmatic way through it; the user must use cookie-paste.
        # 503 + a UI-friendly message so the SvelteKit form can route them
        # to the cookie-paste method.
        logger.warning("monarch_login_captcha user=%s", user["username"])
        raise HTTPException(status_code=503, detail=str(exc))
    except MonarchCloudflareBlocked as exc:
        # 503 because the failure is environmental (server IP), not a
        # client error the caller can fix by re-trying.
        raise HTTPException(status_code=503, detail=str(exc))
    except MonarchAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("monarch_login_unexpected_error")
        raise HTTPException(status_code=500, detail=f"Unexpected: {exc}")

    try:
        secrets_store.set_secret(
            _config.db_path, user["username"], "monarch", "session_id",
            auth.session_id,
        )
        secrets_store.set_secret(
            _config.db_path, user["username"], "monarch", "csrftoken",
            auth.csrftoken,
        )
    except secrets_store.SecretKeyMissingError:
        raise HTTPException(
            status_code=503,
            detail="ISTOTA_SECRET_KEY is not set; cannot store secrets.",
        )

    logger.info(
        "monarch_login_ok user=%s sid_len=%d csrf_len=%d",
        user["username"], len(auth.session_id), len(auth.csrftoken),
    )
    return {"ok": True}


@api_router.delete("/settings/secrets/{service}/{key}")
async def settings_delete_secret(
    service: str,
    key: str,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
) -> dict:
    """Delete a single (service, key) secret for the current user."""
    from . import secrets_store
    from fastapi import HTTPException

    schema = _all_known_services().get(service)
    if not schema:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service}")
    valid_keys = {f["key"] for f in schema["fields"]}
    if key not in valid_keys:
        # Symmetric with the PUT handler — never let a caller delete arbitrary
        # rows by sending a key string that isn't part of the schema.
        raise HTTPException(
            status_code=400,
            detail=f"Unknown key '{key}' for service '{service}'",
        )

    deleted = secrets_store.delete_secret(_config.db_path, user["username"], service, key)
    return {"ok": True, "deleted": deleted}


# ============================================================================
# Phase 6 — User profile (user_profiles table)
# ============================================================================

# Editable scalar/list fields on the profile card. Each entry maps a JSON
# key (sent by the frontend) to a column in user_profiles, plus a coercion
# hook so PUT bodies can be validated without a separate Pydantic model.
_PROFILE_EDITABLE_FIELDS: dict[str, dict] = {
    "display_name":           {"type": "str"},
    "timezone":               {"type": "str"},
    "log_channel":            {"type": "str"},
    "alerts_channel":         {"type": "str"},
    "email_addresses":        {"type": "list[str]"},
    "trusted_email_senders":  {"type": "list[str]"},
    "quiet_email_senders":    {"type": "list[str]"},
    "disabled_skills":        {"type": "list[str]"},
    "disabled_modules":       {"type": "list[str]"},
    "max_foreground_workers": {"type": "int"},
    "max_background_workers": {"type": "int"},
    "default_destination":    {"type": "descriptor"},
    "routing":                {"type": "routing"},
    "briefing_email_html":    {"type": "bool"},
    "timezone_follow_location": {"type": "bool"},
}


def _registered_delivery_surfaces() -> list[str]:
    """Surfaces the UI can offer as a user-chosen destination (briefing output,
    default destination, alert route).

    Only ``user_routable`` registered transports — ``talk`` / ``email`` /
    ``ntfy``. Self-routing surfaces (``istota_file`` delivers back to its own
    TASKS.md line; ``repl`` is the inline terminal) and the events-only
    ``stream`` surface are held back from the UI; all still validate on the wire
    via ``_validate_descriptor_surfaces`` so programmatic / CLI descriptors keep
    working."""
    if _config is None:
        return []
    from .transport import make_registry
    return sorted(make_registry(_config).routable_names())


def _user_rooms(uc) -> list[dict]:
    """Best-effort list of Talk room tokens the UI can offer as a specific
    ``talk:<token>`` destination — the user's auto-provisioned ``log_channel`` /
    ``alerts_channel`` rooms. Shared by the briefings and profile endpoints so a
    routing dropdown can pin a concrete room instead of only the bare ``talk``
    surface (which resolves to the user's default channel / DM)."""
    rooms: list[dict] = []
    seen: set[str] = set()
    if uc:
        for label, token in (
            ("Log channel", uc.log_channel),
            ("Alerts channel", uc.alerts_channel),
        ):
            if token and token not in seen:
                rooms.append({"token": token, "name": label})
                seen.add(token)
    return rooms


_BUILTIN_DELIVERY_SURFACES = frozenset({
    "talk", "email", "ntfy", "istota_file", "stream",
})


def _validate_descriptor_surfaces(descriptor: str) -> None:
    """Raise ValueError if any leaf surface in a descriptor is neither a builtin
    surface nor a registered transport.

    Builtin surfaces (talk/email/ntfy/istota_file/stream) are always accepted
    even when disabled at the instance level — a user may route to email before
    the operator enables it. Only genuinely-unknown surfaces (typos, an
    unregistered Matrix) are rejected."""
    from .transport import make_registry, parse_output_target
    known = set(_BUILTIN_DELIVERY_SURFACES)
    if _config is not None:
        known |= set(make_registry(_config).names())
    for dest in parse_output_target(descriptor):
        if dest.surface not in known:
            raise ValueError(f"unknown delivery surface: {dest.surface}")


def _coerce_profile_value(field: str, value: object) -> object:
    """Validate + coerce a profile field. Raises ValueError on bad input."""
    spec = _PROFILE_EDITABLE_FIELDS.get(field)
    if spec is None:
        raise ValueError(f"unknown profile field: {field}")
    t = spec["type"]
    if t == "str":
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        return value.strip()
    if t == "list[str]":
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"{field} must be a list")
        out = []
        for v in value:
            if not isinstance(v, str):
                raise ValueError(f"{field} entries must be strings")
            v = v.strip()
            if v:
                out.append(v)
        if field == "disabled_modules":
            from .modules import EXPERIMENTAL_MODULES, MODULE_NAMES
            for v in out:
                if v not in MODULE_NAMES:
                    raise ValueError(f"unknown module: {v}")
                # Experimental modules aren't user-visible until the
                # operator enables their flag. Accepting writes for
                # hidden modules would leak the module's existence and
                # persist state that's invisible everywhere else in the
                # UI (the modules endpoint filters them out).
                flag = EXPERIMENTAL_MODULES.get(v)
                if flag and not (_config and _config.experimental.is_enabled(flag)):
                    raise ValueError(f"unknown module: {v}")
        return out
    if t == "int":
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field} must be an integer")
        if n < 0:
            raise ValueError(f"{field} must be >= 0")
        return n
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        raise ValueError(f"{field} must be boolean")
    if t == "descriptor":
        from .transport import parse_output_target
        if value is None or value == "":
            return "talk"
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        value = value.strip()
        if not value:
            return "talk"
        if not parse_output_target(value):
            raise ValueError(f"{field} is not a valid delivery descriptor")
        _validate_descriptor_surfaces(value)
        return value
    if t == "routing":
        from .notifications import PURPOSES
        from .transport import parse_output_target
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be an object")
        out: dict[str, str] = {}
        for purpose, descriptor in value.items():
            if purpose not in PURPOSES:
                raise ValueError(f"unknown routing purpose: {purpose}")
            if descriptor is None or descriptor == "":
                continue  # empty clears the route for that purpose
            if not isinstance(descriptor, str):
                raise ValueError(f"route {purpose} must be a string")
            descriptor = descriptor.strip()
            if not descriptor:
                continue
            if descriptor.lower() == "none":
                # Explicit "deliver nowhere" sentinel — the only way to disable a
                # purpose that would otherwise inherit a legacy field (e.g. turn
                # the execution log off despite a provisioned log_channel).
                out[purpose] = "none"
                continue
            if not parse_output_target(descriptor):
                raise ValueError(f"route {purpose} is not a valid descriptor")
            _validate_descriptor_surfaces(descriptor)
            out[purpose] = descriptor
        return out
    raise ValueError(f"unsupported field type: {t}")  # pragma: no cover


@api_router.get("/settings/profile")
async def settings_profile(user: dict = Depends(_require_api_auth)) -> dict:
    """Return the current user's profile fields (no plaintext secrets)."""
    from . import user_profiles

    if not _config:
        return {"profile": None}
    profile = user_profiles.get_profile(_config.db_path, user["username"])
    if profile is None:
        # Auto-seed; the OAuth callback usually does this, but a logged-in
        # session predating Phase 6 may hit this endpoint with no row.
        profile = user_profiles.ensure_profile(
            _config.db_path, user["username"],
            display_name=user.get("display_name") or user["username"],
        )
    return {"profile": {
        "user_id": profile.user_id,
        "display_name": profile.display_name,
        "timezone": profile.timezone,
        "email_addresses": profile.email_addresses,
        "trusted_email_senders": profile.trusted_email_senders,
        "quiet_email_senders": profile.quiet_email_senders,
        "log_channel": profile.log_channel,
        "alerts_channel": profile.alerts_channel,
        "disabled_skills": profile.disabled_skills,
        "disabled_modules": profile.disabled_modules,
        "max_foreground_workers": profile.max_foreground_workers,
        "max_background_workers": profile.max_background_workers,
        "default_destination": profile.default_destination,
        "routing": profile.routing,
        "briefing_email_html": profile.briefing_email_html,
        "timezone_follow_location": profile.timezone_follow_location,
        "delivery_surfaces": _registered_delivery_surfaces(),
    }}


@api_router.put("/settings/profile")
async def settings_update_profile(
    payload: dict,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
) -> dict:
    """Partial update — only fields present in the body are written.

    Body shape: ``{<field>: <value>, ...}``. Unknown fields → 400. Empty
    payload is a no-op (returns the current profile).
    """
    from . import user_profiles
    from fastapi import HTTPException

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    coerced: dict[str, object] = {}
    for field, value in payload.items():
        if field not in _PROFILE_EDITABLE_FIELDS:
            raise HTTPException(status_code=400, detail=f"unknown field: {field}")
        try:
            coerced[field] = _coerce_profile_value(field, value)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    if _config is None:
        raise HTTPException(status_code=503, detail="config not loaded")

    # Make sure the row exists; web UI auto-seed on login covers the
    # happy path, but a hand-rolled API client could land here cold.
    user_profiles.ensure_profile(
        _config.db_path, user["username"],
        display_name=user.get("display_name") or user["username"],
    )
    if coerced:
        user_profiles.update_profile(_config.db_path, user["username"], **coerced)
        # No in-memory sync needed: gates that depend on these fields
        # (is_module_enabled, …) read user_profiles live, so the next call
        # in this process — and in the scheduler — sees the new value.

    logger.info("profile updated user=%s fields=%s", user["username"], sorted(coerced))
    return {"ok": True, "fields": sorted(coerced)}


# ============================================================================
# Phase 7b — User-managed briefings (briefing_configs table)
# ============================================================================
#
# Briefings can be declared in two places:
#   1. config.toml / per-user TOML  — Ansible-managed (legacy, being retired)
#   2. briefing_configs DB table    — web UI / `istota briefing ensure`
#
# Both sources are merged at config-load time in ``_apply_user_briefings``.
# DB rows replace TOML rows of the same name. The web UI only writes to (2);
# TOML briefings appear with ``"managed": "config"`` so the UI can render
# them as read-only.

def _briefing_to_dict(b, *, managed: str) -> dict:
    """Serialize a BriefingConfig (TOML) or UserBriefing (DB) for the API."""
    out = {
        "managed": managed,
        "name": getattr(b, "name", "") or "",
        "cron": getattr(b, "cron", "") or "",
        "title": getattr(b, "title", "") or "",
        "conversation_token": getattr(b, "conversation_token", "") or "",
        "output": getattr(b, "output", "talk") or "talk",
        "enabled": bool(getattr(b, "enabled", True)),
    }
    if managed == "db":
        out["id"] = int(getattr(b, "id", 0))
    return out


@api_router.get("/settings/briefings")
async def settings_briefings(user: dict = Depends(_require_api_auth)) -> dict:
    """List the current user's briefings, merged from TOML + DB.

    Response: ``{"briefings": [{...}], "rooms": [{token, name}]}``.
    Each entry carries ``managed: "config" | "db"`` so the UI can render
    TOML rows as read-only. ``rooms`` is a best-effort list of Talk room
    tokens the bot can use as the briefing destination — currently
    populated from the user's auto-provisioned ``log_channel`` /
    ``alerts_channel`` (Phase 1) so the UI can offer them as picks
    without exposing every Talk room the bot can see.
    """
    if _config is None:
        return {"briefings": [], "rooms": []}

    from . import user_briefings as _ub

    username = user["username"]
    out: list[dict] = []

    db_rows = _ub.list_briefings(_config.db_path, username)
    db_names = {r.name for r in db_rows}

    uc = _config.get_user(username)
    if uc:
        for b in uc.briefings:
            # Skip DB-merged copies: the current DB query is authoritative,
            # and a stale in-memory copy from startup must not resurface a
            # briefing the user has since deleted as "managed=config".
            if getattr(b, "from_db", False):
                continue
            if b.name in db_names:
                # The DB entry will be rendered below with its real id.
                continue
            out.append(_briefing_to_dict(b, managed="config"))

    for r in db_rows:
        out.append(_briefing_to_dict(r, managed="db"))

    return {
        "briefings": out,
        "rooms": _user_rooms(uc),
        "outputs": _registered_delivery_surfaces(),
    }


def _validate_briefing_payload(payload: dict, *, name_required: bool) -> dict:
    """Common shape check for POST/PUT briefing endpoints.

    Returns the cleaned dict. Raises HTTPException on bad input.
    """
    from fastapi import HTTPException

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    name = (payload.get("name") or "").strip()
    if name_required and not name:
        raise HTTPException(status_code=400, detail="name is required")

    cron = (payload.get("cron") or "").strip()
    if not cron:
        raise HTTPException(status_code=400, detail="cron is required")

    # Blank is meaningful: it means "derive the title from the name".
    title = (payload.get("title") or "").strip()
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="title must be 200 characters or fewer")
    if any(ord(c) < 32 for c in title):
        raise HTTPException(status_code=400, detail="title must not contain control characters")

    output = (payload.get("output") or "talk").strip()
    # Validate every leaf surface is known (rejects typos like "sms"); the
    # grammar stays permissive so legacy ``both`` / comma lists still parse,
    # while the UI offers only ``_registered_delivery_surfaces()``.
    try:
        _validate_descriptor_surfaces(output)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    from .transport import parse_output_target
    token = (payload.get("conversation_token") or "").strip()
    talk_leaf = any(d.surface == "talk" for d in parse_output_target(output))
    if talk_leaf and not token:
        raise HTTPException(
            status_code=400,
            detail=f"conversation_token is required when output is {output!r}",
        )

    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be a boolean")

    return {
        "name": name,
        "cron": cron,
        "title": title,
        "conversation_token": token,
        "output": output,
        "enabled": enabled,
    }


@api_router.post("/settings/briefings")
async def settings_add_briefing(
    payload: dict,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
) -> dict:
    """Upsert a briefing for the current user.

    Body: ``{"name", "cron", "conversation_token"?, "output"?, "enabled"?}``.
    Idempotent — a second POST with the same ``name`` updates in place.
    """
    from fastapi import HTTPException
    from . import user_briefings as _ub

    if _config is None:
        raise HTTPException(status_code=503, detail="config not loaded")

    cleaned = _validate_briefing_payload(payload, name_required=True)
    try:
        briefing, state = _ub.ensure_briefing(
            _config.db_path,
            user_id=user["username"],
            **cleaned,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    logger.info(
        "briefing %s user=%s name=%s",
        state, user["username"], briefing.name,
    )
    return {"ok": True, "id": briefing.id, "state": state}


@api_router.delete("/settings/briefings/{briefing_id}")
async def settings_delete_briefing(
    briefing_id: int,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
) -> dict:
    """Delete a DB-managed briefing. TOML briefings cannot be removed here."""
    from fastapi import HTTPException
    from . import user_briefings as _ub

    if _config is None:
        raise HTTPException(status_code=503, detail="config not loaded")
    if briefing_id <= 0:
        raise HTTPException(status_code=400, detail="briefing_id must be positive")

    deleted = _ub.delete_briefing_by_id(_config.db_path, user["username"], briefing_id)
    if not deleted:
        # Either no row at this id, or the row belongs to another user.
        # Match the resources-endpoint behavior: silent on user_id scoping.
        raise HTTPException(status_code=404, detail="briefing not found")
    return {"ok": True, "deleted": True}


# Tags allowed in feed card excerpts
_ALLOWED_TAGS = {"a", "b", "strong", "i", "em", "br", "p", "ul", "ol", "li", "blockquote", "code", "pre", "img"}


_ALLOWED_HREF_SCHEMES = {"http://", "https://", "mailto:"}


def _sanitize_html(content: str, max_len: int = 600) -> str:
    """Sanitize HTML to allowed tags only, stripping all attributes except img.src and a.href."""
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
                tag_name = tag_match.group(1).lower()
                is_closing = tag_str.startswith("</")
                if is_closing:
                    tag_str = f"</{tag_name}>"
                elif tag_name == "img":
                    src_match = re.search(r'src="([^"]*)"', tag_str)
                    if src_match:
                        tag_str = f'<img src="{escape(html_mod.unescape(src_match.group(1)))}" loading="lazy">'
                    else:
                        tag_str = ""
                elif tag_name == "a":
                    href_match = re.search(r'href="([^"]*)"', tag_str)
                    if href_match:
                        href_val = html_mod.unescape(href_match.group(1)).strip()
                        if any(href_val.lower().startswith(s) for s in _ALLOWED_HREF_SCHEMES):
                            tag_str = f'<a href="{escape(href_val)}">'
                        else:
                            tag_str = "<a>"
                    else:
                        tag_str = "<a>"
                else:
                    # All other allowed tags: strip all attributes
                    tag_str = f"<{tag_name}>"
                result.append(tag_str)
            i = end + 1
        else:
            result.append(escape(content[i]))
            text_len += 1
            i += 1
    return "".join(result).strip()


# ============================================================================
# Location API
# ============================================================================


def _location_query_current(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT lp.timestamp, lp.lat, lp.lon, lp.altitude, lp.accuracy,
                   lp.activity_type, lp.battery, lp.wifi,
                   p.name as place_name
            FROM location_pings lp
            LEFT JOIN places p ON lp.place_id = p.id
            ORDER BY lp.timestamp DESC LIMIT 1
            """
        ).fetchone()
        if not row:
            return {"last_ping": None, "current_visit": None}

        last_ping = {
            "timestamp": row["timestamp"],
            "lat": row["lat"],
            "lon": row["lon"],
            "altitude": row["altitude"],
            "accuracy": row["accuracy"],
            "activity_type": row["activity_type"],
            "battery": row["battery"],
            "place": row["place_name"],
        }

        visit_row = conn.execute(
            """
            SELECT place_name, entered_at, ping_count
            FROM visits
            WHERE exited_at IS NULL
            ORDER BY entered_at DESC LIMIT 1
            """
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
    db_path: str, tz_name: str,
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
                SELECT lp.timestamp, lp.lat, lp.lon, lp.altitude, lp.accuracy,
                       lp.activity_type, lp.speed, lp.battery,
                       p.name as place_name
                FROM location_pings lp
                LEFT JOIN places p ON lp.place_id = p.id
                WHERE lp.timestamp >= ? AND lp.timestamp < ?
                ORDER BY lp.timestamp ASC
            """
            params: list = [since, until]
            if limit:
                query += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(query, params).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT lp.timestamp, lp.lat, lp.lon, lp.altitude, lp.accuracy,
                       lp.activity_type, lp.speed, lp.battery,
                       p.name as place_name
                FROM location_pings lp
                LEFT JOIN places p ON lp.place_id = p.id
                ORDER BY lp.timestamp DESC LIMIT ?
                """,
                (limit or 100,),
            ).fetchall()

        pings = [
            {
                "timestamp": r["timestamp"],
                "lat": r["lat"],
                "lon": r["lon"],
                # Metres as the device reported them; the reference varies by
                # source and is not recorded. Null on a horizontal-only fix, on
                # one the device flagged vertically invalid, and on a point the
                # client declared rather than measured (ISSUE-229).
                "altitude": r["altitude"],
                "accuracy": r["accuracy"],
                "place": r["place_name"],
                "speed": r["speed"],
                "battery": r["battery"],
                "activity_type": r["activity_type"],
            }
            for r in rows
        ]
        return {"pings": pings, "count": len(pings)}
    finally:
        conn.close()


def _location_query_day_summary(db_path: str, tz_name: str, date: str | None) -> dict:
    from zoneinfo import ZoneInfo
    from .geo import (
        cluster_pings, dedupe_near_duplicate_pings, reverse_geocode, haversine,
        filter_transit_clusters, merge_consecutive_stops,
    )

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")

    target_date = date or datetime.now(tz).strftime("%Y-%m-%d")

    day_start = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    since_utc = day_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    until_utc = day_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Per-user pings/places live in location.db; reverse-geocode cache
    # remains in framework istota.db. Two connections.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    framework_db = str(_config.db_path) if _config else ""
    framework_conn = sqlite3.connect(framework_db) if framework_db else None
    if framework_conn is not None:
        framework_conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT lp.timestamp, lp.lat, lp.lon, lp.activity_type, lp.accuracy,
                   lp.place_id, p.name as place_name
            FROM location_pings lp
            LEFT JOIN places p ON lp.place_id = p.id
            WHERE lp.timestamp >= ? AND lp.timestamp < ?
            ORDER BY lp.timestamp ASC
            """,
            (since_utc, until_utc),
        ).fetchall()

        if not rows:
            return {"date": target_date, "timezone": tz_name, "stops": [], "ping_count": 0, "transit_pings": 0}

        pings = [dict(r) for r in rows]
        pings = dedupe_near_duplicate_pings(pings)
        clusters = cluster_pings(pings, radius_m=250)

        saved_places_rows = conn.execute(
            "SELECT id, name, lat, lon, radius_meters FROM places"
        ).fetchall()
        saved_places = [dict(r) for r in saved_places_rows]

        stops, transit_pings = filter_transit_clusters(clusters)

        for stop in stops:
            if stop["place_name"]:
                stop["location"] = stop["place_name"]
                stop["location_source"] = "saved_place"
                # Snap to place center for consistent positioning
                for sp in saved_places:
                    if sp["name"] == stop["place_name"]:
                        stop["lat"] = sp["lat"]
                        stop["lon"] = sp["lon"]
                        break
            else:
                matched = False
                for sp in saved_places:
                    dist = haversine(stop["lat"], stop["lon"], sp["lat"], sp["lon"])
                    if dist <= max(sp["radius_meters"], 100):
                        stop["location"] = sp["name"]
                        stop["location_source"] = "saved_place_proximity"
                        stop["lat"] = sp["lat"]
                        stop["lon"] = sp["lon"]
                        matched = True
                        break
                if not matched:
                    geo = reverse_geocode(
                        stop["lat"], stop["lon"], framework_conn,
                    )
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

        merged = merge_consecutive_stops(stops)

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
        if framework_conn is not None:
            framework_conn.close()


def _location_query_places(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, name, lat, lon, radius_meters, category, notes "
            "FROM places ORDER BY name"
        ).fetchall()
        return {
            "places": [
                {"id": r["id"], "name": r["name"], "lat": r["lat"], "lon": r["lon"],
                 "radius_meters": r["radius_meters"], "category": r["category"],
                 "notes": r["notes"]}
                for r in rows
            ]
        }
    finally:
        conn.close()


def _location_create_place(db_path: str, data: dict) -> dict:
    from .location import db as location_db
    from .geo import haversine

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        notes = (data.get("notes") or "").strip() or None
        place_id = location_db.add_place(
            conn,
            name=data["name"],
            lat=data["lat"],
            lon=data["lon"],
            radius_meters=data.get("radius_meters", 100),
            category=data.get("category"),
            notes=notes,
        )
        # Backfill: assign this place to existing pings within radius
        radius_m = data.get("radius_meters", 100)
        lat, lon = data["lat"], data["lon"]
        # Rough lat/lon bounding box (1 degree lat ~ 111km)
        dlat = radius_m / 111_000
        dlon = radius_m / (111_000 * max(0.01, abs(__import__("math").cos(__import__("math").radians(lat)))))
        candidates = conn.execute(
            """
            SELECT id, lat, lon FROM location_pings
            WHERE place_id IS NULL
              AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
            """,
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        ).fetchall()
        backfilled = 0
        for row in candidates:
            if haversine(lat, lon, row["lat"], row["lon"]) <= radius_m:
                conn.execute("UPDATE location_pings SET place_id = ? WHERE id = ?", (place_id, row["id"]))
                backfilled += 1
        conn.commit()
        return {
            "id": place_id, "name": data["name"], "lat": lat, "lon": lon,
            "radius_meters": radius_m, "category": data.get("category"),
            "notes": notes,
            "backfilled_pings": backfilled,
        }
    finally:
        conn.close()


def _location_update_place(db_path: str, place_id: int, data: dict) -> dict | None:
    from .location import db as location_db
    from .geo import haversine
    import math

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        place = location_db.get_place_by_id(conn, place_id)
        if not place:
            return None

        geo_changed = any(k in data for k in ("lat", "lon", "radius_meters"))

        normalized = {k: v for k, v in data.items() if k in ("name", "lat", "lon", "radius_meters", "category", "notes")}
        # Notes: empty string clears the field. update_place skips None values, so
        # write NULL directly when the client sends an empty notes string.
        if "notes" in normalized:
            n = normalized.pop("notes")
            n = n.strip() if isinstance(n, str) else n
            if n:
                normalized["notes"] = n
            else:
                conn.execute("UPDATE places SET notes = NULL WHERE id = ?", (place_id,))
        location_db.update_place(conn, place_id, **normalized)

        updated = location_db.get_place_by_id(conn, place_id)
        if not updated:
            return None

        # Reassign pings when location or radius changed
        if geo_changed:
            lat, lon = updated.lat, updated.lon
            radius_m = updated.radius_meters

            # Unassign pings that no longer fall within the new geofence
            assigned = conn.execute(
                "SELECT id, lat, lon FROM location_pings WHERE place_id = ?",
                (place_id,),
            ).fetchall()
            for row in assigned:
                if haversine(lat, lon, row["lat"], row["lon"]) > radius_m:
                    conn.execute("UPDATE location_pings SET place_id = NULL WHERE id = ?", (row["id"],))

            # Assign unassigned pings that now fall within the geofence
            dlat = radius_m / 111_000
            dlon = radius_m / (111_000 * max(0.01, abs(math.cos(math.radians(lat)))))
            candidates = conn.execute(
                """
                SELECT id, lat, lon FROM location_pings
                WHERE place_id IS NULL
                  AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
                """,
                (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
            ).fetchall()
            for row in candidates:
                if haversine(lat, lon, row["lat"], row["lon"]) <= radius_m:
                    conn.execute("UPDATE location_pings SET place_id = ? WHERE id = ?", (place_id, row["id"]))

        conn.commit()
        return {
            "id": updated.id, "name": updated.name, "lat": updated.lat,
            "lon": updated.lon, "radius_meters": updated.radius_meters,
            "category": updated.category, "notes": updated.notes,
        }
    finally:
        conn.close()


def _location_delete_place(db_path: str, place_id: int) -> bool:
    from .location import db as location_db

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        place = location_db.get_place_by_id(conn, place_id)
        if not place:
            return False
        location_db.nullify_place_on_pings(conn, place_id)
        location_db.delete_place_by_id(conn, place_id)
        conn.commit()
        return True
    finally:
        conn.close()


@api_router.get("/location/settings-info")
async def api_location_settings_info(user: dict = Depends(_require_api_auth)):
    """Non-secret bits the /location/settings page needs to render.

    Returns the webhook URL the user should paste into Overland — the
    backend never echoes the ingest_token back, so the URL contains a
    ``<token>`` placeholder. Also exposes the instance-wide place-detection
    knobs as read-only context.
    """
    if not _config:
        return {"webhook_url": "", "place_detection": {}}
    loc = _config.location
    return {
        "webhook_url": _location_webhook_url(),
        "module_enabled": _user_has_location(user["username"]),
        "place_detection": {
            "accuracy_threshold_m": loc.accuracy_threshold_m,
            "visit_exit_minutes": loc.visit_exit_minutes,
        },
    }


@api_router.get("/location/current")
async def api_location_current(user: dict = Depends(_require_api_auth)):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, _user_id, _ = loc
    return await asyncio.to_thread(_location_query_current, db_path)


@api_router.get("/location/pings")
async def api_location_pings(
    user: dict = Depends(_require_api_auth),
    date: str = Query(default=""),
    start: str = Query(default=""),
    end: str = Query(default=""),
    limit: int = Query(default=5000, le=50000),
    tz: str = Query(default=""),
):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, _user_id, tz_name = loc
    effective_tz = _resolve_tz(tz, tz_name)
    return await asyncio.to_thread(
        _location_query_pings, db_path, effective_tz,
        date or None, start or None, end or None, limit,
    )


@api_router.get("/location/day-summary")
async def api_location_day_summary(
    user: dict = Depends(_require_api_auth),
    date: str = Query(default=""),
    tz: str = Query(default=""),
):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, _user_id, tz_name = loc
    effective_tz = _resolve_tz(tz, tz_name)
    return await asyncio.to_thread(
        _location_query_day_summary, db_path, effective_tz, date or None,
    )


@api_router.get("/location/places")
async def api_location_places(user: dict = Depends(_require_api_auth)):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, _user_id, _ = loc
    return await asyncio.to_thread(_location_query_places, db_path)


@api_router.post("/location/places")
async def api_location_create_place(request: Request, user: dict = Depends(_require_api_auth), _csrf: None = Depends(_verify_origin)):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, _user_id, _ = loc
    data = await request.json()
    if not data.get("name") or "lat" not in data or "lon" not in data:
        return JSONResponse({"error": "name, lat, lon required"}, status_code=400)
    try:
        result = await asyncio.to_thread(_location_create_place, db_path, data)
        return result
    except Exception as e:
        logger.error("Failed to create place: %s", e)
        return JSONResponse({"error": "failed to create place"}, status_code=400)


@api_router.put("/location/places/{place_id}")
async def api_location_update_place(place_id: int, request: Request, user: dict = Depends(_require_api_auth), _csrf: None = Depends(_verify_origin)):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, _user_id, _ = loc
    data = await request.json()
    result = await asyncio.to_thread(_location_update_place, db_path, place_id, data)
    if not result:
        return JSONResponse({"error": "place not found or not editable"}, status_code=404)
    return result


@api_router.delete("/location/places/{place_id}")
async def api_location_delete_place(place_id: int, request: Request, user: dict = Depends(_require_api_auth), _csrf: None = Depends(_verify_origin)):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, _user_id, _ = loc
    deleted = await asyncio.to_thread(_location_delete_place, db_path, place_id)
    if not deleted:
        return JSONResponse({"error": "place not found or not deletable"}, status_code=404)
    return {"status": "ok"}


@api_router.get("/location/places/{place_id}/stats")
async def api_location_place_stats(place_id: int, user: dict = Depends(_require_api_auth)):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, _user_id, _ = loc
    result = await asyncio.to_thread(_location_place_stats, db_path, place_id)
    if result is None:
        return JSONResponse({"error": "place not found"}, status_code=404)
    return result


@api_router.get("/location/discover-places")
async def api_location_discover_places(
    user: dict = Depends(_require_api_auth),
    min_pings: int = Query(default=10, ge=3, le=1000),
):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, _user_id, _ = loc
    return await asyncio.to_thread(_location_discover_places, db_path, min_pings)


@api_router.get("/location/dismissed-clusters")
async def api_location_list_dismissed(user: dict = Depends(_require_api_auth)):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, _user_id, _ = loc
    return await asyncio.to_thread(_location_list_dismissed, db_path)


@api_router.post("/location/dismissed-clusters")
async def api_location_dismiss_cluster(
    request: Request,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, _user_id, _ = loc
    data = await request.json()
    if "lat" not in data or "lon" not in data or "radius_meters" not in data:
        return JSONResponse({"error": "lat, lon, radius_meters required"}, status_code=400)
    try:
        result = await asyncio.to_thread(
            _location_dismiss_cluster, db_path, data,
        )
        return result
    except Exception as e:
        logger.error("Failed to dismiss cluster: %s", e)
        return JSONResponse({"error": "failed to dismiss cluster"}, status_code=400)


@api_router.delete("/location/dismissed-clusters/{cluster_id}")
async def api_location_restore_dismissed(
    cluster_id: int,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
):
    loc = _get_location_config(user["username"])
    if not loc:
        return JSONResponse({"error": "location not available"}, status_code=404)
    db_path, _user_id, _ = loc
    deleted = await asyncio.to_thread(_location_restore_dismissed, db_path, cluster_id)
    if not deleted:
        return JSONResponse({"error": "dismissed cluster not found"}, status_code=404)
    return {"status": "ok"}


# ============================================================================
# App assembly — order matters: API > auth > static
# ============================================================================

app.include_router(api_router)
app.include_router(auth_router)

# Feeds web API — native, in-process module backed by per-user SQLite.
from istota.feeds.routes import require_auth as _feeds_require_auth
from istota.feeds.routes import router as _feeds_router
from istota.feeds.routes import verify_origin as _feeds_verify_origin

app.include_router(_feeds_router, prefix="/istota/api/feeds", tags=["feeds"])
app.dependency_overrides[_feeds_require_auth] = _require_api_auth
app.dependency_overrides[_feeds_verify_origin] = _verify_origin

# Briefings web API — native, in-process module backed by per-user SQLite.
from istota.briefings.routes import require_auth as _briefings_require_auth
from istota.briefings.routes import router as _briefings_router
from istota.briefings.routes import verify_origin as _briefings_verify_origin

app.include_router(_briefings_router, prefix="/istota/api/briefings", tags=["briefings"])
app.dependency_overrides[_briefings_require_auth] = _require_api_auth
app.dependency_overrides[_briefings_verify_origin] = _verify_origin

# Money web API — mounted when the optional ``money`` extra is installed.
try:
    from istota.money.routes import require_auth as _money_require_auth
    from istota.money.routes import router as _money_router
    from istota.money.routes import verify_origin as _money_verify_origin

    app.include_router(_money_router, prefix="/istota/api/money", tags=["money"])
    app.dependency_overrides[_money_require_auth] = _require_api_auth
    app.dependency_overrides[_money_verify_origin] = _verify_origin
except ImportError:
    pass

# Health web API. Routes mount unconditionally; per-request auth
# resolves via ``is_module_enabled``, which honors the per-user opt-out.
from istota.health.routes import require_auth as _health_require_auth
from istota.health.routes import router as _health_router
from istota.health.routes import verify_origin as _health_verify_origin

app.include_router(_health_router, prefix="/istota/api/health", tags=["health"])
app.dependency_overrides[_health_require_auth] = _require_api_auth
app.dependency_overrides[_health_verify_origin] = _verify_origin

# Garmin auth API — module-agnostic (Garmin is a cross-module connected
# service). Not gated on the health module: a health-opted-out user can
# still connect Garmin for the location track importer.
from istota.garmin_routes import require_auth as _garmin_require_auth
from istota.garmin_routes import router as _garmin_router
from istota.garmin_routes import verify_origin as _garmin_verify_origin

app.include_router(_garmin_router, prefix="/istota/api/garmin", tags=["garmin"])
app.dependency_overrides[_garmin_require_auth] = _require_api_auth
app.dependency_overrides[_garmin_verify_origin] = _verify_origin

# Serve SvelteKit build as static files (catch-all for SPA routing)
if _STATIC_DIR.is_dir():
    app.mount(
        "/istota",
        _CacheHeaderStatics(directory=str(_STATIC_DIR), html=True),
        name="web-static",
    )
