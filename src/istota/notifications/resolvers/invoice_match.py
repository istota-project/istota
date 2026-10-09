"""Invoice payment decisions, backed by the owner's money database."""

from __future__ import annotations

import logging
import re

from . import _common

logger = logging.getLogger(__name__)
SOURCE = "invoice_match"
OBJECT_TYPE = "invoice_payment_match"
REVIEW_HREF = "/money/business/invoices"
PUSH_BODY = "A bank credit needs an invoice decision. Open the notification bell to review it."
_LEDGER_ID = re.compile(r"[a-f0-9]{32}\Z")


def write(conn, user_id, match, *, client=""):
    from istota.confirmations import flatten
    from istota.notifications.resolvers.task_alert import flatten_body
    from istota.notifications.store import write_notification

    review = match["status"] == "review"
    amount = f"${match['amount']:,.2f}"
    if review:
        title = f"Payment needs an invoice: {amount} on {match['txn_date']}"
        body = f"Credit from {match['payee']}. {len(match['candidates'])} candidate invoices need review."
    else:
        number = match["invoice_number"]
        title = f"{number} marked paid ({client}, {amount})"
        body = (f"Credit on {match['txn_date']} from {match['payee']}. "
                f"If this is wrong, use invoice unpaid {number}.")
    result = write_notification(
        conn, user_id,
        **_common.row_kwargs(
            source=SOURCE,
            dedup_key=f"{'review' if review else 'settled'}:{match['ledger_txn_id']}",
            title=flatten(title), body=flatten_body(body),
            severity="warning" if review else "success", actionable=review,
            object_type=OBJECT_TYPE, object_id=match["ledger_txn_id"],
        ),
    )
    return _common.pushing_only(result, PUSH_BODY)


def publish(config, db_path, user_id, match, *, client="", deliver_reviews=False):
    """Commit the bell row before attempting delivery, outside the money write."""
    if db_path is None or not user_id:
        logger.warning("invoice match: no framework database or user; bell write skipped")
        return
    try:
        from istota import db
        from istota.notifications.store import deliver_pending

        with db.get_db(db_path) as conn:
            pending = write(conn, user_id, match, client=client)
        if deliver_reviews and match["status"] == "review":
            if config is None:
                logger.warning("invoice match: no framework config; delivery skipped")
            else:
                deliver_pending(config, [pending])
    except Exception:
        logger.warning("could not publish invoice match notification", exc_info=True)


def close_for_match(db_path, user_id, ledger_txn_id, *, by="web"):
    try:
        from istota import db

        with db.get_db(db_path, busy_timeout_ms=2000) as conn:
            _common.resolve_for(conn, user_id, SOURCE, OBJECT_TYPE, ledger_txn_id, by=by)
    except Exception:
        logger.warning("could not close invoice match notification", exc_info=True)


class InvoiceMatchResolver:
    source = SOURCE
    auto_resolve_on_seen = True
    kept_until_dismissed = ("review:",)

    def resolve(self, config, conn, row):
        from istota.confirmations import flatten
        from istota.notifications.resolvers.task_alert import flatten_body
        from istota.money import db, config_store, resolve_for_user, UserNotFoundError
        from istota.money.invoice_review import open_invoices
        from istota.notifications.sources import NotificationAction, NotificationView

        ident = row.object_id
        if not isinstance(ident, str) or not _LEDGER_ID.fullmatch(ident):
            return None
        review = row.dedup_key == f"review:{ident}"
        if not review and row.dedup_key != f"settled:{ident}":
            return None
        # The loader initialises missing databases; a read must not erase a
        # live notice merely because the module's disk is unavailable.
        path = config.module_db_path(row.user_id, "money")
        if not path.exists():
            raise FileNotFoundError(path)
        try:
            ctx = resolve_for_user(row.user_id, config, conn=conn)
        except UserNotFoundError:
            return None
        with db.get_db(ctx.db_path) as money_conn:
            match = next((m for m in db.list_payment_matches(money_conn)
                          if m["ledger_txn_id"] == ident), None)
        if match is None or match["status"] != ("review" if review else "settled"):
            return None
        if not review:
            return NotificationView(title=flatten(row.title), body=flatten_body(row.body),
                                    severity=row.severity)

        invoicing = config_store.load_invoicing(ctx.db_path)
        live = {i.number: i for i in open_invoices(invoicing, ctx.data_dir)}
        candidates = [live[n] for n in match["candidates"] if n in live]
        if not candidates:
            return None
        actions = [NotificationAction(
            id=f"settle-{i.number}", label=flatten(f"Settle {i.number} ({i.client})"),
            kind="primary", method="POST", endpoint=f"/money/invoices/review/{ident}/settle",
            body={"invoice_number": i.number},
        ) for i in candidates[:3]]
        if len(candidates) > 3:
            actions.append(NotificationAction(id="open", label="Open", kind="default",
                                              method="LINK", href=REVIEW_HREF))
        actions.append(NotificationAction(
            id="dismiss", label="Not an invoice payment", kind="default", method="POST",
            endpoint=f"/money/invoices/review/{ident}/dismiss",
        ))
        body = (f"Credit from {match['payee']}. {len(candidates)} candidate invoices: "
                + "; ".join(f"{i.number} ({i.client}, ${i.total:,.2f})" for i in candidates))
        return NotificationView(
            title=flatten(f"Payment needs an invoice: ${match['amount']:,.2f} on {match['txn_date']}"),
            body=flatten_body(body), severity=row.severity, actions=tuple(actions),
            status_note="This review stays open until decided or dismissed.",
        )


RESOLVER = InvoiceMatchResolver()
