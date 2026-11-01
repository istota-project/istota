"""A scheduled task tried something its owner has not approved.

One row per subject, however many hosts it was refused, written by the CRON.md sync
and at task end (`rooms.egress.raise_refused_bell`), both in the daemon. **The title
and body are fixed text.** A refused hostname can carry data and a job name is
model-writable, and an alert push may be routed into a shared room (#638), so
neither reaches a push, a stored title or the key: the key holds a digest of the
subject. The bell names the subject, flattened, to the signed-in user, and shows no
hostname. The page is where a host is read, and where it is decided.

There is no Allow here. A hostile page chooses the hostname, so a click on the bell
would be the weakest point in the design.

Object-backed: open while the subject is live and has a refusal not dismissed. The
resolver answers None past that, which closes the row, and a later refusal of
something new reopens it.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from istota.config import Config
    from istota.notifications.sources import NotificationRow, NotificationView
    from istota.notifications.store import RaiseResult

SOURCE = "egress_refused"
OBJECT_TYPE = "egress_subject"
TITLE = "A scheduled job or assignment needs network access"
BODY = "Review it in Istota"
PAGE = "/settings/jobs"
_MAX_LABEL_CHARS = 120


def dedup_key(subject: str) -> str:
    digest = hashlib.sha256(subject.encode("utf-8", "surrogatepass")).hexdigest()
    return f"subject:{digest[:32]}"


def link_for(conn: "sqlite3.Connection", user_id: str, subject: str) -> str:
    """Where a subject is decided. It carries only an integer, so no model-written name
    reaches a link. Both are server redirects: a link cannot hold the page's query."""
    kind, _, rest = subject.partition(":")
    if kind == "assignment" and rest.isascii() and rest.isdigit():
        return f"/settings/assignments/open/{int(rest)}"
    if kind == "cron":
        row = conn.execute(
            "SELECT id FROM scheduled_jobs WHERE user_id = ? AND name = ?", (user_id, rest),
        ).fetchone()
        if row is not None:
            return f"/settings/jobs/open/{int(row[0])}"
    return PAGE


def write(conn: "sqlite3.Connection", user_id: str, subject: str) -> "RaiseResult | None":
    """Open or bump the subject's row, on the caller's connection and transaction."""
    from istota.notifications.store import write_notification

    return write_notification(
        conn, user_id, source=SOURCE, dedup_key=dedup_key(subject), title=TITLE, body=BODY,
        severity="warning", actionable=True, object_type=OBJECT_TYPE, object_id=subject,
        link=link_for(conn, user_id, subject),
    )


def resolve_if_settled(conn: "sqlite3.Connection", user_id: str, subject: str, *, by: str) -> None:
    """Close the row now, on the caller's transaction, once nothing is left to ask."""
    from istota.notifications.store import resolve_by_object
    from istota.rooms import egress

    if not egress.has_open_refusal(conn, user_id, subject):
        resolve_by_object(conn, user_id, SOURCE, OBJECT_TYPE, subject, by=by)


class EgressRefusedResolver:
    source = SOURCE
    auto_resolve_on_seen = False

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        from istota.notifications.resolvers.task_alert import flatten
        from istota.notifications.sources import NotificationAction, NotificationView
        from istota.rooms import egress

        subject = row.object_id
        if not subject or not egress.subject_exists(conn, row.user_id, subject):
            return None
        if not egress.has_open_refusal(conn, row.user_id, subject):
            return None
        link = link_for(conn, row.user_id, subject)
        review = NotificationAction(
            id="review", label="Review", kind="primary", method="LINK", href=link,
        )
        return NotificationView(
            title=TITLE,
            body=flatten(egress.subject_label(subject))[:_MAX_LABEL_CHARS],
            severity="warning",
            link=link,
            actions=(review,),
        )


RESOLVER = EgressRefusedResolver()
