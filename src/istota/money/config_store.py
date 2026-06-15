"""DB-backed config store for the money module.

Single home for per-user money configuration: invoicing, tax, monarch.
Lives in the per-user ``money.db`` alongside the existing transaction-tracking
schema. Mirrors the role :mod:`istota.feeds.db` plays for feeds.

The TOML files (``invoicing.toml`` / ``tax.toml`` / ``monarch.toml``) remain
the human-editable seed and the import/export wire format, but they are no
longer the runtime source of truth. The ``parse_*_config`` helpers in
``core/`` stay as thin wrappers over the dict-based ``*_from_toml_dict``
functions here so any escape-hatch caller (the standalone ``money`` CLI
invoked outside istota) keeps working.

Round-trip identity guarantee:

    toml_dict → from_dict() → save → load → to_dict() == toml_dict

modulo defaults the dataclass fills in for missing keys; export does not
write keys that match the dataclass default unless they were present in the
input.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from istota.money.core.models import (
    ClientConfig,
    CompanyConfig,
    InvoicingConfig,
    MonarchConfig,
    MonarchCredentials,
    MonarchProfile,
    MonarchSyncSettings,
    MonarchTagFilters,
    ServiceConfig,
    TaxConfig,
)


# =============================================================================
# Schema
# =============================================================================


SCHEMA = """\
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS invoicing_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS invoicing_companies (
    key                  TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    address              TEXT,
    email                TEXT,
    payment_instructions TEXT,
    logo                 TEXT,
    ar_account           TEXT,
    bank_account         TEXT,
    currency             TEXT
);

CREATE TABLE IF NOT EXISTS invoicing_clients (
    key                 TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    address             TEXT,
    email               TEXT,
    terms               TEXT,
    ar_account          TEXT,
    entity              TEXT,
    schedule            TEXT NOT NULL DEFAULT 'on-demand',
    schedule_day        INTEGER NOT NULL DEFAULT 1,
    reminder_days       INTEGER NOT NULL DEFAULT 3,
    notifications       TEXT,
    days_until_overdue  INTEGER NOT NULL DEFAULT 0,
    ledger_posting      INTEGER NOT NULL DEFAULT 1,
    bundles_json        TEXT NOT NULL DEFAULT '[]',
    separate_json       TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS invoicing_services (
    key            TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    rate           REAL NOT NULL,
    type           TEXT NOT NULL DEFAULT 'hours',
    income_account TEXT
);

CREATE TABLE IF NOT EXISTS tax_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tax_account_patterns (
    kind    TEXT NOT NULL,
    pattern TEXT NOT NULL,
    PRIMARY KEY(kind, pattern)
);

CREATE TABLE IF NOT EXISTS tax_year_rates (
    tax_year                       INTEGER PRIMARY KEY,
    ss_wage_base                   REAL,
    ss_rate                        REAL,
    medicare_rate                  REAL,
    se_taxable_fraction            REAL,
    federal_standard_deduction     REAL,
    ca_standard_deduction          REAL,
    federal_brackets_json          TEXT,
    ca_brackets_json               TEXT
);

CREATE TABLE IF NOT EXISTS monarch_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS monarch_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL UNIQUE,
    ledger                TEXT NOT NULL,
    lookback_days         INTEGER,
    default_account       TEXT,
    recategorize_account  TEXT
);

CREATE TABLE IF NOT EXISTS monarch_account_map (
    profile_id        INTEGER NOT NULL,
    monarch_name      TEXT NOT NULL,
    beancount_account TEXT NOT NULL,
    PRIMARY KEY(profile_id, monarch_name),
    FOREIGN KEY(profile_id) REFERENCES monarch_profiles(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS monarch_category_map (
    profile_id        INTEGER NOT NULL,
    monarch_category  TEXT NOT NULL,
    beancount_account TEXT NOT NULL,
    PRIMARY KEY(profile_id, monarch_category),
    FOREIGN KEY(profile_id) REFERENCES monarch_profiles(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS monarch_tag_filters (
    profile_id INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    tag        TEXT NOT NULL,
    PRIMARY KEY(profile_id, kind, tag),
    FOREIGN KEY(profile_id) REFERENCES monarch_profiles(id) ON DELETE CASCADE
);
"""


SCHEMA_VERSION = "1"
GLOBAL_PROFILE_ID = 0


def init_db(db_path: Path | str) -> None:
    """Create config tables if missing. Idempotent."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )
        conn.execute(
            "INSERT OR IGNORE INTO monarch_profiles(id, name, ledger) "
            "VALUES (?, ?, ?)",
            (GLOBAL_PROFILE_ID, "__global__", ""),
        )


@contextmanager
def _connect(db_path: Path | str):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =============================================================================
# JSON-encoded scalar helpers
# =============================================================================


def _kv_get(conn: sqlite3.Connection, table: str, key: str) -> Any:
    row = conn.execute(
        f"SELECT value FROM {table} WHERE key = ?", (key,),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
        return row["value"]


def _kv_set(conn: sqlite3.Connection, table: str, key: str, value: Any) -> None:
    encoded = json.dumps(value)
    conn.execute(
        f"INSERT INTO {table}(key, value) VALUES (?, ?) "
        f"ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, encoded),
    )


def _kv_delete(conn: sqlite3.Connection, table: str, key: str) -> None:
    conn.execute(f"DELETE FROM {table} WHERE key = ?", (key,))


def _kv_all(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in conn.execute(f"SELECT key, value FROM {table}").fetchall():
        try:
            out[row["key"]] = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            out[row["key"]] = row["value"]
    return out


# =============================================================================
# Invoicing
# =============================================================================


_INVOICING_SCALAR_KEYS = (
    "accounting_path",
    "invoice_output",
    "next_invoice_number",
    "default_entity",
    "currency",
    "default_ar_account",
    "default_bank_account",
    "notifications",
    "days_until_overdue",
)


def invoicing_config_from_toml_dict(data: dict) -> InvoicingConfig:
    """Hydrate :class:`InvoicingConfig` from a parsed TOML dict.

    Accepts both the modern ``[companies.X]`` form and the legacy singular
    ``[company]`` block (which becomes ``companies["default"]``).
    """
    companies: dict[str, CompanyConfig] = {}
    for key, comp in (data.get("companies") or {}).items():
        companies[key] = _company_from_dict(key, comp)
    if not companies:
        legacy = data.get("company")
        if legacy:
            companies["default"] = _company_from_dict("default", legacy)

    default_entity = data.get("default_entity") or ""
    if not default_entity and companies:
        default_entity = next(iter(companies))
    company = companies.get(default_entity) if companies else None
    if company is None and companies:
        company = next(iter(companies.values()))
    if company is None:
        company = CompanyConfig(name="", key="default")

    clients: dict[str, ClientConfig] = {}
    for key, raw in (data.get("clients") or {}).items():
        invoicing_block = raw.get("invoicing", {}) or {}
        clients[key] = ClientConfig(
            key=key,
            name=raw.get("name", key),
            address=raw.get("address", ""),
            email=raw.get("email", ""),
            terms=raw.get("terms", 30),
            ar_account=raw.get("ar_account", ""),
            entity=raw.get("entity", ""),
            schedule=invoicing_block.get("schedule", "on-demand"),
            schedule_day=invoicing_block.get("day", 1),
            reminder_days=invoicing_block.get("reminder_days", 3),
            notifications=invoicing_block.get("notifications", ""),
            days_until_overdue=invoicing_block.get("days_until_overdue", 0),
            ledger_posting=invoicing_block.get("ledger_posting", True),
            bundles=list(invoicing_block.get("bundles", []) or []),
            separate=list(invoicing_block.get("separate", []) or []),
        )

    services: dict[str, ServiceConfig] = {}
    for key, svc in (data.get("services") or {}).items():
        services[key] = ServiceConfig(
            key=key,
            display_name=svc.get("display_name", key),
            rate=float(svc.get("rate", 0)),
            type=svc.get("type", "hours"),
            income_account=svc.get("income_account", ""),
        )

    return InvoicingConfig(
        accounting_path=data.get("accounting_path", ""),
        invoice_output=data.get("invoice_output", "invoices/generated"),
        next_invoice_number=data.get("next_invoice_number", 1),
        company=company,
        clients=clients,
        services=services,
        default_ar_account=data.get(
            "default_ar_account", "Assets:Accounts-Receivable",
        ),
        default_bank_account=data.get(
            "default_bank_account", "Assets:Bank:Checking",
        ),
        currency=data.get("currency", "USD"),
        companies=companies,
        default_entity=default_entity or "default",
        notifications=data.get("notifications", ""),
        days_until_overdue=data.get("days_until_overdue", 0),
    )


def _company_from_dict(key: str, raw: dict) -> CompanyConfig:
    return CompanyConfig(
        name=raw.get("name", ""),
        address=raw.get("address", ""),
        email=raw.get("email", ""),
        payment_instructions=raw.get("payment_instructions", ""),
        logo=raw.get("logo", ""),
        key=key,
        ar_account=raw.get("ar_account", ""),
        bank_account=raw.get("bank_account", ""),
        currency=raw.get("currency", ""),
    )


def invoicing_to_toml_dict(cfg: InvoicingConfig) -> dict:
    """Render :class:`InvoicingConfig` back to a TOML-shaped dict.

    Determinism: alphabetical key ordering, companies/clients/services
    emitted in lexical key order. Values matching the dataclass default
    are still emitted when they differ from the empty defaults the user
    is likely to have set.
    """
    out: dict[str, Any] = {}
    if cfg.accounting_path:
        out["accounting_path"] = cfg.accounting_path
    if cfg.invoice_output and cfg.invoice_output != "invoices/generated":
        out["invoice_output"] = cfg.invoice_output
    if cfg.next_invoice_number and cfg.next_invoice_number != 1:
        out["next_invoice_number"] = cfg.next_invoice_number
    if cfg.default_entity and cfg.default_entity != "default":
        out["default_entity"] = cfg.default_entity
    if cfg.default_ar_account and cfg.default_ar_account != "Assets:Accounts-Receivable":
        out["default_ar_account"] = cfg.default_ar_account
    if cfg.default_bank_account and cfg.default_bank_account != "Assets:Bank:Checking":
        out["default_bank_account"] = cfg.default_bank_account
    if cfg.currency and cfg.currency != "USD":
        out["currency"] = cfg.currency
    if cfg.notifications:
        out["notifications"] = cfg.notifications
    if cfg.days_until_overdue:
        out["days_until_overdue"] = cfg.days_until_overdue

    if cfg.companies:
        out["companies"] = {
            k: _company_to_dict(c)
            for k, c in sorted(cfg.companies.items())
        }
    if cfg.clients:
        out["clients"] = {
            k: _client_to_dict(c)
            for k, c in sorted(cfg.clients.items())
        }
    if cfg.services:
        out["services"] = {
            k: _service_to_dict(s)
            for k, s in sorted(cfg.services.items())
        }
    return out


def _company_to_dict(c: CompanyConfig) -> dict:
    out: dict[str, Any] = {"name": c.name}
    for attr in ("address", "email", "payment_instructions", "logo",
                 "ar_account", "bank_account", "currency"):
        v = getattr(c, attr)
        if v:
            out[attr] = v
    return out


def _client_to_dict(c: ClientConfig) -> dict:
    out: dict[str, Any] = {"name": c.name}
    for attr in ("address", "email", "ar_account", "entity"):
        v = getattr(c, attr)
        if v:
            out[attr] = v
    if c.terms not in ("", 30, None):
        out["terms"] = c.terms

    inv: dict[str, Any] = {}
    if c.schedule and c.schedule != "on-demand":
        inv["schedule"] = c.schedule
    if c.schedule_day and c.schedule_day != 1:
        inv["day"] = c.schedule_day
    if c.reminder_days and c.reminder_days != 3:
        inv["reminder_days"] = c.reminder_days
    if c.notifications:
        inv["notifications"] = c.notifications
    if c.days_until_overdue:
        inv["days_until_overdue"] = c.days_until_overdue
    if not c.ledger_posting:
        inv["ledger_posting"] = False
    if c.bundles:
        inv["bundles"] = list(c.bundles)
    if c.separate:
        inv["separate"] = list(c.separate)
    if inv:
        out["invoicing"] = inv
    return out


def _service_to_dict(s: ServiceConfig) -> dict:
    out: dict[str, Any] = {
        "display_name": s.display_name,
        "rate": s.rate,
    }
    if s.type and s.type != "hours":
        out["type"] = s.type
    if s.income_account:
        out["income_account"] = s.income_account
    return out


def load_invoicing(db_path: Path | str) -> InvoicingConfig:
    """Load :class:`InvoicingConfig` from the DB."""
    init_db(db_path)
    with _connect(db_path) as conn:
        scalars = _kv_all(conn, "invoicing_settings")

        companies: dict[str, CompanyConfig] = {}
        for row in conn.execute(
            "SELECT * FROM invoicing_companies ORDER BY key"
        ).fetchall():
            companies[row["key"]] = CompanyConfig(
                name=row["name"] or "",
                address=row["address"] or "",
                email=row["email"] or "",
                payment_instructions=row["payment_instructions"] or "",
                logo=row["logo"] or "",
                key=row["key"],
                ar_account=row["ar_account"] or "",
                bank_account=row["bank_account"] or "",
                currency=row["currency"] or "",
            )

        clients: dict[str, ClientConfig] = {}
        for row in conn.execute(
            "SELECT * FROM invoicing_clients ORDER BY key"
        ).fetchall():
            terms_raw = row["terms"]
            terms: int | str
            if terms_raw is None:
                terms = 30
            else:
                try:
                    terms = int(terms_raw)
                except (TypeError, ValueError):
                    terms = terms_raw
            clients[row["key"]] = ClientConfig(
                key=row["key"],
                name=row["name"] or row["key"],
                address=row["address"] or "",
                email=row["email"] or "",
                terms=terms,
                ar_account=row["ar_account"] or "",
                entity=row["entity"] or "",
                schedule=row["schedule"] or "on-demand",
                schedule_day=row["schedule_day"] if row["schedule_day"] is not None else 1,
                reminder_days=row["reminder_days"] if row["reminder_days"] is not None else 3,
                notifications=row["notifications"] or "",
                days_until_overdue=row["days_until_overdue"] or 0,
                ledger_posting=bool(row["ledger_posting"]),
                bundles=json.loads(row["bundles_json"] or "[]"),
                separate=json.loads(row["separate_json"] or "[]"),
            )

        services: dict[str, ServiceConfig] = {}
        for row in conn.execute(
            "SELECT * FROM invoicing_services ORDER BY key"
        ).fetchall():
            services[row["key"]] = ServiceConfig(
                key=row["key"],
                display_name=row["display_name"] or row["key"],
                rate=float(row["rate"] or 0),
                type=row["type"] or "hours",
                income_account=row["income_account"] or "",
            )

    default_entity = scalars.get("default_entity") or ""
    if not default_entity and companies:
        default_entity = next(iter(companies))
    company = companies.get(default_entity) if default_entity else None
    if company is None and companies:
        company = next(iter(companies.values()))
    if company is None:
        company = CompanyConfig(name="", key="default")

    return InvoicingConfig(
        accounting_path=scalars.get("accounting_path", ""),
        invoice_output=scalars.get("invoice_output", "invoices/generated"),
        next_invoice_number=scalars.get("next_invoice_number", 1),
        company=company,
        clients=clients,
        services=services,
        default_ar_account=scalars.get(
            "default_ar_account", "Assets:Accounts-Receivable",
        ),
        default_bank_account=scalars.get(
            "default_bank_account", "Assets:Bank:Checking",
        ),
        currency=scalars.get("currency", "USD"),
        companies=companies,
        default_entity=default_entity or "default",
        notifications=scalars.get("notifications", ""),
        days_until_overdue=scalars.get("days_until_overdue", 0),
    )


def save_invoicing(
    db_path: Path | str,
    cfg: InvoicingConfig,
    *,
    replace_collections: bool = True,
) -> None:
    """Save :class:`InvoicingConfig` to the DB.

    With ``replace_collections=True`` (default), the companies/clients/
    services tables are truncated before insert — matching ``--replace``
    semantics. With ``False``, rows are upserted by key (merge semantics).
    Scalar settings are always upserted.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        for key in _INVOICING_SCALAR_KEYS:
            value = _invoicing_scalar(cfg, key)
            if value is None:
                _kv_delete(conn, "invoicing_settings", key)
            else:
                _kv_set(conn, "invoicing_settings", key, value)

        if replace_collections:
            conn.execute("DELETE FROM invoicing_companies")
            conn.execute("DELETE FROM invoicing_clients")
            conn.execute("DELETE FROM invoicing_services")
        for key, comp in cfg.companies.items():
            _upsert_company_row(conn, key, comp)
        for key, client in cfg.clients.items():
            _upsert_client_row(conn, key, client)
        for key, svc in cfg.services.items():
            _upsert_service_row(conn, key, svc)


def set_next_invoice_number(db_path: Path | str, new_number: int) -> None:
    """Persist just the ``next_invoice_number`` scalar to the DB.

    A targeted update so the invoice generator can advance the counter without
    rewriting the whole invoicing config (which would also truncate/replace the
    companies/clients/services tables).
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        _kv_set(conn, "invoicing_settings", "next_invoice_number", new_number)


def _invoicing_scalar(cfg: InvoicingConfig, key: str) -> Any:
    if key == "accounting_path":
        return cfg.accounting_path or None
    if key == "invoice_output":
        return cfg.invoice_output or None
    if key == "next_invoice_number":
        return cfg.next_invoice_number
    if key == "default_entity":
        return cfg.default_entity or None
    if key == "currency":
        return cfg.currency or None
    if key == "default_ar_account":
        return cfg.default_ar_account or None
    if key == "default_bank_account":
        return cfg.default_bank_account or None
    if key == "notifications":
        return cfg.notifications or None
    if key == "days_until_overdue":
        return cfg.days_until_overdue or None
    return None


def _upsert_company_row(conn: sqlite3.Connection, key: str, c: CompanyConfig) -> None:
    conn.execute(
        """
        INSERT INTO invoicing_companies(
            key, name, address, email, payment_instructions, logo,
            ar_account, bank_account, currency
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            name = excluded.name,
            address = excluded.address,
            email = excluded.email,
            payment_instructions = excluded.payment_instructions,
            logo = excluded.logo,
            ar_account = excluded.ar_account,
            bank_account = excluded.bank_account,
            currency = excluded.currency
        """,
        (key, c.name, c.address, c.email, c.payment_instructions, c.logo,
         c.ar_account, c.bank_account, c.currency),
    )


def _upsert_client_row(conn: sqlite3.Connection, key: str, c: ClientConfig) -> None:
    conn.execute(
        """
        INSERT INTO invoicing_clients(
            key, name, address, email, terms, ar_account, entity,
            schedule, schedule_day, reminder_days, notifications,
            days_until_overdue, ledger_posting, bundles_json, separate_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            name = excluded.name,
            address = excluded.address,
            email = excluded.email,
            terms = excluded.terms,
            ar_account = excluded.ar_account,
            entity = excluded.entity,
            schedule = excluded.schedule,
            schedule_day = excluded.schedule_day,
            reminder_days = excluded.reminder_days,
            notifications = excluded.notifications,
            days_until_overdue = excluded.days_until_overdue,
            ledger_posting = excluded.ledger_posting,
            bundles_json = excluded.bundles_json,
            separate_json = excluded.separate_json
        """,
        (
            key, c.name, c.address, c.email, str(c.terms),
            c.ar_account, c.entity,
            c.schedule, c.schedule_day, c.reminder_days, c.notifications,
            c.days_until_overdue, 1 if c.ledger_posting else 0,
            json.dumps(c.bundles), json.dumps(c.separate),
        ),
    )


def _upsert_service_row(conn: sqlite3.Connection, key: str, s: ServiceConfig) -> None:
    conn.execute(
        """
        INSERT INTO invoicing_services(
            key, display_name, rate, type, income_account
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            display_name = excluded.display_name,
            rate = excluded.rate,
            type = excluded.type,
            income_account = excluded.income_account
        """,
        (key, s.display_name, s.rate, s.type, s.income_account),
    )


# Granular ops -----------------------------------------------------------------


def list_companies(db_path: Path | str) -> list[CompanyConfig]:
    return list(load_invoicing(db_path).companies.values())


def upsert_company(
    db_path: Path | str, key: str, **fields: Any,
) -> tuple[CompanyConfig, str]:
    init_db(db_path)
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM invoicing_companies WHERE key = ?", (key,),
        ).fetchone()
        merged = {
            "name": "", "address": "", "email": "", "payment_instructions": "",
            "logo": "", "ar_account": "", "bank_account": "", "currency": "",
        }
        if existing is not None:
            for col in merged:
                merged[col] = existing[col] or ""
        merged.update({k: v for k, v in fields.items() if v is not None})
        comp = CompanyConfig(
            key=key,
            name=merged["name"] or "",
            address=merged["address"] or "",
            email=merged["email"] or "",
            payment_instructions=merged["payment_instructions"] or "",
            logo=merged["logo"] or "",
            ar_account=merged["ar_account"] or "",
            bank_account=merged["bank_account"] or "",
            currency=merged["currency"] or "",
        )
        _upsert_company_row(conn, key, comp)
        if existing is None:
            return comp, "created"
        unchanged = all(
            (existing[col] or "") == (merged[col] or "") for col in merged
        )
        return comp, ("noop" if unchanged else "updated")


def delete_company(db_path: Path | str, key: str) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM invoicing_companies WHERE key = ?", (key,),
        )
        return cur.rowcount > 0


def upsert_client(
    db_path: Path | str, key: str, **fields: Any,
) -> tuple[ClientConfig, str]:
    init_db(db_path)
    defaults: dict[str, Any] = {
        "name": key, "address": "", "email": "", "terms": 30,
        "ar_account": "", "entity": "",
        "schedule": "on-demand", "schedule_day": 1, "reminder_days": 3,
        "notifications": "", "days_until_overdue": 0,
        "ledger_posting": True,
        "bundles": [], "separate": [],
    }
    with _connect(db_path) as conn:
        existing_row = conn.execute(
            "SELECT * FROM invoicing_clients WHERE key = ?", (key,),
        ).fetchone()
        if existing_row is not None:
            terms_raw = existing_row["terms"]
            try:
                terms = int(terms_raw) if terms_raw is not None else 30
            except (TypeError, ValueError):
                terms = terms_raw
            defaults.update({
                "name": existing_row["name"] or key,
                "address": existing_row["address"] or "",
                "email": existing_row["email"] or "",
                "terms": terms,
                "ar_account": existing_row["ar_account"] or "",
                "entity": existing_row["entity"] or "",
                "schedule": existing_row["schedule"] or "on-demand",
                "schedule_day": existing_row["schedule_day"] or 1,
                "reminder_days": existing_row["reminder_days"] or 3,
                "notifications": existing_row["notifications"] or "",
                "days_until_overdue": existing_row["days_until_overdue"] or 0,
                "ledger_posting": bool(existing_row["ledger_posting"]),
                "bundles": json.loads(existing_row["bundles_json"] or "[]"),
                "separate": json.loads(existing_row["separate_json"] or "[]"),
            })
        merged = dict(defaults)
        for k, v in fields.items():
            if v is None:
                continue
            merged[k] = v
        client = ClientConfig(
            key=key,
            name=merged["name"],
            address=merged["address"],
            email=merged["email"],
            terms=merged["terms"],
            ar_account=merged["ar_account"],
            entity=merged["entity"],
            schedule=merged["schedule"],
            schedule_day=merged["schedule_day"],
            reminder_days=merged["reminder_days"],
            notifications=merged["notifications"],
            days_until_overdue=merged["days_until_overdue"],
            ledger_posting=bool(merged["ledger_posting"]),
            bundles=list(merged["bundles"]),
            separate=list(merged["separate"]),
        )
        _upsert_client_row(conn, key, client)
        if existing_row is None:
            return client, "created"
        return client, ("noop" if merged == defaults else "updated")


def delete_client(db_path: Path | str, key: str) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM invoicing_clients WHERE key = ?", (key,),
        )
        return cur.rowcount > 0


def upsert_service(
    db_path: Path | str, key: str, **fields: Any,
) -> tuple[ServiceConfig, str]:
    init_db(db_path)
    defaults: dict[str, Any] = {
        "display_name": key, "rate": 0.0, "type": "hours", "income_account": "",
    }
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM invoicing_services WHERE key = ?", (key,),
        ).fetchone()
        if existing is not None:
            defaults.update({
                "display_name": existing["display_name"] or key,
                "rate": float(existing["rate"] or 0),
                "type": existing["type"] or "hours",
                "income_account": existing["income_account"] or "",
            })
        merged = dict(defaults)
        for k, v in fields.items():
            if v is None:
                continue
            merged[k] = v
        svc = ServiceConfig(
            key=key,
            display_name=merged["display_name"],
            rate=float(merged["rate"]),
            type=merged["type"],
            income_account=merged["income_account"],
        )
        _upsert_service_row(conn, key, svc)
        if existing is None:
            return svc, "created"
        return svc, ("noop" if merged == defaults else "updated")


def delete_service(db_path: Path | str, key: str) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM invoicing_services WHERE key = ?", (key,),
        )
        return cur.rowcount > 0


# =============================================================================
# Tax
# =============================================================================


_TAX_SCALAR_KEYS = (
    "filing_status",
    "tax_year",
    "w2.income",
    "w2.federal_withholding",
    "w2.state_withholding",
    "estimated_payments.federal",
    "estimated_payments.state",
    "options.enable_qbi_deduction",
    "safe_harbor.prior_year_federal_tax",
    "safe_harbor.prior_year_state_tax",
)


def tax_config_from_toml_dict(data: dict) -> TaxConfig:
    """Hydrate :class:`TaxConfig` from a parsed TOML dict.

    Accepts both the wrapped ``[tax]`` form and a flat top-level dict.
    """
    tax = data.get("tax", data)
    w2 = tax.get("w2", {}) or {}
    options = tax.get("options", {}) or {}
    accounts = tax.get("accounts", {}) or {}
    safe_harbor = tax.get("safe_harbor", {}) or {}
    estimated = tax.get("estimated_payments", {}) or {}
    rates = tax.get("rates", {}) or {}

    return TaxConfig(
        filing_status=tax.get("filing_status", "mfj"),
        tax_year=tax.get("tax_year", 2026),
        w2_income=w2.get("income", 0),
        w2_federal_withholding=w2.get("federal_withholding", 0),
        w2_state_withholding=w2.get("state_withholding", 0),
        federal_estimated_paid=estimated.get("federal", 0),
        state_estimated_paid=estimated.get("state", 0),
        enable_qbi_deduction=options.get("enable_qbi_deduction", False),
        se_income_accounts=list(accounts.get("se_income", ["Income:ScheduleC"])),
        se_expense_accounts=list(accounts.get("se_expenses", ["Expenses:Business"])),
        prior_year_federal_tax=safe_harbor.get("prior_year_federal_tax", 0),
        prior_year_state_tax=safe_harbor.get("prior_year_state_tax", 0),
        federal_brackets=rates.get("federal_brackets"),
        ca_brackets=rates.get("ca_brackets"),
        federal_standard_deduction=rates.get("federal_standard_deduction"),
        ca_standard_deduction=rates.get("ca_standard_deduction"),
        ss_wage_base=rates.get("ss_wage_base"),
        ss_rate=rates.get("ss_rate"),
        medicare_rate=rates.get("medicare_rate"),
        se_taxable_fraction=rates.get("se_taxable_fraction"),
    )


def tax_to_toml_dict(cfg: TaxConfig) -> dict:
    """Render :class:`TaxConfig` back to a TOML-shaped dict (with ``[tax]``)."""
    tax: dict[str, Any] = {
        "filing_status": cfg.filing_status,
        "tax_year": cfg.tax_year,
    }
    w2: dict[str, Any] = {}
    if cfg.w2_income:
        w2["income"] = cfg.w2_income
    if cfg.w2_federal_withholding:
        w2["federal_withholding"] = cfg.w2_federal_withholding
    if cfg.w2_state_withholding:
        w2["state_withholding"] = cfg.w2_state_withholding
    if w2:
        tax["w2"] = w2

    estimated: dict[str, Any] = {}
    if cfg.federal_estimated_paid:
        estimated["federal"] = cfg.federal_estimated_paid
    if cfg.state_estimated_paid:
        estimated["state"] = cfg.state_estimated_paid
    if estimated:
        tax["estimated_payments"] = estimated

    if cfg.enable_qbi_deduction:
        tax["options"] = {"enable_qbi_deduction": True}

    accounts: dict[str, Any] = {}
    if cfg.se_income_accounts and cfg.se_income_accounts != ["Income:ScheduleC"]:
        accounts["se_income"] = list(cfg.se_income_accounts)
    if cfg.se_expense_accounts and cfg.se_expense_accounts != ["Expenses:Business"]:
        accounts["se_expenses"] = list(cfg.se_expense_accounts)
    if accounts:
        tax["accounts"] = accounts

    safe_harbor: dict[str, Any] = {}
    if cfg.prior_year_federal_tax:
        safe_harbor["prior_year_federal_tax"] = cfg.prior_year_federal_tax
    if cfg.prior_year_state_tax:
        safe_harbor["prior_year_state_tax"] = cfg.prior_year_state_tax
    if safe_harbor:
        tax["safe_harbor"] = safe_harbor

    rates: dict[str, Any] = {}
    if cfg.ss_wage_base is not None:
        rates["ss_wage_base"] = cfg.ss_wage_base
    if cfg.ss_rate is not None:
        rates["ss_rate"] = cfg.ss_rate
    if cfg.medicare_rate is not None:
        rates["medicare_rate"] = cfg.medicare_rate
    if cfg.se_taxable_fraction is not None:
        rates["se_taxable_fraction"] = cfg.se_taxable_fraction
    if cfg.federal_standard_deduction is not None:
        rates["federal_standard_deduction"] = cfg.federal_standard_deduction
    if cfg.ca_standard_deduction is not None:
        rates["ca_standard_deduction"] = cfg.ca_standard_deduction
    if cfg.federal_brackets is not None:
        rates["federal_brackets"] = [list(b) for b in cfg.federal_brackets]
    if cfg.ca_brackets is not None:
        rates["ca_brackets"] = [list(b) for b in cfg.ca_brackets]
    if rates:
        tax["rates"] = rates

    return {"tax": tax}


def load_tax(db_path: Path | str) -> TaxConfig:
    """Load :class:`TaxConfig` from the DB."""
    init_db(db_path)
    with _connect(db_path) as conn:
        scalars = _kv_all(conn, "tax_settings")
        patterns = {"se_income": [], "se_expense": []}
        for row in conn.execute(
            "SELECT kind, pattern FROM tax_account_patterns ORDER BY kind, pattern"
        ).fetchall():
            patterns.setdefault(row["kind"], []).append(row["pattern"])

        tax_year = int(scalars.get("tax_year", 2026))
        rate_row = conn.execute(
            "SELECT * FROM tax_year_rates WHERE tax_year = ?", (tax_year,),
        ).fetchone()

    # When the DB has no patterns, return empty lists rather than baking in
    # the heuristic defaults (`["Income:ScheduleC"]` etc). Otherwise a
    # round-trip ``load_tax → save_tax`` of an empty DB would inject those
    # defaults into the patterns table and falsely flag the section as
    # "DB-populated", blocking the legacy migration.
    se_income = patterns.get("se_income") or []
    se_expense = patterns.get("se_expense") or []

    fed_brackets = None
    ca_brackets = None
    fed_std_ded = None
    ca_std_ded = None
    ss_wage_base = None
    ss_rate = None
    medicare_rate = None
    se_taxable_fraction = None
    if rate_row is not None:
        ss_wage_base = rate_row["ss_wage_base"]
        ss_rate = rate_row["ss_rate"]
        medicare_rate = rate_row["medicare_rate"]
        se_taxable_fraction = rate_row["se_taxable_fraction"]
        fed_std_ded = rate_row["federal_standard_deduction"]
        ca_std_ded = rate_row["ca_standard_deduction"]
        if rate_row["federal_brackets_json"]:
            fed_brackets = json.loads(rate_row["federal_brackets_json"])
        if rate_row["ca_brackets_json"]:
            ca_brackets = json.loads(rate_row["ca_brackets_json"])

    return TaxConfig(
        filing_status=scalars.get("filing_status", "mfj"),
        tax_year=tax_year,
        w2_income=scalars.get("w2.income", 0),
        w2_federal_withholding=scalars.get("w2.federal_withholding", 0),
        w2_state_withholding=scalars.get("w2.state_withholding", 0),
        federal_estimated_paid=scalars.get("estimated_payments.federal", 0),
        state_estimated_paid=scalars.get("estimated_payments.state", 0),
        enable_qbi_deduction=scalars.get("options.enable_qbi_deduction", False),
        se_income_accounts=list(se_income),
        se_expense_accounts=list(se_expense),
        prior_year_federal_tax=scalars.get("safe_harbor.prior_year_federal_tax", 0),
        prior_year_state_tax=scalars.get("safe_harbor.prior_year_state_tax", 0),
        federal_brackets=fed_brackets,
        ca_brackets=ca_brackets,
        federal_standard_deduction=fed_std_ded,
        ca_standard_deduction=ca_std_ded,
        ss_wage_base=ss_wage_base,
        ss_rate=ss_rate,
        medicare_rate=medicare_rate,
        se_taxable_fraction=se_taxable_fraction,
    )


def save_tax(
    db_path: Path | str,
    cfg: TaxConfig,
    *,
    replace_collections: bool = True,
) -> None:
    """Save :class:`TaxConfig` to the DB."""
    init_db(db_path)
    with _connect(db_path) as conn:
        for key in _TAX_SCALAR_KEYS:
            value = _tax_scalar(cfg, key)
            if value is None or value == 0 or value is False:
                # Don't write zero/false defaults — keep the DB lean.
                _kv_delete(conn, "tax_settings", key)
            else:
                _kv_set(conn, "tax_settings", key, value)
        # Always write filing_status + tax_year (load needs them).
        _kv_set(conn, "tax_settings", "filing_status", cfg.filing_status)
        _kv_set(conn, "tax_settings", "tax_year", cfg.tax_year)

        if replace_collections:
            conn.execute("DELETE FROM tax_account_patterns")
        for p in cfg.se_income_accounts or []:
            conn.execute(
                "INSERT OR IGNORE INTO tax_account_patterns(kind, pattern) "
                "VALUES (?, ?)",
                ("se_income", p),
            )
        for p in cfg.se_expense_accounts or []:
            conn.execute(
                "INSERT OR IGNORE INTO tax_account_patterns(kind, pattern) "
                "VALUES (?, ?)",
                ("se_expense", p),
            )

        if any(v is not None for v in (
            cfg.ss_wage_base, cfg.ss_rate, cfg.medicare_rate,
            cfg.se_taxable_fraction, cfg.federal_standard_deduction,
            cfg.ca_standard_deduction, cfg.federal_brackets, cfg.ca_brackets,
        )):
            _upsert_year_rates(conn, cfg.tax_year, cfg)


def _tax_scalar(cfg: TaxConfig, key: str) -> Any:
    if key == "filing_status":
        return cfg.filing_status
    if key == "tax_year":
        return cfg.tax_year
    if key == "w2.income":
        return cfg.w2_income
    if key == "w2.federal_withholding":
        return cfg.w2_federal_withholding
    if key == "w2.state_withholding":
        return cfg.w2_state_withholding
    if key == "estimated_payments.federal":
        return cfg.federal_estimated_paid
    if key == "estimated_payments.state":
        return cfg.state_estimated_paid
    if key == "options.enable_qbi_deduction":
        return cfg.enable_qbi_deduction
    if key == "safe_harbor.prior_year_federal_tax":
        return cfg.prior_year_federal_tax
    if key == "safe_harbor.prior_year_state_tax":
        return cfg.prior_year_state_tax
    return None


def _upsert_year_rates(
    conn: sqlite3.Connection, year: int, cfg: TaxConfig,
) -> None:
    fed_brackets_json = (
        json.dumps([list(b) for b in cfg.federal_brackets])
        if cfg.federal_brackets is not None else None
    )
    ca_brackets_json = (
        json.dumps([list(b) for b in cfg.ca_brackets])
        if cfg.ca_brackets is not None else None
    )
    conn.execute(
        """
        INSERT INTO tax_year_rates(
            tax_year, ss_wage_base, ss_rate, medicare_rate, se_taxable_fraction,
            federal_standard_deduction, ca_standard_deduction,
            federal_brackets_json, ca_brackets_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tax_year) DO UPDATE SET
            ss_wage_base = excluded.ss_wage_base,
            ss_rate = excluded.ss_rate,
            medicare_rate = excluded.medicare_rate,
            se_taxable_fraction = excluded.se_taxable_fraction,
            federal_standard_deduction = excluded.federal_standard_deduction,
            ca_standard_deduction = excluded.ca_standard_deduction,
            federal_brackets_json = excluded.federal_brackets_json,
            ca_brackets_json = excluded.ca_brackets_json
        """,
        (year, cfg.ss_wage_base, cfg.ss_rate, cfg.medicare_rate,
         cfg.se_taxable_fraction, cfg.federal_standard_deduction,
         cfg.ca_standard_deduction, fed_brackets_json, ca_brackets_json),
    )


def add_tax_pattern(db_path: Path | str, kind: str, pattern: str) -> str:
    """Add an SE account pattern. Returns 'created' or 'noop'."""
    init_db(db_path)
    if kind not in ("se_income", "se_expense"):
        raise ValueError(f"unknown pattern kind: {kind}")
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO tax_account_patterns(kind, pattern) "
            "VALUES (?, ?)",
            (kind, pattern),
        )
        return "created" if cur.rowcount > 0 else "noop"


def remove_tax_pattern(db_path: Path | str, kind: str, pattern: str) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM tax_account_patterns WHERE kind = ? AND pattern = ?",
            (kind, pattern),
        )
        return cur.rowcount > 0


def replace_tax_patterns(
    db_path: Path | str, kind_to_patterns: dict[str, list[str]],
) -> None:
    """Replace-all per kind. Keys not present are left untouched."""
    init_db(db_path)
    with _connect(db_path) as conn:
        for kind, patterns in kind_to_patterns.items():
            if kind not in ("se_income", "se_expense"):
                raise ValueError(f"unknown pattern kind: {kind}")
            conn.execute(
                "DELETE FROM tax_account_patterns WHERE kind = ?", (kind,),
            )
            for p in patterns or []:
                conn.execute(
                    "INSERT OR IGNORE INTO tax_account_patterns(kind, pattern) "
                    "VALUES (?, ?)", (kind, p),
                )


def list_tax_patterns(db_path: Path | str) -> dict[str, list[str]]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT kind, pattern FROM tax_account_patterns ORDER BY kind, pattern"
        ).fetchall()
    out: dict[str, list[str]] = {"se_income": [], "se_expense": []}
    for row in rows:
        out.setdefault(row["kind"], []).append(row["pattern"])
    return out


def list_tax_year_rates(db_path: Path | str) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM tax_year_rates ORDER BY tax_year"
        ).fetchall()
    out = []
    for row in rows:
        out.append({
            "tax_year": row["tax_year"],
            "ss_wage_base": row["ss_wage_base"],
            "ss_rate": row["ss_rate"],
            "medicare_rate": row["medicare_rate"],
            "se_taxable_fraction": row["se_taxable_fraction"],
            "federal_standard_deduction": row["federal_standard_deduction"],
            "ca_standard_deduction": row["ca_standard_deduction"],
            "federal_brackets": (
                json.loads(row["federal_brackets_json"])
                if row["federal_brackets_json"] else None
            ),
            "ca_brackets": (
                json.loads(row["ca_brackets_json"])
                if row["ca_brackets_json"] else None
            ),
        })
    return out


def upsert_tax_year_rates(db_path: Path | str, year: int, **fields: Any) -> str:
    """Upsert a single ``tax_year_rates`` row. Returns 'created'/'updated'/'noop'."""
    init_db(db_path)
    allowed = {
        "ss_wage_base", "ss_rate", "medicare_rate", "se_taxable_fraction",
        "federal_standard_deduction", "ca_standard_deduction",
        "federal_brackets", "ca_brackets",
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown tax_year_rates fields: {sorted(bad)}")
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM tax_year_rates WHERE tax_year = ?", (year,),
        ).fetchone()
        merged: dict[str, Any] = {k: None for k in allowed}
        if existing is not None:
            for k in allowed:
                if k in ("federal_brackets", "ca_brackets"):
                    merged[k] = (
                        json.loads(existing[f"{k}_json"])
                        if existing[f"{k}_json"] else None
                    )
                else:
                    merged[k] = existing[k]
        before = dict(merged)
        for k, v in fields.items():
            if v is None:
                continue
            merged[k] = v
        fed_brackets_json = (
            json.dumps([list(b) for b in merged["federal_brackets"]])
            if merged["federal_brackets"] is not None else None
        )
        ca_brackets_json = (
            json.dumps([list(b) for b in merged["ca_brackets"]])
            if merged["ca_brackets"] is not None else None
        )
        conn.execute(
            """
            INSERT INTO tax_year_rates(
                tax_year, ss_wage_base, ss_rate, medicare_rate, se_taxable_fraction,
                federal_standard_deduction, ca_standard_deduction,
                federal_brackets_json, ca_brackets_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tax_year) DO UPDATE SET
                ss_wage_base = excluded.ss_wage_base,
                ss_rate = excluded.ss_rate,
                medicare_rate = excluded.medicare_rate,
                se_taxable_fraction = excluded.se_taxable_fraction,
                federal_standard_deduction = excluded.federal_standard_deduction,
                ca_standard_deduction = excluded.ca_standard_deduction,
                federal_brackets_json = excluded.federal_brackets_json,
                ca_brackets_json = excluded.ca_brackets_json
            """,
            (year, merged["ss_wage_base"], merged["ss_rate"],
             merged["medicare_rate"], merged["se_taxable_fraction"],
             merged["federal_standard_deduction"], merged["ca_standard_deduction"],
             fed_brackets_json, ca_brackets_json),
        )
    if existing is None:
        return "created"
    return "noop" if before == merged else "updated"


def delete_tax_year_rates(db_path: Path | str, year: int) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM tax_year_rates WHERE tax_year = ?", (year,),
        )
        return cur.rowcount > 0


# =============================================================================
# Monarch
# =============================================================================


def monarch_config_from_toml_dict(
    data: dict, secrets: dict | None = None,
) -> MonarchConfig:
    """Hydrate :class:`MonarchConfig` from a parsed TOML dict.

    ``secrets`` is the optional credentials overlay (typically pulled from
    the encrypted ``secrets`` table).
    """
    monarch = data.get("monarch", {}) or {}
    secret_creds = (secrets or {}).get("monarch", {}) or {}
    credentials = MonarchCredentials(
        session_id=(
            secret_creds.get("session_id") or monarch.get("session_id")
        ),
        csrftoken=(
            secret_creds.get("csrftoken") or monarch.get("csrftoken")
        ),
    )

    sync_data = monarch.get("sync", {}) or {}
    sync = MonarchSyncSettings(
        lookback_days=sync_data.get("lookback_days", 30),
        default_account=sync_data.get(
            "default_account", "Assets:Bank:Checking",
        ),
        recategorize_account=sync_data.get(
            "recategorize_account", "Expenses:Personal-Expense",
        ),
    )

    accounts = dict(monarch.get("accounts") or {})
    categories = dict(monarch.get("categories") or {})
    tags_data = monarch.get("tags", {}) or {}
    tags = MonarchTagFilters(
        include=list(tags_data.get("include", []) or []),
        exclude=list(tags_data.get("exclude", []) or []),
    )

    profiles: list[MonarchProfile] = []
    for name, raw in (monarch.get("profiles") or {}).items():
        profile_sync_data = raw.get("sync", {}) or {}
        profile_sync = MonarchSyncSettings(
            lookback_days=profile_sync_data.get(
                "lookback_days", raw.get("lookback_days", sync.lookback_days),
            ),
            default_account=raw.get(
                "default_account",
                profile_sync_data.get("default_account", sync.default_account),
            ),
            recategorize_account=raw.get(
                "recategorize_account",
                profile_sync_data.get(
                    "recategorize_account", sync.recategorize_account,
                ),
            ),
        )
        profile_tags_data = raw.get("tags", {}) or {}
        profile_tags = MonarchTagFilters(
            include=list(profile_tags_data.get("include", []) or []),
            exclude=list(profile_tags_data.get("exclude", []) or []),
        )
        profile_accounts = raw.get("accounts")
        profile_accounts = dict(profile_accounts) if profile_accounts else dict(accounts)
        profile_categories = raw.get("categories")
        profile_categories = (
            dict(profile_categories) if profile_categories else dict(categories)
        )
        profiles.append(MonarchProfile(
            name=name,
            ledger=raw.get("ledger", name),
            sync=profile_sync,
            accounts=profile_accounts,
            categories=profile_categories,
            tags=profile_tags,
        ))

    return MonarchConfig(
        credentials=credentials,
        sync=sync,
        accounts=accounts,
        categories=categories,
        tags=tags,
        profiles=profiles,
    )


def monarch_to_toml_dict(cfg: MonarchConfig) -> dict:
    """Render :class:`MonarchConfig` back to a TOML-shaped dict.

    Credentials are intentionally omitted — they live in the encrypted
    ``secrets`` table.
    """
    monarch: dict[str, Any] = {}
    sync = {
        "lookback_days": cfg.sync.lookback_days,
        "default_account": cfg.sync.default_account,
        "recategorize_account": cfg.sync.recategorize_account,
    }
    monarch["sync"] = sync

    if cfg.accounts:
        monarch["accounts"] = dict(sorted(cfg.accounts.items()))
    if cfg.categories:
        monarch["categories"] = dict(sorted(cfg.categories.items()))
    if cfg.tags.include or cfg.tags.exclude:
        tags: dict[str, Any] = {}
        if cfg.tags.include:
            tags["include"] = list(cfg.tags.include)
        if cfg.tags.exclude:
            tags["exclude"] = list(cfg.tags.exclude)
        monarch["tags"] = tags

    if cfg.profiles:
        profiles: dict[str, Any] = {}
        for p in sorted(cfg.profiles, key=lambda x: x.name):
            entry: dict[str, Any] = {"ledger": p.ledger}
            if p.sync.lookback_days != cfg.sync.lookback_days:
                entry["lookback_days"] = p.sync.lookback_days
            if p.sync.default_account != cfg.sync.default_account:
                entry["default_account"] = p.sync.default_account
            if p.sync.recategorize_account != cfg.sync.recategorize_account:
                entry["recategorize_account"] = p.sync.recategorize_account
            if p.accounts and p.accounts != cfg.accounts:
                entry["accounts"] = dict(sorted(p.accounts.items()))
            if p.categories and p.categories != cfg.categories:
                entry["categories"] = dict(sorted(p.categories.items()))
            if p.tags.include or p.tags.exclude:
                ptags: dict[str, Any] = {}
                if p.tags.include:
                    ptags["include"] = list(p.tags.include)
                if p.tags.exclude:
                    ptags["exclude"] = list(p.tags.exclude)
                entry["tags"] = ptags
            profiles[p.name] = entry
        monarch["profiles"] = profiles

    return {"monarch": monarch}


def load_monarch(
    db_path: Path | str, secrets: dict | None = None,
) -> MonarchConfig:
    """Load :class:`MonarchConfig` from the DB.

    ``secrets`` overlays credentials onto the loaded config.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        scalars = _kv_all(conn, "monarch_settings")
        sync = MonarchSyncSettings(
            lookback_days=scalars.get("sync.lookback_days", 30),
            default_account=scalars.get(
                "sync.default_account", "Assets:Bank:Checking",
            ),
            recategorize_account=scalars.get(
                "sync.recategorize_account", "Expenses:Personal-Expense",
            ),
        )

        global_accounts = _load_account_map(conn, GLOBAL_PROFILE_ID)
        global_categories = _load_category_map(conn, GLOBAL_PROFILE_ID)
        global_tags = _load_tag_filters(conn, GLOBAL_PROFILE_ID)

        profiles: list[MonarchProfile] = []
        rows = conn.execute(
            "SELECT * FROM monarch_profiles WHERE id != ? ORDER BY name",
            (GLOBAL_PROFILE_ID,),
        ).fetchall()
        for row in rows:
            pid = row["id"]
            psync = MonarchSyncSettings(
                lookback_days=row["lookback_days"] if row["lookback_days"] is not None
                else sync.lookback_days,
                default_account=row["default_account"] or sync.default_account,
                recategorize_account=row["recategorize_account"] or sync.recategorize_account,
            )
            paccounts = _load_account_map(conn, pid) or dict(global_accounts)
            pcategories = _load_category_map(conn, pid) or dict(global_categories)
            ptags = _load_tag_filters(conn, pid)
            profiles.append(MonarchProfile(
                name=row["name"],
                ledger=row["ledger"],
                sync=psync,
                accounts=paccounts,
                categories=pcategories,
                tags=ptags,
            ))

    secret_creds = (secrets or {}).get("monarch", {}) or {}
    credentials = MonarchCredentials(
        session_id=secret_creds.get("session_id"),
        csrftoken=secret_creds.get("csrftoken"),
    )

    return MonarchConfig(
        credentials=credentials,
        sync=sync,
        accounts=global_accounts,
        categories=global_categories,
        tags=global_tags,
        profiles=profiles,
    )


def _load_account_map(conn: sqlite3.Connection, profile_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT monarch_name, beancount_account FROM monarch_account_map "
        "WHERE profile_id = ? ORDER BY monarch_name",
        (profile_id,),
    ).fetchall()
    return {r["monarch_name"]: r["beancount_account"] for r in rows}


def _load_category_map(conn: sqlite3.Connection, profile_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT monarch_category, beancount_account FROM monarch_category_map "
        "WHERE profile_id = ? ORDER BY monarch_category",
        (profile_id,),
    ).fetchall()
    return {r["monarch_category"]: r["beancount_account"] for r in rows}


def _load_tag_filters(conn: sqlite3.Connection, profile_id: int) -> MonarchTagFilters:
    rows = conn.execute(
        "SELECT kind, tag FROM monarch_tag_filters WHERE profile_id = ? "
        "ORDER BY kind, tag",
        (profile_id,),
    ).fetchall()
    include = [r["tag"] for r in rows if r["kind"] == "include"]
    exclude = [r["tag"] for r in rows if r["kind"] == "exclude"]
    return MonarchTagFilters(include=include, exclude=exclude)


def save_monarch(
    db_path: Path | str,
    cfg: MonarchConfig,
    *,
    replace_collections: bool = True,
) -> None:
    """Save :class:`MonarchConfig` to the DB.

    Credentials are NOT persisted here — they belong to the encrypted
    ``secrets`` table managed by :mod:`istota.secrets_store`.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        _kv_set(conn, "monarch_settings",
                "sync.lookback_days", cfg.sync.lookback_days)
        _kv_set(conn, "monarch_settings",
                "sync.default_account", cfg.sync.default_account)
        _kv_set(conn, "monarch_settings",
                "sync.recategorize_account", cfg.sync.recategorize_account)

        if replace_collections:
            # Cascade clears child rows for global, then profiles.
            conn.execute(
                "DELETE FROM monarch_account_map WHERE profile_id = ?",
                (GLOBAL_PROFILE_ID,),
            )
            conn.execute(
                "DELETE FROM monarch_category_map WHERE profile_id = ?",
                (GLOBAL_PROFILE_ID,),
            )
            conn.execute(
                "DELETE FROM monarch_tag_filters WHERE profile_id = ?",
                (GLOBAL_PROFILE_ID,),
            )
            conn.execute(
                "DELETE FROM monarch_profiles WHERE id != ?",
                (GLOBAL_PROFILE_ID,),
            )

        _replace_account_map(conn, GLOBAL_PROFILE_ID, cfg.accounts, clear=False)
        _replace_category_map(conn, GLOBAL_PROFILE_ID, cfg.categories, clear=False)
        _replace_tag_filters(conn, GLOBAL_PROFILE_ID, cfg.tags, clear=False)

        for p in cfg.profiles:
            pid = _upsert_profile_row(conn, p, cfg.sync)
            _replace_account_map(conn, pid, p.accounts, clear=True)
            _replace_category_map(conn, pid, p.categories, clear=True)
            _replace_tag_filters(conn, pid, p.tags, clear=True)


def _upsert_profile_row(
    conn: sqlite3.Connection,
    p: MonarchProfile,
    global_sync: MonarchSyncSettings,
) -> int:
    lookback = (
        p.sync.lookback_days
        if p.sync.lookback_days != global_sync.lookback_days
        else None
    )
    default_acc = (
        p.sync.default_account
        if p.sync.default_account != global_sync.default_account
        else None
    )
    recat_acc = (
        p.sync.recategorize_account
        if p.sync.recategorize_account != global_sync.recategorize_account
        else None
    )
    conn.execute(
        """
        INSERT INTO monarch_profiles(
            name, ledger, lookback_days, default_account, recategorize_account
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            ledger = excluded.ledger,
            lookback_days = excluded.lookback_days,
            default_account = excluded.default_account,
            recategorize_account = excluded.recategorize_account
        """,
        (p.name, p.ledger, lookback, default_acc, recat_acc),
    )
    row = conn.execute(
        "SELECT id FROM monarch_profiles WHERE name = ?", (p.name,),
    ).fetchone()
    return row["id"]


def _replace_account_map(
    conn: sqlite3.Connection, profile_id: int, mapping: dict[str, str], *, clear: bool,
) -> None:
    if clear:
        conn.execute(
            "DELETE FROM monarch_account_map WHERE profile_id = ?", (profile_id,),
        )
    for monarch_name, account in (mapping or {}).items():
        conn.execute(
            "INSERT OR REPLACE INTO monarch_account_map("
            "profile_id, monarch_name, beancount_account) VALUES (?, ?, ?)",
            (profile_id, monarch_name, account),
        )


def _replace_category_map(
    conn: sqlite3.Connection, profile_id: int, mapping: dict[str, str], *, clear: bool,
) -> None:
    if clear:
        conn.execute(
            "DELETE FROM monarch_category_map WHERE profile_id = ?", (profile_id,),
        )
    for category, account in (mapping or {}).items():
        conn.execute(
            "INSERT OR REPLACE INTO monarch_category_map("
            "profile_id, monarch_category, beancount_account) VALUES (?, ?, ?)",
            (profile_id, category, account),
        )


def _replace_tag_filters(
    conn: sqlite3.Connection, profile_id: int, tags: MonarchTagFilters, *, clear: bool,
) -> None:
    if clear:
        conn.execute(
            "DELETE FROM monarch_tag_filters WHERE profile_id = ?", (profile_id,),
        )
    for tag in (tags.include or []):
        conn.execute(
            "INSERT OR IGNORE INTO monarch_tag_filters("
            "profile_id, kind, tag) VALUES (?, ?, ?)",
            (profile_id, "include", tag),
        )
    for tag in (tags.exclude or []):
        conn.execute(
            "INSERT OR IGNORE INTO monarch_tag_filters("
            "profile_id, kind, tag) VALUES (?, ?, ?)",
            (profile_id, "exclude", tag),
        )


# Granular monarch ops ---------------------------------------------------------


def _resolve_profile_id(conn: sqlite3.Connection, name: str | None) -> int:
    if name is None:
        return GLOBAL_PROFILE_ID
    row = conn.execute(
        "SELECT id FROM monarch_profiles WHERE name = ?", (name,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown monarch profile: {name}")
    return row["id"]


def list_monarch_profiles(db_path: Path | str) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, name, ledger, lookback_days, default_account, "
            "recategorize_account FROM monarch_profiles "
            "WHERE id != ? ORDER BY name", (GLOBAL_PROFILE_ID,),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_monarch_profile(
    db_path: Path | str, name: str, **fields: Any,
) -> tuple[dict, str]:
    """Upsert a monarch profile. ``ledger`` required for create."""
    init_db(db_path)
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM monarch_profiles WHERE name = ?", (name,),
        ).fetchone()
        if existing is None:
            ledger = fields.get("ledger")
            if not ledger:
                raise ValueError(f"creating profile '{name}' requires --ledger")
            conn.execute(
                "INSERT INTO monarch_profiles(name, ledger, lookback_days, "
                "default_account, recategorize_account) VALUES (?, ?, ?, ?, ?)",
                (name, ledger,
                 fields.get("lookback_days"),
                 fields.get("default_account"),
                 fields.get("recategorize_account")),
            )
            new_row = conn.execute(
                "SELECT * FROM monarch_profiles WHERE name = ?", (name,),
            ).fetchone()
            return dict(new_row), "created"
        before = dict(existing)
        merged = dict(before)
        for k in ("ledger", "lookback_days", "default_account", "recategorize_account"):
            if k in fields and fields[k] is not None:
                merged[k] = fields[k]
        conn.execute(
            "UPDATE monarch_profiles SET ledger = ?, lookback_days = ?, "
            "default_account = ?, recategorize_account = ? WHERE name = ?",
            (merged["ledger"], merged["lookback_days"],
             merged["default_account"], merged["recategorize_account"], name),
        )
        return merged, ("noop" if merged == before else "updated")


def delete_monarch_profile(db_path: Path | str, name: str) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM monarch_profiles WHERE name = ? AND id != ?",
            (name, GLOBAL_PROFILE_ID),
        )
        return cur.rowcount > 0


def set_account_map_entry(
    db_path: Path | str, profile: str | None,
    monarch_name: str, beancount_account: str,
) -> str:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        existing = conn.execute(
            "SELECT beancount_account FROM monarch_account_map "
            "WHERE profile_id = ? AND monarch_name = ?",
            (pid, monarch_name),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO monarch_account_map("
                "profile_id, monarch_name, beancount_account) VALUES (?, ?, ?)",
                (pid, monarch_name, beancount_account),
            )
            return "created"
        if existing["beancount_account"] == beancount_account:
            return "noop"
        conn.execute(
            "UPDATE monarch_account_map SET beancount_account = ? "
            "WHERE profile_id = ? AND monarch_name = ?",
            (beancount_account, pid, monarch_name),
        )
        return "updated"


def unset_account_map_entry(
    db_path: Path | str, profile: str | None, monarch_name: str,
) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        cur = conn.execute(
            "DELETE FROM monarch_account_map WHERE profile_id = ? "
            "AND monarch_name = ?", (pid, monarch_name),
        )
        return cur.rowcount > 0


def get_account_map(
    db_path: Path | str, profile: str | None,
) -> dict[str, str]:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        return _load_account_map(conn, pid)


def replace_account_map(
    db_path: Path | str, profile: str | None, mapping: dict[str, str],
) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        _replace_account_map(conn, pid, mapping, clear=True)


def set_category_map_entry(
    db_path: Path | str, profile: str | None,
    category: str, beancount_account: str,
) -> str:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        existing = conn.execute(
            "SELECT beancount_account FROM monarch_category_map "
            "WHERE profile_id = ? AND monarch_category = ?",
            (pid, category),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO monarch_category_map("
                "profile_id, monarch_category, beancount_account) "
                "VALUES (?, ?, ?)",
                (pid, category, beancount_account),
            )
            return "created"
        if existing["beancount_account"] == beancount_account:
            return "noop"
        conn.execute(
            "UPDATE monarch_category_map SET beancount_account = ? "
            "WHERE profile_id = ? AND monarch_category = ?",
            (beancount_account, pid, category),
        )
        return "updated"


def unset_category_map_entry(
    db_path: Path | str, profile: str | None, category: str,
) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        cur = conn.execute(
            "DELETE FROM monarch_category_map WHERE profile_id = ? "
            "AND monarch_category = ?", (pid, category),
        )
        return cur.rowcount > 0


def get_category_map(
    db_path: Path | str, profile: str | None,
) -> dict[str, str]:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        return _load_category_map(conn, pid)


def replace_category_map(
    db_path: Path | str, profile: str | None, mapping: dict[str, str],
) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        _replace_category_map(conn, pid, mapping, clear=True)


def add_tag_filter(
    db_path: Path | str, profile: str | None, kind: str, tag: str,
) -> str:
    init_db(db_path)
    if kind not in ("include", "exclude"):
        raise ValueError(f"unknown tag filter kind: {kind}")
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        cur = conn.execute(
            "INSERT OR IGNORE INTO monarch_tag_filters("
            "profile_id, kind, tag) VALUES (?, ?, ?)",
            (pid, kind, tag),
        )
        return "created" if cur.rowcount > 0 else "noop"


def remove_tag_filter(
    db_path: Path | str, profile: str | None, kind: str, tag: str,
) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        cur = conn.execute(
            "DELETE FROM monarch_tag_filters WHERE profile_id = ? "
            "AND kind = ? AND tag = ?", (pid, kind, tag),
        )
        return cur.rowcount > 0


def get_tag_filters(
    db_path: Path | str, profile: str | None,
) -> dict[str, list[str]]:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        tf = _load_tag_filters(conn, pid)
        return {"include": list(tf.include), "exclude": list(tf.exclude)}


def replace_tag_filters(
    db_path: Path | str, profile: str | None,
    include: list[str], exclude: list[str],
) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        pid = _resolve_profile_id(conn, profile)
        _replace_tag_filters(
            conn, pid, MonarchTagFilters(include=list(include), exclude=list(exclude)),
            clear=True,
        )


# =============================================================================
# Schema-meta helpers
# =============================================================================


def get_meta(db_path: Path | str, key: str) -> str | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (key,),
        ).fetchone()
        return row["value"] if row else None


def set_meta(db_path: Path | str, key: str, value: str) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def has_invoicing_data(db_path: Path | str) -> bool:
    """True if invoicing collection tables have any rows."""
    init_db(db_path)
    with _connect(db_path) as conn:
        for table in ("invoicing_clients", "invoicing_services",
                      "invoicing_companies"):
            row = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            if row:
                return True
    return False


def has_tax_data(db_path: Path | str) -> bool:
    """True if tax *collection* tables have any rows.

    Deliberately excludes ``tax_settings`` because ``save_tax`` always
    writes ``filing_status`` / ``tax_year`` even when the rest of the
    config is just dataclass defaults — using it as a populated-check
    would falsely lock out legacy migration after any save round-trip.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        for query in (
            "SELECT 1 FROM tax_account_patterns LIMIT 1",
            "SELECT 1 FROM tax_year_rates LIMIT 1",
        ):
            row = conn.execute(query).fetchone()
            if row:
                return True
    return False


def has_monarch_data(db_path: Path | str) -> bool:
    """True if any non-global ``monarch_profiles`` row exists.

    Excludes ``monarch_settings`` for the same reason as :func:`has_tax_data`.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM monarch_profiles WHERE id != ? LIMIT 1",
            (GLOBAL_PROFILE_ID,),
        ).fetchone()
        return row is not None
