"""Schema init + biomarker_refs seeding for the health module.

:func:`ensure_initialised` is the single entry point — wires up the dirs,
runs schema migrations, and (once per DB) seeds the canonical
``biomarker_refs`` table from the bundled JSON. Re-runs are gated on a
``biomarker_refs_seeded_at`` sentinel in ``schema_meta``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from importlib.resources import as_file, files

from istota.health import db as health_db
from istota.health.models import HealthContext


logger = logging.getLogger(__name__)


_SEED_SENTINEL_KEY = "biomarker_refs_seeded_at"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_bundled_refs() -> list[dict] | None:
    """Read the package-shipped ``biomarker_refs.json``."""
    try:
        resource = files("istota.health").joinpath("data/biomarker_refs.json")
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    try:
        with as_file(resource) as concrete:
            if not concrete.is_file():
                return None
            text = concrete.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("health_biomarker_refs_unparseable error=%s", e)
        return None
    if not isinstance(data, list):
        return None
    return data


def seed_biomarker_refs(ctx: HealthContext) -> int | None:
    """Seed the ``biomarker_refs`` table from the bundled JSON.

    Returns the number of rows seeded on first run, ``None`` if already
    seeded or no bundled file is available.
    """
    health_db.init_db(ctx.db_path)
    with health_db.connect(ctx.db_path) as conn:
        already = conn.execute(
            "SELECT 1 FROM schema_meta WHERE key = ?", (_SEED_SENTINEL_KEY,),
        ).fetchone()
        if already:
            return None
        refs = _read_bundled_refs()
        if not refs:
            return None
        try:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                (_SEED_SENTINEL_KEY, _iso_now()),
            )
        except sqlite3.IntegrityError:
            return None
        for ref in refs:
            try:
                health_db.upsert_biomarker_ref(conn, ref)
            except (KeyError, sqlite3.Error) as e:
                logger.warning(
                    "health_biomarker_ref_skip name=%s error=%s",
                    ref.get("name", "?"), e,
                )
                continue
        conn.commit()
    logger.info("health_biomarker_refs_seeded count=%d", len(refs))
    return len(refs)


def ensure_initialised(ctx: HealthContext) -> None:
    """Wire up the health workspace: dirs + schema + seed."""
    ctx.ensure_dirs()
    health_db.init_db(ctx.db_path)
    seed_biomarker_refs(ctx)
