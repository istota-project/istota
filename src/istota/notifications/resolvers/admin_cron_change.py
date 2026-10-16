"""An administrator disabled, enabled or deleted one of the user's scheduled jobs.

Raised by the admin scheduled-jobs routes in `webui/app.py` for the job's owner,
never for an administrator acting on a job of their own; the routes decide that.

Fire-and-forget like `admin_briefing_change`: nothing closes it but being seen.
The key is ``job:<id>:<action>``, so a repeat of the same action on the same job
bumps the open row rather than pushing again. The resolver never returns None,
since there is no object left to watch after a delete.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from istota.config import Config
    from istota.notifications.sources import NotificationRow, NotificationView

SOURCE = "admin_cron_change"
MAX_BODY_CHARS = 600
_MAX_NAME_CHARS = 80
_MAX_JOB_CHARS = 120

# The action as the route names it, and the verb the owner reads.
ACTIONS = {"disable": "disabled", "enable": "enabled", "delete": "deleted"}

STATUS_NOTE = "This notice has no in-app action. It clears itself once you have seen it."


def title_for(action: str) -> str:
    return f"An administrator {ACTIONS[action]} your scheduled job"


def dedup_key(job_id: int, action: str) -> str:
    return f"job:{int(job_id)}:{action}"


def _after(action: str, job: str) -> str:
    if action == "disable":
        return f" It will not run until it is turned back on, which you can do with !cron enable {job}."
    if action == "enable":
        return " It runs again on its schedule."
    return " It was removed from your CRON.md."


def body_for(admin_name: str, action: str, job_name: str) -> str:
    from istota.notifications.resolvers.task_alert import flatten

    who = flatten(admin_name)[:_MAX_NAME_CHARS] or "An administrator"
    job = flatten(job_name)[:_MAX_JOB_CHARS] or "a job"
    return f"{who} {ACTIONS[action]} your scheduled job “{job}”.{_after(action, job)}"


def write(
    conn: "sqlite3.Connection", user_id: str, *, admin_name: str, action: str,
    job_id: int, job_name: str,
):
    """Write the row on the caller's connection; the web routes hand it to the daemon."""
    from istota.notifications.store import write_notification

    if action not in ACTIONS:
        raise ValueError(f"unknown scheduled job change {action}")
    return write_notification(
        conn, user_id, source=SOURCE, dedup_key=dedup_key(job_id, action),
        title=title_for(action), body=body_for(admin_name, action, job_name),
        severity="info", params={"action": action},
    )


class AdminCronChangeResolver:
    source = SOURCE
    auto_resolve_on_seen = True

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView":
        """Always a view from the stored text: there is no object to watch."""
        from istota.notifications.resolvers.task_alert import flatten, flatten_body
        from istota.notifications.sources import NotificationView

        return NotificationView(
            title=flatten(row.title) or "An administrator changed your scheduled job",
            body=flatten_body(row.body)[:MAX_BODY_CHARS],
            severity=row.severity,
            actions=(),
            link=None,
            status_note=STATUS_NOTE,
        )


RESOLVER = AdminCronChangeResolver()
