"""A scheduled job the scheduler switched off after N consecutive failures.

The first of the three *silent* gaps: three sites in `scheduler.py` disable a
job, write a `task_logs` warning, and tell nobody. A module-prefixed job gets
rescued on a later sweep; a user's own CRON.md job just stops running, and the
first sign of it is a briefing that never arrives.

**The close predicate is `auto_disabled_at IS NULL`** — the daemon's own
column, which is the one that says whether this job is being held back. It is
not `enabled`: that is the user's intent, authored by CRON.md, and a job the
user has switched off by hand was never the condition this row is about.

Three things lift a suspension and each of them genuinely ends the condition: a
successful run (`reset_scheduled_job_failures`), `!cron enable`
(`db.enable_scheduled_job`), and an edit in CRON.md to what the job dispatches,
which `sync_cron_jobs_to_db` reads as the user fixing it. That third path is
worth knowing about here, because it is the one that closes the row with no
surface having been touched. A deleted job has no row to read at all.

There is no HTTP endpoint that re-enables a job — the verb is `!cron enable
<name>` in chat, and the panel does not send chat commands. So the view carries
no actions and says what to do in its `status_note`, which is the case that
field exists for. The row is still stored `actionable=1`, per the spec's source
table: something *is* waiting on the user. `list_open` renders it as
`actionable=False` (`row.actionable and bool(view.actions)`), so it lands under
"All" and not under "Needs action", which is right — a filter promising things
to act on should not list one with no button. The consequence to know about is
that `counts()` is plain SQL over the stored column, so its `actionable` figure
counts these and the panel's does not. Nothing renders that figure today (the
bell shows `open`, and the tab labels come from the list response), which is why
it is recorded here rather than reconciled.
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

# How much of a job name survives into the title. A job name is free text out of
# CRON.md, and the title is stored as `notifications.title` and handed to
# `send_notification(title=…)`, which reaches ntfy as an HTTP header — an
# oversized one is refused by the server and the push is lost with
# `last_delivered_at` correctly null and nothing saying why. Same cap and same
# reasoning as `confirmations.describe_email`. `flatten` does not truncate; the
# caller always does.
_NAME_CHARS = 80

# `cron_loader._MODULE_JOB_PREFIX`, re-spelled rather than imported: this module
# must stay cheap to import from a daemon hot path, and the thing it names is a
# private constant either way. See :func:`should_notify` for what it decides.
MODULE_JOB_PREFIX = "_module."


def dedup_key(job_id: int | str) -> str:
    """``job:{id}``, verbatim — see the note on the confirmation source."""
    return f"job:{job_id}"


def title_for(job_name: str, fail_count: int) -> str:
    """The one-line label. One spelling for the producer and the resolver."""
    from ..confirmations import flatten

    name = flatten(job_name or "")[:_NAME_CHARS] or "a scheduled job"
    return f"Scheduled job '{name}' was switched off after {fail_count} failures"


def body_for(job_name: str, cron_expression: str, last_error: str | None) -> str:
    from ..confirmations import flatten

    name = flatten(job_name or "")[:_NAME_CHARS] or "the job"
    cron = flatten(cron_expression or "")[:_NAME_CHARS]
    lead = f"'{name}' failed on every attempt and will not run again"
    lead += f" on its {cron} schedule." if cron else "."
    error = flatten(last_error or "")[:_ERROR_CHARS]
    tail = f"Last error: {error}" if error else "No error text was recorded."
    return f"{lead} {tail}"


def is_module_job(job_name: str) -> bool:
    """Whether this job belongs to a module rather than to the user's CRON.md.

    Tested against the **raw** name, never a flattened one: `flatten` maps `_` to
    a space (it is a markdown emphasis character), so `_module.health.garmin_sync`
    flattens to something starting with `module.` and the check would never fire.
    """
    return (job_name or "").startswith(MODULE_JOB_PREFIX)


def should_notify(job_name: str) -> bool:
    """Whether a disable of this job is worth telling the user about.

    **A module job is not.** `_sync_module_jobs` lifts the suspension on every
    `_module.*` row on an hourly cooldown, unconditionally — it is a retry, not a repair, and its own comment says a
    genuinely broken row is expected to loop through disable and rescue at that
    rate. A row here would ride that loop: raised on the disable, marked `stale`
    on the next panel read after the rescue zeroed the counter, then *reopened*
    an hour later — and the reopen branch delivers, so a permanently broken
    module job becomes an hourly push about something the user has no verb to
    fix. `!cron enable` operates on CRON.md and a module job is not in it.

    The user is not left uninformed by this. A module job that fails for a
    reason they can act on has a source of its own saying so in terms they can
    act on — a dead Garmin credential raises `connected_service`, not "a job
    named `_module.health.garmin_sync` was switched off".
    """
    return not is_module_job(job_name)


def note_for(job_name: str) -> str:
    """Why the panel has no button, and what to do instead.

    Split from `body_for` because the body is also the push text, and a push
    that lands in Talk is already on the surface where `!cron enable` is typed.
    """
    from ..confirmations import flatten

    raw = job_name or ""
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
        if job.auto_disabled_at is None:
            # The suspension lifted: a successful run, `!cron enable`, or a
            # definition edit in CRON.md. See the module docstring for why this
            # is the predicate and `enabled` is not.
            return None

        return NotificationView(
            title=title_for(job.name, job.consecutive_failures or 0),
            body=body_for(job.name, job.cron_expression, job.last_error),
            severity=row.severity,
            status_note=note_for(job.name),
        )


RESOLVER = CronJobResolver()
