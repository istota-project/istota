"""Workspace-mode loader for the briefings module.

Mirrors :mod:`istota.feeds.workspace`. Given a workspace root, synthesise a
:class:`BriefingsContext` with sensible defaults:

* ``data_dir`` = ``{workspace}/briefings``
* ``db_path`` = ``{data_dir}/data/briefings.db`` (overridden to local disk by
  the loader via ``Config.module_db_path``)

The function does not create files on disk — callers run
:meth:`BriefingsContext.ensure_dirs` (and ``init_db``) before real work.
"""

from __future__ import annotations

from pathlib import Path

from istota.briefings.models import BriefingsContext


def synthesize_briefings_context(
    user_id: str,
    workspace_root: Path,
    *,
    data_dir: Path | None = None,
    db_path: Path | None = None,
) -> BriefingsContext:
    """Build a :class:`BriefingsContext` rooted at a workspace dir.

    All keyword overrides are optional; defaults derive from ``workspace_root``.
    """
    workspace_root = Path(workspace_root).resolve()
    if data_dir is None:
        data_dir = workspace_root / "briefings"
    else:
        data_dir = Path(data_dir).resolve()

    if db_path is None:
        db_path = data_dir / "data" / "briefings.db"
    else:
        db_path = Path(db_path).resolve()

    return BriefingsContext(
        user_id=user_id,
        data_dir=data_dir,
        db_path=db_path,
        workspace_root=workspace_root,
    )
