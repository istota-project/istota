"""Invoicing system for config-driven invoice generation (cash-basis).

Parses INVOICING.md config and work log files, generates PDF invoices
via WeasyPrint. No ledger entries at invoice time — income is recognized
when payment is recorded via `invoice paid` (cash-basis accounting).

Outstanding invoices are tracked via the work log: entries stamped with
`invoice = "INV-XXXX"` but no `paid_date` are outstanding.

Used by the accounting skill CLI:
    python -m istota.skills.accounting invoice generate [--period 2026-02]
    python -m istota.skills.accounting invoice list [--all]
    python -m istota.skills.accounting invoice paid INV-001 --date 2026-02-15
    python -m istota.skills.accounting invoice create acme --service consulting --qty 40
"""

import base64
import mimetypes
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import tomli


@dataclass
class CompanyConfig:
    name: str
    address: str = ""
    email: str = ""
    payment_instructions: str = ""
    logo: str = ""  # path relative to accounting_path, e.g. "invoices/assets/logo.png"
    key: str = ""  # entity key, e.g. "personal", "llc"
    ar_account: str = ""  # per-entity A/R override
    bank_account: str = ""  # per-entity bank account override
    currency: str = ""  # per-entity currency override


@dataclass
class ClientConfig:
    key: str
    name: str
    address: str = ""
    email: str = ""
    terms: int | str = 30
    ar_account: str = ""  # e.g. "Assets:Accounts-Receivable"; auto-generated if empty
    entity: str = ""  # default entity for this client
    schedule: str = "on-demand"
    schedule_day: int = 1
    reminder_days: int = 3
    notifications: str = ""  # per-client notification surface override
    days_until_overdue: int = 0  # per-client override (0 = use global)
    bundles: list[dict] = field(default_factory=list)
    separate: list[str] = field(default_factory=list)


@dataclass
class ServiceConfig:
    key: str
    display_name: str
    rate: float
    type: str = "hours"  # "hours" | "days" | "flat" | "other"
    income_account: str = ""  # e.g. "Income:Consulting"; auto-generated if empty


@dataclass
class InvoicingConfig:
    accounting_path: str
    work_log: str
    invoice_output: str
    next_invoice_number: int
    company: CompanyConfig
    clients: dict[str, ClientConfig]
    services: dict[str, ServiceConfig]
    default_ar_account: str = "Assets:Accounts-Receivable"
    default_bank_account: str = "Assets:Bank:Checking"
    currency: str = "USD"
    companies: dict[str, CompanyConfig] = field(default_factory=dict)
    default_entity: str = "default"
    notifications: str = ""  # global default notification surface
    days_until_overdue: int = 0  # days after invoice date before overdue (0 = disabled)


@dataclass
class WorkEntry:
    date: date
    client: str
    service: str
    qty: float | None = None
    amount: float | None = None
    discount: float = 0
    description: str = ""
    entity: str = ""  # explicit entity override
    invoice: str = ""  # auto-set when invoiced (e.g. "INV-000042")
    paid_date: date | None = None  # auto-set when payment recorded


@dataclass
class InvoiceLineItem:
    display_name: str
    description: str
    quantity: float
    rate: float
    discount: float
    amount: float


@dataclass
class Invoice:
    number: str
    date: date
    due_date: date | None
    client: ClientConfig
    company: CompanyConfig
    items: list[InvoiceLineItem]
    total: float
    group_name: str = ""


def _resolve_nc_path(path: str) -> Path:
    """Resolve a Nextcloud path to a local filesystem path via mount.

    If NEXTCLOUD_MOUNT_PATH is set and the path looks like a Nextcloud path
    (starts with /Users/, /Channels/, etc.), prepend the mount prefix.
    If the path is already absolute and exists, use it as-is.
    """
    mount_path = os.environ.get("NEXTCLOUD_MOUNT_PATH", "")
    if not mount_path:
        return Path(path)
    local = Path(mount_path) / path.lstrip("/")
    return local


WORK_LOG_TEMPLATE = """\
# Work Log

Add billable entries in the TOML block below.

```toml
# [[entries]]
# date = 2026-01-01
# client = "client_key"
# service = "service_key"
# qty = 4
# discount = 0
# description = "Work performed"
# entity = "entity_key"  # optional: override entity for this entry
# invoice = "INV-000001"  # auto-set when invoiced
# paid_date = 2026-02-15  # auto-set when payment recorded

# [[entries]]
# date = 2026-01-02
# client = "client_key"
# service = "expenses"
# amount = 150.00
# description = "Reimbursable expense"
```
"""


def _extract_toml_from_markdown(text: str) -> str:
    """Extract TOML content from a markdown file with ```toml code blocks."""
    pattern = r"```toml\s*\n(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)
    if not matches:
        raise ValueError("No TOML code block found in markdown file")
    return "\n".join(matches)


def resolve_entity(
    config: InvoicingConfig,
    entry: WorkEntry | None = None,
    client_config: ClientConfig | None = None,
) -> CompanyConfig:
    """Resolve entity for an entry/client using the chain: entry > client > default.

    Returns the CompanyConfig for the resolved entity.
    """
    entity_key = ""
    if entry and entry.entity:
        entity_key = entry.entity
    elif client_config and client_config.entity:
        entity_key = client_config.entity
    else:
        entity_key = config.default_entity

    return config.companies.get(entity_key, config.company)


def resolve_bank_account(entity: CompanyConfig, config: InvoicingConfig) -> str:
    """Resolve bank account: entity > config default."""
    if entity.bank_account:
        return entity.bank_account
    return config.default_bank_account


def resolve_currency(entity: CompanyConfig, config: InvoicingConfig) -> str:
    """Resolve currency: entity > config default."""
    if entity.currency:
        return entity.currency
    return config.currency


def _parse_company_data(key: str, company_data: dict) -> CompanyConfig:
    """Parse a single company/entity config dict into a CompanyConfig."""
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


def parse_invoicing_config(config_path: Path) -> InvoicingConfig:
    """Parse INVOICING.md config file.

    The file is markdown with an embedded TOML code block containing
    company info, client definitions, service definitions, and settings.

    Supports two company formats:
    - Single entity: [company] section (backward compat, wrapped as key "default")
    - Multi-entity: [companies.<key>] sections
    """
    text = config_path.read_text()
    toml_str = _extract_toml_from_markdown(text)
    data = tomli.loads(toml_str)

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
        # Use first key as default
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
        work_log=data.get("work_log", ""),
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


def parse_work_log(work_log_path: Path) -> list[WorkEntry]:
    """Parse work log markdown file with embedded TOML entries.

    Format:
    ```toml
    [[entries]]
    date = 2026-02-01
    client = "acme"
    service = "consulting"
    hours = 4
    description = "Architecture review"
    ```
    """
    text = work_log_path.read_text()
    toml_str = _extract_toml_from_markdown(text)
    data = tomli.loads(toml_str)

    entries = []
    for entry_data in data.get("entries", []):
        entry_date = entry_data.get("date")
        if isinstance(entry_date, str):
            parts = entry_date.split("-")
            entry_date = date(int(parts[0]), int(parts[1]), int(parts[2]))

        paid_date_raw = entry_data.get("paid_date")
        paid_date_val = None
        if paid_date_raw:
            if isinstance(paid_date_raw, str):
                parts = paid_date_raw.split("-")
                paid_date_val = date(int(parts[0]), int(parts[1]), int(parts[2]))
            elif isinstance(paid_date_raw, date):
                paid_date_val = paid_date_raw

        entries.append(WorkEntry(
            date=entry_date,
            client=entry_data.get("client", ""),
            service=entry_data.get("service", ""),
            qty=entry_data.get("qty"),
            amount=entry_data.get("amount"),
            discount=float(entry_data.get("discount", 0)),
            description=entry_data.get("description", ""),
            entity=entry_data.get("entity", ""),
            invoice=entry_data.get("invoice", ""),
            paid_date=paid_date_val,
        ))

    return entries


def filter_entries_by_period(
    entries: list[WorkEntry],
    period: str,
    client: str | None = None,
) -> list[WorkEntry]:
    """Filter work entries by billing period (YYYY-MM) and optional client.

    Args:
        entries: All work entries
        period: Period string in YYYY-MM format
        client: Optional client key to filter by
    """
    year, month = map(int, period.split("-"))

    filtered = []
    for entry in entries:
        if entry.date.year == year and entry.date.month == month:
            if client is None or entry.client == client:
                filtered.append(entry)

    return filtered


def select_uninvoiced_entries(
    entries: list[WorkEntry],
    period: str | None = None,
    client: str | None = None,
) -> list[tuple[int, WorkEntry]]:
    """Select uninvoiced work entries, optionally bounded by period and client.

    Returns (original_index, entry) tuples for entries that have no invoice set.
    If period is given (YYYY-MM), it acts as an upper date bound (entries with
    date <= last day of that month). If client is given, filters by client key.
    """
    # Compute upper date bound from period
    upper_bound = None
    if period:
        year, month = map(int, period.split("-"))
        if month == 12:
            upper_bound = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            upper_bound = date(year, month + 1, 1) - timedelta(days=1)

    result = []
    for idx, entry in enumerate(entries):
        if entry.invoice:
            continue
        if upper_bound and entry.date > upper_bound:
            continue
        if client and entry.client != client:
            continue
        result.append((idx, entry))

    return result


def group_entries_by_bundle(
    entries: list[WorkEntry],
    client_config: ClientConfig,
) -> list[tuple[str, list[WorkEntry]]]:
    """Group work entries according to client bundling rules.

    Returns list of (group_name, entries) tuples. Services in the same
    bundle are grouped together. Services in the "separate" list each
    get their own group. Unbundled services go into a "Services" group.
    """
    if not entries:
        return []

    # Build lookup: service -> bundle name
    service_to_bundle = {}
    for bundle in client_config.bundles:
        bundle_name = bundle.get("name", "Services")
        for svc in bundle.get("services", []):
            service_to_bundle[svc] = bundle_name

    # Separate services get their own groups
    separate_set = set(client_config.separate)

    groups: dict[str, list[WorkEntry]] = {}
    for entry in entries:
        if entry.service in separate_set:
            group_key = entry.service
        elif entry.service in service_to_bundle:
            group_key = service_to_bundle[entry.service]
        else:
            group_key = "Services"

        if group_key not in groups:
            groups[group_key] = []
        groups[group_key].append(entry)

    return list(groups.items())


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
        else:
            # hours
            qty = entry.qty or 0
            rate = svc.rate
            subtotal = qty * rate

        items.append(InvoiceLineItem(
            display_name=name, description=desc,
            quantity=qty, rate=rate,
            discount=disc, amount=subtotal - disc,
        ))

    return items


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
        number=number_str,
        date=invoice_date,
        due_date=due_date,
        client=client_config,
        company=company,
        items=items,
        total=total,
        group_name=group_name,
    )


def _embed_logo(logo_path: Path) -> str:
    """Read a logo file and return a base64 data URI for embedding in HTML."""
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
        @page {{
            size: letter;
            margin: 0.75in;
        }}
        body {{
            font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
            font-size: 11pt;
            color: #333;
            margin: 0;
            padding: 0;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 40px;
            padding-bottom: 20px;
            border-bottom: 2px solid #2c3e50;
        }}
        .company-name {{
            font-size: 24pt;
            font-weight: bold;
            color: #2c3e50;
        }}
        .company-logo {{
            max-width: 200px;
            height: auto;
        }}
        .invoice-meta {{
            text-align: right;
        }}
        .invoice-title {{
            font-size: 18pt;
            font-weight: bold;
            color: #2c3e50;
            margin-bottom: 8px;
        }}
        .meta-row {{
            margin: 4px 0;
        }}
        .meta-label {{
            font-weight: bold;
            color: #7f8c8d;
        }}
        .addresses {{
            display: flex;
            justify-content: space-between;
            margin-bottom: 30px;
        }}
        .address-block {{
            width: 45%;
        }}
        .section-label {{
            font-weight: bold;
            color: #7f8c8d;
            text-transform: uppercase;
            font-size: 9pt;
            letter-spacing: 0.5px;
            margin-bottom: 8px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 30px;
        }}
        th {{
            background: #2c3e50;
            color: white;
            padding: 10px 12px;
            text-align: left;
            font-size: 10pt;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        th.right {{
            text-align: right;
        }}
        td {{
            padding: 10px 12px;
            border-bottom: 1px solid #e0e0e0;
        }}
        td.right {{
            text-align: right;
        }}
        .item-desc {{
            font-size: 9pt;
            color: #666;
        }}
        tfoot .summary-row td {{
            border-bottom: none;
            padding-top: 6px;
            padding-bottom: 6px;
        }}
        tfoot .amount-due {{
            font-size: 14pt;
            font-weight: bold;
            color: #2c3e50;
            border-top: 2px solid #2c3e50;
            padding-top: 10px;
        }}
        .payment {{
            margin-top: 30px;
            padding: 16px;
            background: #f8f9fa;
            border-radius: 6px;
            border-left: 4px solid #2c3e50;
        }}
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


def create_income_posting(
    invoice_number: str,
    client_name: str,
    income_lines: dict[str, float],
    payment_date: date,
    bank_account: str = "Assets:Bank:Checking",
    currency: str = "USD",
) -> str:
    """Generate a beancount income entry for cash-basis accounting.

    Called when payment is received. Creates a Bank debit + Income credits
    entry, recognizing income at payment time (not invoice time).

    Example output:
        2026-02-15 * "Acme Corp" "Payment for INV-000042"
          Assets:Bank:Checking  2375.00 USD
          Income:Consulting  -1500.00 USD
          Income:Development  -875.00 USD

    Args:
        invoice_number: e.g. "INV-000042"
        client_name: Client display name
        income_lines: Mapping of income account -> amount
        payment_date: Date of payment
        bank_account: Bank account to debit
        currency: Currency code
    """
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
    """Compute income account -> amount mapping from work entries.

    Uses the same line item calculation as invoice generation to ensure
    amounts match between invoice PDF and income posting.
    """
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


def update_invoice_number(config_path: Path, new_number: int) -> None:
    """Update next_invoice_number in the INVOICING.md config file."""
    text = config_path.read_text()
    updated = re.sub(
        r"(next_invoice_number\s*=\s*)\d+",
        f"\\g<1>{new_number}",
        text,
    )
    config_path.write_text(updated)


def _stamp_work_log_field(
    work_log_path: Path,
    stamps: dict[int, tuple[str, str]],
) -> None:
    """Stamp work log entries with arbitrary key-value pairs.

    Reads the raw markdown file, finds each [[entries]] block by position,
    and inserts a `key = "value"` line. Processes in reverse index order
    to avoid position shifts.

    Args:
        work_log_path: Path to the work log markdown file.
        stamps: Mapping of entry index (0-based) to (field_name, value) tuples.
    """
    if not stamps:
        return

    text = work_log_path.read_text()

    # Find all [[entries]] positions in the raw text
    entry_pattern = re.compile(r"^\[\[entries\]\]\s*$", re.MULTILINE)
    entry_positions = [m.start() for m in entry_pattern.finditer(text)]

    # Process in reverse order to preserve positions
    for idx in sorted(stamps.keys(), reverse=True):
        if idx >= len(entry_positions):
            continue

        field_name, value = stamps[idx]
        entry_start = entry_positions[idx]

        # Find end of this entry's key-value block:
        # either the next [[entries]] or the closing ``` fence
        if idx + 1 < len(entry_positions):
            block_end = entry_positions[idx + 1]
        else:
            # Last entry — find the closing ``` fence
            fence_pos = text.find("```", entry_start)
            block_end = fence_pos if fence_pos != -1 else len(text)

        # Find last key = value line in this block
        block_text = text[entry_start:block_end]
        # Find the last non-empty, non-comment line with a key = value pattern
        lines = block_text.split("\n")
        last_kv_offset = 0
        running_offset = 0
        for line in lines:
            line_end = running_offset + len(line)
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped and not stripped.startswith("[["):
                last_kv_offset = line_end
            running_offset = line_end + 1  # +1 for the newline

        insert_pos = entry_start + last_kv_offset
        text = text[:insert_pos] + f'\n{field_name} = "{value}"' + text[insert_pos:]

    work_log_path.write_text(text)


def stamp_work_log_entries(
    work_log_path: Path,
    stamps: dict[int, str],
) -> None:
    """Stamp processed work log entries with their invoice numbers.

    Args:
        work_log_path: Path to the work log markdown file.
        stamps: Mapping of entry index (0-based, positional) to invoice number string.
    """
    field_stamps = {idx: ("invoice", value) for idx, value in stamps.items()}
    _stamp_work_log_field(work_log_path, field_stamps)


def stamp_work_log_paid_dates(
    work_log_path: Path,
    stamps: dict[int, str],
) -> None:
    """Stamp work log entries with their payment dates.

    Args:
        work_log_path: Path to the work log markdown file.
        stamps: Mapping of entry index (0-based, positional) to date string (YYYY-MM-DD).
    """
    field_stamps = {idx: ("paid_date", value) for idx, value in stamps.items()}
    _stamp_work_log_field(work_log_path, field_stamps)


def _resolve_entry_entity_key(
    entry: WorkEntry,
    client_config: ClientConfig,
    config: InvoicingConfig,
) -> str:
    """Get the entity key for a work entry (without resolving to CompanyConfig)."""
    if entry.entity:
        return entry.entity
    if client_config.entity:
        return client_config.entity
    return config.default_entity


def generate_invoices_for_period(
    config: InvoicingConfig,
    config_path: Path,
    period: str | None = None,
    client_filter: str | None = None,
    entity_filter: str | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Generate invoices for uninvoiced work log entries.

    This is the main orchestration function that:
    1. Parses work log entries
    2. Selects uninvoiced entries (optionally bounded by period/client/entity)
    3. Groups by (client, entity) then by bundle rules
    4. Generates invoice HTML + PDF for each group
    5. Increments invoice number counter
    6. Stamps processed entries with invoice numbers in the work log

    No ledger entries are created at invoice time (cash-basis accounting).
    Income is recognized when payment is recorded via `invoice paid`.

    Args:
        period: Optional YYYY-MM upper date bound. When set, only uninvoiced
            entries with date <= last day of that month are included.
            When None, all uninvoiced entries are selected.

    Returns list of invoice summary dicts.
    """
    work_log_path = _resolve_nc_path(config.work_log)
    if not work_log_path.exists():
        work_log_path.parent.mkdir(parents=True, exist_ok=True)
        work_log_path.write_text(WORK_LOG_TEMPLATE)
        return []  # Newly created, no entries to process

    entries = parse_work_log(work_log_path)
    indexed_entries = select_uninvoiced_entries(entries, period, client_filter)

    if not indexed_entries:
        return []

    # Build id(entry) -> original_index map before bundle grouping
    # (group_entries_by_bundle appends same object refs, doesn't copy)
    index_map = {id(entry): idx for idx, entry in indexed_entries}

    # Group entries by (client, entity_key)
    grouped: dict[tuple[str, str], list[WorkEntry]] = {}
    for _idx, entry in indexed_entries:
        client_config = config.clients.get(entry.client)
        if not client_config:
            continue
        entity_key = _resolve_entry_entity_key(entry, client_config, config)

        # Apply entity filter
        if entity_filter and entity_key != entity_filter:
            continue

        group_key = (entry.client, entity_key)
        if group_key not in grouped:
            grouped[group_key] = []
        grouped[group_key].append(entry)

    if not grouped:
        return []

    # Determine output directory
    year = str(date.today().year) if not period else period.split("-")[0]
    accounting_path = _resolve_nc_path(config.accounting_path)
    output_dir = accounting_path / config.invoice_output / year

    invoice_number = config.next_invoice_number
    results = []
    stamps: dict[int, str] = {}  # entry_index -> invoice_number_str

    for (client_key, entity_key), client_entity_entries in sorted(grouped.items()):
        client_config = config.clients.get(client_key)
        if not client_config:
            continue

        entity = config.companies.get(entity_key, config.company)

        # Resolve logo per entity
        logo_path = None
        if entity.logo:
            logo_path = accounting_path / entity.logo
            if not logo_path.exists():
                logo_path = None

        groups = group_entries_by_bundle(client_entity_entries, client_config)

        for group_name, group_entries in groups:
            invoice_date = date.today()
            invoice = generate_invoice(
                entries=group_entries,
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
                # Generate PDF
                html = generate_invoice_html(invoice, logo_path=logo_path)
                pdf_filename = f"Invoice-{invoice_number:06d}-{invoice_date.strftime('%m_%d_%Y')}.pdf"
                pdf_path = output_dir / pdf_filename
                generate_invoice_pdf(html, pdf_path)
                summary["file"] = str(pdf_path)

                # Record stamps for each entry in this group
                for entry in group_entries:
                    original_idx = index_map.get(id(entry))
                    if original_idx is not None:
                        stamps[original_idx] = number_str

            results.append(summary)
            invoice_number += 1

    # Update invoice number counter
    if not dry_run and results:
        update_invoice_number(config_path, invoice_number)

    # Stamp work log entries with invoice numbers
    if not dry_run and stamps:
        stamp_work_log_entries(work_log_path, stamps)

    return results
