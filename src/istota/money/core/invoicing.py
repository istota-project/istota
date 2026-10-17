"""Invoicing: config-driven invoice generation, PDF export, payment recording (cash-basis).

Work entries and invoices are stored in money.db (see work.py). Config
(clients, services, entities, rates) comes from the config store. No ledger
entries at invoice time — income is recognized when payment is recorded
(cash-basis).
"""

from __future__ import annotations

import base64
import errno
import html
import logging
import mimetypes
import os
import re
import secrets
import threading
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from istota.lib.file_lock import exclusive_lock

from .ids import new_txn_id
from .models import (
    ClientConfig,
    CompanyConfig,
    Invoice,
    InvoiceLineItem,
    InvoicingConfig,
    ServiceConfig,
    WorkEntry,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Config parsing (plain TOML)
# =============================================================================


def parse_invoicing_config(config_path: Path) -> InvoicingConfig:
    """Parse invoicing config file (TOML or UPPERCASE.md with TOML block)."""
    from istota.money._config_io import read_toml_config
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
        payment_detection_enabled=company_data.get("payment_detection_enabled", False),
        payment_detection_ledger=company_data.get("payment_detection_ledger", ""),
        payment_detection_income_account=company_data.get("payment_detection_income_account", ""),
    )


# =============================================================================
# Entity / account resolution
# =============================================================================


def _resolve_logo(accounting_path: Path, logo: str) -> Path | None:
    """Resolve an entity logo, refusing anything outside the accounting folder.

    ``accounting_path / logo`` follows pathlib semantics, so an absolute
    ``logo`` replaces the left-hand side outright and the file is then
    base64-embedded into the PDF. The store rejects such a value at write time;
    this is the read-side half, covering rows written before that check and any
    other writer.
    """
    if not logo:
        return None
    candidate = accounting_path / logo
    try:
        root = accounting_path.resolve()
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        logger.warning(
            "invoice_logo_outside_workspace logo=%r — ignored", logo,
        )
        return None
    return resolved if resolved.exists() else None


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


def entry_entity_key(config: InvoicingConfig, entry: WorkEntry) -> str:
    """The entity key an entry bills under: entry, then client, then default."""
    if entry.entity:
        return entry.entity
    cc = config.clients.get(entry.client) or config.clients.get(entry.client.lower())
    if cc and cc.entity:
        return cc.entity
    return config.default_entity


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


def entry_line_item(entry: WorkEntry, svc: ServiceConfig) -> InvoiceLineItem:
    """Resolve one work entry to a line item using its service's rate rules.

    The single home of the rate branch. The web work list shows the amount an
    entry *will* bill for before it's invoiced, and that number has to be the
    one the invoice actually carries — so both go through here rather than
    each implementing the branch.
    """
    if svc.type == "other":
        subtotal = entry.amount or 0
        rate = subtotal
        qty = 1
    elif svc.type == "flat":
        subtotal = svc.rate
        rate = svc.rate
        qty = 1
    else:
        # "days" and the default (hours) share the same shape: quantity times
        # the service rate, falling back to a bare amount when there's no qty.
        qty = entry.qty or 0
        rate = svc.rate
        subtotal = qty * rate
        if not subtotal and entry.amount:
            subtotal = entry.amount
            rate = subtotal
            qty = 1

    return InvoiceLineItem(
        display_name=svc.display_name,
        description=entry.description,
        quantity=qty,
        rate=rate,
        discount=entry.discount,
        amount=subtotal - entry.discount,
    )


def build_line_items(
    entries: list[WorkEntry],
    services: dict[str, ServiceConfig],
) -> list[InvoiceLineItem]:
    """Convert work entries to invoice line items using service rates.

    An entry whose service isn't configured is silently skipped — it never
    reaches an invoice. The web API rejects unknown services on write and
    flags pre-existing ones, so this stays the last line of defence rather
    than the only one.
    """
    items = []
    for entry in entries:
        svc = services.get(entry.service)
        if not svc:
            continue
        items.append(entry_line_item(entry, svc))
    return items


def invoice_total(
    entries: list[WorkEntry],
    services: dict[str, ServiceConfig],
) -> float:
    """What an invoice over ``entries`` bills, at the current rates.

    ``_manual`` entries (``invoice create --item``) bill their own amount; the
    rest go through :func:`build_line_items`.
    """
    total = sum(
        item.amount
        for item in build_line_items([e for e in entries if e.service != "_manual"], services)
    )
    total += sum(e.amount or 0 for e in entries if e.service == "_manual")
    return total


def group_entries_by_bundle(
    entries: list[WorkEntry],
    client_config: ClientConfig,
) -> list[tuple[str, list[WorkEntry]]]:
    """Group work entries according to client bundling rules.

    Returns ``(group_name, entries)`` pairs. An entry flagged ``separate`` is a
    group of its own, named as the client's ``separate`` list names a service,
    so two flagged entries of one service are two pairs with the same name.
    """
    if not entries:
        return []

    service_to_bundle = {}
    for bundle in client_config.bundles:
        bundle_name = bundle.get("name", "Services")
        for svc in bundle.get("services", []):
            service_to_bundle[svc] = bundle_name

    separate_set = set(client_config.separate)

    groups: dict[str, list[WorkEntry]] = {}
    names: dict[str, str] = {}
    for entry in entries:
        if entry.separate:
            group_key = f"{entry.service}:{entry.uid}"
            group_name = entry.service
        elif entry.service in separate_set:
            group_key = group_name = entry.service
        elif entry.service in service_to_bundle:
            group_key = group_name = service_to_bundle[entry.service]
        else:
            group_key = group_name = "Services"

        groups.setdefault(group_key, []).append(entry)
        names[group_key] = group_name

    return [(names[key], members) for key, members in groups.items()]


# =============================================================================
# Invoice generation
# =============================================================================


def format_invoice_number(number: int) -> str:
    """Format invoice number with zero-padding: INV-000001."""
    return f"INV-{number:06d}"


def parse_invoice_number(number_str: str) -> int | None:
    """Parse the integer out of an ``INV-000238`` style string (None if it doesn't match)."""
    match = re.match(r"INV-0*(\d+)", number_str.strip())
    return int(match.group(1)) if match else None


def highest_existing_invoice_number(data_dir: Path, *, conn=None) -> int:
    """Return the highest invoice number ever issued (0 if none).

    Read from the ``invoices`` table, voided invoices included, so a voided
    number is never issued again. Deriving the next number from this (rather
    than trusting a separate counter) self-heals counter drift.
    """
    from istota.money.work import list_invoices

    highest = 0
    for inv in (record.number for record in list_invoices(data_dir, conn=conn)):
        n = parse_invoice_number(inv)
        if n is not None and n > highest:
            highest = n
    return highest


def persist_next_invoice_number(
    new_number: int,
    *,
    db_path: Path | None,
    config_path: Path | None,
) -> None:
    """Persist the advanced counter to whichever backend the config came from.

    DB wins when present (the in-istota path): the config is loaded from the DB,
    so the counter must be written back there — not to a stale TOML file that
    nobody reads. Falls back to the TOML file for the standalone-CLI escape hatch.
    """
    if db_path is not None:
        from istota.money import config_store

        if config_store.has_invoicing_data(db_path):
            config_store.set_next_invoice_number(db_path, new_number)
            return
    if config_path is not None:
        update_invoice_number(config_path, new_number)


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
    db_path: Path | None = None,
    record_schedule: bool = False,
    entry_uids: list[str] | None = None,
    user_root: Path | None = None,
) -> list[dict]:
    """Generate invoices for uninvoiced work entries.

    Groups uninvoiced entries by client+entity and bundle rules, renders each
    invoice's PDF, then records the invoice and its lines in one transaction.

    ``period`` is an upper bound: every unbilled entry dated on or before the
    end of that month is billed, earlier months included. ``entry_uids`` bills
    exactly the entries named instead, and ``period`` is ignored; an unknown or
    already-invoiced uid is an :class:`EntrySelectionError` raised before
    anything is written.
    The named entries still group by the normal rules.
    If that transaction fails the PDF is deleted and an
    :class:`InvoiceRecordError` is raised, after the counter is advanced past
    the invoices this run did record; no PDF is left without its invoice.

    A real run holds :func:`invoice_issue_lock` from picking the first number
    to recording the last invoice, and each PDF is linked into place without
    replacing a file already at its name.

    ``record_schedule`` writes a monthly client's schedule state in the same
    transaction as each of its invoices.

    If ``invoice_output_dir`` is given it is used as the base directory for
    generated PDFs (a year subdirectory is appended).  Otherwise falls back to
    ``accounting_path / config.invoice_output`` for backward compatibility.
    With ``user_root`` every PDF is written, checked and removed through a
    no-follow walk from it (ISSUE-775).
    """
    kwargs = dict(
        config=config,
        config_path=config_path,
        accounting_path=accounting_path,
        data_dir=data_dir,
        period=period,
        client_filter=client_filter,
        entity_filter=entity_filter,
        dry_run=dry_run,
        invoice_output_dir=invoice_output_dir,
        db_path=db_path,
        record_schedule=record_schedule,
        entry_uids=entry_uids,
        user_root=user_root,
    )
    if dry_run:
        return _generate_invoices(**kwargs)
    with invoice_issue_lock(data_dir):
        return _generate_invoices(**kwargs)


def _generate_invoices(
    config: InvoicingConfig,
    config_path: Path,
    accounting_path: Path,
    data_dir: Path,
    period: str | None = None,
    client_filter: str | None = None,
    entity_filter: str | None = None,
    dry_run: bool = False,
    invoice_output_dir: Path | None = None,
    db_path: Path | None = None,
    record_schedule: bool = False,
    entry_uids: list[str] | None = None,
    user_root: Path | None = None,
) -> list[dict]:
    from istota.money.db import set_invoice_schedule_generation
    from istota.money.work import (
        create_invoice,
        get_uninvoiced_entries,
        load_work_entries,
        write_transaction,
    )

    if entry_uids:
        entries = _named_entries(load_work_entries(data_dir), entry_uids)
        if client_filter:
            entries = [e for e in entries if e.client.lower() == client_filter.lower()]
    else:
        entries = get_uninvoiced_entries(data_dir, client=client_filter, period=period)
    if not entries:
        return []

    def _entity_key(entry: WorkEntry) -> str:
        return entry_entity_key(config, entry)

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

    year = str(date.today().year) if not period or entry_uids else period.split("-")[0]
    base_dir = invoice_output_dir if invoice_output_dir is not None else accounting_path / config.invoice_output
    output_dir = base_dir / year

    # Start at the higher of the stored counter and one past the highest invoice
    # ever issued. The invoices table is ground truth, so this self-heals
    # counter drift and prevents collisions across runs and clients.
    invoice_number = max(
        config.next_invoice_number,
        highest_existing_invoice_number(data_dir) + 1,
    )
    results = []

    for (client_key, entity_key), client_entity_entries in sorted(grouped.items()):
        client_config = config.clients.get(client_key)
        if not client_config:
            continue

        entity = config.companies.get(entity_key, config.company)

        logo_path = _resolve_logo(accounting_path, entity.logo)

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
                "client_key": client_key,
                "client": client_config.name,
                "group": group_name,
                "items": len(invoice.items),
                "total": round(invoice.total, 2),
                "terms": invoice.due_date.isoformat() if invoice.due_date else str(invoice.client.terms),
                "entry_uids": [e.uid for e in billable_entries],
                "entry_descriptions": [e.description for e in billable_entries],
            }
            if entity_key != config.default_entity or len(config.companies) > 1:
                summary["entity"] = entity_key

            if not dry_run:
                html = generate_invoice_html(invoice, logo_path=logo_path)
                pdf_filename = f"Invoice-{invoice_number:06d}-{invoice_date.strftime('%m_%d_%Y')}.pdf"
                pdf_path = output_dir / pdf_filename
                summary["file"] = str(pdf_path)

                # By uid: the entries were resolved before the PDF render, and
                # an entry another writer invoiced since then fails the active
                # line index rather than being billed twice.
                published = False
                try:
                    _refuse_taken_pdf_name(pdf_path, user_root)
                    generate_invoice_pdf(html, pdf_path, root=accounting_path, user_root=user_root)
                    published = True
                    with write_transaction(data_dir) as conn:
                        create_invoice(
                            conn, number=number_str, client=client_key,
                            entity=entity_key, group_name=group_name,
                            issue_date=invoice_date, total=round(invoice.total, 2),
                            entry_uids=[e.uid for e in billable_entries],
                            pdf_path=str(pdf_path),
                            income_lines=invoice_income_lines(
                                billable_entries, config.services, invoice.total,
                            ),
                        )
                        if record_schedule and client_config.schedule == "monthly":
                            set_invoice_schedule_generation(conn, client_key)
                except Exception as exc:
                    # Only a PDF this run published; a name already taken is
                    # somebody else's file.
                    if published:
                        _remove_published_pdf(pdf_path, user_root)
                    logger.error(
                        "invoice_record_failed invoice=%s client=%s error=%s published=%s",
                        number_str, client_key, exc, published,
                    )
                    if results:
                        persist_next_invoice_number(
                            invoice_number, db_path=db_path, config_path=config_path,
                        )
                    issued = ", ".join(r["invoice_number"] for r in results) or "none"
                    raise InvoiceRecordError(
                        f"could not record {number_str}: {exc} "
                        f"(issued earlier in this run: {issued})"
                    ) from exc

            results.append(summary)
            invoice_number += 1

    if not dry_run and results:
        persist_next_invoice_number(
            invoice_number, db_path=db_path, config_path=config_path,
        )

    return results


class EntrySelectionError(ValueError):
    """A named entry that cannot be billed: ``code`` is ``not_found`` or ``invoiced``."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def _named_entries(entries: list[WorkEntry], uids: list[str]) -> list[WorkEntry]:
    """The entries ``uids`` name, in display order; every one must exist and be unbilled."""
    by_uid = {e.uid: e for e in entries if e.uid}
    wanted = list(dict.fromkeys(uids))
    missing = [uid for uid in wanted if uid not in by_uid]
    if missing:
        raise EntrySelectionError(
            f"no work entry with uid {', '.join(missing)}", code="not_found",
        )
    invoiced = [f"{uid} ({by_uid[uid].invoice})" for uid in wanted if by_uid[uid].invoice]
    if invoiced:
        raise EntrySelectionError(
            f"work entry already invoiced: {', '.join(invoiced)}", code="invoiced",
        )
    chosen = set(wanted)
    return [e for e in entries if e.uid in chosen]


class InvoiceRecordError(RuntimeError):
    """An invoice's PDF was rendered but its row and lines could not be recorded."""


class InvoiceIssueBusy(RuntimeError):
    """Another issuer held the invoice-issuing lock past the deadline."""


# A generate run renders one PDF per invoice, about a second each with WeasyPrint.
ISSUE_LOCK_TIMEOUT_SECONDS = 120.0

_ISSUING = threading.local()


def _issue_lock_path(data_dir: Path) -> Path:
    from istota.money.work import db_path_for

    return db_path_for(data_dir).parent / ".invoice-issue.lock"


def _refuse_taken_pdf_name(pdf_path: Path, user_root: Path | None = None) -> None:
    """Refuse before rendering when something already holds the PDF's name.

    ``generate_invoice_pdf`` refuses the same at publish time; checking first
    skips a render that could not be kept. With ``user_root`` the question is
    asked through the no-follow walk, so it cannot probe another user's folder.
    """
    if user_root is not None:
        from istota.money.invoice_files import name_taken  # noqa: PLC0415

        taken = name_taken(user_root, pdf_path)
    else:
        taken = os.path.lexists(pdf_path)
    if taken:
        raise FileExistsError(errno.EEXIST, "an invoice PDF already has this name", pdf_path.name)


def _remove_published_pdf(pdf_path: Path, user_root: Path | None) -> None:
    """Remove a PDF this run published, for an invoice that was not recorded."""
    if user_root is not None:
        from istota.money.invoice_files import remove_file  # noqa: PLC0415

        remove_file(user_root, pdf_path)
    else:
        pdf_path.unlink(missing_ok=True)


@contextmanager
def invoice_issue_lock(data_dir: Path, *, timeout_seconds: float = ISSUE_LOCK_TIMEOUT_SECONDS):
    """Hold the store's invoice-issuing lock: one issuer at a time, across processes.

    Generation and manual creation pick the next number, render a PDF named
    for it and record the row; two at once would pick the same number. The
    anchor sits beside ``money.db``, so the web, the CLI, the skill and the
    scheduler share it. Re-entering on the same thread is a no-op, which lets
    a caller check something under the lock and then call the issuer.
    """
    anchor = _issue_lock_path(data_dir)
    key = os.path.abspath(anchor)
    held = _ISSUING.__dict__.setdefault("paths", set())
    if key in held:
        yield
        return
    anchor.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(
        anchor,
        timeout_seconds=timeout_seconds,
        poll_seconds=0.1,
        on_timeout=lambda _path: InvoiceIssueBusy(
            "another invoice is being issued; try again in a moment",
        ),
        nofollow=True,
    ):
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)


def check_scheduled_invoices(
    config: InvoicingConfig,
    db_conn,
    today: date | None = None,
    data_dir: Path | None = None,
) -> list[str]:
    """Return client keys that are due for scheduled invoice generation.

    Checks each client with ``schedule = "monthly"``.  A client is due when
    ``today >= schedule_day`` (clamped to the last day of the month) and no
    generation has been recorded for the current month in the database. When
    ``data_dir`` is available, an invoice issued this month is also sufficient;
    that keeps the scheduler safe if recording the schedule state failed after
    the invoice itself became durable.
    """
    import calendar

    from istota.money.db import get_invoice_schedule_state

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

        if data_dir is not None:
            from istota.money.work import load_work_entries

            already_invoiced = any(
                entry.client.lower() == client_key.lower()
                and entry.invoice
                and entry.invoice_date is not None
                and entry.invoice_date.year == today.year
                and entry.invoice_date.month == today.month
                for entry in load_work_entries(data_dir)
            )
            if already_invoiced:
                continue

        due.append(client_key)

    return due


def update_invoice_number(config_path: Path, new_number: int) -> None:
    """Update next_invoice_number in the invoicing config file."""
    text = config_path.read_text(encoding="utf-8")
    updated = re.sub(
        r"(next_invoice_number\s*=\s*)\d+",
        f"\\g<1>{new_number}",
        text,
    )
    from istota.storage import write_regular_file  # noqa: PLC0415

    # A link at the config's name is refused rather than written through
    # (ISSUE-723); the counter is not advanced silently either.
    if not write_regular_file(config_path, updated):
        raise OSError(f"could not write {config_path.name}")


# =============================================================================
# Invoice HTML / PDF
# =============================================================================


def _embed_logo(logo_path: Path) -> str:
    mime_type = mimetypes.guess_type(str(logo_path))[0] or "image/png"
    data = base64.b64encode(logo_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{data}"


def generate_invoice_html(invoice: Invoice, logo_path: Path | None = None) -> str:
    """Generate HTML for an invoice, suitable for PDF conversion.

    Every interpolated string is HTML-escaped. These are names, addresses and
    descriptions the user typed — into the CLI once, and into a browser form
    since — so an unescaped ``&`` in a company name is enough to garble the
    rendered PDF, and a stray tag is enough to restyle it.
    """
    esc = html.escape
    company_name = esc(invoice.company.name)
    company_address = esc(invoice.company.address)
    client_name = esc(invoice.client.name)
    client_address = esc(invoice.client.address)
    client_email = esc(invoice.client.email)
    group_name = esc(invoice.group_name) if invoice.group_name else ""
    number = esc(str(invoice.number))

    items_html = ""
    has_discounts = any(item.discount > 0 for item in invoice.items)
    for item in invoice.items:
        desc_html = (
            f"<br><span class='item-desc'>{esc(item.description)}</span>"
            if item.description else ""
        )
        discount_cell = f'<td class="right">${item.discount:,.2f}</td>' if has_discounts else ""
        items_html += f"""
        <tr>
            <td>{esc(item.display_name)}{desc_html}</td>
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
        <div style="white-space: pre-line;">{esc(invoice.company.payment_instructions)}</div>
    </div>"""

    group_label = f" - {group_name}" if group_name else ""
    discount_header = '<th class="right">Discount</th>' if has_discounts else ""
    total_colspan = 4 if has_discounts else 3
    due_text = (
        invoice.due_date.strftime("%B %d, %Y")
        if invoice.due_date else esc(str(invoice.client.terms))
    )

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Invoice {number}</title>
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
            {f'<img class="company-logo" src="{_embed_logo(logo_path)}" alt="{company_name}">' if logo_path and logo_path.exists() else f'<div class="company-name">{company_name}</div>'}
            <div style="white-space: pre-line; color: #666; margin-top: 4px;">{company_address}</div>
        </div>
        <div class="invoice-meta">
            <div class="invoice-title">INVOICE</div>
            <div class="meta-row"><span class="meta-label">Number:</span> {number}</div>
            <div class="meta-row"><span class="meta-label">Date:</span> {invoice.date.strftime("%B %d, %Y")}</div>
            <div class="meta-row"><span class="meta-label">Terms:</span> {due_text}</div>
        </div>
    </div>

    <div class="addresses">
        <div class="address-block">
            <div class="section-label">Bill To</div>
            <div><strong>{client_name}</strong></div>
            <div style="white-space: pre-line;">{client_address}</div>
            {f'<div>{client_email}</div>' if client_email else ''}
        </div>
        <div class="address-block" style="text-align: right;">
            {f'<div class="section-label">Reference</div><div>{group_name}</div>' if group_name else ''}
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


def generate_invoice_pdf(
    html: str, output_path: Path, *, root: Path | None = None, user_root: Path | None = None,
) -> None:
    """Convert invoice HTML to PDF using WeasyPrint, at a name nothing holds yet.

    With ``user_root`` the folders are walked from the user root with no link
    followed, ``data_dir``'s own components included, and an output path not
    below it is refused (ISSUE-775). Without it, and with ``root`` (the
    accounting folder) and an ``output_path`` under it, the walk starts at
    ``root`` (ISSUE-723); an output directory configured elsewhere keeps the
    leaf guard alone.

    The bytes go to a dot-prefixed staging file, which is then linked to its
    name without replacing anything: a name already taken raises
    ``FileExistsError`` and the file there is left alone. Two issuers that
    picked one number cannot overwrite each other's PDF. Only the staging
    file is removed here.
    """
    from weasyprint import HTML

    from istota.workspace.writes import open_or_create_dir_below, publish_new  # noqa: PLC0415

    pdf = HTML(string=html).write_pdf()
    if user_root is not None:
        from istota.money.invoice_files import open_dir_for_write  # noqa: PLC0415

        dir_fd = open_dir_for_write(user_root, output_path.parent)
    elif root is not None and output_path.parent.is_relative_to(root):
        dir_fd = open_or_create_dir_below(root, output_path.parent)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        dir_fd = os.open(output_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    staging = f".{output_path.stem}.{secrets.token_hex(6)}.staging"
    try:
        fd = os.open(
            staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644, dir_fd=dir_fd,
        )
        with os.fdopen(fd, "wb") as file:
            written = os.fstat(file.fileno())
            file.write(pdf)
            file.flush()
            os.fsync(file.fileno())
        publish_new(dir_fd, staging, output_path.name, written)
    finally:
        try:
            os.unlink(staging, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        os.close(dir_fd)


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
    lines.append(f'  id: "{new_txn_id()}"')
    lines.append(f"  {bank_account}  {total:.2f} {currency}")
    for account, amount in sorted(income_lines.items()):
        lines.append(f"  {account}  -{amount:.2f} {currency}")

    return "\n".join(lines)


DEFAULT_INCOME_ACCOUNT = "Income:Services"


def invoice_income_lines(
    entries: list[WorkEntry],
    services: dict[str, ServiceConfig],
    total: float,
) -> dict[str, float]:
    """The income account -> amount split an invoice is issued with.

    Each entry goes to its service's ``income_account`` (or
    ``Income:<Service>``); a ``_manual`` item and an entry whose service is
    not configured go to ``Income:Services``. Amounts are rounded to cents and
    the rounding remainder is put on the largest line, so the lines sum to
    ``round(total, 2)`` exactly: the snapshot is the invoice's total, split.
    Stored on the invoice row at issue and read back when payment is posted,
    so a later rate change never alters what a payment books.
    """
    raw: dict[str, float] = {}
    for entry in entries:
        svc = services.get(entry.service) if entry.service != "_manual" else None
        if svc is not None:
            account = svc.income_account or f"Income:{entry.service.title()}"
            amount = entry_line_item(entry, svc).amount
        else:
            account = DEFAULT_INCOME_ACCOUNT
            amount = (entry.amount or 0) if entry.service == "_manual" else 0
        raw[account] = raw.get(account, 0) + amount
    lines = {account: round(amount, 2) for account, amount in raw.items() if round(amount, 2)}
    target = round(total, 2)
    remainder = round(target - sum(lines.values()), 2)
    if remainder:
        if lines:
            largest = max(sorted(lines), key=lambda a: abs(lines[a]))
            lines[largest] = round(lines[largest] + remainder, 2)
        else:
            lines[DEFAULT_INCOME_ACCOUNT] = target
    return {account: amount for account, amount in lines.items() if amount}


def find_invoice_pdf(invoice_output_dir: Path, invoice_number: str) -> Path | None:
    """Locate the generated PDF for an invoice by its name. Returns the path or None.

    Parses the numeric part of the ``INV-<n>`` invoice number and globs
    ``Invoice-<n:06d>-*.pdf`` across the year subdirectories of
    ``invoice_output_dir``. An invoice row's own ``pdf_path`` is the first
    place to look; this is for a row that has none.
    """
    match = re.match(r"INV-(\d+)", invoice_number)
    if not match:
        return None
    num = int(match.group(1))
    if not invoice_output_dir.exists():
        return None
    pattern = f"Invoice-{num:06d}-*.pdf"
    for year_dir in sorted(invoice_output_dir.iterdir()):
        if year_dir.is_dir():
            for pdf in sorted(year_dir.glob(pattern)):
                return pdf
    return None


# =============================================================================
# Void and manual creation (shared by the CLI and the web routes)
# =============================================================================


def _void_pdf_path(pdf: Path) -> Path:
    """``Invoice-000251-10_15_2026.pdf`` → ``Invoice-000251-10_15_2026-VOID.pdf``."""
    return pdf.with_name(f"{pdf.stem}-VOID{pdf.suffix}")


def void_invoice_with_cleanup(
    data_dir: Path,
    invoice_number: str,
    *,
    invoice_output_dir: Path | None,
    reason: str = "",
    replaced_by: tuple[str, ...] | list[str] = (),
    force: bool = False,
    delete_pdf: bool = False,
    output_tree=None,
) -> dict:
    """Void an invoice, clear its DB state and set its PDF aside.

    With ``output_tree`` (a pinned ``money.invoice_files.OutputTree``) the PDF
    is looked up, renamed and removed only through that tree's descriptors,
    and ``invoice_output_dir`` is not consulted: the web route's path, where
    the tree is task-writable (ISSUE-775). Without it, by path, as the CLI
    always has.

    One transaction voids the row, releases its entries and runs
    :func:`istota.money.db.clear_invoice_state`. A kept PDF is renamed to
    ``…-VOID.pdf`` inside that transaction and renamed back if the commit
    fails, so the stored ``pdf_path`` names the file on disk. With
    ``delete_pdf`` the path is emptied and the file removed after the commit,
    since a deletion cannot be undone. A refusal is a
    :class:`istota.money.work.VoidError` raised before anything changes.
    """
    from istota.money.db import clear_invoice_state
    from istota.money.invoice_files import InvoicePdf
    from istota.money.work import get_invoice, void_invoice, write_transaction

    pdf: InvoicePdf | None = None
    renamed_to: InvoicePdf | None = None
    try:
        with write_transaction(data_dir) as conn:
            record = get_invoice(data_dir, invoice_number, conn=conn)
            if record is not None:
                if output_tree is not None:
                    number = parse_invoice_number(invoice_number)
                    if number is not None:
                        pdf = output_tree.find(number, record.pdf_path)
                else:
                    found = None
                    if record.pdf_path and Path(record.pdf_path).is_file():
                        found = Path(record.pdf_path)
                    elif invoice_output_dir is not None:
                        found = find_invoice_pdf(invoice_output_dir, invoice_number)
                    if found is not None:
                        pdf = InvoicePdf(found.parent, found.name)
            new_pdf_path: str | None = None
            if pdf is not None:
                new_pdf_path = "" if delete_pdf else str(_void_pdf_path(pdf.path))
            result = void_invoice(
                data_dir, invoice_number, reason=reason, replaced_by=replaced_by,
                force=force, pdf_path=new_pdf_path, conn=conn,
            )
            db_cleanup = clear_invoice_state(conn, invoice_number)
            if pdf is not None and not delete_pdf:
                renamed_to = pdf.rename(_void_pdf_path(pdf.path).name)
    except Exception:
        # The commit failed after the rename: put the file back where the
        # still-open invoice's row says it is.
        if renamed_to is not None and pdf is not None:
            renamed_to.rename(pdf.name)
        raise

    out = {
        "invoice_number": invoice_number,
        "entries_voided": result.released,
        "was_paid": result.was_paid,
        "voided_at": result.voided_at,
        "void_reason": result.reason,
        "replaced_by": result.replaced_by,
        "pdf_path": result.pdf_path,
    }
    if db_cleanup:
        out["db_cleanup"] = db_cleanup
    if delete_pdf:
        deleted = False
        if pdf is not None:
            try:
                pdf.unlink()
                deleted = True
            except OSError as exc:
                logger.error("invoice_void_pdf_delete_failed invoice=%s error=%s", invoice_number, exc)
        out["pdf_deleted"] = deleted
    return out


def record_past_void(
    config: InvoicingConfig,
    data_dir: Path,
    invoice_number: str,
    *,
    invoice_output_dir: Path | None,
    client_key: str,
    issue_date: date,
    total: float,
    voided_on: date,
    entity: str = "",
    reason: str = "",
    replaced_by: tuple[str, ...] | list[str] = (),
    was_paid: bool = False,
    output_tree=None,
) -> dict:
    """Record a number voided before void records existed, as a voided invoice.

    The client and entity are checked against the config, the entity
    resolved as a manual invoice's is. A PDF still on disk under the number
    is renamed ``…-VOID.pdf``, as a live void does, and renamed back if the
    write fails. ``output_tree`` confines the lookup and the rename as it
    does for :func:`void_invoice_with_cleanup`.
    """
    from istota.money.invoice_files import InvoicePdf
    from istota.money.work import record_voided_invoice, write_transaction

    client_config = config.clients.get(client_key)
    if not client_config:
        available = ", ".join(config.clients)
        raise ValueError(f"Client '{client_key}' not found. Available: {available}")
    if entity:
        if entity not in config.companies:
            available = ", ".join(config.companies)
            raise ValueError(f"Entity '{entity}' not found. Available: {available}")
    else:
        entity = resolve_entity(config, client_config=client_config).key
    if not _finite_number(total) or total <= 0:
        raise ValueError("Total must be a positive number")
    if voided_on < issue_date:
        raise ValueError("The void date is before the issue date")

    pdf: InvoicePdf | None = None
    if output_tree is not None:
        number = parse_invoice_number(invoice_number)
        if number is not None:
            pdf = output_tree.find(number)
    elif invoice_output_dir:
        found = find_invoice_pdf(invoice_output_dir, invoice_number)
        if found is not None:
            pdf = InvoicePdf(found.parent, found.name)
    target = _void_pdf_path(pdf.path) if pdf is not None else None
    renamed: InvoicePdf | None = None
    try:
        with write_transaction(data_dir) as conn:
            record = record_voided_invoice(
                data_dir, invoice_number, client=client_key, entity=entity,
                issue_date=issue_date, total=round(total, 2), voided_on=voided_on,
                reason=reason, replaced_by=replaced_by, was_paid=was_paid,
                pdf_path=str(target or ""), conn=conn,
            )
            if pdf is not None:
                renamed = pdf.rename(target.name)
    except Exception:
        if renamed is not None:
            renamed.rename(pdf.name)
        raise
    return {
        "invoice_number": record.number,
        "client": record.client,
        "entity": record.entity,
        "date": record.issue_date.isoformat(),
        "total": record.total,
        "voided_at": record.voided_at,
        "was_paid": record.was_paid,
        "void_reason": record.void_reason,
        "replaced_by": record.replaced_by,
        "pdf_path": record.pdf_path,
    }


def _finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def _manual_line(line, services: dict[str, ServiceConfig]) -> tuple[str, float | None, float | None, str]:
    """Validate one manual-invoice line: ``(service, qty, amount, description)``."""
    if not isinstance(line, dict):
        raise ValueError("Each line item must be an object")
    service = line.get("service") or ""
    description = line.get("description") or ""
    if not isinstance(service, str) or not isinstance(description, str):
        raise ValueError("A line item's service and description must be text")
    if service:
        if service not in services:
            available = ", ".join(services)
            raise ValueError(f"Service '{service}' not found. Available: {available}")
        qty = line.get("qty")
        if qty is not None and not _finite_number(qty):
            raise ValueError(f"Invalid qty for service '{service}'")
        return service, (float(qty) if qty is not None else None), None, description
    amount = line.get("amount")
    if not _finite_number(amount):
        raise ValueError(f"A manual item needs a numeric amount: {description or 'no description'}")
    return "_manual", None, float(amount), description


def create_manual_invoice(
    *,
    config: InvoicingConfig,
    data_dir: Path,
    accounting_path: Path,
    invoice_output_dir: Path,
    client_key: str,
    lines: list[dict],
    entity: str | None = None,
    db_path: Path | None = None,
    config_path: Path | None = None,
    user_root: Path | None = None,
) -> dict:
    """Create a one-off invoice from line items, render its PDF and record it.

    Each line is ``{"service", "qty", "description"}`` for a configured service,
    or ``{"description", "amount"}`` for a manual item. Every check runs before
    the PDF is rendered, and a bad line, client or entity is a ``ValueError``.
    The entries and the invoice are written in one transaction after the PDF
    exists; if that fails the PDF is deleted and :class:`InvoiceRecordError`
    is raised. The number is picked and recorded under
    :func:`invoice_issue_lock`, the same lock generation holds.
    """
    client_config = config.clients.get(client_key)
    if not client_config:
        available = ", ".join(config.clients)
        raise ValueError(f"Client '{client_key}' not found. Available: {available}")
    if entity:
        if entity not in config.companies:
            available = ", ".join(config.companies)
            raise ValueError(f"Entity '{entity}' not found. Available: {available}")
        resolved_entity = config.companies[entity]
    else:
        resolved_entity = resolve_entity(config, client_config=client_config)

    if not lines:
        raise ValueError("No line items specified. Use --service/--qty or --item")
    parsed = [_manual_line(line, config.services) for line in lines]

    with invoice_issue_lock(data_dir):
        return _issue_manual_invoice(
            config=config, data_dir=data_dir, accounting_path=accounting_path,
            invoice_output_dir=invoice_output_dir, client_key=client_key,
            client_config=client_config, resolved_entity=resolved_entity,
            entity=entity, parsed=parsed, db_path=db_path, config_path=config_path,
            user_root=user_root,
        )


def _issue_manual_invoice(
    *,
    config: InvoicingConfig,
    data_dir: Path,
    accounting_path: Path,
    invoice_output_dir: Path,
    client_key: str,
    client_config: ClientConfig,
    resolved_entity: CompanyConfig,
    entity: str | None,
    parsed: list,
    db_path: Path | None,
    config_path: Path | None,
    user_root: Path | None = None,
) -> dict:
    from istota.money.work import create_invoice, insert_entry, write_transaction

    invoice_number = max(
        config.next_invoice_number,
        highest_existing_invoice_number(data_dir) + 1,
    )
    invoice_date = date.today()
    number_str = format_invoice_number(invoice_number)

    inv_entries = [
        WorkEntry(
            date=invoice_date, client=client_key.lower(), service=service,
            qty=qty, amount=amount, description=description,
            entity=entity or "", uid=new_txn_id(),
        )
        for service, qty, amount, description in parsed
    ]
    items = build_line_items(
        [e for e in inv_entries if e.service != "_manual"], config.services,
    )
    for e in inv_entries:
        if e.service == "_manual":
            items.append(InvoiceLineItem(
                display_name=e.description or "Manual item", description="",
                quantity=1, rate=e.amount or 0, discount=0, amount=e.amount or 0,
            ))
    total = sum(i.amount for i in items)
    due_date = (
        invoice_date + timedelta(days=client_config.terms)
        if isinstance(client_config.terms, int) else None
    )
    inv = Invoice(
        number=number_str, date=invoice_date, due_date=due_date,
        client=client_config, company=resolved_entity,
        items=items, total=total, group_name="",
    )

    html_text = generate_invoice_html(
        inv, logo_path=_resolve_logo(accounting_path, resolved_entity.logo),
    )
    pdf_path = (
        invoice_output_dir / str(invoice_date.year)
        / f"Invoice-{invoice_number:06d}-{invoice_date.strftime('%m_%d_%Y')}.pdf"
    )
    published = False
    try:
        _refuse_taken_pdf_name(pdf_path, user_root)
        generate_invoice_pdf(html_text, pdf_path, root=accounting_path, user_root=user_root)
        published = True
        with write_transaction(data_dir) as conn:
            for new_entry in inv_entries:
                insert_entry(conn, new_entry)
            create_invoice(
                conn, number=number_str, client=client_key.lower(),
                entity=resolved_entity.key or entity or config.default_entity,
                group_name="", issue_date=invoice_date, total=round(total, 2),
                entry_uids=[e.uid for e in inv_entries], pdf_path=str(pdf_path),
                manual=True,
                income_lines=invoice_income_lines(inv_entries, config.services, total),
            )
    except Exception as exc:
        # Never leave a PDF behind for an invoice that was not recorded, and
        # never remove one this call did not publish.
        if published:
            _remove_published_pdf(pdf_path, user_root)
        raise InvoiceRecordError(f"could not record {number_str}: {exc}") from exc

    persist_next_invoice_number(invoice_number + 1, db_path=db_path, config_path=config_path)

    return {
        "invoice_number": number_str,
        "client_key": client_key.lower(),
        "client": client_config.name,
        "total": round(total, 2),
        "due_date": due_date.isoformat() if due_date else str(client_config.terms),
        "file": str(pdf_path),
        "entry_uids": [e.uid for e in inv_entries],
    }


_LISTED_STATUS = {"open": "outstanding", "paid": "paid", "voided": "voided"}


def invoice_summary(record, config: InvoicingConfig | None) -> dict:
    """One invoice as ``invoice list`` and ``GET /invoices`` show it.

    ``record`` is an :class:`InvoiceRecord`. The total is the stored snapshot,
    never recomputed from the entries. An open invoice shows as
    ``outstanding``.
    """
    client_config = config.clients.get(record.client) if config is not None else None
    row = {
        "invoice_number": record.number,
        "client": client_config.name if client_config else record.client,
        "client_key": record.client,
        "date": record.issue_date.isoformat(),
        "total": round(record.total, 2),
        "status": _LISTED_STATUS.get(record.status, record.status),
        "voided_at": record.voided_at,
        "void_reason": record.void_reason,
        "replaced_by": list(record.replaced_by),
    }
    if record.status == "paid" and record.paid_date:
        row["paid_date"] = record.paid_date.isoformat()
    if record.status == "voided":
        row["was_paid"] = record.was_paid
    return row
