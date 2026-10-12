"""An administrator's configuration change was applied, or failed.

Raised by `istota config-request finish` (and by `take` when it fails a request
the host never recorded an outcome for), in the one-shot container the host
applier runs, which has the database but no delivery surfaces: the row is
written and handed to the daemon with `store.hand_to_daemon`.

The recipients are admins, not a subject user: every admin before or after the
change, except the one who made it, so an admin removed by the change hears
about it.

Fire-and-forget: nothing closes it but being seen. Three rules, as for
`admin_profile_change`:

- **The body names sections, credential names and the acting admin, never a
  value.** Sections come from the dataclass tree and an inline credential is
  shown by its section (`edits.loggable_secret_name`), so no segment somebody
  typed reaches a push.
- **The key is bounded**: one row per request.
- **The resolver never returns None.** None means "the object is gone" and
  would mark an unread notice stale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from istota.config import Config
    from istota.notifications.sources import NotificationRow, NotificationView
    from istota.notifications.store import RaiseResult

SOURCE = "admin_config_change"
TITLES = {
    "applied": "The configuration was changed",
    "failed": "A configuration change failed",
}
MAX_BODY_CHARS = 1000
_MAX_NAME_CHARS = 80
_MAX_LISTED = 12

STATUS_NOTE = "This notice has no in-app action. It clears itself once you have seen it."


def dedup_key(request_id: int) -> str:
    return f"request:{int(request_id)}"


def _listed(names: list[str]) -> str:
    shown = names[:_MAX_LISTED]
    more = len(names) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def body_for(
    admin_name: str, status: str, *, sections: list[str], credentials: list[str],
    admins_changed: bool,
) -> str:
    from istota.notifications.resolvers.task_alert import flatten

    who = flatten(admin_name)[:_MAX_NAME_CHARS] or "An administrator"
    parts = []
    if sections:
        parts.append(f"settings in {_listed(sections)}")
    if credentials:
        parts.append(f"the credentials {_listed(credentials)}")
    if admins_changed:
        parts.append("the admin list")
    what = "; ".join(parts) or "nothing that needed writing"
    if status == "applied":
        return f"{who} changed {what}. Istota restarted to apply it."
    return (
        f"{who} asked to change {what}, and the host could not finish it. "
        "The request's history on the Configuration page has the host's output."
    )


def write(
    conn: "sqlite3.Connection", user_id: str, *, request_id: int, admin_name: str,
    status: str, sections: list[str], credentials: list[str], admins_changed: bool,
) -> "RaiseResult | None":
    """Write one admin's row on the caller's connection."""
    from istota.notifications.store import write_notification

    if status not in TITLES:
        return None
    return write_notification(
        conn, user_id, source=SOURCE, dedup_key=dedup_key(request_id),
        title=TITLES[status],
        body=body_for(
            admin_name, status, sections=sections, credentials=credentials,
            admins_changed=admins_changed,
        ),
        severity="info" if status == "applied" else "warning",
        params={"request": int(request_id), "status": status},
    )


class AdminConfigChangeResolver:
    source = SOURCE
    auto_resolve_on_seen = True

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView":
        """Always a view from the stored text: there is no object to watch."""
        from istota.notifications.resolvers.task_alert import flatten, flatten_body
        from istota.notifications.sources import NotificationView

        return NotificationView(
            title=flatten(row.title) or TITLES["applied"],
            body=flatten_body(row.body)[:MAX_BODY_CHARS],
            severity=row.severity,
            actions=(),
            link=None,
            status_note=STATUS_NOTE,
        )


RESOLVER = AdminConfigChangeResolver()
