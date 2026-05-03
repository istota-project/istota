"""Default scheduled jobs for the native feeds module.

When a user has a ``[[resources]] type = "feeds"`` entry in their istota
config, the istota scheduler auto-seeds these jobs into the
``scheduled_jobs`` table. Mirrors :mod:`istota.money.jobs` — names use the
``_module.feeds.`` prefix so CRON.md orphan deletion never touches them.

Only ``run-scheduled`` is auto-seeded; it polls every feed whose
``next_poll_at`` is in the past. Users wanting a narrated/observable poll
can add their own prompt-based job to CRON.md.
"""

from dataclasses import dataclass

MODULE_PREFIX = "_module.feeds."


@dataclass(frozen=True)
class ModuleJob:
    name: str
    cron: str
    command_template: str


DEFAULT_JOBS: tuple[ModuleJob, ...] = (
    ModuleJob(
        name=f"{MODULE_PREFIX}run_scheduled",
        cron="*/5 * * * *",
        command_template="FEEDS_USER={user_id} istota-skill feeds run-scheduled",
    ),
)


def jobs_for_user(feeds_context, user_id: str) -> list[dict]:
    """Render module job definitions for a specific user.

    ``feeds_context`` is a resolved :class:`istota.feeds.FeedsContext`.
    Currently always seeds ``run-scheduled`` — there is always work to do
    for any user with a feeds resource (the poller checks for due feeds
    every tick and no-ops cheaply when nothing is due).
    """
    if feeds_context is None:
        return []
    return [
        {
            "name": j.name,
            "cron": j.cron,
            "command": j.command_template.format(user_id=user_id),
        }
        for j in DEFAULT_JOBS
    ]
