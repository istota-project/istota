"""An administrator changed the addresses or numbers that identify a user.

Raised by the admin user editor (the PATCH and the login-email PUT) when a
contact field of somebody other than the acting admin changes: the email
addresses mail is routed by, the SMS number, the WhatsApp number or the login
email. Repointing someone's number is exactly what that someone needs to hear,
and the inbox is the one place the old identity cannot intercept.

Fire-and-forget: nothing closes it but being seen, and
`store.sweep_expired_alerts` is the backstop. Three rules:

- **The body names fields and the acting admin, never a value.** The push
  follows the user's alert routing, which can reach a shared room.
- **The key is bounded.** It is built from the changed field names, each from
  the fixed :data:`FIELDS` table, so at most one row per combination; a second
  change of the same fields bumps the open row rather than pushing again.
- **The resolver never returns None.** None means "the object is gone" and
  would mark an unread notice stale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from istota.config import Config
    from istota.notifications.sources import NotificationRow, NotificationView

SOURCE = "admin_profile_change"
TITLE = "An administrator changed your contact details"
MAX_BODY_CHARS = 600
_MAX_NAME_CHARS = 80

# The contact fields, in the order the body names them, with the words used.
FIELDS = {
    "login_email": "login email",
    "email_addresses": "email addresses",
    "sms_phone_number": "SMS number",
    "whatsapp_number": "WhatsApp number",
}

STATUS_NOTE = "This notice has no in-app action. It clears itself once you have seen it."


def contact_fields(fields: object) -> list[str]:
    """The contact fields among ``fields``, in :data:`FIELDS` order."""
    present = set(fields or ())
    return [name for name in FIELDS if name in present]


def dedup_key(fields: list[str]) -> str:
    return "fields:" + "+".join(contact_fields(fields))


def body_for(admin_name: str, fields: list[str]) -> str:
    from istota.notifications.resolvers.task_alert import flatten

    names = [FIELDS[name] for name in contact_fields(fields)]
    if len(names) > 1:
        changed = ", ".join(names[:-1]) + " and " + names[-1]
    else:
        changed = names[0] if names else "contact details"
    who = flatten(admin_name)[:_MAX_NAME_CHARS] or "An administrator"
    return (
        f"{who} changed your {changed}. If you did not expect this, "
        "ask them about it."
    )


def write(conn: "sqlite3.Connection", user_id: str, *, admin_name: str, fields: list[str]):
    """Write the row on the caller's connection; deliver with `deliver_pending` after commit."""
    from istota.notifications.store import write_notification

    changed = contact_fields(fields)
    if not changed:
        return None
    return write_notification(
        conn, user_id, source=SOURCE, dedup_key=dedup_key(changed),
        title=TITLE, body=body_for(admin_name, changed), severity="info",
        params={"fields": changed},
    )


class AdminProfileChangeResolver:
    source = SOURCE
    auto_resolve_on_seen = True

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView":
        """Always a view from the stored text: there is no object to watch."""
        from istota.notifications.resolvers.task_alert import flatten, flatten_body
        from istota.notifications.sources import NotificationView

        return NotificationView(
            title=flatten(row.title) or TITLE,
            body=flatten_body(row.body)[:MAX_BODY_CHARS],
            severity=row.severity,
            actions=(),
            link=None,
            status_note=STATUS_NOTE,
        )


RESOLVER = AdminProfileChangeResolver()
