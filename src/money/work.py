"""File-based work entry storage using yearly TOML files.

Entries are stored in {data_dir}/invoices/work/{year}.toml files, sorted by date.
Display indices (1-based) are assigned across all loaded entries, sorted by date.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import tomli

from money.core.models import WorkEntry


def _work_dir(data_dir: Path) -> Path:
    d = data_dir / "invoices" / "work"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _year_file(work_dir: Path, year: int) -> Path:
    return work_dir / f"{year}.toml"


def _parse_date(s: str) -> date:
    parts = s.split("-")
    return date(int(parts[0]), int(parts[1]), int(parts[2]))


def _load_year(path: Path) -> list[WorkEntry]:
    if not path.exists():
        return []
    data = tomli.loads(path.read_text())
    entries = []
    for raw in data.get("entries", []):
        entries.append(WorkEntry(
            date=raw["date"],
            client=raw["client"],
            service=raw["service"],
            qty=raw.get("qty"),
            amount=raw.get("amount"),
            discount=raw.get("discount", 0),
            description=raw.get("description", ""),
            entity=raw.get("entity", ""),
            invoice=raw.get("invoice", ""),
            paid_date=raw.get("paid_date"),
        ))
    return entries


def _format_num(n: float) -> str:
    if n == int(n):
        return str(int(n))
    return str(n)


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _serialize_entry(entry: WorkEntry) -> str:
    lines = ["[[entries]]"]
    lines.append(f"date = {entry.date.isoformat()}")
    lines.append(f'client = "{_escape(entry.client)}"')
    lines.append(f'service = "{_escape(entry.service)}"')
    if entry.qty is not None:
        lines.append(f"qty = {_format_num(entry.qty)}")
    if entry.amount is not None:
        lines.append(f"amount = {_format_num(entry.amount)}")
    if entry.discount:
        lines.append(f"discount = {_format_num(entry.discount)}")
    if entry.description:
        lines.append(f'description = "{_escape(entry.description)}"')
    if entry.entity:
        lines.append(f'entity = "{_escape(entry.entity)}"')
    if entry.invoice:
        lines.append(f'invoice = "{_escape(entry.invoice)}"')
    if entry.paid_date is not None:
        lines.append(f"paid_date = {entry.paid_date.isoformat()}")
    return "\n".join(lines)


def _save_year(path: Path, entries: list[WorkEntry]) -> None:
    if not entries:
        if path.exists():
            path.unlink()
        return
    entries.sort(key=lambda e: e.date)
    blocks = [_serialize_entry(e) for e in entries]
    path.write_text("\n\n".join(blocks) + "\n")


def _load_all(data_dir: Path) -> list[WorkEntry]:
    wd = _work_dir(data_dir)
    all_entries = []
    for f in sorted(wd.glob("*.toml")):
        try:
            int(f.stem)
        except ValueError:
            continue
        all_entries.extend(_load_year(f))
    all_entries.sort(key=lambda e: e.date)
    return all_entries


def _save_entries(data_dir: Path, entries: list[WorkEntry]) -> None:
    wd = _work_dir(data_dir)
    by_year: dict[int, list[WorkEntry]] = {}
    for entry in entries:
        by_year.setdefault(entry.date.year, []).append(entry)
    existing_years: set[int] = set()
    for f in wd.glob("*.toml"):
        try:
            existing_years.add(int(f.stem))
        except ValueError:
            pass
    for year in existing_years | by_year.keys():
        _save_year(_year_file(wd, year), by_year.get(year, []))


def load_work_entries(data_dir: Path) -> list[WorkEntry]:
    """Load all entries from all year files, sorted by date.
    Sets entry.id to 1-based display index."""
    entries = _load_all(data_dir)
    for i, entry in enumerate(entries, 1):
        entry.id = i
    return entries


def add_work_entry(
    data_dir: Path,
    entry_date: str,
    client: str,
    service: str,
    qty: float | None = None,
    amount: float | None = None,
    discount: float = 0,
    description: str = "",
    entity: str = "",
    invoice: str = "",
) -> int:
    """Append entry to correct year file, return display index."""
    d = _parse_date(entry_date)
    new_entry = WorkEntry(
        date=d, client=client.lower(), service=service,
        qty=qty, amount=amount, discount=discount,
        description=description, entity=entity, invoice=invoice,
    )
    entries = _load_all(data_dir)
    entries.append(new_entry)
    entries.sort(key=lambda e: e.date)
    _save_entries(data_dir, entries)
    for i, e in enumerate(entries, 1):
        if e is new_entry:
            return i
    return len(entries)


def list_work_entries(
    data_dir: Path,
    client: str | None = None,
    invoiced: bool | None = None,
    period: str | None = None,
) -> list[WorkEntry]:
    """Filter and return entries."""
    entries = load_work_entries(data_dir)
    if client:
        client_lower = client.lower()
        entries = [e for e in entries if e.client.lower() == client_lower]
    if invoiced is True:
        entries = [e for e in entries if e.invoice]
    elif invoiced is False:
        entries = [e for e in entries if not e.invoice]
    if period:
        entries = [e for e in entries if e.date.isoformat().startswith(period)]
    return entries


def update_work_entry(data_dir: Path, index: int, **fields) -> bool:
    """Update fields on entry at 1-based display index. Only if uninvoiced."""
    if not fields:
        return False
    entries = load_work_entries(data_dir)
    if index < 1 or index > len(entries):
        return False
    entry = entries[index - 1]
    if entry.invoice:
        return False
    for key, value in fields.items():
        if key == "date" and isinstance(value, str):
            value = _parse_date(value)
        if key == "client" and isinstance(value, str):
            value = value.lower()
        if hasattr(entry, key):
            setattr(entry, key, value)
    _save_entries(data_dir, entries)
    return True


def remove_work_entry(data_dir: Path, index: int) -> bool:
    """Remove entry at 1-based display index. Only if uninvoiced."""
    entries = load_work_entries(data_dir)
    if index < 1 or index > len(entries):
        return False
    entry = entries[index - 1]
    if entry.invoice:
        return False
    entries.pop(index - 1)
    _save_entries(data_dir, entries)
    return True


def get_uninvoiced_entries(
    data_dir: Path,
    client: str | None = None,
    period: str | None = None,
) -> list[WorkEntry]:
    """Get entries where invoice is not set."""
    entries = load_work_entries(data_dir)
    result = [e for e in entries if not e.invoice]
    if client:
        client_lower = client.lower()
        result = [e for e in result if e.client.lower() == client_lower]
    if period:
        year, month = map(int, period.split("-"))
        if month == 12:
            upper = date(year + 1, 1, 1)
        else:
            upper = date(year, month + 1, 1)
        result = [e for e in result if e.date < upper]
    return result


def assign_invoice_number(
    data_dir: Path,
    indices: list[int],
    invoice_number: str,
) -> int:
    """Stamp invoice number on entries at given display indices. Returns count."""
    if not indices:
        return 0
    entries = load_work_entries(data_dir)
    count = 0
    for idx in indices:
        if idx < 1 or idx > len(entries):
            continue
        entry = entries[idx - 1]
        if entry.invoice:
            continue
        entry.invoice = invoice_number
        count += 1
    if count:
        _save_entries(data_dir, entries)
    return count


def record_invoice_payment(
    data_dir: Path,
    invoice_number: str,
    paid_date: str | date,
) -> int:
    """Set paid_date on all entries for an invoice. Returns count."""
    if isinstance(paid_date, str):
        paid_date = _parse_date(paid_date)
    entries = load_work_entries(data_dir)
    count = 0
    for entry in entries:
        if entry.invoice == invoice_number and entry.paid_date is None:
            entry.paid_date = paid_date
            count += 1
    if count:
        _save_entries(data_dir, entries)
    return count


def get_entries_for_invoice(data_dir: Path, invoice_number: str) -> list[WorkEntry]:
    """Get all entries assigned to an invoice."""
    return [e for e in load_work_entries(data_dir) if e.invoice == invoice_number]


def void_invoice(data_dir: Path, invoice_number: str) -> int:
    """Clear invoice and paid_date fields on all entries for an invoice.

    Returns the number of entries modified.
    """
    entries = load_work_entries(data_dir)
    count = 0
    for entry in entries:
        if entry.invoice == invoice_number:
            entry.invoice = ""
            entry.paid_date = None
            count += 1
    if count:
        _save_entries(data_dir, entries)
    return count


def get_invoice_numbers(data_dir: Path) -> list[str]:
    """Get distinct invoice numbers, sorted."""
    entries = load_work_entries(data_dir)
    return sorted(set(e.invoice for e in entries if e.invoice))
