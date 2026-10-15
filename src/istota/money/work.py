"""Work entries and invoices, stored in ``money.db``.

Entries live in ``work_entries``, invoices in ``invoices`` and the link between
them in ``invoice_lines``. An entry is invoiced while it has an *active* line;
voiding an invoice deactivates its lines, so the voided invoice still lists the
entries it covered and the entries become billable again. A partial unique
index allows one active line per entry, so double billing is a constraint
error. ``WorkEntry.invoice``, ``invoice_date`` and ``paid_date`` are filled by
the read from the active line and its invoice, and writing them on an entry
does nothing.

The TOML year files under ``{data_dir}/invoices/work/`` are the old store.
The first time a work function opens the store and the ``work_store`` key is
missing, :func:`istota.money.work_migrate.migrate_work_log` imports them. A
refused or failed import raises :class:`WorkStoreBlocked` from every function
here, and nothing is written.

Two kinds of identity, not interchangeable:

* ``entry.id`` — the 1-based display index, recomputed on every read, ordered
  by ``(date, created_at, uid)``. The CLI's ``#N``.
* ``entry.uid`` — the stable primary key. Anything that holds a reference
  across time (the web UI, invoice generation) addresses entries by uid.

**Where the database is.** ``money.db`` is the same file the rest of the money
module uses, which on a deployment is ``Config.module_db_path`` on local disk,
not ``{data_dir}/data/money.db`` on the workspace mount. The functions here
take ``data_dir``, so the money contexts (``cli.UserContext`` on construction,
``cli.Context.activate_user`` and ``_migrate.ensure_initialised``) call
:func:`bind_db_path` with their ``db_path``; an unbound ``data_dir`` falls back
to ``{data_dir}/data/money.db``.

Writes run under ``BEGIN IMMEDIATE``. A caller already holding a transaction
on ``money.db`` passes its connection as ``conn=`` so the write joins it; a
second connection would wait on the first one's lock.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path

from istota.lib.file_lock import exclusive_lock
from istota.lib.timestamps import iso_now
from istota.money.core.ids import new_txn_id
from istota.money.core.models import InvoiceRecord, WorkEntry

logger = logging.getLogger(__name__)

# Fields a caller may never set through the generic update path. The invoice
# fields are read from the invoice, so an update that names them goes through
# the attach path instead (``invoice``) or is ignored.
_PROTECTED_FIELDS = frozenset({"uid", "id", "extra", "invoice_date", "paid_date"})


class WorkStoreLocked(RuntimeError):
    """Raised when the legacy work-file lock can't be acquired in time."""


class WorkStoreBlocked(RuntimeError):
    """The TOML work log could not be imported, so the store is unavailable.

    The message is ``work log migration blocked: <reason>``, which the CLI and
    routes pass through as the error. ``repeated`` is true when the refusal
    was answered from this process's cache rather than a fresh import attempt,
    so a caller that logs it can log it once.
    """

    def __init__(self, message: str, *, repeated: bool = False):
        super().__init__(message)
        self.repeated = repeated


@dataclass
class WorkMutationResult:
    """Outcome of a uid-addressed mutation.

    ``status`` is one of ``ok`` / ``not_found`` / ``invoiced`` / ``conflict``
    / ``no_fields`` / ``on_voided_invoice`` (a removal refused because a
    voided invoice still lists the entry). ``entry`` carries the current
    server-side entry when one was resolved.
    """
    status: str
    entry: WorkEntry | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# =============================================================================
# Where the store is, and opening it
# =============================================================================

_BOUND_DB_PATHS: dict[str, Path] = {}
_INITIALISED: set[str] = set()


def _key(data_dir: Path) -> str:
    return os.path.abspath(os.fspath(data_dir))


def bind_db_path(data_dir: Path, db_path: Path | None) -> None:
    """Record which ``money.db`` holds the work store for ``data_dir``."""
    if db_path is None:
        return
    _BOUND_DB_PATHS[_key(data_dir)] = Path(db_path)


def db_path_for(data_dir: Path) -> Path:
    bound = _BOUND_DB_PATHS.get(_key(data_dir))
    if bound is not None:
        return bound
    return Path(data_dir) / "data" / "money.db"


def _init(path: Path) -> None:
    from istota.money import db  # noqa: PLC0415

    key = os.path.abspath(path)
    if key in _INITIALISED and path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(path)
    _INITIALISED.add(key)


def _store_record(conn: sqlite3.Connection) -> str | None:
    from istota.money import db  # noqa: PLC0415
    from istota.money.work_migrate import WORK_STORE_KEY  # noqa: PLC0415

    return db.kv_get(conn, WORK_STORE_KEY)


def _is_migrated(conn: sqlite3.Connection) -> bool:
    return _store_record(conn) is not None


# A refusal depends on the year files alone, so it is kept per process until
# one of them changes; without this every money request re-parsed them all
# and logged the refusal again. A failed import is retried, since a write
# failure can be transient.
_REFUSED: dict[tuple[str, str], tuple[tuple, str]] = {}


def _year_files_fingerprint(data_dir: Path) -> tuple:
    from istota.money.work_migrate import year_files  # noqa: PLC0415

    seen = []
    for path in year_files(data_dir):
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        seen.append((path.name, st.st_size, st.st_mtime_ns))
    return tuple(seen)


def _set_aside_leftovers(data_dir: Path, record: str) -> None:
    from istota.money.work_migrate import set_aside_year_files, year_files  # noqa: PLC0415

    if not year_files(data_dir):
        return
    try:
        hashes = json.loads(record).get("source_hashes") or {}
    except (ValueError, AttributeError):
        hashes = {}
    set_aside_year_files(data_dir, hashes)


def ensure_store(data_dir: Path) -> Path:
    """Make sure the store is usable: schema present and the work log imported.

    Returns the database path. Raises :class:`WorkStoreBlocked` when the
    import refuses or fails. Once imported, a ``{year}.toml`` still in the
    work dir (a rename that failed, or a file that came back) is renamed aside
    unread.
    """
    from istota.money import db  # noqa: PLC0415
    from istota.money.work_migrate import migrate_work_log  # noqa: PLC0415

    path = db_path_for(data_dir)
    _init(path)
    with db.get_db(path) as conn:
        record = _store_record(conn)
    if record is not None:
        _set_aside_leftovers(data_dir, record)
        return path

    cache_key = (_key(data_dir), os.path.abspath(path))
    fingerprint = _year_files_fingerprint(data_dir)
    cached = _REFUSED.get(cache_key)
    if cached is not None and cached[0] == fingerprint:
        raise WorkStoreBlocked(cached[1], repeated=True)
    report = migrate_work_log(data_dir, db_path=path)
    if not report.ok:
        message = f"work log migration blocked: {report.reason}"
        if report.status == "refused":
            _REFUSED[cache_key] = (fingerprint, message)
        raise WorkStoreBlocked(message)
    _REFUSED.pop(cache_key, None)
    return path


def _check_conn(conn: sqlite3.Connection, data_dir: Path) -> None:
    if _is_migrated(conn):
        return
    if conn.in_transaction:
        # Importing needs a write lock this caller's own transaction holds.
        raise WorkStoreBlocked(
            "work log migration blocked: the work log has not been imported yet",
        )
    ensure_store(data_dir)


@contextmanager
def _reading(data_dir: Path, conn: sqlite3.Connection | None = None):
    from istota.money import db  # noqa: PLC0415

    if conn is not None:
        _check_conn(conn, data_dir)
        yield conn
        return
    path = ensure_store(data_dir)
    with db.get_db(path) as own:
        yield own


@contextmanager
def write_transaction(data_dir: Path, conn: sqlite3.Connection | None = None):
    """A ``BEGIN IMMEDIATE`` transaction on the store, committed on a clean exit.

    With ``conn``, the caller owns the transaction: nothing begins or commits
    here.
    """
    from istota.money import db  # noqa: PLC0415

    if conn is not None:
        _check_conn(conn, data_dir)
        yield conn
        return
    path = ensure_store(data_dir)
    with db.get_db(path) as own:
        own.execute("BEGIN IMMEDIATE")
        yield own


# =============================================================================
# Legacy file lock (the TOML store's writers; kept for the import and backfill)
# =============================================================================


def _work_dir(data_dir: Path) -> Path:
    from istota.workspace.writes import open_or_create_dirs  # noqa: PLC0415

    # Created from the money data dir with no link followed at `invoices` or
    # `work` (ISSUE-723).
    os.close(open_or_create_dirs(data_dir, ["invoices", "work"]))
    return data_dir / "invoices" / "work"


@contextmanager
def _work_lock(data_dir: Path, *, timeout_seconds: float = 10.0):
    """Exclusive flock on ``{work_dir}/.work.lock`` for the year files' writers."""
    lock_path = _work_dir(data_dir) / ".work.lock"
    with exclusive_lock(
        lock_path,
        timeout_seconds=timeout_seconds,
        on_timeout=WorkStoreLocked,
    ):
        yield


# =============================================================================
# Serialization (the etag, and the uid backfill's year files)
# =============================================================================


def _parse_date(s: str) -> date:
    parts = s.split("-")
    return date(int(parts[0]), int(parts[1]), int(parts[2]))


def _format_num(n: float) -> str:
    if n == int(n):
        return str(int(n))
    return str(n)


_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}

# Bare TOML keys are letters, digits, underscores and dashes; anything else is
# written as a quoted key.
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _escape(s: str) -> str:
    """Escape a string for a TOML basic string, every control character included."""
    out = []
    for ch in s:
        esc = _TOML_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ch < "\x20" or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)


def _format_toml_key(key: str) -> str:
    if _BARE_KEY_RE.match(key):
        return key
    return f'"{_escape(key)}"'


def _format_toml_value(value) -> str | None:
    """Render a value from ``WorkEntry.extra`` as TOML, or None if it has no flat form."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_num(value)
    if isinstance(value, str):
        return f'"{_escape(value)}"'
    if isinstance(value, (datetime, date, time)):
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
    # Only when set, so the etag of every unflagged entry is what it was.
    if entry.separate:
        lines.append("separate = true")
    if entry.invoice:
        lines.append(f'invoice = "{_escape(entry.invoice)}"')
    if entry.invoice_date is not None:
        lines.append(f"invoice_date = {entry.invoice_date.isoformat()}")
    if entry.paid_date is not None:
        lines.append(f"paid_date = {entry.paid_date.isoformat()}")
    for key in sorted(entry.extra):
        rendered = _format_toml_value(entry.extra[key])
        if rendered is None:
            continue
        lines.append(f"{_format_toml_key(key)} = {rendered}")
    return "\n".join(lines)


def entry_etag(entry: WorkEntry) -> str:
    """Content hash of an entry, for optimistic-concurrency checks.

    Derived from the serialized form, so it covers every persisted field
    (``extra`` and the invoice stamps included) and not the display index.
    Never stored.
    """
    return hashlib.sha256(_serialize_entry(entry).encode()).hexdigest()[:12]


# =============================================================================
# Rows
# =============================================================================

# TOML has date, datetime and time values that JSON does not. ``extra`` keeps
# them as one-key objects so they read back as the same type.
_EXTRA_TAGS = {"$date": date, "$datetime": datetime, "$time": time}


def _tag(value):
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, time):
        return {"$time": value.isoformat()}
    if isinstance(value, list):
        return [_tag(v) for v in value]
    if isinstance(value, dict):
        return {k: _tag(v) for k, v in value.items()}
    return value


def _untag(value):
    if isinstance(value, list):
        return [_untag(v) for v in value]
    if isinstance(value, dict):
        if len(value) == 1:
            (tag, raw), = value.items()
            kind = _EXTRA_TAGS.get(tag)
            if kind is not None and isinstance(raw, str):
                return kind.fromisoformat(raw)
        return {k: _untag(v) for k, v in value.items()}
    return value


def _encode_extra(extra: dict) -> str:
    return json.dumps(_tag(extra or {}), sort_keys=True, ensure_ascii=False)


def _decode_extra(text: str) -> dict:
    return _untag(json.loads(text or "{}"))


def _opt_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _now() -> str:
    return iso_now()


_ENTRY_QUERY = """
SELECT e.uid, e.date, e.client, e.service, e.qty, e.amount, e.discount,
       e.description, e.entity, e.separate, e.extra,
       l.invoice_number AS invoice, i.issue_date, i.paid_date
FROM work_entries e
LEFT JOIN invoice_lines l ON l.entry_uid = e.uid AND l.active = 1
LEFT JOIN invoices i ON i.number = l.invoice_number
ORDER BY e.date, e.created_at, e.uid
"""


def read_entries(conn: sqlite3.Connection) -> list[WorkEntry]:
    """Every entry, in display order, with ``id`` set to its display index."""
    entries = []
    for position, row in enumerate(conn.execute(_ENTRY_QUERY), 1):
        entries.append(WorkEntry(
            date=date.fromisoformat(row["date"]),
            client=row["client"],
            service=row["service"],
            qty=row["qty"],
            amount=row["amount"],
            discount=row["discount"],
            description=row["description"],
            entity=row["entity"],
            separate=bool(row["separate"]),
            invoice=row["invoice"] or "",
            invoice_date=_opt_date(row["issue_date"]),
            paid_date=_opt_date(row["paid_date"]),
            id=position,
            uid=row["uid"],
            extra=_decode_extra(row["extra"]),
        ))
    return entries


def _invoice_from_row(row, entry_uids: list[str]) -> InvoiceRecord:
    return InvoiceRecord(
        number=row["number"],
        client=row["client"],
        entity=row["entity"],
        group_name=row["group_name"],
        issue_date=date.fromisoformat(row["issue_date"]),
        total=row["total"],
        status=row["status"],
        paid_date=_opt_date(row["paid_date"]),
        voided_at=row["voided_at"],
        was_paid=bool(row["was_paid"]),
        void_reason=row["void_reason"],
        replaced_by=json.loads(row["replaced_by"] or "[]"),
        pdf_path=row["pdf_path"],
        manual=bool(row["manual"]),
        created_at=row["created_at"],
        entry_uids=entry_uids,
    )


def read_invoices(
    conn: sqlite3.Connection,
    *,
    client: str | None = None,
    include_voided: bool = True,
) -> list[InvoiceRecord]:
    """Every invoice row, by number, each with all the entries it covered."""
    lines: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT l.invoice_number, l.entry_uid FROM invoice_lines l "
        "JOIN work_entries e ON e.uid = l.entry_uid "
        "ORDER BY e.date, e.created_at, e.uid"
    ):
        lines.setdefault(row["invoice_number"], []).append(row["entry_uid"])
    records = []
    for row in conn.execute("SELECT * FROM invoices ORDER BY number"):
        if client and row["client"].lower() != client.lower():
            continue
        if not include_voided and row["status"] == "voided":
            continue
        records.append(_invoice_from_row(row, lines.get(row["number"], [])))
    return records


def insert_entry(
    conn: sqlite3.Connection, entry: WorkEntry, *, created_at: str | None = None,
) -> None:
    """Insert one entry row. The invoice fields on ``entry`` are not written."""
    if not entry.uid:
        raise ValueError("a work entry needs a uid")
    stamp = created_at or _now()
    conn.execute(
        "INSERT INTO work_entries (uid, date, client, service, qty, amount, discount, "
        "description, entity, separate, extra, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entry.uid, entry.date.isoformat(), entry.client, entry.service,
         entry.qty, entry.amount, entry.discount or 0, entry.description,
         entry.entity, int(bool(entry.separate)), _encode_extra(entry.extra), stamp, stamp),
    )


def _update_entry_row(conn: sqlite3.Connection, entry: WorkEntry) -> None:
    conn.execute(
        "UPDATE work_entries SET date = ?, client = ?, service = ?, qty = ?, amount = ?, "
        "discount = ?, description = ?, entity = ?, separate = ?, updated_at = ? "
        "WHERE uid = ?",
        (entry.date.isoformat(), entry.client, entry.service, entry.qty, entry.amount,
         entry.discount or 0, entry.description, entry.entity, int(bool(entry.separate)),
         _now(), entry.uid),
    )


def _stamp_date(value: str | date | None) -> date:
    """An issue date: what the caller passed, or today. Anything else is refused."""
    if value is None:
        return date.today()
    if isinstance(value, str):
        return _parse_date(value)
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, date):
        raise TypeError(f"invoice_date must be a date or ISO string, got {type(value).__name__}")
    return value


def create_invoice(
    conn: sqlite3.Connection,
    *,
    number: str,
    client: str,
    entity: str,
    group_name: str,
    issue_date: date | str,
    total: float,
    entry_uids: list[str],
    pdf_path: str | Path,
    manual: bool = False,
    created_at: str | None = None,
) -> None:
    """Insert an open invoice and an active line per entry, on the caller's transaction.

    An unknown uid is a ``ValueError``; an entry that already has an active
    line, or a number already issued, is an ``sqlite3.IntegrityError``. The
    caller rolls back either way.
    """
    uids = list(dict.fromkeys(u for u in entry_uids if u))
    for uid in uids:
        if conn.execute("SELECT 1 FROM work_entries WHERE uid = ?", (uid,)).fetchone() is None:
            raise ValueError(f"no work entry with uid {uid}")
    issued = _stamp_date(issue_date)
    conn.execute(
        "INSERT INTO invoices (number, client, entity, group_name, issue_date, total, "
        "status, pdf_path, manual, created_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
        (number, client, entity, group_name, issued.isoformat(), total,
         str(pdf_path or ""), int(bool(manual)), created_at or _now()),
    )
    for uid in uids:
        conn.execute(
            "INSERT INTO invoice_lines (invoice_number, entry_uid, active) VALUES (?, ?, 1)",
            (number, uid),
        )


# =============================================================================
# Hand assignment (work update --invoice, add_work_entry(invoice=...))
# =============================================================================


def _invoicing_config(data_dir: Path):
    """The invoicing config beside the store, or None. Read before a write begins."""
    from istota.money import config_store  # noqa: PLC0415

    path = ensure_store(data_dir)
    if not config_store.has_invoicing_data(path):
        return None
    return config_store.load_invoicing(path)


def _attach(
    conn: sqlite3.Connection,
    number: str,
    uids: list[str],
    issue_date: date | None,
    config,
) -> int:
    """Put uninvoiced entries on ``number``, creating the invoice if it is new.

    A hand assignment edits the invoice, so its total is recomputed over its
    active lines at the current rates. A voided number is refused with a
    ``ValueError``. Returns the count attached.
    """
    from istota.money.core.invoicing import entry_entity_key, invoice_total  # noqa: PLC0415

    entries = {e.uid: e for e in read_entries(conn)}
    wanted = [entries[u] for u in dict.fromkeys(uids) if u in entries and not entries[u].invoice]
    if not wanted:
        return 0
    row = conn.execute("SELECT status FROM invoices WHERE number = ?", (number,)).fetchone()
    if row is not None and row["status"] == "voided":
        raise ValueError(f"{number} was voided; a voided number is never reused")
    if row is None:
        first = wanted[0]
        create_invoice(
            conn, number=number, client=first.client,
            entity=entry_entity_key(config, first) if config is not None else first.entity,
            group_name="", issue_date=issue_date or date.today(), total=0.0,
            entry_uids=[e.uid for e in wanted], pdf_path="",
        )
    else:
        for entry in wanted:
            conn.execute(
                "INSERT INTO invoice_lines (invoice_number, entry_uid, active) VALUES (?, ?, 1) "
                "ON CONFLICT (invoice_number, entry_uid) DO UPDATE SET active = 1",
                (number, entry.uid),
            )
    covered = [e for e in read_entries(conn) if e.invoice == number]
    services = config.services if config is not None else {}
    conn.execute(
        "UPDATE invoices SET total = ? WHERE number = ?",
        (round(invoice_total(covered, services), 2), number),
    )
    return len(wanted)


# =============================================================================
# Public API
# =============================================================================


def load_work_entries(data_dir: Path, *, conn: sqlite3.Connection | None = None) -> list[WorkEntry]:
    """Every entry, sorted for display, with ``id`` set to the display index."""
    with _reading(data_dir, conn) as c:
        return read_entries(c)


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
    invoice_date: str | date | None = None,
    uid: str = "",
    *,
    separate: bool = False,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Insert an entry and return its display index.

    Pass ``uid`` to choose the stable id up front; the returned index is stale
    the moment another writer inserts something earlier. ``invoice`` puts the
    entry on that invoice (creating it, dated ``invoice_date`` or today, when
    the number is new). ``separate`` invoices the entry on its own.
    """
    new_entry = WorkEntry(
        date=_parse_date(entry_date), client=client.lower(), service=service,
        qty=qty, amount=amount, discount=discount,
        description=description, entity=entity, separate=bool(separate),
        uid=uid or new_txn_id(),
    )
    config = _invoicing_config(data_dir) if invoice and conn is None else None
    with write_transaction(data_dir, conn) as c:
        insert_entry(c, new_entry)
        if invoice:
            _attach(c, invoice, [new_entry.uid], _stamp_date(invoice_date), config)
        for entry in read_entries(c):
            if entry.uid == new_entry.uid:
                return entry.id
    raise RuntimeError("work entry not readable after insert")


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
    """Assign updatable entry fields, coercing date and client as the CLI does."""
    for key, value in fields.items():
        if key in _PROTECTED_FIELDS or key == "invoice":
            continue
        if key == "date" and isinstance(value, str):
            value = _parse_date(value)
        if key == "client" and isinstance(value, str):
            value = value.lower()
        if hasattr(entry, key):
            setattr(entry, key, value)


def _update(
    data_dir: Path,
    find: Callable[[list[WorkEntry]], WorkEntry | None],
    expect_etag: str | None,
    fields: dict,
) -> WorkMutationResult:
    if not fields:
        return WorkMutationResult("no_fields")
    number = fields.get("invoice") or ""
    config = _invoicing_config(data_dir) if number else None
    with write_transaction(data_dir) as conn:
        entry = find(read_entries(conn))
        if entry is None:
            return WorkMutationResult("not_found")
        if entry.invoice:
            return WorkMutationResult("invoiced", entry)
        if expect_etag and entry_etag(entry) != expect_etag:
            return WorkMutationResult("conflict", entry)
        changed = replace(entry, extra=dict(entry.extra))
        _apply_fields(changed, fields)
        _update_entry_row(conn, changed)
        if number:
            stamp = fields.get("invoice_date")
            _attach(conn, number, [entry.uid], _stamp_date(stamp) if stamp else None, config)
        fresh = _find_by_uid(read_entries(conn), entry.uid)
        return WorkMutationResult("ok", fresh)


def update_work_entry(data_dir: Path, index: int, **fields) -> bool:
    """Update the entry at a 1-based display index. Only if uninvoiced.

    Index-addressed, so only safe when the caller read the list and acts on it
    immediately (the CLI). Anything else uses :func:`update_work_entry_by_uid`.
    """
    def find(entries):
        if index < 1 or index > len(entries):
            return None
        return entries[index - 1]

    return _update(data_dir, find, None, fields).ok


def _find_by_uid(entries: list[WorkEntry], uid: str) -> WorkEntry | None:
    if not uid:
        # An empty uid must not address an arbitrary row.
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

    ``expect_etag`` adds an optimistic-concurrency check: a mismatch means the
    entry changed since the caller read it, and nothing is written.
    """
    return _update(data_dir, lambda entries: _find_by_uid(entries, uid), expect_etag, fields)


def _remove(
    data_dir: Path,
    find: Callable[[list[WorkEntry]], WorkEntry | None],
    expect_etag: str | None,
) -> WorkMutationResult:
    with write_transaction(data_dir) as conn:
        entry = find(read_entries(conn))
        if entry is None:
            return WorkMutationResult("not_found")
        if entry.invoice:
            return WorkMutationResult("invoiced", entry)
        if expect_etag and entry_etag(entry) != expect_etag:
            return WorkMutationResult("conflict", entry)
        listed = conn.execute(
            "SELECT 1 FROM invoice_lines WHERE entry_uid = ?", (entry.uid,),
        ).fetchone()
        if listed is not None:
            return WorkMutationResult("on_voided_invoice", entry)
        conn.execute("DELETE FROM work_entries WHERE uid = ?", (entry.uid,))
        return WorkMutationResult("ok", entry)


def remove_work_entry_by_uid(
    data_dir: Path,
    uid: str,
    *,
    expect_etag: str | None = None,
) -> WorkMutationResult:
    """Remove an entry addressed by its stable ``uid``. Only if uninvoiced."""
    return _remove(data_dir, lambda entries: _find_by_uid(entries, uid), expect_etag)


def remove_work_entry(data_dir: Path, index: int) -> bool:
    """Remove the entry at a 1-based display index. Only if uninvoiced."""
    def find(entries):
        if index < 1 or index > len(entries):
            return None
        return entries[index - 1]

    return _remove(data_dir, find, None).ok


def backfill_work_ids(data_dir: Path, *, timeout_seconds: float = 10.0) -> int:
    """Stamp a uid on year-file entries that lack one, before the import.

    The import refuses an entry with no uid, and a hand-added one can appear
    in the year files until the import has run. After it, every entry is a
    row with a primary key and this is a no-op.
    """
    from istota.money import db  # noqa: PLC0415
    from istota.money.work_migrate import backfill_legacy_uids  # noqa: PLC0415

    path = db_path_for(data_dir)
    if path.exists():
        _init(path)
        with db.get_db(path) as conn:
            if _is_migrated(conn):
                return 0
    return backfill_legacy_uids(data_dir, timeout_seconds=timeout_seconds)


def get_uninvoiced_entries(
    data_dir: Path,
    client: str | None = None,
    period: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[WorkEntry]:
    """Entries with no active invoice line, optionally up to the end of ``period``."""
    entries = load_work_entries(data_dir, conn=conn)
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


def assign_invoice_number_by_uids(
    data_dir: Path,
    uids: list[str],
    invoice_number: str,
    invoice_date: str | date | None = None,
) -> int:
    """Put the entries with these uids on an invoice, by hand. Returns the count.

    Entries already on an invoice are skipped. A new number creates the
    invoice dated ``invoice_date`` (or today). Invoice generation does not use
    this: it creates its invoices with :func:`create_invoice`.
    """
    wanted = [u for u in uids if u]
    if not wanted:
        return 0
    stamp = _stamp_date(invoice_date)
    config = _invoicing_config(data_dir)
    with write_transaction(data_dir) as conn:
        return _attach(conn, invoice_number, wanted, stamp, config)


def assign_invoice_number(
    data_dir: Path,
    indices: list[int],
    invoice_number: str,
    invoice_date: str | date | None = None,
) -> int:
    """The display-index form of :func:`assign_invoice_number_by_uids`."""
    if not indices:
        return 0
    stamp = _stamp_date(invoice_date)
    config = _invoicing_config(data_dir)
    with write_transaction(data_dir) as conn:
        entries = read_entries(conn)
        uids = [entries[i - 1].uid for i in indices if 1 <= i <= len(entries)]
        return _attach(conn, invoice_number, uids, stamp, config)


def _invoice_row(conn: sqlite3.Connection, number: str):
    return conn.execute("SELECT * FROM invoices WHERE number = ?", (number,)).fetchone()


def mark_paid(
    data_dir: Path,
    invoice_number: str,
    paid_date: str | date,
    *,
    validate: Callable[[list[WorkEntry]], bool] | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Mark an open invoice paid. Returns the count of entries it covers, or 0.

    ``validate`` sees the invoice's entries before anything is written and can
    refuse the write by returning False. A paid or voided invoice is left
    alone and returns 0.
    """
    if isinstance(paid_date, str):
        paid_date = _parse_date(paid_date)
    with write_transaction(data_dir, conn) as c:
        entries = [e for e in read_entries(c) if e.invoice == invoice_number]
        if validate is not None and not validate(entries):
            return 0
        row = _invoice_row(c, invoice_number)
        if row is None or row["status"] != "open":
            return 0
        c.execute(
            "UPDATE invoices SET status = 'paid', paid_date = ? WHERE number = ?",
            (paid_date.isoformat(), invoice_number),
        )
        return len(entries)


def mark_unpaid(
    data_dir: Path,
    invoice_number: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Reopen a paid invoice, keeping its number. Returns its entry count, or 0."""
    with write_transaction(data_dir, conn) as c:
        row = _invoice_row(c, invoice_number)
        if row is None or row["status"] != "paid":
            return 0
        c.execute(
            "UPDATE invoices SET status = 'open', paid_date = NULL WHERE number = ?",
            (invoice_number,),
        )
        return len([e for e in read_entries(c) if e.invoice == invoice_number])


def get_entries_for_invoice(
    data_dir: Path, invoice_number: str, *, conn: sqlite3.Connection | None = None,
) -> list[WorkEntry]:
    """The entries on an invoice's active lines (none for a voided invoice)."""
    return [e for e in load_work_entries(data_dir, conn=conn) if e.invoice == invoice_number]


def invoice_issue_date(entries: list[WorkEntry]) -> date | None:
    """When an invoice was issued, or the closest sound estimate.

    Every entry read from the store carries its invoice's issue date. The
    import uses this on the year files, where invoices raised before the
    ``invoice_date`` stamp existed have none: those fall back to the *latest*
    work billed, since an invoice cannot predate the last work on it. Stamps
    that disagree take the earliest. ``None`` for an empty list.
    """
    if not entries:
        return None
    stamped = [e.invoice_date for e in entries if e.invoice_date is not None]
    if stamped:
        return min(stamped)
    return max(e.date for e in entries)


class VoidError(ValueError):
    """A void the store refused. ``code`` says why, for a caller mapping it.

    ``not_found``, ``already_voided``, ``paid`` (a paid invoice without
    ``force``) or ``unknown_replacement`` (a ``replaced_by`` number with no
    invoice row).
    """

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass
class VoidResult:
    """What :func:`void_invoice` did: the entries it released and the row it left."""
    number: str
    released: int
    was_paid: bool
    voided_at: str
    pdf_path: str
    reason: str
    replaced_by: list[str]


def void_invoice(
    data_dir: Path,
    invoice_number: str,
    *,
    reason: str = "",
    replaced_by: tuple[str, ...] | list[str] = (),
    force: bool = False,
    pdf_path: str | Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> VoidResult:
    """Void an invoice and release its entries, in one transaction.

    The invoice row stays, with ``status = 'voided'``, the reason and its
    replacements, and its lines stay listed with ``active = 0``. A paid invoice
    needs ``force`` and is recorded with ``was_paid``. ``pdf_path``, when
    given, replaces the stored path (the caller renamed or deleted the file).
    Every refusal is a :class:`VoidError` raised before anything is written.
    """
    replacements = list(dict.fromkeys(n.strip() for n in replaced_by if n and n.strip()))
    with write_transaction(data_dir, conn) as c:
        row = _invoice_row(c, invoice_number)
        if row is None:
            raise VoidError(f"Invoice {invoice_number} not found", code="not_found")
        if row["status"] == "voided":
            voided_on = (row["voided_at"] or "")[:10]
            raise VoidError(
                f"Invoice {invoice_number} already voided on {voided_on}",
                code="already_voided",
            )
        was_paid = row["status"] == "paid"
        if was_paid and not force:
            raise VoidError(
                f"Invoice {invoice_number} has been marked as paid. Use --force to void anyway.",
                code="paid",
            )
        for number in replacements:
            if number == invoice_number:
                raise VoidError(
                    f"Invoice {invoice_number} cannot replace itself",
                    code="unknown_replacement",
                )
            if _invoice_row(c, number) is None:
                raise VoidError(
                    f"Replacement invoice {number} not found",
                    code="unknown_replacement",
                )
        released = c.execute(
            "UPDATE invoice_lines SET active = 0 WHERE invoice_number = ? AND active = 1",
            (invoice_number,),
        ).rowcount
        voided_at = _now()
        stored_pdf = row["pdf_path"] if pdf_path is None else str(pdf_path)
        c.execute(
            "UPDATE invoices SET status = 'voided', voided_at = ?, was_paid = ?, "
            "void_reason = ?, replaced_by = ?, pdf_path = ? WHERE number = ?",
            (voided_at, int(was_paid), reason or "", json.dumps(replacements),
             stored_pdf, invoice_number),
        )
        return VoidResult(
            number=invoice_number, released=released, was_paid=was_paid,
            voided_at=voided_at, pdf_path=stored_pdf,
            reason=reason or "", replaced_by=replacements,
        )


def get_invoice(
    data_dir: Path, invoice_number: str, *, conn: sqlite3.Connection | None = None,
) -> InvoiceRecord | None:
    """One invoice record by number, voided or not, or None."""
    with _reading(data_dir, conn) as c:
        row = _invoice_row(c, invoice_number)
        if row is None:
            return None
        uids = [
            r["entry_uid"] for r in c.execute(
                "SELECT l.entry_uid FROM invoice_lines l "
                "JOIN work_entries e ON e.uid = l.entry_uid "
                "WHERE l.invoice_number = ? ORDER BY e.date, e.created_at, e.uid",
                (invoice_number,),
            )
        ]
        return _invoice_from_row(row, uids)


def list_invoices(
    data_dir: Path,
    *,
    client: str | None = None,
    include_voided: bool = True,
    conn: sqlite3.Connection | None = None,
) -> list[InvoiceRecord]:
    """Every invoice record, by number."""
    with _reading(data_dir, conn) as c:
        return read_invoices(c, client=client, include_voided=include_voided)


def get_invoice_numbers(data_dir: Path) -> list[str]:
    """Numbers of the invoices that are not voided, sorted."""
    return [r.number for r in list_invoices(data_dir, include_voided=False)]
