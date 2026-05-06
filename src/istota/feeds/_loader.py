"""Resolve a user's :class:`FeedsContext` from istota's config.

Single entry point for the web routes, scheduler hooks, and the CLI/skill
facade. Mirrors :mod:`istota.money._loader`.

Feeds is a "module" in the modules/connected-services taxonomy: on by
default for every configured user, gated by
``Config.is_module_enabled(user_id, "feeds")``. The user's workspace path is
derived from ``nextcloud_mount_path`` + ``get_user_bot_path``; per-user
overrides (``data_dir``, ``db_path``, …) and the Tumblr API key live in the
encrypted secrets table once Phase 2 of the refactor lands. For now the
loader still consults the secrets table as the only source of
``tumblr_api_key``.
"""

from __future__ import annotations

from pathlib import Path

from istota.feeds.models import FeedsContext
from istota.feeds.workspace import synthesize_feeds_context


class UserNotFoundError(Exception):
    """The user has no usable feeds configuration."""


def resolve_for_user(user_id: str, istota_config) -> FeedsContext:
    """Build a feeds context for ``user_id``.

    Gated on ``Config.is_module_enabled(user_id, "feeds")``. The workspace
    root is always ``{nextcloud_mount}/{get_user_bot_path(...)}``.
    """
    if istota_config is None:
        raise UserNotFoundError("istota config not loaded")

    if not istota_config.is_module_enabled(user_id, "feeds"):
        raise UserNotFoundError(f"feeds module disabled for '{user_id}'")

    uc = istota_config.get_user(user_id)
    if not uc:
        raise UserNotFoundError(f"user '{user_id}' not in istota config")

    mount = getattr(istota_config, "nextcloud_mount_path", None)
    if not mount:
        raise UserNotFoundError(
            f"feeds module for '{user_id}' has no nextcloud mount configured"
        )

    from istota.storage import get_user_bot_path

    workspace = Path(mount) / get_user_bot_path(
        user_id, istota_config.bot_dir_name,
    ).lstrip("/")

    tumblr_api_key = ""
    try:
        from istota import secrets_store  # noqa: PLC0415

        db_path = getattr(istota_config, "db_path", None)
        if db_path is not None:
            stored = secrets_store.get_secret(
                db_path, user_id, "feeds", "tumblr_api_key",
            )
            if stored:
                tumblr_api_key = stored
    except Exception:  # noqa: BLE001
        # Best-effort — fall back to empty key if the secrets store is
        # unavailable (no key, no cryptography, etc.).
        pass

    return synthesize_feeds_context(
        user_id,
        workspace,
        tumblr_api_key=tumblr_api_key,
    )


def list_users(istota_config) -> list[str]:
    """Istota usernames with the feeds module enabled."""
    if istota_config is None:
        return []
    return [
        uid for uid in (istota_config.users or {})
        if istota_config.is_module_enabled(uid, "feeds")
    ]
