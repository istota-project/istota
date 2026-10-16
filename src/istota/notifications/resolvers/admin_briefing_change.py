"""An administrator paused, resumed or deleted a briefing somebody else owns.

Raised by the admin briefings routes (`briefings/admin_routes.py`) for the
owner of a personal briefing and for each current owner of a group briefing.
An All users briefing has no owner to tell, and the acting administrator is
never told about their own action; the routes decide both.

Fire-and-forget like `admin_profile_change`: nothing closes it but being seen.
The key is ``<kind>:<id>:<action>``, so a repeat of the same action on the same
briefing bumps the open row rather than pushing again. The resolver never
returns None, since there is no object left to watch after a delete.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from istota.config import Config
    from istota.notifications.sources import NotificationRow, NotificationView

SOURCE = "admin_briefing_change"
MAX_BODY_CHARS = 600
_MAX_NAME_CHARS = 80
_MAX_TITLE_CHARS = 120

# The action as the route names it, and the verb the owner reads.
ACTIONS = {"hold": "paused", "resume": "resumed", "delete": "deleted"}
KINDS = ("personal", "group")

_AFTER = {
    "hold": " It will not run until an administrator resumes it.",
    "resume": " It runs again on its schedule if it is turned on.",
    "delete": "",
}

STATUS_NOTE = "This notice has no in-app action. It clears itself once you have seen it."


def title_for(action: str) -> str:
    return f"An administrator {ACTIONS[action]} your briefing"


def dedup_key(kind: str, briefing_id: int, action: str) -> str:
    return f"{kind}:{int(briefing_id)}:{action}"


def body_for(admin_name: str, action: str, briefing_title: str, group_label: str | None = None) -> str:
    from istota.notifications.resolvers.task_alert import flatten

    who = flatten(admin_name)[:_MAX_NAME_CHARS] or "An administrator"
    what = flatten(briefing_title)[:_MAX_TITLE_CHARS] or "a briefing"
    where = ""
    if group_label:
        where = f" in {flatten(group_label)[:_MAX_NAME_CHARS]}"
    return f"{who} {ACTIONS[action]} the briefing “{what}”{where}.{_AFTER[action]}"


def write(
    conn: "sqlite3.Connection", user_id: str, *, admin_name: str, action: str,
    briefing_title: str, kind: str, briefing_id: int, group_label: str | None = None,
):
    """Write the row on the caller's connection; the web routes hand it to the daemon."""
    from istota.notifications.store import write_notification

    if action not in ACTIONS or kind not in KINDS:
        raise ValueError(f"unknown briefing change {kind}/{action}")
    return write_notification(
        conn, user_id, source=SOURCE, dedup_key=dedup_key(kind, briefing_id, action),
        title=title_for(action),
        body=body_for(admin_name, action, briefing_title, group_label),
        severity="info", params={"kind": kind, "action": action},
    )


class AdminBriefingChangeResolver:
    source = SOURCE
    auto_resolve_on_seen = True

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView":
        """Always a view from the stored text: there is no object to watch."""
        from istota.notifications.resolvers.task_alert import flatten, flatten_body
        from istota.notifications.sources import NotificationView

        return NotificationView(
            title=flatten(row.title) or "An administrator changed your briefing",
            body=flatten_body(row.body)[:MAX_BODY_CHARS],
            severity=row.severity,
            actions=(),
            link=None,
            status_note=STATUS_NOTE,
        )


RESOLVER = AdminBriefingChangeResolver()
