"""Tests for money.core.tax module."""

from datetime import date
from pathlib import Path

import pytest

from istota.money.core.tax import (
    annualization_months,
    apply_brackets,
    compute_ca_tax,
    compute_federal_tax,
    compute_se_tax,
    estimate_quarterly_tax,
    parse_tax_config,
    payment_quarter_from_date,
    ANNUALIZATION_PERIOD_END_MONTH,
    ADDITIONAL_MEDICARE_RATE,
    ADDITIONAL_MEDICARE_THRESHOLD,
)
from istota.money.core.models import TaxConfig


class TestComputeSeTax:
    """SE tax = 92.35% of net SE income * 15.3% (SS 12.4% + Medicare 2.9%).
    SS portion capped at wage base minus W-2 wages."""

    def test_zero_income(self):
        se_tax, half_se = compute_se_tax(0)
        assert se_tax == 0
        assert half_se == 0

    def test_negative_income(self):
        se_tax, half_se = compute_se_tax(-5000)
        assert se_tax == 0
        assert half_se == 0

    def test_basic_se_income(self):
        # $100,000 SE income
        # Taxable SE = 100000 * 0.9235 = 92350
        # SS portion = 92350 * 0.124 = 11451.40
        # Medicare = 92350 * 0.029 = 2678.15
        # Total SE tax = 14129.55
        # Half SE = 7064.775
        se_tax, half_se = compute_se_tax(100_000, year=2025)
        assert round(se_tax, 2) == 14129.55
        assert round(half_se, 2) == 7064.77

    def test_above_ss_wage_base(self):
        # $250,000 SE income, no W-2, 2025 wage base = 176,100
        # Taxable SE = 250000 * 0.9235 = 230875
        # SS portion = min(230875, 176100) * 0.124 = 176100 * 0.124 = 21836.40
        # Medicare = 230875 * 0.029 = 6695.375
        # Total SE tax = 28531.775
        se_tax, half_se = compute_se_tax(250_000, year=2025)
        assert round(se_tax, 2) == 28531.78
        assert round(half_se, 2) == 14265.89

    def test_above_ss_wage_base_se_only(self):
        # $250,000 SE income but SS capped at wage base
        # Already tested in test_above_ss_wage_base; this confirms
        # that SE tax uses the SE person's own income only (no spouse W-2)
        se_tax, _ = compute_se_tax(100_000, year=2025)
        # Full SS applies: 92350 * 0.124 + 92350 * 0.029
        assert round(se_tax, 2) == 14129.55


class TestApplyBrackets:
    def test_zero_income(self):
        brackets = [(0, 0.10), (10000, 0.20)]
        assert apply_brackets(0, brackets) == 0

    def test_negative_income(self):
        brackets = [(0, 0.10), (10000, 0.20)]
        assert apply_brackets(-5000, brackets) == 0

    def test_single_bracket(self):
        brackets = [(0, 0.10), (10000, 0.20)]
        # $5,000 income: 5000 * 0.10 = 500
        assert apply_brackets(5000, brackets) == 500

    def test_spans_two_brackets(self):
        brackets = [(0, 0.10), (10000, 0.20)]
        # $15,000: 10000 * 0.10 + 5000 * 0.20 = 1000 + 1000 = 2000
        assert apply_brackets(15000, brackets) == 2000

    def test_federal_mfj_2025_known_scenario(self):
        # Verify against known 2025 MFJ brackets
        # Taxable income = $200,000
        # 0-23,850 @ 10% = 2,385
        # 23,850-96,950 @ 12% = 8,772
        # 96,950-200,000 = 103,050 @ 22% = 22,671
        # Total = 33,828
        from istota.money.core.tax import FEDERAL_BRACKETS
        brackets = FEDERAL_BRACKETS[(2025, "mfj")]
        assert round(apply_brackets(200_000, brackets), 2) == 33828.00


class TestComputeFederalTax:
    def test_mfj_w2_only(self):
        # AGI = $150,000 (all W-2)
        # 2025 MFJ standard deduction = 30,000
        # Taxable = 120,000
        # 0-23,850 @ 10% = 2,385
        # 23,850-96,950 @ 12% = 8,772
        # 96,950-120,000 = 23,050 @ 22% = 5,071
        # Total = 16,228
        taxable, std_ded, tax = compute_federal_tax(150_000, "mfj", 2025)
        assert std_ded == 30_000
        assert taxable == 120_000
        assert round(tax, 2) == 16228.00

    def test_mfj_with_se_deduction_in_agi(self):
        # AGI = $193,000 (already reduced by $7,000 half-SE above the line)
        # Taxable = 193000 - 30000 = 163,000
        # 0-23,850 @ 10% = 2,385
        # 23,850-96,950 @ 12% = 8,772
        # 96,950-163,000 = 66,050 @ 22% = 14,531
        # Total = 25,688
        taxable, std_ded, tax = compute_federal_tax(193_000, "mfj", 2025)
        assert taxable == 163_000
        assert round(tax, 2) == 25688.00

    def test_with_qbi_deduction(self):
        # AGI = $150,000, QBI deduction = $10,000
        # Taxable = 150000 - 30000 - 10000 = 110,000
        # 0-23,850 @ 10% = 2,385
        # 23,850-96,950 @ 12% = 8,772
        # 96,950-110,000 = 13,050 @ 22% = 2,871
        # Total = 14,028
        taxable, std_ded, tax = compute_federal_tax(
            150_000, "mfj", 2025, qbi_deduction=10_000,
        )
        assert taxable == 110_000
        assert round(tax, 2) == 14028.00

    def test_income_below_standard_deduction(self):
        taxable, std_ded, tax = compute_federal_tax(20_000, "mfj", 2025)
        assert taxable == 0
        assert tax == 0

    def test_single_filer(self):
        # AGI = $80,000, single
        # 2025 single standard deduction = 15,000
        # Taxable = 65,000
        # 0-11,925 @ 10% = 1,192.50
        # 11,925-48,475 @ 12% = 4,386
        # 48,475-65,000 = 16,525 @ 22% = 3,635.50
        # Total = 9,214
        taxable, std_ded, tax = compute_federal_tax(80_000, "single", 2025)
        assert std_ded == 15_000
        assert taxable == 65_000
        assert round(tax, 2) == 9214.00


class TestComputeCaTax:
    def test_mfj_basic(self):
        # AGI = $150,000, MFJ
        # CA 2025 MFJ standard deduction = 10,726
        # Taxable = 139,274
        # CA MFJ brackets 2025:
        # 0-21,428 @ 1% = 214.28
        # 21,428-50,798 @ 2% = 587.40
        # 50,798-80,158 @ 4% = 1,174.40
        # 80,158-111,340 @ 6% = 1,870.92
        # 111,340-139,274 @ 8% = 2,234.72
        # Total = 6,081.72
        taxable, std_ded, tax = compute_ca_tax(150_000, "mfj", 2025)
        assert std_ded == 10_726
        assert taxable == 139_274
        assert round(tax, 2) == 6081.72

    def test_single_basic(self):
        # AGI = $80,000, single
        # CA 2025 single standard deduction = 5,363
        # Taxable = 74,637
        # CA single brackets 2025:
        # 0-10,714 @ 1% = 107.14
        # 10,714-25,399 @ 2% = 293.70
        # 25,399-40,084 @ 4% = 587.40
        # 40,084-55,670 @ 6% = 935.16
        # 55,670-70,349 @ 8% = 1,174.32
        # 70,349-74,637 @ 9.3% = 398.78
        # Total = 3,496.50
        taxable, std_ded, tax = compute_ca_tax(80_000, "single", 2025)
        assert std_ded == 5_363
        assert taxable == 74_637
        assert round(tax, 2) == 3496.50

    def test_income_below_standard_deduction(self):
        taxable, std_ded, tax = compute_ca_tax(5_000, "mfj", 2025)
        assert taxable == 0
        assert tax == 0

    def test_single_high_income_12_3_bracket(self):
        # Verify the 12.3% bracket kicks in at $721,314 for single, not $1M
        # AGI = $800,000, single
        # Taxable = 800,000 - 5,363 = 794,637
        # ... brackets up to 11.3% at $432,787
        # $432,787 - $721,314 @ 11.3% = 32,603.55
        # $721,314 - $794,637 @ 12.3% = 9,018.73
        # Total should include the 12.3% bracket income
        _, _, tax_800k = compute_ca_tax(800_000, "single", 2025)
        # Compare with $730,000 (just above 12.3% threshold)
        _, _, tax_730k = compute_ca_tax(730_000, "single", 2025)
        # Marginal rate between $730K and $800K should be 12.3%
        marginal = (tax_800k - tax_730k) / (800_000 - 730_000)
        assert abs(marginal - 0.123) < 0.001

    def test_mfj_above_1m_mhs(self):
        # Verify 1% MHS applies above $1M for MFJ
        # Income just below and above $1M: marginal rate should jump by 1%
        _, _, tax_below = compute_ca_tax(1_010_000, "mfj", 2025)
        _, _, tax_above = compute_ca_tax(1_020_000, "mfj", 2025)
        # Between $1M and $1.44M for MFJ, rate is 12.3% (11.3% + 1% MHS)
        marginal = (tax_above - tax_below) / 10_000
        assert abs(marginal - 0.123) < 0.001


class TestAdditionalMedicareTax:
    def test_below_threshold_mfj(self):
        # Combined income below $250K MFJ threshold: no additional Medicare
        result = estimate_quarterly_tax(
            se_income_ytd=50_000,
            w2_income=0,
            w2_federal_withholding=0,
            w2_state_withholding=0,
            federal_estimated_paid=0,
            state_estimated_paid=0,
            filing_status="mfj",
            tax_year=2025,
            current_quarter=1,
        )
        # Annualized SE = 200K, SE taxable = 200K * 0.9235 = 184,700 < $250K
        assert result.additional_medicare_tax == 0

    def test_above_threshold_mfj(self):
        # SE income high enough that combined > $250K
        result = estimate_quarterly_tax(
            se_income_ytd=100_000,  # annualized to $400K
            w2_income=0,
            w2_federal_withholding=0,
            w2_state_withholding=0,
            federal_estimated_paid=0,
            state_estimated_paid=0,
            filing_status="mfj",
            tax_year=2025,
            current_quarter=1,
        )
        # Annualized SE = $400K, SE taxable = $400K * 0.9235 = $369,400
        # Additional Medicare = ($369,400 - $250,000) * 0.009 = $1,074.60
        assert round(result.additional_medicare_tax, 2) == 1074.60
        # Should be included in federal total liability
        assert result.federal_total_liability == result.federal_tax + result.se_tax + result.additional_medicare_tax

    def test_combined_w2_and_se(self):
        # W-2 + SE combined exceeds threshold
        result = estimate_quarterly_tax(
            se_income_ytd=25_000,  # annualized to $100K
            w2_income=50_000,  # annualized to $200K
            w2_federal_withholding=7_500,
            w2_state_withholding=3_000,
            federal_estimated_paid=0,
            state_estimated_paid=0,
            filing_status="mfj",
            tax_year=2025,
            current_quarter=1,
        )
        # SE taxable = $100K * 0.9235 = $92,350
        # Combined = $200K + $92,350 = $292,350
        # Additional Medicare = ($292,350 - $250,000) * 0.009 = $381.15
        assert round(result.additional_medicare_tax, 2) == 381.15

    def test_single_lower_threshold(self):
        # Single threshold is $200K, lower than MFJ $250K
        result = estimate_quarterly_tax(
            se_income_ytd=75_000,  # annualized to $300K
            w2_income=0,
            w2_federal_withholding=0,
            w2_state_withholding=0,
            federal_estimated_paid=0,
            state_estimated_paid=0,
            filing_status="single",
            tax_year=2025,
            current_quarter=1,
        )
        # SE taxable = $300K * 0.9235 = $277,050
        # Additional Medicare = ($277,050 - $200,000) * 0.009 = $693.45
        assert round(result.additional_medicare_tax, 2) == 693.45


class TestEstimateQuarterlyTax:
    def test_annualized_q1(self):
        result = estimate_quarterly_tax(
            se_income_ytd=25_000,
            w2_income=30_000,  # YTD Q1
            w2_federal_withholding=4_500,
            w2_state_withholding=1_800,
            federal_estimated_paid=0,
            state_estimated_paid=0,
            filing_status="mfj",
            tax_year=2025,
            method="annualized",
            current_quarter=1,
        )
        assert result.tax_year == 2025
        assert result.quarter == 1
        assert result.method == "annualized"
        # SE annualized: 25000 * 4 = 100,000
        assert result.se_income_annualized == 100_000
        # W-2 annualized: 30000 * 4 = 120,000
        assert result.w2_income_annualized == 120_000
        # SE tax on 100k annualized (W-2 is spouse's, doesn't affect SE person's SS cap)
        # SE taxable = 92350, full 15.3%: SS = 92350 * 0.124 = 11451.40, Medicare = 92350 * 0.029 = 2678.15
        assert round(result.se_tax, 2) == 14129.55
        # AGI = SE + W-2 - half SE deduction
        half_se = result.half_se_deduction
        assert result.federal_agi == 100_000 + 120_000 - half_se
        # Total liability includes income tax + SE tax + additional Medicare
        assert result.federal_total_liability == result.federal_tax + result.se_tax + result.additional_medicare_tax
        assert result.federal_total_liability > 0
        assert result.ca_tax > 0
        # Withholding is annualized too: 4500 * 4 = 18000
        assert result.federal_withholding == 18_000
        assert result.state_withholding == 7_200
        # Quarters remaining from Q1 = 4
        assert result.quarters_remaining == 4
        # Federal Q1 = 25% of total required (cumulative installment approach)
        fed_total_required = result.federal_total_liability - result.federal_withholding
        assert result.federal_quarterly_amount == round(fed_total_required * 0.25, 2)

    def test_annualized_q3_with_prior_payments(self):
        result = estimate_quarterly_tax(
            se_income_ytd=75_000,
            w2_income=90_000,
            w2_federal_withholding=13_500,
            w2_state_withholding=5_400,
            federal_estimated_paid=10_000,
            state_estimated_paid=3_000,
            filing_status="mfj",
            tax_year=2025,
            method="annualized",
            current_quarter=3,
        )
        assert result.quarter == 3
        # SE annualized: 75000 * (12/8) = 112,500 (IRS Q3 period = 8 months, x1.5)
        assert result.se_income_annualized == 112_500
        # W-2 annualized: 90000 projected from 8 months to a full year = 135,000
        assert result.w2_income_annualized == 135_000
        # Federal estimated paid reduces net due
        assert result.federal_estimated_paid == 10_000
        assert result.state_estimated_paid == 3_000
        # Quarters remaining from Q3 = 2
        assert result.quarters_remaining == 2
        # Federal Q3 = 75% cumulative of total required, minus $10K already paid
        fed_total_required = result.federal_total_liability - result.federal_withholding
        assert result.federal_quarterly_amount == round(
            max(0, fed_total_required * 0.75 - 10_000), 2
        )

    def test_safe_harbor_high_income_uses_110_pct(self):
        # With AGI > $150K, safe harbor should use 110% of prior year tax
        result = estimate_quarterly_tax(
            se_income_ytd=25_000,
            w2_income=30_000,
            w2_federal_withholding=4_500,
            w2_state_withholding=1_800,
            federal_estimated_paid=0,
            state_estimated_paid=0,
            filing_status="mfj",
            tax_year=2025,
            method="safe_harbor",
            prior_year_federal_tax=40_000,
            prior_year_state_tax=12_000,
            current_quarter=1,
        )
        assert result.method == "safe_harbor"
        # AGI is well above $150K, so 110% multiplier applies
        # Federal: 40000 * 1.10 = 44000, minus annualized withholding 18000 = 26000
        # Q1 = 25% cumulative of 26000 = 6500
        assert result.federal_quarterly_amount == round(
            max(0, (40_000 * 1.10 - 18_000) * 0.25), 2
        )
        # State: 12000 * 1.10 = 13200, minus annualized withholding 7200 = 6000
        # CA Q1 = 30% cumulative of 6000 = 1800
        assert result.state_quarterly_amount == round(
            max(0, (12_000 * 1.10 - 7_200) * 0.30), 2
        )

    def test_safe_harbor_low_income_uses_100_pct(self):
        # With AGI < $150K, safe harbor uses 100% of prior year tax
        result = estimate_quarterly_tax(
            se_income_ytd=5_000,   # annualized to $20K
            w2_income=30_000,      # annualized to $120K
            w2_federal_withholding=4_500,
            w2_state_withholding=1_800,
            federal_estimated_paid=0,
            state_estimated_paid=0,
            filing_status="mfj",
            tax_year=2025,
            method="safe_harbor",
            prior_year_federal_tax=20_000,
            prior_year_state_tax=6_000,
            current_quarter=1,
        )
        # AGI ~ $20K + $120K - half_se ~ $139K < $150K, so 100% multiplier
        # Federal: 20000 * 1.0 = 20000, minus annualized withholding 18000 = 2000
        # Quarterly = 2000/4 = 500
        assert result.federal_quarterly_amount == round(
            max(0, (20_000 - 18_000) / 4), 2
        )

    def test_withholding_exceeds_liability(self):
        # High withholding, low SE income -- nothing due
        result = estimate_quarterly_tax(
            se_income_ytd=5_000,
            w2_income=40_000,
            w2_federal_withholding=15_000,
            w2_state_withholding=8_000,
            federal_estimated_paid=0,
            state_estimated_paid=0,
            filing_status="mfj",
            tax_year=2025,
            method="annualized",
            current_quarter=1,
        )
        # With low income and high withholding, net due should be <= 0
        assert result.federal_quarterly_amount == 0
        assert result.state_quarterly_amount == 0

    def test_qbi_deduction_reduces_tax(self):
        without_qbi = estimate_quarterly_tax(
            se_income_ytd=50_000,
            w2_income=0,
            w2_federal_withholding=0,
            w2_state_withholding=0,
            federal_estimated_paid=0,
            state_estimated_paid=0,
            filing_status="mfj",
            tax_year=2025,
            method="annualized",
            enable_qbi=False,
            current_quarter=1,
        )
        with_qbi = estimate_quarterly_tax(
            se_income_ytd=50_000,
            w2_income=0,
            w2_federal_withholding=0,
            w2_state_withholding=0,
            federal_estimated_paid=0,
            state_estimated_paid=0,
            filing_status="mfj",
            tax_year=2025,
            method="annualized",
            enable_qbi=True,
            current_quarter=1,
        )
        assert with_qbi.qbi_deduction > 0
        assert with_qbi.federal_tax < without_qbi.federal_tax

    def test_qbi_capped_at_taxable_income(self):
        # Low income scenario: QBI should not exceed 20% of taxable income
        result = estimate_quarterly_tax(
            se_income_ytd=10_000,  # annualized to $40K
            w2_income=0,
            w2_federal_withholding=0,
            w2_state_withholding=0,
            federal_estimated_paid=0,
            state_estimated_paid=0,
            filing_status="mfj",
            tax_year=2025,
            method="annualized",
            enable_qbi=True,
            current_quarter=1,
        )
        # SE annualized = $40K, AGI = $40K - half_se
        # Taxable before QBI = AGI - $30K std ded = ~$7K
        # 20% of SE income = $8K, but 20% of taxable = ~$1.4K
        # QBI should be capped at the lower amount
        taxable_before_qbi = max(0, result.federal_agi - result.federal_standard_deduction)
        assert result.qbi_deduction <= taxable_before_qbi * 0.20 + 0.01  # floating point

    def test_qbi_phaseout_high_income(self):
        # Very high income: QBI should be zero (sole prop, no W-2 employees)
        result = estimate_quarterly_tax(
            se_income_ytd=200_000,  # annualized to $800K
            w2_income=0,
            w2_federal_withholding=0,
            w2_state_withholding=0,
            federal_estimated_paid=0,
            state_estimated_paid=0,
            filing_status="mfj",
            tax_year=2025,
            method="annualized",
            enable_qbi=True,
            current_quarter=1,
        )
        # AGI ~ $800K - half_se >> $394,600 + $100K phase-out range
        assert result.qbi_deduction == 0


    def test_ca_installment_schedule(self):
        """CA uses 30/40/0/30 quarterly split, not equal 25% like federal."""
        base_kwargs = dict(
            se_income_ytd=50_000,
            w2_income=0,
            w2_federal_withholding=0,
            w2_state_withholding=0,
            federal_estimated_paid=0,
            state_estimated_paid=0,
            filing_status="mfj",
            tax_year=2025,
            method="annualized",
            current_quarter=1,
        )
        q1 = estimate_quarterly_tax(**base_kwargs)
        # State total required = ca_tax - state_withholding (both 0 withholding here)
        state_total = q1.state_total_liability
        assert state_total > 0
        # Q1 = 30% of total
        assert q1.state_quarterly_amount == round(state_total * 0.30, 2)

        # Q2 = 40% of total (cumulative 70%, minus 30% already paid)
        q2 = estimate_quarterly_tax(
            **{**base_kwargs, "current_quarter": 2, "se_income_ytd": 100_000,
               "state_estimated_paid": q1.state_quarterly_amount}
        )
        state_total_q2 = q2.state_total_liability
        expected_q2 = round(max(0, state_total_q2 * 0.70 - q1.state_quarterly_amount), 2)
        assert q2.state_quarterly_amount == expected_q2

        # Q3 = 0% (cumulative stays at 70%)
        q3 = estimate_quarterly_tax(
            **{**base_kwargs, "current_quarter": 3, "se_income_ytd": 150_000,
               "state_estimated_paid": 5000}
        )
        state_total_q3 = q3.state_total_liability
        cumulative_q3 = state_total_q3 * 0.70
        expected_q3 = round(max(0, cumulative_q3 - 5000), 2)
        assert q3.state_quarterly_amount == expected_q3

        # Q4 = remaining 30% (cumulative 100%)
        q4 = estimate_quarterly_tax(
            **{**base_kwargs, "current_quarter": 4, "se_income_ytd": 200_000,
               "state_estimated_paid": 8000}
        )
        state_total_q4 = q4.state_total_liability
        expected_q4 = round(max(0, state_total_q4 * 1.00 - 8000), 2)
        assert q4.state_quarterly_amount == expected_q4


class TestPaymentQuarterFromDate:
    """The payment quarter for a date. June 15 is the Q2 due date, not Q3 —
    the boundary that produced wrong numbers when resolved in server time."""

    @pytest.mark.parametrize("d,expected", [
        (date(2026, 1, 1), 1),
        (date(2026, 3, 31), 1),
        (date(2026, 4, 15), 1),    # Q1 due date
        (date(2026, 4, 16), 2),
        (date(2026, 6, 15), 2),    # Q2 due date
        (date(2026, 6, 16), 3),
        (date(2026, 9, 15), 3),    # Q3 due date
        (date(2026, 9, 16), 4),
        (date(2026, 12, 31), 4),
    ])
    def test_boundaries(self, d, expected):
        assert payment_quarter_from_date(d) == expected

    def test_next_year_without_tax_year_is_legacy_q1(self):
        # Backward compatible: with no tax_year, a January date reads as Q1.
        assert payment_quarter_from_date(date(2027, 1, 15)) == 1


class TestPaymentQuarterCrossYear:
    """The Q4 payment for tax year Y is due Jan 15 of Y+1. A bare date in
    January would read as Q1; passing the tax year resolves it to Q4."""

    @pytest.mark.parametrize("d,expected", [
        (date(2027, 1, 1), 4),     # Q4 window opens in the new year
        (date(2027, 1, 15), 4),    # Q4 due date
        (date(2027, 2, 1), 4),     # filed late — still the Q4 payment
        (date(2027, 6, 30), 4),    # very late
    ])
    def test_year_after_tax_year_is_q4(self, d, expected):
        assert payment_quarter_from_date(d, tax_year=2026) == expected

    @pytest.mark.parametrize("d,expected", [
        (date(2026, 4, 15), 1),
        (date(2026, 6, 15), 2),
        (date(2026, 9, 15), 3),
        (date(2026, 11, 1), 4),    # within the tax year, post-Sep -> Q4
    ])
    def test_same_year_unchanged(self, d, expected):
        assert payment_quarter_from_date(d, tax_year=2026) == expected

    def test_before_tax_year_is_q1(self):
        # Pre-paying before the tax year even starts: floor at Q1.
        assert payment_quarter_from_date(date(2025, 12, 1), tax_year=2026) == 1


class TestAnnualizationMonths:
    """IRS annualized-income installment periods: Q1=3, Q2=5, Q3=8, Q4=12 months."""

    def test_irs_periods_match_form_2210(self):
        assert ANNUALIZATION_PERIOD_END_MONTH == {1: 3, 2: 5, 3: 8, 4: 12}

    @pytest.mark.parametrize("quarter,expected", [(1, 3), (2, 5), (3, 8), (4, 12)])
    def test_full_period_without_today(self, quarter, expected):
        assert annualization_months(quarter, 2026) == expected

    @pytest.mark.parametrize("quarter,today,expected", [
        (1, date(2026, 4, 15), 3),    # on each due date the period is complete
        (2, date(2026, 6, 15), 5),
        (3, date(2026, 9, 15), 8),
        (4, date(2027, 1, 15), 12),   # Q4 settles in the next calendar year
    ])
    def test_on_time_periods_complete(self, quarter, today, expected):
        assert annualization_months(quarter, 2026, today) == expected

    @pytest.mark.parametrize("quarter,today,expected", [
        (3, date(2026, 6, 16), 5),    # Q3 mis-selected mid-June: only 5 months real
        (2, date(2026, 5, 20), 4),    # Q2 computed early in May: 4 complete months
        (4, date(2026, 6, 15), 5),    # Q4 mid-June: capped hard
        (1, date(2026, 2, 10), 1),    # Q1 in February: 1 complete month
    ])
    def test_caps_to_elapsed_when_period_incomplete(self, quarter, today, expected):
        assert annualization_months(quarter, 2026, today) == expected

    def test_before_tax_year_floors_at_one(self):
        assert annualization_months(1, 2026, date(2025, 12, 1)) == 1


class TestIrsAnnualizationFactors:
    """SE income annualizes by 12/period: Q1 x4, Q2 x2.4, Q3 x1.5, Q4 x1."""

    @pytest.mark.parametrize("quarter,factor", [(1, 4.0), (2, 2.4), (3, 1.5), (4, 1.0)])
    def test_se_factor_per_quarter(self, quarter, factor):
        result = estimate_quarterly_tax(
            se_income_ytd=10_000, w2_income=0,
            w2_federal_withholding=0, w2_state_withholding=0,
            federal_estimated_paid=0, state_estimated_paid=0,
            filing_status="mfj", tax_year=2026, current_quarter=quarter,
        )
        assert result.annualization_months == ANNUALIZATION_PERIOD_END_MONTH[quarter]
        assert round(result.se_income_annualized, 2) == round(10_000 * factor, 2)


class TestW2ProjectionNeverBelowYtd:
    """An annualized figure can never be below year-to-date while employed."""

    def test_reported_bug_scenario(self):
        # w2 YTD 80,000 with an 8-month job, computed at Q3. The old code scaled
        # by 8/9 -> 71,111 (below YTD). It must be >= YTD.
        result = estimate_quarterly_tax(
            se_income_ytd=0, w2_income=80_000,
            w2_federal_withholding=10_000, w2_state_withholding=6_000,
            federal_estimated_paid=0, state_estimated_paid=0,
            filing_status="mfj", tax_year=2026, current_quarter=3, w2_months=8,
        )
        # Q3 full period = 8 months; project 8 -> 8 months = YTD exactly.
        assert result.w2_income_annualized == 80_000
        assert result.w2_income_annualized >= 80_000

    def test_contradictory_employment_months_clamps_to_ytd(self):
        # 8 months of data but a claimed 6-month job: raw projection dips below
        # YTD; the clamp holds it at YTD.
        result = estimate_quarterly_tax(
            se_income_ytd=0, w2_income=80_000,
            w2_federal_withholding=0, w2_state_withholding=0,
            federal_estimated_paid=0, state_estimated_paid=0,
            filing_status="mfj", tax_year=2026, current_quarter=3,
            income_months=8, w2_months=6,
        )
        assert result.w2_income_annualized == 80_000

    def test_partial_year_projection_and_withholding(self):
        # 50,000 over 5 months, a 10-month job -> 50000/5*10 = 100,000.
        result = estimate_quarterly_tax(
            se_income_ytd=0, w2_income=50_000,
            w2_federal_withholding=5_000, w2_state_withholding=0,
            federal_estimated_paid=0, state_estimated_paid=0,
            filing_status="mfj", tax_year=2026, current_quarter=2,
            income_months=5, w2_months=10,
        )
        assert result.w2_income_annualized == 100_000
        # Withholding scales by the same ratio: 5000/5*10 = 10,000.
        assert result.federal_withholding == 10_000


class TestIncomeMonthsOverride:
    def test_income_months_overrides_quarter_period(self):
        # Q3 normally annualizes from 8 months (x1.5). With only 5 months of
        # real data, income_months=5 annualizes x2.4 instead.
        result = estimate_quarterly_tax(
            se_income_ytd=10_000, w2_income=0,
            w2_federal_withholding=0, w2_state_withholding=0,
            federal_estimated_paid=0, state_estimated_paid=0,
            filing_status="mfj", tax_year=2026, current_quarter=3, income_months=5,
        )
        assert result.annualization_months == 5
        assert round(result.se_income_annualized, 2) == 24_000.00

    def test_q3_selected_midyear_does_not_underproject(self):
        # The exact reported bug: Q3 on June 16 with ~5 months of data. The cap
        # annualizes x2.4 (5 mo), not the buggy x1.333 (treating it as 9 mo).
        today = date(2026, 6, 16)
        months = annualization_months(3, 2026, today)
        assert months == 5
        result = estimate_quarterly_tax(
            se_income_ytd=33_391.01, w2_income=80_000,
            w2_federal_withholding=0, w2_state_withholding=0,
            federal_estimated_paid=0, state_estimated_paid=0,
            filing_status="mfj", tax_year=2026,
            current_quarter=3, income_months=months, w2_months=8,
        )
        # 33,391.01 x 12/5 = 80,138.42 (not the buggy 44,521.35).
        assert round(result.se_income_annualized, 2) == 80_138.42
        # 80,000 / 5 * 8 = 128,000, and never below the 80,000 YTD.
        assert result.w2_income_annualized == 128_000
        assert result.w2_income_annualized >= 80_000


class TestYearLongConvergence:
    """A steady income rate must yield the same full-year projection at every
    payment date — Q1 through Q4 converge when each period's data is complete."""

    @pytest.mark.parametrize("quarter", [1, 2, 3, 4])
    def test_se_income_converges(self, quarter):
        monthly = 5_000.0          # true annual SE = 60,000
        months = ANNUALIZATION_PERIOD_END_MONTH[quarter]
        result = estimate_quarterly_tax(
            se_income_ytd=monthly * months, w2_income=0,
            w2_federal_withholding=0, w2_state_withholding=0,
            federal_estimated_paid=0, state_estimated_paid=0,
            filing_status="mfj", tax_year=2026, current_quarter=quarter,
        )
        assert round(result.se_income_annualized, 2) == 60_000.00

    @pytest.mark.parametrize("quarter", [1, 2, 3, 4])
    def test_w2_full_year_converges(self, quarter):
        monthly = 6_000.0          # true annual W-2 = 72,000
        months = ANNUALIZATION_PERIOD_END_MONTH[quarter]
        result = estimate_quarterly_tax(
            se_income_ytd=0, w2_income=monthly * months,
            w2_federal_withholding=900.0 * months, w2_state_withholding=0,
            federal_estimated_paid=0, state_estimated_paid=0,
            filing_status="mfj", tax_year=2026, current_quarter=quarter, w2_months=12,
        )
        assert round(result.w2_income_annualized, 2) == 72_000.00
        # Withholding annualizes consistently: 900 * 12 = 10,800.
        assert round(result.federal_withholding, 2) == 10_800.00

    @pytest.mark.parametrize("quarter", [1, 2, 3, 4])
    def test_w2_partial_year_converges(self, quarter):
        # 8-month job at 6,000/mo -> true annual W-2 = 48,000. At Q4 the YTD
        # spans 12 calendar months but income only in 8; the clamp keeps it right.
        monthly, employment_months = 6_000.0, 8
        period = ANNUALIZATION_PERIOD_END_MONTH[quarter]
        ytd = monthly * min(period, employment_months)
        result = estimate_quarterly_tax(
            se_income_ytd=0, w2_income=ytd,
            w2_federal_withholding=0, w2_state_withholding=0,
            federal_estimated_paid=0, state_estimated_paid=0,
            filing_status="mfj", tax_year=2026, current_quarter=quarter,
            w2_months=employment_months,
        )
        assert round(result.w2_income_annualized, 2) == 48_000.00


class TestDateDrivenEndToEnd:
    """Mirror the route flow: today -> payment quarter -> annualization months
    -> estimate, at each due date with a steady income rate. Same annual result."""

    @pytest.mark.parametrize("today", [
        date(2026, 4, 15), date(2026, 6, 15),
        date(2026, 9, 15), date(2027, 1, 15),
    ])
    def test_steady_income_same_annual_on_each_due_date(self, today):
        monthly_se, monthly_w2 = 4_000.0, 7_000.0  # annual SE 48k, W-2 84k
        quarter = payment_quarter_from_date(today, tax_year=2026)
        months = annualization_months(quarter, 2026, today)
        if today == date(2027, 1, 15):
            assert quarter == 4  # cross-year Q4 resolves correctly
        result = estimate_quarterly_tax(
            se_income_ytd=monthly_se * months,
            w2_income=monthly_w2 * months,
            w2_federal_withholding=0, w2_state_withholding=0,
            federal_estimated_paid=0, state_estimated_paid=0,
            filing_status="mfj", tax_year=2026,
            current_quarter=quarter, income_months=months, w2_months=12,
        )
        assert round(result.se_income_annualized, 2) == 48_000.00
        assert round(result.w2_income_annualized, 2) == 84_000.00


class TestParseTaxConfig:
    def test_minimal_config(self, tmp_path):
        config_file = tmp_path / "tax.toml"
        config_file.write_text(
            '[tax]\n'
            'filing_status = "mfj"\n'
            'tax_year = 2025\n'
        )
        config = parse_tax_config(config_file)
        assert config.filing_status == "mfj"
        assert config.tax_year == 2025
        assert config.w2_income == 0
        assert config.enable_qbi_deduction is False
        assert config.se_income_accounts == ["Income:ScheduleC"]

    def test_full_config(self, tmp_path):
        config_file = tmp_path / "tax.toml"
        config_file.write_text(
            '[tax]\n'
            'filing_status = "single"\n'
            'tax_year = 2026\n\n'
            '[tax.w2]\n'
            'income = 120000\n'
            'federal_withholding = 18000\n'
            'state_withholding = 6000\n\n'
            '[tax.estimated_payments]\n'
            'federal = 8000\n'
            'state = 2000\n\n'
            '[tax.options]\n'
            'enable_qbi_deduction = true\n\n'
            '[tax.accounts]\n'
            'se_income = ["Income:Consulting"]\n'
            'se_expenses = ["Expenses:Consulting"]\n\n'
            '[tax.safe_harbor]\n'
            'prior_year_federal_tax = 35000\n'
            'prior_year_state_tax = 12000\n'
        )
        config = parse_tax_config(config_file)
        assert config.filing_status == "single"
        assert config.tax_year == 2026
        assert config.w2_income == 120_000
        assert config.w2_federal_withholding == 18_000
        assert config.w2_state_withholding == 6_000
        assert config.federal_estimated_paid == 8_000
        assert config.state_estimated_paid == 2_000
        assert config.enable_qbi_deduction is True
        assert config.se_income_accounts == ["Income:Consulting"]
        assert config.se_expense_accounts == ["Expenses:Consulting"]
        assert config.prior_year_federal_tax == 35_000
        assert config.prior_year_state_tax == 12_000
