"""Estimated quarterly tax calculator.

Pure calculation module for federal and CA state estimated tax payments.
No network calls, no external APIs. Bracket data is embedded as versioned
constants, updated annually.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

from istota.money.core.models import QuarterlyTaxEstimate, TaxConfig


# =============================================================================
# Tax bracket data
# =============================================================================

# Federal brackets: (threshold, rate) tuples per (year, filing_status)
FEDERAL_BRACKETS: dict[tuple[int, str], list[tuple[float, float]]] = {
    (2025, "mfj"): [
        (0, 0.10),
        (23_850, 0.12),
        (96_950, 0.22),
        (206_700, 0.24),
        (394_600, 0.32),
        (501_050, 0.35),
        (751_600, 0.37),
    ],
    (2025, "single"): [
        (0, 0.10),
        (11_925, 0.12),
        (48_475, 0.22),
        (103_350, 0.24),
        (197_300, 0.32),
        (250_525, 0.35),
        (626_350, 0.37),
    ],
    (2026, "mfj"): [
        (0, 0.10),
        (23_850, 0.12),
        (96_950, 0.22),
        (206_700, 0.24),
        (394_600, 0.32),
        (501_050, 0.35),
        (751_600, 0.37),
    ],
    (2026, "single"): [
        (0, 0.10),
        (11_925, 0.12),
        (48_475, 0.22),
        (103_350, 0.24),
        (197_300, 0.32),
        (250_525, 0.35),
        (626_350, 0.37),
    ],
}

# CA state brackets (includes mental health surcharge at $1M+)
CA_BRACKETS: dict[tuple[int, str], list[tuple[float, float]]] = {
    (2025, "mfj"): [
        (0, 0.01),
        (21_428, 0.02),
        (50_798, 0.04),
        (80_158, 0.06),
        (111_340, 0.08),
        (140_698, 0.093),
        (721_314, 0.103),
        (865_574, 0.113),
        (1_000_000, 0.123),  # 11.3% + 1% mental health surcharge
        (1_442_628, 0.133),  # 12.3% + 1% mental health surcharge
    ],
    (2025, "single"): [
        (0, 0.01),
        (10_714, 0.02),
        (25_399, 0.04),
        (40_084, 0.06),
        (55_670, 0.08),
        (70_349, 0.093),
        (360_657, 0.103),
        (432_787, 0.113),
        (721_314, 0.123),
        (1_000_000, 0.133),  # 12.3% + 1% mental health surcharge
    ],
    (2026, "mfj"): [
        (0, 0.01),
        (21_428, 0.02),
        (50_798, 0.04),
        (80_158, 0.06),
        (111_340, 0.08),
        (140_698, 0.093),
        (721_314, 0.103),
        (865_574, 0.113),
        (1_000_000, 0.123),  # 11.3% + 1% mental health surcharge
        (1_442_628, 0.133),  # 12.3% + 1% mental health surcharge
    ],
    (2026, "single"): [
        (0, 0.01),
        (10_714, 0.02),
        (25_399, 0.04),
        (40_084, 0.06),
        (55_670, 0.08),
        (70_349, 0.093),
        (360_657, 0.103),
        (432_787, 0.113),
        (721_314, 0.123),
        (1_000_000, 0.133),  # 12.3% + 1% mental health surcharge
    ],
}

FEDERAL_STANDARD_DEDUCTION: dict[tuple[int, str], float] = {
    (2025, "mfj"): 30_000,
    (2025, "single"): 15_000,
    (2026, "mfj"): 30_000,
    (2026, "single"): 15_000,
}

CA_STANDARD_DEDUCTION: dict[tuple[int, str], float] = {
    (2025, "mfj"): 10_726,
    (2025, "single"): 5_363,
    (2026, "mfj"): 10_726,
    (2026, "single"): 5_363,
}

SS_WAGE_BASE: dict[int, float] = {
    2025: 176_100,
    2026: 176_100,  # placeholder, update when announced
}

SS_RATE = 0.124
MEDICARE_RATE = 0.029
SE_TAXABLE_FRACTION = 0.9235

ADDITIONAL_MEDICARE_RATE = 0.009
ADDITIONAL_MEDICARE_THRESHOLD: dict[str, float] = {
    "mfj": 250_000,
    "single": 200_000,
}

# QBI (Section 199A) income thresholds and phase-out ranges.
# Above the threshold, QBI deduction phases out over the range.
# For sole props with no W-2 employees, it reaches $0 at the top of the range.
QBI_THRESHOLD: dict[tuple[int, str], float] = {
    (2025, "mfj"): 394_600,
    (2025, "single"): 197_300,
    (2026, "mfj"): 394_600,
    (2026, "single"): 197_300,
}
QBI_PHASEOUT_RANGE: dict[str, float] = {
    "mfj": 100_000,
    "single": 50_000,
}

# Safe harbor: AGI threshold above which 110% of prior year tax is required
SAFE_HARBOR_AGI_THRESHOLD = 150_000

# Federal estimated tax installment schedule: equal 25% quarters.
FED_CUMULATIVE_PCT: dict[int, float] = {1: 0.25, 2: 0.50, 3: 0.75, 4: 1.00}

# CA estimated tax installment schedule (differs from federal's equal 25% quarters).
# Payment quarters: Q1=Apr 15, Q2=Jun 15, Q3=Sep 15, Q4=Jan 15.
CA_INSTALLMENT_PCT: dict[int, float] = {1: 0.30, 2: 0.40, 3: 0.00, 4: 0.30}
CA_CUMULATIVE_PCT: dict[int, float] = {1: 0.30, 2: 0.70, 3: 0.70, 4: 1.00}


# =============================================================================
# Pure calculation functions
# =============================================================================


def apply_brackets(
    taxable_income: float, brackets: list[tuple[float, float]]
) -> float:
    """Compute tax using progressive brackets.

    Each bracket is (threshold, rate). Income in each range is taxed at that rate.
    """
    if taxable_income <= 0:
        return 0

    tax = 0.0
    for i, (threshold, rate) in enumerate(brackets):
        if i + 1 < len(brackets):
            next_threshold = brackets[i + 1][0]
            bracket_income = min(taxable_income, next_threshold) - threshold
        else:
            bracket_income = taxable_income - threshold

        if bracket_income <= 0:
            break
        tax += bracket_income * rate

    return tax


def _resolve_brackets(
    config_brackets: list[list[float]] | None,
    fallback: dict[tuple[int, str], list[tuple[float, float]]],
    year: int,
    filing_status: str,
) -> list[tuple[float, float]]:
    """Return brackets from config if set, otherwise from hardcoded fallback."""
    if config_brackets:
        return [(b[0], b[1]) for b in config_brackets]
    key = (year, filing_status)
    if key in fallback:
        return fallback[key]
    # Fall back to latest year available for this filing status
    candidates = [y for y, fs in fallback if fs == filing_status]
    if candidates:
        return fallback[(max(candidates), filing_status)]
    return []


def compute_se_tax(
    se_net_income: float,
    config: TaxConfig | None = None,
    year: int = 2026,
) -> tuple[float, float]:
    """Compute self-employment tax.

    Returns (se_tax, half_se_deduction).
    SE tax = 92.35% of net SE income * (SS rate + Medicare rate).
    SS portion is capped at the wage base.
    """
    if se_net_income <= 0:
        return 0.0, 0.0

    se_frac = (config.se_taxable_fraction if config and config.se_taxable_fraction else SE_TAXABLE_FRACTION)
    ss_rate = (config.ss_rate if config and config.ss_rate else SS_RATE)
    med_rate = (config.medicare_rate if config and config.medicare_rate else MEDICARE_RATE)
    wage_base = (config.ss_wage_base if config and config.ss_wage_base
                 else SS_WAGE_BASE.get(year, SS_WAGE_BASE[max(SS_WAGE_BASE)]))

    taxable_se = se_net_income * se_frac
    ss_income = min(taxable_se, wage_base)
    ss_tax = ss_income * ss_rate
    medicare_tax = taxable_se * med_rate

    se_tax = ss_tax + medicare_tax
    half_se = se_tax / 2
    return se_tax, half_se


def compute_federal_tax(
    agi: float,
    filing_status: str,
    year: int,
    qbi_deduction: float = 0,
    config: TaxConfig | None = None,
) -> tuple[float, float, float]:
    """Compute federal income tax.

    AGI should already reflect above-the-line deductions (half SE tax, etc.).
    Returns (taxable_income, standard_deduction, tax).
    """
    if config and config.federal_standard_deduction is not None:
        std_ded = config.federal_standard_deduction
    else:
        std_ded = FEDERAL_STANDARD_DEDUCTION.get(
            (year, filing_status),
            FEDERAL_STANDARD_DEDUCTION.get(
                (max(y for y, _ in FEDERAL_STANDARD_DEDUCTION), filing_status), 0
            ),
        )
    taxable = max(0, agi - std_ded - qbi_deduction)

    brackets = _resolve_brackets(
        config.federal_brackets if config else None,
        FEDERAL_BRACKETS, year, filing_status,
    )
    tax = apply_brackets(taxable, brackets)
    return taxable, std_ded, tax


def compute_ca_tax(
    agi: float, filing_status: str, year: int,
    config: TaxConfig | None = None,
) -> tuple[float, float, float]:
    """Compute California state income tax.

    Returns (taxable_income, standard_deduction, tax).
    CA does not allow SE or QBI deductions from state taxable income.
    """
    if config and config.ca_standard_deduction is not None:
        std_ded = config.ca_standard_deduction
    else:
        std_ded = CA_STANDARD_DEDUCTION.get(
            (year, filing_status),
            CA_STANDARD_DEDUCTION.get(
                (max(y for y, _ in CA_STANDARD_DEDUCTION), filing_status), 0
            ),
        )
    taxable = max(0, agi - std_ded)

    brackets = _resolve_brackets(
        config.ca_brackets if config else None,
        CA_BRACKETS, year, filing_status,
    )
    tax = apply_brackets(taxable, brackets)
    return taxable, std_ded, tax


def estimate_quarterly_tax(
    se_income_ytd: float,
    w2_income: float,
    w2_federal_withholding: float,
    w2_state_withholding: float,
    federal_estimated_paid: float,
    state_estimated_paid: float,
    filing_status: str,
    tax_year: int,
    method: str = "annualized",
    prior_year_federal_tax: float = 0,
    prior_year_state_tax: float = 0,
    enable_qbi: bool = False,
    current_quarter: int = 1,
    w2_months: int = 12,
    config: TaxConfig | None = None,
) -> QuarterlyTaxEstimate:
    """Compute estimated quarterly tax payment.

    current_quarter is the payment quarter (1-4), not the calendar quarter.
    SE income covers completed quarters through the payment quarter.

    w2_months is the expected number of months the W-2 job will last this year
    (default 12). W-2 income/withholding YTD is projected to w2_months, not
    to a full 12 months. This handles partial-year employment.

    For safe_harbor method, uses prior_year tax / 4 as the quarterly target.
    """
    se_annualize = 4 / current_quarter
    months_elapsed = current_quarter * 3
    w2_annualize = w2_months / months_elapsed

    se_annualized = se_income_ytd * se_annualize
    w2_annualized = w2_income * w2_annualize
    fed_withholding_annual = w2_federal_withholding * w2_annualize
    state_withholding_annual = w2_state_withholding * w2_annualize

    # SE tax on annualized SE income.
    # W-2 wages are NOT passed here: SE tax SS cap is per-person, and the W-2
    # income is the spouse's, not the SE person's. The SE person's own W-2
    # wages (if any) would need a separate input.
    se_tax, half_se = compute_se_tax(se_annualized, config=config, year=tax_year)

    # AGI: gross income minus above-the-line deductions (half SE is above-the-line)
    federal_agi = se_annualized + w2_annualized - half_se

    # Additional Medicare Tax: 0.9% on combined earned income above threshold.
    # Applies to total wages + SE earnings (after 92.35% factor).
    se_frac = (config.se_taxable_fraction if config and config.se_taxable_fraction
               else SE_TAXABLE_FRACTION)
    se_taxable_for_medicare = se_annualized * se_frac
    amt_threshold = ADDITIONAL_MEDICARE_THRESHOLD.get(filing_status, 200_000)
    additional_medicare = max(0, (w2_annualized + se_taxable_for_medicare) - amt_threshold) * ADDITIONAL_MEDICARE_RATE

    # QBI deduction: 20% of qualified business income, with caps and phase-out.
    # First pass: get standard deduction for the taxable income cap.
    _, fed_std_ded, _ = compute_federal_tax(
        federal_agi, filing_status, tax_year, config=config,
    )
    qbi_deduction = 0.0
    if enable_qbi and se_annualized > 0:
        qbi_deduction = se_annualized * 0.20
        # Phase-out above income thresholds. For sole props with no W-2
        # employees, QBI deduction reaches $0 above the phase-out range.
        threshold = QBI_THRESHOLD.get((tax_year, filing_status), 0)
        phaseout = QBI_PHASEOUT_RANGE.get(filing_status, 50_000)
        if threshold > 0 and federal_agi > threshold:
            if federal_agi >= threshold + phaseout:
                qbi_deduction = 0.0
            else:
                qbi_deduction *= 1 - (federal_agi - threshold) / phaseout
        # Cap at 20% of taxable income (before QBI deduction)
        taxable_before_qbi = max(0, federal_agi - fed_std_ded)
        qbi_deduction = min(qbi_deduction, taxable_before_qbi * 0.20)

    fed_taxable, fed_std_ded, fed_tax = compute_federal_tax(
        federal_agi, filing_status, tax_year,
        qbi_deduction=qbi_deduction, config=config,
    )
    federal_total_liability = fed_tax + se_tax + additional_medicare

    # CA state: same AGI (half SE is above-the-line for CA too), no QBI
    ca_agi = federal_agi
    ca_taxable, ca_std_ded, ca_tax = compute_ca_tax(
        ca_agi, filing_status, tax_year, config=config,
    )

    quarters_remaining = max(1, 5 - current_quarter)

    if method == "safe_harbor":
        # For AGI > $150K, safe harbor requires 110% of prior year tax
        safe_harbor_mult = 1.10 if federal_agi > SAFE_HARBOR_AGI_THRESHOLD else 1.00
        federal_target = prior_year_federal_tax * safe_harbor_mult
        state_target = prior_year_state_tax * safe_harbor_mult
        federal_net_due = max(0, federal_target - fed_withholding_annual)
        state_net_due = max(0, state_target - state_withholding_annual)
        fed_cumulative_due = federal_net_due * FED_CUMULATIVE_PCT[current_quarter]
        fed_quarterly = round(max(0, fed_cumulative_due - federal_estimated_paid), 2)
        state_cumulative_due = state_net_due * CA_CUMULATIVE_PCT[current_quarter]
        state_quarterly = round(max(0, state_cumulative_due - state_estimated_paid), 2)
    else:
        federal_net_due = max(
            0,
            federal_total_liability - fed_withholding_annual - federal_estimated_paid,
        )
        state_net_due = max(
            0,
            ca_tax - state_withholding_annual - state_estimated_paid,
        )
        fed_total_required = max(0, federal_total_liability - fed_withholding_annual)
        fed_cumulative_due = fed_total_required * FED_CUMULATIVE_PCT[current_quarter]
        fed_quarterly = round(max(0, fed_cumulative_due - federal_estimated_paid), 2)
        state_total_required = max(0, ca_tax - state_withholding_annual)
        state_cumulative_due = state_total_required * CA_CUMULATIVE_PCT[current_quarter]
        state_quarterly = round(max(0, state_cumulative_due - state_estimated_paid), 2)

    return QuarterlyTaxEstimate(
        tax_year=tax_year,
        quarter=current_quarter,
        method=method,
        filing_status=filing_status,
        w2_months=w2_months,
        se_income_ytd=se_income_ytd,
        se_income_annualized=se_annualized,
        w2_income=w2_income,
        w2_income_annualized=w2_annualized,
        se_tax=se_tax,
        half_se_deduction=half_se,
        additional_medicare_tax=additional_medicare,
        federal_agi=federal_agi,
        federal_standard_deduction=fed_std_ded,
        federal_taxable_income=fed_taxable,
        federal_tax=fed_tax,
        qbi_deduction=qbi_deduction,
        ca_agi=ca_agi,
        ca_standard_deduction=ca_std_ded,
        ca_taxable_income=ca_taxable,
        ca_tax=ca_tax,
        federal_withholding=fed_withholding_annual,
        state_withholding=state_withholding_annual,
        federal_estimated_paid=federal_estimated_paid,
        state_estimated_paid=state_estimated_paid,
        federal_total_liability=federal_total_liability,
        state_total_liability=ca_tax,
        federal_net_due=federal_net_due,
        state_net_due=state_net_due,
        federal_quarterly_amount=fed_quarterly,
        state_quarterly_amount=state_quarterly,
        quarters_remaining=quarters_remaining,
    )


# =============================================================================
# Config parsing
# =============================================================================


def parse_tax_config(config_path: Path) -> TaxConfig:
    """Parse tax configuration from a TOML file or TAX.md."""
    from istota.money._config_io import read_toml_config
    data = read_toml_config(config_path)
    tax = data.get("tax", data)  # support both [tax] wrapper and flat

    w2 = tax.get("w2", {})
    options = tax.get("options", {})
    accounts = tax.get("accounts", {})
    safe_harbor = tax.get("safe_harbor", {})
    estimated = tax.get("estimated_payments", {})
    rates = tax.get("rates", {})

    return TaxConfig(
        filing_status=tax.get("filing_status", "mfj"),
        tax_year=tax.get("tax_year", 2026),
        w2_income=w2.get("income", 0),
        w2_federal_withholding=w2.get("federal_withholding", 0),
        w2_state_withholding=w2.get("state_withholding", 0),
        federal_estimated_paid=estimated.get("federal", 0),
        state_estimated_paid=estimated.get("state", 0),
        enable_qbi_deduction=options.get("enable_qbi_deduction", False),
        se_income_accounts=accounts.get("se_income", ["Income:ScheduleC"]),
        se_expense_accounts=accounts.get("se_expenses", ["Expenses:Business"]),
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


# =============================================================================
# Input persistence
# =============================================================================

_TAX_INPUTS_KEY = "tax_inputs"


def _ensure_kv_table(conn) -> None:
    """Create kv_store table if it doesn't exist yet."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS kv_store ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
        "updated_at TEXT DEFAULT (datetime('now')))"
    )


def load_tax_inputs(db_path: Path) -> dict:
    """Load saved tax inputs from the DB kv_store, or empty dict."""
    import json

    from istota.money.db import get_db, kv_get

    if not db_path or not db_path.exists():
        return {}
    try:
        with get_db(db_path) as conn:
            _ensure_kv_table(conn)
            raw = kv_get(conn, _TAX_INPUTS_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


def save_tax_inputs(db_path: Path, inputs: dict) -> None:
    """Save tax inputs to the DB kv_store."""
    import json

    from istota.money.db import get_db, kv_set

    if not db_path:
        return
    with get_db(db_path) as conn:
        _ensure_kv_table(conn)
        kv_set(conn, _TAX_INPUTS_KEY, json.dumps(inputs))


# =============================================================================
# Ledger queries
# =============================================================================


def _parse_amount(value: str) -> float:
    """Parse a beancount amount string like '1234.56 USD' to float."""
    if not value or value.strip() == "":
        return 0.0
    parts = value.strip().split()
    return float(parts[0])


def payment_quarter_from_date(today: date) -> int:
    """Determine which estimated tax payment you're making based on today's date.

    Q1 payment due Apr 15 (covers Jan-Mar income)
    Q2 payment due Jun 15 (covers Jan-Jun income)
    Q3 payment due Sep 15 (covers Jan-Sep income)
    Q4 payment due Jan 15 next year (covers full year)
    """
    month, day = today.month, today.day
    if month < 4 or (month == 4 and day <= 15):
        return 1
    if month < 6 or (month == 6 and day <= 15):
        return 2
    if month < 9 or (month == 9 and day <= 15):
        return 3
    return 4


def _quarter_end_month(quarter: int) -> int:
    """Return the last month of the given calendar quarter."""
    return quarter * 3


def query_se_income(
    ledger_path: Path, config: TaxConfig, quarter: int,
) -> float:
    """Query beancount ledger for SE income through end of the given quarter.

    Quarter here is the payment quarter: Q1 = through month 3, Q2 = through month 6, etc.
    Returns net SE income as a positive number.
    """
    from istota.money.core.ledger import run_bean_query

    year = config.tax_year
    end_month = _quarter_end_month(quarter)

    # Query SE revenue (Income accounts are negative in beancount)
    # Anchor with ^ to avoid matching e.g. Assets:SK-Income-Fidelity
    income_patterns = " OR ".join(
        f"account ~ '^{acct}'" for acct in config.se_income_accounts
    )
    income_query = (
        f"SELECT sum(position) WHERE ({income_patterns}) "
        f"AND year = {year} AND month <= {end_month}"
    )
    income_rows = run_bean_query(ledger_path, income_query)
    revenue = 0.0
    if income_rows:
        revenue = abs(_parse_amount(income_rows[0].get("sum(position)", "0")))

    # Query business expenses
    expense_patterns = " OR ".join(
        f"account ~ '^{acct}'" for acct in config.se_expense_accounts
    )
    expense_query = (
        f"SELECT sum(position) WHERE ({expense_patterns}) "
        f"AND year = {year} AND month <= {end_month}"
    )
    expense_rows = run_bean_query(ledger_path, expense_query)
    expenses = 0.0
    if expense_rows:
        expenses = abs(_parse_amount(expense_rows[0].get("sum(position)", "0")))

    return max(0, revenue - expenses)
