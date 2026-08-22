"""A bloodwork panel left in `draft` after an OCR extraction nobody confirmed.

The third silent gap, and the one the store's scoping rule was written for. A
panel uploaded to `/health/bloodwork/upload` is inserted with `draft=1` and stays
there until the extracted values are reviewed and posted back with
`confirm: true`. A draft panel is excluded from the health dashboard *and* from
the biomarker trends, so a user who closes the tab mid-review has lab results in
the system that nothing will ever show them again unless they think to pass
`include_drafts=1` by hand.

**The row goes in the framework DB, keyed by the session user; the panel id
comes from the per-user health module DB.** Those are two different databases
and every user has a panel `12` in theirs. So the producer writes against
`ctx.framework_db_path`, never `ctx.db_path`, and both close paths go through
`resolve_by_object`, which takes `user_id` first-class. `idx_notifications_object`
leads with `user_id` for the same reason. Getting this wrong would mean
confirming your panel 12 closed everyone else's.

The resolver has to open the *reading* user's health DB to answer whether the
panel is still a draft, which is why resolvers receive a `Config` at all. It
resolves the module for `row.user_id` — the row's owner — and never for a value
that came from the request.

There is no one-click confirm: `POST /health/panels/{id}/biomarkers` needs the
whole reviewed biomarker list in its body, and `PUT /health/panels/{id}` is a
PUT, which the action vocabulary does not carry. So the action is a link to the
bloodwork page, where the review UI lives.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from ..config import Config
    from ..notification_sources import NotificationRow, NotificationView
    from ..notification_store import RaiseResult

logger = logging.getLogger(__name__)

SOURCE = "health_panel"
OBJECT_TYPE = "health_panel"

# Nothing is broken and nothing is at risk — there is just unfinished work
# holding data out of the trends.
SEVERITY = "info"

# The bloodwork list, from which every draft panel is one click away. The panel
# page itself is `/health/bloodwork/panel?id=N`, which the URL allowlist refuses
# (no query strings, deliberately), so the list is where the link goes.
REVIEW_HREF = "/health/bloodwork"


def dedup_key(panel_id: int | str) -> str:
    """``panel:{id}``, verbatim — see the note on the confirmation source."""
    return f"panel:{panel_id}"


def title_for(drawn_at: str | None, lab_name: str | None) -> str:
    """The one-line label. One spelling for the producer and the resolver."""
    from ..confirmations import flatten

    lab = flatten(lab_name or "")
    when = flatten(drawn_at or "")[:10]
    who = f" from {lab}" if lab else ""
    dated = f" ({when})" if when else ""
    return f"Lab results{who}{dated} are waiting to be reviewed"


def body_for(drawn_at: str | None, lab_name: str | None) -> str:
    from ..confirmations import flatten

    lab = flatten(lab_name or "")
    when = flatten(drawn_at or "")[:10]
    bits = []
    if lab:
        bits.append(f"from {lab}")
    if when:
        bits.append(f"drawn {when}")
    which = " ".join(bits)
    lead = f"An uploaded lab report{' ' + which if which else ''} was read but"
    return (
        f"{lead} never confirmed, so it is still a draft. Draft panels are left "
        f"out of the health dashboard and out of the biomarker trends until the "
        f"extracted values are checked."
    )


def write(
    conn: "sqlite3.Connection",
    user_id: str,
    *,
    panel_id: int,
    drawn_at: str | None = None,
    lab_name: str | None = None,
) -> "RaiseResult | None":
    """Write the row on the caller's connection to the **framework** DB.

    The caller is a web handler that holds a health-module connection, not a
    framework one, so in practice it goes through :func:`raise_for_panel`. This
    exists for a producer that does already hold the framework connection.
    """
    from ..notification_store import write_notification

    return write_notification(
        conn, user_id,
        source=SOURCE,
        dedup_key=dedup_key(panel_id),
        title=title_for(drawn_at, lab_name),
        body=body_for(drawn_at, lab_name),
        severity=SEVERITY,
        actionable=True,
        object_type=OBJECT_TYPE,
        object_id=str(panel_id),
        params={"drawn_at": drawn_at or "", "lab_name": lab_name or ""},
        purpose="alert",
    )


def raise_for_panel(
    config: "Config",
    user_id: str,
    *,
    panel_id: int,
    drawn_at: str | None = None,
    lab_name: str | None = None,
) -> int | None:
    """Write **and** deliver, on a connection of the store's own.

    `raise_notification` rather than the buffered pair, and the reason its
    docstring demands be named: the caller is the panel-upload handler, whose
    only open connection is to the *health module* DB. It holds no write lock on
    the framework DB — different file, different lock — so there is nothing here
    for a second connection to wait thirty seconds on.
    """
    from ..notification_store import raise_notification

    return raise_notification(
        config, user_id,
        source=SOURCE,
        dedup_key=dedup_key(panel_id),
        title=title_for(drawn_at, lab_name),
        body=body_for(drawn_at, lab_name),
        severity=SEVERITY,
        actionable=True,
        object_type=OBJECT_TYPE,
        object_id=str(panel_id),
        params={"drawn_at": drawn_at or "", "lab_name": lab_name or ""},
        purpose="alert",
    )


def resolve_for_panel(
    conn: "sqlite3.Connection", user_id: str, panel_id: int, *, by: str,
) -> int:
    """Close the row for a panel that has just been confirmed.

    `conn` is the **framework** DB, and `user_id` is the session user. Both are
    load-bearing: see the module docstring.
    """
    from ..notification_store import resolve_by_object

    return resolve_by_object(
        conn, user_id, SOURCE, OBJECT_TYPE, str(panel_id), by=by,
    )


def close_for_panel(config: "Config", user_id: str, panel_id: int, *, by: str) -> None:
    """`resolve_for_panel` for a caller holding only its health-module connection.

    Never raises: closing an inbox row must not be able to fail a confirmation.
    """
    try:
        from .. import db

        if getattr(config, "db_path", None) is None:
            return
        with db.get_db(config.db_path) as conn:
            resolve_for_panel(conn, user_id, panel_id, by=by)
    except Exception:
        logger.warning(
            "could not close the health panel notification for %r panel %s",
            user_id, panel_id, exc_info=True,
        )


def _panel_id(row: "NotificationRow") -> int | None:
    """The row's ``object_id`` as an integer, or None. See the confirmation source."""
    try:
        return int(str(row.object_id).strip())
    except (TypeError, ValueError):
        logger.warning(
            "notification %s names a non-numeric panel id %r", row.id, row.object_id,
        )
        return None


class HealthPanelResolver:
    source = SOURCE
    auto_resolve_on_seen = False

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        from ..health import db as health_db
        from ..health._loader import UserNotFoundError, resolve_for_user
        from ..notification_sources import NotificationAction, NotificationView

        panel_id = _panel_id(row)
        if panel_id is None:
            return None

        try:
            ctx = resolve_for_user(row.user_id, config)
        except UserNotFoundError:
            # The module was turned off, or the mount went away. There is no
            # panel to review and no page to review it on.
            return None
        # Any *other* failure is deliberately not caught: `list_open` degrades
        # the row to its stored text and leaves it open. Returning None here
        # would close a real draft panel on a transient fault, and nothing would
        # ever raise it again — the producer fires once, at upload.

        if not ctx.db_path.exists():
            return None

        with health_db.connect(ctx.db_path) as health_conn:
            panel = health_db.get_panel(health_conn, panel_id)
        if panel is None:
            return None
        if not panel.draft:
            # Confirmed, here or on another surface.
            return None

        return NotificationView(
            title=title_for(panel.drawn_at, panel.lab_name),
            body=body_for(panel.drawn_at, panel.lab_name),
            severity=row.severity,
            actions=(
                NotificationAction(
                    id="review", label="Review", kind="primary",
                    method="LINK", href=REVIEW_HREF,
                ),
            ),
        )


RESOLVER = HealthPanelResolver()
