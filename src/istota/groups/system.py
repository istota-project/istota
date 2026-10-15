"""The installation audience, using the same intervals as ordinary groups."""
import logging
from uuid import uuid4

from istota import db, storage
from istota.groups.service import _event, _finish, _transaction

logger = logging.getLogger(__name__)

ALL_USERS = "all_users"
ACCOUNT_HISTORY_DISCLOSURE = (
    "Your account joins All users automatically. You can read its retained briefing history, "
    "including editions published before you joined. Its notebook and files are shared "
    "with everyone currently using this installation and anyone who joins later."
)


def sync_account_membership(conn, user_id: str, *, enabled: bool = True) -> None:
    """Apply an account lifecycle change inside its caller's write transaction."""
    disabled = conn.execute("SELECT disabled FROM web_auth_identities WHERE user_id = ?", (user_id,)).fetchone()
    enabled = enabled and not user_id.startswith("__") and not (disabled and disabled["disabled"])
    if not enabled:
        from istota.briefings.source_grants import revoke_owner_grants
        revoke_owner_grants(conn, user_id)
    row = conn.execute("SELECT group_id FROM groups WHERE system_key = ?", (ALL_USERS,)).fetchone()
    if row is None:
        return
    group_id = row["group_id"]
    current = conn.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ? AND ended_at IS NULL",
        (group_id, user_id),
    ).fetchone()
    if enabled and not current:
        db.add_group_member(conn, group_id, user_id, added_by="__system__")
        _event(conn, group_id, "__system__", "member_added", subject=user_id)
        _finish(conn, group_id)
    elif not enabled and current:
        db.end_group_membership(conn, group_id, user_id, ended_by="__system__")
        _event(conn, group_id, "__system__", "member_removed", subject=user_id)
        _finish(conn, group_id)


def eligible_user_ids(conn, config) -> set[str]:
    profiles = {row["user_id"] for row in conn.execute("SELECT user_id FROM user_profiles")}
    # The general profile overlay keeps deleted entries until restart.
    stale = config._profile_user_ids - profiles - (config._configured_user_ids or set())
    users = (set(config.users) - stale) | profiles
    disabled = {row["user_id"] for row in conn.execute("SELECT user_id FROM web_auth_identities WHERE disabled = 1")}
    return {user for user in users - disabled if not user.startswith("__")}


def reconcile_all_users(conn, config, *, registry_complete: bool | None = None) -> dict:
    """Reconcile before taking an audience snapshot; never remove on a failed load."""
    with _transaction(conn):
        return reconcile_all_users_in_transaction(conn, config, registry_complete=registry_complete)


def reconcile_all_users_in_transaction(conn, config, *, registry_complete: bool | None = None) -> dict:
    """Let an edition and its current audience commit together."""
    complete = config._user_registry_complete if registry_complete is None else registry_complete
    from istota.briefings.source_grants import reconcile_source_grants
    reconcile_source_grants(conn, config, registry_complete=complete)
    row = conn.execute("SELECT group_id FROM groups WHERE system_key = ?", (ALL_USERS,)).fetchone()
    if row is None:
        # Neither a display-name nor an id collision transfers old history.
        group_id = "all-users"
        while db.get_group(conn, group_id, include_archived=True) is not None:
            group_id = "all-users-" + uuid4().hex[:12]
        db.create_group(conn, group_id, kind="group", display_name="All users", created_by="__system__")
        conn.execute("UPDATE groups SET system_key = ? WHERE group_id = ?", (ALL_USERS, group_id))
        _event(conn, group_id, "__system__", "created", details={"system_key": ALL_USERS})
    else:
        group_id = row["group_id"]
    eligible = eligible_user_ids(conn, config)
    for user_id in sorted(eligible):
        sync_account_membership(conn, user_id)
    if complete:
        for user_id in db.list_group_members(conn, group_id):
            if user_id not in eligible:
                sync_account_membership(conn, user_id, enabled=False)
    return db.get_group(conn, group_id)


def ensure_all_users_group(conn, config) -> dict:
    return reconcile_all_users(conn, config)


def refresh_all_users(config, *, provision: bool = False, busy_timeout_ms: int | None = None) -> None:
    """Refresh existing installations after registry loads; provision at startup."""
    if not config.db_path or not config.db_path.exists():
        return
    with db.get_db(config.db_path, busy_timeout_ms=busy_timeout_ms) as conn:
        if provision or conn.execute("SELECT 1 FROM groups WHERE system_key = ?", (ALL_USERS,)).fetchone():
            group = reconcile_all_users(conn, config)
        else:
            return
    if provision:
        try:
            storage.ensure_group_directories(config, group["group_id"], display_name="All users")
        except OSError:
            logger.warning("All users storage is unavailable")
