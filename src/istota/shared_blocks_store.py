"""Shared-block definition store (admin-shared-briefing-blocks spec).

Admin-editable shared briefing block definitions live in the framework
``shared_block_configs`` table. They are seeded once from config
(``DEFAULT_SHARED_BLOCKS`` / ``[[briefing_shared_blocks]]``) and DB-wins
thereafter, so an admin's web edit survives operator re-runs — the same
seed-once + edit-preservation contract as ``default_briefings`` /
``briefing_configs`` (see ``user_briefings.py``).

The DB rows are overlaid onto ``config.briefing_shared_blocks`` at config-load
time by ``config._apply_shared_blocks``; ``check_shared_blocks`` then reads the
DB-authoritative definitions.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import db

logger = logging.getLogger(__name__)


def import_from_config(db_path: Path, blocks: "list") -> int:
    """Seed ``shared_block_configs`` rows from config-declared shared blocks.

    Inserts only names that don't already have a DB row (seed-once,
    edit-preservation). Idempotent across restarts. Returns the count written.
    ``blocks`` is a list of ``config.BriefingSharedBlock`` (or anything with the
    same attributes).
    """
    if not Path(db_path).exists():
        return 0

    written = 0
    with db.get_db(db_path) as conn:
        existing = {r.name for r in db.list_shared_block_configs(conn)}
        for b in blocks or []:
            name = getattr(b, "name", "")
            cron = getattr(b, "cron", "")
            if not name or not cron or name in existing:
                continue
            try:
                db.upsert_shared_block_config(
                    conn,
                    name=name,
                    cron=cron,
                    title=getattr(b, "title", "") or "",
                    directive=getattr(b, "directive", None),
                    render_mode=getattr(b, "render_mode", "synthesis") or "synthesis",
                    enabled=bool(getattr(b, "enabled", True)),
                    trusted=bool(getattr(b, "trusted", False)),
                    sources=list(getattr(b, "sources", []) or []),
                )
                written += 1
                logger.info("shared block seeded from config name=%s", name)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("shared block seed failed name=%s: %s", name, e)

    if written:
        logger.info(
            "shared_blocks_store: seeded %d new definition(s) from config", written,
        )
    return written
