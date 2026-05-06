"""Resolve a user's money :class:`UserContext` from istota's config.

Single entry point for both web routes and the in-process skill.

Money is a "module" in the modules/connected-services taxonomy: on by
default for every configured user, gated by
``Config.is_module_enabled(user_id, "money")``. The user's workspace path is
derived from ``nextcloud_mount_path`` + ``get_user_bot_path``, and Monarch
credentials come from the encrypted secrets table. Legacy mode (the
``[[resources]] type = "money" config_path = …``-driven branch) was removed
when modules took over module gating.
"""

from __future__ import annotations

import os
from pathlib import Path

import tomli

from istota.money.cli import UserContext
from istota.money.workspace import synthesize_user_context


class UserNotFoundError(Exception):
    """The user has no usable money configuration."""


def load_user_secrets(user_id: str, istota_config) -> dict:
    """Load per-user money secrets (e.g. Monarch credentials).

    Resolution order:

    1. ``MONEY_SECRETS_FILE`` env var (escape hatch for direct ``money`` CLI
       invocations and tests).
    2. The encrypted ``secrets`` table — the only durable home for Monarch
       credentials after the modules refactor.

    Returns ``{}`` if no credentials are configured — sync commands that
    require them surface their own error.
    """
    explicit = os.environ.get("MONEY_SECRETS_FILE", "")
    if explicit:
        path = Path(explicit)
        if path.exists():
            return tomli.loads(path.read_text())
        return {}

    if istota_config is None:
        return {}

    monarch: dict[str, str] = {}
    try:
        from istota import secrets_store  # noqa: PLC0415

        db_path = getattr(istota_config, "db_path", None)
        if db_path is not None:
            for sk in ("email", "password", "session_token"):
                val = secrets_store.get_secret(db_path, user_id, "monarch", sk)
                if val:
                    monarch[sk] = val
    except Exception:  # noqa: BLE001
        # Best-effort: a missing/unavailable secrets store yields no creds.
        pass

    return {"monarch": monarch} if monarch else {}


def resolve_for_user(user_id: str, istota_config) -> UserContext:
    """Build a money :class:`UserContext` for ``user_id``.

    Gated on ``Config.is_module_enabled(user_id, "money")``. The workspace
    root is always ``{nextcloud_mount}/{get_user_bot_path(...)}``.
    """
    if istota_config is None:
        raise UserNotFoundError("istota config not loaded")

    if not istota_config.is_module_enabled(user_id, "money"):
        raise UserNotFoundError(f"money module disabled for '{user_id}'")

    uc = istota_config.get_user(user_id)
    if not uc:
        raise UserNotFoundError(f"user '{user_id}' not in istota config")

    mount = getattr(istota_config, "nextcloud_mount_path", None)
    if not mount:
        raise UserNotFoundError(
            f"money module for '{user_id}' has no nextcloud mount configured"
        )

    from istota.storage import get_user_bot_path

    workspace = Path(mount) / get_user_bot_path(
        user_id, istota_config.bot_dir_name,
    ).lstrip("/")
    ctx = synthesize_user_context(workspace)
    # Lazy import — _migrate imports config_store, which imports model
    # dataclasses. Keeping the import here avoids a startup-time cost when
    # the module isn't enabled for any user.
    from istota.money._migrate import ensure_initialised  # noqa: PLC0415
    ensure_initialised(ctx)
    return ctx


def list_users(istota_config) -> list[str]:
    """List istota usernames with the money module enabled."""
    if istota_config is None:
        return []
    return [
        uid for uid in (istota_config.users or {})
        if istota_config.is_module_enabled(uid, "money")
    ]
