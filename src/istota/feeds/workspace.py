"""Workspace-mode loader for the feeds module.

Mirrors :mod:`istota.money.workspace`. Given a workspace root, synthesise a
:class:`FeedsContext` with sensible defaults:

* ``data_dir`` = ``{workspace}/feeds``
* ``config_dir`` = ``{data_dir}/config`` first, falling back to
  ``{workspace}/config`` (so a user can colocate FEEDS.md with the rest of
  their CRON.md / BRIEFINGS.md if they prefer that layout).
* ``config_path`` = ``FEEDS.md`` (preferred) or ``feeds.toml`` (legacy).
* ``db_path`` = ``{data_dir}/data/feeds.db``

The function does not create files on disk — callers run
:meth:`FeedsContext.ensure_dirs` (and ``init_db``) before doing real work.
"""

from __future__ import annotations

from pathlib import Path

from istota.feeds.models import FeedsContext


CONFIG_FILENAME = "FEEDS.md"
LEGACY_CONFIG_FILENAME = "feeds.toml"


def _resolve_config_path(config_dirs: list[Path]) -> Path:
    """Find the first existing FEEDS.{md,toml}. Falls back to FEEDS.md in
    the first dir if nothing exists yet.
    """
    for fname in (CONFIG_FILENAME, LEGACY_CONFIG_FILENAME):
        for cd in config_dirs:
            p = cd / fname
            if p.exists():
                return p
    return config_dirs[0] / CONFIG_FILENAME


def _config_search_dirs(
    workspace_root: Path, data_dir: Path, config_dir: Path | None,
) -> list[Path]:
    """Return ordered config-search dirs.

    Explicit ``config_dir`` wins outright. Otherwise prefer
    ``{data_dir}/config`` (module-local) and fall back to
    ``{workspace_root}/config`` (colocated with USER.md / CRON.md / etc).
    """
    if config_dir is not None:
        return [Path(config_dir).resolve()]
    return [data_dir / "config", workspace_root / "config"]


def synthesize_feeds_context(
    user_id: str,
    workspace_root: Path,
    *,
    data_dir: Path | None = None,
    config_dir: Path | None = None,
    db_path: Path | None = None,
    config_path: Path | None = None,
    tumblr_api_key: str = "",
) -> FeedsContext:
    """Build a :class:`FeedsContext` rooted at a workspace dir.

    All keyword overrides are optional; sensible defaults are derived from
    ``workspace_root``. ``config_path`` overrides config-dir search entirely
    (used by tests and the legacy ``[[resources]] config_path`` knob).
    """
    workspace_root = Path(workspace_root).resolve()
    if data_dir is None:
        data_dir = workspace_root / "feeds"
    else:
        data_dir = Path(data_dir).resolve()

    if config_path is not None:
        cfg = Path(config_path).resolve()
        cfg_dir = cfg.parent
    else:
        config_dirs = _config_search_dirs(workspace_root, data_dir, config_dir)
        cfg = _resolve_config_path(config_dirs)
        cfg_dir = cfg.parent

    if db_path is None:
        db_path = data_dir / "data" / "feeds.db"
    else:
        db_path = Path(db_path).resolve()

    return FeedsContext(
        user_id=user_id,
        data_dir=data_dir,
        config_dir=cfg_dir,
        config_path=cfg,
        db_path=db_path,
        tumblr_api_key=tumblr_api_key,
    )
