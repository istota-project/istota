"""Resolve a user's :class:`LocationContext` from istota's config.

Single entry point for the webhook receiver, scheduler hooks, web routes,
and the location skill's ``setup_env`` hook. Mirrors
:mod:`istota.feeds._loader` and :mod:`istota.money._loader`.

Location is a "module" in the modules/connected-services taxonomy: on by
default for every configured user, gated by
``Config.is_module_enabled(user_id, "location")``. The user's workspace
path is derived from ``nextcloud_mount_path`` + ``get_user_bot_path``.
"""

from __future__ import annotations

from pathlib import Path

from istota.location.models import LocationContext
from istota.location.workspace import synthesize_location_context


class UserNotFoundError(Exception):
    """The user has no usable location configuration."""


def resolve_for_user(user_id: str, istota_config) -> LocationContext:
    """Build a location context for ``user_id``.

    Raises :class:`UserNotFoundError` if the config is missing, the user
    is unknown, the location module is opted out, or the Nextcloud mount
    path is unset.
    """
    if istota_config is None:
        raise UserNotFoundError("istota config not loaded")

    if not istota_config.is_module_enabled(user_id, "location"):
        raise UserNotFoundError(f"location module disabled for '{user_id}'")

    if user_id not in (istota_config.users or {}):
        raise UserNotFoundError(f"user '{user_id}' not in istota config")

    mount = getattr(istota_config, "nextcloud_mount_path", None)
    if not mount:
        raise UserNotFoundError(
            f"location module for '{user_id}' has no nextcloud mount configured"
        )

    from istota.storage import get_user_bot_path

    workspace = Path(mount) / get_user_bot_path(
        user_id, istota_config.bot_dir_name,
    ).lstrip("/")

    return synthesize_location_context(user_id, workspace)


def list_users(istota_config) -> list[str]:
    """Istota usernames with the location module enabled."""
    if istota_config is None:
        return []
    return [
        uid for uid in (istota_config.users or {})
        if istota_config.is_module_enabled(uid, "location")
    ]
