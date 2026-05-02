"""Resolve a user's :class:`FeedsContext` from istota's config.

Single entry point for the web routes, scheduler hooks, and the CLI/skill
facade. Mirrors :mod:`istota.money._loader`.

Only "workspace mode" is supported — there's no legacy multi-user config
file to migrate from. Users either have a ``[[resources]] type = "feeds"``
entry that points the loader at their workspace, or they don't.
"""

from __future__ import annotations

from pathlib import Path

from istota.feeds.models import FeedsContext
from istota.feeds.workspace import synthesize_feeds_context


_FEEDS_RESOURCE_TYPES = ("feeds",)


class UserNotFoundError(Exception):
    """The user has no usable feeds configuration."""


def resolve_for_user(user_id: str, istota_config) -> FeedsContext:
    """Build a feeds context for ``user_id``.

    Resolution order, in line with the money loader:

    1. The user's ``[[resources]] type = "feeds"`` entry. ``extra`` keys
       (``data_dir``, ``config_dir``, ``db_path``, ``config_path``,
       ``tumblr_api_key``) override the workspace defaults.
    2. The workspace root is computed as
       ``{nextcloud_mount}/{get_user_bot_path(user_id, bot_dir_name)}``.
    """
    if istota_config is None:
        raise UserNotFoundError("istota config not loaded")

    uc = istota_config.get_user(user_id)
    if not uc:
        raise UserNotFoundError(f"user '{user_id}' not in istota config")

    for r in uc.resources:
        if r.type not in _FEEDS_RESOURCE_TYPES:
            continue

        extra = getattr(r, "extra", {}) or {}

        mount = getattr(istota_config, "nextcloud_mount_path", None)
        if not mount:
            raise UserNotFoundError(
                f"feeds resource for '{user_id}' has no nextcloud mount "
                "configured; set nextcloud_mount_path or override "
                "data_dir/db_path on the resource"
            )

        from istota.storage import get_user_bot_path

        workspace = Path(mount) / get_user_bot_path(
            user_id, istota_config.bot_dir_name,
        ).lstrip("/")

        return synthesize_feeds_context(
            user_id,
            workspace,
            data_dir=Path(extra["data_dir"]) if extra.get("data_dir") else None,
            config_dir=Path(extra["config_dir"]) if extra.get("config_dir") else None,
            db_path=Path(extra["db_path"]) if extra.get("db_path") else None,
            config_path=(
                Path(extra["config_path"]) if extra.get("config_path") else None
            ),
            tumblr_api_key=str(extra.get("tumblr_api_key") or ""),
        )

    raise UserNotFoundError(f"no feeds resource for user '{user_id}'")


def list_users(istota_config) -> list[str]:
    """Istota usernames with a feeds resource configured."""
    if istota_config is None:
        return []
    out: list[str] = []
    for username, uc in (istota_config.users or {}).items():
        for r in uc.resources:
            if r.type in _FEEDS_RESOURCE_TYPES:
                out.append(username)
                break
    return out
