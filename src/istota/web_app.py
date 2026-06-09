"""Authenticated web interface for istota.

Run as: uvicorn istota.web_app:app --host 127.0.0.1 --port 8766

Provides an OIDC-authenticated web UI using Nextcloud as the identity provider.
SvelteKit frontend served as static files, Python handles auth and API.
"""

import asyncio
import json
import logging
import os
import platform
import re
import secrets
import shutil
import signal
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import httpx
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, FastAPI, File, Query, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

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

# Resolve static build directory. Default walks up from this module's path,
# which works for editable installs from the repo root. For non-editable installs
# (Docker runtime), the operator sets ISTOTA_WEB_STATIC_DIR explicitly.
_STATIC_DIR = Path(
    os.environ.get(
        "ISTOTA_WEB_STATIC_DIR",
        str(Path(__file__).resolve().parent.parent.parent / "web" / "build"),
    )
)


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


def _publish_config(app: FastAPI) -> None:
    """Expose the loaded istota config to mounted routers via app.state."""
    app.state.istota_config = _config


@asynccontextmanager
async def lifespan(app: FastAPI):
    _reload_config()
    _publish_config(app)
    signal.signal(signal.SIGHUP, lambda *_: (_reload_config(), _publish_config(app)))
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
        config_secret = (load_config().web.session_secret_key or "").strip()
    except Exception:  # pragma: no cover - defensive
        config_secret = ""
    if config_secret:
        return config_secret

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


def _user_is_web_admin(username: str) -> bool:
    """Web dashboard admin check — fails closed.

    Distinct from ``Config.is_admin``, which treats an empty ``admin_users``
    set as "all users are admin" for sandbox/skill/command back-compat. The
    web admin dashboard requires an explicit allowlist: a missing or empty
    ``/etc/istota/admins`` means no admin access via the web UI.
    """
    if not _config or not _config.admin_users:
        return False
    return username in _config.admin_users


def _require_admin(user: dict = Depends(_require_api_auth)) -> dict:
    """Dependency for admin API routes: returns user or 403."""
    if not _user_is_web_admin(user["username"]):
        raise _ForbiddenException("admin only")
    return user


def _verify_origin(request: Request) -> None:
    """Check Origin/Referer header against configured hostname for CSRF protection."""
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


@auth_router.get("/login")
async def login(request: Request):
    if _oauth is None or not hasattr(_oauth, "nextcloud"):
        return Response("Auth not configured", status_code=500)
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
    return await _oauth.nextcloud.authorize_redirect(request, _nc_redirect_uri(request))


@auth_router.get("/callback")
async def callback(request: Request):
    if _oauth is None or not hasattr(_oauth, "nextcloud"):
        return Response("Auth not configured", status_code=500)
    token = await _oauth.nextcloud.authorize_access_token(request)

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
    return await _oauth.google.authorize_redirect(request, redirect_uri)


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
        return RedirectResponse(url="/istota/?google=error", status_code=302)

    access_token = token.get("access_token", "")
    refresh_token = token.get("refresh_token", "")
    expires_in = token.get("expires_in", 3600)
    scopes = token.get("scope", "")

    if not access_token or not refresh_token:
        logger.error("Google OAuth: missing tokens (access=%s, refresh=%s)",
                      bool(access_token), bool(refresh_token))
        return RedirectResponse(url="/istota/?google=error", status_code=302)

    import json
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    scopes_json = json.dumps(scopes.split()) if isinstance(scopes, str) else json.dumps(scopes)

    from . import db
    with db.get_db(_config.db_path) as conn:
        db.upsert_google_token(
            conn, user["username"], access_token, refresh_token, expiry, scopes_json,
        )
    logger.info("Google account connected for user %s", user["username"])
    return RedirectResponse(url="/istota/?google=connected", status_code=302)


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
        "google_workspace": False,
        "google_workspace_enabled": False,
        "admin": is_admin,
    }
    if _config:
        features["feeds"] = _user_has_feeds(username)
        features["location"] = _user_has_location(username)
        features["money"] = _user_has_money(username)
        features["health"] = _user_has_health(username)
        features["google_workspace_enabled"] = _config.google_workspace.enabled
        if _config.google_workspace.enabled:
            features["google_workspace"] = _has_google_token(username)
    return {
        "username": username,
        "display_name": user.get("display_name", username),
        "bot_name": _config.bot_name if _config else "Istota",
        "is_admin": is_admin,
        "features": features,
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
    return payload


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

    feeds = _admin_module_feeds()
    if feeds is not None:
        modules["feeds"] = feeds

    money = _admin_module_money()
    if money is not None:
        modules["money"] = money

    if _config.location.enabled:
        location_users = sum(
            1 for uid in _config.users
            if _config.is_module_enabled(uid, "location")
        )
        if location_users:
            loc = _admin_module_location()
            loc["users_configured"] = location_users
            modules["location"] = loc

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


def _admin_module_location() -> dict:
    """Aggregate location stats across every user's ``location.db``.

    Per-user files: sum visits + places, take max(last ping timestamp).
    Per-user try/except so one broken DB doesn't blank the whole row.
    """
    out = {"visits_total": 0, "places_total": 0, "last_update": None}
    if not _config:
        return out
    try:
        from . import location as _location  # noqa: PLC0415

        latest: str | None = None
        for uid in _location.list_users(_config):
            try:
                ctx = _location.resolve_for_user(uid, _config)
                if not ctx.db_path.exists():
                    continue
                with _location.connect(ctx.db_path) as conn:
                    out["visits_total"] += conn.execute(
                        "SELECT COUNT(*) AS n FROM visits"
                    ).fetchone()["n"]
                    out["places_total"] += conn.execute(
                        "SELECT COUNT(*) AS n FROM places"
                    ).fetchone()["n"]
                    row = conn.execute(
                        "SELECT MAX(timestamp) AS ts FROM location_pings"
                    ).fetchone()
                    ts = row["ts"] if row else None
                    if ts and (latest is None or ts > latest):
                        latest = ts
            except Exception:
                logger.exception(
                    "location module stats failed for user=%s", uid,
                )
        out["last_update"] = _iso_utc(latest)
    except Exception:
        logger.exception("location module stats failed")
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
    with db.get_db(_config.db_path) as conn:
        task = db.get_task(conn, task_id)
        if task is None or task.status not in _TERMINAL_TASK_STATUSES:
            return []
        pending = db.get_task_events(conn, task_id, after_seq)
    if any(e["kind"] == "done" for e in pending):
        return []  # a real terminal frame is still on its way to this client
    seq = max([after_seq, *(e["seq"] for e in pending)]) + 1
    frames: list[dict] = []
    if task.status == "completed":
        frames.append({"seq": seq, "kind": "result",
                       "payload": {"text": (task.result or "")[:8000]}})
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


@api_router.get("/admin/tasks/{task_id}/events")
async def admin_task_events(
    task_id: int, since_seq: int = 0, _: dict = Depends(_require_admin),
):
    """All events for a task — backs the admin in-flight task-detail view."""
    events = await asyncio.to_thread(_load_task_events, task_id, since_seq)
    return {"events": events}


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


def _chat_list_rooms(username: str) -> list[dict]:
    """The user's non-archived rooms from the unified registry — both web- and
    Talk-origin. A Talk room the bot joined surfaces here automatically (it was
    lazily registered on its first inbound message). Each registry room is given
    a ``web_chat_rooms`` handle (the frontend's integer room id) and a ``web``
    binding on first listing — that handle/binding *is* the room's web presence.
    Each entry carries ``origin`` so the UI can badge Talk rooms and gate the
    promote action."""
    from . import db
    with db.get_db(_config.db_path) as conn:
        db.ensure_default_web_chat_room(conn, username)
        registry = db.list_rooms(conn, username, include_archived=False)
        out: list[dict] = []
        for r in registry:
            handle = db.ensure_web_chat_handle(
                conn, username, r.token, r.name or "Talk room",
            )
            db.add_room_binding(conn, r.token, "web", r.token)
            d = _room_to_dict(handle)
            d["name"] = r.name or handle.name
            d["origin"] = r.origin
            out.append(d)
    return out


def _chat_create_room(username: str, name: str) -> dict:
    from . import db
    with db.get_db(_config.db_path) as conn:
        room = db.create_web_chat_room(conn, username, name)
    return _room_to_dict(room)


def _chat_owned_room(username: str, room_id: int):
    """Return the room if it belongs to ``username``, else None."""
    from . import db
    with db.get_db(_config.db_path) as conn:
        room = db.get_web_chat_room(conn, room_id)
    if room is None or room.user_id != username:
        return None
    return room


def _chat_update_room(
    username: str, room_id: int, name: str | None, archived: bool | None,
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
            if name is not None:
                db.rename_room(conn, updated.token, updated.name)
            if archived is not None:
                db.set_room_archived(conn, updated.token, bool(archived))
    return _room_to_dict(updated) if updated else None


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
        # A Talk-origin room is hidden (archived), not destroyed: deleting from
        # web must not wipe a Nextcloud Talk conversation's mirrored history.
        reg = db.get_room(conn, room.token)
        if reg is not None and reg.origin == "talk":
            db.set_room_archived(conn, room.token, True)
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
                str(e.get("text", ""))
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
) -> list[dict]:
    """Ordered, interleaved ``text`` / ``tool`` segments for a finished task, so
    the web client reconstructs the same in-order layout as the live stream.

    Prefers the ordered ``execution_trace`` (``type`` of ``text`` / ``tool`` /
    ``cm_boundary``; the boundary is skipped). The canonical answer is the
    ``result``: when non-empty it overwrites the trailing text segment (or is
    appended when the trace ends on a tool). Falls back to the flat
    ``actions_taken`` tool descriptions plus a result text segment when the
    trace is absent or malformed. Never raises.
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
                        segments.append({"kind": "text", "text": str(e.get("text", ""))})
                    elif etype == "tool":
                        segments.append({"kind": "tool", "text": str(e.get("text", ""))})
                    # cm_boundary (and anything else) is skipped.
        except (ValueError, TypeError):
            parsed_trace = False
    if not parsed_trace:
        # Fallback: ordered tool descriptions, then the answer.
        for desc in _trace_tool_descriptions(None, actions_taken):
            segments.append({"kind": "tool", "text": desc})
    if result:
        if segments and segments[-1]["kind"] == "text":
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
    if confirmation:
        return {
            "role": "assistant", "text": text, "task_id": _row_get(row, "task_id") or _row_get(row, "id"),
            "status": status, "confirmation": True, "created_at": _row_get(row, "created_at"),
        }
    trace = _row_get(row, "execution_trace")
    actions = _row_get(row, "actions_taken")
    return {
        "role": "assistant", "text": text,
        "task_id": _row_get(row, "task_id") or _row_get(row, "id"),
        "status": status, "created_at": _row_get(row, "created_at"),
        "tools": _trace_tool_descriptions(trace, actions),
        "segments": _trace_segments(trace, actions, text),
        "duration_seconds": _task_duration_seconds(
            _row_get(row, "started_at"), _row_get(row, "completed_at"),
        ),
        "model": _row_get(row, "model_used") or None,
    }


def _row_get(row, key: str):
    """sqlite3.Row.get() equivalent — returns None for a column absent from the
    row's keys instead of raising (the two source queries differ in columns)."""
    return row[key] if key in row.keys() else None


def _chat_room_messages(username: str, token: str, limit: int) -> dict:
    """Recent messages for a room plus the active (in-flight) task, if any.

    The transcript is read from the **durable** canonical `messages` store, not
    the `tasks` table: `cleanup_old_tasks` GCs completed tasks after a few days,
    so a `tasks`-sourced transcript silently lost a dormant room's history and
    surfaced only the stray cancelled/failed tasks retention happens to keep
    (ISSUE-126). Surviving `tasks` rows are joined in only to *enrich* a stored
    turn (trace / timing / model) and to *fill* turns the store doesn't hold —
    failed/cancelled answers (the scheduler stores only successful turns), the
    in-flight assistant slot, and any legacy turn not yet backfilled. Dedup is
    keyed on `(role, task_id)`: the store is authoritative, `tasks` fills gaps.
    """
    from . import db
    with db.get_db(_config.db_path) as conn:
        # 1. Durable turns from the canonical store. LEFT JOIN tasks to enrich a
        #    surviving turn; a retention-deleted turn (t.* NULL) renders plain.
        #    web/talk turns render both sides; scheduled-job posts (the daily
        #    money sync, etc.) render the assistant post only — the synthetic
        #    cron prompt was never user-authored, so its 'user' row stays hidden.
        #    limit*2 because a conversational turn is two rows (user + assistant);
        #    scheduled posts contribute one, so this over-fetches a little for
        #    scheduled-heavy rooms, which is harmless.
        msg_rows = conn.execute(
            "SELECT m.role AS role, m.body AS body, m.task_id AS task_id, "
            "  m.created_at AS created_at, t.status AS status, "
            "  t.actions_taken AS actions_taken, t.execution_trace AS execution_trace, "
            "  t.started_at AS started_at, t.completed_at AS completed_at, "
            "  t.model_used AS model_used "
            "FROM messages m LEFT JOIN tasks t ON t.id = m.task_id "
            "WHERE m.room_token = ? AND ("
            "    (m.origin_surface IN ('web', 'talk') AND m.role IN ('user', 'assistant')) "
            "    OR (m.origin_surface = 'scheduled' AND m.role = 'assistant') "
            "  ) "
            "ORDER BY m.id DESC LIMIT ?",
            (token, limit * 2),
        ).fetchall()
        # 2. Tasks fill gaps the store doesn't hold (failed/cancelled answers,
        #    in-flight slots, un-backfilled legacy turns). Same surface filter.
        task_rows = conn.execute(
            "SELECT id, prompt, result, status, error, confirmation_prompt, "
            "created_at, actions_taken, execution_trace, started_at, completed_at, "
            "model_used "
            "FROM tasks "
            "WHERE conversation_token = ? AND user_id = ? "
            "AND source_type IN ('web', 'talk') "
            "ORDER BY id DESC LIMIT ?",
            (token, username, limit),
        ).fetchall()
        # Bot-delivered system messages (alerts / logs / notifications routed to
        # web) live in the canonical messages store (role='system').
        notes = db.list_system_messages(conn, token, limit)

    messages: list[dict] = []
    seen: set[tuple[str, object]] = set()  # (role, task_id) already rendered

    # 1. Durable store turns (authoritative).
    for r in reversed(msg_rows):  # oldest-first
        tid = r["task_id"]
        if r["role"] == "user":
            messages.append({
                "role": "user", "text": r["body"], "task_id": tid,
                "created_at": r["created_at"],
            })
        else:  # assistant — a stored assistant row is by definition a completed turn
            messages.append(_assistant_message_dict(r, r["body"], r["status"] or "completed"))
        if tid is not None:
            seen.add((r["role"], tid))

    # 2. Tasks fill the gaps, oldest-first. The room runs tasks one at a time
    #    (the per-channel claim gate serializes them), so in-flight ones stream
    #    in this order. `active_task` is kept as the oldest for back-compat.
    active_tasks: list[dict] = []
    for r in reversed(task_rows):
        tid = r["id"]
        if ("user", tid) not in seen:
            messages.append({
                "role": "user", "text": r["prompt"], "task_id": tid,
                "created_at": r["created_at"],
            })
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
                "status": status, "created_at": r["created_at"],
            })
            active_tasks.append({"id": tid, "status": status})

    # Merge bot-delivered messages in by time. `notif_id` gives the client a
    # stable key so an idle poll appends only ones that arrived later.
    for n in notes:
        text = f"**{n.title}**\n\n{n.body}" if n.title else n.body
        messages.append({
            "role": n.role, "text": text, "notif_id": n.id,
            "created_at": n.created_at,
        })
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
        "active_task": active_tasks[0] if active_tasks else None,
        "active_tasks": active_tasks,
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


def _chat_create_web_task(
    username: str, token: str, text: str,
    attachments: list[str] | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> tuple[str, int]:
    """Rate-limited web-task creation. Returns ``("ok", task_id)`` or
    ``("rate_limited", window_seconds)``."""
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
        )
    return ("ok", task_id)


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


@api_router.get("/chat/rooms")
async def chat_list_rooms(user: dict = Depends(_require_api_auth)):
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
    updated = await asyncio.to_thread(
        _chat_update_room, user["username"], room_id, name, archived,
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
    room_id: int, limit: int = 50, user: dict = Depends(_require_api_auth),
):
    room = await asyncio.to_thread(_chat_owned_room, user["username"], room_id)
    if room is None:
        return JSONResponse({"error": "room not found"}, status_code=404)
    limit = max(1, min(limit, 200))
    return await asyncio.to_thread(
        _chat_room_messages, user["username"], room.token, limit,
    )


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
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    if len(text) > _config.web.chat.max_prompt_chars:
        return JSONResponse({"error": "message too long"}, status_code=400)

    attachments = _validate_chat_attachments(username, data.get("attachments") or [])
    if attachments is None:
        return JSONResponse({"error": "invalid attachment path"}, status_code=400)

    # A leading "!" is either a `!model` prefix (strip + carry overrides into the
    # task) or a `!command` (run synchronously, return inline — no task row, no
    # events). Mirrors the Talk inbound order so the command set is identical
    # across surfaces.
    model_override: str | None = None
    effort_override: str | None = None
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
                return {"task_id": None, "inline_result": result.text or ""}

    outcome, value = await asyncio.to_thread(
        _chat_create_web_task, username, room.token, text, attachments,
        model_override, effort_override,
    )
    if outcome == "rate_limited":
        return JSONResponse(
            {"error": "rate limit exceeded"},
            status_code=429,
            headers={"Retry-After": str(value)},
        )
    task_id = value
    return {
        "task_id": task_id,
        "status": "pending",
        "stream_url": f"/istota/api/chat/tasks/{task_id}/stream",
        "snapshot_url": f"/istota/api/chat/tasks/{task_id}/events",
    }


def _chat_confirm_task(task_id: int) -> None:
    from . import db
    with db.get_db(_config.db_path) as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row or row["status"] != "pending_confirmation":
            # Only a parked confirmation is confirmable. Returning early keeps a
            # stray confirm (a duplicate click, a running re-run) from wiping a
            # live task's event log — delete_task_events is unconditional, so
            # the status gate must live here, not just in db.confirm_task.
            return
        # Clear prior events so the confirmed re-run's reset seq counter can't
        # collide on UNIQUE(task_id, seq) — the client already captured them.
        db.delete_task_events(conn, task_id)
        db.confirm_task(conn, task_id)


def _chat_cancel_task(task_id: int) -> None:
    from . import db
    with db.get_db(_config.db_path) as conn:
        row = conn.execute(
            "SELECT worker_pid, status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        status = row["status"] if row else None
        if status == "pending_confirmation":
            # A parked confirmation isn't running — reject it outright rather
            # than flagging a worker that will never see the flag.
            db.cancel_task(conn, task_id)
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


def _save_chat_attachment(username: str, filename: str, data: bytes) -> str:
    import uuid
    from datetime import date
    ext = Path(filename).suffix.lower()
    day = date.today().isoformat()
    dest_dir = _chat_attachment_dir(username, day)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex}{ext}"
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
    path = await asyncio.to_thread(_save_chat_attachment, user["username"], name, data)
    return {"path": path, "name": name, "size": len(data)}


# ---- Google Workspace API routes ----


@api_router.get("/google/status")
async def google_status(user: dict = Depends(_require_api_auth)):
    """Check if user has connected their Google account."""
    if not _config or not _config.google_workspace.enabled:
        return {"enabled": False, "connected": False}
    connected = _has_google_token(user["username"])
    return {"enabled": True, "connected": connected}


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

    logger.info(
        "settings: %s %s/%s for user=%s",
        "cleared" if not value else "stored",
        service, key, user["username"],
    )
    return {"ok": True, "service": service, "key": key, "configured": bool(value)}


@api_router.post("/money/monarch/login")
async def money_monarch_login(
    payload: dict,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
) -> dict:
    """Derive Monarch session cookies from email+password and store them.

    Body: ``{"email": "...", "password": "...", "mfa_totp": "..."}``.
    Only ``email`` and ``password`` are required; ``mfa_totp`` is the
    *current* 6-digit code (we never store the TOTP secret).

    On success: persists ``session_id`` + ``csrftoken`` to the encrypted
    secrets table and returns ``{"ok": True}``. The plaintext credentials
    are never written to disk — they exist only for the duration of the
    /auth/login/ call.

    Failure modes map to status codes so the UI can render specific
    messages:
    - 400: invalid input (missing email/password, etc.)
    - 401: Monarch rejected the credentials
    - 412: MFA required and no code supplied
    - 503: Cloudflare blocked (server IP can't reach login endpoint)
    """
    import asyncio
    from fastapi import HTTPException

    from . import secrets_store
    from .money._vendor.monarch_client import (
        MonarchAuthError, MonarchCaptchaRequired, MonarchClient,
        MonarchClientOutdated, MonarchCloudflareBlocked, MonarchMFARequired,
    )

    email = (payload.get("email") or "").strip() if isinstance(payload, dict) else ""
    password = payload.get("password") or "" if isinstance(payload, dict) else ""
    mfa_totp = (payload.get("mfa_totp") or "").strip() if isinstance(payload, dict) else ""
    if not (email and password):
        raise HTTPException(status_code=400, detail="email and password required")

    try:
        auth = await MonarchClient.login_with_credentials(
            email=email, password=password, mfa_totp=mfa_totp or None,
        )
    except MonarchMFARequired as exc:
        raise HTTPException(
            status_code=412, detail=f"MFA required: {exc}",
        )
    except MonarchClientOutdated as exc:
        # 503 because the user can't fix this — the operator needs to bump
        # CLIENT_VERSION in the source. Surface it loudly.
        logger.error("monarch_login_client_outdated msg=%s", exc)
        raise HTTPException(
            status_code=503,
            detail=f"Monarch client is outdated and login is blocked. "
                   f"This needs an operator-side fix. {exc}",
        )
    except MonarchCaptchaRequired as exc:
        # Monarch's bot-protection gate is sticky once tripped. There is no
        # programmatic way through it; the user must use cookie-paste.
        # 503 + a UI-friendly message so the SvelteKit form can route them
        # to Option B.
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
    "disabled_skills":        {"type": "list[str]"},
    "disabled_modules":       {"type": "list[str]"},
    "max_foreground_workers": {"type": "int"},
    "max_background_workers": {"type": "int"},
    "site_enabled":           {"type": "bool"},
    "default_destination":    {"type": "descriptor"},
    "routing":                {"type": "routing"},
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
        "log_channel": profile.log_channel,
        "alerts_channel": profile.alerts_channel,
        "disabled_skills": profile.disabled_skills,
        "disabled_modules": profile.disabled_modules,
        "max_foreground_workers": profile.max_foreground_workers,
        "max_background_workers": profile.max_background_workers,
        "site_enabled": profile.site_enabled,
        "default_destination": profile.default_destination,
        "routing": profile.routing,
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
# Phase 6 — User-managed resources (user_resources table)
# ============================================================================
#
# Resources can be declared in two places:
#   1. config.toml under [[users.X.resources]]  — Ansible-managed, system topology
#   2. user_resources DB table                  — web UI / `istota resource add`
#
# Both sources are merged at runtime in executor.execute_task. The web UI
# only writes to (2): the operator-controlled config.toml stays read-only
# from the browser. TOML resources show up in the response with
# ``"managed": "config"`` so the UI can render them as read-only rows.

# Resource types that need credentials and/or a real path. The frontend
# uses this list to render the "add resource" picker; the backend validates
# against it on POST.
_RESOURCE_TYPE_SCHEMA: dict[str, dict] = {
    "calendar":      {"label": "Calendar (CalDAV)",        "needs_path": True,  "permissions": ("read", "readwrite")},
    "folder":        {"label": "Nextcloud folder",         "needs_path": True,  "permissions": ("read", "readwrite")},
    "todo_file":     {"label": "TODO file (markdown)",     "needs_path": True,  "permissions": ("read", "readwrite")},
    "notes_folder":  {"label": "Notes folder",             "needs_path": True,  "permissions": ("read", "readwrite")},
    "email_folder":  {"label": "Email folder (IMAP)",      "needs_path": True,  "permissions": ("read",)},
    "reminders_file":{"label": "Reminders (markdown)",     "needs_path": True,  "permissions": ("read", "readwrite")},
}


@api_router.get("/settings/resources")
async def settings_resources(user: dict = Depends(_require_api_auth)) -> dict:
    """List the user's resources, merged from TOML + DB.

    Response: ``{"types": [{type, label, ...}], "resources": [{...}]}``.
    Each resource carries ``managed: "config" | "db"`` so the UI can
    render TOML rows as read-only.
    """
    if _config is None:
        return {"types": [], "resources": []}
    username = user["username"]

    from . import db

    out: list[dict] = []
    with db.get_db(_config.db_path) as conn:
        db_resources = db.get_user_resources(conn, username)
    db_keys = {(r.resource_type, r.resource_path) for r in db_resources}

    uc = _config.get_user(username)
    if uc:
        for rc in uc.resources:
            # _apply_user_resources merges DB rows into uc.resources at
            # config-load time and tags them with ``from_db=True``. Skip
            # those — the current DB query is authoritative, and a stale
            # in-memory copy from startup must not resurface a row the
            # user has since deleted.
            if getattr(rc, "from_db", False):
                continue
            # Defensive: a TOML entry whose (type, path) collides with a
            # current DB row is rendered once via the DB-row loop below.
            if (rc.type, rc.path) in db_keys:
                continue
            out.append({
                "managed": "config",
                "type": rc.type,
                "name": rc.name or "",
                "path": rc.path or "",
                "permissions": rc.permissions or "read",
                # extras suppressed — they may contain credentials we don't
                # want to leak (Phase 5 import path covers the safe shape).
            })

    for r in db_resources:
        out.append({
            "managed": "db",
            "id": r.id,
            "type": r.resource_type,
            "name": r.display_name or "",
            "path": r.resource_path or "",
            "permissions": r.permissions or "read",
            "extras": dict(r.extras or {}),
        })

    types = [
        {"type": t, **{k: v for k, v in spec.items() if k != "permissions"}, "permissions": list(spec["permissions"])}
        for t, spec in _RESOURCE_TYPE_SCHEMA.items()
    ]
    return {"types": types, "resources": out}


@api_router.post("/settings/resources")
async def settings_add_resource(
    payload: dict,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
) -> dict:
    """Add a resource for the current user. Writes to the user_resources DB table.

    Body: ``{"type": ..., "path": ..., "name": ..., "permissions": ...}``.
    TOML-declared resources of the same type are not affected — both
    sources merge at runtime.
    """
    from fastapi import HTTPException

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    rtype = (payload.get("type") or "").strip()
    schema = _RESOURCE_TYPE_SCHEMA.get(rtype)
    if not schema:
        raise HTTPException(status_code=400, detail=f"unknown resource type: {rtype!r}")

    path = (payload.get("path") or "").strip()
    if schema["needs_path"] and not path:
        raise HTTPException(status_code=400, detail=f"{rtype} requires a path")

    name = (payload.get("name") or "").strip() or None
    perms = (payload.get("permissions") or schema["permissions"][0]).strip()
    if perms not in schema["permissions"]:
        raise HTTPException(
            status_code=400,
            detail=f"{rtype} permissions must be one of {list(schema['permissions'])}",
        )

    raw_extras = payload.get("extras")
    if raw_extras is not None and not isinstance(raw_extras, dict):
        raise HTTPException(status_code=400, detail="extras must be a JSON object")
    extras = dict(raw_extras) if raw_extras else None

    if _config is None:
        raise HTTPException(status_code=503, detail="config not loaded")

    from . import db

    # Resources without a path use the type as the implicit path so the
    # UNIQUE(user_id, resource_type, resource_path) constraint still works
    # for one-per-type resources (feeds, money, location, etc.).
    storage_path = path or rtype

    add_kwargs: dict[str, object] = {
        "user_id": user["username"],
        "resource_type": rtype,
        "resource_path": storage_path,
        "display_name": name,
        "permissions": perms,
    }
    if extras is not None:
        add_kwargs["extras"] = extras

    with db.get_db(_config.db_path) as conn:
        rid = db.add_user_resource(conn, **add_kwargs)
    logger.info(
        "resource added user=%s id=%d type=%s path=%s",
        user["username"], rid, rtype, storage_path,
    )
    return {"ok": True, "id": rid}


@api_router.delete("/settings/resources/{resource_id}")
async def settings_delete_resource(
    resource_id: int,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
) -> dict:
    """Delete a DB-managed resource. TOML-declared resources cannot be removed here."""
    from fastapi import HTTPException

    if _config is None:
        raise HTTPException(status_code=503, detail="config not loaded")
    if resource_id <= 0:
        raise HTTPException(status_code=400, detail="resource_id must be positive")

    from . import db

    with db.get_db(_config.db_path) as conn:
        deleted = db.delete_user_resource(conn, user["username"], resource_id)
    if not deleted:
        # Either no row at this id, or the row belongs to another user.
        # Return 404 either way — user_id scoping is silent on purpose.
        raise HTTPException(status_code=404, detail="resource not found")
    return {"ok": True, "deleted": True}


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
        "conversation_token": getattr(b, "conversation_token", "") or "",
        "output": getattr(b, "output", "talk") or "talk",
        "components": dict(getattr(b, "components", {}) or {}),
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

    from . import db as _db
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

    raw_components = payload.get("components")
    if raw_components is None:
        components = {}
    elif isinstance(raw_components, dict):
        components = dict(raw_components)
    else:
        raise HTTPException(status_code=400, detail="components must be a JSON object")

    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be a boolean")

    return {
        "name": name,
        "cron": cron,
        "conversation_token": token,
        "output": output,
        "components": components,
        "enabled": enabled,
    }


@api_router.post("/settings/briefings")
async def settings_add_briefing(
    payload: dict,
    user: dict = Depends(_require_api_auth),
    _csrf: None = Depends(_verify_origin),
) -> dict:
    """Upsert a briefing for the current user.

    Body: ``{"name", "cron", "conversation_token"?, "output"?, "components"?, "enabled"?}``.
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
            SELECT lp.timestamp, lp.lat, lp.lon, lp.accuracy,
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
                SELECT lp.timestamp, lp.lat, lp.lon, lp.accuracy,
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
                SELECT lp.timestamp, lp.lat, lp.lon, lp.accuracy,
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
    hostname = _config.site.hostname or ""
    scheme = "https" if hostname else ""
    webhook_url = (
        f"{scheme}://{hostname}/webhooks/location?token=<token>"
        if hostname else "/webhooks/location?token=<token>"
    )
    loc = _config.location
    return {
        "webhook_url": webhook_url,
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

# Money web API — mounted when the optional ``money`` extra is installed.
try:
    from istota.money.routes import require_auth as _money_require_auth
    from istota.money.routes import router as _money_router
    from istota.money.routes import verify_origin as _money_verify_origin

    app.include_router(_money_router, prefix="/istota/money/api", tags=["money"])
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

# Serve SvelteKit build as static files (catch-all for SPA routing)
if _STATIC_DIR.is_dir():
    app.mount("/istota", StaticFiles(directory=str(_STATIC_DIR), html=True), name="web-static")
