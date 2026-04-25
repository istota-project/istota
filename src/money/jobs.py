"""Default scheduled jobs for the money module.

When a user has a ``[[resources]] type = "money"`` entry in their istota
config, the istota scheduler auto-seeds these jobs into the
``scheduled_jobs`` table. Names use the ``_module.money.`` prefix so they
are not subject to CRON.md orphan deletion — users cannot rename or remove
them via CRON.md, only disable them in the DB.

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
        command_template=(
            "MONEYMAN_CONFIG={config_path}{secrets_env} "
            "money --user {user_key} sync-monarch"
        ),
        requires="monarch",
    ),
    ModuleJob(
        name=f"{MODULE_PREFIX}run_scheduled",
        cron="0 8 * * *",
        command_template=(
            "MONEYMAN_CONFIG={config_path}{secrets_env} "
            "money --user {user_key} run-scheduled"
        ),
        requires="invoicing",
    ),
)


def jobs_for_user(
    user_context,
    config_path: str,
    user_key: str,
    *,
    secrets_path: str | None = None,
) -> list[dict]:
    """Render module job definitions for a specific user.

    ``user_context`` is a ``money.cli.UserContext``. Filters jobs whose
    ``requires`` feature is not configured for the user.

    If ``secrets_path`` is given, it's exported as ``MONEYMAN_SECRETS_FILE``
    in the command so the CLI subprocess reads per-user credentials from
    that file instead of falling back to ``/etc/moneyman/secrets.toml``.
    """
    secrets_env = f" MONEYMAN_SECRETS_FILE={secrets_path}" if secrets_path else ""
    out: list[dict] = []
    for j in DEFAULT_JOBS:
        if j.requires == "monarch" and not user_context.monarch_config_path:
            continue
        if j.requires == "invoicing" and not user_context.invoicing_config_path:
            continue
        out.append({
            "name": j.name,
            "cron": j.cron,
            "command": j.command_template.format(
                config_path=config_path,
                user_key=user_key,
                secrets_env=secrets_env,
            ),
        })
    return out
