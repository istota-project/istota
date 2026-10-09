"""Invoice match actions shared by the CLI and web routes.

Money writers take the DB write reservation before the work lock. The work
stamp precedes the decision row; a failed row write leaves a stale review,
never a settled row claiming work that was not stamped.
"""

from istota.money import db, work
from istota.money.core.invoice_matching import OpenInvoice
from istota.money.core.invoicing import build_line_items, resolve_entity, resolve_bank_account


def open_invoice(config, number, entries):
    """Build a matchable invoice from a live work snapshot."""
    if not entries or any(e.paid_date is not None for e in entries):
        return None
    items = build_line_items(entries, config.services)
    if not items or len(items) != len(entries):
        return None
    entity = resolve_entity(
        config, entry=entries[0], client_config=config.clients.get(entries[0].client),
    )
    return OpenInvoice(
        number=number, client=entries[0].client,
        date=work.invoice_issue_date(entries),
        total=sum(item.amount for item in items),
        bank_account=resolve_bank_account(entity, config),
    )


def open_invoices(config, data_dir):
    invoices = []
    for number in work.get_invoice_numbers(data_dir):
        invoice = open_invoice(config, number, work.get_entries_for_invoice(data_dir, number))
        if invoice is not None:
            invoices.append(invoice)
    return invoices


def list_reviews(conn, config, data_dir, *, show_all=False):
    rows = db.list_payment_matches(conn, status=None if show_all else "review")
    live = {invoice.number: invoice for invoice in open_invoices(config, data_dir)}
    for row in rows:
        row["candidate_details"] = [
            {"invoice_number": number, "client": live[number].client,
             "total": live[number].total}
            for number in row["candidates"] if number in live
        ]
    return rows


def settle_payment_review(db_path, data_dir, config, ledger_txn_id, invoice_number):
    with db.get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = next((r for r in db.list_payment_matches(conn, status="review")
                    if r["ledger_txn_id"] == ledger_txn_id), None)
        if row is None:
            raise ValueError("Payment review not found or already decided")
        if invoice_number not in row["candidates"]:
            raise ValueError("Invoice is not a candidate for this credit")

        def validate(entries):
            live = open_invoice(config, invoice_number, entries)
            return live is not None and bool(row["account"]) and live.bank_account == row["account"]

        count = work.record_invoice_payment(
            data_dir, invoice_number, row["txn_date"], validate=validate,
        )
        if not count:
            raise ValueError("Invoice is no longer wholly unpaid and eligible; review remains open")
        if not db.settle_review(conn, ledger_txn_id, invoice_number):
            raise ValueError("Could not close payment review after stamping the invoice")
    return {"status": "ok", "ledger_txn_id": ledger_txn_id,
            "invoice_number": invoice_number, "entries_paid": count}


def dismiss_payment_review(db_path, ledger_txn_id):
    with db.get_db(db_path) as conn:
        if not db.dismiss_review(conn, ledger_txn_id):
            raise ValueError("Payment review not found or already decided")
    return {"status": "ok", "ledger_txn_id": ledger_txn_id}
