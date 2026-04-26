"""Default scheduled jobs for the money module.

When a user has a ``[[resources]] type = "money"`` entry in their istota
config, the istota scheduler auto-seeds these jobs into the
``scheduled_jobs`` table. Names use the ``_module.money.`` prefix so they
are not subject to CRON.md orphan deletion — users cannot rename or remove
them via CRON.md, only disable them in the DB.

Job commands invoke the money skill through ``istota-skill``, which calls
:func:`istota.money.resolve_for_user` in-process — both legacy
(``config_path``) and workspace-mode users are handled uniformly. The
scheduler passes ``MONEY_USER`` to identify the user.

Only ``run-scheduled`` is auto-seeded; it folds in an opportunistic
monarch sync (when configured) plus the invoice schedule check. Users
who want narrated/observable monarch syncs add their own prompt-based
job to CRON.md.
"""

from dataclasses import dataclass

MODULE_PREFIX = "_module.money."


@dataclass(frozen=True)
class ModuleJob:
    name: str
    cron: str
    command_template: str


DEFAULT_JOBS: tuple[ModuleJob, ...] = (
    ModuleJob(
        name=f"{MODULE_PREFIX}run_scheduled",
        cron="0 8 * * *",
        command_template="MONEY_USER={user_id} istota-skill money run-scheduled",
    ),
)


def jobs_for_user(user_context, user_id: str) -> list[dict]:
    """Render module job definitions for a specific user.

    ``user_context`` is the resolved :class:`istota.money.cli.UserContext`.
    Skips ``run-scheduled`` entirely when neither monarch nor invoicing is
    configured (nothing periodic to do).
    """
    has_periodic_work = bool(
        user_context.monarch_config_path or user_context.invoicing_config_path
    )
    if not has_periodic_work:
        return []
    return [
        {
            "name": j.name,
            "cron": j.cron,
            "command": j.command_template.format(user_id=user_id),
        }
        for j in DEFAULT_JOBS
    ]
