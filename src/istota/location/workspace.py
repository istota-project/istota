"""Workspace-mode loader for the location module.

Mirrors :mod:`istota.feeds.workspace` and :mod:`istota.money.workspace`.
Given a workspace root, synthesise a :class:`LocationContext` with the
default per-user ``location.db`` path:

* ``db_path`` = ``{workspace}/location/data/location.db``

The function does not touch disk. Callers run :func:`istota.location.db.init_db`
before doing real work.
"""

from __future__ import annotations

from pathlib import Path

from istota.location.models import LocationContext


def synthesize_location_context(
    user_id: str,
    workspace: Path,
    *,
    db_path: Path | None = None,
) -> LocationContext:
    """Build a :class:`LocationContext` rooted at a workspace dir."""
    workspace = Path(workspace).resolve()
    if db_path is None:
        db_path = workspace / "location" / "data" / "location.db"
    else:
        db_path = Path(db_path).resolve()
    return LocationContext(
        user_id=user_id,
        workspace=workspace,
        db_path=db_path,
    )
