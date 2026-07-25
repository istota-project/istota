"""File-based work entry storage using yearly TOML files.

Entries are stored in {data_dir}/invoices/work/{year}.toml files, sorted by date.
Display indices (1-based) are assigned across all loaded entries, sorted by date.

Two kinds of identity live here, and they are not interchangeable:

* ``entry.id`` — the 1-based display index, recomputed on every load. It is a
  presentation detail (the CLI's ``#N`` UX) and shifts whenever anything is
  inserted before an entry.
* ``entry.uid`` — a stable id stamped by every writer, mirroring the ``id:``
  metadata beancount transactions carry (see ``core/edit.py``). Programmatic
  callers that resolve an entry at one moment and mutate it at another — the
  web UI above all — must address it by ``uid`` via
  :func:`update_work_entry_by_uid` / :func:`remove_work_entry_by_uid`, which
  resolve *inside* the write lock. Reading never stamps a uid;
  :func:`backfill_work_ids` does, and runs from ``ensure_initialised``.

Round-trip caveat: these files are deliberately hand-editable, but a write
rewrites the whole year file from the serializer's output. Unrecognised keys
survive (they're captured into ``WorkEntry.extra``); **comments do not**, and
neither do nested tables. Preserving comments would need a comment-aware TOML
library, which this module doesn't carry.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import tomli

from istota.money.core.ids import new_txn_id
from istota.money.core.models import WorkEntry

logger = logging.getLogger(__name__)

# Fields the loader understands. Anything else in a year file is captured into
# WorkEntry.extra and written back verbatim.
_KNOWN_ENTRY_KEYS = frozenset({
    "uid", "date", "client", "service", "qty", "amount",
    "discount", "description", "entity", "invoice", "paid_date",
})

# Fields a caller may never set through the generic update path.
_PROTECTED_FIELDS = frozenset({"uid", "id", "extra"})


class WorkStoreLocked(RuntimeError):
    """Raised when the work-entry write lock can't be acquired in time."""


@dataclass
class WorkMutationResult:
    """Outcome of a uid-addressed mutation.

    ``update_work_entry``/``remove_work_entry`` return a bare bool, which
    can't tell "no such entry" from "found it, but it's invoiced" — a
    distinction the web API needs to pick 404 vs 409.

    ``status`` is one of ``ok`` / ``not_found`` / ``invoiced`` / ``conflict``
    / ``no_fields``. ``entry`` carries the current server-side entry when one
    was resolved (so a conflict can show the caller what it actually looks like).
    """
    status: str
    entry: WorkEntry | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _work_dir(data_dir: Path) -> Path:
    d = data_dir / "invoices" / "work"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _year_file(work_dir: Path, year: int) -> Path:
    return work_dir / f"{year}.toml"


@contextmanager
def _work_lock(data_dir: Path, *, timeout_seconds: float = 10.0):
    """Serialize read-modify-write cycles on the work-entry store.

    The web process (mark-paid / mark-pending) and the scheduler/CLI
    (invoice generate, invoice paid, add) both rewrite these yearly TOML
    files. Without a lock, two concurrent load→modify→save cycles are
    last-writer-wins on the whole file and one mutation is silently lost.

    Holds an exclusive flock on ``{work_dir}/.work.lock`` (a sibling anchor
    file, never the data files themselves) for the duration of the context.
    Readers don't take the lock; atomic per-file writes (see ``_save_year``)
    keep each file individually consistent for them. Linux + macOS only.
    """
    lock_path = _work_dir(data_dir) / ".work.lock"
    fd = open(lock_path, "a+")
    try:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise WorkStoreLocked(str(lock_path)) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        fd.close()


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
            uid=raw.get("uid", ""),
            extra={k: v for k, v in raw.items() if k not in _KNOWN_ENTRY_KEYS},
        ))
    return entries


def _format_num(n: float) -> str:
    if n == int(n):
        return str(int(n))
    return str(n)


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_toml_value(value) -> str | None:
    """Render a value from ``WorkEntry.extra`` back to TOML, or None if we can't.

    Deliberately narrow: the serializer is hand-rolled string building, so a
    nested table or an arbitrary object has no place to go. Returning None
    drops the key rather than writing something that won't parse back — a
    hand edit must not be able to poison every subsequent save.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_num(value)
    if isinstance(value, str):
        return f'"{_escape(value)}"'
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        rendered = [_format_toml_value(v) for v in value]
        if any(r is None for r in rendered):
            return None
        return "[" + ", ".join(rendered) + "]"
    return None


def _serialize_entry(entry: WorkEntry) -> str:
    lines = ["[[entries]]"]
    if entry.uid:
        lines.append(f'uid = "{_escape(entry.uid)}"')
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
    for key in sorted(entry.extra):
        rendered = _format_toml_value(entry.extra[key])
        if rendered is None:
            logger.warning(
                "work_entry_extra_key_dropped key=%s type=%s — "
                "the work serializer only writes scalars and flat lists",
                key, type(entry.extra[key]).__name__,
            )
            continue
        lines.append(f"{key} = {rendered}")
    return "\n".join(lines)


def entry_etag(entry: WorkEntry) -> str:
    """Content hash of an entry, for optimistic-concurrency checks.

    Derived from the serialized form, so it covers every persisted field
    (``extra`` included) and nothing transient — notably not the display
    index, which shifts whenever an earlier entry is inserted. Never stored.
    """
    return hashlib.sha256(_serialize_entry(entry).encode()).hexdigest()[:12]


def _save_year(path: Path, entries: list[WorkEntry]) -> None:
    if not entries:
        if path.exists():
            path.unlink()
        return
    entries.sort(key=lambda e: e.date)
    blocks = [_serialize_entry(e) for e in entries]
    text = "\n\n".join(blocks) + "\n"
    # Atomic write: a crash (or a half-written FUSE/rclone flush) mid-write
    # must not leave a truncated or partial year file. Write to a temp file
    # in the same dir, then os.replace (atomic rename on the same fs).
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


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
    # Any entry touched by a write acquires a uid as a side effect, so the
    # store converges on full coverage even between explicit backfills.
    for entry in entries:
        if not entry.uid:
            entry.uid = new_txn_id()
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
    uid: str = "",
) -> int:
    """Append entry to correct year file, return display index.

    Pass ``uid`` to choose the stable id up front — a caller that needs to
    find the entry it just created (the web API) generates one, passes it,
    and looks the entry back up by it, since the returned display index is
    already stale the moment another writer inserts something earlier.
    """
    d = _parse_date(entry_date)
    new_entry = WorkEntry(
        date=d, client=client.lower(), service=service,
        qty=qty, amount=amount, discount=discount,
        description=description, entity=entity, invoice=invoice,
        uid=uid or new_txn_id(),
    )
    with _work_lock(data_dir):
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


def _apply_fields(entry: WorkEntry, fields: dict) -> None:
    """Assign updatable fields onto an entry, coercing date/client as the CLI does."""
    for key, value in fields.items():
        if key in _PROTECTED_FIELDS:
            continue
        if key == "date" and isinstance(value, str):
            value = _parse_date(value)
        if key == "client" and isinstance(value, str):
            value = value.lower()
        if hasattr(entry, key):
            setattr(entry, key, value)


def update_work_entry(data_dir: Path, index: int, **fields) -> bool:
    """Update fields on entry at 1-based display index. Only if uninvoiced.

    Index-addressed, so only safe when the caller read the list and acts on it
    immediately (the CLI). Anything holding a reference across time should use
    :func:`update_work_entry_by_uid`.
    """
    if not fields:
        return False
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        if index < 1 or index > len(entries):
            return False
        entry = entries[index - 1]
        if entry.invoice:
            return False
        _apply_fields(entry, fields)
        _save_entries(data_dir, entries)
        return True


def _find_by_uid(entries: list[WorkEntry], uid: str) -> WorkEntry | None:
    if not uid:
        # An un-backfilled entry carries uid == "" — it must not be addressable
        # by an empty uid, or one bad request would hit an arbitrary row.
        return None
    for entry in entries:
        if entry.uid == uid:
            return entry
    return None


def update_work_entry_by_uid(
    data_dir: Path,
    uid: str,
    *,
    expect_etag: str | None = None,
    **fields,
) -> WorkMutationResult:
    """Update an entry addressed by its stable ``uid``. Only if uninvoiced.

    Resolve-and-mutate happens inside the write lock, so a concurrent insert
    can't make this land on a different entry. ``expect_etag`` adds an
    optimistic-concurrency check: a mismatch means the entry changed since the
    caller read it, and nothing is written.
    """
    if not fields:
        return WorkMutationResult("no_fields")
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        entry = _find_by_uid(entries, uid)
        if entry is None:
            return WorkMutationResult("not_found")
        if entry.invoice:
            return WorkMutationResult("invoiced", entry)
        if expect_etag and entry_etag(entry) != expect_etag:
            return WorkMutationResult("conflict", entry)
        _apply_fields(entry, fields)
        _save_entries(data_dir, entries)
        # Re-read so the caller gets a correct display index (a date change
        # may have moved the entry) rather than the pre-save one.
        fresh = _find_by_uid(load_work_entries(data_dir), uid)
        return WorkMutationResult("ok", fresh or entry)


def remove_work_entry_by_uid(
    data_dir: Path,
    uid: str,
    *,
    expect_etag: str | None = None,
) -> WorkMutationResult:
    """Remove an entry addressed by its stable ``uid``. Only if uninvoiced."""
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        entry = _find_by_uid(entries, uid)
        if entry is None:
            return WorkMutationResult("not_found")
        if entry.invoice:
            return WorkMutationResult("invoiced", entry)
        if expect_etag and entry_etag(entry) != expect_etag:
            return WorkMutationResult("conflict", entry)
        entries.remove(entry)
        _save_entries(data_dir, entries)
        return WorkMutationResult("ok", entry)


def backfill_work_ids(data_dir: Path) -> int:
    """Stamp a ``uid`` on every entry that lacks one. Returns the count stamped.

    Idempotent, and safe to run from ``ensure_initialised`` on every entry
    point: it only writes when something is actually missing an id, so a
    fully-backfilled store costs one read. Unlike the ledger's equivalent
    this carries no sentinel — a hand-added entry with no ``uid`` can appear
    at any time, and re-running is how that self-heals.
    """
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        missing = [e for e in entries if not e.uid]
        if not missing:
            return 0
        _save_entries(data_dir, entries)  # stamps every uid-less entry
        return len(missing)


def remove_work_entry(data_dir: Path, index: int) -> bool:
    """Remove entry at 1-based display index. Only if uninvoiced."""
    with _work_lock(data_dir):
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
    with _work_lock(data_dir):
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
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        count = 0
        for entry in entries:
            if entry.invoice == invoice_number and entry.paid_date is None:
                entry.paid_date = paid_date
                count += 1
        if count:
            _save_entries(data_dir, entries)
        return count


def clear_invoice_payment(data_dir: Path, invoice_number: str) -> int:
    """Clear paid_date on all entries for an invoice, keeping the invoice number.

    The inverse of :func:`record_invoice_payment` — marks a paid invoice
    pending again without un-invoicing it (unlike :func:`void_invoice`).
    Returns the number of entries modified.
    """
    with _work_lock(data_dir):
        entries = load_work_entries(data_dir)
        count = 0
        for entry in entries:
            if entry.invoice == invoice_number and entry.paid_date is not None:
                entry.paid_date = None
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
    with _work_lock(data_dir):
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
