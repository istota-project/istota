"""Resolve a user's :class:`HealthContext` from istota's config.

Single entry point for the web routes, scheduler hooks, and the CLI/skill
facade. Mirrors :mod:`istota.location._loader` and :mod:`istota.feeds._loader`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from istota.health.models import HealthContext
from istota.health.workspace import synthesize_health_context


class UserNotFoundError(Exception):
    """The user has no usable health configuration."""


def resolve_for_user(
    user_id: str,
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> HealthContext:
    """Build a health context for ``user_id``.

    Raises :class:`UserNotFoundError` if the config is missing, the user is
    unknown, the module is opted out, or the Nextcloud mount path is
    unset.

    Pass ``conn`` to reuse an existing framework-DB connection for the
    module-enabled check.
    """
    if istota_config is None:
        raise UserNotFoundError("istota config not loaded")

    if not istota_config.is_module_enabled(user_id, "health", conn=conn):
        raise UserNotFoundError(f"health module disabled for '{user_id}'")

    if user_id not in (istota_config.users or {}):
        raise UserNotFoundError(f"user '{user_id}' not in istota config")

    mount = getattr(istota_config, "nextcloud_mount_path", None)
    if not mount:
        raise UserNotFoundError(
            f"health module for '{user_id}' has no nextcloud mount configured"
        )

    from istota.storage import get_user_bot_path

    workspace = Path(mount) / get_user_bot_path(
        user_id, istota_config.bot_dir_name,
    ).lstrip("/")

    return synthesize_health_context(user_id, workspace)


def list_users(
    istota_config,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Istota usernames with the health module enabled."""
    if istota_config is None:
        return []
    return [
        uid for uid in (istota_config.users or {})
        if istota_config.is_module_enabled(uid, "health", conn=conn)
    ]
