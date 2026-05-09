"""One-shot migrator: framework ``istota.db`` → per-user ``location.db``.

Idempotent. Safe to re-run after success — gated on the
``location_legacy_db_migrated_at`` sentinel in the per-user
``schema_meta`` table.

Invocation:

    python -m istota.location._migrate

The CLI iterates :func:`list_users(config)`, resolves each user's
:class:`LocationContext`, and copies their rows from framework
``location_pings`` / ``places`` / ``visits`` / ``dismissed_clusters`` /
``location_state`` into the per-user file. Place IDs are preserved
verbatim; FK orphans (rows pointing at deleted parents) are NULLed
before commit.

Stage 4 (a later deploy) drops the framework tables. Until then this
script can be re-run as many times as desired — already-migrated users
return zero counts.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

from istota.location import db as location_db
from istota.location._loader import list_users, resolve_for_user
from istota.location.models import LocationContext


logger = logging.getLogger(__name__)


_SENTINEL_KEY = "location_legacy_db_migrated_at"
_SCHEMA_ONLY_KEY = "schema_only_at"


_LEGACY_TABLES = (
    "places",
    "visits",
    "dismissed_clusters",
    "location_pings",
    "location_state",
)


class MigrationConflict(Exception):
    """Refuse to migrate into a target ``location.db`` that already
    holds user data without the migration sentinel."""


def _table_exists(conn: sqlite3.Connection, schema: str, table: str) -> bool:
    row = conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _read_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (key,),
    ).fetchone()
    return row["value"] if row else None


def _target_has_user_data(conn: sqlite3.Connection) -> bool:
    """True if any of the five user tables in target has rows."""
    for tbl in _LEGACY_TABLES:
        row = conn.execute(f"SELECT 1 FROM {tbl} LIMIT 1").fetchone()
        if row is not None:
            return True
    return False


def migrate_legacy_data(
    framework_db: Path, ctx: LocationContext,
) -> dict[str, int]:
    """Copy ``ctx.user_id``'s rows from ``framework_db`` into ``ctx.db_path``.

    Idempotent via the ``location_legacy_db_migrated_at`` sentinel.
    Returns a per-table count of rows copied (zeros on already-migrated).
    The dict also carries a synthetic ``_orphans_nulled`` sub-dict with
    the row counts for the FK-orphan NULL pass.
    """
    framework_db = Path(framework_db)
    location_db.init_db(ctx.db_path)

    conn = sqlite3.connect(ctx.db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        # FK pragma must be set BEFORE any BEGIN — it's a no-op inside a
        # transaction. We turn it OFF for the bulk copy because we'll
        # validate with foreign_key_check after the orphan-NULL pass.
        conn.execute("PRAGMA foreign_keys = OFF")

        existing_sentinel = _read_meta(conn, _SENTINEL_KEY)
        if existing_sentinel:
            return {tbl: 0 for tbl in _LEGACY_TABLES} | {"_orphans_nulled": {}}

        conn.execute("ATTACH DATABASE ? AS framework", (str(framework_db),))
        try:
            # If the framework no longer has the legacy tables (e.g. Stage 4
            # ran first), this is already migrated — write the sentinel.
            if not _table_exists(conn, "framework", "location_pings"):
                conn.execute("BEGIN")
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) "
                    "VALUES(?, datetime('now'))",
                    (_SENTINEL_KEY,),
                )
                conn.commit()
                return {tbl: 0 for tbl in _LEGACY_TABLES} | {
                    "_orphans_nulled": {},
                }

            # Pre-check: if target already has user rows but no sentinel,
            # refuse — the schema_only_at sentinel does NOT count as
            # "user data" so a freshly-init_db'd file is fine to migrate
            # into.
            if _target_has_user_data(conn):
                raise MigrationConflict(
                    f"location.db at {ctx.db_path} has user data without "
                    f"migration sentinel; refuse to risk duplicates"
                )

            uid = ctx.user_id
            counts: dict[str, int] = {}
            conn.execute("BEGIN")

            cur = conn.execute(
                """
                INSERT INTO places (id, name, lat, lon, radius_meters,
                                    category, created_at, notes)
                SELECT id, name, lat, lon, radius_meters, category,
                       created_at, notes
                FROM framework.places WHERE user_id = ?
                """,
                (uid,),
            )
            counts["places"] = cur.rowcount or 0

            cur = conn.execute(
                """
                INSERT INTO visits (id, place_id, place_name, entered_at,
                                    exited_at, duration_sec, ping_count)
                SELECT id, place_id, place_name, entered_at, exited_at,
                       duration_sec, ping_count
                FROM framework.visits WHERE user_id = ?
                """,
                (uid,),
            )
            counts["visits"] = cur.rowcount or 0

            cur = conn.execute(
                """
                INSERT INTO dismissed_clusters (id, lat, lon, radius_meters,
                                                dismissed_at)
                SELECT id, lat, lon, radius_meters, dismissed_at
                FROM framework.dismissed_clusters WHERE user_id = ?
                """,
                (uid,),
            )
            counts["dismissed_clusters"] = cur.rowcount or 0

            cur = conn.execute(
                """
                INSERT INTO location_pings (id, timestamp, received_at, lat,
                    lon, altitude, accuracy, speed, course, battery,
                    activity_type, wifi, place_id, visit_id)
                SELECT id, timestamp, received_at, lat, lon, altitude,
                       accuracy, speed, course, battery, activity_type, wifi,
                       place_id, visit_id
                FROM framework.location_pings WHERE user_id = ?
                """,
                (uid,),
            )
            counts["location_pings"] = cur.rowcount or 0

            cur = conn.execute(
                """
                INSERT INTO location_state (id, current_place_id,
                    current_visit_id, consecutive_count, last_ping_place_id,
                    exit_started_at)
                SELECT 1, current_place_id, current_visit_id,
                       consecutive_count, last_ping_place_id, exit_started_at
                FROM framework.location_state WHERE user_id = ?
                """,
                (uid,),
            )
            counts["location_state"] = cur.rowcount or 0

            # FK-orphan NULL pass — production data may have stale refs
            # (places deleted manually etc.). Run before the FK check.
            orphans: dict[str, int] = {}

            cur = conn.execute(
                """
                UPDATE location_pings
                SET visit_id = NULL
                WHERE visit_id IS NOT NULL
                  AND visit_id NOT IN (SELECT id FROM visits)
                """
            )
            orphans["location_pings.visit_id"] = cur.rowcount or 0

            cur = conn.execute(
                """
                UPDATE location_pings
                SET place_id = NULL
                WHERE place_id IS NOT NULL
                  AND place_id NOT IN (SELECT id FROM places)
                """
            )
            orphans["location_pings.place_id"] = cur.rowcount or 0

            cur = conn.execute(
                """
                UPDATE visits
                SET place_id = NULL
                WHERE place_id IS NOT NULL
                  AND place_id NOT IN (SELECT id FROM places)
                """
            )
            orphans["visits.place_id"] = cur.rowcount or 0

            cur = conn.execute(
                """
                UPDATE location_state
                SET current_place_id = NULL
                WHERE current_place_id IS NOT NULL
                  AND current_place_id NOT IN (SELECT id FROM places)
                """
            )
            orphans["location_state.current_place_id"] = cur.rowcount or 0

            cur = conn.execute(
                """
                UPDATE location_state
                SET current_visit_id = NULL
                WHERE current_visit_id IS NOT NULL
                  AND current_visit_id NOT IN (SELECT id FROM visits)
                """
            )
            orphans["location_state.current_visit_id"] = cur.rowcount or 0

            cur = conn.execute(
                """
                UPDATE location_state
                SET last_ping_place_id = NULL
                WHERE last_ping_place_id IS NOT NULL
                  AND last_ping_place_id NOT IN (SELECT id FROM places)
                """
            )
            orphans["location_state.last_ping_place_id"] = cur.rowcount or 0

            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) "
                "VALUES(?, datetime('now'))",
                (_SENTINEL_KEY,),
            )
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE framework")

        conn.execute("PRAGMA foreign_keys = ON")
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_violations:
            raise RuntimeError(
                f"foreign_key_check failed after migration: {fk_violations}"
            )
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity and integrity[0] != "ok":
            raise RuntimeError(
                f"integrity_check failed after migration: {integrity[0]}"
            )

        counts["_orphans_nulled"] = orphans
        logger.info(
            "location_migrated user=%s counts=%s orphans=%s",
            ctx.user_id, counts, orphans,
        )
        return counts
    finally:
        conn.close()


def main() -> int:
    from istota.config import load_config

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = load_config()
    framework_db = Path(config.db_path)
    if not framework_db.exists():
        print("framework DB missing; nothing to migrate", file=sys.stderr)
        return 0

    failures: list[tuple[str, Exception]] = []
    for user_id in list_users(config):
        try:
            ctx = resolve_for_user(user_id, config)
            counts = migrate_legacy_data(framework_db, ctx)
            print(f"{user_id}: {counts}")
        except Exception as e:  # noqa: BLE001
            failures.append((user_id, e))
            print(f"{user_id}: FAILED {e}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
