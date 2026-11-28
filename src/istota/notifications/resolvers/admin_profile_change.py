"""An administrator changed the addresses or numbers that identify a user.

Raised by the admin user editor (the PATCH and the login-email PUT) when a
contact field of somebody other than the acting admin changes: the email
addresses mail is routed by, the SMS number, the WhatsApp number or the login
email. Repointing someone's number is exactly what that someone needs to hear,
and the inbox is the one place the old identity cannot intercept.

Also raised by `istota apply` for any profile field it changes on an existing
profile (:func:`write_from_plan`), with its own title and a ``plan:`` key.

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


PLAN_TITLE = "Provisioning changed your settings"
_PLAN_NAMED = 5

# Every field a settings plan can change on a profile (`settings_plan.schema.
# PROFILE_FIELDS`, plus the WhatsApp enrollment), in the order the body names them.
PLAN_FIELDS = {
    "display_name": "display name",
    "timezone": "timezone",
    "timezone_follow_location": "travel timezone setting",
    "email_addresses": "email addresses",
    "sms_phone_number": "SMS number",
    "enrollment": "WhatsApp number",
    "trusted_email_senders": "trusted email senders",
    "quiet_email_senders": "quiet email senders",
    "outbound_approval": "outbound mail approval",
    "correspondent_scopes": "correspondent access",
    "disabled_skills": "disabled skills",
    "disabled_modules": "disabled modules",
    "log_channel": "log room",
    "alerts_channel": "alerts room",
    "default_destination": "default destination",
    "default_room": "default room",
    "routing": "delivery routing",
    "relay_delivery": "relay delivery",
    "calendar_delivery": "calendar delivery",
    "external_turn_display": "external message display",
    "briefing_email_html": "HTML briefing email",
    "quick_notes_folder": "quick notes folder",
    "show_nav_unread_badges": "navigation unread counts",
    "chat_read_on_view": "chat read on view",
    "push_chat_alerts": "chat push alerts",
    "push_preview": "push previews",
    "push_inbox_categories": "push inbox categories",
    "max_foreground_workers": "foreground worker limit",
    "max_background_workers": "background worker limit",
}


def plan_body_for(fields: list[str], *, managed: bool = False) -> str:
    names = [PLAN_FIELDS[name] for name in PLAN_FIELDS if name in set(fields)]
    if len(names) > _PLAN_NAMED:
        rest = len(names) - (_PLAN_NAMED - 1)
        names = names[:_PLAN_NAMED - 1] + [f"{rest} other settings"]
    changed = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]
    if managed:
        return (
            f"Your administrator's provisioning changed your {changed}, and now "
            "sets them, so Settings no longer changes them. Ask your administrator "
            "if you want them different."
        )
    return (
        f"Your administrator's provisioning changed your {changed}. If you had "
        "changed these in Settings, set them again and ask your administrator to "
        "refresh the deployment's settings plan, or the next run changes them back."
    )


def write_from_plan(
    conn: "sqlite3.Connection", user_id: str, fields: list[str], *, managed: bool = False,
):
    """An `istota apply` changed an existing profile (ISSUE-1003).

    The admin's own copy of a plan is often a snapshot older than the user's
    last edit in Settings, so the apply is a revert the user would otherwise
    never hear about. ``managed`` is a `profile_mode = "managed"` apply, which
    also locks the fields. Written in the apply's transaction and handed to the
    daemon.
    """
    from istota.notifications.store import write_notification

    changed = [name for name in PLAN_FIELDS if name in set(fields)]
    if not changed:
        return None
    return write_notification(
        conn, user_id, source=SOURCE, dedup_key="plan:" + "+".join(changed),
        title=PLAN_TITLE, body=plan_body_for(changed, managed=managed), severity="info",
        params={"fields": changed},
    )


def write(conn: "sqlite3.Connection", user_id: str, *, admin_name: str, fields: list[str]):
    """Write the row on the caller's connection; the web routes hand it to the daemon."""
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
