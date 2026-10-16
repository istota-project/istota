"""Work entries and invoices out of ``money.db``, as CSV or JSON.

Read-only: nothing here writes to the store. The CLI (``money work export``),
the money skill and the web download all call :func:`collect` and
:func:`render`, so the three cannot disagree about what an export holds.

* **CSV** is one row per work entry, for a spreadsheet. The invoice columns
  name the entry's active invoice; ``voided_invoices`` lists the voided ones
  that also covered it, since a row has room for only one invoice.
* **JSON** is the whole record: every entry, and every invoice with its stored
  total, income split, status, void reason, replacements and every entry it
  covered, voided lines included. An invoice's lines are active exactly when
  it is not voided, so the document is enough to rebuild the store. An
  entry's ``extra`` keeps the store's own tags for dates and times
  (``{"$date": "2026-03-02"}``), so they round-trip.

With ``year``, the entries are those dated in that year, and the invoices are
those covering any of them plus entry-less void records issued that year. An
invoice that also covered an entry from another year still lists its uid.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path

from istota.lib.timestamps import iso_now
from istota.money.core.models import InvoiceRecord, WorkEntry
from istota.money.work import read_snapshot, tagged_extra

EXPORT_FORMATS = ("csv", "json")
JSON_FORMAT = "istota.money.work"
JSON_VERSION = 1

CSV_COLUMNS = (
    "uid", "date", "client", "service", "qty", "amount", "discount",
    "description", "entity", "separate",
    "invoice", "invoice_status", "invoice_date", "paid_date", "voided_invoices",
)

# A spreadsheet reads a cell starting with one of these as a formula. A
# description is free text, sometimes written by the model from an email.
_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")

MEDIA_TYPES = {"csv": "text/csv; charset=utf-8", "json": "application/json"}


@dataclass
class WorkExport:
    data_dir: Path
    entries: list[WorkEntry]
    invoices: list[InvoiceRecord]
    year: int | None = None


def collect(data_dir: Path, *, year: int | None = None) -> WorkExport:
    """Read the store and keep what an export for ``year`` (or everything) holds."""
    entries, invoices = read_snapshot(data_dir)
    if year is None:
        return WorkExport(data_dir, entries, invoices)
    entries = [e for e in entries if e.date.year == year]
    uids = {e.uid for e in entries}
    invoices = [
        inv for inv in invoices
        if uids.intersection(inv.entry_uids)
        or (not inv.entry_uids and inv.issue_date.year == year)
    ]
    return WorkExport(data_dir, entries, invoices, year)


def render(export: WorkExport, fmt: str) -> str:
    if fmt == "csv":
        return _csv(export)
    if fmt == "json":
        return json.dumps(_json(export), indent=2, ensure_ascii=False) + "\n"
    raise ValueError(f"unknown export format: {fmt}")


def filename(fmt: str, year: int | None = None) -> str:
    return f"work-{year}.{fmt}" if year is not None else f"work.{fmt}"


def _num(value: float | None) -> str:
    if value is None:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _text(value: str) -> str:
    if value.startswith(_FORMULA_LEADS):
        return "'" + value
    return value


def _csv(export: WorkExport) -> str:
    status = {inv.number: inv.status for inv in export.invoices}
    voided: dict[str, list[str]] = {}
    for inv in export.invoices:
        if inv.status == "voided":
            for uid in inv.entry_uids:
                voided.setdefault(uid, []).append(inv.number)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for e in export.entries:
        writer.writerow([
            e.uid,
            e.date.isoformat(),
            _text(e.client),
            _text(e.service),
            _num(e.qty),
            _num(e.amount),
            _num(e.discount),
            _text(e.description),
            _text(e.entity),
            "true" if e.separate else "false",
            _text(e.invoice),
            status.get(e.invoice, "") if e.invoice else "",
            e.invoice_date.isoformat() if e.invoice_date else "",
            e.paid_date.isoformat() if e.paid_date else "",
            "; ".join(_text(n) for n in voided.get(e.uid, [])),
        ])
    return buf.getvalue()


def _pdf(path: str, data_dir: Path) -> str:
    """The PDF's path under the data dir, or its bare name: never a host path."""
    if not path:
        return ""
    pdf = Path(path)
    try:
        return pdf.relative_to(data_dir).as_posix()
    except ValueError:
        return pdf.name


def _json(export: WorkExport) -> dict:
    return {
        "format": JSON_FORMAT,
        "version": JSON_VERSION,
        "exported_at": iso_now(),
        "year": export.year,
        "entries": [
            {
                "uid": e.uid,
                "date": e.date.isoformat(),
                "client": e.client,
                "service": e.service,
                "qty": e.qty,
                "amount": e.amount,
                "discount": e.discount,
                "description": e.description,
                "entity": e.entity,
                "separate": e.separate,
                "invoice": e.invoice or None,
                "extra": tagged_extra(e.extra),
            }
            for e in export.entries
        ],
        "invoices": [
            {
                "number": inv.number,
                "client": inv.client,
                "entity": inv.entity,
                "group_name": inv.group_name,
                "issue_date": inv.issue_date.isoformat(),
                "total": inv.total,
                "income_lines": inv.income_lines,
                "status": inv.status,
                "paid_date": inv.paid_date.isoformat() if inv.paid_date else None,
                "voided_at": inv.voided_at,
                "was_paid": inv.was_paid,
                "void_reason": inv.void_reason,
                "replaced_by": inv.replaced_by,
                "manual": inv.manual,
                "pdf": _pdf(inv.pdf_path, export.data_dir),
                "created_at": inv.created_at,
                "entry_uids": inv.entry_uids,
            }
            for inv in export.invoices
        ],
    }
