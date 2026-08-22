"""A connected third-party service whose stored credential stopped working.

Today that is Garmin and only Garmin. A six-hourly sync hits an auth error,
`garmin.mark_token_error` wipes the OAuth blob so the settings card stops
claiming "Connected", and nothing at all is pushed — the "row, no push" half of
the inventory. Worse than merely silent: `health/jobs.py` renders the sync job
only for a user who *has* stored tokens, so the scheduler's idempotent sync pass
deletes the job row on its next tick and the failure stops recurring. The row
has to be written at the moment of the error, because there will not be a second
one.

**`object_id` is a service name, not an integer**, so it gets the explicit
segment check the spec asks for in place of the `int()` coercion the other
sources use: only a name in `SERVICES` below is ever rendered. Nothing here
interpolates it into a path — the action is a fixed link to the settings page —
but a source whose ids are free text is one wrong f-string away from being the
thing the runtime allowlist has to catch, and the allowlist is meant to be the
backstop rather than the check.

The close path is the credential coming back: `garmin.store_tokens` on a
successful (re)connect, and `garmin.clear_tokens` when the user disconnects
deliberately. The resolver is the backstop for both.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from ..config import Config
    from ..notification_sources import NotificationRow, NotificationView
    from ..notification_store import RaiseResult

logger = logging.getLogger(__name__)

SOURCE = "connected_service"

# `secret`, matching the schema comment: the object being watched is the stored
# credential, not the remote account.
OBJECT_TYPE = "secret"

# A service that has stopped syncing is losing data every hour it stays that
# way, and only the user can fix it.
SEVERITY = "warning"

# The settings page mounts the Garmin card (`web/src/routes/settings/+page.svelte`
# → `GarminCard`), so one link covers every service this source can name. It is
# a frontend route, not an API path; `NotificationItem` renders it as
# `{base}{href}`.
RECONNECT_HREF = "/settings"

# The allowlist that stands in for `int()` here. Keys are the `object_id` values
# this source may carry; the value is what the user calls the service.
SERVICES: dict[str, str] = {
    "garmin": "Garmin Connect",
}


def dedup_key(service: str) -> str:
    """``service:{name}``, verbatim — see the note on the confirmation source."""
    return f"service:{service}"


def label_for(service: str) -> str:
    return SERVICES.get(service, service or "a connected service")


def title_for(service: str) -> str:
    return f"{label_for(service)} needs to be reconnected"


def body_for(service: str, reason: str = "") -> str:
    """The stored body, which is also what the push says."""
    from ..confirmations import flatten

    label = label_for(service)
    detail = flatten(reason or "")[:200]
    lead = (
        f"The stored {label} credentials were rejected, so syncing has stopped "
        f"and no new data is coming in."
    )
    if detail:
        lead += f" ({detail})"
    return f"{lead} Reconnect under Settings → Connected services."


def write(
    conn: "sqlite3.Connection",
    user_id: str,
    *,
    service: str,
    reason: str = "",
) -> "RaiseResult | None":
    """Write the row on the caller's connection, inside its transaction."""
    from ..notification_store import write_notification

    return write_notification(
        conn, user_id,
        source=SOURCE,
        dedup_key=dedup_key(service),
        title=title_for(service),
        body=body_for(service, reason),
        severity=SEVERITY,
        actionable=True,
        object_type=OBJECT_TYPE,
        object_id=service,
        params={"service": service, "reason": reason},
        purpose="alert",
    )


def raise_for_service(
    config: "Config", user_id: str, service: str, reason: str = "",
) -> int | None:
    """Write **and** deliver, on a connection of the store's own.

    `raise_notification` rather than the buffered pair, and the reason is the
    one its docstring demands be named: the caller is `sync_garmin`, which holds
    no framework-DB transaction at any of its three call sites. It reaches the
    framework DB only through `secrets_store`, and every one of those helpers
    opens and closes a connection of its own around a single statement — there
    is no open write lock for a second connection to wait thirty seconds on.
    """
    from ..notification_store import raise_notification

    return raise_notification(
        config, user_id,
        source=SOURCE,
        dedup_key=dedup_key(service),
        title=title_for(service),
        body=body_for(service, reason),
        severity=SEVERITY,
        actionable=True,
        object_type=OBJECT_TYPE,
        object_id=service,
        params={"service": service, "reason": reason},
        purpose="alert",
    )


def resolve_for_service(
    conn: "sqlite3.Connection", user_id: str, service: str, *, by: str,
) -> int:
    """Close the row for a service whose credentials are working again."""
    from ..notification_store import resolve_by_object

    return resolve_by_object(
        conn, user_id, SOURCE, OBJECT_TYPE, service, by=by,
    )


def close_for_service(db_path: "Path", user_id: str, service: str, *, by: str) -> None:
    """`resolve_for_service` for a caller that holds no connection at all.

    The two close sites are `garmin.store_tokens` and `garmin.clear_tokens`,
    which reach the framework DB only through `secrets_store` — see
    :func:`raise_for_service` for why opening a connection there is safe. Never
    raises: closing an inbox row must not be able to fail a reconnect.
    """
    try:
        from .. import db

        with db.get_db(db_path) as conn:
            resolve_for_service(conn, user_id, service, by=by)
    except Exception:
        logger.warning(
            "could not close the %s notification for %r",
            service, user_id, exc_info=True,
        )


class ConnectedServiceResolver:
    source = SOURCE
    auto_resolve_on_seen = False

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        from ..notification_sources import NotificationAction, NotificationView

        service = str(row.object_id or "").strip()
        if service not in SERVICES:
            logger.warning(
                "notification %s names an unknown service %r", row.id, row.object_id,
            )
            return None

        if _is_connected(config, row.user_id, service):
            return None

        reason = ""
        if isinstance(row.params, dict):
            reason = str(row.params.get("reason") or "")

        return NotificationView(
            title=title_for(service),
            body=body_for(service, reason),
            severity=row.severity,
            actions=(
                NotificationAction(
                    id="reconnect", label="Reconnect", kind="primary",
                    method="LINK", href=RECONNECT_HREF,
                ),
            ),
        )


def _is_connected(config: "Config", user_id: str, service: str) -> bool:
    """Whether the stored credential is usable again.

    Reads the framework DB through the service's own status helper rather than
    the panel's connection: `secrets_store` opens its own, and it is the only
    thing that knows how to decrypt. It answers `{}` — and so this answers
    False — when `ISTOTA_SECRET_KEY` is out of scope, which is the safe
    direction: an unreadable store leaves the row open rather than closing a
    warning nobody has acted on.
    """
    db_path = getattr(config, "db_path", None)
    if db_path is None:
        return False
    if service == "garmin":
        from ..health import garmin

        return bool(garmin.get_status(db_path, user_id).get("connected"))
    return False


RESOLVER = ConnectedServiceResolver()
