"""Resolve a user's :class:`AppsContext` from istota's config.

Mirrors :mod:`istota.location._loader`: the refusals and the workspace come
from :mod:`istota.module_loader`, gated on
``Config.is_module_enabled(user_id, "apps")``, which is off unless the operator
has enabled ``module_apps``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from istota import module_loader

MODULE = "apps"


class UserNotFoundError(module_loader.UserNotFoundError):
    """The user has no usable apps configuration."""


@dataclass(frozen=True)
class AppsContext:
    user_id: str
    workspace: Path
    db_path: Path


def resolve_for_user(
    user_id: str,
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> AppsContext:
    """Build an apps context for ``user_id``.

    ``workspace`` is the user's bot directory, the parent of the drafts folder.
    There is no fallback database path under the workspace: a file there would
    sit beside the drafts, readable by every task.
    """
    workspace = module_loader.resolve_module_workspace(
        istota_config, user_id,
        module=MODULE, conn=conn, error=UserNotFoundError,
    )
    db_path = module_loader.resolve_module_db_path(istota_config, user_id, MODULE)
    if db_path is None:
        raise UserNotFoundError(f"{MODULE} module for '{user_id}' has no database path")
    return AppsContext(user_id=user_id, workspace=workspace, db_path=Path(db_path))


def list_users(
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Istota usernames with the apps module enabled."""
    return module_loader.list_module_users(istota_config, MODULE, conn)
