"""Per-request user-scoped configuration resolution.

Replaces the eager-load-everything-at-startup pattern. Each web request
resolves the authenticated user's config through ``resolve_user_config``,
which caches per user with a TTL and file-mtime invalidation.

In standalone moneyman, the source is the config file pointed to by the
``MONEYMAN_CONFIG`` environment variable (or ``./config.toml``). When this
package is folded into istota, the loader can be replaced via
``set_loader`` so the source becomes the istota resource entry.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from money.cli import Context, UserContext, load_context

# Five minutes — short enough that config edits show up promptly, long
# enough that we are not re-parsing TOML on every request.
CACHE_TTL_SECONDS = 300


class ConfigNotFoundError(Exception):
    """No moneyman config file is configured."""


class UserNotFoundError(Exception):
    """The user_id does not exist in the configured users."""


@dataclass(frozen=True)
class _CacheEntry:
    user_ctx: UserContext
    expires_at: float
    mtimes: tuple[tuple[Path, float], ...]


_cache: dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


# Loader: (user_id) -> UserContext. Pluggable so istota can inject its own
# source. In standalone moneyman the default reads from MONEYMAN_CONFIG.
Loader = Callable[[str], UserContext]
_loader: Loader | None = None


def set_loader(loader: Loader | None) -> None:
    """Replace the user-config loader. Pass ``None`` to restore the default."""
    global _loader
    _loader = loader
    invalidate_all()


def _config_path() -> Path | None:
    env = os.environ.get("MONEYMAN_CONFIG", "")
    if env:
        return Path(env)
    cwd = Path("config.toml")
    if cwd.exists():
        return cwd
    return None


def _default_loader(user_id: str) -> UserContext:
    config_path = _config_path()
    if config_path is None:
        raise ConfigNotFoundError("No moneyman config found (set MONEYMAN_CONFIG)")
    ctx = load_context(str(config_path))
    if user_id not in ctx.users:
        raise UserNotFoundError(f"User '{user_id}' not in config")
    return ctx.users[user_id]


def _tracked_files(user_ctx: UserContext) -> list[Path]:
    paths: list[Path] = []
    cp = _config_path()
    if cp is not None:
        paths.append(cp)
    for p in (
        user_ctx.invoicing_config_path,
        user_ctx.monarch_config_path,
        user_ctx.tax_config_path,
    ):
        if p is not None:
            paths.append(p)
    return paths


def _mtimes(paths: list[Path]) -> tuple[tuple[Path, float], ...]:
    out: list[tuple[Path, float]] = []
    for p in paths:
        try:
            out.append((p, p.stat().st_mtime))
        except FileNotFoundError:
            out.append((p, 0.0))
    return tuple(out)


def resolve_user_config(user_id: str) -> UserContext:
    """Resolve a user's moneyman configuration.

    Returns a cached entry if it is fresh (within TTL and no underlying
    file has changed); otherwise re-parses from source.
    """
    now = time.time()
    with _cache_lock:
        entry = _cache.get(user_id)
        if entry is not None and now < entry.expires_at:
            current = _mtimes([p for p, _ in entry.mtimes])
            if current == entry.mtimes:
                return entry.user_ctx

    loader = _loader or _default_loader
    user_ctx = loader(user_id)
    mtimes = _mtimes(_tracked_files(user_ctx))

    with _cache_lock:
        _cache[user_id] = _CacheEntry(
            user_ctx=user_ctx,
            expires_at=now + CACHE_TTL_SECONDS,
            mtimes=mtimes,
        )
    return user_ctx


def invalidate_user(user_id: str) -> None:
    with _cache_lock:
        _cache.pop(user_id, None)


def invalidate_all() -> None:
    with _cache_lock:
        _cache.clear()


def list_users() -> list[str]:
    """List all configured user IDs (excludes the legacy ``default`` key).

    Does not use the per-user cache; reads the config file directly.
    """
    config_path = _config_path()
    if config_path is None:
        return []
    ctx = load_context(str(config_path))
    return [u for u in ctx.users if u != "default"]
