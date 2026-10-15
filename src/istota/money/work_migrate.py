"""The one-time import of the TOML work log into ``money.db``.

Work entries used to live in ``{data_dir}/invoices/work/{year}.toml``. This
module is the only code that still reads those files: it parses them with the
loader that used to back ``work.py``, refuses anything it cannot carry over
faithfully, writes entries, invoices and invoice lines in one transaction,
re-reads what it wrote and compares it field by field with what it parsed, and
only then records the ``work_store`` key that makes ``work.py`` treat the
database as the store. Any refusal or difference rolls the whole transaction
back, so a blocked import leaves the database exactly as it found it.

The year files are never written by the import. After it commits, each one is
renamed ``{year}.toml.migrated`` in place (:func:`set_aside_year_files`), and
every later store open renames any ``{year}.toml`` that is still there or has
come back, without reading it. The one writer left here is
:func:`backfill_legacy_uids`, which stamps a uid on hand-added entries before
the import has run (the import refuses an entry without one).
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import tomli

from istota.lib.atomic_write import write_text_atomic
from istota.money.core.models import InvoiceRecord, WorkEntry

logger = logging.getLogger(__name__)

WORK_STORE_KEY = "work_store"

# Fields the loader understands. Anything else in a year file is captured into
# WorkEntry.extra.
_KNOWN_ENTRY_KEYS = frozenset({
    "uid", "date", "client", "service", "qty", "amount",
    "discount", "description", "entity", "invoice", "invoice_date", "paid_date",
})


# =============================================================================
# Legacy loader
# =============================================================================


def _parse_date(s: str) -> date:
    parts = s.split("-")
    return date(int(parts[0]), int(parts[1]), int(parts[2]))


def _coerce_date(value) -> date | None:
    """Read a date field written as a TOML date, datetime, or quoted string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return _parse_date(value.strip())
        except (ValueError, IndexError):
            return None
    return None


def _coerce_number(value) -> float | int | None:
    """Read a numeric field, tolerating a quoted number. None if unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _coerce_text(value) -> str | None:
    """Read a string field, stringifying a bare scalar. None if unusable."""
    from istota.money.work import _format_num  # noqa: PLC0415

    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _format_num(value)
    return None


def load_year(path: Path) -> tuple[list[WorkEntry], list[str]]:
    """Load one year file. Returns the entries and the rows it had to skip.

    Coercible values are coerced (a quoted date or number). A row missing a
    usable date, client or service is skipped and named in the second list;
    the import refuses a year with any skipped row, since importing the rest
    would drop that row for good.
    """
    data = tomli.loads(path.read_text(encoding="utf-8"))
    entries = []
    skipped: list[str] = []
    for position, raw in enumerate(data.get("entries", []), 1):
        entry_date = _coerce_date(raw.get("date"))
        client = _coerce_text(raw.get("client"))
        service = _coerce_text(raw.get("service"))
        if entry_date is None or client is None or service is None:
            skipped.append(f"entry #{position} (uid={raw.get('uid') or '?'})")
            continue
        # An unusable optional number is dropped rather than skipping the row:
        # a $0 line is something a human notices, a vanished entry is not.
        qty = _coerce_number(raw["qty"]) if raw.get("qty") is not None else None
        amount = _coerce_number(raw["amount"]) if raw.get("amount") is not None else None
        discount = _coerce_number(raw.get("discount", 0))
        entries.append(WorkEntry(
            date=entry_date,
            client=client,
            service=service,
            qty=qty,
            amount=amount,
            discount=0 if discount is None else discount,
            description=_coerce_text(raw.get("description", "")) or "",
            entity=_coerce_text(raw.get("entity", "")) or "",
            invoice=_coerce_text(raw.get("invoice", "")) or "",
            invoice_date=_coerce_date(raw.get("invoice_date")),
            paid_date=_coerce_date(raw.get("paid_date")),
            uid=_coerce_text(raw.get("uid", "")) or "",
            extra={k: v for k, v in raw.items() if k not in _KNOWN_ENTRY_KEYS},
        ))
    return entries, skipped


def legacy_work_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "invoices" / "work"


def year_files(data_dir: Path) -> list[Path]:
    """The ``{year}.toml`` files under the legacy work dir, by year."""
    wd = legacy_work_dir(data_dir)
    if not wd.is_dir():
        return []
    files = []
    for f in sorted(wd.glob("*.toml")):
        try:
            int(f.stem)
        except ValueError:
            continue
        files.append(f)
    return files


@dataclass
class LegacyLog:
    entries: list[WorkEntry]
    quarantined: dict[str, str]
    source_hashes: dict[str, str]


def load_legacy(data_dir: Path) -> LegacyLog:
    """Parse every year file, in the order the old store displayed them."""
    entries: list[WorkEntry] = []
    quarantined: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for path in year_files(data_dir):
        raw = path.read_bytes()
        hashes[path.stem] = hashlib.sha256(raw).hexdigest()
        year_entries, skipped = load_year(path)
        if skipped:
            quarantined[path.name] = ", ".join(skipped)
        entries.extend(year_entries)
    # Stable sort, so same-date entries keep their file order: that order is
    # the display index users knew, and the import preserves it.
    entries.sort(key=lambda e: e.date)
    return LegacyLog(entries, quarantined, hashes)


MIGRATED_SUFFIX = ".migrated"


def set_aside_files(data_dir: Path) -> list[Path]:
    """The ``{year}.toml.migrated*`` files an earlier import renamed aside."""
    wd = legacy_work_dir(data_dir)
    if not wd.is_dir():
        return []
    found = []
    for f in sorted(wd.glob(f"*.toml{MIGRATED_SUFFIX}*")):
        try:
            int(f.name.split(".", 1)[0])
        except ValueError:
            continue
        found.append(f)
    return found


def _set_aside_target(path: Path) -> Path:
    """``{year}.toml.migrated``, or a UTC-stamped name beside it if that exists."""
    target = path.with_name(path.name + MIGRATED_SUFFIX)
    if not target.exists():
        return target
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}{MIGRATED_SUFFIX}-{stamp}")
    counter = 2
    while target.exists():
        target = path.with_name(f"{path.name}{MIGRATED_SUFFIX}-{stamp}-{counter}")
        counter += 1
    return target


def set_aside_year_files(data_dir: Path, source_hashes: dict[str, str]) -> list[Path]:
    """Rename every ``{year}.toml`` to its ``.migrated`` name. Returns the new paths.

    Called once the import has committed, and on every later store open. A
    file whose bytes match the hash the import recorded for its year is the
    imported file (a rename that failed before is being retried); any other
    file has come back since, and is moved aside with a warning, never read
    into the store. A failed rename is logged and left for the next open.
    """
    moved = []
    try:
        present = year_files(data_dir)
    except OSError as exc:
        logger.error("work_year_file_rename_failed dir=%s error=%s", legacy_work_dir(data_dir), exc)
        return moved
    for path in present:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            target = _set_aside_target(path)
            path.rename(target)
        except FileNotFoundError:
            continue  # another process moved it first
        except OSError as exc:
            logger.error("work_year_file_rename_failed path=%s error=%s", path, exc)
            continue
        if source_hashes.get(path.stem) == digest:
            logger.info("work_year_file_set_aside path=%s target=%s", path, target.name)
        else:
            logger.warning(
                "work_year_file_reappeared path=%s target=%s: not imported; "
                "the work log is in money.db",
                path, target.name,
            )
        moved.append(target)
    return moved


@contextmanager
def _legacy_lock(data_dir: Path, *, timeout_seconds: float = 10.0):
    """The flock the old store's writers held, for the files' last writers.

    Taken only when the work dir exists, so reading never creates it.
    """
    from istota.money.work import _work_lock  # noqa: PLC0415

    if not legacy_work_dir(data_dir).is_dir():
        yield
        return
    with _work_lock(data_dir, timeout_seconds=timeout_seconds):
        yield


def backfill_legacy_uids(data_dir: Path, *, timeout_seconds: float = 10.0) -> int:
    """Stamp a uid on every year-file entry that lacks one. Returns the count.

    Only meaningful before the import: the import refuses an entry with no
    uid, and this is how hand-added entries get one. The check is lock-free,
    so the common no-op case costs one read; a year file with a skipped row is
    left alone, since rewriting it would drop that row.
    """
    from istota.money.core.ids import new_txn_id  # noqa: PLC0415
    from istota.money.work import _serialize_entry  # noqa: PLC0415

    if not any(not e.uid for e in load_legacy(data_dir).entries):
        return 0
    stamped = 0
    with _legacy_lock(data_dir, timeout_seconds=timeout_seconds):
        for path in year_files(data_dir):
            entries, skipped = load_year(path)
            missing = [e for e in entries if not e.uid]
            if not missing or skipped:
                continue
            for entry in missing:
                entry.uid = new_txn_id()
            entries.sort(key=lambda e: e.date)
            text = "\n\n".join(_serialize_entry(e) for e in entries) + "\n"
            tomli.loads(text)  # never publish a file that won't parse back
            write_text_atomic(path, text)
            stamped += len(missing)
    return stamped


# =============================================================================
# The import
# =============================================================================


@dataclass
class MigrationReport:
    """What :func:`migrate_work_log` did.

    ``status`` is ``migrated``, ``already`` (the key was set; nothing ran),
    ``dry_run`` (verified, then rolled back), ``refused`` (a precondition
    failed; nothing was written) or ``failed`` (a write or the verification
    failed; rolled back). ``reason`` names the refusal or the difference.
    """
    status: str
    reason: str = ""
    entries: int = 0
    invoices: int = 0
    years: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("migrated", "already", "dry_run")

    def as_dict(self) -> dict:
        return {
            "status": self.status, "reason": self.reason,
            "entries": self.entries, "invoices": self.invoices, "years": self.years,
        }


class _Refused(Exception):
    pass


def _refusals(log: LegacyLog) -> str | None:
    if log.quarantined:
        names = ", ".join(f"{name} ({why})" for name, why in sorted(log.quarantined.items()))
        return f"unreadable entries in {names}; fix the rows by hand"
    missing = [e for e in log.entries if not e.uid]
    if missing:
        return (
            f"{len(missing)} entr{'y has' if len(missing) == 1 else 'ies have'} no uid; "
            "run `istota money work backfill-ids -u USER` first"
        )
    seen: set[str] = set()
    for entry in log.entries:
        if entry.uid in seen:
            return f"uid {entry.uid} appears on more than one entry"
        seen.add(entry.uid)
    unbilled_paid = [e.uid for e in log.entries if e.paid_date is not None and not e.invoice]
    if unbilled_paid:
        return (
            f"paid_date set but no invoice on {', '.join(unbilled_paid)}; "
            "the database keeps a paid date only on an invoice, so remove the "
            "paid_date or add the invoice number to those rows by hand"
        )
    paid: dict[str, set] = {}
    for entry in log.entries:
        if entry.invoice:
            paid.setdefault(entry.invoice, set()).add(entry.paid_date)
    for number, dates in sorted(paid.items()):
        if len(dates) > 1:
            shown = ", ".join(sorted(d.isoformat() if d else "unpaid" for d in dates))
            return f"entries on {number} disagree on paid_date ({shown})"
    return None


def _restore_refusal(data_dir: Path) -> str | None:
    """Why a database with no import record must not import again, or None.

    A ``{year}.toml.migrated`` with no ``{year}.toml`` beside it means an
    import already ran and took that year. With the key gone, ``money.db`` was
    lost or restored from a snapshot taken before the import, and importing
    what is left would start the work log over without that year.
    """
    # A stamped `.migrated-<time>` name is a copy that came back after the
    # import, so the plain name alone is the evidence.
    aside = [
        p for p in set_aside_files(data_dir)
        if p.name.endswith(".toml" + MIGRATED_SUFFIX)
        and not p.with_name(p.name.removesuffix(MIGRATED_SUFFIX)).exists()
    ]
    if not aside:
        return None
    return (
        f"money.db has no record of the work log import, but {aside[0].name} "
        "shows it already ran; money.db was lost or restored from a backup "
        "taken before the import. Rename each {year}.toml.migrated in "
        "invoices/work/ back to {year}.toml and run "
        "`istota money work migrate -u USER`"
    )


def _load_config(db_path: Path):
    from istota.money import config_store  # noqa: PLC0415

    if not config_store.has_invoicing_data(db_path):
        return None
    return config_store.load_invoicing(db_path)


def plan_invoices(
    entries: list[WorkEntry], config, data_dir: Path,
) -> list[InvoiceRecord]:
    """The ``invoices`` rows the stamps on ``entries`` describe."""
    from istota.money.core.invoicing import (  # noqa: PLC0415
        entry_entity_key, find_invoice_pdf, invoice_income_lines, invoice_total,
    )
    from istota.money.work import invoice_issue_date  # noqa: PLC0415

    by_number: dict[str, list[WorkEntry]] = {}
    for entry in entries:
        if entry.invoice:
            by_number.setdefault(entry.invoice, []).append(entry)

    output_dir = None
    if config is not None:
        output_dir = Path(data_dir) / config.invoice_output

    plans = []
    for number in sorted(by_number):
        group = by_number[number]
        first = group[0]
        paid_date = first.paid_date
        pdf = find_invoice_pdf(output_dir, number) if output_dir is not None else None
        services = config.services if config else {}
        total = round(invoice_total(group, services), 2)
        plans.append(InvoiceRecord(
            number=number,
            client=first.client,
            entity=entry_entity_key(config, first) if config is not None else first.entity,
            issue_date=invoice_issue_date(group),
            total=total,
            income_lines=invoice_income_lines(group, services, total),
            status="paid" if paid_date is not None else "open",
            paid_date=paid_date,
            pdf_path=str(pdf) if pdf else "",
            manual=any(e.service == "_manual" for e in group),
            entry_uids=[e.uid for e in group],
        ))
    return plans


def _verify(conn, expected: list[WorkEntry], plans: list[InvoiceRecord]) -> str | None:
    """The first difference between what was parsed and what reads back."""
    from istota.money import work  # noqa: PLC0415

    stored = {e.uid: e for e in work.read_entries(conn)}
    if len(stored) != len(expected):
        return f"{len(expected)} entries parsed, {len(stored)} stored"
    issue_dates = {p.number: p.issue_date for p in plans}
    for want in expected:
        got = stored.get(want.uid)
        if got is None:
            return f"entry {want.uid} missing after the write"
        for name in ("date", "client", "service", "qty", "amount", "discount",
                     "description", "entity", "invoice", "paid_date", "extra"):
            if getattr(got, name) != getattr(want, name):
                return (
                    f"entry {want.uid} field {name}: parsed {getattr(want, name)!r}, "
                    f"stored {getattr(got, name)!r}"
                )
        want_issue = issue_dates.get(want.invoice) if want.invoice else None
        if got.invoice_date != want_issue:
            return (
                f"entry {want.uid} field invoice_date: expected {want_issue!r}, "
                f"stored {got.invoice_date!r}"
            )
    stored_invoices = {r.number: r for r in work.read_invoices(conn)}
    if len(stored_invoices) != len(plans):
        return f"{len(plans)} invoices planned, {len(stored_invoices)} stored"
    for plan in plans:
        got = stored_invoices.get(plan.number)
        if got is None:
            return f"invoice {plan.number} missing after the write"
        for name in ("client", "entity", "group_name", "issue_date", "total", "status",
                     "paid_date", "pdf_path", "manual", "income_lines"):
            if getattr(got, name) != getattr(plan, name):
                return (
                    f"invoice {plan.number} field {name}: planned {getattr(plan, name)!r}, "
                    f"stored {getattr(got, name)!r}"
                )
        if sorted(got.entry_uids) != sorted(plan.entry_uids):
            return f"invoice {plan.number} lines differ from its stamps"
    return None


def _write(conn, entries: list[WorkEntry], plans: list[InvoiceRecord], stamp: datetime) -> None:
    from datetime import timedelta  # noqa: PLC0415

    from istota.money import work  # noqa: PLC0415

    for position, entry in enumerate(entries):
        # One microsecond apart, so (date, created_at) keeps the file order.
        created = (stamp + timedelta(microseconds=position)).isoformat()
        work.insert_entry(conn, entry, created_at=created)
    for plan in plans:
        work.create_invoice(
            conn, number=plan.number, client=plan.client, entity=plan.entity,
            group_name=plan.group_name, issue_date=plan.issue_date, total=plan.total,
            entry_uids=plan.entry_uids, pdf_path=plan.pdf_path, manual=plan.manual,
            created_at=stamp.isoformat(), income_lines=plan.income_lines,
        )
        if plan.status == "paid":
            conn.execute(
                "UPDATE invoices SET status = 'paid', paid_date = ? WHERE number = ?",
                (plan.paid_date.isoformat(), plan.number),
            )


def migrate_work_log(
    data_dir: Path, *, dry_run: bool = False, db_path: Path | None = None,
) -> MigrationReport:
    """Import the TOML work log into ``money.db``, or report why not.

    Idempotent: with the ``work_store`` key set it returns ``already`` and
    touches nothing. ``dry_run`` runs every step, verification included,
    inside a transaction it always rolls back.
    """
    from istota.money import db, work  # noqa: PLC0415

    data_dir = Path(data_dir)
    path = Path(db_path) if db_path is not None else work.db_path_for(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(path)
    config = _load_config(path)

    with db.get_db(path) as conn:
        if db.kv_get(conn, WORK_STORE_KEY) is not None:
            return MigrationReport("already")
        with _legacy_lock(data_dir):
            conn.execute("BEGIN IMMEDIATE")
            if db.kv_get(conn, WORK_STORE_KEY) is not None:
                conn.rollback()
                return MigrationReport("already")
            log = load_legacy(data_dir)
            years = sorted(log.source_hashes)
            reason = _restore_refusal(data_dir) or _refusals(log)
            if reason:
                conn.rollback()
                logger.warning("work_log_migration_refused reason=%s", reason)
                return MigrationReport("refused", reason, years=years)
            plans = plan_invoices(log.entries, config, data_dir)
            stamp = datetime.now(timezone.utc)
            try:
                _write(conn, log.entries, plans, stamp)
                difference = _verify(conn, log.entries, plans)
            except Exception as exc:  # noqa: BLE001 — every failure rolls back
                conn.rollback()
                logger.error("work_log_migration_failed error=%s", exc)
                return MigrationReport("failed", f"write failed: {exc}", years=years)
            if difference:
                conn.rollback()
                logger.error("work_log_migration_failed difference=%s", difference)
                return MigrationReport(
                    "failed", f"verification failed: {difference}", years=years,
                )
            report = MigrationReport(
                "dry_run" if dry_run else "migrated",
                entries=len(log.entries), invoices=len(plans), years=years,
            )
            if dry_run:
                conn.rollback()
                return report
            db.kv_set(conn, WORK_STORE_KEY, json.dumps({
                "migrated_at": stamp.isoformat(),
                "source_hashes": log.source_hashes,
            }))
            conn.commit()
            # Outside the transaction: a failed rename never undoes the import.
            set_aside_year_files(data_dir, log.source_hashes)
    logger.info(
        "work_log_migrated entries=%d invoices=%d years=%s",
        report.entries, report.invoices, ",".join(years),
    )
    return report
