"""Apps: single pages that run in a sandboxed frame and reach data only through
a permissioned bridge.

Experimental, behind ``module_apps``. One SQLite file per user at
``config.module_db_path(user_id, "apps")`` holds installed versions, their
grants, pending installs and the call log; drafts live in the user's workspace
under ``{bot_dir}/apps/<id>/``.
"""

from istota.apps._loader import (
    AppsContext,
    UserNotFoundError,
    list_users,
    resolve_for_user,
)
from istota.apps.db import connect, init_db


__all__ = [
    "AppsContext",
    "UserNotFoundError",
    "connect",
    "init_db",
    "list_users",
    "resolve_for_user",
]
