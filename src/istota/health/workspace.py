"""Workspace-mode loader for the health module.

Mirrors :mod:`istota.feeds.workspace`. Given a workspace root, synthesise a
:class:`HealthContext` with sensible defaults:

* ``data_dir`` = ``{workspace}/health``
* ``db_path`` = ``{data_dir}/data/health.db``
* ``uploads_dir`` = ``{data_dir}/uploads``

The function does not touch disk. Callers run :meth:`HealthContext.ensure_dirs`
and :func:`istota.health.db.init_db` before doing real work.
"""

from __future__ import annotations

from pathlib import Path

from istota.health.models import HealthContext


def synthesize_health_context(
    user_id: str,
    workspace_root: Path,
    *,
    data_dir: Path | None = None,
    db_path: Path | None = None,
    uploads_dir: Path | None = None,
) -> HealthContext:
    """Build a :class:`HealthContext` rooted at a workspace dir."""
    workspace_root = Path(workspace_root).resolve()
    if data_dir is None:
        data_dir = workspace_root / "health"
    else:
        data_dir = Path(data_dir).resolve()
    if db_path is None:
        db_path = data_dir / "data" / "health.db"
    else:
        db_path = Path(db_path).resolve()
    if uploads_dir is None:
        uploads_dir = data_dir / "uploads"
    else:
        uploads_dir = Path(uploads_dir).resolve()
    return HealthContext(
        user_id=user_id,
        workspace_root=workspace_root,
        data_dir=data_dir,
        db_path=db_path,
        uploads_dir=uploads_dir,
    )
