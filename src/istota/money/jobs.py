"""Default scheduled jobs for the money module.

When a user has the money module enabled, the istota scheduler auto-seeds
these jobs into the ``scheduled_jobs`` table. Names use the
``_module.money.`` prefix so they are not subject to CRON.md orphan
deletion — users cannot rename or remove them via CRON.md, only disable
them in the DB.

Phase 1.3 (unified credential resolution refactor): jobs are dispatched
as skill-tasks (``skill`` + ``skill_args``) rather than shell
command-tasks, so the master Fernet key no longer needs to flow into the
subprocess env. The skill module's ``setup_env`` and the env-first
loader resolve ``MONEY_USER`` and the Monarch credential triple.

Only ``run-scheduled`` is auto-seeded; it folds in an opportunistic
monarch sync (when configured) plus the invoice schedule check. Users
who want narrated/observable monarch syncs add their own prompt-based
job to CRON.md.
"""

import json
from dataclasses import dataclass

MODULE_PREFIX = "_module.money."


@dataclass(frozen=True)
class ModuleJob:
    name: str
    cron: str
    skill: str
    skill_args: tuple[str, ...]


DEFAULT_JOBS: tuple[ModuleJob, ...] = (
    ModuleJob(
        name=f"{MODULE_PREFIX}run_scheduled",
        cron="0 8 * * *",
        skill="money",
        skill_args=("run-scheduled",),
    ),
)


def jobs_for_user(user_context, user_id: str) -> list[dict]:
    """Render module job definitions for a specific user.

    Returns dicts with ``name``, ``cron``, ``skill``, ``skill_args``
    (JSON-encoded ``list[str]``). Skips ``run-scheduled`` entirely when
    neither monarch nor invoicing is configured (nothing periodic to do).
    """
    has_periodic_work = bool(
        user_context.monarch_config_path or user_context.invoicing_config_path
    )
    if not has_periodic_work and user_context.db_path:
        from istota.money.config_store import has_invoicing_data, has_monarch_data
        has_periodic_work = (
            has_monarch_data(user_context.db_path)
            or has_invoicing_data(user_context.db_path)
        )
    if not has_periodic_work:
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
