"""Dataclasses for accounting domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


# =============================================================================
# Monarch Money configuration
# =============================================================================


@dataclass
class MonarchCredentials:
    """Credentials for Monarch Money API authentication."""
    email: str | None = None
    password: str | None = None
    session_token: str | None = None


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
    id: int | None = None  # populated when loaded from DB


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
    # Tax rates and brackets (year-specific, loaded from config)
    federal_brackets: list[list[float]] | None = None
    ca_brackets: list[list[float]] | None = None
    federal_standard_deduction: float | None = None
    ca_standard_deduction: float | None = None
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
    # State (CA)
    ca_agi: float
    ca_standard_deduction: float
    ca_taxable_income: float
    ca_tax: float
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
