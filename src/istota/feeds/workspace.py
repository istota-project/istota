"""Workspace-mode loader for the feeds module.

Mirrors :mod:`istota.money.workspace`. Given a workspace root, synthesise a
:class:`FeedsContext` with sensible defaults:

* ``data_dir`` = ``{workspace}/feeds``
* ``db_path`` = ``{data_dir}/data/feeds.db``

The function does not create files on disk — callers run
:meth:`FeedsContext.ensure_dirs` (and ``init_db``) before doing real work.
The legacy ``feeds.toml`` is no longer part of the runtime; existing
files are imported into the DB on first touch by
:func:`istota.feeds._migrate.migrate_legacy_toml`.
"""

from __future__ import annotations

from pathlib import Path

from istota.feeds.models import FeedsContext


def synthesize_feeds_context(
    user_id: str,
    workspace_root: Path,
    *,
    data_dir: Path | None = None,
    db_path: Path | None = None,
    tumblr_api_key: str = "",
) -> FeedsContext:
    """Build a :class:`FeedsContext` rooted at a workspace dir.

    All keyword overrides are optional; sensible defaults are derived from
    ``workspace_root``.
    """
    workspace_root = Path(workspace_root).resolve()
    if data_dir is None:
        data_dir = workspace_root / "feeds"
    else:
        data_dir = Path(data_dir).resolve()

    if db_path is None:
        db_path = data_dir / "data" / "feeds.db"
    else:
        db_path = Path(db_path).resolve()

    return FeedsContext(
        user_id=user_id,
        data_dir=data_dir,
        db_path=db_path,
        tumblr_api_key=tumblr_api_key,
    )
