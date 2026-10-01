"""Whether a skill CLI may touch a group's store, for ``kv --group`` and
``memory --group``.

One rule for both, because ``group_kv`` and ``GROUP.md`` are one group's store
(multiplayer D21): the caller, read from ``ISTOTA_USER_ID`` (which the proxy
sets from the task, never from the command line), is a current member, and the
group is in the task's resolved set, ``ISTOTA_TASK_GROUPS``. Absent means none.

Every refusal reads the same, so a CLI built on this is no group-existence
oracle: an invalid id, an unknown group, a group the caller is not in and one
outside the resolved set all answer ``not a member of group '<id>'``.
"""

from __future__ import annotations

import os
from pathlib import Path


def group_refusal(group_id: str) -> str:
    """The one refusal text for every reason a group is out of reach."""
    return f"not a member of group '{group_id}'"


def group_access_denied(group_id: str) -> bool:
    """Whether the caller may not touch ``group_id``'s store. Fail-closed.

    A database that is missing or raises is a refusal, and a missing one is
    not created (#570).
    """
    from istota import db
    from istota.skill_host_paths import TASK_GROUPS_VAR

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    db_path = os.environ.get("ISTOTA_DB_PATH", "")
    if not user_id or not db_path or not db.is_valid_group_id(group_id):
        return True
    resolved = {g.strip() for g in os.environ.get(TASK_GROUPS_VAR, "").split(",")}
    if group_id not in resolved:
        return True
    if not Path(db_path).is_file():
        return True
    try:
        with db.get_db(Path(db_path)) as conn:
            return not db.is_group_member(conn, group_id, user_id)
    except Exception:  # noqa: BLE001
        return True
