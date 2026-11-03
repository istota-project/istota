"""What members did to a group's files from the web, as group events.

`group_events` has two insert sites: `service._event` and `record_file_event`.
`_event` writes inside a caller's open transaction and is followed by
`service._finish`, which bumps `groups.revision`; a file write recorded that way
would make an owner's concurrent rename or membership edit fail its revision
check for a reason unrelated to it. So a file event is its own short
transaction on its own connection, after the filesystem write, and leaves the
revision alone.

Writes made by tasks or directly on the server do not pass through here and
are not recorded.
"""
import json
import logging
import sqlite3

from istota import db

logger = logging.getLogger(__name__)

FILE_EVENTS = ("file_uploaded", "folder_created", "file_edited", "file_deleted")
PATH_MAX_CHARS = 512


def record_file_event(config, group_id: str, actor_id: str, event_type: str,
                      path: str, *, size: int | None = None) -> bool:
    """Record one web write; False (and a WARNING without the path) if it failed."""
    details = {"path": path[:PATH_MAX_CHARS], "size": size}
    try:
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO group_events (group_id, actor_id, event_type, subject_user_id, details_json) "
                "VALUES (?, ?, ?, NULL, ?)",
                (group_id, actor_id, event_type, json.dumps(details)),
            )
            conn.commit()
    except sqlite3.Error:
        logger.warning("group_file_event_failed group=%s event=%s", group_id, event_type)
        return False
    return True


def recent_file_events(conn, group_id: str, *, limit: int = 20) -> list[dict]:
    """The group's newest file events, newest first."""
    placeholders = ", ".join("?" for _ in FILE_EVENTS)
    rows = conn.execute(
        "SELECT id, actor_id, event_type, details_json, created_at FROM group_events "
        f"WHERE group_id = ? AND event_type IN ({placeholders}) ORDER BY id DESC LIMIT ?",
        (group_id, *FILE_EVENTS, limit),
    ).fetchall()
    events = []
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except (TypeError, ValueError):
            details = {}
        if not isinstance(details, dict):
            details = {}
        events.append({
            "id": row["id"], "actor_id": row["actor_id"], "event_type": row["event_type"],
            "path": details.get("path"), "size": details.get("size"), "created_at": row["created_at"],
        })
    return events
