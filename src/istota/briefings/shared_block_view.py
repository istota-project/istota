"""A shared block's definition and runtime status, as ``GET /shared-blocks`` lists it.

Moved out of ``briefings.routes`` unchanged, so the ``briefings_config`` skill
renders the same listing without importing FastAPI. The routes import it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from istota.briefings.sources.kv import SHARED_BLOCK_NAMESPACE


def _shared_last_run_utc(value: str | None, tz_name: str) -> str | None:
    """The scheduler's naive last-run stamp, which is wall-clock time in the
    shared-block timezone, as ISO UTC: the clock ``run_answered.at`` is on."""
    if not value:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - the scheduler falls back the same way
        tz = ZoneInfo("UTC")
    try:
        stamped = datetime.fromisoformat(value)
    except ValueError:
        return value
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=tz)
    return stamped.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shared_block_status(conn, name: str, tz_name: str) -> dict:
    """Per-block runtime status: last generation, current shared_kv preview,
    and any Run now request with the daemon's last answer to one."""
    from istota import db as fdb
    from istota.briefings import shared_block_runs

    last_run_at = _shared_last_run_utc(
        fdb.get_briefing_shared_block_last_run(conn, name), tz_name,
    )
    val = fdb.shared_kv_get(conn, SHARED_BLOCK_NAMESPACE, name)
    preview = None
    updated_at = None
    stored_trusted = None
    if val:
        updated_at = val.get("updated_at")
        try:
            import json as _json
            parsed = _json.loads(val["value"])
            if isinstance(parsed, dict):
                stored_trusted = bool(parsed.get("trusted", False))
                text = parsed.get("text")
                if isinstance(text, str):
                    preview = text[:400]
        except (ValueError, TypeError):
            preview = (val.get("value") or "")[:400]
    runs = shared_block_runs.run_state(conn, name)
    return {
        "run_pending": runs["pending"],
        "run_answered": runs["answered"],
        "agent": runs["agent"],
        "last_run_at": last_run_at,
        "value_updated_at": updated_at,
        "value_preview": preview,
        "stored_trusted": stored_trusted,
        "has_content": val is not None,
    }


def _map_shared_block(row, status: dict) -> dict:
    return {
        "name": row.name,
        "cron": row.cron,
        "title": row.title,
        "directive": row.directive or "",
        "render_mode": row.render_mode,
        "enabled": row.enabled,
        "trusted": row.trusted,
        "sources": row.sources,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "status": status,
    }
