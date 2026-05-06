"""Tests for money.config_store — DB-backed config storage."""

from __future__ import annotations

import json
import sqlite3
import tomli

import pytest

from istota.money import config_store as cs
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


# Real-world TOML fixtures (sanitized) lifted from the production configs.

INVOICING_TOML = """\
accounting_path = "."
invoice_output = "invoices/generated"
next_invoice_number = 236

default_entity = "ochotona"
default_ar_account = "Assets:Accounts-Receivable"
default_bank_account = "Assets:SK-Income-Fidelity"
currency = "USD"

[companies.ochotona]
name = "Ochotona LLC"
address = "1 Sample St\\nCity, State 12345"
email = "billing@example.com"
payment_instructions = "Pay via ACH"
ar_account = "Assets:Accounts-Receivable"
bank_account = "Assets:SK-Income-Fidelity"
currency = "USD"

[companies.personal]
name = "Personal"
address = "1 Sample St"
email = "me@example.com"

[clients.acme]
name = "Acme Corp"
address = "100 Acme Way"
email = "ap@acme.example"
terms = "On receipt"
ar_account = "Assets:Accounts-Receivable"
entity = "ochotona"

[clients.acme.invoicing]
schedule = "monthly"
day = 1
ledger_posting = true
reminder_days = 5
notifications = "billing@example.com"
days_until_overdue = 30

[clients.sosf]
name = "SOSF"
terms = 30
entity = "personal"

[clients.sosf.invoicing]
schedule = "monthly"
day = 15
separate = ["consulting", "training"]

[services.consulting]
display_name = "Consulting"
rate = 150.0
type = "hours"
income_account = "Income:Consulting"

[services.flat]
display_name = "Flat Project"
rate = 5000.0
type = "flat"
"""


TAX_TOML = """\
[tax]
filing_status = "mfj"
tax_year = 2026

[tax.w2]
income = 80000
federal_withholding = 12000
state_withholding = 4000

[tax.estimated_payments]
federal = 5000
state = 1500

[tax.options]
enable_qbi_deduction = true

[tax.accounts]
se_income = ["Income:ScheduleC", "Income:Side"]
se_expenses = ["Expenses:Business"]

[tax.safe_harbor]
prior_year_federal_tax = 25000
prior_year_state_tax = 8000

[tax.rates]
ss_wage_base = 176100
ss_rate = 0.124
medicare_rate = 0.029
se_taxable_fraction = 0.9235
federal_standard_deduction = 30000
ca_standard_deduction = 10726
federal_brackets = [[0, 0.1], [23850, 0.12], [96950, 0.22]]
ca_brackets = [[0, 0.01], [21428, 0.02]]
"""


MONARCH_TOML = """\
[monarch.sync]
lookback_days = 30

[monarch.profiles.cynium]
ledger = "cynium"
default_account = "Assets:Cynium:Bank"

[monarch.profiles.cynium.tags]
include = ["Stefan Business"]

[monarch.profiles.cynium.accounts]
"Cynium Visa" = "Liabilities:Cynium:Visa"
"Cynium Bank" = "Assets:Cynium:Bank"

[monarch.profiles.cynium.categories]
"Software" = "Expenses:Cynium:Software"

[monarch.profiles.personal]
ledger = "personal"
lookback_days = 60
recategorize_account = "Expenses:Personal:Misc"

[monarch.profiles.personal.tags]
exclude = ["Hide"]

[monarch.profiles.personal.accounts]
"Fidelity VISA" = "Liabilities:Visa-Fidelity"
"""


class TestInitDb:
    def test_creates_all_tables(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for expected in (
            "schema_meta",
            "invoicing_settings", "invoicing_companies", "invoicing_clients",
            "invoicing_services",
            "tax_settings", "tax_account_patterns", "tax_year_rates",
            "monarch_settings", "monarch_profiles", "monarch_account_map",
            "monarch_category_map", "monarch_tag_filters",
        ):
            assert expected in tables, f"missing table: {expected}"

    def test_idempotent(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        cs.init_db(db_path)  # no error
        assert cs.get_meta(db_path, "schema_version") == "1"

    def test_global_profile_row_present(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT name FROM monarch_profiles WHERE id = 0"
            ).fetchone()
        assert row[0] == "__global__"


class TestInvoicingRoundTrip:
    def test_round_trip_dict_save_load(self, tmp_path):
        data = tomli.loads(INVOICING_TOML)
        cfg = cs.invoicing_config_from_toml_dict(data)
        db_path = tmp_path / "money.db"
        cs.save_invoicing(db_path, cfg)
        loaded = cs.load_invoicing(db_path)

        assert loaded.accounting_path == "."
        assert loaded.next_invoice_number == 236
        assert loaded.default_entity == "ochotona"
        assert loaded.default_bank_account == "Assets:SK-Income-Fidelity"
        assert loaded.currency == "USD"

        assert set(loaded.companies) == {"ochotona", "personal"}
        assert loaded.companies["ochotona"].bank_account == "Assets:SK-Income-Fidelity"
        assert loaded.company.key == "ochotona"

        assert set(loaded.clients) == {"acme", "sosf"}
        acme = loaded.clients["acme"]
        assert acme.terms == "On receipt"
        assert acme.schedule == "monthly"
        assert acme.schedule_day == 1
        assert acme.ledger_posting is True
        assert acme.reminder_days == 5
        assert acme.notifications == "billing@example.com"
        assert acme.days_until_overdue == 30

        sosf = loaded.clients["sosf"]
        assert sosf.terms == 30
        assert sosf.schedule_day == 15
        assert sosf.separate == ["consulting", "training"]

        assert set(loaded.services) == {"consulting", "flat"}
        assert loaded.services["consulting"].rate == 150.0
        assert loaded.services["flat"].type == "flat"

    def test_to_toml_dict_round_trip(self, tmp_path):
        data = tomli.loads(INVOICING_TOML)
        cfg = cs.invoicing_config_from_toml_dict(data)
        out = cs.invoicing_to_toml_dict(cfg)
        # Re-hydrate, save+load, render again — should match the first render.
        cfg2 = cs.invoicing_config_from_toml_dict(out)
        db_path = tmp_path / "money.db"
        cs.save_invoicing(db_path, cfg2)
        roundtripped = cs.load_invoicing(db_path)
        out2 = cs.invoicing_to_toml_dict(roundtripped)
        assert out == out2

    def test_legacy_company_block(self, tmp_path):
        toml = (
            'accounting_path = "."\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "Default Co"\n\n'
            '[clients.foo]\nname = "Foo"\n\n'
            '[services.bar]\ndisplay_name = "Bar"\nrate = 100\n'
        )
        cfg = cs.invoicing_config_from_toml_dict(tomli.loads(toml))
        assert "default" in cfg.companies
        assert cfg.companies["default"].name == "Default Co"


class TestInvoicingGranular:
    def test_upsert_company_create_then_update_then_noop(self, tmp_path):
        db_path = tmp_path / "money.db"
        comp, state = cs.upsert_company(db_path, "acme", name="Acme")
        assert state == "created"
        comp, state = cs.upsert_company(db_path, "acme", address="123 St")
        assert state == "updated"
        assert comp.address == "123 St"
        comp, state = cs.upsert_company(db_path, "acme", address="123 St")
        assert state == "noop"

    def test_delete_company(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_company(db_path, "acme", name="Acme")
        assert cs.delete_company(db_path, "acme") is True
        assert cs.delete_company(db_path, "acme") is False

    def test_upsert_client(self, tmp_path):
        db_path = tmp_path / "money.db"
        client, state = cs.upsert_client(db_path, "acme", name="Acme")
        assert state == "created"
        client, state = cs.upsert_client(db_path, "acme", terms="NET 15")
        assert state == "updated"
        assert client.terms == "NET 15"

    def test_upsert_service(self, tmp_path):
        db_path = tmp_path / "money.db"
        svc, state = cs.upsert_service(db_path, "consulting",
                                       display_name="Consulting", rate=150.0)
        assert state == "created"
        assert svc.rate == 150.0


class TestTaxRoundTrip:
    def test_round_trip(self, tmp_path):
        data = tomli.loads(TAX_TOML)
        cfg = cs.tax_config_from_toml_dict(data)
        db_path = tmp_path / "money.db"
        cs.save_tax(db_path, cfg)
        loaded = cs.load_tax(db_path)

        assert loaded.filing_status == "mfj"
        assert loaded.tax_year == 2026
        assert loaded.w2_income == 80000
        assert loaded.federal_estimated_paid == 5000
        assert loaded.enable_qbi_deduction is True
        assert sorted(loaded.se_income_accounts) == [
            "Income:ScheduleC", "Income:Side",
        ]
        assert loaded.prior_year_federal_tax == 25000
        assert loaded.federal_standard_deduction == 30000.0
        assert loaded.ca_standard_deduction == 10726.0
        assert loaded.federal_brackets == [[0, 0.1], [23850, 0.12], [96950, 0.22]]
        assert loaded.ss_wage_base == 176100

    def test_to_toml_dict_round_trip(self, tmp_path):
        data = tomli.loads(TAX_TOML)
        cfg = cs.tax_config_from_toml_dict(data)
        out = cs.tax_to_toml_dict(cfg)
        cfg2 = cs.tax_config_from_toml_dict(out)
        db_path = tmp_path / "money.db"
        cs.save_tax(db_path, cfg2)
        roundtripped = cs.load_tax(db_path)
        out2 = cs.tax_to_toml_dict(roundtripped)
        assert out == out2

    def test_patterns_add_remove(self, tmp_path):
        db_path = tmp_path / "money.db"
        assert cs.add_tax_pattern(db_path, "se_income", "Income:Side") == "created"
        assert cs.add_tax_pattern(db_path, "se_income", "Income:Side") == "noop"
        patterns = cs.list_tax_patterns(db_path)
        assert "Income:Side" in patterns["se_income"]
        assert cs.remove_tax_pattern(db_path, "se_income", "Income:Side") is True

    def test_year_rates_upsert(self, tmp_path):
        db_path = tmp_path / "money.db"
        state = cs.upsert_tax_year_rates(
            db_path, 2026,
            ss_wage_base=176100, ss_rate=0.124, federal_standard_deduction=30000,
        )
        assert state == "created"
        state = cs.upsert_tax_year_rates(
            db_path, 2026, federal_standard_deduction=30000,
        )
        assert state == "noop"
        state = cs.upsert_tax_year_rates(db_path, 2026, ca_standard_deduction=10726)
        assert state == "updated"


class TestMonarchRoundTrip:
    def test_round_trip(self, tmp_path):
        data = tomli.loads(MONARCH_TOML)
        cfg = cs.monarch_config_from_toml_dict(data)
        db_path = tmp_path / "money.db"
        cs.save_monarch(db_path, cfg)
        loaded = cs.load_monarch(db_path)

        assert loaded.sync.lookback_days == 30
        # Profiles preserved
        names = sorted(p.name for p in loaded.profiles)
        assert names == ["cynium", "personal"]

        cynium = next(p for p in loaded.profiles if p.name == "cynium")
        assert cynium.ledger == "cynium"
        assert cynium.sync.default_account == "Assets:Cynium:Bank"
        assert cynium.tags.include == ["Stefan Business"]
        assert cynium.accounts == {
            "Cynium Visa": "Liabilities:Cynium:Visa",
            "Cynium Bank": "Assets:Cynium:Bank",
        }
        assert cynium.categories == {"Software": "Expenses:Cynium:Software"}

        personal = next(p for p in loaded.profiles if p.name == "personal")
        assert personal.sync.lookback_days == 60
        assert personal.sync.recategorize_account == "Expenses:Personal:Misc"
        assert personal.tags.exclude == ["Hide"]

    def test_to_toml_dict_round_trip(self, tmp_path):
        data = tomli.loads(MONARCH_TOML)
        cfg = cs.monarch_config_from_toml_dict(data)
        out = cs.monarch_to_toml_dict(cfg)
        cfg2 = cs.monarch_config_from_toml_dict(out)
        db_path = tmp_path / "money.db"
        cs.save_monarch(db_path, cfg2)
        roundtripped = cs.load_monarch(db_path)
        out2 = cs.monarch_to_toml_dict(roundtripped)
        assert out == out2

    def test_credentials_omitted_from_export(self, tmp_path):
        cfg = MonarchConfig(
            credentials=MonarchCredentials(email="a@b", password="x"),
            sync=MonarchSyncSettings(),
            accounts={}, categories={}, tags=MonarchTagFilters(),
            profiles=[],
        )
        out = cs.monarch_to_toml_dict(cfg)
        assert "email" not in out["monarch"]
        assert "password" not in out["monarch"]
        assert "session_token" not in out["monarch"]

    def test_credentials_loaded_from_secrets(self, tmp_path):
        db_path = tmp_path / "money.db"
        cfg = MonarchConfig(
            credentials=MonarchCredentials(),
            sync=MonarchSyncSettings(),
            accounts={}, categories={}, tags=MonarchTagFilters(),
            profiles=[],
        )
        cs.save_monarch(db_path, cfg)
        loaded = cs.load_monarch(
            db_path, secrets={"monarch": {"email": "a@b", "session_token": "tok"}},
        )
        assert loaded.credentials.email == "a@b"
        assert loaded.credentials.session_token == "tok"


class TestMonarchGranular:
    def test_profile_lifecycle(self, tmp_path):
        db_path = tmp_path / "money.db"
        prof, state = cs.upsert_monarch_profile(
            db_path, "cynium", ledger="cynium",
        )
        assert state == "created"
        assert prof["ledger"] == "cynium"
        prof, state = cs.upsert_monarch_profile(
            db_path, "cynium", lookback_days=60,
        )
        assert state == "updated"
        assert cs.delete_monarch_profile(db_path, "cynium") is True
        assert cs.delete_monarch_profile(db_path, "cynium") is False

    def test_global_profile_cannot_be_deleted(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        assert cs.delete_monarch_profile(db_path, "__global__") is False

    def test_account_map_set_unset(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_monarch_profile(db_path, "cynium", ledger="cynium")
        assert cs.set_account_map_entry(
            db_path, "cynium", "Visa", "Liabilities:Visa",
        ) == "created"
        assert cs.set_account_map_entry(
            db_path, "cynium", "Visa", "Liabilities:Visa",
        ) == "noop"
        assert cs.set_account_map_entry(
            db_path, "cynium", "Visa", "Liabilities:NewVisa",
        ) == "updated"
        assert cs.get_account_map(db_path, "cynium") == {
            "Visa": "Liabilities:NewVisa",
        }
        assert cs.unset_account_map_entry(db_path, "cynium", "Visa") is True

    def test_account_map_global_scope(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.set_account_map_entry(db_path, None, "Bank", "Assets:Bank")
        assert cs.get_account_map(db_path, None) == {"Bank": "Assets:Bank"}

    def test_unknown_profile_raises(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with pytest.raises(ValueError):
            cs.set_account_map_entry(db_path, "nonexistent", "X", "Y")

    def test_tag_filters(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_monarch_profile(db_path, "cynium", ledger="cynium")
        assert cs.add_tag_filter(db_path, "cynium", "include", "Biz") == "created"
        assert cs.add_tag_filter(db_path, "cynium", "include", "Biz") == "noop"
        assert cs.get_tag_filters(db_path, "cynium") == {
            "include": ["Biz"], "exclude": [],
        }
        assert cs.remove_tag_filter(db_path, "cynium", "include", "Biz") is True


class TestSchemaMeta:
    def test_has_data_helpers(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        assert cs.has_invoicing_data(db_path) is False
        assert cs.has_tax_data(db_path) is False
        assert cs.has_monarch_data(db_path) is False
        cs.upsert_client(db_path, "acme", name="Acme")
        assert cs.has_invoicing_data(db_path) is True

    def test_meta_set_get(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        cs.set_meta(db_path, "test_key", "test_value")
        assert cs.get_meta(db_path, "test_key") == "test_value"


class TestReplaceVsMerge:
    def test_save_replace_truncates(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_client(db_path, "old", name="Old Client")
        new_cfg = InvoicingConfig(
            accounting_path=".",
            invoice_output="invoices/generated",
            next_invoice_number=1,
            company=CompanyConfig(name="X", key="x"),
            clients={"new": ClientConfig(key="new", name="New")},
            services={},
            companies={"x": CompanyConfig(name="X", key="x")},
            default_entity="x",
        )
        cs.save_invoicing(db_path, new_cfg, replace_collections=True)
        loaded = cs.load_invoicing(db_path)
        assert set(loaded.clients) == {"new"}

    def test_save_merge_preserves_existing(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_client(db_path, "old", name="Old Client")
        new_cfg = InvoicingConfig(
            accounting_path=".",
            invoice_output="invoices/generated",
            next_invoice_number=1,
            company=CompanyConfig(name="X", key="x"),
            clients={"new": ClientConfig(key="new", name="New")},
            services={},
            companies={"x": CompanyConfig(name="X", key="x")},
            default_entity="x",
        )
        cs.save_invoicing(db_path, new_cfg, replace_collections=False)
        loaded = cs.load_invoicing(db_path)
        assert {"new", "old"} <= set(loaded.clients)


# =============================================================================
# Regression tests from mulder/scully review
# =============================================================================


class TestHasDataExcludesScalarRoundTrip:
    """Mulder P0: save→load→save of empty cfg must not flag DB-populated."""

    def test_tax_save_load_save_does_not_block_migration(self, tmp_path):
        db_path = tmp_path / "money.db"
        cfg = cs.load_tax(db_path)  # empty defaults
        cs.save_tax(db_path, cfg)
        # tax_settings now has filing_status + tax_year, but the section
        # should NOT be considered "populated" — collection tables are empty.
        assert cs.has_tax_data(db_path) is False
        # Once we add a real pattern, it does count as populated.
        cs.add_tax_pattern(db_path, "se_income", "Income:Real")
        assert cs.has_tax_data(db_path) is True

    def test_monarch_save_load_save_does_not_block_migration(self, tmp_path):
        db_path = tmp_path / "money.db"
        cfg = cs.load_monarch(db_path)  # empty defaults
        cs.save_monarch(db_path, cfg)
        # monarch_settings has the three sync defaults; not "populated".
        assert cs.has_monarch_data(db_path) is False
        cs.upsert_monarch_profile(db_path, "real", ledger="real")
        assert cs.has_monarch_data(db_path) is True


class TestReplaceTaxPatterns:
    """Mulder P1 #4: replace_tax_patterns helper used by routes."""

    def test_replace_per_kind(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.add_tax_pattern(db_path, "se_income", "Income:Old")
        cs.add_tax_pattern(db_path, "se_expense", "Expenses:Old")
        cs.replace_tax_patterns(db_path, {"se_income": ["Income:New"]})
        patterns = cs.list_tax_patterns(db_path)
        assert patterns["se_income"] == ["Income:New"]
        # se_expense untouched (not in the dict)
        assert patterns["se_expense"] == ["Expenses:Old"]

    def test_unknown_kind_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            cs.replace_tax_patterns(tmp_path / "money.db", {"bogus": ["x"]})
