"""Group definition and edition HTTP adapters over the framework stores."""
from datetime import datetime
import json
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from istota import db, modules
from istota.briefings import group_store as store, source_grants
from istota.briefings.document import edition_preview
from istota.briefings.models import GROUP_SOURCE_KINDS
from istota.groups.policy import GroupError
from istota.groups.routes import _payload, _run, _summary, _user
from istota.groups.system import eligible_user_ids
from istota.user_briefings import hold_view
from istota.webui.router_stubs import require_auth, verify_origin

router = APIRouter()
DEFINITION_FIELDS = {"revision": (int,), "name": (str,), "title": (str,), "cron": (str,),
                     "timezone": (str,), "enabled": (bool,), "generation_mode": (str,),
                     "generation": (dict,), "channel": (str,), "excluded_user_ids": (list,)}
PREFERENCE_FIELDS = {"outbound_muted": (bool,), "in_app_muted": (bool,)}


def _access(conn, config, actor, group_id, name=None, *, author=False):
    # The Groups summary owns the host's local-mode admin exception.
    group = db.get_group(conn, group_id)
    if group is None:
        raise GroupError(404, "group_not_found", "Group not found")
    summary = _summary(conn, group, actor, config=config)
    caps = store.briefing_capabilities(conn, config, group_id, actor,
                                       is_admin=summary["capabilities"]["author_briefings"])
    if not caps["read_briefings"]:
        raise GroupError(404, "group_not_found", "Group not found")
    if author and not caps["author_briefings"]:
        raise GroupError(403, "briefing_forbidden", "Group briefing author required")
    definition = None
    if name is not None:
        row = conn.execute("SELECT id FROM group_briefing_configs WHERE group_id=? AND name=?", (group_id, name)).fetchone()
        if row is None:
            raise GroupError(404, "definition_not_found", "Briefing not found")
        definition = store.get_definition(conn, row["id"])
    return caps, definition


def _definition_view(config, definition, caps):
    # A member reads published editions, not private instructions in a draft.
    fields = ("id", "group_id", "name", "title", "cron", "timezone", "enabled", "channel", "revision")
    result = {key: definition[key] for key in fields}
    if caps["author_briefings"]:
        result = dict(definition)
    # The hold travels as a display name; the administrator's id stays here.
    result.pop("admin_hold_at", None)
    result.pop("admin_hold_by", None)
    held = hold_view(config, definition["admin_hold_at"], definition["admin_hold_by"])
    return {**result, "held": held, "scope": "group", "capabilities": caps}


def _edition_view(run, *, body=False):
    result = {key: run[key] for key in ("id", "definition_id", "mode", "state", "due_at", "created_at",
                                       "published_at", "error_code")}
    result.update(scope="group", run_id=run["id"], group_id=run["snapshot"]["definition"]["group_id"])
    if run["state"] in ("published", "preview_ready"):
        result["subject"] = run["subject"]
        if body:
            result["body_md"] = run["body_md"]
            result["document"] = run.get("document")
    return result


def _edition(conn, config, definition, caps, run_id):
    run = store.get_run(conn, run_id)
    if (run is None or run["definition_id"] != definition["id"]
            or (run["state"] != "published" and not caps["author_briefings"])):
        raise GroupError(404, "run_not_found", "Edition not found")
    if run["state"] == "preview_ready":
        try:
            if definition["revision"] != run["snapshot"]["definition"]["revision"]:
                raise GroupError(409, "revision_conflict", "Preview definition changed")
            source_grants.validate_run_grants(conn, config, run)
        except GroupError:
            raise GroupError(404, "run_not_found", "Edition not found") from None
    return run


def _targets(conn, config, actor):
    eligible = eligible_user_ids(conn, config)
    targets = [{"scope": "user", "label": "Just me", "capabilities": {"author_briefings": actor in eligible}}]
    for group in db.list_groups(conn):
        try:
            caps, _ = _access(conn, config, actor, group["group_id"])
        except GroupError:
            continue
        targets.append({"scope": "group", "group_id": group["group_id"], "label": group["display_name"],
                        "system_key": group["system_key"], "capabilities": caps,
                        "member_count": len(set(db.list_group_members(conn, group["group_id"])) & eligible)})
    return {"targets": targets, "channels": store.available_channels(config),
            "timezone": config.briefings.shared_block_timezone,
            "source_scopes": {kind: ("Personal contribution" if kind.startswith("personal_") else
                                     "Current group" if kind.startswith("group_") else
                                     "Installation-shared" if kind == "shared_block" else "Public")
                              for kind in sorted(GROUP_SOURCE_KINDS)}}


def _group_editions(conn, config, actor, *, group_id=None, limit=20, offset=0):
    """Published editions across every group the caller may read, newest first."""
    labels = {}
    for group in db.list_groups(conn):
        if group_id is not None and group["group_id"] != group_id:
            continue
        try:
            _access(conn, config, actor, group["group_id"])
        except GroupError:
            continue
        labels[group["group_id"]] = group["display_name"]
    if not labels:
        return {"items": [], "total": 0}
    marks = ",".join("?" * len(labels))
    where = (f"FROM group_briefing_runs r JOIN group_briefing_configs c ON c.id = r.definition_id "
             f"WHERE r.state = 'published' AND r.mode = 'publish' AND c.group_id IN ({marks})")
    rows = conn.execute(f"SELECT r.id, r.subject, r.body_md, r.document_json, r.published_at, c.group_id, c.name, c.title {where} "
                        "ORDER BY r.published_at DESC, r.id DESC LIMIT ? OFFSET ?",
                        (*labels, limit, offset)).fetchall()
    total = conn.execute(f"SELECT COUNT(*) {where}", tuple(labels)).fetchone()[0]
    return {"total": total, "items": [
        {"scope": "group", "run_id": row["id"], "group_id": row["group_id"], "group_label": labels[row["group_id"]],
         "briefing_name": row["name"], "briefing_title": row["title"], "subject": row["subject"],
         "published_at": row["published_at"],
         "preview": edition_preview(row["body_md"], json.loads(row["document_json"]) if row["document_json"] else None)}
        for row in rows]}


def _definitions(conn, config, actor, group_id, name=None):
    caps, definition = _access(conn, config, actor, group_id, name)
    definitions = [definition] if definition else store.list_definitions(conn, group_id)
    items = []
    for item in definitions:
        view = _definition_view(config, item, caps)
        view["next_run"] = (croniter(item["cron"], datetime.now(ZoneInfo(item["timezone"]))).get_next(datetime).isoformat()
                            if item["enabled"] and modules.module_available("briefings") else None)
        # Preview status is private; members see only retained published history.
        predicate = "definition_id=? AND mode='publish'" + ("" if caps["author_briefings"] else " AND state='published'")
        last = conn.execute(f"SELECT id, state, published_at FROM group_briefing_runs WHERE {predicate} ORDER BY id DESC LIMIT 1",
                            (item["id"],)).fetchone()
        view["last_run"] = dict(last) if last else None
        view["source_approvals"] = [source_grants.source_approval_state(
            conn, config, item["id"], source["id"], actor_id=actor, is_admin=caps["author_briefings"])
            for source in store.definition_sources(item) if source["kind"] in ("personal_file", "personal_calendar")
            and (caps["author_briefings"] or source["config"]["owner_id"] == actor)]
        items.append(view)
    return items[0] if definition else {"group_id": group_id, "items": items, "capabilities": caps}


def _save(conn, config, actor, group_id, payload, name=None):
    caps, old = _access(conn, config, actor, group_id, name, author=True)
    if old is None and payload.get("enabled", False):
        raise GroupError(422, "draft_required", "Create a disabled draft before enabling")
    value = dict(payload)
    revision = value.pop("revision")
    definition = store.save_definition(conn, config, group_id, value, actor_id=actor,
        expected_revision=revision, definition_id=old["id"] if old else None, is_admin=caps["author_briefings"])
    return _definition_view(config, definition, caps)


def _runs(conn, config, actor, group_id, name, *, limit=20, offset=0, payload=None):
    caps, definition = _access(conn, config, actor, group_id, name, author=payload is not None)
    if payload is not None:
        run = store.enqueue_run(conn, config, definition["id"], actor_id=actor,
                                is_admin=caps["author_briefings"], **payload)
        return _edition_view(run)
    predicate = "definition_id=?" + ("" if caps["author_briefings"] else " AND state='published'")
    rows = conn.execute(f"SELECT id FROM group_briefing_runs WHERE {predicate} ORDER BY id DESC LIMIT ? OFFSET ?",
                        (definition["id"], limit, offset)).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM group_briefing_runs WHERE {predicate}", (definition["id"],)).fetchone()[0]
    return {"group_id": group_id, "items": [_edition_view(store.get_run(conn, r["id"])) for r in rows], "total": total}


def _read_edition(conn, config, actor, group_id, name, run_id, *, download=False, delivery=False, retry=None):
    caps, definition = _access(conn, config, actor, group_id, name, author=retry is not None)
    run = _edition(conn, config, definition, caps, run_id)
    if retry is not None:
        count = store.retry_deliveries(conn, config, run_id, actor_id=actor,
                                      is_admin=caps["author_briefings"], **retry)
        return {"group_id": group_id, "retried": count}
    if delivery:
        rows = store.list_recipients(conn, run_id)
        counts = {}
        reasons = {"excluded": 0, "muted": 0, "missing_destination": 0}
        for row in rows:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
            if row["state"] == "skipped":
                reason = row["error_code"]
                if reason in ("missing_email", "missing_talk", "missing_ntfy"):
                    reason = "missing_destination"
                if reason == "module_disabled":
                    reason = "muted"
                if reason in reasons:
                    reasons[reason] += 1
        return {"group_id": group_id, "counts": counts,
                **({"reasons": reasons} if caps["author_briefings"] else {}), "recipients": [
            {**_user(config, r["user_id"]), **{k: r[k] for k in ("channel", "state", "attempts", "error_code")}}
            for r in rows if caps["author_briefings"] or r["user_id"] == actor]}
    if download:
        if run["state"] not in ("published", "preview_ready"):
            raise GroupError(404, "edition_unavailable", "Edition is not ready")
        return Response(run["body_md"], media_type="text/markdown", headers={
            "Content-Disposition": f'attachment; filename="briefing-{run_id}.md"'})
    return _edition_view(run, body=True)


def _preferences(conn, config, actor, group_id, name=None, *, payload=None):
    _, definition = _access(conn, config, actor, group_id, name)
    definition_id = definition["id"] if definition else None
    if payload is not None:
        store.set_delivery_preferences(conn, config, group_id, actor_id=actor,
                                                     definition_id=definition_id, **payload)
    preferences = store.delivery_preferences(conn, group_id, actor, definition_id=definition_id,
                                              include_group=definition_id is None)
    return {"group_id": group_id, **preferences}


def _approval(conn, config, actor, group_id, name, source_id, *, payload=None, revoke=False):
    caps, definition = _access(conn, config, actor, group_id, name)
    if payload is not None:
        if revoke:
            source_grants.revoke_source(conn, config, definition["id"], source_id, actor_id=actor, **payload)
        else:
            source_grants.approve_source(conn, config, definition["id"], source_id, actor_id=actor, **payload)
    return {"group_id": group_id, **source_grants.source_approval_state(
        conn, config, definition["id"], source_id, actor_id=actor, is_admin=caps["author_briefings"])}


def _audience(conn, config, actor, group_id, name):
    from istota.briefings.group_delivery import _destination
    caps, definition = _access(conn, config, actor, group_id, name)
    members = set(db.list_group_members(conn, group_id)) & eligible_user_ids(conn, config)
    result = {"group_id": group_id, "member_count": len(members), "future_members_read_history": True}
    if not caps["author_briefings"]:
        return result
    counts = {"deliverable": 0, "muted": 0, "missing_destination": 0, "excluded": 0}
    for member in members:
        if member in definition["excluded_user_ids"]:
            counts["excluded"] += 1
            continue
        channel = definition["channel"]
        prefs = store.delivery_preferences(conn, group_id, member, definition_id=definition["id"])
        if (prefs["in_app_muted" if channel == "in_app" else "outbound_muted"]
                or (channel != "in_app" and not config.is_module_enabled(member, "briefings", conn=conn))):
            counts["muted"] += 1
            continue
        try:
            if channel != "in_app":
                if channel not in store.available_channels(config):
                    raise GroupError(409, "channel_unavailable", "Channel unavailable")
                _destination(config, {"user_id": member, "channel": channel})
            counts["deliverable"] += 1
        except GroupError:
            counts["missing_destination"] += 1
    approvals = [source_grants.source_approval_state(conn, config, definition["id"], source["id"],
                 actor_id=actor, is_admin=caps["author_briefings"])
                 for source in store.definition_sources(definition) if source["kind"] in ("personal_file", "personal_calendar")]
    return {**result, **counts, "sources": approvals}


def _resources(conn, config, actor, group_id, query):
    from istota.briefings.routes import _walk_text_files
    from istota.briefings.sources.contributions import _calendar_client
    from istota.skills.calendar import owned_calendars
    _access(conn, config, actor, group_id)
    root = config.workspace_root(actor)
    files = _walk_text_files(root, query) if root is not None and root.is_dir() else []
    calendars = []
    calendar_unavailable = False
    try:
        with _calendar_client(config) as client:
            calendars = [{"label": name, "value": url} for name, url in owned_calendars(client, actor)]
    except Exception:
        calendar_unavailable = True
    return {"files": files, "calendars": calendars, "calendar_unavailable": calendar_unavailable}


@router.get("/groups/{group_id}/briefing-resources")
async def resources(group_id: str, request: Request, q: str = Query("", max_length=1000),
                    user: dict = Depends(require_auth)):
    return await _run(request, user, _resources, group_id, q, read=True)


@router.get("/briefings/group-editions")
async def group_editions(request: Request, group: str | None = Query(None, max_length=200),
                         limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                         user: dict = Depends(require_auth)):
    return await _run(request, user, _group_editions, group_id=group, limit=limit, offset=offset, read=True)


@router.get("/briefings/targets")
async def targets(request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, _targets, read=True)


@router.get("/groups/{group_id}/briefings")
@router.get("/groups/{group_id}/briefings/{name}")
async def definitions(group_id: str, request: Request, name: str = None, user: dict = Depends(require_auth)):
    return await _run(request, user, _definitions, group_id, name, read=True)


@router.post("/groups/{group_id}/briefings")
@router.put("/groups/{group_id}/briefings/{name}")
async def save_definition(group_id: str, request: Request, name: str = None, user: dict = Depends(require_auth),
                          _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, DEFINITION_FIELDS, {"revision", "name", "generation", "generation_mode"})
    return await _run(request, user, _save, group_id, payload, name)


@router.get("/groups/{group_id}/briefings/{name}/audience")
async def audience(group_id: str, name: str, request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, _audience, group_id, name, read=True)


@router.get("/groups/{group_id}/briefings/{name}/runs")
async def runs(group_id: str, name: str, request: Request, limit: int = Query(20, ge=1, le=100),
               offset: int = Query(0, ge=0), user: dict = Depends(require_auth)):
    return await _run(request, user, _runs, group_id, name, limit=limit, offset=offset, read=True)


@router.post("/groups/{group_id}/briefings/{name}/runs")
async def enqueue(group_id: str, name: str, request: Request, user: dict = Depends(require_auth),
                  _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, {"request_id": (str,), "mode": (str,)}, {"request_id", "mode"})
    return await _run(request, user, _runs, group_id, name, payload=payload)


@router.get("/groups/{group_id}/briefings/{name}/runs/{run_id}")
async def edition(group_id: str, name: str, run_id: int, request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, _read_edition, group_id, name, run_id, read=True)


@router.get("/groups/{group_id}/briefings/{name}/runs/{run_id}/download")
async def download(group_id: str, name: str, run_id: int, request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, _read_edition, group_id, name, run_id, download=True, read=True)


@router.get("/groups/{group_id}/briefing-delivery")
@router.get("/groups/{group_id}/briefings/{name}/delivery")
async def preferences(group_id: str, request: Request, name: str = None, user: dict = Depends(require_auth)):
    return await _run(request, user, _preferences, group_id, name, read=True)


@router.put("/groups/{group_id}/briefing-delivery")
@router.put("/groups/{group_id}/briefings/{name}/delivery")
async def set_preferences(group_id: str, request: Request, name: str = None, user: dict = Depends(require_auth),
                          _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, PREFERENCE_FIELDS, set(PREFERENCE_FIELDS))
    return await _run(request, user, _preferences, group_id, name, payload=payload)


@router.get("/groups/{group_id}/briefings/{name}/runs/{run_id}/delivery")
async def delivery(group_id: str, name: str, run_id: int, request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, _read_edition, group_id, name, run_id, delivery=True, read=True)


@router.post("/groups/{group_id}/briefings/{name}/runs/{run_id}/retry")
async def retry(group_id: str, name: str, run_id: int, request: Request, user: dict = Depends(require_auth),
                _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, {"acknowledge_duplicates": (bool,)}, set())
    return await _run(request, user, _read_edition, group_id, name, run_id, retry=payload)


@router.get("/groups/{group_id}/briefings/{name}/sources/{source_id}/approval")
async def approval(group_id: str, name: str, source_id: str, request: Request, user: dict = Depends(require_auth)):
    return await _run(request, user, _approval, group_id, name, source_id, read=True)


@router.put("/groups/{group_id}/briefings/{name}/sources/{source_id}/approval")
async def approve(group_id: str, name: str, source_id: str, request: Request, user: dict = Depends(require_auth),
                  _csrf: None = Depends(verify_origin)):
    fields = {"expected_definition_revision": (int,), "expected_grant_revision": (int,), "acknowledged": (bool,),
              "audience_contract": (dict,), "resource_config": (dict,)}
    payload = await _payload(request, fields, set(fields) - {"resource_config"})
    return await _run(request, user, _approval, group_id, name, source_id, payload=payload)


@router.delete("/groups/{group_id}/briefings/{name}/sources/{source_id}/approval")
async def revoke(group_id: str, name: str, source_id: str, request: Request, user: dict = Depends(require_auth),
                 _csrf: None = Depends(verify_origin)):
    payload = await _payload(request, {"expected_grant_revision": (int,)}, {"expected_grant_revision"})
    return await _run(request, user, _approval, group_id, name, source_id, payload=payload, revoke=True)
