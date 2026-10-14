"""Member notebook reads, locked replacements and recorded changes."""
import json

from istota import db, storage
from istota.groups.policy import GroupError, require_group_member
from istota.memory.curation import audit
from istota.memory.curation.file_lock import group_memory_lock_dir
from istota.skills._loader import OVERLAY_UNREADABLY_LARGE, OVERLAY_UNREADABLE
from istota.workspace import text_files


def _refused(reason):
    status = 413 if reason == OVERLAY_UNREADABLY_LARGE else 503 if reason in {
        "storage_unavailable", OVERLAY_UNREADABLE, "unreadable"} else 422
    raise GroupError(status, "memory_unreadable", "Group memory cannot be read", reason=reason)


def read_memory(conn, config, actor, group_id):
    require_group_member(conn, group_id, actor)
    result = storage.read_group_memory_result(config, group_id)
    if result["status"] == "refused":
        _refused(result["reason"])
    content = result.get("content", "")
    size = len(content.encode("utf-8"))
    row = conn.execute(
        "SELECT key, value FROM group_kv WHERE group_id = ? AND namespace = ? "
        "AND CASE WHEN json_valid(value) THEN json_extract(value, '$.applied[0].outcome') END = 'applied' "
        "ORDER BY key DESC LIMIT 1", (group_id, audit.AUDIT_NAMESPACE)).fetchone()
    last_change = {**json.loads(row["value"]), "id": row["key"]} if row else None
    return {**result, "group_id": group_id, "content": content,
            "revision": text_files.revision(content), "bytes": size, "last_change": last_change,
            "max_bytes": storage.USER_CONFIG_READ_CAP_BYTES,
            "large": size > storage.GROUP_MEMORY_SOFT_LIMIT_BYTES}


def save_memory(conn, config, actor, group_id, payload):
    require_group_member(conn, group_id, actor)
    group_dir = storage._contained_group_dir(config, group_id)
    if group_dir is None:
        result = storage.read_group_memory_result(config, group_id)
        _refused(result.get("reason", "storage_unavailable"))
    before = None
    audit_recorded = False

    def read():
        nonlocal before
        # No DB transaction survives the wait for the filesystem lock.
        require_group_member(conn, group_id, actor)
        result = storage.read_group_memory_result(config, group_id)
        if result["status"] != "refused":
            before = text_files.revision(result.get("content", ""))
        return result

    def write(content):
        nonlocal audit_recorded
        require_group_member(conn, group_id, actor)
        if not storage.write_group_memory(config, group_id, content):
            return False
        audit_recorded = audit.write_group_audit_log(
            config, group_id, actor, source="web",
            applied=[{"op": {"op": "edit"}, "outcome": "applied"}], rejected=[],
            before_revision=before, after_revision=text_files.revision(content))
        return True

    result = text_files.save(
        config, actor, group_dir / "GROUP.md", payload["content"], payload["revision"],
        read=read, write=write, max_bytes=storage.USER_CONFIG_READ_CAP_BYTES,
        lock_anchor=group_memory_lock_dir(config.db_path))
    if result.status == "read_refused":
        _refused(result.reason)
    if result.status != "ok":
        status = {"conflict": 409, "locked": 423, "too_large": 413}.get(result.status, 503)
        raise GroupError(status, "memory_" + result.status, "Group memory save " + result.status)
    return {"status": "ok", "group_id": group_id, "revision": result.revision, "audit_recorded": audit_recorded}


def read_activity(conn, config, actor, group_id, *, cursor=None, limit=25):
    require_group_member(conn, group_id, actor)
    rows = conn.execute(
        "SELECT key, value FROM group_kv WHERE group_id = ? AND namespace = ? "
        "AND (? IS NULL OR key < ?) ORDER BY key DESC LIMIT ?",
        (group_id, audit.AUDIT_NAMESPACE, cursor, cursor, limit + 1)).fetchall()
    entries = []
    for row in rows[:limit]:
        try:
            entry = json.loads(row["value"])
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict):
            entries.append({**entry, "id": row["key"]})
    return {"group_id": group_id, "entries": entries,
            "next_cursor": rows[limit - 1]["key"] if len(rows) > limit else None}


def retry_storage(conn, config, actor, group_id):
    group = db.get_group(conn, group_id)
    if group is None or group["archived_at"]:
        raise GroupError(404, "group_not_found", "Group not found")
    # A missing configured workspace is an outage, never a request to create it.
    if not config.has_workspace or not config.workspace_path.is_dir():
        raise GroupError(503, "memory_unavailable", "Group storage unavailable")
    group_dir = storage._contained_group_dir(config, group_id)
    if group_dir is None:
        _refused("group_dir_outside_group_root")
    try:
        with text_files.file_lock(config, actor, group_dir / "GROUP.md",
                                  lock_anchor=group_memory_lock_dir(config.db_path)):
            if not storage.ensure_group_directories(config, group_id, display_name=group["display_name"]):
                raise GroupError(503, "memory_unavailable", "Group storage unavailable")
            result = storage.read_group_memory_result(config, group_id)
            if result["status"] != "readable":
                _refused(result.get("reason", "storage_unavailable"))
    except text_files.MemoryMdLocked:
        raise GroupError(423, "memory_locked", "Group memory is locked") from None
    except OSError:
        raise GroupError(503, "memory_unavailable", "Group storage unavailable") from None
    return {"group_id": group_id, "storage_available": True}
