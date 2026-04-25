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

A user with no money resource has no module jobs. A user with a money
resource but no monarch config gets only the invoice scheduler. A user
with both gets both.
"""

from dataclasses import dataclass

MODULE_PREFIX = "_module.money."


@dataclass(frozen=True)
class ModuleJob:
    name: str
    cron: str
    command_template: str
    requires: str  # "monarch", "invoicing", or "" (always present)


DEFAULT_JOBS: tuple[ModuleJob, ...] = (
    ModuleJob(
        name=f"{MODULE_PREFIX}monarch_sync",
        cron="0 6 * * *",
        command_template="MONEY_USER={user_id} istota-skill money sync-monarch",
        requires="monarch",
    ),
    ModuleJob(
        name=f"{MODULE_PREFIX}run_scheduled",
        cron="0 8 * * *",
        command_template="MONEY_USER={user_id} istota-skill money run-scheduled",
        requires="invoicing",
    ),
)


def jobs_for_user(user_context, user_id: str) -> list[dict]:
    """Render module job definitions for a specific user.

    ``user_context`` is the resolved :class:`istota.money.cli.UserContext`.
    Filters jobs whose ``requires`` feature is not configured for the user.
    Credentials live on the user's money resource entry in the istota config
    and are loaded in-process by the skill — no env-var indirection needed.
    """
    out: list[dict] = []
    for j in DEFAULT_JOBS:
        if j.requires == "monarch" and not user_context.monarch_config_path:
            continue
        if j.requires == "invoicing" and not user_context.invoicing_config_path:
            continue
        out.append({
            "name": j.name,
            "cron": j.cron,
            "command": j.command_template.format(user_id=user_id),
        })
    return out
