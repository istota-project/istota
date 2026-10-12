"""Authenticated browsing of the caller's current workspace places."""

import asyncio
import errno
import logging
from dataclasses import asdict
from pathlib import PurePosixPath
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from istota import db
from istota.webui import nextcloud_files
from istota.credentials.google_scopes import levels_from_scopes
from istota.credentials.google_tokens import access_token_for
from istota.google import drive
from istota.nextcloud import OcsError, shares
from istota.webui.chat_files import servable_roots
from istota.workspace.pins import validate_pin, workspace_pin_kind
from istota.webui.router_stubs import require_auth, verify_origin
from istota.workspace.listing import ListingError, list_dir, list_file
from istota.workspace.places import list_places
from istota.workspace.reach import deployment_notes, place_reach
from istota.workspace import config_files, text_files
from istota.workspace.writes import delete_entry, make_folder, write_new_file

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/places")
async def api_workspace_places(request: Request, user: dict = Depends(require_auth)):
    config = getattr(request.app.state, "istota_config", None)
    if config is None:
        return JSONResponse({"error": "config unavailable"}, status_code=503)

    def query():
        with db.get_db(config.db_path) as conn:
            places = list_places(config, conn, user["username"])
            reach = place_reach(config, conn, user["username"], places)
            payload = [{
                "id": place.id, "kind": place.kind, "label": place.label,
                "browsable": place.browsable, "view_root": place.view_root,
                "reach": asdict(reach[place.id]),
            } for place in places]
        return {"places": payload, "notes": deployment_notes(config)}

    return await asyncio.to_thread(query)


@router.get("/list")
async def api_workspace_list(
    request: Request, place: str, path: str = "", user: dict = Depends(require_auth),
):
    config = getattr(request.app.state, "istota_config", None)
    if config is None:
        return JSONResponse({"error": "config unavailable"}, status_code=503)
    parts = [part for part in path.split("/") if part]

    def query():
        if place == "nextcloud":
            return nextcloud_files.list_folder(config, user["username"], parts)
        with db.get_db(config.db_path) as conn:
            places = list_places(config, conn, user["username"])
            selected = next((p for p in places if p.id == place), None)
            if selected is None or not selected.browsable or selected.root is None:
                raise ListingError(404, "Place not found.")
            file_root = selected.root.is_file()
            if file_root:
                if parts:
                    raise ListingError(404, "Folder not found.")
                listing = list_file(selected.root)
            else:
                listing = list_dir(selected.root, parts)
        payload = {"place": selected.id, "path": "/".join(parts), **asdict(listing)}
        if file_root:
            payload["entries"][0]["view_path"] = selected.view_root
        if config.storage_is_nextcloud and selected.kind == "home":
            directory = "/".join([selected.view_root, *parts])
            _add_nextcloud_shares(config, user["username"], directory, payload)
        return payload

    try:
        return await asyncio.to_thread(query)
    except (ListingError, OSError) as exc:
        return _filesystem_error(config, exc, place, write=False)


def _add_nextcloud_shares(config, username, directory, payload):
    entries = {}
    for entry in payload["entries"]:
        entry.update(share=None, nextcloud_url=None)
        if entry["kind"] in {"dir", "file"}:
            entries[f"{directory}/{entry['name']}"] = entry
    try:
        rows = shares.list_shares(config, path=directory, subfiles=True, timeout=5)
    except OcsError:
        logger.warning("Nextcloud share state is unavailable place=home")
        payload["notes"] = ["Nextcloud share state is unavailable."]
        return
    kinds = {0: "user", 1: "group", 3: "link"}
    for row in rows:
        entry = entries.get(row.get("path"))
        kind = kinds.get(row.get("share_type"))
        if entry is None or kind is None:
            continue
        if entry["share"] is None:
            entry["share"] = {"kind": kind, "count": 1}
        else:
            entry["share"]["count"] += 1
        target = row.get("file_target")
        if (kind == "user" and row.get("share_with") == username
                and isinstance(target, str) and target.startswith("/") and "\0" not in target):
            query = urlencode({"dir": str(PurePosixPath(target).parent)})
            entry["nextcloud_url"] = f"{config.nextcloud.url.rstrip('/')}/apps/files/files?{query}"


class _CappedMultipartParser(MultiPartParser):
    """Reject at the incoming stream, before UploadFile spools the whole body."""

    def __init__(self, headers, stream, max_bytes):
        super().__init__(headers, stream, max_files=1, max_fields=2, max_part_size=4096)
        self.max_bytes = max_bytes
        self.file_bytes = 0
        self.over_cap = False
        self.finished = False

    def on_end(self):
        self.finished = True

    async def parse(self):
        try:
            form = await super().parse()
            if not self.finished:
                raise MultiPartException("Incomplete upload")
            return form
        except BaseException:
            for file in self._files_to_close_on_error:
                file.close()
            raise

    def on_part_data(self, data, start, end):
        if self._current_part.file is not None:
            self.file_bytes += end - start
            if self.file_bytes > self.max_bytes:
                self.over_cap = True
                # MultiPartException makes Starlette close its temporary files.
                raise MultiPartException("file too large")
        super().on_part_data(data, start, end)


def _home_root(config, username, place):
    with db.get_db(config.db_path) as conn:
        home = next((p for p in list_places(config, conn, username) if p.id == place == "home"), None)
    if home is None or home.root is None:
        raise ListingError(404, "Place not found.")
    return home.root


def _filesystem_error(config, exc, place, *, write):
    if isinstance(exc, ListingError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    logger.warning("Workspace %s failed place=%s errno=%s", "write" if write else "listing", place, exc.errno)
    status = 500
    message = "Could not write to this folder." if write else "Could not read this folder."
    if config.storage_is_nextcloud and exc.errno in {errno.ENOTCONN, errno.EIO}:
        status, message = 503, "Your Nextcloud folder is not reachable right now."
    elif exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
        status, message = 404, "Folder not found."
    elif exc.errno == errno.EEXIST:
        status, message = 409, "A file or folder already exists."
    elif exc.errno in {errno.ENOSPC, errno.EDQUOT}:
        status, message = 507, "Workspace storage is full."
    return JSONResponse({"error": message}, status_code=status)


@router.post("/upload")
async def api_workspace_upload(
    request: Request, user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = getattr(request.app.state, "istota_config", None)
    if config is None:
        return JSONResponse({"error": "config unavailable"}, status_code=503)
    cap = config.web.workspace.max_upload_mb * 1024 * 1024
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data;"):
        return JSONResponse({"error": "Invalid upload."}, status_code=422)
    parser = _CappedMultipartParser(request.headers, request.stream(), cap)
    try:
        form = await parser.parse()
    except (MultiPartException, ValueError):
        if parser.over_cap:
            return JSONResponse({"error": f"file exceeds {config.web.workspace.max_upload_mb} MB"}, status_code=413)
        return JSONResponse({"error": "Invalid upload."}, status_code=422)
    try:
        file = form.get("file")
        place, path = form.get("place"), form.get("path", "")
        if not isinstance(file, UploadFile) or not isinstance(place, str) or not isinstance(path, str):
            return JSONResponse({"error": "Invalid upload."}, status_code=422)
        parts = [part for part in path.split("/") if part]

        def write():
            root = _home_root(config, user["username"], place)
            chunks = iter(lambda: file.file.read(1024 * 1024), b"")
            name = write_new_file(root, parts, file.filename or "upload", chunks, max_bytes=cap)
            return {"name": name, "path": "/".join([*parts, name]), "size": file.size}

        try:
            return await asyncio.to_thread(write)
        except (ListingError, OSError) as exc:
            return _filesystem_error(config, exc, "home", write=True)
    finally:
        await form.close()


@router.post("/folders")
async def api_workspace_folder(
    request: Request, user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = getattr(request.app.state, "istota_config", None)
    if config is None:
        return JSONResponse({"error": "config unavailable"}, status_code=503)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or any(not isinstance(body.get(key), str) for key in ("place", "path", "name")) or not body["name"].strip():
        return JSONResponse({"error": "Invalid folder."}, status_code=422)
    parts = [part for part in body["path"].split("/") if part]

    def write():
        root = _home_root(config, user["username"], body["place"])
        name = make_folder(root, parts, body["name"])
        return {"path": "/".join([*parts, name])}

    try:
        return await asyncio.to_thread(write)
    except (ListingError, OSError) as exc:
        return _filesystem_error(config, exc, "home", write=True)


def _no_workspace(config):
    if config is None:
        return JSONResponse({"error": "config unavailable"}, status_code=503)
    if not config.has_workspace:
        return JSONResponse({"error": "No workspace is configured."}, status_code=503)
    return None


_CONFIG_FILE_NOT_FOUND = {"error": "File not found."}

# One body per save outcome. `locked` is 423 rather than the room-memory
# editor's 409, so a client can tell "try again" from "reload first".
_SAVE_REFUSALS = {
    "conflict": (409, "This file changed since it was loaded."),
    "locked": (423, "This file is being written right now. Try again."),
    "invalid": (422, "The daemon would not load this file as written."),
    "too_large": (413, "This file is too large."),
    "failed": (500, "Could not save this file."),
}


@router.get("/config-files")
async def api_workspace_config_files(request: Request, user: dict = Depends(require_auth)):
    config = getattr(request.app.state, "istota_config", None)
    refusal = _no_workspace(config)
    if refusal is not None:
        return refusal
    files = await asyncio.to_thread(config_files.list_files, config, user["username"])
    return {"files": files}


@router.get("/config-files/{name:path}")
async def api_workspace_config_file(
    request: Request, name: str, user: dict = Depends(require_auth),
):
    config = getattr(request.app.state, "istota_config", None)
    refusal = _no_workspace(config)
    if refusal is not None:
        return refusal

    def query():
        file = config_files.lookup(config, user["username"], name)
        if file is None:
            return None, None
        return file, config_files.read(config, user["username"], file)

    file, text = await asyncio.to_thread(query)
    if file is None:
        return JSONResponse(_CONFIG_FILE_NOT_FOUND, status_code=404)
    if text is None:
        return JSONResponse({"error": "Could not read this file."}, status_code=500)
    return {"content": text, "revision": text_files.revision(text), "exists": bool(text.strip())}


@router.put("/config-files/{name:path}")
async def api_workspace_config_file_save(
    request: Request, name: str,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = getattr(request.app.state, "istota_config", None)
    refusal = _no_workspace(config)
    if refusal is not None:
        return refusal
    try:
        body = await request.json()
    except ValueError:
        body = None
    # The revision is required: a client that may omit it can silently
    # overwrite, which is the failure the revision exists to stop.
    if (not isinstance(body, dict) or not isinstance(body.get("content"), str)
            or not isinstance(body.get("revision"), str)):
        return JSONResponse({"error": "content and revision are required"}, status_code=400)

    def write():
        file = config_files.lookup(config, user["username"], name)
        if file is None:
            return None, None
        return file, config_files.save(config, user["username"], file, body["content"], body["revision"])

    file, outcome = await asyncio.to_thread(write)
    if file is None:
        return JSONResponse(_CONFIG_FILE_NOT_FOUND, status_code=404)
    if outcome.status == "ok":
        payload = {"revision": outcome.revision}
        if file.name == "CRON.md":
            # The scheduler's sync reads the file; the web does not touch
            # `scheduled_jobs`, so the jobs change at the next sync.
            payload["applies"] = "next_sync"
        return payload
    status, message = _SAVE_REFUSALS[outcome.status]
    payload = {"error": message, "code": outcome.status}
    if outcome.status == "invalid":
        payload["errors"] = list(outcome.errors)
    if outcome.status == "too_large":
        payload["max_bytes"] = file.max_bytes
    return JSONResponse(payload, status_code=status)
@router.delete("/files")
async def api_workspace_delete(
    request: Request, place: str, path: str, name: str,
    user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = getattr(request.app.state, "istota_config", None)
    if config is None:
        return JSONResponse({"error": "config unavailable"}, status_code=503)
    parts = [part for part in path.split("/") if part]

    def write():
        root = _home_root(config, user["username"], place)
        delete_entry(root, parts, name)
        return {"status": "ok"}

    try:
        return await asyncio.to_thread(write)
    except (ListingError, OSError) as exc:
        return _filesystem_error(config, exc, "home", write=True)


@router.get("/drive/list")
async def api_workspace_drive_list(
    request: Request, folder: str = "root", page_token: str | None = None,
    user: dict = Depends(require_auth),
):
    config = getattr(request.app.state, "istota_config", None)
    if config is None:
        return JSONResponse({"error": "config unavailable"}, status_code=503)

    def query():
        drive.validate_folder(folder)
        with db.get_db(config.db_path) as conn:
            scopes = db.get_google_scopes(conn, user["username"])
            if not config.google_workspace.enabled or levels_from_scopes(scopes or []).get("drive") not in {"readonly", "full"}:
                raise drive.DriveError(409, "drive_not_granted")
            token = access_token_for(conn, config, user["username"])
        if token is None:
            raise drive.DriveError(502, drive.RECONNECT_MESSAGE)
        items, next_page = drive.list_folder(token, folder, page_token)
        return {"items": [asdict(item) for item in items], "next_page_token": next_page}

    try:
        return await asyncio.to_thread(query)
    except drive.DriveError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status)


@router.get("/pins")
async def api_workspace_pins(request: Request, user: dict = Depends(require_auth)):
    config = getattr(request.app.state, "istota_config", None)
    if config is None:
        return JSONResponse({"error": "config unavailable"}, status_code=503)

    def query():
        with db.get_db(config.db_path) as conn:
            roots = servable_roots(config, conn, user["username"])
            pins = db.list_pins(conn, user["username"])
            for pin in pins:
                pin["available"] = True
                pin["is_dir"] = pin["mime_type"] == "application/vnd.google-apps.folder"
                if pin["provider"] == "workspace":
                    try:
                        pin["is_dir"] = workspace_pin_kind(config, user["username"], pin["ref"], roots)
                    except (ListingError, OSError):
                        pin["available"] = False
            return {"pins": pins}

    return await asyncio.to_thread(query)


@router.put("/pins")
async def api_workspace_pin_put(
    request: Request, user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = getattr(request.app.state, "istota_config", None)
    if config is None:
        return JSONResponse({"error": "config unavailable"}, status_code=503)
    try:
        body = await request.json()
    except ValueError:
        body = None

    def write():
        with db.get_db(config.db_path) as conn:
            roots = servable_roots(config, conn, user["username"])
            values, is_dir = validate_pin(config, user["username"], body, roots)
            pin = db.add_pin(conn, user["username"], **values)
            if pin is None:
                raise ListingError(409, "pin_limit")
            return {"pin": {**pin, "available": True, "is_dir": is_dir}}

    try:
        return await asyncio.to_thread(write)
    except (ListingError, OSError) as exc:
        return _filesystem_error(config, exc, "pins", write=True)


@router.delete("/pins/{pin_id}")
async def api_workspace_pin_delete(
    request: Request, pin_id: int, user: dict = Depends(require_auth), _csrf: None = Depends(verify_origin),
):
    config = getattr(request.app.state, "istota_config", None)
    if config is None:
        return JSONResponse({"error": "config unavailable"}, status_code=503)

    def write():
        with db.get_db(config.db_path) as conn:
            return db.delete_pin(conn, user["username"], pin_id)

    if not await asyncio.to_thread(write):
        return JSONResponse({"error": "Pin not found."}, status_code=404)
    return {"status": "ok"}
