"""Tests for jurisdiction as a real dimension of the tax estimate.

Before this, California was the only state that existed anywhere in the repo:
the field names carried it (`ca_brackets`, `ca_agi`), `compute_ca_tax` was the
only state function, and California's unusual 30/40/0/30 installment schedule
was applied unconditionally — so any other state would have been given
California's payment timing.
"""

from __future__ import annotations

import pytest

from istota.money import config_store
from istota.money.core.models import TaxConfig
from istota.money.core.tax import (
    compute_state_tax,
    estimate_quarterly_tax,
    installment_schedule,
    state_starting_income,
)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "money.db"


def _estimate(**overrides):
    kwargs = dict(
        se_income_ytd=150_000,
        w2_income=0,
        w2_federal_withholding=0,
        w2_state_withholding=0,
        federal_estimated_paid=0,
        state_estimated_paid=0,
        filing_status="mfj",
        tax_year=2025,
        method="annualized",
        current_quarter=4,
        income_months=12,
    )
    kwargs.update(overrides)
    return estimate_quarterly_tax(**kwargs)


class TestComputeStateTax:
    def test_california_computes(self):
        res = compute_state_tax(150_000, "CA", "mfj", 2025)
        assert res.available is True
        assert res.standard_deduction == 11_412
        assert res.taxable_income == 150_000 - 11_412
        assert res.tax > 0

    def test_california_is_case_insensitive(self):
        assert compute_state_tax(150_000, "ca", "mfj", 2025).tax == (
            compute_state_tax(150_000, "CA", "mfj", 2025).tax
        )

    def test_unset_state_is_unavailable_not_zero(self):
        # A zero is a computed result. A user who has not picked a state should
        # not be shown a state tax row at all, so the caller needs to tell the
        # two apart.
        res = compute_state_tax(150_000, "", "mfj", 2025)
        assert res.available is False
        assert res.reason == "no_state"
        assert res.tax == 0

    def test_no_income_tax_state_is_unavailable_with_its_own_reason(self):
        res = compute_state_tax(150_000, "TX", "mfj", 2025)
        assert res.available is False
        assert res.reason == "no_income_tax"

    def test_state_without_bundled_brackets_is_unavailable(self):
        # Selectable, but override-driven. Until an override exists the page
        # must say the brackets are missing rather than compute a zero.
        res = compute_state_tax(150_000, "NY", "mfj", 2025)
        assert res.available is False
        assert res.reason == "no_brackets"
        assert res.tax == 0

    def test_unknown_state_code_is_unavailable(self):
        res = compute_state_tax(150_000, "ZZ", "mfj", 2025)
        assert res.available is False
        assert res.reason == "unknown_state"

    def test_override_makes_an_unbundled_state_computable(self):
        cfg = TaxConfig(
            state="NY",
            state_brackets=[[0, 0.04], [100_000, 0.06]],
            state_standard_deduction=16_050,
        )
        res = compute_state_tax(150_000, "NY", "mfj", 2025, config=cfg)
        assert res.available is True
        assert res.standard_deduction == 16_050
        taxable = 150_000 - 16_050
        assert round(res.tax, 2) == round(
            100_000 * 0.04 + (taxable - 100_000) * 0.06, 2
        )

    def test_override_does_not_resurrect_a_no_income_tax_state(self):
        # Texas levies no income tax. An override is a correction to a rate,
        # not a licence to invent a liability.
        cfg = TaxConfig(state="TX", state_brackets=[[0, 0.05]])
        res = compute_state_tax(150_000, "TX", "mfj", 2025, config=cfg)
        assert res.available is False
        assert res.reason == "no_income_tax"


class TestStartingPoint:
    def test_federal_agi_is_the_default(self):
        got = state_starting_income(
            "federal_agi",
            federal_agi=100_000,
            federal_taxable_income=70_000,
            gross_compensation=110_000,
        )
        assert got == 100_000

    def test_federal_taxable_income(self):
        got = state_starting_income(
            "federal_taxable_income",
            federal_agi=100_000,
            federal_taxable_income=70_000,
            gross_compensation=110_000,
        )
        assert got == 70_000

    def test_gross_compensation(self):
        got = state_starting_income(
            "gross_compensation",
            federal_agi=100_000,
            federal_taxable_income=70_000,
            gross_compensation=110_000,
        )
        assert got == 110_000

    def test_unknown_starting_point_falls_back_to_agi(self):
        got = state_starting_income(
            "something_new",
            federal_agi=100_000,
            federal_taxable_income=70_000,
            gross_compensation=110_000,
        )
        assert got == 100_000

    def test_california_starts_from_federal_agi(self):
        # CA conforms to federal AGI, which already carries the above-the-line
        # half-SE deduction. The old docstring said it allowed neither SE nor
        # QBI, contradicting both this implementation and the mock's.
        r = _estimate(state="CA")
        assert r.state_agi == r.federal_agi
        assert r.federal_agi < r.se_income_annualized  # half-SE came off


class TestInstallmentSchedule:
    def test_california_keeps_its_own_schedule(self):
        assert installment_schedule("CA") == (0.30, 0.70, 0.70, 1.00)

    def test_other_states_get_the_federal_equal_quarters(self):
        # The bug this closes: CA's schedule was applied unconditionally, so
        # every other state was given California's payment timing.
        assert installment_schedule("NY") == (0.25, 0.50, 0.75, 1.00)
        assert installment_schedule("") == (0.25, 0.50, 0.75, 1.00)

    def test_estimate_applies_the_configured_states_schedule(self):
        cfg = TaxConfig(
            state="NY",
            state_brackets=[[0, 0.04], [100_000, 0.06]],
            state_standard_deduction=16_050,
        )
        q1 = _estimate(state="NY", config=cfg, current_quarter=1)
        assert q1.state_total_liability > 0
        # Federal quarters, not California's 30%.
        assert q1.state_quarterly_amount == round(q1.state_total_liability * 0.25, 2)

    def test_california_still_uses_thirty_percent_in_q1(self):
        q1 = _estimate(state="CA", current_quarter=1)
        assert q1.state_quarterly_amount == round(q1.state_total_liability * 0.30, 2)


class TestEstimateStateFields:
    def test_state_is_reported_on_the_result(self):
        r = _estimate(state="CA")
        assert r.state == "CA"
        assert r.state_name == "California"
        assert r.state_available is True

    def test_unset_state_reports_nothing_computable(self):
        r = _estimate(state="")
        assert r.state == ""
        assert r.state_available is False
        assert r.state_tax == 0
        assert r.state_total_liability == 0
        assert r.state_quarterly_amount == 0

    def test_no_income_tax_state_reports_its_name_and_no_liability(self):
        r = _estimate(state="TX")
        assert r.state == "TX"
        assert r.state_name == "Texas"
        assert r.state_available is False
        assert r.state_unavailable_reason == "no_income_tax"
        assert r.state_total_liability == 0

    def test_federal_is_unaffected_by_the_state_being_unset(self):
        with_ca = _estimate(state="CA")
        without = _estimate(state="")
        assert with_ca.federal_total_liability == without.federal_total_liability
        assert with_ca.federal_quarterly_amount == without.federal_quarterly_amount


class TestEstimateProvenance:
    def test_federal_provenance_names_the_source_and_year(self):
        r = _estimate(tax_year=2026)
        p = r.federal_rates
        assert p["year"] == 2026
        assert p["requested_year"] == 2026
        assert p["is_fallback"] is False
        assert "Rev. Proc. 2025-32" in p["source"]
        assert p["source_url"].startswith("https://")
        assert p["verified_on"]

    def test_missing_year_is_reported_as_a_fallback(self):
        r = _estimate(tax_year=2031)
        p = r.federal_rates
        assert p["requested_year"] == 2031
        assert p["year"] == 2026
        assert p["is_fallback"] is True

    def test_state_provenance_present_for_a_bundled_state(self):
        r = _estimate(state="CA", tax_year=2025)
        assert r.state_rates is not None
        assert r.state_rates["year"] == 2025
        assert "Franchise Tax Board" in r.state_rates["source"]

    def test_state_provenance_absent_when_no_state(self):
        assert _estimate(state="").state_rates is None

    def test_override_is_flagged_as_such(self):
        cfg = TaxConfig(
            state="NY",
            state_brackets=[[0, 0.04]],
            state_standard_deduction=16_050,
        )
        r = _estimate(state="NY", config=cfg)
        assert r.state_rates is not None
        assert r.state_rates["overridden"] is True

    def test_bundled_rates_are_not_flagged_as_overridden(self):
        r = _estimate(state="CA")
        assert r.state_rates["overridden"] is False


class TestTaxSchedulesTable:
    """Brackets and deductions get the dimensions they actually have.

    `tax_year_rates` is keyed on the year alone, so a stored bracket override
    was filing-status-agnostic while the constants it overrode were keyed
    (year, filing_status) — an override entered while filing jointly silently
    continued to apply after switching to single.
    """

    def test_upsert_and_read_back(self, db):
        config_store.upsert_tax_schedule(
            db, 2026, "federal", "mfj",
            brackets=[[0, 0.1], [50_000, 0.2]], standard_deduction=33_000,
        )
        rows = config_store.list_tax_schedules(db)
        assert len(rows) == 1
        assert rows[0]["tax_year"] == 2026
        assert rows[0]["jurisdiction"] == "federal"
        assert rows[0]["filing_status"] == "mfj"
        assert rows[0]["standard_deduction"] == 33_000
        assert rows[0]["brackets"] == [[0, 0.1], [50_000, 0.2]]

    def test_filing_statuses_do_not_collide(self, db):
        config_store.upsert_tax_schedule(
            db, 2026, "federal", "mfj", standard_deduction=33_000,
        )
        config_store.upsert_tax_schedule(
            db, 2026, "federal", "single", standard_deduction=16_500,
        )
        rows = {r["filing_status"]: r for r in config_store.list_tax_schedules(db)}
        assert rows["mfj"]["standard_deduction"] == 33_000
        assert rows["single"]["standard_deduction"] == 16_500

    def test_jurisdictions_do_not_collide(self, db):
        config_store.upsert_tax_schedule(
            db, 2026, "federal", "mfj", standard_deduction=33_000,
        )
        config_store.upsert_tax_schedule(
            db, 2026, "CA", "mfj", standard_deduction=11_000,
        )
        rows = {r["jurisdiction"]: r for r in config_store.list_tax_schedules(db)}
        assert rows["federal"]["standard_deduction"] == 33_000
        assert rows["CA"]["standard_deduction"] == 11_000

    def test_upsert_merges_rather_than_replacing(self, db):
        config_store.upsert_tax_schedule(
            db, 2026, "CA", "mfj", brackets=[[0, 0.01]],
        )
        config_store.upsert_tax_schedule(
            db, 2026, "CA", "mfj", standard_deduction=11_000,
        )
        row = config_store.list_tax_schedules(db)[0]
        assert row["brackets"] == [[0, 0.01]]
        assert row["standard_deduction"] == 11_000

    def test_delete_reverts_to_bundled(self, db):
        config_store.upsert_tax_schedule(
            db, 2026, "CA", "mfj", standard_deduction=11_000,
        )
        assert config_store.delete_tax_schedule(db, 2026, "CA", "mfj") is True
        assert config_store.list_tax_schedules(db) == []

    def test_delete_missing_is_false(self, db):
        assert config_store.delete_tax_schedule(db, 2026, "CA", "mfj") is False

    def test_unknown_filing_status_rejected(self, db):
        with pytest.raises(ValueError):
            config_store.upsert_tax_schedule(db, 2026, "CA", "hoh")

    def test_unknown_jurisdiction_rejected(self, db):
        with pytest.raises(ValueError):
            config_store.upsert_tax_schedule(db, 2026, "Narnia", "mfj")

    def test_federal_and_state_codes_both_accepted(self, db):
        config_store.upsert_tax_schedule(db, 2026, "federal", "mfj")
        config_store.upsert_tax_schedule(db, 2026, "NY", "mfj")


class TestScheduleMigration:
    """The legacy year-keyed columns fold into the status-keyed table once."""

    def test_migrates_legacy_columns_under_the_configured_status(self, db):
        config_store.save_tax(db, TaxConfig(filing_status="single", tax_year=2025))
        config_store.upsert_tax_year_rates(
            db, 2025,
            federal_brackets=[[0, 0.1], [40_000, 0.2]],
            federal_standard_deduction=15_000,
            ca_brackets=[[0, 0.01]],
            ca_standard_deduction=5_363,
        )
        config_store.migrate_tax_schedules(db)

        rows = {
            (r["jurisdiction"], r["filing_status"]): r
            for r in config_store.list_tax_schedules(db)
        }
        # The configured status is the honest reading of data that never
        # recorded one.
        assert rows[("federal", "single")]["brackets"] == [[0, 0.1], [40_000, 0.2]]
        assert rows[("federal", "single")]["standard_deduction"] == 15_000
        assert rows[("CA", "single")]["brackets"] == [[0, 0.01]]
        assert ("federal", "mfj") not in rows

    def test_is_idempotent(self, db):
        config_store.save_tax(db, TaxConfig(filing_status="mfj", tax_year=2025))
        config_store.upsert_tax_year_rates(
            db, 2025, ca_standard_deduction=5_363,
        )
        config_store.migrate_tax_schedules(db)
        config_store.upsert_tax_schedule(
            db, 2025, "CA", "mfj", standard_deduction=9_999,
        )
        # A second run must not clobber the edit the user made after the first.
        config_store.migrate_tax_schedules(db)
        row = config_store.list_tax_schedules(db)[0]
        assert row["standard_deduction"] == 9_999

    def test_runs_on_a_db_with_no_legacy_rows(self, db):
        config_store.migrate_tax_schedules(db)
        assert config_store.list_tax_schedules(db) == []

    def test_payroll_scalars_stay_on_tax_year_rates(self, db):
        # Genuinely federal, year-keyed and status-agnostic — they have no
        # business in a status-keyed table.
        config_store.upsert_tax_year_rates(db, 2026, ss_wage_base=184_500)
        config_store.migrate_tax_schedules(db)
        assert config_store.list_tax_year_rates(db)[0]["ss_wage_base"] == 184_500


class TestStateSetting:
    def test_defaults_to_empty(self, db):
        assert config_store.load_tax(db).state == ""

    def test_round_trips(self, db):
        config_store.save_tax(db, TaxConfig(state="CA"))
        assert config_store.load_tax(db).state == "CA"

    def test_normalizes_to_upper_case(self, db):
        config_store.save_tax(db, TaxConfig(state="ca"))
        assert config_store.load_tax(db).state == "CA"

    def test_empty_state_round_trips_as_empty(self, db):
        # "" is a real choice — no state tax — not an unset field to be
        # defaulted back to California.
        config_store.save_tax(db, TaxConfig(state="CA"))
        config_store.save_tax(db, TaxConfig(state=""))
        assert config_store.load_tax(db).state == ""

    def test_toml_round_trip(self):
        cfg = TaxConfig(state="NY")
        out = config_store.tax_to_toml_dict(cfg)
        assert out["tax"]["state"] == "NY"
        assert config_store.tax_config_from_toml_dict(out).state == "NY"


class TestSchedulesFeedTheEstimate:
    def test_stored_override_reaches_the_computation(self, db):
        config_store.save_tax(db, TaxConfig(state="CA", filing_status="mfj",
                                            tax_year=2025))
        config_store.upsert_tax_schedule(
            db, 2025, "CA", "mfj", standard_deduction=99_999,
        )
        cfg = config_store.load_tax(db)
        assert cfg.state_standard_deduction == 99_999

    def test_override_is_scoped_to_its_filing_status(self, db):
        # The latent defect: an override entered while filing jointly silently
        # continued to apply after switching to single.
        config_store.save_tax(db, TaxConfig(state="CA", filing_status="mfj",
                                            tax_year=2025))
        config_store.upsert_tax_schedule(
            db, 2025, "CA", "mfj", standard_deduction=99_999,
        )
        config_store.save_tax(db, TaxConfig(state="CA", filing_status="single",
                                            tax_year=2025))
        cfg = config_store.load_tax(db)
        assert cfg.state_standard_deduction is None

    def test_federal_override_is_scoped_to_its_year(self, db):
        config_store.save_tax(db, TaxConfig(filing_status="mfj", tax_year=2025))
        config_store.upsert_tax_schedule(
            db, 2026, "federal", "mfj", standard_deduction=88_888,
        )
        assert config_store.load_tax(db).federal_standard_deduction is None


class TestSaveTaxDoesNotDestroyOverrides:
    """`save_tax` persists scalar edits; it must never clear a schedule row.

    Its TaxConfig may have been loaded under a different state or filing status,
    in which case the schedule fields are None because they were never *read* —
    not because the user cleared them. Treating that None as "clear" made
    turning a state on delete the override that state already had.
    """

    def test_enabling_a_state_keeps_its_existing_override(self, db):
        config_store.save_tax(db, TaxConfig(filing_status="mfj", tax_year=2025))
        config_store.upsert_tax_schedule(
            db, 2025, "CA", "mfj",
            brackets=[[0, 0.011]], standard_deduction=10_800,
        )
        # Loaded with state unset, so state_* come back None.
        cfg = config_store.load_tax(db)
        assert cfg.state_standard_deduction is None
        cfg.state = "CA"
        config_store.save_tax(db, cfg)

        reloaded = config_store.load_tax(db)
        assert reloaded.state == "CA"
        assert reloaded.state_standard_deduction == 10_800
        assert reloaded.state_brackets == [[0, 0.011]]

    def test_switching_filing_status_neither_clears_nor_copies(self, db):
        """The spec's latent defect, in both directions.

        The old year-keyed table let an override entered while filing jointly
        carry over to single. A load-modify-save must not recreate that by
        writing the loaded mfj values under the newly-selected status.
        """
        config_store.save_tax(db, TaxConfig(filing_status="mfj", tax_year=2025,
                                            state="CA"))
        config_store.upsert_tax_schedule(
            db, 2025, "CA", "mfj", standard_deduction=10_800,
        )
        cfg = config_store.load_tax(db)
        cfg.filing_status = "single"
        config_store.save_tax(db, cfg)

        assert config_store.get_tax_schedule(db, 2025, "CA", "mfj") == {
            "brackets": None, "standard_deduction": 10_800,
        }
        assert config_store.get_tax_schedule(db, 2025, "CA", "single") is None

    def test_switching_year_neither_clears_nor_copies(self, db):
        config_store.save_tax(db, TaxConfig(filing_status="mfj", tax_year=2025))
        config_store.upsert_tax_schedule(
            db, 2025, "federal", "mfj", standard_deduction=31_000,
        )
        cfg = config_store.load_tax(db)
        cfg.tax_year = 2026
        config_store.save_tax(db, cfg)

        assert config_store.get_tax_schedule(db, 2025, "federal", "mfj") == {
            "brackets": None, "standard_deduction": 31_000,
        }
        assert config_store.get_tax_schedule(db, 2026, "federal", "mfj") is None

    def test_switching_state_does_not_copy_onto_the_new_state(self, db):
        config_store.save_tax(db, TaxConfig(filing_status="mfj", tax_year=2025,
                                            state="CA"))
        config_store.upsert_tax_schedule(
            db, 2025, "CA", "mfj", standard_deduction=10_800,
        )
        cfg = config_store.load_tax(db)
        cfg.state = "NY"
        config_store.save_tax(db, cfg)

        assert config_store.get_tax_schedule(db, 2025, "NY", "mfj") is None
        assert config_store.get_tax_schedule(db, 2025, "CA", "mfj") == {
            "brackets": None, "standard_deduction": 10_800,
        }

    def test_an_importer_may_write_schedules_explicitly(self, db):
        """The TOML import path genuinely does carry the user's own rates."""
        cfg = TaxConfig(
            filing_status="mfj", tax_year=2025, state="CA",
            federal_standard_deduction=31_000,
            state_brackets=[[0, 0.011]],
        )
        config_store.save_tax(db, cfg, write_schedules=True)
        assert config_store.get_tax_schedule(db, 2025, "federal", "mfj") == {
            "brackets": None, "standard_deduction": 31_000,
        }
        assert config_store.get_tax_schedule(db, 2025, "CA", "mfj") == {
            "brackets": [[0, 0.011]], "standard_deduction": None,
        }

    def test_setting_one_field_does_not_clear_the_other(self, db):
        config_store.save_tax(db, TaxConfig(filing_status="mfj", tax_year=2025,
                                            state="CA"))
        config_store.upsert_tax_schedule(
            db, 2025, "CA", "mfj",
            brackets=[[0, 0.011]], standard_deduction=10_800,
        )
        config_store.save_tax(
            db,
            TaxConfig(filing_status="mfj", tax_year=2025, state="CA",
                      state_standard_deduction=99),
            write_schedules=True,
        )
        assert config_store.get_tax_schedule(db, 2025, "CA", "mfj") == {
            "brackets": [[0, 0.011]], "standard_deduction": 99,
        }

    def test_a_migrated_override_survives_being_switched_on(self, db):
        """The end-to-end path the bug was found on."""
        import json
        import sqlite3

        config_store.init_db(db)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO tax_settings(key, value) VALUES ('filing_status', 'mfj')"
            )
            conn.execute(
                "INSERT INTO tax_settings(key, value) VALUES ('tax_year', '2025')"
            )
            conn.execute(
                "INSERT INTO tax_year_rates(tax_year, ca_standard_deduction,"
                " ca_brackets_json) VALUES (2025, 10800, ?)",
                (json.dumps([[0, 0.011]]),),
            )
        config_store.migrate_tax_schedules(db)

        cfg = config_store.load_tax(db)
        cfg.state = "CA"
        config_store.save_tax(db, cfg)

        reloaded = config_store.load_tax(db)
        assert reloaded.state_standard_deduction == 10_800
        assert reloaded.state_brackets == [[0, 0.011]]
