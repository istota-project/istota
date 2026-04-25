"""Invoicing: config-driven invoice generation, PDF export, payment recording (cash-basis).

Work entries are stored in plaintext TOML files (see work.py). Config (clients,
services, entities, rates) is parsed from a plain TOML file. No ledger entries
at invoice time — income is recognized when payment is recorded (cash-basis).
"""

from __future__ import annotations

import base64
import mimetypes
import re
from datetime import date, timedelta
from pathlib import Path

import tomli

from .models import (
    ClientConfig,
    CompanyConfig,
    Invoice,
    InvoiceLineItem,
    InvoicingConfig,
    ServiceConfig,
    WorkEntry,
)


# =============================================================================
# Config parsing (plain TOML)
# =============================================================================


def parse_invoicing_config(config_path: Path) -> InvoicingConfig:
    """Parse invoicing config file (TOML or UPPERCASE.md with TOML block)."""
    from money._config_io import read_toml_config
    data = read_toml_config(config_path)

    # Parse companies (multi-entity) or company (single entity)
    companies = {}
    companies_data = data.get("companies", {})
    if companies_data:
        for key, comp_data in companies_data.items():
            companies[key] = _parse_company_data(key, comp_data)
    else:
        company_data = data.get("company", {})
        companies["default"] = _parse_company_data("default", company_data)

    default_entity = data.get("default_entity", "")
    if not default_entity:
        default_entity = next(iter(companies))

    company = companies[default_entity]

    # Parse clients
    clients = {}
    for key, client_data in data.get("clients", {}).items():
        invoicing_data = client_data.get("invoicing", {})
        clients[key] = ClientConfig(
            key=key,
            name=client_data.get("name", key),
            address=client_data.get("address", ""),
            email=client_data.get("email", ""),
            terms=client_data.get("terms", 30),
            ar_account=client_data.get("ar_account", ""),
            entity=client_data.get("entity", ""),
            schedule=invoicing_data.get("schedule", "on-demand"),
            schedule_day=invoicing_data.get("day", 1),
            reminder_days=invoicing_data.get("reminder_days", 3),
            notifications=invoicing_data.get("notifications", ""),
            days_until_overdue=invoicing_data.get("days_until_overdue", 0),
            ledger_posting=invoicing_data.get("ledger_posting", True),
            bundles=invoicing_data.get("bundles", []),
            separate=invoicing_data.get("separate", []),
        )

    # Parse services
    services = {}
    for key, svc_data in data.get("services", {}).items():
        services[key] = ServiceConfig(
            key=key,
            display_name=svc_data.get("display_name", key),
            rate=float(svc_data.get("rate", 0)),
            type=svc_data.get("type", "hours"),
            income_account=svc_data.get("income_account", ""),
        )

    return InvoicingConfig(
        accounting_path=data.get("accounting_path", ""),
        invoice_output=data.get("invoice_output", "invoices/generated"),
        next_invoice_number=data.get("next_invoice_number", 1),
        company=company,
        clients=clients,
        services=services,
        default_ar_account=data.get("default_ar_account", "Assets:Accounts-Receivable"),
        default_bank_account=data.get("default_bank_account", "Assets:Bank:Checking"),
        currency=data.get("currency", "USD"),
        companies=companies,
        default_entity=default_entity,
        notifications=data.get("notifications", ""),
        days_until_overdue=data.get("days_until_overdue", 0),
    )


def _parse_company_data(key: str, company_data: dict) -> CompanyConfig:
    return CompanyConfig(
        name=company_data.get("name", ""),
        address=company_data.get("address", ""),
        email=company_data.get("email", ""),
        payment_instructions=company_data.get("payment_instructions", ""),
        logo=company_data.get("logo", ""),
        key=key,
        ar_account=company_data.get("ar_account", ""),
        bank_account=company_data.get("bank_account", ""),
        currency=company_data.get("currency", ""),
    )


# =============================================================================
# Entity / account resolution
# =============================================================================


def resolve_entity(
    config: InvoicingConfig,
    entry: WorkEntry | None = None,
    client_config: ClientConfig | None = None,
) -> CompanyConfig:
    """Resolve entity for an entry/client using the chain: entry > client > default."""
    entity_key = ""
    if entry and entry.entity:
        entity_key = entry.entity
    elif client_config and client_config.entity:
        entity_key = client_config.entity
    else:
        entity_key = config.default_entity
    return config.companies.get(entity_key, config.company)


def resolve_bank_account(entity: CompanyConfig, config: InvoicingConfig) -> str:
    if entity.bank_account:
        return entity.bank_account
    return config.default_bank_account


def resolve_currency(entity: CompanyConfig, config: InvoicingConfig) -> str:
    if entity.currency:
        return entity.currency
    return config.currency


# =============================================================================
# Line items and grouping
# =============================================================================


def build_line_items(
    entries: list[WorkEntry],
    services: dict[str, ServiceConfig],
) -> list[InvoiceLineItem]:
    """Convert work entries to invoice line items using service rates."""
    items = []
    for entry in entries:
        svc = services.get(entry.service)
        if not svc:
            continue

        name = svc.display_name
        desc = entry.description
        disc = entry.discount

        if svc.type == "other":
            subtotal = entry.amount or 0
            rate = subtotal
            qty = 1
        elif svc.type == "flat":
            subtotal = svc.rate
            rate = svc.rate
            qty = 1
        elif svc.type == "days":
            qty = entry.qty or 0
            rate = svc.rate
            subtotal = qty * rate
            if not subtotal and entry.amount:
                subtotal = entry.amount
                rate = subtotal
                qty = 1
        else:
            qty = entry.qty or 0
            rate = svc.rate
            subtotal = qty * rate
            if not subtotal and entry.amount:
                subtotal = entry.amount
                rate = subtotal
                qty = 1

        items.append(InvoiceLineItem(
            display_name=name, description=desc,
            quantity=qty, rate=rate,
            discount=disc, amount=subtotal - disc,
        ))

    return items


def group_entries_by_bundle(
    entries: list[WorkEntry],
    client_config: ClientConfig,
) -> list[tuple[str, list[WorkEntry]]]:
    """Group work entries according to client bundling rules."""
    if not entries:
        return []

    service_to_bundle = {}
    for bundle in client_config.bundles:
        bundle_name = bundle.get("name", "Services")
        for svc in bundle.get("services", []):
            service_to_bundle[svc] = bundle_name

    separate_set = set(client_config.separate)

    groups: dict[str, list[WorkEntry]] = {}
    for entry in entries:
        if entry.service in separate_set:
            group_key = entry.service
        elif entry.service in service_to_bundle:
            group_key = service_to_bundle[entry.service]
        else:
            group_key = "Services"

        groups.setdefault(group_key, []).append(entry)

    return list(groups.items())


# =============================================================================
# Invoice generation
# =============================================================================


def format_invoice_number(number: int) -> str:
    """Format invoice number with zero-padding: INV-000001."""
    return f"INV-{number:06d}"


def generate_invoice(
    entries: list[WorkEntry],
    group_name: str,
    client_config: ClientConfig,
    company: CompanyConfig,
    services: dict[str, ServiceConfig],
    invoice_number: int,
    invoice_date: date,
) -> Invoice:
    """Build an Invoice from work entries for a single group."""
    items = build_line_items(entries, services)
    total = sum(item.amount for item in items)
    if isinstance(client_config.terms, int):
        due_date = invoice_date + timedelta(days=client_config.terms)
    else:
        due_date = None
    number_str = format_invoice_number(invoice_number)

    return Invoice(
        number=number_str, date=invoice_date, due_date=due_date,
        client=client_config, company=company,
        items=items, total=total, group_name=group_name,
    )


def generate_invoices_for_period(
    config: InvoicingConfig,
    config_path: Path,
    accounting_path: Path,
    data_dir: Path,
    period: str | None = None,
    client_filter: str | None = None,
    entity_filter: str | None = None,
    dry_run: bool = False,
    invoice_output_dir: Path | None = None,
) -> list[dict]:
    """Generate invoices for uninvoiced work entries.

    Reads uninvoiced entries from TOML files, groups by client+entity and
    bundle rules, generates PDFs, assigns invoice numbers.

    If ``invoice_output_dir`` is given it is used as the base directory for
    generated PDFs (a year subdirectory is appended).  Otherwise falls back to
    ``accounting_path / config.invoice_output`` for backward compatibility.
    """
    from money.work import get_uninvoiced_entries, assign_invoice_number

    entries = get_uninvoiced_entries(data_dir, client=client_filter, period=period)
    if not entries:
        return []

    # Resolve entity key for each entry
    def _entity_key(entry: WorkEntry) -> str:
        if entry.entity:
            return entry.entity
        cc = config.clients.get(entry.client) or config.clients.get(entry.client.lower())
        if cc and cc.entity:
            return cc.entity
        return config.default_entity

    # Group entries by (client, entity_key)
    grouped: dict[tuple[str, str], list[WorkEntry]] = {}
    for entry in entries:
        client_config = config.clients.get(entry.client) or config.clients.get(entry.client.lower())
        if not client_config:
            continue
        ek = _entity_key(entry)
        if entity_filter and ek != entity_filter:
            continue
        grouped.setdefault((entry.client, ek), []).append(entry)

    if not grouped:
        return []

    year = str(date.today().year) if not period else period.split("-")[0]
    base_dir = invoice_output_dir if invoice_output_dir is not None else accounting_path / config.invoice_output
    output_dir = base_dir / year

    invoice_number = config.next_invoice_number
    results = []

    for (client_key, entity_key), client_entity_entries in sorted(grouped.items()):
        client_config = config.clients.get(client_key)
        if not client_config:
            continue

        entity = config.companies.get(entity_key, config.company)

        logo_path = None
        if entity.logo:
            logo_path = accounting_path / entity.logo
            if not logo_path.exists():
                logo_path = None

        groups = group_entries_by_bundle(client_entity_entries, client_config)

        for group_name, group_entries in groups:
            # Filter to entries whose service exists in config — entries
            # with unknown services cannot produce line items and must not
            # be stamped with an invoice number.
            billable_entries = [
                e for e in group_entries if e.service in config.services
            ]
            if not billable_entries:
                continue

            invoice_date = date.today()
            invoice = generate_invoice(
                entries=billable_entries,
                group_name=group_name,
                client_config=client_config,
                company=entity,
                services=config.services,
                invoice_number=invoice_number,
                invoice_date=invoice_date,
            )

            number_str = format_invoice_number(invoice_number)

            summary = {
                "invoice_number": invoice.number,
                "client": client_config.name,
                "group": group_name,
                "items": len(invoice.items),
                "total": round(invoice.total, 2),
                "terms": invoice.due_date.isoformat() if invoice.due_date else str(invoice.client.terms),
            }
            if entity_key != config.default_entity or len(config.companies) > 1:
                summary["entity"] = entity_key

            if not dry_run:
                html = generate_invoice_html(invoice, logo_path=logo_path)
                pdf_filename = f"Invoice-{invoice_number:06d}-{invoice_date.strftime('%m_%d_%Y')}.pdf"
                pdf_path = output_dir / pdf_filename
                generate_invoice_pdf(html, pdf_path)
                summary["file"] = str(pdf_path)

                entry_indices = [e.id for e in billable_entries if e.id is not None]
                if entry_indices:
                    assign_invoice_number(data_dir, entry_indices, number_str)

            results.append(summary)
            invoice_number += 1

    if not dry_run and results:
        update_invoice_number(config_path, invoice_number)

    return results


def check_scheduled_invoices(
    config: InvoicingConfig,
    db_conn,
    today: date | None = None,
) -> list[str]:
    """Return client keys that are due for scheduled invoice generation.

    Checks each client with ``schedule = "monthly"``.  A client is due when
    ``today >= schedule_day`` (clamped to the last day of the month) and no
    generation has been recorded for the current month in the database.
    """
    import calendar
    import sqlite3

    from money.db import get_invoice_schedule_state

    if today is None:
        today = date.today()

    due: list[str] = []
    for client_key, client_cfg in config.clients.items():
        if client_cfg.schedule != "monthly":
            continue

        # Clamp schedule_day to last day of the current month
        last_day = calendar.monthrange(today.year, today.month)[1]
        effective_day = min(client_cfg.schedule_day, last_day)

        if today.day < effective_day:
            continue

        # Check if already generated this month
        state = get_invoice_schedule_state(db_conn, client_key)
        if state and state.last_generation_at:
            last_gen = date.fromisoformat(state.last_generation_at[:10])
            if last_gen.year == today.year and last_gen.month == today.month:
                continue

        due.append(client_key)

    return due


def update_invoice_number(config_path: Path, new_number: int) -> None:
    """Update next_invoice_number in the invoicing config file."""
    text = config_path.read_text()
    updated = re.sub(
        r"(next_invoice_number\s*=\s*)\d+",
        f"\\g<1>{new_number}",
        text,
    )
    config_path.write_text(updated)


# =============================================================================
# Invoice HTML / PDF
# =============================================================================


def _embed_logo(logo_path: Path) -> str:
    mime_type = mimetypes.guess_type(str(logo_path))[0] or "image/png"
    data = base64.b64encode(logo_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{data}"


def generate_invoice_html(invoice: Invoice, logo_path: Path | None = None) -> str:
    """Generate HTML for an invoice, suitable for PDF conversion."""
    items_html = ""
    has_discounts = any(item.discount > 0 for item in invoice.items)
    for item in invoice.items:
        desc_html = f"<br><span class='item-desc'>{item.description}</span>" if item.description else ""
        discount_cell = f'<td class="right">${item.discount:,.2f}</td>' if has_discounts else ""
        items_html += f"""
        <tr>
            <td>{item.display_name}{desc_html}</td>
            <td class="right">{item.quantity:.2f}</td>
            <td class="right">${item.rate:,.2f}</td>
            {discount_cell}
            <td class="right">${item.amount:,.2f}</td>
        </tr>"""

    payment_html = ""
    if invoice.company.payment_instructions:
        payment_html = f"""
    <div class="payment">
        <div class="section-label">Payment Instructions</div>
        <div style="white-space: pre-line;">{invoice.company.payment_instructions}</div>
    </div>"""

    group_label = f" - {invoice.group_name}" if invoice.group_name else ""
    discount_header = '<th class="right">Discount</th>' if has_discounts else ""
    total_colspan = 4 if has_discounts else 3
    due_text = invoice.due_date.strftime("%B %d, %Y") if invoice.due_date else str(invoice.client.terms)

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Invoice {invoice.number}</title>
    <style>
        @page {{ size: letter; margin: 0.75in; }}
        body {{ font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 11pt; color: #333; margin: 0; padding: 0; }}
        .header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 40px; padding-bottom: 20px; border-bottom: 2px solid #2c3e50; }}
        .company-name {{ font-size: 24pt; font-weight: bold; color: #2c3e50; }}
        .company-logo {{ max-width: 200px; height: auto; }}
        .invoice-meta {{ text-align: right; }}
        .invoice-title {{ font-size: 18pt; font-weight: bold; color: #2c3e50; margin-bottom: 8px; }}
        .meta-row {{ margin: 4px 0; }}
        .meta-label {{ font-weight: bold; color: #7f8c8d; }}
        .addresses {{ display: flex; justify-content: space-between; margin-bottom: 30px; }}
        .address-block {{ width: 45%; }}
        .section-label {{ font-weight: bold; color: #7f8c8d; text-transform: uppercase; font-size: 9pt; letter-spacing: 0.5px; margin-bottom: 8px; }}
        table {{ width: 100%; border-collapse: collapse; margin-bottom: 30px; }}
        th {{ background: #2c3e50; color: white; padding: 10px 12px; text-align: left; font-size: 10pt; text-transform: uppercase; letter-spacing: 0.5px; }}
        th.right {{ text-align: right; }}
        td {{ padding: 10px 12px; border-bottom: 1px solid #e0e0e0; }}
        td.right {{ text-align: right; }}
        .item-desc {{ font-size: 9pt; color: #666; }}
        tfoot .summary-row td {{ border-bottom: none; padding-top: 6px; padding-bottom: 6px; }}
        tfoot .amount-due {{ font-size: 14pt; font-weight: bold; color: #2c3e50; border-top: 2px solid #2c3e50; padding-top: 10px; }}
        .payment {{ margin-top: 30px; padding: 16px; background: #f8f9fa; border-radius: 6px; border-left: 4px solid #2c3e50; }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            {f'<img class="company-logo" src="{_embed_logo(logo_path)}" alt="{invoice.company.name}">' if logo_path and logo_path.exists() else f'<div class="company-name">{invoice.company.name}</div>'}
            <div style="white-space: pre-line; color: #666; margin-top: 4px;">{invoice.company.address}</div>
        </div>
        <div class="invoice-meta">
            <div class="invoice-title">INVOICE</div>
            <div class="meta-row"><span class="meta-label">Number:</span> {invoice.number}</div>
            <div class="meta-row"><span class="meta-label">Date:</span> {invoice.date.strftime("%B %d, %Y")}</div>
            <div class="meta-row"><span class="meta-label">Terms:</span> {due_text}</div>
        </div>
    </div>

    <div class="addresses">
        <div class="address-block">
            <div class="section-label">Bill To</div>
            <div><strong>{invoice.client.name}</strong></div>
            <div style="white-space: pre-line;">{invoice.client.address}</div>
            {f'<div>{invoice.client.email}</div>' if invoice.client.email else ''}
        </div>
        <div class="address-block" style="text-align: right;">
            {f'<div class="section-label">Reference</div><div>{invoice.group_name}</div>' if invoice.group_name else ''}
        </div>
    </div>

    <table>
        <thead>
            <tr>
                <th>Description{group_label}</th>
                <th class="right">Qty</th>
                <th class="right">Unit Price</th>
                {discount_header}
                <th class="right">Total</th>
            </tr>
        </thead>
        <tbody>
            {items_html}
        </tbody>
        <tfoot>
            <tr class="summary-row">
                <td colspan="{total_colspan}" class="right"><strong>Net Price</strong></td>
                <td class="right">${invoice.total:,.2f}</td>
            </tr>
            <tr class="summary-row">
                <td colspan="{total_colspan}" class="right"><strong>Amount Due</strong></td>
                <td class="right amount-due">${invoice.total:,.2f}</td>
            </tr>
        </tfoot>
    </table>

    {payment_html}
</body>
</html>"""


def generate_invoice_pdf(html: str, output_path: Path) -> None:
    """Convert invoice HTML to PDF using WeasyPrint."""
    from weasyprint import HTML
    output_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html).write_pdf(str(output_path))


# =============================================================================
# Income posting (cash-basis)
# =============================================================================


def create_income_posting(
    invoice_number: str,
    client_name: str,
    income_lines: dict[str, float],
    payment_date: date,
    bank_account: str = "Assets:Bank:Checking",
    currency: str = "USD",
) -> str:
    """Generate a beancount income entry for cash-basis accounting."""
    total = sum(income_lines.values())
    client_name_escaped = client_name.replace('"', '\\"')
    narration = f"Payment for {invoice_number}"

    lines = [f'{payment_date.isoformat()} * "{client_name_escaped}" "{narration}"']
    lines.append(f"  {bank_account}  {total:.2f} {currency}")
    for account, amount in sorted(income_lines.items()):
        lines.append(f"  {account}  -{amount:.2f} {currency}")

    return "\n".join(lines)


def compute_income_lines(
    entries: list[WorkEntry],
    services: dict[str, ServiceConfig],
) -> dict[str, float]:
    """Compute income account -> amount mapping from work entries."""
    items = build_line_items(entries, services)
    income_lines: dict[str, float] = {}
    for item in items:
        income_account = "Income:Services"
        for svc_key, svc in services.items():
            if svc.display_name == item.display_name:
                income_account = svc.income_account or f"Income:{svc_key.title()}"
                break
        income_lines[income_account] = income_lines.get(income_account, 0) + item.amount
    return income_lines
