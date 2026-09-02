"""Default scheduled jobs for the native feeds module.

When a user has the feeds module enabled, the istota scheduler auto-seeds
these jobs into the ``scheduled_jobs`` table. Names use the
``_module.feeds.`` prefix so CRON.md orphan deletion never touches them.

Two jobs are auto-seeded. ``run_scheduled`` polls every feed whose
``next_poll_at`` is in the past, every five minutes. ``prune`` applies
the entry retention policy once a day. Users wanting a
narrated/observable poll can add their own prompt-based job to CRON.md.

Phase 1.3 (unified credential resolution refactor): jobs are dispatched
as skill-tasks (``skill`` + ``skill_args``) rather than shell
command-tasks, so the master Fernet key no longer needs to flow into the
subprocess env.
"""

import json
from dataclasses import dataclass

MODULE_PREFIX = "_module.feeds."


@dataclass(frozen=True)
class ModuleJob:
    name: str
    cron: str
    skill: str
    skill_args: tuple[str, ...]


DEFAULT_JOBS: tuple[ModuleJob, ...] = (
    ModuleJob(
        name=f"{MODULE_PREFIX}run_scheduled",
        cron="*/5 * * * *",
        skill="feeds",
        skill_args=("run-scheduled",),
    ),
    # Entry retention. Daily, at an off-peak minute that is not on the
    # five-minute poll boundary, so the two jobs do not contend for the
    # same per-user SQLite write lock on every hour they share.
    ModuleJob(
        name=f"{MODULE_PREFIX}prune",
        cron="17 3 * * *",
        skill="feeds",
        skill_args=("prune",),
    ),
)


def jobs_for_user(feeds_context, user_id: str) -> list[dict]:
    """Render module job definitions for a specific user.

    Returns dicts with ``name``, ``cron``, ``skill``, ``skill_args``
    (JSON-encoded ``list[str]``). Consumed by
    ``_sync_feeds_module_jobs``. Always seeds both jobs: the poller
    cheaply no-ops when no feed is due, and the prune cheaply no-ops
    when nothing is over its age window or its per-feed maximum.
    """
    if feeds_context is None:
        return []
    return [
        {
            "name": j.name,
            "cron": j.cron,
            "skill": j.skill,
            "skill_args": json.dumps(list(j.skill_args)),
        }
        for j in DEFAULT_JOBS
    ]
