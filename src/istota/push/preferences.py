"""Personal push filtering; ordinary inbox and room counts stay unfiltered."""

import json
from pathlib import Path

from istota.notifications import sources

ROOM_MODES = ("default", "alerts", "badge", "off")
_CATEGORY_LABELS = {
    "confirmation": "Approvals",
    "outbound_draft": "Draft replies",
    "cron_job": "Paused scheduled jobs",
    "connected_service": "Connected services",
    "health_panel": "Health reviews",
    "relay_question": "Questions from others",
    "job_failure": "Failed jobs",
    "message_relay": "Relayed messages",
    "admin_profile_change": "Account changes",
    "admin_config_change": "Configuration changes",
    "admin_briefing_change": "Briefing changes",
    "admin_cron_change": "Scheduled job changes",
    "task_alert": "Task alerts",
    "group_briefing": "Group briefings",
    "invoice_match": "Invoice matches",
    "assignment": "Assignments",
}


def notification_categories() -> list[dict[str, str]]:
    return [{"id": source, "label": _CATEGORY_LABELS.get(source, source.replace("_", " ").capitalize())}
            for source in sources.all_resolvers()]


def validate_inbox_categories(value: object) -> dict[str, bool]:
    if not isinstance(value, dict):
        raise ValueError("push_inbox_categories must be an object")
    registered = sources.all_resolvers()
    if any(source not in registered or type(enabled) is not bool for source, enabled in value.items()):
        raise ValueError("push_inbox_categories requires registered source IDs and booleans")
    return dict(value)


def stored_inbox_categories(value: object, *, strict: bool = False) -> dict[str, bool]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if not isinstance(value, dict):
        if strict:
            raise ValueError("invalid stored push_inbox_categories")
        return {}
    registered = sources.all_resolvers()
    known = {source: enabled for source, enabled in value.items() if source in registered}
    if strict and any(type(enabled) is not bool for enabled in known.values()):
        raise ValueError("invalid stored push_inbox_categories")
    return {source: enabled for source, enabled in known.items() if type(enabled) is bool}


def effective_room_mode(account_default: str, override: str | None) -> str:
    return override if override in ("alerts", "badge", "off") else account_default


def push_badge_count(conn, user_id: str, *, profile=None) -> int:
    from istota import db, user_profiles

    if profile is None:
        profile = user_profiles.get_profile(Path(), user_id, conn=conn)
    categories = stored_inbox_categories(getattr(profile, "push_inbox_categories", {}))
    disabled = [source for source, enabled in categories.items() if not enabled]
    sql = "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND state = 'open'"
    params = [user_id]
    if disabled:
        sql += " AND source NOT IN (" + ",".join("?" for _ in disabled) + ")"
        params.extend(disabled)
    inbox = int(conn.execute(sql, params).fetchone()[0])
    return inbox + db.total_unread_messages(
        conn, user_id, "web", push_default=getattr(profile, "push_chat_alerts", "alerts"),
    )
