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
    owner = member and db.is_group_owner(conn, group_id, actor_id)
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


def is_group_admin(config, actor_id: str) -> bool:
    """The admin answer group capabilities take: a named admin, or the local user under `auth = "none"`."""
    return bool(config.is_named_admin(actor_id)
                or (config.web.has_method("none") and actor_id == config.local_user_id))


def user_label(config, user_id: str) -> dict:
    profile = config.users.get(user_id)
    return {"user_id": user_id, "display_name": (profile.display_name if profile else "") or user_id}


def require_group_member(conn, group_id: str, actor_id: str) -> None:
    if not db.is_group_member(conn, group_id, actor_id):
        raise GroupError(404, "group_not_found", "Group not found")
