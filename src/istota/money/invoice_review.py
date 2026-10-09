"""Invoice match actions shared by the CLI and web routes.

Money writers take the DB write reservation before the work lock. The work
stamp precedes the decision row; a failed row write leaves a stale review,
never a settled row claiming work that was not stamped.
"""

import logging

from istota.money import db, work
from istota.money.core.invoice_matching import OpenInvoice
from istota.money.core.invoicing import build_line_items, resolve_entity, resolve_bank_account


def validate_payment_detection(company, ledgers, *, previous=None):
    """Validate a merged entity and return its canonical detection ledger name.

    A disabled entity may retain its saved pair even after the ledger disappears.
    New selections and every enabled entity must resolve against this user's registry.
    """
    from click import ClickException
    from istota.money.cli import _ledger_scope_name, resolve_ledger
    from istota.money.core.ledger import list_open_accounts

    enabled = company.payment_detection_enabled
    ledger = company.payment_detection_ledger
    account = company.payment_detection_income_account
    if not isinstance(enabled, bool):
        raise ValueError("invalid payment_detection_enabled — expected a boolean")
    if not isinstance(ledger, str) or not isinstance(account, str):
        raise ValueError("Payment detection ledger and income account must be text")
    if not enabled and previous is not None and (
        ledger == previous.payment_detection_ledger
        and account == previous.payment_detection_income_account
    ):
        return ledger
    if enabled and (not ledger or not account):
        raise ValueError("Payment detection requires a ledger and income account")
    if not ledger:
        if account:
            raise ValueError("Payment detection income account requires a ledger")
        return ""
    try:
        ledger_path = resolve_ledger(ledger, ledgers)
    except ClickException as exc:
        raise ValueError(f"Payment detection ledger not found: {ledger}") from exc
    if account:
        if not account.startswith("Income:"):
            raise ValueError("Payment detection account must be a declared Income:* account")
        try:
            accounts = list_open_accounts(ledger_path, strict=True)
        except OSError as exc:
            raise ValueError("Could not read payment detection ledger") from exc
        if account not in accounts:
            raise ValueError(f"Payment detection income account not declared in ledger: {account}")
    return _ledger_scope_name(ledger, ledgers)


def open_invoice(config, number, entries, *, automatic=False, ledgers=(), detection_cache=None):
    """Build a matchable invoice from a live work snapshot."""
    if not entries or any(e.paid_date is not None for e in entries):
        return None
    items = build_line_items(entries, config.services)
    if not items or len(items) != len(entries):
        return None
    entity = resolve_entity(
        config, entry=entries[0], client_config=config.clients.get(entries[0].client),
    )
    ledger = entity.payment_detection_ledger
    if automatic:
        if not entity.payment_detection_enabled:
            return None
        if detection_cache is None:
            detection_cache = {}
        identity = id(entity)
        if identity not in detection_cache:
            try:
                detection_cache[identity] = validate_payment_detection(entity, ledgers)
            except ValueError as exc:
                logging.getLogger(__name__).warning(
                    "auto-match: skipping entity %s: %s", entity.name, exc,
                )
                detection_cache[identity] = None
        ledger = detection_cache[identity]
        if ledger is None:
            return None
    return OpenInvoice(
        number=number, client=entries[0].client,
        date=work.invoice_issue_date(entries),
        total=sum(item.amount for item in items),
        bank_account=resolve_bank_account(entity, config),
        ledger=ledger, income_account=entity.payment_detection_income_account,
    )


def open_invoices(config, data_dir, *, automatic=False, ledgers=(), detection_cache=None):
    invoices = []
    if detection_cache is None:
        detection_cache = {}
    for number in work.get_invoice_numbers(data_dir):
        invoice = open_invoice(
            config, number, work.get_entries_for_invoice(data_dir, number),
            automatic=automatic, ledgers=ledgers, detection_cache=detection_cache,
        )
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


def revert_invoice_payment(conn, data_dir, invoice_number):
    """Reopen work and its match history with the same lock order as settlement."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        count = work.clear_invoice_payment(data_dir, invoice_number)
        db.revert_settled(conn, invoice_number)
    return count
