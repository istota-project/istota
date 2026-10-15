"""Human group policy; task audience gates remain in rooms.scopes."""
from istota import db


class GroupError(ValueError):
    def __init__(self, status_code: int, code: str, message: str, **details):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details


def group_capabilities(conn, group_id: str, actor_id: str, *, is_admin: bool) -> dict:
    group = db.get_group(conn, group_id)
    member = bool(group) and db.is_group_member(conn, group_id, actor_id)
    row = conn.execute(
        "SELECT role FROM group_members WHERE group_id = ? AND user_id = ? AND ended_at IS NULL",
        (group_id, actor_id),
    ).fetchone() if member else None
    owner = bool(row and row["role"] == "owner")
    system = bool(group and group["system_key"] == "all_users")
    return {
        "member": member, "owner": owner,
        "manage": bool(group) and not system and (owner or is_admin),
        "archive": bool(group) and not system and is_admin,
        "read_memory": member, "edit_memory": member,
        "leave": member and not system,
        "read_briefings": member,
        "author_briefings": member and (owner or (system and is_admin)),
        "receive_briefings": member,
    }


def require_group_member(conn, group_id: str, actor_id: str) -> None:
    if not db.is_group_member(conn, group_id, actor_id):
        raise GroupError(404, "group_not_found", "Group not found")
