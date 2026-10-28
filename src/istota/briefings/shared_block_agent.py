"""Agent shared blocks: the directive runs as a research task, its result is the block.

``dispatch`` queues one ``shared_block_research`` task under the Briefings
identity, shaped as ``group_runs.create_research_task`` shapes group research,
and records it at ``_shared_block_run/agent:<name>``. ``capture`` runs on every
``check_shared_blocks`` pass and writes a finished task's result to
``briefing_shared_blocks/<name>``, always untrusted and as ``__system__``.

Both take a write transaction that re-reads the entry first, because dispatch
runs on the cron thread and on the Run now thread and capture on the dispatch
loop: whichever comes second sees the first one's write. Capture checks for a
live agent row rather than relying on ``shared_block_runs.forget``, since only
the web delete calls it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from istota import db
from istota.briefings import shared_block_runs as runs
from istota.briefings.shared_blocks import SYSTEM_IDENTITY
from istota.browser.owner import BRIEFINGS_IDENTITY

logger = logging.getLogger(__name__)

SOURCE_TYPE = "shared_block_research"
_TERMINAL = ("completed", "failed", "cancelled")
# Consecutive failed or kept-prior runs before one operator alert. The count
# resets on a regenerated block, which re-arms the alert.
ALERT_AFTER = 3


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_entry(conn, name: str) -> dict | None:
    """The block's agent entry, or None when there is none or it is unreadable."""
    row = db.shared_kv_get(conn, runs.RUN_NAMESPACE, runs.AGENT_PREFIX + name)
    if row is None:
        return None
    try:
        entry = json.loads(row["value"])
    except (TypeError, ValueError):
        entry = None
    if not isinstance(entry, dict) or not isinstance(entry.get("task_id"), int):
        logger.warning("shared_block_agent_entry_unreadable name=%s", name)
        return None
    return entry


def _write_entry(conn, name: str, entry: dict) -> None:
    db.shared_kv_set(conn, runs.RUN_NAMESPACE, runs.AGENT_PREFIX + name,
                     json.dumps(entry), "daemon")


def _settle(conn, name: str, entry: dict, now: datetime) -> tuple[str | None, str | None]:
    """Capture one uncaptured entry inside the caller's write transaction.

    Returns ``(outcome, alert)``, or ``(None, None)`` while the task still runs.
    """
    from istota.session.result import is_no_final_answer

    task = db.get_task(conn, entry["task_id"])
    belongs = (
        task is not None
        and task.user_id == BRIEFINGS_IDENTITY
        and task.source_type == SOURCE_TYPE
        and task.briefing_name == entry.get("briefing_name")
    )
    if belongs and task.status not in _TERMINAL:
        return None, None

    row = db.get_shared_block_config(conn, name)
    if row is None or row.render_mode != "agent":
        outcome = runs.DISCARDED
    elif not belongs or task.status != "completed":
        outcome = runs.FAILED
        if task is None:
            error = "task_missing"
        elif not belongs:
            error = "task_mismatch"
        else:
            error = (task.error or task.status)[:200]
        logger.warning("shared_block_agent_failed name=%s task=%s error=%s",
                       name, entry["task_id"], error)
    else:
        text = (task.result or "").strip()
        if not text or is_no_final_answer(text):
            outcome = runs.KEPT_PRIOR
            logger.warning("shared_block_agent_kept_prior name=%s task=%s", name, task.id)
        else:
            from istota.briefings.sources.kv import SHARED_BLOCK_NAMESPACE

            db.shared_kv_set(conn, SHARED_BLOCK_NAMESPACE, name,
                             json.dumps({"text": text, "trusted": False}), SYSTEM_IDENTITY)
            outcome = runs.REGENERATED
            logger.info("shared block %s regenerated from task %s", name, task.id)

    failures = int(entry.get("consecutive_failures") or 0)
    alert = None
    if outcome == runs.REGENERATED:
        failures = 0
    elif outcome in (runs.FAILED, runs.KEPT_PRIOR):
        failures += 1
        if failures == ALERT_AFTER:
            alert = (
                f"Shared block '{name}': the last {failures} agent runs produced "
                "nothing usable. Briefings keep serving the last good content "
                "until it goes stale."
            )
    entry.update(captured=True, outcome=outcome, answered_at=_stamp(now),
                 consecutive_failures=failures)
    _write_entry(conn, name, entry)
    return outcome, alert


def _send_alerts(config, alerts: list[str]) -> None:
    # After the commit: a delivery can take up to its own timeout, and the
    # write lock must not be held across it.
    for message in alerts:
        try:
            from istota.notifications.delivery import send_operator_alert

            send_operator_alert(config, message)
        except Exception:  # noqa: BLE001 - an alert about the block, not the block
            logger.warning("shared_block_agent_alert_failed", exc_info=True)


def dispatch(config, block, *, now: datetime | None = None) -> str:
    """Queue the block's research task, unless one is still running.

    Returns one of ``shared_block_runs``' outcomes: ``queued``, ``in_flight``,
    ``brain_unavailable`` or ``failed``. A finished task not yet captured is
    captured first, so a Run now between completion and the next capture pass
    does not lose its result.
    """
    from istota.brain import primary_brain_unavailable

    now = now or datetime.now(timezone.utc)
    name = block.name
    alerts: list[str] = []
    try:
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            entry = _read_entry(conn, name)
            if entry is not None and not entry.get("captured"):
                outcome, alert = _settle(conn, name, entry, now)
                if outcome is None:
                    return runs.IN_FLIGHT
                if alert:
                    alerts.append(alert)
            available, _reason = primary_brain_unavailable(config.brain)
            if not available:
                logger.info("shared block %s skipped — primary brain unavailable", name)
                result = runs.BRAIN_UNAVAILABLE
            else:
                briefing_name = (
                    f"shared_block:{name}:"
                    f"{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
                )
                # The directive alone enters the task: no conversation, no
                # attachments, nothing gathered beforehand.
                task_id = db.create_task(
                    conn, user_id=BRIEFINGS_IDENTITY, source_type=SOURCE_TYPE,
                    prompt=block.directive or "", briefing_name=briefing_name,
                    queue="background", skip_log_channel=True,
                )
                _write_entry(conn, name, {
                    "task_id": task_id,
                    "briefing_name": briefing_name,
                    "queued_at": _stamp(now),
                    "captured": False,
                    "consecutive_failures": int((entry or {}).get("consecutive_failures") or 0),
                })
                logger.info("shared block %s queued task %s", name, task_id)
                result = runs.QUEUED
    except Exception as exc:  # noqa: BLE001 - the connection closes uncommitted
        logger.error("shared_block_agent_dispatch_failed name=%s err=%s", name, exc)
        return runs.FAILED
    _send_alerts(config, alerts)
    return result


def capture(config) -> list[str]:
    """Settle every agent entry whose task has finished. Never raises.

    Runs inline on the dispatch loop, so each write uses the loop's short busy
    timeout and a contended entry waits for the next pass. Returns the names
    settled this pass.
    """
    if not db.database_present(config.db_path):
        return []
    busy = config.scheduler.main_loop_read_timeout_ms or None
    try:
        with db.get_db(config.db_path, busy_timeout_ms=busy) as conn:
            rows = conn.execute(
                "SELECT key FROM shared_kv WHERE namespace = ? AND key LIKE ? ORDER BY key",
                (runs.RUN_NAMESPACE, runs.AGENT_PREFIX + "%"),
            ).fetchall()
            pending = []
            for row in rows:
                name = row["key"][len(runs.AGENT_PREFIX):]
                entry = _read_entry(conn, name)
                if entry is not None and not entry.get("captured"):
                    pending.append((name, entry["task_id"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("shared_block_agent_capture_skipped err=%s", exc)
        return []

    settled: list[str] = []
    alerts: list[str] = []
    now = datetime.now(timezone.utc)
    for name, task_id in pending:
        try:
            with db.get_db(config.db_path, busy_timeout_ms=busy) as conn:
                conn.execute("BEGIN IMMEDIATE")
                entry = _read_entry(conn, name)
                # A dispatch may have settled it and queued the next task since.
                if entry is None or entry.get("captured") or entry["task_id"] != task_id:
                    continue
                outcome, alert = _settle(conn, name, entry, now)
            if outcome is not None:
                settled.append(name)
            if alert:
                alerts.append(alert)
        except Exception as exc:  # noqa: BLE001 - one entry never stops the rest
            logger.warning("shared_block_agent_capture_failed name=%s err=%s", name, exc)
    _send_alerts(config, alerts)
    return settled
