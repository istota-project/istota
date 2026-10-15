"""Ordinary research tasks correlated with a group run's current claim.

The scheduler admits and executes these tasks through its existing workers.
This module neither executes models nor publishes editions.
"""

from istota import db
from istota.briefings import group_store
from istota.briefings.source_grants import validate_run_grants
from istota.browser.owner import BRIEFINGS_IDENTITY
from istota.groups.policy import GroupError
from istota.groups.service import _transaction


def _research_run(conn, config, run_id, token, now):
    run = group_store._owned_run(conn, run_id, token, group_store._timestamp(now))
    definition = group_store.get_definition(conn, run["definition_id"])
    if (definition is None or db.get_group(conn, definition["group_id"]) is None
            or definition["revision"] != run["snapshot"]["definition"]["revision"]):
        raise GroupError(409, "definition_changed", "Briefing changed during research")
    validate_run_grants(conn, config, run)
    if run["state"] != "researching":
        raise GroupError(409, "research_conflict", "Run is not awaiting research")
    return run


def create_research_task(conn, config, run_id, token, *, now=None):
    """Idempotently admit one ordinary task for this run attempt."""
    with _transaction(conn):
        run = _research_run(conn, config, run_id, token, now)
        correlation = f"{run_id}:{token}"
        existing = conn.execute(
            "SELECT id FROM tasks WHERE user_id = ? AND source_type = 'group_briefing_research' "
            "AND briefing_name = ? ORDER BY id LIMIT 1", (BRIEFINGS_IDENTITY, correlation),
        ).fetchone()
        if existing:
            return db.get_task(conn, existing["id"])
        research = run["snapshot"]["definition"]["generation"].get("research", {})
        if not research.get("enabled") or not research.get("prompt") or research.get("allowed_domains"):
            raise GroupError(422, "research_settings_refused", "Research settings are unavailable")
        # No final prompt or gathered sources enter this task, including group inputs.
        task_id = db.create_task(
            conn, user_id=BRIEFINGS_IDENTITY, source_type="group_briefing_research",
            prompt=research["prompt"], briefing_name=correlation,
            queue="background", skip_log_channel=True,
        )
        return db.get_task(conn, task_id)


def capture_research_result(conn, config, run_id, token, task_id, *, now=None):
    """Save completed task output under the same attempt that requested it."""
    _research_run(conn, config, run_id, token, now)
    task = db.get_task(conn, task_id)
    if (task is None or task.user_id != BRIEFINGS_IDENTITY
            or task.source_type != "group_briefing_research"
            or task.briefing_name != f"{run_id}:{token}"):
        raise GroupError(409, "research_task_mismatch", "Research task does not belong to this claim")
    if task.status != "completed" or not task.result or not task.result.strip():
        raise GroupError(409, "research_task_incomplete", "Research task has no completed result")
    return group_store.save_research(conn, run_id, token, {
        "task_id": task.id, "task_attempt": task.attempt_count + 1, "text": task.result,
    }, now=now, config=config)
