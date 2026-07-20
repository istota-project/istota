"""Resolve a user's :class:`BriefingsContext` from istota's config.

Single entry point for the web routes, scheduler/executor hooks, and the
CLI/skill facade. Mirrors :mod:`istota.feeds._loader` but with no credentials —
briefings needs only paths.

Briefings is a "module": on by default for every configured user, gated by
``Config.is_module_enabled(user_id, "briefings")``. The workspace path derives
from ``nextcloud_mount_path`` + ``get_user_bot_path``; the DB relocates to
local disk via ``Config.module_db_path``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from istota.briefings.models import BriefingsContext
from istota.briefings.workspace import synthesize_briefings_context


class UserNotFoundError(Exception):
    """The user has no usable briefings configuration."""


def resolve_for_user(
    user_id: str,
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> BriefingsContext:
    """Build a briefings context for ``user_id``.

    Gated on ``Config.is_module_enabled(user_id, "briefings")``. Pass ``conn``
    to reuse an existing framework-DB connection for the module-enabled check
    (hot scheduler loops).
    """
    if istota_config is None:
        raise UserNotFoundError("istota config not loaded")

    if not istota_config.is_module_enabled(user_id, "briefings", conn=conn):
        raise UserNotFoundError(f"briefings module disabled for '{user_id}'")

    uc = istota_config.get_user(user_id)
    if not uc:
        raise UserNotFoundError(f"user '{user_id}' not in istota config")

    mount = getattr(istota_config, "nextcloud_mount_path", None)
    if not mount:
        raise UserNotFoundError(
            f"briefings module for '{user_id}' has no nextcloud mount configured"
        )

    from istota.storage import get_user_bot_path

    workspace = Path(mount) / get_user_bot_path(
        user_id, istota_config.bot_dir_name,
    ).lstrip("/")

    # DB lives on local disk (WAL-safe); workspace files stay on the mount.
    db_override = None
    resolver = getattr(istota_config, "module_db_path", None)
    if callable(resolver):
        db_override = resolver(user_id, "briefings")

    return synthesize_briefings_context(
        user_id,
        workspace,
        db_path=db_override,
    )


def list_users(
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Istota usernames with the briefings module enabled."""
    if istota_config is None:
        return []
    return [
        uid for uid in (istota_config.users or {})
        if istota_config.is_module_enabled(uid, "briefings", conn=conn)
    ]
