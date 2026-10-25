"""Folders offered to Istota over Open Cloud Mesh, waiting for the user.

One row per user (`DEDUP_KEY`), written by `sharing.ocm.receive_share` for
each new or repeated offer, so a flood of offers is one row and one push.
**The title and body are fixed text.** The folder's name, its owner and the
sending host are all chosen by whoever sent the offer, and an alert push may
be routed into a shared room, so none of them reaches a push (#638). They
appear in the bell's detail view, read live from `shared_sources`, each
flattened to one line of text.

Object-backed: open while the user has a ``pending`` OCM row or an active one
flagged ``reoffer_pending``; the resolver answers None once neither is left,
which closes the row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from istota.config import Config
    from istota.notifications.sources import NotificationRow, NotificationView
    from istota.notifications.store import RaiseResult

SOURCE = "shared_source"
DEDUP_KEY = "pending-offers"
TITLE = "A folder was shared with you"
BODY = "Review it in Istota."
LINK = "/settings/connections"
MAX_LISTED = 10
_MAX_FIELD_CHARS = 120


def write(conn: "sqlite3.Connection", user_id: str) -> "RaiseResult | None":
    """Open or bump the user's row, on the caller's connection and transaction."""
    from istota.notifications.store import write_notification

    return write_notification(
        conn, user_id, source=SOURCE, dedup_key=DEDUP_KEY, title=TITLE, body=BODY,
        severity="info", object_type="shared_offers", object_id=user_id, link=LINK,
    )


def waiting(conn: "sqlite3.Connection", user_id: str) -> list:
    """The user's offers waiting on them: pending ones, and re-offers of active shares."""
    from istota.sharing import sources

    rows = sources.sources_for_user(conn, user_id, kinds=("ocm_share",),
                                    states=("pending", "active", "error"))
    return [row for row in rows
            if row.state == "pending" or row.extras.get("reoffer_pending") is True]


def _line(source) -> str:
    from istota.notifications.resolvers.task_alert import flatten

    name = flatten(str(source.extras.get("name") or ""))[:_MAX_FIELD_CHARS] or "A folder"
    owner = flatten(str(source.extras.get("owner") or ""))[:_MAX_FIELD_CHARS]
    host = flatten(source.remote_host)[:_MAX_FIELD_CHARS]
    again = " (offered again)" if source.state != "pending" else ""
    who = f"{owner}, " if owner else ""
    return f"{name}{again}: from {who}{host}"


class SharedSourceResolver:
    source = SOURCE
    auto_resolve_on_seen = False

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        from istota.notifications.sources import NotificationAction, NotificationView

        offers = waiting(conn, row.user_id)
        if not offers:
            return None
        lines = [_line(offer) for offer in offers[:MAX_LISTED]]
        if len(offers) > MAX_LISTED:
            lines.append(f"and {len(offers) - MAX_LISTED} more")
        return NotificationView(
            title=TITLE,
            body="\n".join(lines),
            severity="info",
            link=LINK,
            actions=(NotificationAction(
                id="review", label="Review", kind="primary", method="LINK", href=LINK,
            ),),
        )


RESOLVER = SharedSourceResolver()
