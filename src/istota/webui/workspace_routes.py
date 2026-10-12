"""Authenticated browsing of the caller's current workspace places."""

import asyncio
import errno
import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from istota import db
from istota.webui.router_stubs import require_auth
from istota.workspace.listing import ListingError, list_dir
from istota.workspace.places import list_places
from istota.workspace.reach import deployment_notes, place_reach

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
