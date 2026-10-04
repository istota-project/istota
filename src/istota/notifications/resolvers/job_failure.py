"""A scheduled job or briefing that failed for good, with a Run-now action.

ISSUE-632. The scheduler withholds an automated task's error from its delivery
room (an apology in the digest's room confuses whoever reads it), so before this
source the user learned of the failure only because the briefing never came, and
then had nothing to run. This row is the private half of that: the user's own
bell, never the room.

**Object-backed, keyed on the failed task.** ``object_id`` is the task id, which
is what the action posts to (``/chat/tasks/{id}/retry``, the same endpoint as
the web chat's Retry button, so the refusal rules cannot drift). The row closes
when a newer occurrence of the same job exists, whatever its outcome: a manual
run, the next scheduled fire, or a later failure that raised its own row. So
only the latest failure of a job stays open, and pressing Run now closes it on
the next read.

**No error text.** The body names the job and what to do. The push follows the
user's alert routing, and the reason the scheduler suppresses the error in the
room is a reason to keep it out of a push as well; the task log has it.

Produced in ``scheduler.process_one_task`` on the terminal-failure branch,
inside its write transaction, buffered and delivered by ``deliver_pending``.
Not raised for a ``_module.*`` job (its module owns its retry) nor when the same
failure switched the job off, since ``cron_job`` already says so.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import _common

if TYPE_CHECKING:
    import sqlite3

    from istota.config import Config
    from istota.notifications.sources import NotificationRow, NotificationView
    from istota.notifications.store import RaiseResult

logger = logging.getLogger(__name__)

SOURCE = "job_failure"
OBJECT_TYPE = "task"
SEVERITY = "warning"

# A job name and a briefing name are free text, and the title reaches ntfy as an
# HTTP header; same cap as `cron_job`.
_NAME_CHARS = 80

_SOURCE_TYPES = ("scheduled", "briefing")


def dedup_key(task_id: int | str) -> str:
    """``task:{id}``: one row per failed occurrence."""
    return _common.object_dedup_key("task", task_id)


def _label(source_type: str, name: str) -> str:
    from istota.confirmations import flatten

    safe = flatten(name or "")[:_NAME_CHARS]
    noun = "Briefing" if source_type == "briefing" else "Scheduled job"
    return f"{noun} '{safe}'" if safe else noun


def title_for(source_type: str, name: str) -> str:
    return f"{_label(source_type, name)} failed"


def body_for(task_id: int, *, manual: bool) -> str:
    lead = "This manual run failed." if manual else (
        "It failed on every attempt and won't run again until its next "
        "scheduled time."
    )
    return f"{lead} Run it now from here, or send `!retry #{task_id}`."


def write(
    conn: "sqlite3.Connection", task, job_name: str,
) -> "RaiseResult | None":
    """Write the row for ``task`` on the caller's connection.

    ``job_name`` is the scheduled job's name or the briefing's name. Returns
    ``None`` for anything that is not an automated occurrence.
    """
    from istota.notifications.store import write_notification

    if task.source_type not in _SOURCE_TYPES:
        return None
    return write_notification(
        conn, task.user_id,
        **_common.row_kwargs(
            source=SOURCE,
            dedup_key=dedup_key(task.id),
            title=title_for(task.source_type, job_name),
            body=body_for(task.id, manual=task.parent_task_id is not None),
            severity=SEVERITY,
            actionable=True,
            object_type=OBJECT_TYPE,
            object_id=str(task.id),
            params={"job_name": job_name, "source_type": task.source_type},
        ),
    )


def _superseded(conn: "sqlite3.Connection", task) -> bool:
    """Is there a newer occurrence of the job ``task`` was one run of?"""
    if task.source_type == "briefing":
        found = conn.execute(
            "SELECT 1 FROM tasks WHERE user_id = ? AND source_type = 'briefing' "
            "AND briefing_name = ? AND id > ? LIMIT 1",
            (task.user_id, task.briefing_name, task.id),
        ).fetchone()
    else:
        found = conn.execute(
            "SELECT 1 FROM tasks WHERE scheduled_job_id = ? AND id > ? LIMIT 1",
            (task.scheduled_job_id, task.id),
        ).fetchone()
    return found is not None


class JobFailureResolver:
    source = SOURCE
    auto_resolve_on_seen = False

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        from istota import db
        from istota.notifications.sources import NotificationAction, NotificationView

        task_id = _common.coerce_object_id(row, noun="task", logger=logger, positive=True)
        if task_id is None:
            return None
        task = db.get_task(conn, task_id)
        if task is None or task.user_id != row.user_id:
            return None
        if task.status != "failed" or task.source_type not in _SOURCE_TYPES:
            return None
        if task.source_type == "scheduled":
            job = (
                db.get_scheduled_job(conn, task.scheduled_job_id)
                if task.scheduled_job_id else None
            )
            if job is None:
                # Deleted from CRON.md: there is nothing left to run.
                return None
        try:
            if _superseded(conn, task):
                return None
        except Exception:  # noqa: BLE001
            logger.warning(
                "could not check for a newer run of task %s; holding the row open",
                task_id, exc_info=True,
            )
        return NotificationView(
            title=row.title,
            body=row.body,
            severity=row.severity,
            actions=(
                NotificationAction(
                    id="run_now", label="Run now", kind="primary", method="POST",
                    endpoint=f"/chat/tasks/{task_id}/retry",
                ),
            ),
        )


RESOLVER = JobFailureResolver()
