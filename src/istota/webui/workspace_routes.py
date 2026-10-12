"""Authenticated browsing of the caller's current workspace places."""

import asyncio
import errno
import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from istota import db
from istota.webui.router_stubs import require_auth, verify_origin
from istota.workspace.listing import ListingError, list_dir
from istota.workspace.places import list_places
from istota.workspace.reach import deployment_notes, place_reach
from istota.workspace.writes import make_folder, write_new_file

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
        with db.get_db(config.db_path) as conn:
            places = list_places(config, conn, user["username"])
            selected = next((p for p in places if p.id == place), None)
            if selected is None or not selected.browsable or selected.root is None:
                raise ListingError(404, "Place not found.")
            try:
                listing = list_dir(selected.root, parts)
            except OSError as exc:
                logger.warning("Workspace listing failed place=%s errno=%s", selected.id, exc.errno)
                status, message = 500, "Could not read this folder."
                if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                    status, message = 404, "Folder not found."
                elif exc.errno == errno.EEXIST:
                    status, message = 409, "A file or folder already exists."
                elif exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                    status, message = 507, "Workspace storage is full."
                raise ListingError(status, message) from exc
        return {"place": selected.id, "path": "/".join(parts), **asdict(listing)}

    try:
        return await asyncio.to_thread(query)
    except ListingError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status)


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


def _write_error(exc):
    if isinstance(exc, ListingError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    logger.warning("Workspace write failed place=home errno=%s", exc.errno)
    status, message = 500, "Could not write to this folder."
    if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
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
            return _write_error(exc)
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
        return _write_error(exc)
