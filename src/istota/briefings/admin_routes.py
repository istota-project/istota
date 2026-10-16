"""Admin › Briefings: every personal and group briefing, with hold, resume and delete.

Definitions and state only. No blocks, sources, prompts or editions leave this
router, because administering briefings grants no content access (the Groups
rule). Personal rows are `briefing_configs` (`user_briefings`), group rows are
`group_briefing_configs` (`group_store`); every write goes through those
modules' own hold, resume and delete functions.

A delete needs the briefing's name typed back and a step-up proof for
``admin_briefing_delete``. The proof is redeemed last, after every refusal the
administrator would have to fix first, so a refusal never spends their code.
The step-up check lives in the host app; `step_up_gate` is the seam it fills,
and unmounted it refuses every delete.

Owners hear about a change through the `admin_briefing_change` notification
source: a personal briefing's owner, a group briefing's current owners, nobody
for an All users briefing, and never the acting administrator.
"""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from istota import db, user_briefings
from istota.briefings import group_store
from istota.groups.policy import GroupError
from istota.groups.routes import _payload, _run, _user, require_admin
from istota.groups.service import _transaction
from istota.groups.system import ALL_USERS
from istota.lib.date_parse import iso_utc
from istota.webui.router_stubs import verify_origin

logger = logging.getLogger(__name__)
router = APIRouter()
STEP_UP_ACTION = "admin_briefing_delete"
DELETE_FIELDS = {"confirm": (str,), "step_up": (dict,)}
_NO_STORE = {"Cache-Control": "no-store"}


def step_up_gate():
    """The host's ``_require_step_up(request, body, action)``; None refuses every delete."""
    return None


def _not_found():
    return GroupError(404, "briefing_not_found", "Briefing not found")


def _admin_name(config, actor):
    profile = config.users.get(actor)
    return (profile.display_name if profile else "") or "An administrator"


def _log(action, kind, briefing_id, owner, actor):
    # No titles: the log is read more widely than the admin pane.
    logger.info("admin_briefing action=%s kind=%s id=%s owner=%s admin=%s",
                action, kind, briefing_id, owner, actor)


def _notify(conn, config, actor, recipients, **fields):
    """Tell each recipient who has a profile, never the actor; a failure is logged only."""
    from istota.notifications.resolvers import admin_briefing_change
    from istota.notifications.store import hand_to_daemon

    targets = [user_id for user_id in dict.fromkeys(recipients)
               if user_id != actor and user_id in config.users]
    if not targets:
        return
    try:
        with _transaction(conn):
            hand_to_daemon(conn, [
                admin_briefing_change.write(conn, user_id, admin_name=_admin_name(config, actor), **fields)
                for user_id in targets
            ])
    except Exception:
        logger.warning("admin_briefing notification not written kind=%s id=%s action=%s",
                       fields.get("kind"), fields.get("briefing_id"), fields.get("action"))


def _personal_title(briefing):
    if briefing.title:
        return briefing.title
    from istota.briefings.generate import default_briefing_title
    return default_briefing_title(briefing.name)


def _personal_view(conn, config, briefing):
    profile = config.users.get(briefing.user_id)
    return {
        "id": briefing.id, "user_id": briefing.user_id,
        "owner_label": _user(config, briefing.user_id)["display_name"],
        "name": briefing.name, "title": briefing.title, "cron": briefing.cron,
        "timezone": profile.timezone if profile else None,
        "output": briefing.output, "enabled": briefing.enabled,
        "held": user_briefings.hold_view(config, briefing.admin_hold_at, briefing.admin_hold_by),
        "last_run_at": iso_utc(db.get_briefing_last_run(conn, briefing.user_id, briefing.name)),
    }


def _group_owners(conn, group_id):
    return [member["user_id"] for member in db.group_membership_history(conn, group_id)
            if member["role"] == "owner" and member["ended_at"] is None]


def _group_view(conn, config, definition, groups):
    group = groups.get(definition["group_id"]) or {}
    last = conn.execute("SELECT MAX(published_at) FROM group_briefing_runs "
                        "WHERE definition_id = ? AND state = 'published' AND mode = 'publish'",
                        (definition["id"],)).fetchone()[0]
    return {
        "id": definition["id"], "group_id": definition["group_id"],
        "group_label": group.get("display_name") or definition["group_id"],
        "system_key": group.get("system_key"),
        "name": definition["name"], "title": definition["title"], "cron": definition["cron"],
        "timezone": definition["timezone"], "channel": definition["channel"],
        "enabled": bool(definition["enabled"]),
        "held": user_briefings.hold_view(config, definition["admin_hold_at"], definition["admin_hold_by"]),
        "owners": [_user(config, user_id)["display_name"]
                   for user_id in _group_owners(conn, definition["group_id"])],
        "last_published_at": iso_utc(last),
    }


def _groups(conn):
    return {group["group_id"]: group for group in db.list_groups(conn, include_archived=True)}


def _list(conn, config, actor):
    personal = [_personal_view(conn, config, briefing)
                for briefing in user_briefings.list_briefings(config.db_path, conn=conn)]
    groups = _groups(conn)
    group = [_group_view(conn, config, group_store.get_definition(conn, row["id"]), groups)
             for row in conn.execute("SELECT id FROM group_briefing_configs WHERE deleted_at IS NULL "
                                     "ORDER BY group_id, name").fetchall()]
    return {"personal": personal, "group": group}


def _personal(conn, briefing_id):
    briefing = user_briefings.get_briefing_by_id(conn, briefing_id)
    if briefing is None:
        raise _not_found()
    return briefing


def _personal_hold(conn, config, actor, briefing_id, *, action):
    before = _personal(conn, briefing_id)
    if action == "hold":
        after = user_briefings.set_hold(config.db_path, briefing_id, admin_id=actor)
    else:
        after = user_briefings.clear_hold(config.db_path, briefing_id)
    if after is None:
        raise _not_found()
    # set_hold and clear_hold are idempotent and say nothing about whether
    # they changed the row, so a repeat is told apart by the hold itself.
    if (before.admin_hold_at is None) != (after.admin_hold_at is None):
        _log(action, "personal", briefing_id, after.user_id, actor)
        _notify(conn, config, actor, [after.user_id], action=action, kind="personal",
                briefing_id=briefing_id, briefing_title=_personal_title(after))
    return _personal_view(conn, config, after)


def _personal_delete_check(conn, config, actor, briefing_id, confirm):
    briefing = _personal(conn, briefing_id)
    if confirm != briefing.name:
        raise GroupError(409, "confirm_required", "Type the briefing's name to delete it")
    return {"ok": True}


def _personal_delete(conn, config, actor, briefing_id):
    briefing = _personal(conn, briefing_id)
    # The owner's own delete, so blocks, sources and the archive go the same way.
    if not user_briefings.delete_briefing_by_id(config.db_path, briefing.user_id, briefing_id):
        raise _not_found()
    _log("delete", "personal", briefing_id, briefing.user_id, actor)
    _notify(conn, config, actor, [briefing.user_id], action="delete", kind="personal",
            briefing_id=briefing_id, briefing_title=_personal_title(briefing))
    return {"ok": True, "deleted": True, "id": briefing_id}


def _definition(conn, definition_id):
    definition = group_store.get_definition(conn, definition_id)
    if definition is None or definition["deleted_at"]:
        raise _not_found()
    return definition


def _group_recipients(conn, definition, groups):
    group = groups.get(definition["group_id"]) or {}
    # An administrator authored the All users briefing; there is nobody to tell.
    if group.get("system_key") == ALL_USERS:
        return []
    return _group_owners(conn, definition["group_id"])


def _group_notify(conn, config, actor, definition, groups, action):
    group = groups.get(definition["group_id"]) or {}
    _notify(conn, config, actor, _group_recipients(conn, definition, groups), action=action,
            kind="group", briefing_id=definition["id"], briefing_title=definition["title"] or definition["name"],
            group_label=group.get("display_name") or definition["group_id"])


def _group_hold(conn, config, actor, definition_id, *, action):
    before = _definition(conn, definition_id)
    if action == "hold":
        after = group_store.set_hold(conn, config, definition_id, admin_id=actor)
    else:
        after = group_store.clear_hold(conn, config, definition_id)
    if after is None:
        raise _not_found()
    groups = _groups(conn)
    if (before["admin_hold_at"] is None) != (after["admin_hold_at"] is None):
        _log(action, "group", definition_id, after["group_id"], actor)
        _group_notify(conn, config, actor, after, groups, action)
    return _group_view(conn, config, after, groups)


def _group_delete_check(conn, config, actor, definition_id, confirm):
    definition = _definition(conn, definition_id)
    if confirm != definition["name"]:
        raise GroupError(409, "confirm_required", "Type the briefing's name to delete it")
    group_store.require_no_pending_migration(conn, definition_id)
    return {"ok": True}


def _group_delete(conn, config, actor, definition_id):
    definition = _definition(conn, definition_id)
    group_store.delete_definition(conn, definition_id, actor_id=actor)
    _log("delete", "group", definition_id, definition["group_id"], actor)
    _group_notify(conn, config, actor, definition, _groups(conn), "delete")
    return {"ok": True, "deleted": True, "id": definition_id}


async def _delete(request, user, step_up, check, delete, briefing_id):
    payload = await _payload(request, DELETE_FIELDS, set())
    refused = await _run(request, user, check, briefing_id, payload.get("confirm"), read=True)
    if isinstance(refused, JSONResponse):
        return refused
    if step_up is None:
        return JSONResponse({"error": "step_up_unavailable", "message": "Confirmation is unavailable"},
                            status_code=503)
    refusal = await step_up(request, payload, STEP_UP_ACTION)
    if refusal is not None:
        return refusal
    return await _run(request, user, delete, briefing_id)


@router.get("/admin/briefings")
async def admin_list_briefings(request: Request, user: dict = Depends(require_admin)):
    result = await _run(request, user, _list, read=True)
    if isinstance(result, JSONResponse):
        return result
    return JSONResponse(result, headers=_NO_STORE)


@router.post("/admin/briefings/personal/{briefing_id}/hold")
async def admin_hold_personal(briefing_id: int, request: Request, user: dict = Depends(require_admin),
                              _csrf: None = Depends(verify_origin)):
    await _payload(request, {}, set())
    return await _run(request, user, _personal_hold, briefing_id, action="hold")


@router.post("/admin/briefings/personal/{briefing_id}/resume")
async def admin_resume_personal(briefing_id: int, request: Request, user: dict = Depends(require_admin),
                                _csrf: None = Depends(verify_origin)):
    await _payload(request, {}, set())
    return await _run(request, user, _personal_hold, briefing_id, action="resume")


@router.post("/admin/briefings/group/{definition_id}/hold")
async def admin_hold_group(definition_id: int, request: Request, user: dict = Depends(require_admin),
                           _csrf: None = Depends(verify_origin)):
    await _payload(request, {}, set())
    return await _run(request, user, _group_hold, definition_id, action="hold")


@router.post("/admin/briefings/group/{definition_id}/resume")
async def admin_resume_group(definition_id: int, request: Request, user: dict = Depends(require_admin),
                             _csrf: None = Depends(verify_origin)):
    await _payload(request, {}, set())
    return await _run(request, user, _group_hold, definition_id, action="resume")


@router.delete("/admin/briefings/personal/{briefing_id}")
async def admin_delete_personal(briefing_id: int, request: Request, user: dict = Depends(require_admin),
                                _csrf: None = Depends(verify_origin), step_up=Depends(step_up_gate)):
    return await _delete(request, user, step_up, _personal_delete_check, _personal_delete, briefing_id)


@router.delete("/admin/briefings/group/{definition_id}")
async def admin_delete_group(definition_id: int, request: Request, user: dict = Depends(require_admin),
                             _csrf: None = Depends(verify_origin), step_up=Depends(step_up_gate)):
    return await _delete(request, user, step_up, _group_delete_check, _group_delete, definition_id)
