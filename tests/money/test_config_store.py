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

[clients.globex]
name = "Globex"
terms = 30
entity = "personal"

[clients.globex.invoicing]
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
state = "CA"

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
state_standard_deduction = 10726
federal_brackets = [[0, 0.1], [23850, 0.12], [96950, 0.22]]
state_brackets = [[0, 0.01], [21428, 0.02]]
"""


MONARCH_TOML = """\
[monarch.sync]
lookback_days = 30

[monarch.profiles.acme]
ledger = "cynium"
default_account = "Assets:Acme:Bank"

[monarch.profiles.acme.tags]
include = ["Consulting"]

[monarch.profiles.acme.accounts]
"Acme Visa" = "Liabilities:Acme:Visa"
"Cynium Bank" = "Assets:Acme:Bank"

[monarch.profiles.acme.categories]
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

        assert set(loaded.clients) == {"acme", "globex"}
        acme = loaded.clients["acme"]
        assert acme.terms == "On receipt"
        assert acme.schedule == "monthly"
        assert acme.schedule_day == 1
        assert acme.ledger_posting is True
        assert acme.reminder_days == 5
        assert acme.notifications == "billing@example.com"
        assert acme.days_until_overdue == 30

        globex = loaded.clients["globex"]
        assert globex.terms == 30
        assert globex.schedule_day == 15
        assert globex.separate == ["consulting", "training"]

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
        assert loaded.state == "CA"
        assert loaded.state_standard_deduction == 10726.0
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
        assert cynium.sync.default_account == "Assets:Acme:Bank"
        assert cynium.tags.include == ["Consulting"]
        assert cynium.accounts == {
            "Acme Visa": "Liabilities:Acme:Visa",
            "Cynium Bank": "Assets:Acme:Bank",
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
            credentials=MonarchCredentials(session_id="s", csrftoken="c"),
            sync=MonarchSyncSettings(),
            accounts={}, categories={}, tags=MonarchTagFilters(),
            profiles=[],
        )
        out = cs.monarch_to_toml_dict(cfg)
        assert "session_id" not in out["monarch"]
        assert "csrftoken" not in out["monarch"]

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
            db_path,
            secrets={"monarch": {"session_id": "SID-x", "csrftoken": "CSRF-y"}},
        )
        assert loaded.credentials.session_id == "SID-x"
        assert loaded.credentials.csrftoken == "CSRF-y"


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


# =============================================================================
# Invoicing collection validation (money-config-editing spec, Stage 1)
#
# The invariants whose violation changes behaviour *silently* live in the
# store, not the route, so the CLI and the agent are held to them too.
# =============================================================================


class TestKeyValidation:
    def test_new_key_must_be_slug_shaped(self, tmp_path):
        db_path = tmp_path / "money.db"
        for bad in ("has space", "has.dot", "-leading", "", "a" * 65):
            with pytest.raises(ValueError, match="key"):
                cs.upsert_client(db_path, bad, name="X")

    def test_valid_keys_accepted(self, tmp_path):
        db_path = tmp_path / "money.db"
        for good in ("acme", "acme-corp", "acme_corp", "9to5", "a" * 64):
            cs.upsert_client(db_path, good, name="X")

    def test_existing_nonconforming_key_still_updatable(self, tmp_path):
        """A legacy key with a dot in it must stay editable.

        The rule exists so *new* keys stay TOML- and CLI-friendly; enforcing
        it on every write would lock a user out of their own data.
        """
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with cs._connect(db_path) as conn:
            conn.execute(
                "INSERT INTO invoicing_clients(key, name) VALUES (?, ?)",
                ("legacy.key", "Legacy"),
            )
        client, state = cs.upsert_client(db_path, "legacy.key", email="x@example.com")
        assert state == "updated"
        assert client.email == "x@example.com"

    def test_applies_to_companies_and_services(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="key"):
            cs.upsert_company(db_path, "bad key", name="X")
        with pytest.raises(ValueError, match="key"):
            cs.upsert_service(db_path, "bad key", display_name="X", rate=1)


class TestServiceValidation:
    def test_type_is_a_closed_set(self, tmp_path):
        db_path = tmp_path / "money.db"
        # "hourly" is the plausible typo: entry_line_item has no branch for it
        # and silently bills as hours.
        with pytest.raises(ValueError, match="type"):
            cs.upsert_service(db_path, "consulting", type="hourly")
        for good in ("hours", "days", "flat", "other"):
            cs.upsert_service(db_path, "consulting", type=good)

    def test_rate_must_be_a_finite_non_negative_number(self, tmp_path):
        db_path = tmp_path / "money.db"
        for bad in ("abc", -1, float("nan"), float("inf")):
            with pytest.raises(ValueError, match="rate"):
                cs.upsert_service(db_path, "consulting", rate=bad)
        cs.upsert_service(db_path, "consulting", rate="150.5")
        assert cs.load_invoicing(db_path).services["consulting"].rate == 150.5

    def test_income_account_shape(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="income_account"):
            cs.upsert_service(db_path, "consulting", income_account="income consulting")
        cs.upsert_service(db_path, "consulting", income_account="Income:Consulting")
        # Empty clears the field rather than failing the shape check.
        cs.upsert_service(db_path, "consulting", income_account="")

    def test_unknown_field_rejected(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="unknown"):
            cs.upsert_service(db_path, "consulting", rat=150)


class TestClientValidation:
    def test_schedule_is_a_closed_set(self, tmp_path):
        db_path = tmp_path / "money.db"
        # check_scheduled_invoices only acts on "monthly" — anything else is
        # accepted and then never fires.
        with pytest.raises(ValueError, match="schedule"):
            cs.upsert_client(db_path, "acme", schedule="weekly")
        for good in ("on-demand", "monthly"):
            cs.upsert_client(db_path, "acme", schedule=good)

    def test_schedule_day_range(self, tmp_path):
        db_path = tmp_path / "money.db"
        for bad in (0, 40, "x", 1.5):
            with pytest.raises(ValueError, match="schedule_day"):
                cs.upsert_client(db_path, "acme", schedule_day=bad)
        cs.upsert_client(db_path, "acme", schedule_day=15)

    def test_non_negative_day_counts(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="reminder_days"):
            cs.upsert_client(db_path, "acme", reminder_days=-1)
        with pytest.raises(ValueError, match="days_until_overdue"):
            cs.upsert_client(db_path, "acme", days_until_overdue=-1)
        cs.upsert_client(db_path, "acme", reminder_days=0, days_until_overdue=45)

    def test_terms_int_or_nonempty_string(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_client(db_path, "acme", terms=30)
        cs.upsert_client(db_path, "acme", terms="NET 15")
        with pytest.raises(ValueError, match="terms"):
            cs.upsert_client(db_path, "acme", terms=-5)
        with pytest.raises(ValueError, match="terms"):
            cs.upsert_client(db_path, "acme", terms="")

    def test_ar_account_shape(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="ar_account"):
            cs.upsert_client(db_path, "acme", ar_account="assets receivable")
        cs.upsert_client(db_path, "acme", ar_account="Assets:Accounts-Receivable")
        cs.upsert_client(db_path, "acme", ar_account="")

    def test_booleans_are_not_numbers(self, tmp_path):
        """JSON `true` is an int to Python and would sail into a day field."""
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="schedule_day"):
            cs.upsert_client(db_path, "acme", schedule_day=True)

    def test_unknown_field_rejected(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="unknown"):
            cs.upsert_client(db_path, "acme", nmae="Acme")


class TestCompanyValidation:
    def test_account_and_currency_shape(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="bank_account"):
            cs.upsert_company(db_path, "ochotona", bank_account="checking")
        with pytest.raises(ValueError, match="currency"):
            cs.upsert_company(db_path, "ochotona", currency="us dollars")
        cs.upsert_company(
            db_path, "ochotona",
            bank_account="Assets:Bank:Checking", currency="USD",
            ar_account="Assets:Accounts-Receivable",
        )

    def test_multiline_text_fields_allowed(self, tmp_path):
        """Address and payment instructions are genuinely multi-line."""
        db_path = tmp_path / "money.db"
        comp, _ = cs.upsert_company(
            db_path, "ochotona", address="1 St\nCity", payment_instructions="Wire\nIBAN",
        )
        assert comp.address == "1 St\nCity"

    def test_unknown_field_rejected(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="unknown"):
            cs.upsert_company(db_path, "ochotona", nmae="X")


class TestValidationRejectsBeforeWriting:
    def test_failed_upsert_leaves_no_row(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError):
            cs.upsert_service(db_path, "consulting", type="hourly")
        assert "consulting" not in cs.load_invoicing(db_path).services


class TestClientKeyIsLowercase:
    """A mixed-case client key matches no work entry, so its work never bills.

    `add_work_entry` stores `client.lower()` and `build_line_items` looks the
    client up by the entry's (lowercased) key, so an `Acme` config key silently
    produces empty invoices. Only clients are constrained — `service` and
    `entity` are stored verbatim on the entry.
    """

    def test_mixed_case_client_key_refused_on_create(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="lowercase"):
            cs.upsert_client(db_path, "Acme", name="Acme Corp")
        assert "Acme" not in cs.load_invoicing(db_path).clients

    def test_lowercase_client_key_accepted(self, tmp_path):
        db_path = tmp_path / "money.db"
        client, state = cs.upsert_client(db_path, "acme", name="Acme Corp")
        assert (client.key, state) == ("acme", "created")

    def test_existing_mixed_case_client_stays_editable(self, tmp_path):
        """The rule fires on create only, so a legacy row can still be fixed."""
        db_path = tmp_path / "money.db"
        cs.upsert_company(db_path, "main")  # unrelated collection, still mixed-case ok
        cs.upsert_company(db_path, "Main")
        with cs._connect(db_path) as conn:
            conn.execute(
                "INSERT INTO invoicing_clients(key, name) VALUES (?, ?)", ("Legacy", "Legacy"),
            )
        client, _ = cs.upsert_client(db_path, "Legacy", name="Renamed")
        assert client.name == "Renamed"

    def test_entities_and_services_may_be_mixed_case(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_company(db_path, "MainCo", name="Main Co")
        cs.upsert_service(db_path, "DesignWork", display_name="Design")
        cfg = cs.load_invoicing(db_path)
        assert "MainCo" in cfg.companies
        assert "DesignWork" in cfg.services


class TestUnchangedFieldsAreGrandfathered:
    """A legacy row with one bad value has to stay editable.

    A form seeds every input from the stored value and sends the lot back, so
    validating a field the caller didn't change makes such a row permanently
    unsaveable — and the error names a field the user never touched.
    """

    def _legacy_service(self, db_path):
        cs.init_db(db_path)
        with cs._connect(db_path) as conn:
            conn.execute(
                "INSERT INTO invoicing_services(key, display_name, rate, type) "
                "VALUES (?, ?, ?, ?)", ("consulting", "Consulting", 150.0, "hourly"),
            )

    def test_resending_an_unchanged_bad_type_is_allowed(self, tmp_path):
        db_path = tmp_path / "money.db"
        self._legacy_service(db_path)
        svc, _ = cs.upsert_service(
            db_path, "consulting", display_name="Renamed", type="hourly", rate=150.0,
        )
        assert svc.display_name == "Renamed"
        assert svc.type == "hourly"

    def test_changing_a_bad_value_to_another_bad_one_is_refused(self, tmp_path):
        db_path = tmp_path / "money.db"
        self._legacy_service(db_path)
        with pytest.raises(ValueError, match="type"):
            cs.upsert_service(db_path, "consulting", type="weekly")

    def test_legacy_client_schedule_survives_a_rename(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with cs._connect(db_path) as conn:
            conn.execute(
                "INSERT INTO invoicing_clients(key, name, schedule) VALUES (?, ?, ?)",
                ("acme", "Acme", "weekly"),
            )
        client, _ = cs.upsert_client(db_path, "acme", name="Acme Corp", schedule="weekly")
        assert client.name == "Acme Corp"

    def test_legacy_account_survives_a_rename(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.init_db(db_path)
        with cs._connect(db_path) as conn:
            conn.execute(
                "INSERT INTO invoicing_companies(key, name, ar_account) VALUES (?, ?, ?)",
                ("main", "Main", "assets:ar"),
            )
        comp, _ = cs.upsert_company(db_path, "main", name="Main Co", ar_account="assets:ar")
        assert comp.name == "Main Co"
        with pytest.raises(ValueError, match="ar_account"):
            cs.upsert_company(db_path, "main", ar_account="still:not valid")

    def test_a_new_record_gets_no_exemption(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="type"):
            cs.upsert_service(db_path, "new", type="hourly")


class TestTermsAsNumericString:
    """The column is TEXT and the loader coerces it back, so "-5" *is* -5."""

    def test_negative_numeric_string_refused(self, tmp_path):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="terms"):
            cs.upsert_client(db_path, "acme", terms="-5")

    def test_a_label_is_still_accepted(self, tmp_path):
        db_path = tmp_path / "money.db"
        client, _ = cs.upsert_client(db_path, "acme", terms="NET 15")
        assert client.terms == "NET 15"

    def test_a_non_negative_numeric_string_round_trips_as_int(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_client(db_path, "acme", terms="45")
        assert cs.load_invoicing(db_path).clients["acme"].terms == 45


class TestAccountShapeIsUnicodeAware:
    """Beancount's own account regex is Unicode; an ASCII-only check locks a
    non-English ledger out of the account it has been posting to."""

    @pytest.mark.parametrize("account", [
        "Assets:Forderungen:Müller",
        "Assets:Accounts-Receivable",
        "Income:Consulting",
        "Aktiva:Bank:Girokonto",
        "Assets:Bank:2024",
    ])
    def test_valid_accounts_accepted(self, tmp_path, account):
        db_path = tmp_path / f"{abs(hash(account))}.db"
        cs.upsert_company(db_path, "main", ar_account=account)

    @pytest.mark.parametrize("account", [
        "assets:ar",            # lowercase root
        "Assets",               # single component
        "Assets:Bank_Checking",  # underscore
        "Assets: Bank",         # space
    ])
    def test_invalid_accounts_refused(self, tmp_path, account):
        db_path = tmp_path / f"{abs(hash(account))}.db"
        with pytest.raises(ValueError, match="ar_account"):
            cs.upsert_company(db_path, "main", ar_account=account)

    def test_single_letter_commodity_accepted(self, tmp_path):
        db_path = tmp_path / "money.db"
        comp, _ = cs.upsert_company(db_path, "main", currency="X")
        assert comp.currency == "X"


class TestLogoStaysInsideTheWorkspace:
    """The logo is base64-embedded into the PDF, resolved as
    `accounting_path / logo` — pathlib lets an absolute operand escape."""

    @pytest.mark.parametrize("logo", ["/etc/passwd", "../../secrets.png", "~/private.png"])
    def test_escaping_paths_refused(self, tmp_path, logo):
        db_path = tmp_path / "money.db"
        with pytest.raises(ValueError, match="logo"):
            cs.upsert_company(db_path, "main", logo=logo)

    def test_relative_path_accepted(self, tmp_path):
        db_path = tmp_path / "money.db"
        comp, _ = cs.upsert_company(db_path, "main", logo="invoices/logo.png")
        assert comp.logo == "invoices/logo.png"


class TestCreateOnly:
    """The 409 is decided inside the write transaction, so two concurrent
    creates can't both pass a pre-check and have the second overwrite."""

    def test_create_only_refuses_an_existing_key(self, tmp_path):
        db_path = tmp_path / "money.db"
        cs.upsert_client(db_path, "acme", name="Acme")
        with pytest.raises(cs.KeyExistsError):
            cs.upsert_client(db_path, "acme", create_only=True, name="Other")
        assert cs.load_invoicing(db_path).clients["acme"].name == "Acme"

    def test_create_only_allows_a_fresh_key(self, tmp_path):
        db_path = tmp_path / "money.db"
        _, state = cs.upsert_service(db_path, "design", create_only=True, display_name="Design")
        assert state == "created"

    def test_key_exists_error_is_a_value_error(self, tmp_path):
        """So an `except ValueError` caller keeps behaving as before."""
        assert issubclass(cs.KeyExistsError, ValueError)


class TestSaveInvoicingSanitizes:
    """`save_invoicing` is the bulk path the migration and `config import` use.

    It bypassed the per-field validation entirely, so the exact values the
    granular ops exist to keep out could still land in the store.
    """

    def test_out_of_set_service_type_is_coerced(self, tmp_path, caplog):
        db_path = tmp_path / "money.db"
        cfg = InvoicingConfig(
            accounting_path="", invoice_output="", next_invoice_number=1,
            company=CompanyConfig(name="Main", key="main"),
            clients={}, services={
                "consulting": ServiceConfig(
                    key="consulting", display_name="Consulting", rate=150.0, type="hourly",
                ),
            },
        )
        with caplog.at_level("WARNING"):
            cs.save_invoicing(db_path, cfg)
        assert cs.load_invoicing(db_path).services["consulting"].type == "hours"
        assert "money_config_sanitized" in caplog.text

    def test_out_of_set_client_schedule_is_coerced(self, tmp_path):
        db_path = tmp_path / "money.db"
        cfg = InvoicingConfig(
            accounting_path="", invoice_output="", next_invoice_number=1,
            company=CompanyConfig(name="Main", key="main"),
            clients={
                "acme": ClientConfig(key="acme", name="Acme", schedule="weekly"),
            },
            services={},
        )
        cs.save_invoicing(db_path, cfg)
        assert cs.load_invoicing(db_path).clients["acme"].schedule == "on-demand"

    def test_a_conforming_config_is_untouched(self, tmp_path):
        db_path = tmp_path / "money.db"
        cfg = InvoicingConfig(
            accounting_path="", invoice_output="", next_invoice_number=1,
            company=CompanyConfig(name="Main", key="main"),
            clients={"acme": ClientConfig(key="acme", name="Acme", schedule="monthly")},
            services={
                "design": ServiceConfig(key="design", display_name="Design", rate=90.0,
                                        type="flat"),
            },
        )
        cs.save_invoicing(db_path, cfg)
        loaded = cs.load_invoicing(db_path)
        assert loaded.clients["acme"].schedule == "monthly"
        assert loaded.services["design"].type == "flat"


class TestInvoicingScalarShapes:
    def test_default_accounts_are_shape_checked(self):
        with pytest.raises(ValueError, match="default_ar_account"):
            cs.check_invoicing_scalars({"default_ar_account": "assets ar"})
        cs.check_invoicing_scalars({"default_ar_account": "Assets:Accounts-Receivable"})

    def test_currency_is_shape_checked(self):
        with pytest.raises(ValueError, match="currency"):
            cs.check_invoicing_scalars({"currency": "us dollars"})
        cs.check_invoicing_scalars({"currency": "EUR"})

    def test_blank_values_are_a_noop(self):
        cs.check_invoicing_scalars({"default_ar_account": "", "currency": ""})
