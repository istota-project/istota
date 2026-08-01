"""Dataclasses for accounting domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


# =============================================================================
# Monarch Money configuration
# =============================================================================


@dataclass
class MonarchCredentials:
    """Credentials for Monarch Money API authentication.

    Monarch's API enforces Django CSRF on `/graphql`, so every request needs
    session cookies plus a matching `X-Csrftoken` header. The cookie pair is
    the only durable credential — paste once from browser DevTools and it
    lasts months on a trusted-device login.

    Programmatic email/password login is handled separately by
    ``MonarchClient.login_with_credentials`` and only ever produces these
    cookies; the plaintext credentials are never stored.
    """
    session_id: str | None = None
    csrftoken: str | None = None


@dataclass
class MonarchSyncSettings:
    """Settings for Monarch Money sync behavior."""
    lookback_days: int = 30
    default_account: str = "Assets:Bank:Checking"
    recategorize_account: str = "Expenses:Personal-Expense"


@dataclass
class MonarchTagFilters:
    """Tag-based transaction filtering."""
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class MonarchProfile:
    """Per-ledger Monarch sync profile."""
    name: str
    ledger: str  # ledger name from [[ledgers]]
    sync: MonarchSyncSettings
    accounts: dict[str, str]
    categories: dict[str, str]
    tags: MonarchTagFilters


@dataclass
class MonarchConfig:
    """Complete Monarch Money configuration from ACCOUNTING.md."""
    credentials: MonarchCredentials
    sync: MonarchSyncSettings
    accounts: dict[str, str]  # Monarch account name -> beancount account
    categories: dict[str, str]  # Monarch category -> beancount account (overrides)
    tags: MonarchTagFilters
    profiles: list[MonarchProfile] = field(default_factory=list)


# =============================================================================
# Wash sale analysis
# =============================================================================


@dataclass
class SaleTransaction:
    """Represents a sale transaction for wash sale analysis."""
    date: date
    account: str
    symbol: str
    units: float
    proceeds: float
    cost_basis: float
    gain_loss: float


@dataclass
class PurchaseTransaction:
    """Represents a purchase transaction for wash sale analysis."""
    date: date
    account: str
    symbol: str
    units: float
    cost: float


# =============================================================================
# Invoicing
# =============================================================================


@dataclass
class CompanyConfig:
    name: str
    address: str = ""
    email: str = ""
    payment_instructions: str = ""
    logo: str = ""  # path relative to accounting_path
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
    ar_account: str = ""
    entity: str = ""  # default entity for this client
    schedule: str = "on-demand"
    schedule_day: int = 1
    reminder_days: int = 3
    notifications: str = ""
    days_until_overdue: int = 0
    ledger_posting: bool = True  # post income to ledger on payment
    bundles: list[dict] = field(default_factory=list)
    separate: list[str] = field(default_factory=list)


@dataclass
class ServiceConfig:
    key: str
    display_name: str
    rate: float
    type: str = "hours"  # "hours" | "days" | "flat" | "other"
    income_account: str = ""


@dataclass
class InvoicingConfig:
    accounting_path: str
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
    notifications: str = ""
    days_until_overdue: int = 0


@dataclass
class WorkEntry:
    date: date
    client: str
    service: str
    qty: float | None = None
    amount: float | None = None
    discount: float = 0
    description: str = ""
    entity: str = ""
    invoice: str = ""
    paid_date: date | None = None
    id: int | None = None  # 1-based display index, recomputed on every load
    # Stable identity, stamped by every writer. The display index shifts
    # whenever anything is inserted before an entry, so programmatic callers
    # (the web UI) address entries by ``uid``. Empty until the backfill runs.
    uid: str = ""
    # Keys present in the TOML that the loader doesn't recognise, kept so a
    # hand-authored custom key survives an unrelated programmatic write.
    extra: dict = field(default_factory=dict)


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


# =============================================================================
# Tax estimation
# =============================================================================


@dataclass
class TaxConfig:
    """Configuration for estimated quarterly tax calculations."""
    filing_status: str = "mfj"  # "mfj" | "single"
    tax_year: int = 2026
    # Two-letter state code, or "" for no state tax. "" is a real choice — the
    # nine states with no broad-based income tax, or a user who does not want a
    # state estimate — and the taxes page drops the state column entirely for
    # it rather than showing zeros. The default is "" so a fresh user picks
    # their own state rather than inheriting California.
    state: str = ""
    # W-2 defaults (YTD values, editable in UI)
    w2_income: float = 0
    w2_federal_withholding: float = 0
    w2_state_withholding: float = 0
    # Estimated payments defaults (YTD, editable in UI)
    federal_estimated_paid: float = 0
    state_estimated_paid: float = 0
    # Options
    enable_qbi_deduction: bool = False
    # Account patterns for SE income from ledger
    se_income_accounts: list[str] = field(default_factory=lambda: ["Income:ScheduleC"])
    se_expense_accounts: list[str] = field(default_factory=lambda: ["Expenses:Business"])
    # Safe harbor: prior year total tax
    prior_year_federal_tax: float = 0
    prior_year_state_tax: float = 0
    # Rate overrides for the configured (tax_year, state, filing_status).
    # None means "use the bundled data"; these are user corrections, not the
    # shipped values. Resolution order per field is override, then bundled,
    # then absent — and absent is a real state the caller must report rather
    # than compute a zero liability from.
    federal_brackets: list[list[float]] | None = None
    state_brackets: list[list[float]] | None = None
    federal_standard_deduction: float | None = None
    state_standard_deduction: float | None = None
    ss_wage_base: float | None = None
    ss_rate: float | None = None
    medicare_rate: float | None = None
    se_taxable_fraction: float | None = None


@dataclass
class QuarterlyTaxEstimate:
    """Result of a quarterly estimated tax calculation."""
    tax_year: int
    quarter: int
    method: str  # "annualized" | "safe_harbor"
    filing_status: str
    w2_months: int
    annualization_months: int
    # Income
    se_income_ytd: float
    se_income_annualized: float
    w2_income: float
    w2_income_annualized: float
    # SE tax
    se_tax: float
    half_se_deduction: float
    additional_medicare_tax: float
    # Federal
    federal_agi: float
    federal_standard_deduction: float
    federal_taxable_income: float
    federal_tax: float
    qbi_deduction: float
    # State. The payment-side fields below (state_withholding,
    # state_estimated_paid, state_quarterly_amount) were always neutral, which
    # is what made the ca_-prefixed half of the pair incoherent rather than
    # merely dated.
    state_agi: float
    state_standard_deduction: float
    state_taxable_income: float
    state_tax: float
    # Credits / payments
    federal_withholding: float
    state_withholding: float
    federal_estimated_paid: float
    state_estimated_paid: float
    # Net due
    federal_total_liability: float
    state_total_liability: float
    federal_net_due: float
    state_net_due: float
    federal_quarterly_amount: float
    state_quarterly_amount: float
    quarters_remaining: int
    # Jurisdiction. `state_available` False with a reason is distinct from a
    # zero liability: no state selected, a state that levies no income tax, and
    # a state we ship no brackets for all produce no state figures, and the
    # page must say which rather than render zeros.
    state: str = ""
    state_name: str = ""
    state_available: bool = False
    state_unavailable_reason: str = ""
    # Provenance for the rates actually used, as plain dicts so the whole
    # estimate stays JSON-serializable by spreading `__dict__`. Keys: year,
    # requested_year, is_fallback, is_stale, overridden, source, source_url,
    # verified_on. `is_fallback` is what makes the old silent year fallback
    # visible; `state_rates` is None when there is no state to have rates.
    federal_rates: dict = field(default_factory=dict)
    state_rates: dict | None = None
