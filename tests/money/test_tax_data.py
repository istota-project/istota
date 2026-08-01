"""Tests for the bundled tax rate data and its resolver.

The rate data is the one thing in this module that a user sends to a government
on the strength of, so these tests pin the figures against their published
sources rather than against whatever the loader happens to return. A test that
reads the number out of the file and asserts it equals itself would have passed
throughout the period the 2026 rows were byte-for-byte copies of 2025.
"""

from __future__ import annotations

import json

import pytest

from istota.money.core import tax_data


class TestBundledDataShape:
    def test_data_file_parses(self):
        raw = json.loads(tax_data.DATA_PATH.read_text())
        assert "jurisdictions" in raw
        assert "federal" in raw
        assert "states" in raw

    def test_load_is_cached(self):
        assert tax_data.load_tax_rates() is tax_data.load_tax_rates()

    def test_jurisdiction_registry_covers_all_states(self):
        rates = tax_data.load_tax_rates()
        # 50 states + District of Columbia.
        assert len(rates.jurisdictions) == 51
        codes = {j.code for j in rates.jurisdictions}
        assert len(codes) == 51
        assert "CA" in codes
        assert "DC" in codes
        assert all(len(c) == 2 and c.isupper() for c in codes)

    def test_no_income_tax_states_flagged(self):
        rates = tax_data.load_tax_rates()
        no_tax = {j.code for j in rates.jurisdictions if not j.taxes_income}
        assert no_tax == {"AK", "FL", "NV", "NH", "SD", "TN", "TX", "WA", "WY"}

    def test_every_bundled_year_carries_provenance(self):
        rates = tax_data.load_tax_rates()
        for year in rates.federal_years():
            res = rates.federal_year(year)
            assert res is not None
            assert res.source
            assert res.source_url.startswith("https://")
            assert res.verified_on
        for code in rates.bundled_state_codes():
            for year in rates.state_years(code):
                res = rates.state_year(code, year)
                assert res is not None, f"{code} {year}"
                assert res.source, f"{code} {year}"
                assert res.verified_on, f"{code} {year}"

    def test_brackets_are_ascending_and_start_at_zero(self):
        rates = tax_data.load_tax_rates()
        for year in rates.federal_years():
            for status in ("mfj", "single"):
                brackets = rates.federal_year(year).brackets(status)
                assert brackets[0][0] == 0
                thresholds = [b[0] for b in brackets]
                assert thresholds == sorted(thresholds)
                assert len(set(thresholds)) == len(thresholds)
        for code in rates.bundled_state_codes():
            for year in rates.state_years(code):
                for status in ("mfj", "single"):
                    brackets = rates.state_year(code, year).brackets(status)
                    if not brackets:
                        continue
                    assert brackets[0][0] == 0, f"{code} {year} {status}"
                    thresholds = [b[0] for b in brackets]
                    assert thresholds == sorted(thresholds), f"{code} {year} {status}"


class TestFederal2026:
    """Anchors from Rev. Proc. 2025-32.

    These are the figures the module was getting wrong: the 2026 rows were
    copies of 2025, so the page produced 2026 estimates from 2025 law.
    """

    @pytest.fixture
    def y2026(self):
        return tax_data.load_tax_rates().federal_year(2026)

    def test_standard_deduction(self, y2026):
        assert y2026.standard_deduction("mfj") == 32_200
        assert y2026.standard_deduction("single") == 16_100

    def test_top_bracket_thresholds(self, y2026):
        assert y2026.brackets("mfj")[-1] == (768_700, 0.37)
        assert y2026.brackets("single")[-1] == (640_600, 0.37)

    def test_full_mfj_bracket_table(self, y2026):
        assert y2026.brackets("mfj") == [
            (0, 0.10),
            (24_800, 0.12),
            (100_800, 0.22),
            (211_400, 0.24),
            (403_550, 0.32),
            (512_450, 0.35),
            (768_700, 0.37),
        ]

    def test_full_single_bracket_table(self, y2026):
        assert y2026.brackets("single") == [
            (0, 0.10),
            (12_400, 0.12),
            (50_400, 0.22),
            (105_700, 0.24),
            (201_775, 0.32),
            (256_225, 0.35),
            (640_600, 0.37),
        ]

    def test_social_security_wage_base(self, y2026):
        assert y2026.payroll.ss_wage_base == 184_500

    def test_payroll_rates(self, y2026):
        assert y2026.payroll.ss_rate == 0.124
        assert y2026.payroll.medicare_rate == 0.029
        assert y2026.payroll.se_taxable_fraction == 0.9235
        assert y2026.payroll.additional_medicare_rate == 0.009

    def test_qbi_thresholds_reflect_obbba_widened_range(self, y2026):
        # OBBBA widened the phase-in range from 100k/50k. A bracket-fetching
        # API would have returned current thresholds and still been wrong here.
        assert y2026.qbi_threshold("mfj") == 403_500
        assert y2026.qbi_phaseout_range("mfj") == 150_000
        assert y2026.qbi_threshold("single") == 201_750
        assert y2026.qbi_phaseout_range("single") == 75_000

    def test_additional_medicare_thresholds_are_statutory(self, y2026):
        # Not indexed — unchanged since 2013.
        assert y2026.additional_medicare_threshold("mfj") == 250_000
        assert y2026.additional_medicare_threshold("single") == 200_000

    def test_2026_is_not_a_copy_of_2025(self):
        rates = tax_data.load_tax_rates()
        y2025 = rates.federal_year(2025)
        y2026 = rates.federal_year(2026)
        for status in ("mfj", "single"):
            assert y2025.brackets(status) != y2026.brackets(status)
            assert y2025.standard_deduction(status) != y2026.standard_deduction(status)
        assert y2025.payroll.ss_wage_base != y2026.payroll.ss_wage_base


class TestFederal2025:
    """Anchors from Rev. Proc. 2024-40 — the year that was already right."""

    @pytest.fixture
    def y2025(self):
        return tax_data.load_tax_rates().federal_year(2025)

    def test_standard_deduction(self, y2025):
        assert y2025.standard_deduction("mfj") == 30_000
        assert y2025.standard_deduction("single") == 15_000

    def test_top_bracket_thresholds(self, y2025):
        assert y2025.brackets("mfj")[-1] == (751_600, 0.37)
        assert y2025.brackets("single")[-1] == (626_350, 0.37)

    def test_social_security_wage_base(self, y2025):
        assert y2025.payroll.ss_wage_base == 176_100

    def test_qbi(self, y2025):
        assert y2025.qbi_threshold("mfj") == 394_600
        assert y2025.qbi_phaseout_range("mfj") == 100_000
        assert y2025.qbi_threshold("single") == 197_300
        assert y2025.qbi_phaseout_range("single") == 50_000


class TestYearResolution:
    """The silent fallback becomes visible.

    `_resolve_brackets` fell back to the newest year present when the requested
    year was missing, so in 2027 the page would compute from 2026 numbers and
    report them as 2027. The resolver now says which year it used.
    """

    def test_exact_year_is_not_a_fallback(self):
        res = tax_data.load_tax_rates().federal_year(2026)
        assert res.year == 2026
        assert res.requested_year == 2026
        assert res.is_fallback is False

    def test_future_year_falls_back_to_newest_and_says_so(self):
        rates = tax_data.load_tax_rates()
        newest = max(rates.federal_years())
        res = rates.federal_year(newest + 5)
        assert res is not None
        assert res.year == newest
        assert res.requested_year == newest + 5
        assert res.is_fallback is True

    def test_past_year_falls_back_to_oldest(self):
        # Falling forward to the oldest available beats returning nothing: the
        # estimate is still wrong, but it is flagged rather than empty.
        rates = tax_data.load_tax_rates()
        oldest = min(rates.federal_years())
        res = rates.federal_year(oldest - 3)
        assert res.year == oldest
        assert res.is_fallback is True

    def test_unknown_state_resolves_to_nothing(self):
        assert tax_data.load_tax_rates().state_year("ZZ", 2026) is None

    def test_state_without_bundled_data_resolves_to_nothing(self):
        # Selectable, but override-driven — not silently computed from another
        # state's numbers.
        rates = tax_data.load_tax_rates()
        assert "NY" not in rates.bundled_state_codes()
        assert rates.state_year("NY", 2026) is None

    def test_no_income_tax_state_resolves_to_nothing(self):
        rates = tax_data.load_tax_rates()
        assert rates.state_year("TX", 2026) is None


class TestStaleness:
    def test_verified_before_the_tax_year_started_is_stale(self):
        assert tax_data.is_stale(verified_on="2025-06-01", tax_year=2026) is True

    def test_verified_during_the_tax_year_is_fresh(self):
        assert tax_data.is_stale(verified_on="2026-08-01", tax_year=2026) is False

    def test_verified_after_the_tax_year_is_fresh(self):
        assert tax_data.is_stale(verified_on="2027-01-15", tax_year=2026) is False

    def test_missing_verification_date_reads_as_stale(self):
        assert tax_data.is_stale(verified_on="", tax_year=2026) is True

    def test_unparseable_verification_date_reads_as_stale(self):
        assert tax_data.is_stale(verified_on="not-a-date", tax_year=2026) is True


class TestStateMetadata:
    def test_california_installment_schedule_is_cumulative(self):
        meta = tax_data.load_tax_rates().state_meta("CA")
        assert meta.installment_schedule == (0.30, 0.70, 0.70, 1.00)

    def test_california_starts_from_federal_agi(self):
        # CA conforms to federal AGI, which already carries the above-the-line
        # half-SE deduction. It does not allow the QBI deduction.
        meta = tax_data.load_tax_rates().state_meta("CA")
        assert meta.starts_from == "federal_agi"

    def test_default_installment_schedule_is_the_federal_one(self):
        assert tax_data.DEFAULT_INSTALLMENT_SCHEDULE == (0.25, 0.50, 0.75, 1.00)

    def test_state_meta_for_unknown_code_is_none(self):
        assert tax_data.load_tax_rates().state_meta("ZZ") is None

    def test_every_bundled_state_declares_a_starting_point(self):
        rates = tax_data.load_tax_rates()
        for code in rates.bundled_state_codes():
            meta = rates.state_meta(code)
            assert meta is not None, code
            assert meta.starts_from in tax_data.STARTING_POINTS, code

    def test_every_bundled_state_is_in_the_jurisdiction_registry(self):
        rates = tax_data.load_tax_rates()
        codes = {j.code for j in rates.jurisdictions}
        for code in rates.bundled_state_codes():
            assert code in codes, code

    def test_no_income_tax_state_ships_no_rate_data(self):
        rates = tax_data.load_tax_rates()
        no_tax = {j.code for j in rates.jurisdictions if not j.taxes_income}
        assert no_tax.isdisjoint(rates.bundled_state_codes())
