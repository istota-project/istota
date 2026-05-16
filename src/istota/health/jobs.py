"""Default scheduled jobs for the health module.

Mirrors :mod:`istota.feeds.jobs` and :mod:`istota.money.jobs`. The
scheduler auto-seeds these into ``scheduled_jobs`` for each user where
the health module is enabled *and* Garmin tokens are stored — there is
no point running a 6-hourly job for users who haven't connected
Garmin, and the auto-seed sync pass would otherwise create rows that
fail every cron tick with ``no Garmin tokens``.

Job rows use ``_module.health.`` prefix so CRON.md orphan deletion
never touches them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from istota.health import db as health_db
from istota.health import garmin as health_garmin
from istota.health._migrate import ensure_initialised
from istota.health.models import HealthContext


MODULE_PREFIX = "_module.health."


@dataclass(frozen=True)
class ModuleJob:
    name: str
    cron: str
    skill: str
    skill_args: tuple[str, ...]


# Garmin sync: every 6 hours, 2-day lookback so late uploads from the
# watch (which can lag by several hours) still get caught.
GARMIN_SYNC_JOB = ModuleJob(
    name=f"{MODULE_PREFIX}garmin_sync",
    cron="0 */6 * * *",
    skill="health",
    skill_args=("garmin-sync", "--days-back", "2"),
)


def jobs_for_user(health_ctx: HealthContext | None, user_id: str) -> list[dict]:
    """Render module job definitions for a user.

    Returns an empty list when the user has no Garmin tokens, so the
    scheduler's idempotent sync pass cleans up any stale row.
    """
    if health_ctx is None:
        return []
    # Inspect the user's health DB to decide whether to seed Garmin sync.
    try:
        ensure_initialised(health_ctx)
        with health_db.connect(health_ctx.db_path) as conn:
            tokens = health_garmin.load_tokens(conn)
    except Exception:
        # If the DB isn't readable yet (fresh user, race during init),
        # don't seed — the next pass will pick it up.
        return []
    if not tokens:
        return []
    return [
        {
            "name": GARMIN_SYNC_JOB.name,
            "cron": GARMIN_SYNC_JOB.cron,
            "skill": GARMIN_SYNC_JOB.skill,
            "skill_args": json.dumps(list(GARMIN_SYNC_JOB.skill_args)),
        }
    ]
