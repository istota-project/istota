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


class TestCalifornia2025:
    """Anchors from the FTB's 2025 California Tax Rate Schedules.

    The figures previously shipped as "CA 2025" were nothing of the kind — the
    single standard deduction was $5,363 (a 2023 amount) and the 9.3% band
    started at $70,349 against the real $72,724. The same defect as the federal
    2026 rows, in the state table, and equally invisible.
    """

    @pytest.fixture
    def ca(self):
        return tax_data.load_tax_rates().state_year("CA", 2025)

    def test_standard_deduction(self, ca):
        assert ca.standard_deduction("single") == 5_706
        assert ca.standard_deduction("mfj") == 11_412

    def test_single_band_thresholds(self, ca):
        thresholds = [b[0] for b in ca.brackets("single")]
        assert thresholds[:9] == [
            0, 11_079, 26_264, 41_452, 57_542, 72_724, 371_479, 445_771, 742_953,
        ]

    def test_mfj_thresholds_are_twice_the_single_ones(self, ca):
        # True of every FTB band, and the cheapest internal check on a
        # transcription slip.
        single = [b[0] for b in ca.brackets("single")]
        mfj = [b[0] for b in ca.brackets("mfj")]
        for s, m in zip(single, mfj):
            if s in (0, 1_000_000) or m == 1_000_000:
                continue  # the surcharge band is a fixed amount, not doubled
            assert m == s * 2, (s, m)

    def test_behavioral_health_surcharge_folds_into_the_top_bands(self, ca):
        # A flat 1% above $1,000,000, the same figure for every filing status —
        # it has never been indexed and joint filers get no doubling.
        assert ca.brackets("single")[-1] == (1_000_000, 0.133)
        assert ca.brackets("mfj")[-1] == (1_485_906, 0.133)
        assert (1_000_000, 0.123) in ca.brackets("mfj")

    def test_2026_is_deliberately_absent(self):
        # The FTB indexes on the June-to-June CCPI and publishes in the autumn;
        # its own 2026 Form 540-ES tells you to use the 2025 tax table. Shipping
        # a "2026" copy of 2025 is the exact defect this module exists to stop,
        # so the year is left out and the resolver reports the substitution.
        rates = tax_data.load_tax_rates()
        assert 2026 not in rates.state_years("CA")
        res = rates.state_year("CA", 2026)
        assert res.year == 2025
        assert res.is_fallback is True


class TestFlatStates:
    """A flat state is a one-bracket table, but it is not one number.

    The rate is one decision; the base it applies to is a second, and several
    carry an exemption on top. That is what makes `starts_from` load-bearing
    rather than a California-shaped afterthought.
    """

    @pytest.fixture
    def rates(self):
        return tax_data.load_tax_rates()

    def test_pennsylvania_taxes_gross_compensation_with_no_relief(self, rates):
        meta = rates.state_meta("PA")
        assert meta.starts_from == "gross_compensation"
        year = rates.state_year("PA", 2026)
        assert year.brackets("single") == [(0, 0.0307)]
        assert year.standard_deduction("single") == 0
        assert year.personal_exemption("single") == 0

    def test_colorado_starts_from_federal_taxable_income(self, rates):
        assert rates.state_meta("CO").starts_from == "federal_taxable_income"
        assert rates.state_year("CO", 2026).brackets("mfj") == [(0, 0.0440)]

    def test_iowa_also_starts_from_federal_taxable_income(self, rates):
        # Iowa moved to this base for 2026; treating it as AGI-based overstates
        # the tax by the whole federal standard deduction.
        assert rates.state_meta("IA").starts_from == "federal_taxable_income"
        assert rates.state_year("IA", 2026).brackets("mfj") == [(0, 0.038)]

    def test_exemption_states_carry_an_exemption_and_no_deduction(self, rates):
        for code, single, mfj in (("IL", 2_925, 5_850), ("IN", 1_000, 2_000),
                                  ("MI", 5_900, 11_800)):
            year = rates.state_year(code, 2026)
            assert rates.state_meta(code).starts_from == "federal_agi", code
            assert year.standard_deduction("single") == 0, code
            assert year.personal_exemption("single") == single, code
            assert year.personal_exemption("mfj") == mfj, code

    def test_deduction_states_carry_a_deduction_and_no_exemption(self, rates):
        for code, single, mfj in (
            ("GA", 15_000, 30_000),
            ("KY", 3_360, 3_360),
            ("LA", 12_875, 25_750),
            ("NC", 12_750, 25_500),
        ):
            year = rates.state_year(code, 2026)
            assert year.standard_deduction("single") == single, code
            assert year.standard_deduction("mfj") == mfj, code
            assert year.personal_exemption("single") == 0, code

    def test_georgia_carries_the_2026_cut_not_the_superseded_rate(self, rates):
        # HB 463 cut 5.19% -> 4.99% retroactive to 1 Jan 2026, after the
        # comparison tables most secondary sources were compiled from.
        assert rates.state_year("GA", 2026).brackets("single") == [(0, 0.0499)]

    def test_every_flat_state_is_a_single_bracket_at_zero(self, rates):
        flat = ("CO", "GA", "IA", "IL", "IN", "KY", "LA", "MI", "NC", "PA")
        for code in flat:
            for status in ("mfj", "single"):
                brackets = rates.state_year(code, 2026).brackets(status)
                assert len(brackets) == 1, (code, status)
                assert brackets[0][0] == 0, (code, status)

    def test_flat_states_use_the_federal_installment_schedule(self, rates):
        # Only California's is unusual. Applying its 30/40/0/30 to everywhere
        # else was the bug; this is the assertion that keeps it closed.
        for code in ("CO", "GA", "IA", "IL", "IN", "KY", "LA", "MI", "NC", "PA"):
            assert rates.state_meta(code).installment_schedule == (
                0.25, 0.50, 0.75, 1.00
            ), code

    def test_omitted_states_are_recorded_with_a_reason(self, rates):
        # Absent data is fine; absent data that looks like an oversight is not.
        omitted = rates.omitted_states()
        assert set(omitted) == {"AZ", "ID", "MS", "OH", "UT"}
        for code, reason in omitted.items():
            assert reason, code

    def test_omitted_states_ship_no_rate_data(self, rates):
        for code in rates.omitted_states():
            assert code not in rates.bundled_state_codes()
            assert rates.state_year(code, 2026) is None


class TestDataIntegrityGuards:
    """Shapes a hand-edit of the rate file could produce that compute silently.

    Every one of these is a wrong *number* rather than a crash, which is the
    failure mode this module exists to make impossible.
    """

    def test_every_federal_year_carries_a_payroll_block(self):
        # A year block without one yields an all-zeros PayrollRates, and a zero
        # wage base means `min(taxable_se, 0) == 0` — the entire Social Security
        # half of SE tax vanishes, reported as a normal result.
        rates = tax_data.load_tax_rates()
        for year in rates.federal_years():
            payroll = rates.federal_year(year).payroll
            assert payroll is not None, year
            assert payroll.ss_wage_base > 0, year
            assert payroll.ss_rate > 0, year
            assert payroll.medicare_rate > 0, year
            assert payroll.se_taxable_fraction > 0, year

    def test_every_installment_schedule_is_four_ascending_ending_at_one(self):
        rates = tax_data.load_tax_rates()
        for code in rates.bundled_state_codes():
            schedule = rates.state_meta(code).installment_schedule
            assert len(schedule) == 4, code
            assert list(schedule) == sorted(schedule), code
            assert schedule[-1] == 1.0, code
            assert all(0 <= v <= 1 for v in schedule), code

    def test_the_default_schedule_satisfies_the_same_rules(self):
        s = tax_data.DEFAULT_INSTALLMENT_SCHEDULE
        assert len(s) == 4 and list(s) == sorted(s) and s[-1] == 1.0

    def test_no_bundled_year_carries_a_non_finite_number(self):
        import math

        rates = tax_data.load_tax_rates()

        def _check(year_rates, label):
            for status in ("mfj", "single"):
                for threshold, rate in year_rates.brackets(status):
                    assert math.isfinite(threshold), f"{label} {status}"
                    assert math.isfinite(rate), f"{label} {status}"
                    assert 0 <= rate <= 1, f"{label} {status}"
                assert math.isfinite(year_rates.standard_deduction(status))
                assert year_rates.standard_deduction(status) >= 0

        for year in rates.federal_years():
            _check(rates.federal_year(year), f"federal {year}")
        for code in rates.bundled_state_codes():
            for year in rates.state_years(code):
                _check(rates.state_year(code, year), f"{code} {year}")
