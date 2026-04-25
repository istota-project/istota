"""Invoice API routes."""

from __future__ import annotations

import click
from fastapi import APIRouter, Depends, HTTPException

from istota.money.api.deps import get_ctx
from istota.money.api.models import (
    InvoiceCreateRequest,
    InvoiceGenerateRequest,
    InvoicePaidRequest,
    InvoiceVoidRequest,
)
from istota.money.cli import Context, _get_db_conn, _load_invoicing_config, _require_data_dir

router = APIRouter()


@router.get("/invoices")
def list_invoices(
    client: str | None = None,
    show_all: bool = False,
    ctx: Context = Depends(get_ctx),
):
    try:
        config, _, _ = _load_invoicing_config(ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    from istota.money.core.invoicing import build_line_items
    from istota.money.work import get_invoice_numbers, get_entries_for_invoice

    try:
        data_dir = _require_data_dir(ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    invoice_numbers = get_invoice_numbers(data_dir)

    invoices = []
    for inv_num in invoice_numbers:
        inv_entries = get_entries_for_invoice(data_dir, inv_num)
        if not inv_entries:
            continue

        if client and not any(e.client == client for e in inv_entries):
            continue

        items = build_line_items(inv_entries, config.services)
        total = sum(item.amount for item in items)

        is_paid = all(e.paid_date is not None for e in inv_entries)
        paid_date_val = inv_entries[0].paid_date

        if is_paid and not show_all:
            continue

        client_key = inv_entries[0].client
        client_config = config.clients.get(client_key)
        client_name = client_config.name if client_config else client_key
        inv_date = min(e.date for e in inv_entries)

        invoice_info = {
            "invoice_number": inv_num,
            "client": client_name,
            "date": inv_date.isoformat(),
            "total": round(total, 2),
            "status": "paid" if is_paid else "outstanding",
        }
        if is_paid and paid_date_val:
            invoice_info["paid_date"] = paid_date_val.isoformat()
        invoices.append(invoice_info)

    outstanding = [i for i in invoices if i["status"] == "outstanding"]
    return {
        "status": "ok",
        "invoice_count": len(invoices),
        "outstanding_count": len(outstanding),
        "invoices": invoices,
    }


@router.post("/invoices/generate")
def generate_invoices(
    req: InvoiceGenerateRequest,
    ctx: Context = Depends(get_ctx),
):
    from istota.money.core.invoicing import generate_invoices_for_period

    try:
        config, accounting_path, invoice_output_dir = _load_invoicing_config(ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        data_dir = _require_data_dir(ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        results = generate_invoices_for_period(
            config=config, config_path=ctx.invoicing_config_path,
            accounting_path=accounting_path, data_dir=data_dir,
            period=req.period, client_filter=req.client,
            entity_filter=req.entity, dry_run=req.dry_run,
            invoice_output_dir=invoice_output_dir,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not results:
        period_desc = f" for period {req.period}" if req.period else ""
        return {"status": "ok", "message": f"No uninvoiced entries found{period_desc}", "invoices": []}

    total = sum(r["total"] for r in results)
    result = {
        "status": "ok",
        "invoice_count": len(results),
        "total": round(total, 2),
        "dry_run": req.dry_run,
        "invoices": results,
    }
    if req.period:
        result["period"] = req.period
    return result


@router.post("/invoices/paid")
def invoice_paid(
    req: InvoicePaidRequest,
    ctx: Context = Depends(get_ctx),
):
    from datetime import datetime

    from istota.money.core.invoicing import (
        compute_income_lines, create_income_posting,
        resolve_bank_account, resolve_currency, resolve_entity,
    )
    from istota.money.core.transactions import append_to_ledger
    from istota.money.core.ledger import run_bean_check
    from istota.money.work import get_entries_for_invoice, record_invoice_payment

    try:
        parsed_date = datetime.strptime(req.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    try:
        config, _, _ = _load_invoicing_config(ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        data_dir = _require_data_dir(ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    entries = get_entries_for_invoice(data_dir, req.invoice_number)
    if not entries:
        raise HTTPException(status_code=404, detail=f"Invoice {req.invoice_number} not found")

    if all(e.paid_date is not None for e in entries):
        raise HTTPException(status_code=400, detail=f"Invoice {req.invoice_number} is already paid")

    first_entry = entries[0]
    client_config = config.clients.get(first_entry.client)
    if not client_config:
        raise HTTPException(status_code=400, detail=f"Client '{first_entry.client}' not found in config")

    entity = resolve_entity(config, entry=first_entry, client_config=client_config)
    bank_account = req.bank or resolve_bank_account(entity, config)
    currency = resolve_currency(entity, config)

    income_lines = compute_income_lines(entries, config.services)
    if not income_lines:
        raise HTTPException(status_code=400, detail=f"No billable items found for {req.invoice_number}")

    total = sum(income_lines.values())
    ledger_path = None

    should_post = not req.no_post and client_config.ledger_posting
    if should_post:
        posting = create_income_posting(
            invoice_number=req.invoice_number, client_name=client_config.name,
            income_lines=income_lines, payment_date=parsed_date,
            bank_account=bank_account, currency=currency,
        )
        try:
            ledger_path = get_ledger_path(req.ledger, ctx)
        except click.ClickException as e:
            raise HTTPException(status_code=400, detail=str(e))
        append_to_ledger(ledger_path, [posting])

        success, errors = run_bean_check(ledger_path)
        if not success:
            raise HTTPException(status_code=500, detail={
                "error": "Payment recorded but ledger validation failed",
                "validation_errors": errors[:5],
            })

    record_invoice_payment(data_dir, req.invoice_number, parsed_date.isoformat())

    result = {
        "status": "ok",
        "invoice_number": req.invoice_number,
        "client": client_config.name,
        "amount": round(total, 2),
        "payment_date": parsed_date.isoformat(),
        "bank_account": bank_account,
    }
    if not should_post:
        result["no_post"] = True
    return result


@router.post("/invoices/create")
def invoice_create(
    req: InvoiceCreateRequest,
    ctx: Context = Depends(get_ctx),
):
    from datetime import date, timedelta

    from istota.money.core.invoicing import (
        build_line_items, format_invoice_number,
        generate_invoice_html, generate_invoice_pdf,
        resolve_entity as resolve_entity_fn, update_invoice_number,
    )
    from istota.money.core.models import Invoice, InvoiceLineItem
    from istota.money.work import add_work_entry, get_entries_for_invoice

    try:
        config, accounting_path, invoice_output_dir = _load_invoicing_config(ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    client_config = config.clients.get(req.client_key)
    if not client_config:
        available = list(config.clients.keys())
        raise HTTPException(status_code=400, detail=f"Client '{req.client_key}' not found. Available: {', '.join(available)}")

    if req.entity:
        if req.entity not in config.companies:
            available = list(config.companies.keys())
            raise HTTPException(status_code=400, detail=f"Entity '{req.entity}' not found. Available: {', '.join(available)}")
        resolved_entity = config.companies[req.entity]
    else:
        resolved_entity = resolve_entity_fn(config, client_config=client_config)

    service_entries = []
    if req.service:
        if req.service not in config.services:
            available = list(config.services.keys())
            raise HTTPException(status_code=400, detail=f"Service '{req.service}' not found. Available: {', '.join(available)}")
        service_entries.append((req.service, req.qty, req.description or "", req.entity or ""))

    manual_items = []
    if req.items:
        for item_str in req.items:
            parts = item_str.rsplit(" ", 1)
            if len(parts) != 2:
                raise HTTPException(status_code=400, detail=f"Invalid item format: {item_str}. Use: \"description amount\"")
            desc = parts[0].strip('"').strip("'")
            try:
                amt = float(parts[1])
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid amount in item: {parts[1]}")
            manual_items.append((desc, amt))

    if not service_entries and not manual_items:
        raise HTTPException(status_code=400, detail="No line items specified. Use service/qty or items")

    try:
        data_dir = _require_data_dir(ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    invoice_number = config.next_invoice_number
    invoice_date = date.today()
    number_str = format_invoice_number(invoice_number)

    for svc_key, svc_qty, svc_desc, svc_entity in service_entries:
        add_work_entry(
            data_dir, invoice_date.isoformat(), req.client_key, svc_key,
            qty=svc_qty, description=svc_desc, entity=svc_entity,
            invoice=number_str,
        )

    for desc, amt in manual_items:
        add_work_entry(
            data_dir, invoice_date.isoformat(), req.client_key, "_manual",
            amount=amt, description=desc, entity=req.entity or "",
            invoice=number_str,
        )

    inv_entries = get_entries_for_invoice(data_dir, number_str)

    items = build_line_items(
        [e for e in inv_entries if e.service != "_manual"],
        config.services,
    )
    for e in inv_entries:
        if e.service == "_manual":
            items.append(InvoiceLineItem(
                display_name=e.description or "Manual item",
                description="",
                quantity=1, rate=e.amount or 0,
                discount=0, amount=e.amount or 0,
            ))

    total = sum(i.amount for i in items)
    due_date = invoice_date + timedelta(days=client_config.terms) if isinstance(client_config.terms, int) else None

    inv = Invoice(
        number=number_str,
        date=invoice_date, due_date=due_date,
        client=client_config, company=resolved_entity,
        items=items, total=total, group_name="",
    )

    logo_path = None
    if resolved_entity.logo:
        logo_path = accounting_path / resolved_entity.logo
        if not logo_path.exists():
            logo_path = None
    html = generate_invoice_html(inv, logo_path=logo_path)
    output_dir = invoice_output_dir / str(invoice_date.year)
    pdf_filename = f"Invoice-{invoice_number:06d}-{invoice_date.strftime('%m_%d_%Y')}.pdf"
    pdf_path = output_dir / pdf_filename
    generate_invoice_pdf(html, pdf_path)

    update_invoice_number(ctx.invoicing_config_path, invoice_number + 1)

    return {
        "status": "ok",
        "invoice_number": inv.number,
        "client": client_config.name,
        "total": round(total, 2),
        "due_date": due_date.isoformat() if due_date else str(client_config.terms),
    }


@router.post("/invoices/void")
def invoice_void(
    req: InvoiceVoidRequest,
    ctx: Context = Depends(get_ctx),
):
    from pathlib import Path

    from istota.money.work import get_entries_for_invoice, void_invoice

    try:
        data_dir = _require_data_dir(ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    entries = get_entries_for_invoice(data_dir, req.invoice_number)
    if not entries:
        raise HTTPException(status_code=404, detail=f"Invoice {req.invoice_number} not found")

    # Check if invoice has been paid (ledger posting may exist)
    is_paid = any(e.paid_date is not None for e in entries)
    if is_paid and not req.force:
        raise HTTPException(
            status_code=400,
            detail=f"Invoice {req.invoice_number} has been marked as paid. "
            "Use force=true to void anyway.",
        )

    count = void_invoice(data_dir, req.invoice_number)

    # Clean up DB state
    db_cleanup = {}
    db_conn = _get_db_conn(ctx)
    if db_conn:
        try:
            from istota.money.db import clear_invoice_state
            db_cleanup = clear_invoice_state(db_conn, req.invoice_number)
            db_conn.commit()
        finally:
            db_conn.close()

    # Optionally delete PDF
    pdf_deleted = False
    if req.delete_pdf:
        try:
            config, _, invoice_output_dir = _load_invoicing_config(ctx)
            pdf_deleted = _delete_invoice_pdf(invoice_output_dir, req.invoice_number)
        except click.ClickException:
            pass  # No invoicing config — skip PDF deletion

    result = {
        "status": "ok",
        "invoice_number": req.invoice_number,
        "entries_voided": count,
        "was_paid": is_paid,
    }
    if db_cleanup:
        result["db_cleanup"] = db_cleanup
    if req.delete_pdf:
        result["pdf_deleted"] = pdf_deleted
    return result


def _delete_invoice_pdf(invoice_output_dir: Path, invoice_number: str) -> bool:
    """Find and delete PDF file for an invoice number. Returns True if a file was deleted."""
    # Invoice number format: INV-NNNNNN — extract the numeric part
    import re
    match = re.match(r"INV-(\d+)", invoice_number)
    if not match:
        return False
    num = int(match.group(1))
    # PDFs are stored as Invoice-NNNNNN-MM_DD_YYYY.pdf across year directories
    if not invoice_output_dir.exists():
        return False
    pattern = f"Invoice-{num:06d}-*.pdf"
    for year_dir in invoice_output_dir.iterdir():
        if year_dir.is_dir():
            for pdf in year_dir.glob(pattern):
                pdf.unlink()
                return True
    return False
