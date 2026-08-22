"""A scheduled job the scheduler switched off after N consecutive failures.

The first of the three *silent* gaps: three sites in `scheduler.py` disable a
job, write a `task_logs` warning, and tell nobody. A module-prefixed job gets
rescued on a later sweep; a user's own CRON.md job just stops running, and the
first sign of it is a briefing that never arrives.

**The close predicate is `consecutive_failures == 0`, not `enabled`.** That
looks backwards and is not. `sync_cron_jobs_to_db` treats CRON.md as
authoritative for `enabled` ("file true → DB 1"), and `check_scheduled_jobs`
calls it on every tick, so a job defined in CRON.md is switched back on within
one scheduler tick of being auto-disabled — a row watching `enabled` would go
`stale` minutes after it was raised, leaving the user a push about something the
panel then denies all knowledge of. The same sync deliberately preserves the
state columns, so `consecutive_failures` survives it, and every path that really
does end the condition zeroes the counter: a successful run
(`reset_scheduled_job_failures`), `!cron enable` (`db.enable_scheduled_job`), and
the scheduler's module-job rescue. A deleted job has no row to read at all.

There is no HTTP endpoint that re-enables a job — the verb is `!cron enable
<name>` in chat, and the panel does not send chat commands. So the view carries
no actions and says what to do in its `status_note`, which is the case that
field exists for.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from ..config import Config
    from ..notification_sources import NotificationRow, NotificationView
    from ..notification_store import RaiseResult

logger = logging.getLogger(__name__)

SOURCE = "cron_job"
OBJECT_TYPE = "scheduled_job"

# A job that has stopped running is a warning, not a failure of the system: the
# user's other jobs are unaffected and nothing was lost.
SEVERITY = "warning"

# How much of the last error survives into the body. `last_error` is the failed
# task's own result text, which on a prompt job is model-authored, so it is
# flattened as well as cut.
_ERROR_CHARS = 300

# `cron_loader._MODULE_JOB_PREFIX`, re-spelled rather than imported: this module
# must stay cheap to import from a daemon hot path, and the thing it names is a
# private constant either way. Used only to word the note correctly — `!cron
# enable` works on CRON.md jobs, and a module job is not in CRON.md; it comes
# back on the scheduler's own rescue sweep instead.
MODULE_JOB_PREFIX = "_module."


def dedup_key(job_id: int | str) -> str:
    """``job:{id}``, verbatim — see the note on the confirmation source."""
    return f"job:{job_id}"


def title_for(job_name: str, fail_count: int) -> str:
    """The one-line label. One spelling for the producer and the resolver."""
    from ..confirmations import flatten

    name = flatten(job_name or "") or "a scheduled job"
    return f"Scheduled job '{name}' was switched off after {fail_count} failures"


def body_for(job_name: str, cron_expression: str, last_error: str | None) -> str:
    from ..confirmations import flatten

    name = flatten(job_name or "") or "the job"
    cron = flatten(cron_expression or "")
    lead = f"'{name}' failed on every attempt and will not run again"
    lead += f" on its {cron} schedule." if cron else "."
    error = flatten(last_error or "")[:_ERROR_CHARS]
    tail = f"Last error: {error}" if error else "No error text was recorded."
    return f"{lead} {tail}"


def note_for(job_name: str) -> str:
    """Why the panel has no button, and what to do instead.

    Split from `body_for` because the body is also the push text, and a push
    that lands in Talk is already on the surface where `!cron enable` is typed.
    """
    from ..confirmations import flatten

    raw = job_name or ""
    # Tested against the **raw** name. `flatten` maps `_` to a space (it is a
    # markdown emphasis character), so `_module.health.garmin_sync` flattens to
    # something that starts with `module.` and the branch would never be taken.
    if raw.startswith(MODULE_JOB_PREFIX):
        return (
            "This job is managed by one of the bot's own modules. It is "
            "re-created automatically once the underlying problem is fixed."
        )
    # The job is named only when flattening left it untouched. A job name is
    # user-authored free text out of CRON.md and this note is delivered into
    # Talk, which renders markdown — but a *flattened* name is no longer the
    # string `!cron enable` takes, so printing it would be an instruction that
    # does not work. Either the name survives verbatim or it is not named.
    safe = flatten(raw)
    verb = f"`!cron enable {safe}`" if safe and safe == raw else "`!cron enable <name>`"
    return f"Fix what it was failing on, then re-enable it with {verb}."


def write(
    conn: "sqlite3.Connection",
    user_id: str,
    *,
    job_id: int,
    job_name: str,
    fail_count: int,
    cron_expression: str = "",
    last_error: str | None = None,
    room_token: str | None = None,
) -> "RaiseResult | None":
    """Write the row on the caller's connection, inside its transaction.

    Every one of the three producer sites sits inside `process_one_task`'s write
    transaction, so the caller buffers the result and hands it to
    `deliver_pending` after the `with` block — see the store's module docstring.
    """
    from ..notification_store import write_notification

    return write_notification(
        conn, user_id,
        source=SOURCE,
        dedup_key=dedup_key(job_id),
        title=title_for(job_name, fail_count),
        body=body_for(job_name, cron_expression, last_error),
        severity=SEVERITY,
        actionable=True,
        object_type=OBJECT_TYPE,
        object_id=str(job_id),
        params={"job_name": job_name, "failures": int(fail_count)},
        room_token=room_token,
        purpose="alert",
    )


def resolve_for_job(
    conn: "sqlite3.Connection", user_id: str, job_id: int, *, by: str,
) -> int:
    """Close the row for a job that has just recovered or been re-enabled."""
    from ..notification_store import resolve_by_object

    return resolve_by_object(
        conn, user_id, SOURCE, OBJECT_TYPE, str(job_id), by=by,
    )


def _job_id(row: "NotificationRow") -> int | None:
    """The row's ``object_id`` as an integer, or None. See the confirmation source."""
    try:
        return int(str(row.object_id).strip())
    except (TypeError, ValueError):
        logger.warning(
            "notification %s names a non-numeric job id %r", row.id, row.object_id,
        )
        return None


class CronJobResolver:
    source = SOURCE
    auto_resolve_on_seen = False

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        from .. import db
        from ..notification_sources import NotificationView

        job_id = _job_id(row)
        if job_id is None:
            return None

        job = db.get_scheduled_job(conn, job_id)
        if job is None:
            # Deleted, or an orphan the CRON.md sync cleaned up. Nothing left to
            # re-enable.
            return None
        if job.user_id != row.user_id:
            logger.error(
                "notification %s belongs to %r but names %r's job %s",
                row.id, row.user_id, job.user_id, job_id,
            )
            return None
        if (job.consecutive_failures or 0) == 0:
            # Recovered: a successful run, `!cron enable`, or the module rescue.
            # See the module docstring for why this is the predicate and
            # `enabled` is not.
            return None

        return NotificationView(
            title=title_for(job.name, job.consecutive_failures or 0),
            body=body_for(job.name, job.cron_expression, job.last_error),
            severity=row.severity,
            status_note=note_for(job.name),
        )


RESOLVER = CronJobResolver()
