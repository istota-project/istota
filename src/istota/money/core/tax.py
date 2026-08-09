"""Estimated quarterly tax calculator.

Pure calculation module for federal and state estimated tax payments. No network
calls, no external APIs.

Rate data is **not** in this file. It lives in ``data/tax_rates.json`` behind
:mod:`istota.money.core.tax_data`, where each year carries the document it was
transcribed from and the date it was last verified. It used to be module-level
dicts here, and the 2026 rows were byte-for-byte copies of 2025 — so the page
produced 2026 estimates from 2025 law with nothing in the interface able to say
so. The constants that remain below are the ones that are *statutory rather than
indexed* (they do not change each January) or are pure calculation policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from istota.money.core.models import QuarterlyTaxEstimate, TaxConfig
from istota.money.core.tax_data import (
    DEFAULT_INSTALLMENT_SCHEDULE,
    YearRates,
    load_tax_rates,
)


# =============================================================================
# Statutory / policy constants
#
# Everything that gets indexed for inflation — brackets, standard deductions,
# the wage base, QBI thresholds — lives in data/tax_rates.json. These are the
# figures that do not.
# =============================================================================

# FICA rates. Statutory, unchanged since 1990. Used only as a fallback when a
# resolved year carries no payroll block.
SS_RATE = 0.124
MEDICARE_RATE = 0.029
SE_TAXABLE_FRACTION = 0.9235

# Additional Medicare Tax (§ 3101(b)(2)). Statutory and explicitly NOT indexed —
# the thresholds have been $250k/$200k since 2013.
ADDITIONAL_MEDICARE_RATE = 0.009
ADDITIONAL_MEDICARE_THRESHOLD: dict[str, float] = {
    "mfj": 250_000,
    "single": 200_000,
}

# Safe harbor: AGI threshold above which 110% of prior year tax is required.
SAFE_HARBOR_AGI_THRESHOLD = 150_000

# Federal estimated tax installment schedule: equal 25% quarters.
FED_CUMULATIVE_PCT: dict[int, float] = {1: 0.25, 2: 0.50, 3: 0.75, 4: 1.00}

# IRS annualized-income installment method (Form 2210 Schedule AI / 1040-ES).
# Each payment quarter annualizes income earned through the end of its period.
# Period end months: Q1=Mar(3), Q2=May(5), Q3=Aug(8), Q4=Dec(12). The
# annualization factor is 12 / period_months: Q1 x4, Q2 x2.4, Q3 x1.5, Q4 x1.
# (NOT even calendar quarters of 3/6/9/12 months — those over-state the Q2/Q3
# periods and under-project mid-year income.)
ANNUALIZATION_PERIOD_END_MONTH: dict[int, int] = {1: 3, 2: 5, 3: 8, 4: 12}


@dataclass(frozen=True)
class StateTaxResult:
    """A state's computed tax, or the reason there isn't one.

    ``reason`` is one of ``no_state`` (none selected), ``no_income_tax`` (one of
    the nine states that levies none), ``no_brackets`` (selectable but we ship
    no data and the user has entered no override) or ``unknown_state``. The UI
    renders a different thing for each; collapsing them to a zero renders the
    wrong thing for all four.
    """

    available: bool = False
    reason: str = ""
    name: str = ""
    taxable_income: float = 0.0
    standard_deduction: float = 0.0
    personal_exemption: float = 0.0
    tax: float = 0.0


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
    year_rates: YearRates | None,
    filing_status: str,
) -> list[tuple[float, float]]:
    """Brackets from the user's override if set, otherwise the bundled data.

    An empty list is a real answer — a state with neither an override nor
    bundled data has no brackets, and the caller must report that rather than
    compute a zero liability from an empty table.
    """
    if config_brackets:
        return [(b[0], b[1]) for b in config_brackets]
    if year_rates is None:
        return []
    return year_rates.brackets(filing_status)


def federal_rates(year: int) -> YearRates | None:
    """The bundled federal rates for ``year``, carrying the year actually used."""
    return load_tax_rates().federal_year(year)


def state_rates(state: str, year: int) -> YearRates | None:
    """The bundled rates for ``state``, or None when we ship none for it.

    None covers three distinct cases the caller must keep distinct: an unset
    state, a no-tax state, and a state that is selectable but override-driven.
    """
    if not state:
        return None
    return load_tax_rates().state_year(state, year)


def build_provenance(rates: YearRates | None, *, overridden: bool) -> dict:
    """The citation block the UI renders beside a set of figures.

    A plain dict rather than a dataclass so the estimate stays JSON-serializable
    by spreading ``__dict__`` into the API response. ``rates`` of None with
    ``overridden`` True is the ordinary case for a state we ship no data for:
    the user's own numbers are in use and there is no source to name.
    """
    if rates is None:
        return {
            "year": None,
            "requested_year": None,
            "is_fallback": False,
            "is_stale": False,
            "overridden": overridden,
            "source": "",
            "source_url": "",
            "verified_on": "",
        }
    return {
        "year": rates.year,
        "requested_year": rates.requested_year,
        "is_fallback": rates.is_fallback,
        "is_stale": rates.is_stale,
        "overridden": overridden,
        "source": rates.source,
        "source_url": rates.source_url,
        "verified_on": rates.verified_on,
    }


def installment_schedule(state: str) -> tuple[float, float, float, float]:
    """Cumulative fraction of the year's state liability due by each quarter.

    Defaults to the federal equal quarters. California's 30/40/0/30 split used
    to be applied unconditionally to every state's amounts, so any other state
    would have been given California's payment timing.
    """
    meta = load_tax_rates().state_meta(state) if state else None
    return meta.installment_schedule if meta else DEFAULT_INSTALLMENT_SCHEDULE


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

    fed = federal_rates(year)
    payroll = fed.payroll if fed else None

    def _rate(override: float | None, bundled: float | None, statutory: float) -> float:
        # Override wins, then the resolved year's payroll block, then the
        # statutory constant. `or` rather than `is not None` throughout: a zero
        # rate is indistinguishable from "unset" everywhere else in this config,
        # and a genuine 0% FICA rate is not a case worth modelling.
        return (override or 0) or (bundled or 0) or statutory

    se_frac = _rate(
        config.se_taxable_fraction if config else None,
        payroll.se_taxable_fraction if payroll else None,
        SE_TAXABLE_FRACTION,
    )
    ss_rate = _rate(
        config.ss_rate if config else None,
        payroll.ss_rate if payroll else None,
        SS_RATE,
    )
    med_rate = _rate(
        config.medicare_rate if config else None,
        payroll.medicare_rate if payroll else None,
        MEDICARE_RATE,
    )
    wage_base = (
        (config.ss_wage_base if config else None)
        or (payroll.ss_wage_base if payroll else None)
        # Third tier, matching the rates above. Without it a year block missing
        # its payroll data yields 0, and `min(taxable_se, 0)` drops the whole
        # Social Security portion — a five-figure understatement reported as a
        # normal result.
        or load_tax_rates().latest_ss_wage_base()
    )

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
    rates = federal_rates(year)
    if config and config.federal_standard_deduction is not None:
        std_ded = config.federal_standard_deduction
    else:
        std_ded = rates.standard_deduction(filing_status) if rates else 0
    taxable = max(0, agi - std_ded - qbi_deduction)

    brackets = _resolve_brackets(
        config.federal_brackets if config else None, rates, filing_status,
    )
    tax = apply_brackets(taxable, brackets)
    return taxable, std_ded, tax


def state_starting_income(
    starts_from: str,
    *,
    federal_agi: float,
    federal_taxable_income: float,
    gross_compensation: float,
) -> float:
    """Which federal figure a state's tax starts from.

    The one conformity knob — enough to express "this state starts from federal
    AGI and allows neither the SE nor the QBI deduction", without building a
    conformity engine. Benefit recapture, exemption phase-outs and per-state
    credits are not modeled and are named in the disclaimer.

    An unrecognised value falls back to federal AGI rather than raising: a data
    file naming a starting point this build does not know about should produce
    the common-case answer, not a broken page.
    """
    if starts_from == "federal_taxable_income":
        return federal_taxable_income
    if starts_from == "gross_compensation":
        return gross_compensation
    return federal_agi


def compute_state_tax(
    starting_income: float,
    state: str,
    filing_status: str,
    year: int,
    config: TaxConfig | None = None,
) -> StateTaxResult:
    """Compute a state's income tax, or say why it could not be computed.

    ``starting_income`` is whatever :func:`state_starting_income` resolved for
    this state's ``starts_from`` — for California, federal AGI, which already
    carries the above-the-line half-SE deduction. California does not allow the
    QBI deduction, which is why AGI rather than federal taxable income is the
    right basis for it.

    Returning "unavailable with a reason" rather than a zero is the point: a
    zero is a computed result, and a user in Texas should not be looking at a
    state tax row at all, while a user in New York mid-setup needs to be told
    their brackets are missing rather than shown a zero liability.
    """
    code = (state or "").upper()
    if not code:
        return StateTaxResult(reason="no_state")

    jurisdiction = load_tax_rates().jurisdiction(code)
    if jurisdiction is None:
        return StateTaxResult(reason="unknown_state")
    if not jurisdiction.taxes_income:
        # Checked ahead of any override: an override corrects a rate, it does
        # not license inventing a liability in a state that levies none.
        return StateTaxResult(reason="no_income_tax", name=jurisdiction.name)

    rates = state_rates(code, year)
    override_brackets = config.state_brackets if config else None
    brackets = _resolve_brackets(override_brackets, rates, filing_status)
    if not brackets:
        return StateTaxResult(reason="no_brackets", name=jurisdiction.name)

    if config and config.state_standard_deduction is not None:
        std_ded = config.state_standard_deduction
    else:
        std_ded = rates.standard_deduction(filing_status) if rates else 0
    exemption = rates.personal_exemption(filing_status) if rates else 0

    taxable = max(0, starting_income - std_ded - exemption)
    return StateTaxResult(
        available=True,
        name=jurisdiction.name,
        taxable_income=taxable,
        standard_deduction=std_ded,
        personal_exemption=exemption,
        tax=apply_brackets(taxable, brackets),
    )


def annualization_months(
    quarter: int, tax_year: int, today: date | None = None
) -> int:
    """Number of months of income to annualize from for a payment quarter.

    Defaults to the full IRS annualized-income installment period
    (``ANNUALIZATION_PERIOD_END_MONTH``: Q1=3, Q2=5, Q3=8, Q4=12). When
    ``today`` is given and falls before that period has fully elapsed, only the
    completed months are available, so we annualize from those instead. Scaling
    partial-year data as if a full period of income existed under- or
    over-projects the annual figure (the mid-year Q3 bug). Floors at 1.
    """
    period_end = ANNUALIZATION_PERIOD_END_MONTH.get(quarter, 12)
    if today is None:
        return period_end
    if today.year > tax_year:
        elapsed = 12
    elif today.year < tax_year:
        elapsed = 0
    else:
        elapsed = today.month - 1  # the current month is still in progress
    return max(1, min(period_end, elapsed))


def _project_full_year(ytd: float, months_elapsed: int, target_months: int) -> float:
    """Project a year-to-date amount to a full- or partial-year total.

    Scales ``ytd`` from the months actually elapsed up to ``target_months``
    (capped at 12). Never returns less than ``ytd`` — an annualized total can't
    be below what's already been earned while the income source is ongoing
    (the "annualized < YTD" bug). Used for W-2 wages and withholding.
    """
    if ytd <= 0 or months_elapsed <= 0:
        return max(ytd, 0.0)
    monthly = ytd / months_elapsed
    projected = monthly * min(target_months, 12)
    return max(projected, ytd)


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
    income_months: int | None = None,
    config: TaxConfig | None = None,
    state: str = "",
) -> QuarterlyTaxEstimate:
    """Compute estimated quarterly tax payment.

    current_quarter is the payment quarter (1-4), not the calendar quarter.

    income_months is the number of months of income the YTD figures actually
    span. When None it defaults to the full IRS annualization period for the
    quarter (3/5/8/12). Callers that know the real date should pass the
    date-capped value from ``annualization_months(quarter, year, today)`` so a
    payment quarter whose period hasn't elapsed yet doesn't annualize partial
    data as if a full period existed.

    w2_months is the expected number of months the W-2 job will last this year
    (default 12). W-2 income/withholding is projected from income_months to
    w2_months, and never falls below the YTD amount already earned.

    For safe_harbor method, uses prior_year tax / 4 as the quarterly target.
    """
    months = income_months if income_months is not None else annualization_months(
        current_quarter, tax_year
    )
    months = max(1, months)

    # SE income annualizes by 12 / months (x4, x2.4, x1.5, x1 for full periods).
    se_annualized = max(se_income_ytd, se_income_ytd * (12 / months))
    # W-2 wages + withholding project from the same elapsed months to the
    # expected employment months, never below YTD.
    w2_annualized = _project_full_year(w2_income, months, w2_months)
    fed_withholding_annual = _project_full_year(w2_federal_withholding, months, w2_months)
    state_withholding_annual = _project_full_year(w2_state_withholding, months, w2_months)

    # SE tax on annualized SE income.
    # W-2 wages are NOT passed here: SE tax SS cap is per-person, and the W-2
    # income is the spouse's, not the SE person's. The SE person's own W-2
    # wages (if any) would need a separate input.
    se_tax, half_se = compute_se_tax(se_annualized, config=config, year=tax_year)
    _fed_payroll = (federal_rates(tax_year) or None)
    resolved_wage_base = (
        (config.ss_wage_base if config and config.ss_wage_base else 0)
        or (_fed_payroll.payroll.ss_wage_base
            if _fed_payroll and _fed_payroll.payroll else 0)
    )

    # AGI: gross income minus above-the-line deductions (half SE is above-the-line)
    federal_agi = se_annualized + w2_annualized - half_se

    # Additional Medicare Tax: 0.9% on combined earned income above threshold.
    # Applies to total wages + SE earnings (after 92.35% factor).
    fed_rates = federal_rates(tax_year)
    se_frac = (config.se_taxable_fraction if config and config.se_taxable_fraction
               else (fed_rates.payroll.se_taxable_fraction
                     if fed_rates and fed_rates.payroll else 0) or SE_TAXABLE_FRACTION)
    se_taxable_for_medicare = se_annualized * se_frac
    amt_threshold = (
        (fed_rates.additional_medicare_threshold(filing_status) if fed_rates else 0)
        or ADDITIONAL_MEDICARE_THRESHOLD.get(filing_status, 200_000)
    )
    amt_rate = (
        (fed_rates.payroll.additional_medicare_rate
         if fed_rates and fed_rates.payroll else 0) or ADDITIONAL_MEDICARE_RATE
    )
    additional_medicare = max(0, (w2_annualized + se_taxable_for_medicare) - amt_threshold) * amt_rate

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
        # The phase-out range is year-keyed, not a per-status constant: OBBBA
        # widened it from 100k/50k to 150k/75k for 2026. That is a structural
        # change, and hardcoding the old range would have quietly zeroed the
        # deduction for incomes that now still qualify for part of it.
        threshold = fed_rates.qbi_threshold(filing_status) if fed_rates else 0
        phaseout = (fed_rates.qbi_phaseout_range(filing_status) if fed_rates else 0) or 50_000
        # § 199A(e)(2) measures the threshold against TAXABLE income computed
        # without regard to the QBI deduction — not AGI. Keying on AGI started
        # the phase-out a whole standard deduction of income too early ($32,200
        # MFJ for 2026), and disagreed with the cap below, which had the basis
        # right all along.
        taxable_before_qbi = max(0, federal_agi - fed_std_ded)
        if threshold > 0 and taxable_before_qbi > threshold:
            if taxable_before_qbi >= threshold + phaseout:
                qbi_deduction = 0.0
            else:
                qbi_deduction *= 1 - (taxable_before_qbi - threshold) / phaseout
        # Cap at 20% of taxable income (before QBI deduction)
        qbi_deduction = min(qbi_deduction, taxable_before_qbi * 0.20)

    fed_taxable, fed_std_ded, fed_tax = compute_federal_tax(
        federal_agi, filing_status, tax_year,
        qbi_deduction=qbi_deduction, config=config,
    )
    federal_total_liability = fed_tax + se_tax + additional_medicare

    # State. `starts_from` picks which federal figure the state's tax is based
    # on; California takes AGI, which already carries the half-SE deduction.
    state_code = (state or (config.state if config else "") or "").upper()
    meta = load_tax_rates().state_meta(state_code) if state_code else None
    starts_from = meta.starts_from if meta else "federal_agi"
    state_agi = state_starting_income(
        starts_from,
        federal_agi=federal_agi,
        federal_taxable_income=fed_taxable,
        gross_compensation=se_annualized + w2_annualized,
    )
    state_result = compute_state_tax(
        state_agi, state_code, filing_status, tax_year, config=config,
    )
    state_tax = state_result.tax
    # A state we cannot compute contributes no AGI figure either — rendering
    # one beside an unavailable liability reads as a partial answer.
    if not state_result.available:
        state_agi = 0.0

    quarters_remaining = max(1, 5 - current_quarter)

    # Cumulative fraction due by each quarter, indexed 1-4 to match the payment
    # quarter. Per-state: California's 30/40/0/30 used to be applied to every
    # state's amounts, so anywhere else got California's payment timing.
    state_cumulative = installment_schedule(state_code)

    if method == "safe_harbor":
        # For AGI > $150K, safe harbor requires 110% of prior year tax
        safe_harbor_mult = 1.10 if federal_agi > SAFE_HARBOR_AGI_THRESHOLD else 1.00
        federal_target = prior_year_federal_tax * safe_harbor_mult
        # A prior-year state figure cannot create a liability in a state that
        # has none. The annualized branch gets this for free by keying on the
        # computed tax; this branch keys on a stored number, so it has to ask.
        # The case is ordinary: move from a taxing state to Texas and the stale
        # prior-year figure is still sitting in the config.
        state_target = (
            prior_year_state_tax * safe_harbor_mult if state_result.available else 0.0
        )
        federal_net_due = max(0, federal_target - fed_withholding_annual)
        state_net_due = max(0, state_target - state_withholding_annual)
        fed_cumulative_due = federal_net_due * FED_CUMULATIVE_PCT[current_quarter]
        fed_quarterly = round(max(0, fed_cumulative_due - federal_estimated_paid), 2)
        state_cumulative_due = state_net_due * state_cumulative[current_quarter - 1]
        state_quarterly = round(max(0, state_cumulative_due - state_estimated_paid), 2)
    else:
        federal_net_due = max(
            0,
            federal_total_liability - fed_withholding_annual - federal_estimated_paid,
        )
        state_net_due = max(
            0,
            state_tax - state_withholding_annual - state_estimated_paid,
        )
        fed_total_required = max(0, federal_total_liability - fed_withholding_annual)
        fed_cumulative_due = fed_total_required * FED_CUMULATIVE_PCT[current_quarter]
        fed_quarterly = round(max(0, fed_cumulative_due - federal_estimated_paid), 2)
        state_total_required = max(0, state_tax - state_withholding_annual)
        state_cumulative_due = state_total_required * state_cumulative[current_quarter - 1]
        state_quarterly = round(max(0, state_cumulative_due - state_estimated_paid), 2)

    return QuarterlyTaxEstimate(
        tax_year=tax_year,
        quarter=current_quarter,
        method=method,
        filing_status=filing_status,
        w2_months=w2_months,
        annualization_months=months,
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
        state_agi=state_agi,
        state_standard_deduction=state_result.standard_deduction,
        state_taxable_income=state_result.taxable_income,
        state_personal_exemption=state_result.personal_exemption,
        state_tax=state_tax,
        federal_withholding=fed_withholding_annual,
        state_withholding=state_withholding_annual,
        federal_estimated_paid=federal_estimated_paid,
        state_estimated_paid=state_estimated_paid,
        federal_total_liability=federal_total_liability,
        state_total_liability=state_tax,
        federal_net_due=federal_net_due,
        state_net_due=state_net_due,
        federal_quarterly_amount=fed_quarterly,
        state_quarterly_amount=state_quarterly,
        quarters_remaining=quarters_remaining,
        ss_wage_base=resolved_wage_base,
        se_taxable_fraction=se_frac,
        state_installment_schedule=list(state_cumulative),
        state=state_code,
        state_name=state_result.name,
        state_starts_from=starts_from if state_code else "",
        state_available=state_result.available,
        state_unavailable_reason=state_result.reason,
        federal_rates=build_provenance(fed_rates, overridden=bool(
            config and (config.federal_brackets or
                        config.federal_standard_deduction is not None)
        )),
        state_rates=None if not state_code else build_provenance(
            state_rates(state_code, tax_year),
            overridden=bool(config and (config.state_brackets or
                                        config.state_standard_deduction is not None)),
        ),
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
        state=(tax.get("state") or "").upper(),
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
        state_brackets=rates.get("state_brackets"),
        federal_standard_deduction=rates.get("federal_standard_deduction"),
        state_standard_deduction=rates.get("state_standard_deduction"),
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


def payment_quarter_from_date(today: date, tax_year: int | None = None) -> int:
    """Determine which estimated tax payment you're making based on today's date.

    Payment quarters map to the IRS annualization periods (see
    ``ANNUALIZATION_PERIOD_END_MONTH``):

    Q1 payment due Apr 15 (annualizes Jan-Mar income)
    Q2 payment due Jun 15 (annualizes Jan-May income)
    Q3 payment due Sep 15 (annualizes Jan-Aug income)
    Q4 payment due Jan 15 next year (full year)

    The Q4 payment for tax year Y is due in *January of Y+1*, so a bare January
    date would otherwise read as Q1. Pass ``tax_year`` to disambiguate: a date
    in any calendar year after the tax year is the (possibly late) Q4 payment,
    and a date before the tax year floors at Q1. Without ``tax_year`` the date
    is interpreted within its own calendar year (legacy behavior).

    Resolve ``today`` in the user's timezone, not server UTC — on the Jun 15
    boundary a UTC clock can read Jun 16 and skip the Q2 payment due that day.
    """
    if tax_year is not None:
        if today.year > tax_year:
            return 4  # the only estimated payment falling in a later year is Q4
        if today.year < tax_year:
            return 1  # pre-paying before the tax year begins

    month, day = today.month, today.day
    if month < 4 or (month == 4 and day <= 15):
        return 1
    if month < 6 or (month == 6 and day <= 15):
        return 2
    if month < 9 or (month == 9 and day <= 15):
        return 3
    return 4


def query_se_income(
    ledger_path: Path, config: TaxConfig, through_month: int,
) -> float:
    """Query beancount ledger for net SE income through the given month.

    ``through_month`` is the last month of the tax year to include (1-12) — e.g.
    5 includes Jan–May. Callers pass the annualization span
    (``annualization_months(...)``) so the queried income period matches the
    period the estimate annualizes from. Returns net SE income as a positive
    number.
    """
    from istota.money.core.ledger import run_bean_query

    year = config.tax_year
    end_month = max(1, min(12, through_month))

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
