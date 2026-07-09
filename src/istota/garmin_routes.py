"""Module-agnostic Garmin auth routes.

Garmin is a cross-module connected service: the health module syncs daily
summaries and the location module imports GPS tracks, both off one shared
token blob in the framework ``secrets`` table (``service="garmin"``, keyed
on ``user_id``). Its *auth* surface therefore lives here — not under the
health router — so a user who has opted out of the health module can still
connect Garmin (for location) via Settings → Connected services.

What stays health-owned: ``/garmin/sync`` (daily-summary sync into the
health ``stats`` table) remains on the health router — it is a health
*consumer*, not auth.

The four routes here mirror the ones they replaced verbatim except for the
dropped ``HealthContext`` dependency (which had gated them on the health
module being enabled). ``require_auth`` / ``verify_origin`` are placeholder
dependencies overridden by ``web_app`` (same pattern as the health / money
routers), so session auth + CSRF are wired identically.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from istota.health import garmin as health_garmin


router = APIRouter()


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


def _user_id(request: Request) -> str:
    user = request.session.get("user") if hasattr(request, "session") else None
    if not isinstance(user, dict) or not user.get("username"):
        raise HTTPException(401, "unauthorized")
    return user["username"]


def _framework_db_path(request: Request) -> Path:
    """Framework istota.db path — where the encrypted ``secrets`` table
    (and therefore Garmin tokens) lives."""
    cfg = getattr(request.app.state, "istota_config", None)
    db_path = getattr(cfg, "db_path", None) if cfg else None
    if not db_path:
        raise HTTPException(503, "framework db_path unavailable")
    return Path(db_path)


@router.get("/status")
async def api_garmin_status(
    request: Request, _user: dict = Depends(require_auth),
):
    user_id = _user_id(request)
    db_path = _framework_db_path(request)

    def _query():
        return health_garmin.get_status(db_path, user_id)
    return await asyncio.to_thread(_query)


@router.post("/connect")
async def api_garmin_connect(
    request: Request,
    _csrf: None = Depends(verify_origin),
    _user: dict = Depends(require_auth),
):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    email = body.get("email")
    password = body.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        return JSONResponse(
            {"error": "email and password are required"}, status_code=400,
        )
    user_id = _user_id(request)
    db_path = _framework_db_path(request)

    def _do():
        return health_garmin.connect(
            db_path, user_id=user_id, email=email, password=password,
        )

    try:
        return await asyncio.to_thread(_do)
    except health_garmin.GarminAuthError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)
    except health_garmin.GarminNotInstalled as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=503)


@router.post("/mfa")
async def api_garmin_mfa(
    request: Request,
    _csrf: None = Depends(verify_origin),
    _user: dict = Depends(require_auth),
):
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("code"), str):
        return JSONResponse({"error": "code is required"}, status_code=400)
    user_id = _user_id(request)
    db_path = _framework_db_path(request)

    def _do():
        return health_garmin.complete_mfa(
            db_path, user_id=user_id, code=body["code"],
        )

    try:
        return await asyncio.to_thread(_do)
    except health_garmin.GarminAuthError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)


@router.post("/disconnect")
async def api_garmin_disconnect(
    request: Request,
    _csrf: None = Depends(verify_origin),
    _user: dict = Depends(require_auth),
):
    user_id = _user_id(request)
    db_path = _framework_db_path(request)

    def _do():
        return health_garmin.disconnect(db_path, user_id=user_id)

    return await asyncio.to_thread(_do)
