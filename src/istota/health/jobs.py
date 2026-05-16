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
import logging
from dataclasses import dataclass

from istota.health import garmin as health_garmin
from istota.health.models import HealthContext


logger = logging.getLogger(__name__)


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

    Returns an empty list when the user has no Garmin tokens (per the
    framework's encrypted ``secrets`` table) so the scheduler's
    idempotent sync pass cleans up any stale row.
    """
    if health_ctx is None or health_ctx.framework_db_path is None:
        return []
    try:
        tokens = health_garmin.load_tokens(health_ctx.framework_db_path, user_id)
    except Exception as exc:  # noqa: BLE001
        # L7: log instead of silently swallowing. A SQLite lock,
        # corrupted DB, or missing ISTOTA_SECRET_KEY all degrade to "no
        # Garmin sync for this user this tick" — operators need a signal.
        logger.warning(
            "health jobs_for_user probe failed for user=%s: %s", user_id, exc,
        )
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
