"""Resolve a user's money :class:`UserContext` from istota's config.

Single entry point for both web routes and the in-process skill. Replaces
the earlier ``set_loader`` injection / TTL-cache machinery — the istota
config is already in-process and TOML files are cheap to re-read on demand.

Two modes:

* **Legacy:** the user's ``[[resources]] type = "money"`` entry carries
  ``extra.config_path`` (or ``path``) pointing at a money config TOML with
  ``[users.X]`` sections.
* **Workspace:** no ``config_path``. Synthesize a context rooted at
  ``{nextcloud_mount}/Users/{user_id}/{bot_dir}`` and read
  ``INVOICING.md`` / ``TAX.md`` / ``MONARCH.md`` from its ``config/``
  subdir.
"""

from __future__ import annotations

import os
from pathlib import Path

import tomli

from istota.money.cli import UserContext, load_context
from istota.money.workspace import synthesize_user_context


_MONEY_RESOURCE_TYPES = ("money", "moneyman")


class UserNotFoundError(Exception):
    """The user has no usable money configuration."""


def load_user_secrets(user_id: str, istota_config) -> dict:
    """Load per-user money secrets (e.g. ``[monarch] session_token``).

    Resolution order:

    1. ``MONEY_SECRETS_FILE`` env var (used by the scheduler).
    2. ``/etc/{namespace}/secrets/{user_id}/money.toml`` (the ansible-rendered
       location).

    Returns ``{}`` if no file is found — sync commands that require credentials
    will surface their own error.
    """
    explicit = os.environ.get("MONEY_SECRETS_FILE", "")
    if explicit:
        path = Path(explicit)
    else:
        namespace = getattr(istota_config, "namespace", None) or "istota"
        path = Path(f"/etc/{namespace}/secrets/{user_id}/money.toml")
    if path.exists():
        return tomli.loads(path.read_text())
    return {}


def resolve_for_user(user_id: str, istota_config) -> UserContext:
    if istota_config is None:
        raise UserNotFoundError("istota config not loaded")

    uc = istota_config.get_user(user_id)
    if not uc:
        raise UserNotFoundError(f"user '{user_id}' not in istota config")

    for r in uc.resources:
        if r.type not in _MONEY_RESOURCE_TYPES:
            continue

        extra = getattr(r, "extra", {}) or {}
        config_path = extra.get("config_path") or getattr(r, "path", "")
        user_key = extra.get("user_key") or user_id

        if config_path:
            ctx = load_context(str(config_path))
            if user_key not in ctx.users:
                raise UserNotFoundError(
                    f"user '{user_key}' not in money config at {config_path}"
                )
            return ctx.users[user_key]

        mount = getattr(istota_config, "nextcloud_mount_path", None)
        if not mount:
            raise UserNotFoundError(
                f"money resource for '{user_id}' has no config_path "
                "and no nextcloud mount is configured"
            )

        from istota.storage import get_user_bot_path

        workspace = Path(mount) / get_user_bot_path(
            user_id, istota_config.bot_dir_name,
        ).lstrip("/")
        data_dir = Path(extra["data_dir"]) if extra.get("data_dir") else None
        config_dir = Path(extra["config_dir"]) if extra.get("config_dir") else None
        db_path = Path(extra["db_path"]) if extra.get("db_path") else None
        ledgers = extra.get("ledgers")
        return synthesize_user_context(
            workspace,
            data_dir=data_dir,
            config_dir=config_dir,
            ledgers=ledgers,
            db_path=db_path,
        )

    raise UserNotFoundError(f"no money resource for user '{user_id}'")


def list_users(istota_config) -> list[str]:
    """List istota usernames with a money/moneyman resource configured."""
    if istota_config is None:
        return []
    out: list[str] = []
    for username, uc in (istota_config.users or {}).items():
        for r in uc.resources:
            if r.type in _MONEY_RESOURCE_TYPES:
                out.append(username)
                break
    return out
