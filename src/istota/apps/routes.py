"""The apps web API, mounted at ``/istota/api/apps``.

Two halves: the frame, the bridge's call endpoint and error reports, which an
open app uses; and the management routes the Apps section uses (the list with
starter seeding and drafts, app detail, pending installs and approval,
versions, grants, uninstall, starter reinstall, storage). Every route resolves
the user's apps context first, so a user with the module off gets a 404 from
all of them, and every route that changes anything takes the origin check.

Literal first segments (``storage``, ``drafts``, ``starter``) are declared
before ``/{app_id}`` and are reserved as app ids by the manifest parser, so no
app is shadowed by them.

A route that writes ``apps.db`` opens its transaction with ``BEGIN
IMMEDIATE``, so the read that decides (the pending install, the current
version) and the write it leads to see one state. The starter seeding and
reinstall commit per app themselves, so they run with no transaction open.

The frame is a navigation the shell page starts, so it is checked by session
only; the call and error endpoints are fetches from the shell page and take
the origin check too. The frame's document comes from ``apps.db`` with the
version's stored CSP, which carries ``sandbox allow-scripts``, so the URL
opened in a tab is isolated as the iframe is.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from istota import db as framework_db
from istota.apps import bridge, install
from istota.apps import db as apps_db
from istota.apps._loader import AppsContext, UserNotFoundError, resolve_for_user
from istota.apps.capabilities import ALWAYS_GRANTED, Capability, describe, is_sensitive, split_capability
from istota.apps.document import DocumentError
from istota.apps.drafts import DraftError, DraftNotFound, list_drafts, read_draft
from istota.apps.kit import KitMissing, load_kit
from istota.apps.manifest import ManifestError
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


# -- the management routes ----------------------------------------------------

#: Who has had the starter apps seeded by this process, by app object.
STARTER_SEEN_ATTR = "apps_starter_seen"
MANAGEMENT_BODY_MAX = 16 * 1024
KIT_NOT_BUILT = "the apps kit is not built"
PENDING_GONE = "That install is no longer waiting. Reopen it from the app's folder."


def _error(text: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": text, **extra}, status_code=status)


def _not_found(message: str = "app not found") -> JSONResponse:
    return _error(message, 404)


def _begin_write(conn) -> None:
    conn.execute("BEGIN IMMEDIATE")


async def _read_json_object(request: Request) -> dict | JSONResponse:
    """The request's JSON object body, or the response refusing it."""
    try:
        raw = await read_bounded_body(request, MANAGEMENT_BODY_MAX)
    except BodyError as exc:
        return _error(exc.message, exc.status)
    try:
        body = _parse_json(raw)
    except ValueError:
        return _error("the request is not JSON", 400)
    if not isinstance(body, dict):
        return _error("the request is not an object", 400)
    return body


def _capability_row(text: str, scope: object) -> dict:
    """One capability as the approval screen and app detail show it."""
    family = split_capability(text)
    shown = Capability(family.name, scope if isinstance(scope, str) else family.scope)
    return {
        "capability": text,
        "scope": scope,
        "description": describe(shown),
        "sensitive": is_sensitive(shown),
    }


def _matches_source(files: dict[str, bytes], source: dict[str, str]) -> bool:
    """Whether a draft's files are what a stored version or pending install was built from.

    Only the files the build read are compared, so an unreferenced file in the
    folder changes nothing; a newly referenced one changes ``index.html``.
    """
    for path, text in source.items():
        raw = files.get(path)
        if raw is None:
            return False
        try:
            if raw.decode("utf-8") != text:
                return False
        except UnicodeDecodeError:
            return False
    return True


def _draft_state(config, user_id: str, conn, summary, apps: dict, pending: dict) -> str | None:
    """``new`` or ``changed`` for a draft "All apps" should offer, else None."""
    app = apps.get(summary.app_id)
    try:
        files = read_draft(config, user_id, summary.app_id)
    except DraftError:
        # Unreadable drafts are still offered; "Review install" says why.
        files = None
    if files is not None:
        if summary.app_id in pending:
            row = apps_db.get_pending(conn, pending[summary.app_id]["pending_id"])
            if row is not None and _matches_source(files, row["source"]):
                return None
        if app is not None and app["current_version"] is not None:
            version = apps_db.get_version(conn, summary.app_id, app["current_version"])
            if version is not None and _matches_source(files, version["source"]):
                return None
    return "changed" if app is not None else "new"


def _starter_state(conn, app_id: str, apps: dict) -> str:
    state = apps_db.get_starter_state(conn, app_id)
    if state is not None and state["dismissed_at"] is not None:
        return "dismissed"
    app = apps.get(app_id)
    if app is None:
        return "unavailable"
    return "installed" if app["origin"] == "starter" else "conflict"


def _kit_built() -> bool:
    try:
        load_kit()
    except KitMissing:
        return False
    return True


@router.get("/")
async def api_apps_list(request: Request, ctx: AppsContext = Depends(get_user_context)):
    config = request.app.state.istota_config
    seen: set | None = getattr(request.app.state, STARTER_SEEN_ATTR, None)
    if seen is None:
        seen = set()
        setattr(request.app.state, STARTER_SEEN_ATTR, seen)

    def load() -> dict:
        with apps_db.connect(ctx.db_path) as conn:
            if ctx.user_id not in seen:
                statuses = install.ensure_starter_apps(config, ctx.user_id, conn)
                # An unbuilt kit is retried on the next visit; anything else
                # waits for the next process, as the spec's seeding rule says.
                if "unavailable" not in statuses.values():
                    seen.add(ctx.user_id)
            app_rows = {row["app_id"]: row for row in apps_db.list_apps(conn)}
            pending = {row["app_id"]: row for row in apps_db.list_pending(conn)}
            apps = []
            for app_id, row in app_rows.items():
                version = None
                if row["current_version"] is not None:
                    version = apps_db.get_version(conn, app_id, row["current_version"])
                apps.append({
                    "app_id": app_id,
                    "name": row["name"],
                    "icon": row["icon"],
                    "description": row["description"],
                    "origin": row["origin"],
                    "current_version": row["current_version"],
                    "narrowest": version["narrowest"] if version else None,
                    "pending_id": pending[app_id]["pending_id"] if app_id in pending else None,
                    "ungranted": install.ungranted(conn, app_id),
                })
            drafts = []
            for summary in list_drafts(config, ctx.user_id):
                state = _draft_state(config, ctx.user_id, conn, summary, app_rows, pending)
                if state is not None:
                    drafts.append({
                        "app_id": summary.app_id,
                        "name": summary.name,
                        "has_manifest": summary.has_manifest,
                        "state": state,
                    })
            starter = [
                {"app_id": app_id, "state": _starter_state(conn, app_id, app_rows)}
                for app_id in install.STARTER_APPS
            ]
        return {
            "apps": apps,
            "pending": [
                {
                    "pending_id": row["pending_id"], "app_id": row["app_id"], "name": row["name"],
                    "origin": row["origin"], "size": row["size"], "created_at": row["created_at"],
                }
                for row in pending.values()
            ],
            "drafts": drafts,
            "starter": starter,
            "kit_built": _kit_built(),
        }

    return await asyncio.to_thread(load)


@router.get("/storage")
async def api_apps_storage(request: Request, ctx: AppsContext = Depends(get_user_context)):
    config = request.app.state.istota_config

    def load() -> dict:
        with apps_db.connect(ctx.db_path) as conn:
            app_rows = apps_db.list_apps(conn)
        rows = []
        with framework_db.get_db(config.db_path) as fw:
            for app in app_rows:
                kv_bytes = framework_db.kv_namespace_bytes(fw, ctx.user_id, install.kv_namespace(app["app_id"]))
                try:
                    data_bytes = bridge.data_folder_bytes(config, ctx.user_id, app["app_id"])
                except bridge.BridgeError:
                    # A link or a file where data/ should be: not measured, never followed.
                    data_bytes = None
                rows.append({
                    "app_id": app["app_id"], "name": app["name"],
                    "kv_bytes": kv_bytes, "data_bytes": data_bytes,
                })
        return {"apps": rows}

    return await asyncio.to_thread(load)


@router.post("/drafts/{app_id}/pending")
async def api_apps_create_pending(
    request: Request,
    app_id: str,
    ctx: AppsContext = Depends(get_user_context),
    _csrf: None = Depends(verify_origin),
):
    config = request.app.state.istota_config
    if not _APP_ID.fullmatch(app_id):
        return _not_found(f"There is no app folder named {app_id}.")

    def create() -> JSONResponse:
        with apps_db.connect(ctx.db_path) as conn:
            _begin_write(conn)
            app = apps_db.get_app(conn, app_id)
            if app is not None and app["origin"] == "starter":
                return _error(f"{app_id} is an app that ships with Istota; give the folder another id.", 409)
            try:
                pending_id = install.create_pending(config, ctx.user_id, conn, app_id, origin="draft")
            except DraftNotFound as exc:
                return _not_found(str(exc))
            except DraftError as exc:
                return JSONResponse({"errors": [str(exc)]}, status_code=422)
            except (ManifestError, DocumentError) as exc:
                return JSONResponse({"errors": list(exc.errors)}, status_code=422)
            except KitMissing as exc:
                return _error(KIT_NOT_BUILT, 503, reason=exc.reason)
            conn.commit()
        return JSONResponse({"pending_id": pending_id, "app_id": app_id}, status_code=201)

    return await asyncio.to_thread(create)


def _diff(old: dict[str, str] | None, new: dict[str, str]) -> list[dict]:
    """Per-file unified diff of ``new`` against ``old`` (None for a first install)."""
    old = old or {}
    out = []
    for path in sorted(set(old) | set(new)):
        before = old.get(path)
        after = new.get(path)
        if before == after:
            out.append({"path": path, "status": "unchanged", "diff": ""})
            continue
        status = "added" if before is None else "removed" if after is None else "changed"
        lines = difflib.unified_diff(
            _lines(before), _lines(after),
            fromfile="/dev/null" if before is None else f"a/{path}",
            tofile="/dev/null" if after is None else f"b/{path}",
        )
        out.append({"path": path, "status": status, "diff": "".join(lines)})
    return out


def _lines(text: str | None) -> list[str]:
    if text is None:
        return []
    return [line if line.endswith("\n") else line + "\n" for line in text.splitlines(keepends=True)]


@router.get("/{app_id}/pending/{pending_id}")
async def api_apps_get_pending(app_id: str, pending_id: str, ctx: AppsContext = Depends(get_user_context)):
    def load() -> dict | None:
        with apps_db.connect(ctx.db_path) as conn:
            pending = apps_db.get_pending(conn, pending_id)
            if pending is None or pending["app_id"] != app_id:
                return None
            manifest = pending["manifest"]
            app = apps_db.get_app(conn, app_id)
            current = None
            if app is not None and app["current_version"] is not None:
                current = apps_db.get_version(conn, app_id, app["current_version"])
            new = install.new_capabilities(conn, app_id, manifest)
        capabilities = []
        for text in manifest["capabilities"]:
            row = _capability_row(text, split_capability(text).scope)
            row["new"] = text in new
            row["always_granted"] = text in ALWAYS_GRANTED
            capabilities.append(row)
        return {
            "pending_id": pending_id,
            "app_id": app_id,
            "origin": pending["origin"],
            "created_at": pending["created_at"],
            "manifest": manifest,
            "narrowest": manifest["narrowest"],
            "reason": manifest["reason"],
            "current_version": current["version"] if current else None,
            "capabilities": capabilities,
            "new_capabilities": new,
            "one_button": not new,
            "size": len(pending["document"]),
            "warnings": pending["warnings"],
            "diff": _diff(current["source"] if current else None, pending["source"]),
        }

    body = await asyncio.to_thread(load)
    if body is None:
        return _not_found(PENDING_GONE)
    return body


@router.post("/{app_id}/pending/{pending_id}/approve")
async def api_apps_approve(
    request: Request,
    app_id: str,
    pending_id: str,
    ctx: AppsContext = Depends(get_user_context),
    _csrf: None = Depends(verify_origin),
):
    body = await _read_json_object(request)
    if isinstance(body, JSONResponse):
        return body
    ack = body.get("new_capabilities")
    if not isinstance(ack, list) or not all(isinstance(cap, str) for cap in ack):
        return _error("new_capabilities is a list of the capabilities the screen showed as new", 400)

    def run() -> JSONResponse:
        with apps_db.connect(ctx.db_path) as conn:
            _begin_write(conn)
            pending = apps_db.get_pending(conn, pending_id)
            if pending is None or pending["app_id"] != app_id:
                return _not_found(PENDING_GONE)
            try:
                version = install.approve(conn, pending_id, new_capabilities_ack=ack)
            except install.NotFound as exc:
                return _not_found(str(exc))
            except install.StaleApproval as exc:
                return _error("stale", 409, message=str(exc), new_capabilities=exc.expected)
            conn.commit()
        return JSONResponse({"app_id": app_id, "version": version})

    return await asyncio.to_thread(run)


@router.delete("/{app_id}/pending/{pending_id}")
async def api_apps_delete_pending(
    app_id: str,
    pending_id: str,
    ctx: AppsContext = Depends(get_user_context),
    _csrf: None = Depends(verify_origin),
):
    def run() -> bool:
        with apps_db.connect(ctx.db_path) as conn:
            _begin_write(conn)
            pending = apps_db.get_pending(conn, pending_id)
            if pending is None or pending["app_id"] != app_id:
                return False
            apps_db.delete_pending(conn, pending_id)
            conn.commit()
        return True

    if not await asyncio.to_thread(run):
        return _not_found(PENDING_GONE)
    return Response(status_code=204)


@router.post("/starter/{app_id}/reinstall")
async def api_apps_reinstall_starter(
    request: Request,
    app_id: str,
    ctx: AppsContext = Depends(get_user_context),
    _csrf: None = Depends(verify_origin),
):
    config = request.app.state.istota_config

    def run() -> str | None:
        with apps_db.connect(ctx.db_path) as conn:
            try:
                return install.reinstall_starter(config, ctx.user_id, conn, app_id)
            except install.NotFound:
                return None

    status = await asyncio.to_thread(run)
    if status is None:
        return _not_found(f"{app_id} is not an app that ships with Istota.")
    if status == "unavailable":
        return _error(KIT_NOT_BUILT, 503)
    if status == "conflict":
        return _error(f"An app you installed already uses the id {app_id}.", 409)
    if status == "failed":
        return _error(f"{app_id} could not be installed.", 500)
    return {"app_id": app_id, "status": status}


@router.get("/{app_id}")
async def api_apps_detail(app_id: str, ctx: AppsContext = Depends(get_user_context)):
    def load() -> dict | None:
        with apps_db.connect(ctx.db_path) as conn:
            app = apps_db.get_app(conn, app_id)
            if app is None or app["current_version"] is None:
                return None
            version = apps_db.get_version(conn, app_id, app["current_version"])
            grants = apps_db.get_grants(conn, app_id, app["current_version"])
            ungranted = install.ungranted(conn, app_id)
            pending = [row for row in apps_db.list_pending(conn) if row["app_id"] == app_id]
        if version is None:
            return None
        return {
            "app_id": app_id,
            "name": app["name"],
            "icon": app["icon"],
            "description": app["description"],
            "origin": app["origin"],
            "current_version": app["current_version"],
            "installed_at": version["installed_at"],
            "approved_at": version["approved_at"],
            "manifest": version["manifest"],
            "narrowest": version["narrowest"],
            "reason": version["manifest"].get("reason", ""),
            "warnings": version["warnings"],
            "pending_id": pending[0]["pending_id"] if pending else None,
            "grants": [_capability_row(cap, scope) for cap, scope in grants.items()],
            "ungranted": [_capability_row(cap, split_capability(cap).scope) for cap in ungranted],
        }

    if not _APP_ID.fullmatch(app_id):
        return _not_found()
    body = await asyncio.to_thread(load)
    if body is None:
        return _not_found()
    return body


@router.delete("/{app_id}")
async def api_apps_uninstall(
    request: Request,
    app_id: str,
    delete_data: str = "",
    ctx: AppsContext = Depends(get_user_context),
    _csrf: None = Depends(verify_origin),
):
    config = request.app.state.istota_config
    wipe = delete_data.strip().lower() in ("1", "true", "yes")

    def run() -> JSONResponse | None:
        with apps_db.connect(ctx.db_path) as conn:
            _begin_write(conn)
            try:
                install.uninstall(config, ctx.user_id, conn, app_id, delete_data=wipe)
            except install.NotFound:
                return _not_found()
            except install.UninstallError as exc:
                return _error(str(exc), 500)
            conn.commit()
        return None

    if not _APP_ID.fullmatch(app_id):
        return _not_found()
    refused = await asyncio.to_thread(run)
    if refused is not None:
        return refused
    return Response(status_code=204)


@router.get("/{app_id}/versions")
async def api_apps_versions(app_id: str, ctx: AppsContext = Depends(get_user_context)):
    def load() -> dict | None:
        with apps_db.connect(ctx.db_path) as conn:
            app = apps_db.get_app(conn, app_id)
            if app is None:
                return None
            versions = []
            for row in apps_db.list_versions(conn, app_id):
                versions.append({
                    "version": row["version"],
                    "current": row["version"] == app["current_version"],
                    "installed_at": row["installed_at"],
                    "approved_at": row["approved_at"],
                    "narrowest": row["narrowest"],
                    "capabilities": row["manifest"]["capabilities"],
                    "grants": sorted(apps_db.get_grants(conn, app_id, row["version"])),
                })
        return {"app_id": app_id, "current_version": app["current_version"], "versions": versions}

    body = await asyncio.to_thread(load)
    if body is None:
        return _not_found()
    return body


@router.post("/{app_id}/versions/{version}/current")
async def api_apps_make_current(
    app_id: str,
    version: int,
    ctx: AppsContext = Depends(get_user_context),
    _csrf: None = Depends(verify_origin),
):
    def run() -> str | None:
        with apps_db.connect(ctx.db_path) as conn:
            _begin_write(conn)
            try:
                install.rollback(conn, app_id, version)
            except install.NotFound as exc:
                return str(exc)
            conn.commit()
        return None

    missing = await asyncio.to_thread(run)
    if missing is not None:
        return _not_found(missing)
    return {"app_id": app_id, "current_version": version}


@router.delete("/{app_id}/versions/{version}")
async def api_apps_delete_version(
    app_id: str,
    version: int,
    ctx: AppsContext = Depends(get_user_context),
    _csrf: None = Depends(verify_origin),
):
    def run() -> JSONResponse | None:
        with apps_db.connect(ctx.db_path) as conn:
            _begin_write(conn)
            try:
                install.delete_version(conn, app_id, version)
            except install.NotFound as exc:
                return _not_found(str(exc))
            except install.CurrentVersionError as exc:
                return _error(str(exc), 409)
            conn.commit()
        return None

    refused = await asyncio.to_thread(run)
    if refused is not None:
        return refused
    return Response(status_code=204)


@router.post("/{app_id}/grants")
async def api_apps_grant(
    request: Request,
    app_id: str,
    ctx: AppsContext = Depends(get_user_context),
    _csrf: None = Depends(verify_origin),
):
    config = request.app.state.istota_config
    body = await _read_json_object(request)
    if isinstance(body, JSONResponse):
        return body
    capability = body.get("capability")
    scope = body.get("scope")
    if not isinstance(capability, str) or not (scope is None or isinstance(scope, str)):
        return _error("a grant names a capability and, for a folder, its scope", 400)

    def run() -> JSONResponse:
        with apps_db.connect(ctx.db_path) as conn:
            _begin_write(conn)
            app = apps_db.get_app(conn, app_id)
            if app is None or app["current_version"] is None:
                return _not_found()
            version = app["current_version"]
            try:
                install.grant(conn, app_id, version, capability, scope, bot_dir=config.bot_dir_name)
            except install.NotFound:
                return _not_found()
            except install.GrantError as exc:
                return _error(str(exc), 422)
            stored = apps_db.get_grants(conn, app_id, version)[capability]
            conn.commit()
        return JSONResponse({"capability": capability, "scope": stored})

    return await asyncio.to_thread(run)
