"""Session-facing group management; notebook routes live beside this router."""
import asyncio
import json
import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from istota import db, storage
from istota.config import refresh_user_profiles_if_changed
from istota.groups import memory, service
from istota.groups.policy import GroupError, group_capabilities, require_group_member
from istota.webui.request_guards import BodyError, read_bounded_body
from istota.webui.router_stubs import require_auth, verify_origin

logger = logging.getLogger(__name__)
router = APIRouter()
BODY_LIMIT = 64 * 1024


def require_admin(request: Request, user: dict = Depends(require_auth)) -> dict:
    # The host overrides this with its web admin gate, including local mode.
    config = getattr(request.app.state, "istota_config", None)
    if config is None or not config.is_named_admin(user["username"]):
        raise HTTPException(403, "admin only")
    return user


async def _payload(request, fields, required, *, limit=BODY_LIMIT):
    try:
        raw = await read_bounded_body(request, limit)
    except BodyError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(422, "Invalid JSON object") from None
    if not isinstance(data, dict) or data.keys() - fields.keys() or required - data.keys():
        raise HTTPException(422, "Missing or unknown fields")
    for field, value in data.items():
        if type(value) not in fields[field]:
            raise HTTPException(422, f"Invalid {field}")
    return data


async def _run(request, user, operation, *args, read=False, **kwargs):
    config = getattr(request.app.state, "istota_config", None)
    if config is None:
        raise HTTPException(503, "Group management unavailable")
    actor = user["username"]
    group_id = args[0] if args and isinstance(args[0], str) else None

    def run():
        refresh_user_profiles_if_changed(config)
        with db.get_db(config.db_path) as conn:
            if read:
                # Keep authority and the data returned in one read snapshot.
                conn.execute("BEGIN")
            return operation(conn, config, actor, *args, **kwargs)

    try:
        result = await asyncio.to_thread(run)
    except GroupError as exc:
        logger.info("Group request actor=%s group=%r operation=%s outcome=%s",
                    actor, group_id, operation.__name__, exc.code)
        return JSONResponse({"error": exc.code, "message": str(exc), **exc.details}, status_code=exc.status_code)
    except sqlite3.Error:
        logger.warning("Group request actor=%s group=%r operation=%s outcome=unavailable",
                       actor, group_id, operation.__name__)
        return JSONResponse({"error": "groups_unavailable", "message": "Group management unavailable"}, status_code=503)
    if not read:
        logger.info("Group request actor=%s group=%s operation=%s outcome=ok",
                    actor, result["group_id"], operation.__name__)
    return result


def _user(config, user_id):
    profile = config.users.get(user_id)
    return {"user_id": user_id, "display_name": (profile.display_name if profile else "") or user_id}


def _summary(conn, group, actor, *, admin=False, config=None):
    is_admin = admin or bool(config and (
        config.is_named_admin(actor) or (config.web.has_method("none") and actor == config.local_user_id)
    ))
    capabilities = group_capabilities(conn, group["group_id"], actor, is_admin=is_admin)
    role = "owner" if capabilities["owner"] else "member" if capabilities["member"] else None
    metadata = group if admin else {field: group[field] for field in ("group_id", "display_name", "kind", "revision", "system_key")}
    return {**metadata, "role": role, "capabilities": capabilities}


def _list(conn, config, actor, *, admin=False):
    groups = db.list_groups(conn, include_archived=True) if admin else [
        db.get_group(conn, group_id) for group_id in db.list_user_groups(conn, actor)]
    result = []
    for group in groups:
        item = _summary(conn, group, actor, admin=admin, config=config)
        if admin:
            members = db.group_membership_history(conn, group["group_id"])
            item["needs_owner"] = not group["archived_at"] and not group["system_key"] and not any(
                m["role"] == "owner" and m["ended_at"] is None for m in members)
        result.append(item)
    return {"groups": result}


def _detail(conn, config, actor, group_id, *, admin=False):
    if not admin:
        require_group_member(conn, group_id, actor)
    group = db.get_group(conn, group_id, include_archived=admin)
    if group is None:
        raise GroupError(404, "group_not_found", "Group not found")
    history = db.group_membership_history(conn, group_id)
    result = _summary(conn, group, actor, admin=admin, config=config)
    result["members"] = [{**_user(config, m["user_id"]), "role": m["role"]}
                         for m in history if m["ended_at"] is None]
    if admin:
        result["history"] = [{**m, **_user(config, m["user_id"])} for m in history]
        result["events"] = [
            {**dict(row), "details": json.loads(row["details_json"])}
            for row in conn.execute("SELECT id, actor_id, event_type, subject_user_id, details_json, created_at "
                                    "FROM group_events WHERE group_id = ? ORDER BY id", (group_id,))]
        for event in result["events"]:
            del event["details_json"]
        result["needs_owner"] = not group["archived_at"] and not group["system_key"] and not any(
            m["role"] == "owner" and m["ended_at"] is None for m in history)
    else:
        result["rooms"] = [{"token": room.token, "name": room.name, "origin": room.origin}
                           for room in db.list_member_rooms(conn, actor) if room.group_id == group_id]
    result["disclosure"] = service.history_disclosure(group_id)
    return result


def _user_options(conn, config, actor, group_id=None):
    if group_id is not None:
        require_group_member(conn, group_id, actor)
        if not group_capabilities(conn, group_id, actor, is_admin=False)["owner"]:
            raise GroupError(403, "group_forbidden", "Group owner required")
    disabled = {row["user_id"] for row in conn.execute("SELECT user_id FROM web_auth_identities WHERE disabled = 1")}
    return {"users": [_user(config, user_id) for user_id in sorted(config.users) if user_id not in disabled]}


def _update(conn, config, actor, group_id, payload, *, admin=False):
    revision = payload.pop("revision")
    if "user_id" in payload:
        return service.change_membership(conn, config, group_id, payload.pop("user_id"),
                                         actor_id=actor, expected_revision=revision, is_admin=admin, **payload)
    result = service.update_group(conn, group_id, actor_id=actor, expected_revision=revision,
                                  is_admin=admin, **payload)
    return _summary(conn, result, actor, admin=admin)


def _membership(conn, config, actor, group_id, user_id, payload):
    result = service.change_membership(conn, config, group_id, user_id, actor_id=actor,
                                       expected_revision=payload.pop("revision"), **payload)
    return {**_summary(conn, result, actor), "changed": result["changed"]}


def _create(conn, config, actor, payload):
    return service.create_group(conn, config, payload.pop("group_id"), actor_id=actor, is_admin=True,
                                expected_revision=payload.pop("revision"), **payload)


def _archive(conn, config, actor, group_id, payload):
    return service.archive_group(conn, group_id, actor_id=actor, is_admin=True,
                                 expected_revision=payload.pop("revision"), **payload)


METADATA_FIELDS = {"revision": (int,), "display_name": (str,), "kind": (str,)}
MEMBERSHIP_FIELDS = {"revision": (int,), "role": (str,), "acknowledge_history": (bool,),
                     "replacement_owner": (str,)}


@router.get("/groups")
async def list_groups(request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, _list, read=True)


@router.get("/groups/{group_id}")
async def group_detail(group_id: str, request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, _detail, group_id, read=True)


@router.get("/groups/{group_id}/user-options")
async def member_user_options(group_id: str, request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, _user_options, group_id, read=True)


@router.patch("/groups/{group_id}")
async def update_group(group_id: str, request: Request, user: dict = Depends(require_auth),
                       _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, METADATA_FIELDS, {"revision"})
    return await _run(request, user, _update, group_id, payload)


@router.put("/groups/{group_id}/members/{user_id}")
async def put_member(group_id: str, user_id: str, request: Request, user: dict = Depends(require_auth),
                     _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, MEMBERSHIP_FIELDS, {"revision", "role"})
    return await _run(request, user, _membership, group_id, user_id, payload)


@router.delete("/groups/{group_id}/members/{user_id}")
async def delete_member(group_id: str, user_id: str, request: Request, user: dict = Depends(require_auth),
                        _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, {"revision": (int,), "replacement_owner": (str,)}, {"revision"})
    return await _run(request, user, _membership, group_id, user_id, {**payload, "role": None})


@router.get("/admin/groups")
async def admin_list_groups(request: Request, user: dict = Depends(require_admin)):
    return await _run(request, user, _list, admin=True, read=True)


@router.get("/admin/groups/user-options")
async def admin_user_options(request: Request, user: dict = Depends(require_admin)):
    return await _run(request, user, _user_options, read=True)


@router.get("/admin/groups/{group_id}")
async def admin_group_detail(group_id: str, request: Request, user: dict = Depends(require_admin)):
    return await _run(request, user, _detail, group_id, admin=True, read=True)


@router.post("/admin/groups")
async def admin_create_group(request: Request, user: dict = Depends(require_admin),
                             _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, {**METADATA_FIELDS, "group_id": (str,), "owners": (list,)},
                             {"revision", "group_id", "display_name", "owners"})
    return await _run(request, user, _create, payload)


@router.patch("/admin/groups/{group_id}")
async def admin_update_group(group_id: str, request: Request, user: dict = Depends(require_admin),
                             _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, {**METADATA_FIELDS, **MEMBERSHIP_FIELDS,
                                      "user_id": (str,), "role": (str, type(None))}, {"revision"})
    if "user_id" in payload:
        if "role" not in payload or {"display_name", "kind"} & payload.keys():
            raise HTTPException(422, "Send one membership change or one metadata change")
    elif {"role", "acknowledge_history", "replacement_owner"} & payload.keys():
        raise HTTPException(422, "Membership changes require user_id")
    return await _run(request, user, _update, group_id, payload, admin=True)


@router.post("/admin/groups/{group_id}/archive")
async def admin_archive_group(group_id: str, request: Request, user: dict = Depends(require_admin),
                              _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, {"revision": (int,), "confirm": (bool,)}, {"revision"})
    return await _run(request, user, _archive, group_id, payload)


@router.get("/groups/{group_id}/memory")
async def group_memory(group_id: str, request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, memory.read_memory, group_id, read=True)


@router.put("/groups/{group_id}/memory")
async def put_group_memory(group_id: str, request: Request, user: dict = Depends(require_auth),
                           _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, {"content": (str,), "revision": (str,)},
                             {"content", "revision"}, limit=6 * storage.USER_CONFIG_READ_CAP_BYTES + 1024)
    try:
        payload["content"].encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(422, "Content must be valid UTF-8") from None
    return await _run(request, user, memory.save_memory, group_id, payload)


@router.get("/groups/{group_id}/memory/activity")
async def group_memory_activity(group_id: str, request: Request, user: dict = Depends(require_auth),
                                cursor: str | None = Query(None, max_length=128),
                                limit: int = Query(25, ge=1, le=100)):
    return await _run(request, user, memory.read_activity, group_id, cursor=cursor, limit=limit, read=True)


@router.post("/admin/groups/{group_id}/storage")
async def retry_group_storage(group_id: str, request: Request, user: dict = Depends(require_admin),
                              _csrf: None = Depends(verify_origin)):
    await _payload(request, {}, set())
    return await _run(request, user, memory.retry_storage, group_id)
