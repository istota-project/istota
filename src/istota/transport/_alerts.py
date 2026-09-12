"""Pushing one alert everywhere except the surface it is about.

A push surface that cannot reach somebody has to say so, and the one route it
must not say it on is its own: reporting an SMS failure over SMS, or a WhatsApp
identity mismatch over WhatsApp, is a notice delivered to exactly the person
who cannot receive it — and on a paid surface it is a second billed message
chasing a first that failed.

The shape was written once in `sms/outbound.py` and again in
`whatsapp/webhook.py`, identical but for the surface name and the reference
prefix, and the WhatsApp outbound ledger owes a third. The descriptor
construction is the part worth having in one place: it is `resolve_destinations`
filtered, then rejoined into the comma-list grammar `send_notification` parses,
and a surface that spelled that join differently would silently route to the
wrong place rather than fail.

**The row is written by the caller, inside its own transaction; only the push
is here.** `.claude/rules/notifications.md` is the reason — a producer holding
a write transaction that opens a second connection to deliver waits out the
full busy timeout and then raises into a never-raises contract. So a caller
buffers the `RaiseResult` and hands it here after its `with` block has closed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config
    from ..notification_store import RaiseResult

logger = logging.getLogger(__name__)


def push_off_surface(
    config: "Config",
    raised: "RaiseResult | None",
    *,
    exclude_surface: str,
    reference_prefix: str,
) -> bool:
    """Deliver one buffered alert to every route but ``exclude_surface``.

    Returns whether it was sent. `False` covers three different nothings, and
    none of them is an error: no row was raised, the row was a bump rather than
    an insert or a reopen (so this call is not the one that should send), or
    the user's alert routing named no destination outside the failing surface.

    Never raises. Every caller is on a path whose own contract is that it does
    not — an inbound webhook that has already committed, or a delivery failure
    handler — so an alert that cannot be pushed is a warning and the durable
    row stays open for the notification panel to render.
    """
    from .. import db, notifications
    from ..notification_store import mark_delivered

    if raised is None or not raised.deliver or not raised.user_id:
        return False
    destinations = [
        dest
        for dest in notifications.resolve_destinations(config, raised.user_id, "alert")
        if dest.surface != exclude_surface
    ]
    descriptor = ",".join(
        dest.surface if dest.channel is None else f"{dest.surface}:{dest.channel}"
        for dest in destinations
    )
    if not descriptor:
        return False
    try:
        sent = notifications.send_notification(
            config, raised.user_id, raised.text, surface=descriptor,
            title=raised.title,
            reference_id=f"{reference_prefix}:{raised.notification_id}",
        )
    except Exception:
        logger.warning("%s.alert.not_delivered", exclude_surface, exc_info=True)
        return False
    if sent:
        try:
            with db.get_db(config.db_path) as conn:
                mark_delivered(conn, [raised.notification_id])
        except Exception:
            # The push happened; only the record of it did not. Losing that
            # costs one redelivery at worst, and raising here would turn a
            # delivered alert into a caller-visible failure.
            logger.warning("%s.alert.not_recorded", exclude_surface, exc_info=True)
    return bool(sent)


__all__ = ["push_off_surface"]
