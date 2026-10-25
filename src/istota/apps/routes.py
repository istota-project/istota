"""The apps web API, mounted at ``/istota/api/apps``.

This module holds the frame, the bridge's call endpoint and error reports;
the management routes (list, install, versions, grants) follow. Every route
resolves the user's apps context first, so a user with the module off gets a
404 from all of them.

The frame is a navigation the shell page starts, so it is checked by session
only; the call and error endpoints are fetches from the shell page and take
the origin check too. The frame's document comes from ``apps.db`` with the
version's stored CSP, which carries ``sandbox allow-scripts``, so the URL
opened in a tab is isolated as the iframe is.
"""

from __future__ import annotations

import asyncio
import json
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from istota.apps import bridge
from istota.apps import db as apps_db
from istota.apps._loader import AppsContext, UserNotFoundError, resolve_for_user
from istota.webui.request_guards import BodyError, read_bounded_body
from istota.webui.router_stubs import (  # noqa: F401
    make_get_user_context,
    require_auth,  # re-exported: `webui/app.py` keys `dependency_overrides` on it
    verify_origin,
)

router = APIRouter()

get_user_context = make_get_user_context(
    cache_attr="apps_initialised_dbs",
    resolve=resolve_for_user,
    ensure=lambda ctx, cfg: apps_db.init_db(ctx.db_path),
    not_found=UserNotFoundError,
)

_APP_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
#: Room in a call's body for the envelope and a write's other arguments,
#: beyond the largest value ``kv:own`` will store.
CALL_BODY_EXTRA = 64 * 1024
ERRORS_BODY_MAX = 16 * 1024
ERROR_TEXT_MAX = 4096
ERROR_KINDS = ("error", "rejection", "navigated", "shell")

FRAME_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "private, no-store",
}


def _bridge_error(code: str, message: str, *, retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=bridge.STATUS[code],
        headers=headers,
    )


def _refuse_constant(name: str):
    raise ValueError(f"{name} is not JSON")


def _parse_json(raw: bytes) -> object:
    return json.loads(raw, parse_constant=_refuse_constant)


@router.get("/{app_id}/v/{version}/frame")
async def api_app_frame(app_id: str, version: int, ctx: AppsContext = Depends(get_user_context)):
    if not _APP_ID.fullmatch(app_id):
        return JSONResponse({"error": "app not found"}, status_code=404)

    def load():
        with apps_db.connect(ctx.db_path) as conn:
            app = apps_db.get_app(conn, app_id)
            if app is None or app["current_version"] is None:
                return 404, None, None
            if app["current_version"] != version:
                return 409, None, None
            row = apps_db.get_version(conn, app_id, version)
            if row is None:
                return 404, None, None
            return 200, row["csp"], apps_db.get_document(conn, row["content_hash"])

    status, csp, document = await asyncio.to_thread(load)
    if status == 404:
        return JSONResponse({"error": "app not found"}, status_code=404)
    if status == 409:
        return JSONResponse({"error": "this version is not current; reload the app"}, status_code=409)
    if not csp or document is None:
        # Never the document without its policy.
        return JSONResponse({"error": "this version cannot be served"}, status_code=500)
    return Response(
        content=document,
        media_type="text/html; charset=utf-8",
        headers={"Content-Security-Policy": csp, **FRAME_HEADERS},
    )


@router.post("/{app_id}/v/{version}/call")
async def api_app_call(
    request: Request,
    app_id: str,
    version: int,
    ctx: AppsContext = Depends(get_user_context),
    _csrf: None = Depends(verify_origin),
):
    config = request.app.state.istota_config
    if not _APP_ID.fullmatch(app_id):
        return _bridge_error("not_found", "This app is not installed.")
    try:
        raw = await read_bounded_body(request, config.apps.kv_value_max_bytes + CALL_BODY_EXTRA)
    except BodyError as exc:
        return _bridge_error("quota" if exc.status == 413 else "bad_args", exc.message)
    try:
        body = _parse_json(raw)
    except ValueError:
        return _bridge_error("bad_args", "The call is not JSON.")
    if not isinstance(body, dict):
        return _bridge_error("bad_args", "The call is not an object.")

    def run():
        with apps_db.connect(ctx.db_path) as conn:
            return bridge.dispatch(
                config, ctx.user_id, conn,
                app_id=app_id, version=version,
                capability=body.get("capability"), op=body.get("op"), args=body.get("args", {}),
            )

    try:
        result = await asyncio.to_thread(run)
    except bridge.BridgeError as exc:
        return _bridge_error(exc.code, exc.message, retry_after=exc.retry_after)
    return JSONResponse({"result": result})


@router.post("/{app_id}/v/{version}/errors")
async def api_app_errors(
    request: Request,
    app_id: str,
    version: int,
    ctx: AppsContext = Depends(get_user_context),
    _csrf: None = Depends(verify_origin),
):
    config = request.app.state.istota_config
    if not _APP_ID.fullmatch(app_id):
        return JSONResponse({"error": "app not found"}, status_code=404)
    try:
        raw = await read_bounded_body(request, ERRORS_BODY_MAX)
    except BodyError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    try:
        body = _parse_json(raw)
    except ValueError:
        return JSONResponse({"error": "the report is not JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "the report is not an object"}, status_code=400)
    kind = body.get("kind")
    message = body.get("message")
    stack = body.get("stack", "")
    if kind not in ERROR_KINDS or not isinstance(message, str) or not isinstance(stack, str):
        return JSONResponse({"error": "a report has a kind, a message and a stack"}, status_code=400)

    def record() -> bool:
        with apps_db.connect(ctx.db_path) as conn:
            if apps_db.get_version(conn, app_id, version) is None:
                return False
            apps_db.insert_error(
                conn, app_id=app_id, version=version, kind=kind,
                message=message[:ERROR_TEXT_MAX], stack=stack[:ERROR_TEXT_MAX],
                keep=config.apps.errors_per_version,
            )
            conn.commit()
            return True

    if not await asyncio.to_thread(record):
        return JSONResponse({"error": "app not found"}, status_code=404)
    return Response(status_code=204)
