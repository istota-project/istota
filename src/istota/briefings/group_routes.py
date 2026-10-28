"""Group definition and edition HTTP adapters over `group_service`."""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from istota.briefings import group_service
from istota.groups.policy import GroupError
from istota.groups.routes import _payload, _run
from istota.webui.router_stubs import require_auth, verify_origin

router = APIRouter()
DEFINITION_FIELDS = {"revision": (int,), "name": (str,), "title": (str,), "cron": (str,),
                     "timezone": (str,), "enabled": (bool,), "generation_mode": (str,),
                     "generation": (dict,), "channel": (str,), "excluded_user_ids": (list,)}
PREFERENCE_FIELDS = {"outbound_muted": (bool,), "in_app_muted": (bool,)}


def _download(conn, config, actor, group_id, name, run_id):
    caps, definition = group_service.access(conn, config, actor, group_id, name, deleted_ok=True)
    run = group_service.edition(conn, config, definition, caps, run_id)
    if run["state"] not in ("published", "preview_ready"):
        raise GroupError(404, "edition_unavailable", "Edition is not ready")
    return Response(run["body_md"], media_type="text/markdown", headers={
        "Content-Disposition": f'attachment; filename="briefing-{run_id}.md"'})


@router.get("/groups/{group_id}/briefing-resources")
async def resources(group_id: str, request: Request, q: str = Query("", max_length=1000),
                    user: dict = Depends(require_auth)):
    return await _run(request, user, group_service.resources, group_id, q, read=True)


@router.get("/briefings/group-editions")
async def group_editions(request: Request, group: str | None = Query(None, max_length=200),
                         limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                         user: dict = Depends(require_auth)):
    return await _run(request, user, group_service.group_editions, group_id=group, limit=limit, offset=offset, read=True)


@router.get("/briefings/targets")
async def targets(request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, group_service.targets, read=True)


@router.get("/groups/{group_id}/briefings")
@router.get("/groups/{group_id}/briefings/{name}")
async def definitions(group_id: str, request: Request, name: str = None, user: dict = Depends(require_auth)):
    return await _run(request, user, group_service.definitions, group_id, name, read=True)


@router.post("/groups/{group_id}/briefings")
@router.put("/groups/{group_id}/briefings/{name}")
async def save_definition(group_id: str, request: Request, name: str = None, user: dict = Depends(require_auth),
                          _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, DEFINITION_FIELDS, {"revision", "name", "generation", "generation_mode"})
    return await _run(request, user, group_service.save, group_id, payload, name)


@router.delete("/groups/{group_id}/briefings/{name}")
async def delete_definition(group_id: str, name: str, request: Request, user: dict = Depends(require_auth),
                            _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, {"revision": (int,)}, {"revision"})
    return await _run(request, user, group_service.delete, group_id, name, payload["revision"])


@router.get("/groups/{group_id}/briefings/{name}/audience")
async def audience(group_id: str, name: str, request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, group_service.audience, group_id, name, read=True)


@router.get("/groups/{group_id}/briefings/{name}/runs")
async def runs(group_id: str, name: str, request: Request, limit: int = Query(20, ge=1, le=100),
               offset: int = Query(0, ge=0), user: dict = Depends(require_auth)):
    return await _run(request, user, group_service.runs, group_id, name, limit=limit, offset=offset, read=True)


@router.post("/groups/{group_id}/briefings/{name}/runs")
async def enqueue(group_id: str, name: str, request: Request, user: dict = Depends(require_auth),
                  _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, {"request_id": (str,), "mode": (str,)}, {"request_id", "mode"})
    return await _run(request, user, group_service.runs, group_id, name, payload=payload)


@router.get("/groups/{group_id}/briefings/{name}/runs/{run_id}")
async def edition(group_id: str, name: str, run_id: int, request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, group_service.read_edition, group_id, name, run_id, read=True)


@router.get("/groups/{group_id}/briefings/{name}/runs/{run_id}/download")
async def download(group_id: str, name: str, run_id: int, request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, _download, group_id, name, run_id, read=True)


@router.get("/groups/{group_id}/briefing-delivery")
@router.get("/groups/{group_id}/briefings/{name}/delivery")
async def preferences(group_id: str, request: Request, name: str = None, user: dict = Depends(require_auth)):
    return await _run(request, user, group_service.preferences, group_id, name, read=True)


@router.put("/groups/{group_id}/briefing-delivery")
@router.put("/groups/{group_id}/briefings/{name}/delivery")
async def set_preferences(group_id: str, request: Request, name: str = None, user: dict = Depends(require_auth),
                          _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, PREFERENCE_FIELDS, set(PREFERENCE_FIELDS))
    return await _run(request, user, group_service.preferences, group_id, name, payload=payload)


@router.get("/groups/{group_id}/briefings/{name}/runs/{run_id}/delivery")
async def delivery(group_id: str, name: str, run_id: int, request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, group_service.read_edition, group_id, name, run_id, delivery=True, read=True)


@router.post("/groups/{group_id}/briefings/{name}/runs/{run_id}/retry")
async def retry(group_id: str, name: str, run_id: int, request: Request, user: dict = Depends(require_auth),
                _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, {"acknowledge_duplicates": (bool,)}, set())
    return await _run(request, user, group_service.read_edition, group_id, name, run_id, retry=payload)


@router.get("/groups/{group_id}/briefings/{name}/sources/{source_id}/approval")
async def approval(group_id: str, name: str, source_id: str, request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, group_service.approval, group_id, name, source_id, read=True)


@router.put("/groups/{group_id}/briefings/{name}/sources/{source_id}/approval")
async def approve(group_id: str, name: str, source_id: str, request: Request, user: dict = Depends(require_auth),
                  _csrf: None = Depends(verify_origin)):
    fields = {"expected_definition_revision": (int,), "expected_grant_revision": (int,), "acknowledged": (bool,),
              "audience_contract": (dict,), "resource_config": (dict,)}
    payload = await _payload(request, fields, set(fields) - {"resource_config"})
    return await _run(request, user, group_service.approval, group_id, name, source_id, payload=payload)


@router.delete("/groups/{group_id}/briefings/{name}/sources/{source_id}/approval")
async def revoke(group_id: str, name: str, source_id: str, request: Request, user: dict = Depends(require_auth),
                 _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, {"expected_grant_revision": (int,)}, {"expected_grant_revision"})
    return await _run(request, user, group_service.approval, group_id, name, source_id, payload=payload, revoke=True)
