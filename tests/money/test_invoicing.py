"""Tests for money.core.invoicing module."""

import sqlite3
import sys
import types
from datetime import date
from pathlib import Path

import pytest


def _skip_unless_weasyprint():
    """Skip if weasyprint is not importable (missing module or native libs)."""
    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError) as exc:
        pytest.skip(f"weasyprint not usable: {exc}")

from istota.money.core.invoicing import (
    build_line_items,
    compute_income_lines,
    create_income_posting,
    find_invoice_pdf,
    format_invoice_number,
    generate_invoice,
    generate_invoice_html,
    group_entries_by_bundle,
    parse_invoicing_config,
    resolve_bank_account,
    resolve_currency,
    resolve_entity,
    update_invoice_number,
)
from istota.money.core.models import (
    ClientConfig,
    CompanyConfig,
    Invoice,
    InvoiceLineItem,
    InvoicingConfig,
    ServiceConfig,
    WorkEntry,
)


class TestParseInvoicingConfig:
    def test_single_entity(self, tmp_path):
        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            'accounting_path = "accounting"\n'
            'next_invoice_number = 42\n\n'
            '[company]\nname = "My Company"\naddress = "123 Main"\n\n'
            '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
            '[services.consulting]\ndisplay_name = "Consulting"\nrate = 150\n'
        )
        config = parse_invoicing_config(config_file)
        assert config.next_invoice_number == 42
        assert config.company.name == "My Company"
        assert "acme" in config.clients
        assert config.clients["acme"].name == "Acme Corp"
        assert "consulting" in config.services
        assert config.services["consulting"].rate == 150

    def test_multi_entity(self, tmp_path):
        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            'accounting_path = "."\nnext_invoice_number = 1\n'
            'default_entity = "personal"\n\n'
            '[companies.personal]\nname = "Personal"\n\n'
            '[companies.llc]\nname = "My LLC"\nbank_account = "Assets:LLC:Bank"\n\n'
            '[clients.acme]\nname = "Acme"\n\n'
            '[services.dev]\ndisplay_name = "Dev"\nrate = 200\n'
        )
        config = parse_invoicing_config(config_file)
        assert len(config.companies) == 2
        assert config.default_entity == "personal"
        assert config.companies["llc"].bank_account == "Assets:LLC:Bank"


class TestClientLedgerPosting:
    def test_default_true(self, tmp_path):
        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            'accounting_path = "."\nnext_invoice_number = 1\n\n'
            '[company]\nname = "Co"\n\n'
            '[clients.acme]\nname = "Acme"\n\n'
            '[services.dev]\ndisplay_name = "Dev"\nrate = 100\n'
        )
        config = parse_invoicing_config(config_file)
        assert config.clients["acme"].ledger_posting is True

    def test_explicit_false(self, tmp_path):
        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            'accounting_path = "."\nnext_invoice_number = 1\n\n'
            '[company]\nname = "Co"\n\n'
            '[clients.acme]\nname = "Acme"\n\n'
            '[clients.acme.invoicing]\nledger_posting = false\n\n'
            '[services.dev]\ndisplay_name = "Dev"\nrate = 100\n'
        )
        config = parse_invoicing_config(config_file)
        assert config.clients["acme"].ledger_posting is False


class TestBuildLineItems:
    def test_hours(self):
        entries = [WorkEntry(date=date(2026, 1, 1), client="a", service="dev", qty=8)]
        services = {"dev": ServiceConfig(key="dev", display_name="Development", rate=150)}
        items = build_line_items(entries, services)
        assert len(items) == 1
        assert items[0].quantity == 8
        assert items[0].rate == 150
        assert items[0].amount == 1200

    def test_flat_rate(self):
        entries = [WorkEntry(date=date(2026, 1, 1), client="a", service="hosting")]
        services = {"hosting": ServiceConfig(key="hosting", display_name="Hosting", rate=50, type="flat")}
        items = build_line_items(entries, services)
        assert items[0].quantity == 1
        assert items[0].amount == 50

    def test_discount(self):
        entries = [WorkEntry(date=date(2026, 1, 1), client="a", service="dev", qty=10, discount=100)]
        services = {"dev": ServiceConfig(key="dev", display_name="Dev", rate=100)}
        items = build_line_items(entries, services)
        assert items[0].amount == 900

    def test_unknown_service_skipped(self):
        entries = [WorkEntry(date=date(2026, 1, 1), client="a", service="unknown")]
        services = {"dev": ServiceConfig(key="dev", display_name="Dev", rate=100)}
        items = build_line_items(entries, services)
        assert len(items) == 0

    def test_hours_amount_fallback_when_no_qty(self):
        """Hours-type entry with amount but no qty uses amount as fallback."""
        entries = [WorkEntry(date=date(2026, 1, 1), client="a", service="dev", amount=1200)]
        services = {"dev": ServiceConfig(key="dev", display_name="Development", rate=150)}
        items = build_line_items(entries, services)
        assert len(items) == 1
        assert items[0].amount == 1200
        assert items[0].quantity == 1
        assert items[0].rate == 1200

    def test_hours_qty_takes_precedence_over_amount(self):
        """When both qty and amount are set, qty*rate is used for hours type."""
        entries = [WorkEntry(date=date(2026, 1, 1), client="a", service="dev", qty=8, amount=999)]
        services = {"dev": ServiceConfig(key="dev", display_name="Development", rate=150)}
        items = build_line_items(entries, services)
        assert len(items) == 1
        assert items[0].amount == 1200  # 8 * 150, not 999

    def test_days_amount_fallback_when_no_qty(self):
        """Days-type entry with amount but no qty uses amount as fallback."""
        entries = [WorkEntry(date=date(2026, 1, 1), client="a", service="retainer", amount=3000)]
        services = {"retainer": ServiceConfig(key="retainer", display_name="Monthly Retainer", rate=500, type="days")}
        items = build_line_items(entries, services)
        assert len(items) == 1
        assert items[0].amount == 3000
        assert items[0].quantity == 1


class TestGroupEntriesByBundle:
    def test_unbundled(self):
        entries = [
            WorkEntry(date=date(2026, 1, 1), client="a", service="dev"),
            WorkEntry(date=date(2026, 1, 2), client="a", service="consulting"),
        ]
        client = ClientConfig(key="a", name="A")
        groups = group_entries_by_bundle(entries, client)
        assert len(groups) == 1
        assert groups[0][0] == "Services"

    def test_with_separate(self):
        entries = [
            WorkEntry(date=date(2026, 1, 1), client="a", service="dev"),
            WorkEntry(date=date(2026, 1, 2), client="a", service="expenses"),
        ]
        client = ClientConfig(key="a", name="A", separate=["expenses"])
        groups = group_entries_by_bundle(entries, client)
        assert len(groups) == 2

    def test_a_flagged_entry_gets_a_group_of_its_own(self):
        flagged = WorkEntry(date=date(2026, 1, 1), client="a", service="dev", uid="u1", separate=True)
        other = WorkEntry(date=date(2026, 1, 2), client="a", service="dev", uid="u2")
        groups = group_entries_by_bundle([flagged, other], ClientConfig(key="a", name="A"))
        assert [(name, [e.uid for e in members]) for name, members in groups] == [
            ("dev", ["u1"]), ("Services", ["u2"]),
        ]

    def test_two_flagged_same_service_entries_stay_apart(self):
        entries = [
            WorkEntry(date=date(2026, 1, 1), client="a", service="dev", uid="u1", separate=True),
            WorkEntry(date=date(2026, 1, 2), client="a", service="dev", uid="u2", separate=True),
        ]
        groups = group_entries_by_bundle(entries, ClientConfig(key="a", name="A"))
        assert [[e.uid for e in members] for _, members in groups] == [["u1"], ["u2"]]

    def test_the_flag_wins_over_a_bundle(self):
        entries = [
            WorkEntry(date=date(2026, 1, 1), client="a", service="dev", uid="u1", separate=True),
            WorkEntry(date=date(2026, 1, 2), client="a", service="dev", uid="u2"),
        ]
        client = ClientConfig(
            key="a", name="A", bundles=[{"name": "Build", "services": ["dev"]}],
        )
        groups = group_entries_by_bundle(entries, client)
        assert [(name, [e.uid for e in members]) for name, members in groups] == [
            ("dev", ["u1"]), ("Build", ["u2"]),
        ]


class TestFindInvoicePdf:
    def _make_pdf(self, output_dir: Path, year: str, filename: str) -> Path:
        year_dir = output_dir / year
        year_dir.mkdir(parents=True, exist_ok=True)
        pdf = year_dir / filename
        pdf.write_bytes(b"%PDF-1.4 fake")
        return pdf

    def test_finds_pdf_by_zero_padded_number(self, tmp_path):
        out = tmp_path / "generated"
        expected = self._make_pdf(out, "2026", "Invoice-000007-04_15_2026.pdf")
        found = find_invoice_pdf(out, "INV-000007")
        assert found == expected

    def test_finds_pdf_with_short_number(self, tmp_path):
        out = tmp_path / "generated"
        expected = self._make_pdf(out, "2026", "Invoice-000007-04_15_2026.pdf")
        # The stored invoice number may not be zero-padded the same way.
        found = find_invoice_pdf(out, "INV-7")
        assert found == expected

    def test_returns_none_when_missing(self, tmp_path):
        out = tmp_path / "generated"
        out.mkdir()
        assert find_invoice_pdf(out, "INV-000099") is None

    def test_returns_none_when_dir_absent(self, tmp_path):
        assert find_invoice_pdf(tmp_path / "nope", "INV-000007") is None

    def test_returns_none_for_unparseable_number(self, tmp_path):
        out = tmp_path / "generated"
        self._make_pdf(out, "2026", "Invoice-000007-04_15_2026.pdf")
        assert find_invoice_pdf(out, "garbage") is None


class TestFormatInvoiceNumber:
    def test_format(self):
        assert format_invoice_number(1) == "INV-000001"
        assert format_invoice_number(42) == "INV-000042"
        assert format_invoice_number(123456) == "INV-123456"


class TestGenerateInvoice:
    def test_basic(self):
        entries = [WorkEntry(date=date(2026, 2, 1), client="acme", service="dev", qty=8)]
        client = ClientConfig(key="acme", name="Acme Corp", terms=30)
        company = CompanyConfig(name="My Co")
        services = {"dev": ServiceConfig(key="dev", display_name="Development", rate=150)}

        invoice = generate_invoice(entries, "Services", client, company, services, 1, date(2026, 3, 1))
        assert invoice.number == "INV-000001"
        assert invoice.total == 1200
        assert invoice.due_date == date(2026, 3, 31)


class TestGenerateInvoiceHtml:
    def test_basic_html(self):
        client = ClientConfig(key="acme", name="Acme Corp", terms=30)
        company = CompanyConfig(name="My Co", address="123 Main")
        items = [InvoiceLineItem(display_name="Dev", description="Work", quantity=8, rate=150, discount=0, amount=1200)]
        inv = Invoice(
            number="INV-000001", date=date(2026, 3, 1),
            due_date=date(2026, 3, 31), client=client, company=company,
            items=items, total=1200,
        )
        html = generate_invoice_html(inv)
        assert "INVOICE" in html
        assert "INV-000001" in html
        assert "Acme Corp" in html
        assert "$1,200.00" in html


class TestIncomePosting:
    def test_create_income_posting(self):
        result = create_income_posting(
            invoice_number="INV-000042",
            client_name="Acme Corp",
            income_lines={"Income:Consulting": 1500, "Income:Dev": 875},
            payment_date=date(2026, 2, 15),
        )
        assert '2026-02-15 * "Acme Corp" "Payment for INV-000042"' in result
        assert "Assets:Bank:Checking  2375.00 USD" in result
        assert "Income:Consulting  -1500.00 USD" in result
        assert "Income:Dev  -875.00 USD" in result

    def test_compute_income_lines(self):
        entries = [
            WorkEntry(date=date(2026, 1, 1), client="acme", service="dev", qty=8),
            WorkEntry(date=date(2026, 1, 2), client="acme", service="dev", qty=4),
        ]
        services = {"dev": ServiceConfig(key="dev", display_name="Development", rate=150, income_account="Income:Dev")}
        result = compute_income_lines(entries, services)
        assert result["Income:Dev"] == 1800


class TestUpdateInvoiceNumber:
    def test_update(self, tmp_path):
        config = tmp_path / "invoicing.toml"
        config.write_text('next_invoice_number = 42\n')
        update_invoice_number(config, 43)
        assert "next_invoice_number = 43" in config.read_text()


class TestResolveEntity:
    def _config(self):
        personal = CompanyConfig(name="Personal", key="personal")
        llc = CompanyConfig(name="LLC", key="llc", bank_account="Assets:LLC:Bank")
        return InvoicingConfig(
            accounting_path="", invoice_output="", next_invoice_number=1,
            company=personal, clients={}, services={},
            companies={"personal": personal, "llc": llc},
            default_entity="personal",
        )

    def test_default(self):
        config = self._config()
        entity = resolve_entity(config)
        assert entity.name == "Personal"

    def test_client_override(self):
        config = self._config()
        client = ClientConfig(key="acme", name="Acme", entity="llc")
        entity = resolve_entity(config, client_config=client)
        assert entity.name == "LLC"

    def test_entry_override(self):
        config = self._config()
        entry = WorkEntry(date=date(2026, 1, 1), client="acme", service="dev", entity="llc")
        entity = resolve_entity(config, entry=entry)
        assert entity.name == "LLC"

    def test_resolve_bank_account(self):
        config = self._config()
        entity = config.companies["llc"]
        assert resolve_bank_account(entity, config) == "Assets:LLC:Bank"

    def test_resolve_bank_account_default(self):
        config = self._config()
        entity = config.companies["personal"]
        assert resolve_bank_account(entity, config) == "Assets:Bank:Checking"

    def test_resolve_currency(self):
        config = self._config()
        entity = CompanyConfig(name="EUR Co", currency="EUR")
        assert resolve_currency(entity, config) == "EUR"

    def test_resolve_currency_default(self):
        config = self._config()
        entity = config.companies["personal"]
        assert resolve_currency(entity, config) == "USD"


class TestClientCaseInsensitiveMatching:
    """Invoice generation should match work entries to config clients case-insensitively."""

    def test_generate_invoices_matches_uppercase_entries(self, tmp_path):
        """Entries with uppercase client key should match lowercase config key."""
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            f'accounting_path = "{tmp_path}"\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "My Co"\n\n'
            '[clients.acme]\nname = "Acme Inc"\nterms = 30\n\n'
            '[services.dev]\ndisplay_name = "Development"\nrate = 150\ntype = "hours"\n'
            'income_account = "Income:Dev"\n'
        )
        config = parse_invoicing_config(config_file)

        # add_work_entry now normalizes to lowercase, so this tests the full flow
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=8)

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            dry_run=True,
        )
        assert len(results) == 1
        assert results[0]["client"] == "Acme Inc"
        assert results[0]["total"] == 1200.0


class TestDryRunDoesNotMutate:
    """Dry-run must not stamp entries, create PDFs, or update config."""

    def _make_config(self, tmp_path):
        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            f'accounting_path = "{tmp_path}"\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "My Co"\n\n'
            '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
            '[services.dev]\ndisplay_name = "Development"\nrate = 150\ntype = "hours"\n'
            'income_account = "Income:Dev"\n'
        )
        return config_file

    def test_dry_run_does_not_stamp_entries(self, tmp_path):
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry, get_uninvoiced_entries

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=8)

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            dry_run=True,
        )
        assert len(results) == 1

        # Entries must still be uninvoiced
        uninvoiced = get_uninvoiced_entries(tmp_path)
        assert len(uninvoiced) == 1
        assert uninvoiced[0].invoice == ""

    def test_dry_run_does_not_update_invoice_number(self, tmp_path):
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=8)

        generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            dry_run=True,
        )

        # Config file must still have original invoice number
        reloaded = parse_invoicing_config(config_file)
        assert reloaded.next_invoice_number == 1

    def test_dry_run_does_not_create_pdfs(self, tmp_path):
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=8)

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            dry_run=True,
        )
        assert "file" not in results[0]
        invoice_dir = tmp_path / "invoices"
        assert not invoice_dir.exists() or not list(invoice_dir.rglob("*.pdf"))

    def test_real_run_after_dry_run_still_finds_entries(self, tmp_path):
        """Running generate after dry-run must still find the entries."""
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)
        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=8)

        # Dry run first
        dry_results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            dry_run=True,
        )
        assert len(dry_results) == 1

        # Real run must still find entries
        # Re-parse config since it may have been updated (it shouldn't be)
        config = parse_invoicing_config(config_file)

        _skip_unless_weasyprint()

        real_results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            dry_run=False,
        )
        assert len(real_results) == 1
        assert "file" in real_results[0]


class TestSkipUnmatchedService:
    """Entries with services not in config must not be stamped or invoiced."""

    def _make_config(self, tmp_path):
        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            f'accounting_path = "{tmp_path}"\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "My Co"\n\n'
            '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
            '[services.dev]\ndisplay_name = "Development"\nrate = 150\ntype = "hours"\n'
            'income_account = "Income:Dev"\n'
        )
        return config_file

    def test_unknown_service_not_stamped(self, tmp_path):
        """Entry with unknown service must not get an invoice number."""
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry, load_work_entries

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)

        # "consulting" is not a configured service — only "dev" exists
        add_work_entry(tmp_path, "2026-03-01", "acme", "consulting", qty=8)

        _skip_unless_weasyprint()

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
        )
        # No billable items → no invoices
        assert results == []

        # Entry must remain uninvoiced
        entries = load_work_entries(tmp_path)
        assert len(entries) == 1
        assert entries[0].invoice == ""

    def test_unknown_service_dry_run_returns_empty(self, tmp_path):
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)
        add_work_entry(tmp_path, "2026-03-01", "acme", "consulting", qty=8)

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            dry_run=True,
        )
        assert results == []

    def test_mixed_known_and_unknown_services(self, tmp_path):
        """Only entries with known services get invoiced."""
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry, load_work_entries

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)

        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(tmp_path, "2026-03-02", "acme", "consulting", qty=4)

        _skip_unless_weasyprint()

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
        )
        assert len(results) == 1
        assert results[0]["items"] == 1
        assert results[0]["total"] == 1200.0

        entries = load_work_entries(tmp_path)
        # "dev" entry stamped, "consulting" entry NOT stamped
        dev_entry = [e for e in entries if e.service == "dev"][0]
        consulting_entry = [e for e in entries if e.service == "consulting"][0]
        assert dev_entry.invoice == "INV-000001"
        assert consulting_entry.invoice == ""

    def test_invoice_number_not_bumped_for_empty(self, tmp_path):
        """Invoice number counter must not advance for skipped groups."""
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)
        add_work_entry(tmp_path, "2026-03-01", "acme", "consulting", qty=8)

        _skip_unless_weasyprint()

        generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
        )
        reloaded = parse_invoicing_config(config_file)
        assert reloaded.next_invoice_number == 1  # unchanged


class TestDryRunWithAmounts:
    """Dry-run should correctly report items and totals for entries with amounts."""

    def _make_config(self, tmp_path):
        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            f'accounting_path = "{tmp_path}"\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "My Co"\n\n'
            '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
            '[services.dev]\ndisplay_name = "Development"\nrate = 150\ntype = "hours"\n'
            'income_account = "Income:Dev"\n\n'
            '[services.expenses]\ndisplay_name = "Expenses"\nrate = 0\ntype = "other"\n'
            'income_account = "Income:Reimbursement"\n'
        )
        return config_file

    def test_dry_run_hours_with_qty(self, tmp_path):
        """Standard hours entry with qty shows correct items and total."""
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)

        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=8)

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            dry_run=True,
        )
        assert len(results) == 1
        assert results[0]["items"] == 1
        assert results[0]["total"] == 1200.0

    def test_dry_run_other_type_with_amount(self, tmp_path):
        """'other' type entry with amount shows correct items and total."""
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)

        add_work_entry(tmp_path, "2026-03-01", "acme", "expenses", amount=500)

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            dry_run=True,
        )
        assert len(results) == 1
        assert results[0]["items"] == 1
        assert results[0]["total"] == 500.0

    def test_dry_run_hours_with_amount_no_qty(self, tmp_path):
        """Hours-type entry with amount but no qty should use amount as fallback."""
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)

        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", amount=1200)

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            dry_run=True,
        )
        assert len(results) == 1
        assert results[0]["items"] == 1
        assert results[0]["total"] == 1200.0

    def test_dry_run_mixed_entries(self, tmp_path):
        """Mix of hours (with qty) and amount-only entries."""
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)

        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(tmp_path, "2026-03-15", "acme", "dev", amount=500)

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            dry_run=True,
        )
        assert len(results) == 1
        assert results[0]["items"] == 2
        assert results[0]["total"] == 1700.0  # 8*150 + 500


class TestResolve:
    def test_relative_path(self, tmp_path):
        from istota.money.cli import _resolve
        result = _resolve(tmp_path, "config/invoicing.toml")
        assert result == tmp_path / "config/invoicing.toml"

    def test_absolute_path_unchanged(self, tmp_path):
        from istota.money.cli import _resolve
        result = _resolve(tmp_path, "/etc/moneyman/config.toml")
        assert result == Path("/etc/moneyman/config.toml")


class TestLoadInvoicingConfigReturnsResolvedPaths:
    """_load_invoicing_config must return absolute paths so PDFs land in data_dir."""

    def _make_config(self, tmp_path, accounting_path=".", invoice_output="invoices/generated"):
        from istota.money import config_store
        from istota.money.cli import Context
        from istota.money.core.models import CompanyConfig, InvoicingConfig
        db_path = tmp_path / "money.db"
        config_store.init_db(db_path)
        config_store.save_invoicing(db_path, InvoicingConfig(
            accounting_path=accounting_path, invoice_output=invoice_output,
            next_invoice_number=1,
            company=CompanyConfig(name="Co", key="co"),
            clients={}, services={},
            companies={"co": CompanyConfig(name="Co", key="co")},
            default_entity="co",
        ))
        ctx = Context()
        ctx.data_dir = tmp_path / "data"
        ctx.data_dir.mkdir()
        ctx.db_path = db_path
        return ctx

    def test_invoice_output_dir_resolved_relative_to_data_dir(self, tmp_path):
        """invoice_output should resolve relative to data_dir, not accounting_path."""
        from istota.money.cli import _load_invoicing_config
        ctx = self._make_config(tmp_path, accounting_path="accounting", invoice_output="invoices/generated")
        _, accounting_path, invoice_output_dir = _load_invoicing_config(ctx)
        # accounting_path is data_dir / "accounting"
        assert accounting_path == ctx.data_dir / "accounting"
        # invoice_output_dir is data_dir / "invoices/generated" (NOT accounting/invoices/generated)
        assert invoice_output_dir == ctx.data_dir / "invoices/generated"

    def test_invoice_output_dir_is_absolute(self, tmp_path):
        """Returned invoice_output_dir must be absolute."""
        from istota.money.cli import _load_invoicing_config
        ctx = self._make_config(tmp_path)
        _, _, invoice_output_dir = _load_invoicing_config(ctx)
        assert invoice_output_dir.is_absolute()

    def test_accounting_path_dot_resolves_to_data_dir(self, tmp_path):
        """accounting_path='.' should resolve to data_dir itself."""
        from istota.money.cli import _load_invoicing_config
        ctx = self._make_config(tmp_path, accounting_path=".")
        _, accounting_path, _ = _load_invoicing_config(ctx)
        assert accounting_path == ctx.data_dir / "."

    def test_fallback_without_data_dir_uses_resolve(self, tmp_path):
        """When data_dir is None, paths should still be absolute via resolve()."""
        from istota.money.cli import _load_invoicing_config, Context
        from istota.money import config_store
        from istota.money.core.models import CompanyConfig, InvoicingConfig
        db_path = tmp_path / "money.db"
        config_store.init_db(db_path)
        config_store.save_invoicing(db_path, InvoicingConfig(
            accounting_path=".", invoice_output="invoices/out",
            next_invoice_number=1,
            company=CompanyConfig(name="Co", key="co"),
            clients={}, services={},
            companies={"co": CompanyConfig(name="Co", key="co")},
            default_entity="co",
        ))
        ctx = Context()
        ctx.data_dir = None
        ctx.db_path = db_path
        _, accounting_path, invoice_output_dir = _load_invoicing_config(ctx)
        assert accounting_path.is_absolute()
        assert invoice_output_dir.is_absolute()


class TestGenerateInvoicesPdfOutputDir:
    """generate_invoices_for_period should use invoice_output_dir when provided."""

    def test_pdf_written_to_invoice_output_dir(self, tmp_path):
        """PDF should be written under invoice_output_dir, not under accounting_path."""
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        accounting_dir = tmp_path / "accounting"
        accounting_dir.mkdir()
        output_dir = tmp_path / "data" / "invoices" / "out"

        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            'accounting_path = "accounting"\n'
            'invoice_output = "invoices/out"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "My Co"\n\n'
            '[clients.acme]\nname = "Acme"\nterms = 30\n\n'
            '[services.dev]\ndisplay_name = "Dev"\nrate = 100\ntype = "hours"\n'
            'income_account = "Income:Dev"\n'
        )
        config = parse_invoicing_config(config_file)

        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=4)

        _skip_unless_weasyprint()

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=accounting_dir, data_dir=data_dir,
            invoice_output_dir=output_dir,
        )
        assert len(results) == 1
        pdf_path = Path(results[0]["file"])
        # PDF must be under the provided output_dir, not under accounting_dir
        assert str(pdf_path).startswith(str(output_dir))
        assert pdf_path.exists()

    def test_fallback_to_accounting_path_when_no_output_dir(self, tmp_path):
        """Without invoice_output_dir, falls back to accounting_path / invoice_output."""
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            f'accounting_path = "{tmp_path}"\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "My Co"\n\n'
            '[clients.acme]\nname = "Acme"\nterms = 30\n\n'
            '[services.dev]\ndisplay_name = "Dev"\nrate = 100\ntype = "hours"\n'
            'income_account = "Income:Dev"\n'
        )
        config = parse_invoicing_config(config_file)

        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=4)

        _skip_unless_weasyprint()

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            # invoice_output_dir not passed — fallback
        )
        assert len(results) == 1
        pdf_path = Path(results[0]["file"])
        assert str(pdf_path).startswith(str(tmp_path / "invoices"))


class TestCheckScheduledInvoices:
    """Tests for the scheduled invoice generation logic."""

    def _make_config(self, tmp_path, schedule="monthly", day=15):
        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            f'accounting_path = "{tmp_path}"\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "My Co"\n\n'
            f'[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
            f'[clients.acme.invoicing]\nschedule = "{schedule}"\nday = {day}\n\n'
            '[services.dev]\ndisplay_name = "Development"\nrate = 150\ntype = "hours"\n'
            'income_account = "Income:Dev"\n'
        )
        return config_file

    def _make_db(self, tmp_path):
        import sqlite3
        from istota.money.db import init_db
        db_path = tmp_path / "money.db"
        init_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def test_returns_due_clients_on_schedule_day(self, tmp_path):
        from istota.money.core.invoicing import check_scheduled_invoices

        config_file = self._make_config(tmp_path, day=15)
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        result = check_scheduled_invoices(config, conn, today=date(2026, 3, 15))
        assert result == ["acme"]

    def test_returns_empty_when_not_schedule_day(self, tmp_path):
        from istota.money.core.invoicing import check_scheduled_invoices

        config_file = self._make_config(tmp_path, day=15)
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        result = check_scheduled_invoices(config, conn, today=date(2026, 3, 14))
        assert result == []

    def test_skips_on_demand_clients(self, tmp_path):
        from istota.money.core.invoicing import check_scheduled_invoices

        config_file = self._make_config(tmp_path, schedule="on-demand", day=15)
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        result = check_scheduled_invoices(config, conn, today=date(2026, 3, 15))
        assert result == []

    def test_skips_already_generated_this_month(self, tmp_path):
        from istota.money.core.invoicing import check_scheduled_invoices

        config_file = self._make_config(tmp_path, day=15)
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        # Simulate generation earlier this month
        conn.execute(
            "INSERT INTO invoice_schedule_state (client_key, last_generation_at) VALUES (?, ?)",
            ("acme", "2026-03-15T06:00:00"),
        )
        conn.commit()

        result = check_scheduled_invoices(config, conn, today=date(2026, 3, 15))
        assert result == []

    def test_generates_again_next_month(self, tmp_path):
        from istota.money.core.invoicing import check_scheduled_invoices

        config_file = self._make_config(tmp_path, day=15)
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        # Last generated in February
        conn.execute(
            "INSERT INTO invoice_schedule_state (client_key, last_generation_at) VALUES (?, ?)",
            ("acme", "2026-02-15T06:00:00"),
        )
        conn.commit()

        result = check_scheduled_invoices(config, conn, today=date(2026, 3, 15))
        assert result == ["acme"]

    def test_catches_up_if_past_schedule_day(self, tmp_path):
        """If the cron missed the exact day, still generate later in the month."""
        from istota.money.core.invoicing import check_scheduled_invoices

        config_file = self._make_config(tmp_path, day=15)
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        # It's the 20th, no generation happened this month
        result = check_scheduled_invoices(config, conn, today=date(2026, 3, 20))
        assert result == ["acme"]

    def test_does_not_fire_before_schedule_day(self, tmp_path):
        """Before the schedule day, don't generate even if no generation this month."""
        from istota.money.core.invoicing import check_scheduled_invoices

        config_file = self._make_config(tmp_path, day=15)
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        result = check_scheduled_invoices(config, conn, today=date(2026, 3, 10))
        assert result == []

    def test_schedule_day_end_of_month_clamped(self, tmp_path):
        """Schedule day 31 should fire on the last day of shorter months."""
        from istota.money.core.invoicing import check_scheduled_invoices

        config_file = self._make_config(tmp_path, day=31)
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        # February 28 (non-leap) — last day, should fire
        result = check_scheduled_invoices(config, conn, today=date(2026, 2, 28))
        assert result == ["acme"]

    def test_multiple_clients_mixed(self, tmp_path):
        """Multiple clients: only due ones returned."""
        from istota.money.core.invoicing import check_scheduled_invoices

        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            f'accounting_path = "{tmp_path}"\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "My Co"\n\n'
            '[clients.acme]\nname = "Acme"\nterms = 30\n\n'
            '[clients.acme.invoicing]\nschedule = "monthly"\nday = 15\n\n'
            '[clients.beta]\nname = "Beta"\nterms = 30\n\n'
            '[clients.beta.invoicing]\nschedule = "monthly"\nday = 1\n\n'
            '[clients.gamma]\nname = "Gamma"\nterms = 30\n\n'
            '[services.dev]\ndisplay_name = "Dev"\nrate = 150\n'
        )
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        result = check_scheduled_invoices(config, conn, today=date(2026, 3, 15))
        assert "acme" in result
        assert "beta" in result  # past day 1, catch-up
        assert "gamma" not in result  # on-demand


class TestNextInvoiceNumberDerivation:
    """The starting invoice number must never collide with an already-issued one.

    Regression for the DB-migration counter drift: the counter is loaded from
    the DB but historically persisted only to a (now-unread) TOML file, so it
    froze while real invoices marched ahead. Deriving the start defensively
    from the highest existing invoice self-heals that drift and also prevents
    collisions when several clients are generated in one run.
    """

    def test_highest_existing_invoice_number_empty(self, tmp_path):
        from istota.money.core.invoicing import highest_existing_invoice_number

        assert highest_existing_invoice_number(tmp_path) == 0

    def test_highest_existing_invoice_number_reads_work_entries(self, tmp_path):
        from istota.money.core.invoicing import highest_existing_invoice_number
        from istota.money.work import add_work_entry

        add_work_entry(tmp_path, "2026-05-01", "acme", "dev", qty=1, invoice="INV-000236")
        add_work_entry(tmp_path, "2026-05-01", "acme", "dev", qty=1, invoice="INV-000238")
        add_work_entry(tmp_path, "2026-05-01", "acme", "dev", qty=1)  # uninvoiced

        assert highest_existing_invoice_number(tmp_path) == 238

    def test_generate_starts_above_highest_existing_when_counter_lags(self, tmp_path):
        """Counter says 236 but invoices 236-238 already exist: start at 239."""
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            f'accounting_path = "{tmp_path}"\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 236\n\n'
            '[company]\nname = "My Co"\n\n'
            '[clients.acme]\nname = "Acme Inc"\nterms = 30\n\n'
            '[services.dev]\ndisplay_name = "Dev"\nrate = 150\ntype = "hours"\n'
            'income_account = "Income:Dev"\n'
        )
        config = parse_invoicing_config(config_file)

        # Already-issued invoices, counter never advanced past 236
        add_work_entry(tmp_path, "2026-05-01", "acme", "dev", qty=1, invoice="INV-000236")
        add_work_entry(tmp_path, "2026-05-01", "acme", "dev", qty=1, invoice="INV-000237")
        add_work_entry(tmp_path, "2026-05-01", "acme", "dev", qty=1, invoice="INV-000238")
        # New uninvoiced work
        add_work_entry(tmp_path, "2026-06-15", "acme", "dev", qty=8)

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            dry_run=True,
        )
        assert len(results) == 1
        assert results[0]["invoice_number"] == "INV-000239"

    def test_generate_uses_counter_when_ahead_of_existing(self, tmp_path):
        """When the counter leads the ledger, honor the counter."""
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            f'accounting_path = "{tmp_path}"\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 500\n\n'
            '[company]\nname = "My Co"\n\n'
            '[clients.acme]\nname = "Acme Inc"\nterms = 30\n\n'
            '[services.dev]\ndisplay_name = "Dev"\nrate = 150\ntype = "hours"\n'
            'income_account = "Income:Dev"\n'
        )
        config = parse_invoicing_config(config_file)
        add_work_entry(tmp_path, "2026-05-01", "acme", "dev", qty=1, invoice="INV-000238")
        add_work_entry(tmp_path, "2026-06-15", "acme", "dev", qty=8)

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            dry_run=True,
        )
        assert results[0]["invoice_number"] == "INV-000500"


class TestCounterPersistenceBackend:
    """The advanced counter must land in whichever backend the config came from."""

    def _seed_db(self, tmp_path, next_number):
        from istota.money import config_store

        db_path = tmp_path / "money.db"
        config_store.init_db(db_path)
        # has_invoicing_data() gates DB routing on collection tables, so seed a
        # company (matching the in-istota state) plus the counter scalar.
        cfg = InvoicingConfig(
            accounting_path="", invoice_output="invoices/generated",
            next_invoice_number=next_number,
            company=CompanyConfig(name="My Co", key="myco"),
            clients={}, services={},
            companies={"myco": CompanyConfig(name="My Co", key="myco")},
            default_entity="myco",
        )
        config_store.save_invoicing(db_path, cfg)
        return db_path

    def test_persist_writes_db_when_db_backed(self, tmp_path):
        from istota.money import config_store
        from istota.money.core.invoicing import persist_next_invoice_number

        db_path = self._seed_db(tmp_path, 236)

        persist_next_invoice_number(240, db_path=db_path, config_path=None)

        assert config_store.load_invoicing(db_path).next_invoice_number == 240

    def test_persist_falls_back_to_toml_without_db(self, tmp_path):
        from istota.money.core.invoicing import persist_next_invoice_number

        config_file = tmp_path / "invoicing.toml"
        config_file.write_text('next_invoice_number = 1\n')

        persist_next_invoice_number(7, db_path=None, config_path=config_file)

        assert "next_invoice_number = 7" in config_file.read_text()

    def test_persist_prefers_db_over_toml_when_both_present(self, tmp_path):
        from istota.money import config_store
        from istota.money.core.invoicing import persist_next_invoice_number

        db_path = self._seed_db(tmp_path, 1)
        config_file = tmp_path / "invoicing.toml"
        config_file.write_text('next_invoice_number = 1\n')

        persist_next_invoice_number(9, db_path=db_path, config_path=config_file)

        assert config_store.load_invoicing(db_path).next_invoice_number == 9
        # Stale TOML left untouched — the DB is the source of truth
        assert "next_invoice_number = 1" in config_file.read_text()


class TestSetNextInvoiceNumber:
    def test_round_trip(self, tmp_path):
        from istota.money import config_store

        db_path = tmp_path / "money.db"
        config_store.init_db(db_path)
        config_store.set_next_invoice_number(db_path, 239)

        assert config_store.load_invoicing(db_path).next_invoice_number == 239


class TestInvoiceNumberStampingIsUidAddressed:
    """Invoice numbers must be stamped by uid, not by display index.

    ``generate_invoices_for_period`` reads uninvoiced entries outside any
    lock, renders PDFs (seconds), then stamps. Display indices shift whenever
    another writer — the web Work tab above all — inserts an earlier-dated
    entry in that window, so an index-addressed stamp lands on the wrong row:
    one client's entry gets another's invoice number, and the entry actually
    on the rendered PDF stays uninvoiced and gets billed again next run.
    """

    def _config(self, tmp_path):
        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            f'accounting_path = "{tmp_path}"\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "My Co"\n\n'
            '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
            '[services.dev]\ndisplay_name = "Development"\nrate = 150\ntype = "hours"\n'
            'income_account = "Income:Dev"\n'
        )
        return config_file

    def test_concurrent_insert_does_not_misstamp(self, tmp_path, monkeypatch):
        from istota.money.core import invoicing as invoicing_mod
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry, load_work_entries

        config_file = self._config(tmp_path)
        config = parse_invoicing_config(config_file)
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=8)

        def fake_pdf(html, output_path, **kwargs):
            # Simulates the web Work tab adding a backdated entry while the
            # PDF renders — every display index after it shifts by one.
            add_work_entry(tmp_path, "2026-03-01", "hooli", "dev", qty=1)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"%PDF-1.4")

        monkeypatch.setattr(invoicing_mod, "generate_invoice_html", lambda *a, **k: "<html></html>")
        monkeypatch.setattr(invoicing_mod, "generate_invoice_pdf", fake_pdf)

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
        )
        assert len(results) == 1

        by_client = {e.client: e for e in load_work_entries(tmp_path)}
        assert by_client["acme"].invoice == "INV-000001"
        assert by_client["hooli"].invoice == ""

    def test_entry_invoiced_mid_render_is_not_restamped(self, tmp_path, monkeypatch):
        from istota.money.core import invoicing as invoicing_mod
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import (
            add_work_entry,
            assign_invoice_number_by_uids,
            load_work_entries,
        )

        config_file = self._config(tmp_path)
        config = parse_invoicing_config(config_file)
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=8)
        uid = load_work_entries(tmp_path)[0].uid

        def fake_pdf(html, output_path, **kwargs):
            assign_invoice_number_by_uids(tmp_path, [uid], "INV-000999")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"%PDF-1.4")

        monkeypatch.setattr(invoicing_mod, "generate_invoice_html", lambda *a, **k: "<html></html>")
        monkeypatch.setattr(invoicing_mod, "generate_invoice_pdf", fake_pdf)

        # The active-line index refuses the second invoice, the transaction
        # rolls back, and the PDF rendered for it is removed.
        with pytest.raises(invoicing_mod.InvoiceRecordError, match="INV-000001"):
            generate_invoices_for_period(
                config=config, config_path=config_file,
                accounting_path=tmp_path, data_dir=tmp_path,
            )
        assert load_work_entries(tmp_path)[0].invoice == "INV-000999"
        assert list((tmp_path / "invoices").rglob("*.pdf")) == []


class TestTransactionalGeneration:
    """An invoice and its lines are one transaction, with its PDF before it."""

    def _setup(self, tmp_path, monkeypatch, *, schedule="on-demand"):
        from istota.money.core import invoicing as invoicing_mod

        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            f'accounting_path = "{tmp_path}"\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "My Co"\n\n'
            f'[clients.acme]\nname = "Acme Corp"\nterms = 30\n'
            f'[clients.acme.invoicing]\nschedule = "{schedule}"\n\n'
            '[services.dev]\ndisplay_name = "Development"\nrate = 150\ntype = "hours"\n'
        )

        def fake_pdf(html, output_path, **kwargs):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"%PDF-1.4")

        monkeypatch.setattr(invoicing_mod, "generate_invoice_html", lambda *a, **k: "<html></html>")
        monkeypatch.setattr(invoicing_mod, "generate_invoice_pdf", fake_pdf)
        return config_file, parse_invoicing_config(config_file)

    def _generate(self, tmp_path, config_file, config, **kwargs):
        from istota.money.core.invoicing import generate_invoices_for_period

        return generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path, **kwargs,
        )

    def test_records_the_invoice_row_with_its_total_and_pdf(self, tmp_path, monkeypatch):
        from istota.money.work import add_work_entry, list_invoices, load_work_entries

        config_file, config = self._setup(tmp_path, monkeypatch)
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=8)
        add_work_entry(tmp_path, "2026-03-11", "acme", "dev", qty=2)

        (result,) = self._generate(tmp_path, config_file, config)

        (record,) = list_invoices(tmp_path)
        assert record.number == "INV-000001" == result["invoice_number"]
        assert record.client == "acme"
        assert record.status == "open"
        assert record.total == 1500.0
        assert record.issue_date == date.today()
        assert record.pdf_path == result["file"]
        assert sorted(record.entry_uids) == sorted(e.uid for e in load_work_entries(tmp_path))
        assert {e.invoice for e in load_work_entries(tmp_path)} == {"INV-000001"}

    def test_a_commit_failure_removes_the_rendered_pdf(self, tmp_path, monkeypatch):
        from istota.money import work
        from istota.money.core import invoicing as invoicing_mod
        from istota.money.work import add_work_entry, list_invoices, load_work_entries

        config_file, config = self._setup(tmp_path, monkeypatch)
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=8)

        def failing_create(conn, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(work, "create_invoice", failing_create)

        with pytest.raises(invoicing_mod.InvoiceRecordError, match="disk I/O error"):
            self._generate(tmp_path, config_file, config)

        assert list((tmp_path / "invoices").rglob("*.pdf")) == []
        assert list_invoices(tmp_path) == []
        assert load_work_entries(tmp_path)[0].invoice == ""
        assert parse_invoicing_config(config_file).next_invoice_number == 1

    def test_schedule_state_is_written_with_the_invoice(self, tmp_path, monkeypatch):
        from istota.money import db
        from istota.money.work import add_work_entry, db_path_for

        config_file, config = self._setup(tmp_path, monkeypatch, schedule="monthly")
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=8)

        self._generate(tmp_path, config_file, config, record_schedule=True)

        with db.get_db(db_path_for(tmp_path)) as conn:
            state = db.get_invoice_schedule_state(conn, "acme")
        assert state is not None and state.last_generation_at

    def test_schedule_state_rolls_back_with_a_failed_invoice(self, tmp_path, monkeypatch):
        from istota.money import db
        from istota.money.core import invoicing as invoicing_mod
        from istota.money.work import add_work_entry, db_path_for

        config_file, config = self._setup(tmp_path, monkeypatch, schedule="monthly")
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=8)

        def failing_schedule(conn, client_key):
            db.set_invoice_schedule_generation(conn, client_key)
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(db, "set_invoice_schedule_generation", failing_schedule)
        with pytest.raises(invoicing_mod.InvoiceRecordError):
            self._generate(tmp_path, config_file, config, record_schedule=True)

        monkeypatch.undo()
        with db.get_db(db_path_for(tmp_path)) as conn:
            assert db.get_invoice_schedule_state(conn, "acme") is None
            assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0

    def test_the_next_number_skips_a_voided_number(self, tmp_path, monkeypatch):
        from istota.money.core.invoicing import highest_existing_invoice_number
        from istota.money.work import add_work_entry, void_invoice

        config_file, config = self._setup(tmp_path, monkeypatch)
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=8)
        self._generate(tmp_path, config_file, config)
        void_invoice(tmp_path, "INV-000001")
        assert highest_existing_invoice_number(tmp_path) == 1

        # The counter in the config is behind on purpose: the invoices table wins.
        config.next_invoice_number = 1
        (result,) = self._generate(tmp_path, config_file, config)
        assert result["invoice_number"] == "INV-000002"

    def test_the_index_rejects_a_second_active_line(self, tmp_path):
        from istota.money.work import (
            add_work_entry, create_invoice, load_work_entries, write_transaction,
        )

        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=8)
        uid = load_work_entries(tmp_path)[0].uid
        with write_transaction(tmp_path) as conn:
            create_invoice(
                conn, number="INV-000001", client="acme", entity="default", group_name="",
                issue_date=date(2026, 3, 31), total=1200, entry_uids=[uid], pdf_path="",
            )
        with pytest.raises(sqlite3.IntegrityError):
            with write_transaction(tmp_path) as conn:
                create_invoice(
                    conn, number="INV-000002", client="acme", entity="default", group_name="",
                    issue_date=date(2026, 3, 31), total=1200, entry_uids=[uid], pdf_path="",
                )
        assert load_work_entries(tmp_path)[0].invoice == "INV-000001"


class TestSeparateAndNamedEntries:
    """#754: a flagged entry is invoiced alone, and ``entry_uids`` bills exactly those."""

    def _setup(self, tmp_path, monkeypatch):
        return TestTransactionalGeneration()._setup(tmp_path, monkeypatch)

    def _generate(self, tmp_path, config_file, config, **kwargs):
        from istota.money.core.invoicing import generate_invoices_for_period

        return generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path, **kwargs,
        )

    def _uids(self, tmp_path):
        from istota.money.work import load_work_entries

        return {e.description: e.uid for e in load_work_entries(tmp_path)}

    def test_a_flagged_entry_gets_its_own_invoice(self, tmp_path, monkeypatch):
        from istota.money.work import add_work_entry, list_invoices

        config_file, config = self._setup(tmp_path, monkeypatch)
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=8, description="a", separate=True)
        add_work_entry(tmp_path, "2026-03-11", "acme", "dev", qty=2, description="b")
        uids = self._uids(tmp_path)

        results = self._generate(tmp_path, config_file, config)

        assert len(results) == 2
        by_entries = {tuple(r.entry_uids): r for r in list_invoices(tmp_path)}
        assert by_entries[(uids["a"],)].total == 1200.0
        assert by_entries[(uids["a"],)].group_name == "dev"
        assert by_entries[(uids["b"],)].total == 300.0

    def test_two_unflagged_same_service_entries_stay_combined(self, tmp_path, monkeypatch):
        from istota.money.work import add_work_entry, list_invoices

        config_file, config = self._setup(tmp_path, monkeypatch)
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=8, description="a")
        add_work_entry(tmp_path, "2026-03-11", "acme", "dev", qty=2, description="b")

        (result,) = self._generate(tmp_path, config_file, config)

        (record,) = list_invoices(tmp_path)
        assert record.total == 1500.0 == result["total"]
        assert len(record.entry_uids) == 2

    def test_one_named_entry_is_billed_alone(self, tmp_path, monkeypatch):
        from istota.money.work import add_work_entry, list_invoices, load_work_entries

        config_file, config = self._setup(tmp_path, monkeypatch)
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=8, description="a")
        add_work_entry(tmp_path, "2026-03-11", "acme", "dev", qty=2, description="b")
        uids = self._uids(tmp_path)

        (result,) = self._generate(tmp_path, config_file, config, entry_uids=[uids["b"]])

        (record,) = list_invoices(tmp_path)
        assert record.entry_uids == [uids["b"]]
        assert result["total"] == 300.0
        invoiced = {e.description: e.invoice for e in load_work_entries(tmp_path)}
        assert invoiced == {"a": "", "b": "INV-000001"}

    def test_two_named_same_service_entries_share_one_invoice(self, tmp_path, monkeypatch):
        from istota.money.work import add_work_entry, list_invoices

        config_file, config = self._setup(tmp_path, monkeypatch)
        for name in ("a", "b", "c"):
            add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=1, description=name)
        uids = self._uids(tmp_path)

        results = self._generate(
            tmp_path, config_file, config, entry_uids=[uids["a"], uids["c"]],
        )

        assert len(results) == 1
        (record,) = list_invoices(tmp_path)
        assert sorted(record.entry_uids) == sorted([uids["a"], uids["c"]])

    def test_named_entries_ignore_the_period(self, tmp_path, monkeypatch):
        from istota.money.work import add_work_entry

        config_file, config = self._setup(tmp_path, monkeypatch)
        add_work_entry(tmp_path, "2026-05-10", "acme", "dev", qty=1, description="may")
        uids = self._uids(tmp_path)

        results = self._generate(
            tmp_path, config_file, config, period="2026-03", entry_uids=[uids["may"]],
        )
        assert len(results) == 1

    def test_an_unknown_uid_errors_and_writes_nothing(self, tmp_path, monkeypatch):
        from istota.money.work import add_work_entry, list_invoices

        config_file, config = self._setup(tmp_path, monkeypatch)
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=8, description="a")
        uids = self._uids(tmp_path)

        with pytest.raises(ValueError, match="no-such-uid"):
            self._generate(tmp_path, config_file, config, entry_uids=[uids["a"], "no-such-uid"])

        assert list_invoices(tmp_path) == []
        assert not (tmp_path / "invoices").exists()
        assert parse_invoicing_config(config_file).next_invoice_number == 1

    def test_an_invoiced_uid_errors_and_writes_nothing(self, tmp_path, monkeypatch):
        from istota.money.work import add_work_entry, list_invoices

        config_file, config = self._setup(tmp_path, monkeypatch)
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=8, description="a")
        add_work_entry(tmp_path, "2026-03-11", "acme", "dev", qty=2, description="b")
        uids = self._uids(tmp_path)
        self._generate(tmp_path, config_file, config, entry_uids=[uids["a"]])

        with pytest.raises(ValueError, match="already invoiced"):
            self._generate(tmp_path, config_file, config, entry_uids=[uids["b"], uids["a"]])

        assert [r.number for r in list_invoices(tmp_path)] == ["INV-000001"]
        assert len(list((tmp_path / "invoices").rglob("*.pdf"))) == 1


class TestVoidWithCleanup:
    """#755: the void core the CLI and the web route share."""

    def _setup(self, tmp_path, monkeypatch):
        return TestTransactionalGeneration()._setup(tmp_path, monkeypatch)

    def _issue(self, tmp_path, monkeypatch, *, qty=8):
        from istota.money.core.invoicing import generate_invoices_for_period
        from istota.money.work import add_work_entry

        config_file, config = self._setup(tmp_path, monkeypatch)
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=qty)
        (result,) = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
        )
        return Path(result["file"])

    def _void(self, tmp_path, number="INV-000001", **kwargs):
        from istota.money.core.invoicing import void_invoice_with_cleanup

        return void_invoice_with_cleanup(
            tmp_path, number, invoice_output_dir=tmp_path / "invoices", **kwargs,
        )

    def test_a_kept_pdf_is_renamed_void_and_still_found(self, tmp_path, monkeypatch):
        from istota.money.work import get_invoice

        pdf = self._issue(tmp_path, monkeypatch)
        result = self._void(tmp_path, reason="wrong client")

        renamed = pdf.with_name(f"{pdf.stem}-VOID.pdf")
        assert not pdf.exists()
        assert renamed.read_bytes() == b"%PDF-1.4"
        assert renamed.name.startswith("Invoice-000001-") and renamed.name.endswith("-VOID.pdf")
        record = get_invoice(tmp_path, "INV-000001")
        assert record.pdf_path == str(renamed) == result["pdf_path"]
        assert record.status == "voided"
        assert record.void_reason == "wrong client"
        assert result["entries_voided"] == 1
        assert result["was_paid"] is False
        assert find_invoice_pdf(tmp_path / "invoices", "INV-000001") == renamed

    def test_delete_pdf_removes_the_file_and_empties_the_path(self, tmp_path, monkeypatch):
        from istota.money.work import get_invoice

        pdf = self._issue(tmp_path, monkeypatch)
        result = self._void(tmp_path, delete_pdf=True)

        assert result["pdf_deleted"] is True
        assert list((tmp_path / "invoices").rglob("*.pdf")) == []
        assert get_invoice(tmp_path, "INV-000001").pdf_path == ""
        assert not pdf.exists()

    def test_overdue_state_is_cleared_in_the_same_transaction(self, tmp_path, monkeypatch):
        from istota.money import db
        from istota.money.work import db_path_for

        self._issue(tmp_path, monkeypatch)
        with db.get_db(db_path_for(tmp_path)) as conn:
            db.mark_invoice_overdue_notified(conn, "INV-000001")
        result = self._void(tmp_path)

        assert result["db_cleanup"] == {"overdue_notifications_cleared": 1}
        with db.get_db(db_path_for(tmp_path)) as conn:
            assert db.get_notified_overdue_invoices(conn) == set()

    def test_a_failed_rename_rolls_the_void_back(self, tmp_path, monkeypatch):
        from istota.money.work import get_invoice, load_work_entries

        pdf = self._issue(tmp_path, monkeypatch)

        def refuse(self, target):
            raise OSError("read-only file system")

        monkeypatch.setattr(Path, "rename", refuse)
        with pytest.raises(OSError, match="read-only"):
            self._void(tmp_path)
        monkeypatch.undo()

        assert pdf.exists()
        record = get_invoice(tmp_path, "INV-000001")
        assert record.status == "open"
        assert record.pdf_path == str(pdf)
        assert load_work_entries(tmp_path)[0].invoice == "INV-000001"

    def test_a_refused_void_touches_no_file(self, tmp_path, monkeypatch):
        from istota.money.work import VoidError, mark_paid

        pdf = self._issue(tmp_path, monkeypatch)
        mark_paid(tmp_path, "INV-000001", "2026-04-15")
        with pytest.raises(VoidError) as raised:
            self._void(tmp_path, delete_pdf=True)
        assert raised.value.code == "paid"
        assert pdf.exists()

    def test_a_paid_invoice_voids_with_force(self, tmp_path, monkeypatch):
        from istota.money.work import get_invoice, mark_paid

        self._issue(tmp_path, monkeypatch)
        mark_paid(tmp_path, "INV-000001", "2026-04-15")
        result = self._void(tmp_path, force=True)
        assert result["was_paid"] is True
        assert get_invoice(tmp_path, "INV-000001").was_paid is True

    def test_an_invoice_with_no_pdf_voids(self, tmp_path, monkeypatch):
        from istota.money.work import get_invoice

        pdf = self._issue(tmp_path, monkeypatch)
        pdf.unlink()
        result = self._void(tmp_path, delete_pdf=True)
        assert result["pdf_deleted"] is False
        assert get_invoice(tmp_path, "INV-000001").status == "voided"


class TestCreateManualInvoice:
    """The manual-invoice core shared by ``invoice create`` and the web route."""

    def _create(self, tmp_path, monkeypatch, lines, **kwargs):
        from istota.money.core.invoicing import create_manual_invoice

        config_file, config = TestTransactionalGeneration()._setup(tmp_path, monkeypatch)
        return create_manual_invoice(
            config=config, data_dir=tmp_path, accounting_path=tmp_path,
            invoice_output_dir=tmp_path / "invoices", client_key="acme",
            lines=lines, config_path=config_file, **kwargs,
        ), config_file

    def test_stores_the_invoice_with_its_total(self, tmp_path, monkeypatch):
        from istota.money.work import get_invoice, load_work_entries

        result, config_file = self._create(tmp_path, monkeypatch, [
            {"service": "dev", "qty": 2, "description": "Fix"},
            {"description": "Travel", "amount": 300},
        ])

        assert result["invoice_number"] == "INV-000001"
        assert result["total"] == 600.0
        record = get_invoice(tmp_path, "INV-000001")
        assert record.manual is True
        assert record.total == 600.0
        assert record.pdf_path == result["file"]
        assert Path(result["file"]).exists()
        assert sorted(record.entry_uids) == sorted(result["entry_uids"])
        services = sorted(e.service for e in load_work_entries(tmp_path))
        assert services == ["_manual", "dev"]
        assert parse_invoicing_config(config_file).next_invoice_number == 2

    @pytest.mark.parametrize("lines, message", [
        ([], "No line items"),
        ([{"service": "nope", "qty": 1}], "Service 'nope' not found"),
        ([{"description": "Travel"}], "amount"),
        ([{"description": "Travel", "amount": "lots"}], "amount"),
        ([{"description": "Travel", "amount": float("nan")}], "amount"),
    ])
    def test_a_bad_line_is_refused_before_anything_is_written(
        self, tmp_path, monkeypatch, lines, message,
    ):
        from istota.money.work import list_invoices, load_work_entries

        with pytest.raises(ValueError, match=message):
            self._create(tmp_path, monkeypatch, lines)
        assert list_invoices(tmp_path) == []
        assert load_work_entries(tmp_path) == []
        assert list((tmp_path / "invoices").rglob("*.pdf")) == []

    def test_an_unknown_client_is_refused(self, tmp_path, monkeypatch):
        from istota.money.core.invoicing import create_manual_invoice

        _, config = TestTransactionalGeneration()._setup(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="Client 'globex' not found"):
            create_manual_invoice(
                config=config, data_dir=tmp_path, accounting_path=tmp_path,
                invoice_output_dir=tmp_path / "invoices", client_key="globex",
                lines=[{"description": "x", "amount": 1}],
            )


class TestConcurrentIssuers:
    """Two issuers in different processes must never overwrite or delete each
    other's PDF: one holds the store's issuing lock from number to commit, and
    a PDF is published under its name only if nothing is there yet."""

    def _setup(self, tmp_path, monkeypatch):
        from istota.money.core import invoicing as invoicing_mod

        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            f'accounting_path = "{tmp_path}"\n'
            'invoice_output = "invoices"\nnext_invoice_number = 1\n\n'
            '[company]\nname = "My Co"\n\n'
            '[clients.acme]\nname = "Acme"\nterms = 30\n\n'
            '[clients.beta]\nname = "Beta"\nterms = 30\n\n'
            '[services.dev]\ndisplay_name = "Dev"\nrate = 150\ntype = "hours"\n'
        )

        # The real publish step runs; only WeasyPrint is stood in for.
        fake = types.ModuleType("weasyprint")
        fake.HTML = lambda string: types.SimpleNamespace(write_pdf=lambda: string.encode())
        monkeypatch.setitem(sys.modules, "weasyprint", fake)
        monkeypatch.setattr(
            invoicing_mod, "generate_invoice_html",
            lambda invoice, **k: f"PDF for {invoice.client.name}",
        )
        return config_file, parse_invoicing_config(config_file)

    def _generate(self, tmp_path, config_file, config, client):
        from istota.money.core.invoicing import generate_invoices_for_period

        return generate_invoices_for_period(
            config=config, config_path=config_file, accounting_path=tmp_path,
            data_dir=tmp_path, client_filter=client,
        )

    def test_a_stale_number_does_not_touch_the_winners_pdf(self, tmp_path, monkeypatch):
        from istota.money.core import invoicing as invoicing_mod
        from istota.money.work import add_work_entry, get_invoice, list_invoices

        config_file, config = self._setup(tmp_path, monkeypatch)
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=1)
        add_work_entry(tmp_path, "2026-03-10", "beta", "dev", qty=2)
        (first,) = self._generate(tmp_path, config_file, config, "acme")
        pdf = Path(first["file"])

        # The second issuer read the highest number before the first committed.
        monkeypatch.setattr(invoicing_mod, "highest_existing_invoice_number", lambda d: 0)
        with pytest.raises(invoicing_mod.InvoiceRecordError, match="INV-000001"):
            self._generate(tmp_path, config_file, config, "beta")

        assert pdf.read_text() == "PDF for Acme"
        assert Path(get_invoice(tmp_path, "INV-000001").pdf_path).is_file()
        assert [r.client for r in list_invoices(tmp_path)] == ["acme"]
        assert [p.name for p in pdf.parent.iterdir()] == [pdf.name]

    def test_publishing_never_replaces_a_pdf(self, tmp_path, monkeypatch):
        from istota.money.core.invoicing import generate_invoice_pdf

        self._setup(tmp_path, monkeypatch)
        pdf = tmp_path / "invoices" / "2026" / "Invoice-000001-03_10_2026.pdf"
        generate_invoice_pdf("first", pdf, root=tmp_path)
        with pytest.raises(FileExistsError):
            generate_invoice_pdf("second", pdf, root=tmp_path)
        assert pdf.read_text() == "first"
        assert [p.name for p in pdf.parent.iterdir()] == [pdf.name]

    def test_a_stale_manual_number_does_not_touch_the_winners_pdf(self, tmp_path, monkeypatch):
        from istota.money.core import invoicing as invoicing_mod
        from istota.money.core.invoicing import create_manual_invoice

        config_file, config = self._setup(tmp_path, monkeypatch)

        def create(client):
            return create_manual_invoice(
                config=config, data_dir=tmp_path, accounting_path=tmp_path,
                invoice_output_dir=tmp_path / "invoices", client_key=client,
                lines=[{"description": "x", "amount": 10}], config_path=config_file,
            )

        pdf = Path(create("acme")["file"])
        monkeypatch.setattr(invoicing_mod, "highest_existing_invoice_number", lambda d: 0)
        config.next_invoice_number = 1
        with pytest.raises(invoicing_mod.InvoiceRecordError, match="INV-000001"):
            create("beta")
        assert pdf.read_text() == "PDF for Acme"

    def test_generation_holds_the_issuing_lock_while_it_renders(self, tmp_path, monkeypatch):
        import threading

        from istota.money.core import invoicing as invoicing_mod
        from istota.money.work import add_work_entry

        config_file, config = self._setup(tmp_path, monkeypatch)
        add_work_entry(tmp_path, "2026-03-10", "acme", "dev", qty=1)
        seen = []
        real_pdf = invoicing_mod.generate_invoice_pdf

        def probing_pdf(html_text, output_path, **kwargs):
            # Another issuer (its own descriptor, as another process would be)
            # must not get the lock while this one renders.
            def other():
                try:
                    with invoicing_mod.invoice_issue_lock(tmp_path, timeout_seconds=0.2):
                        seen.append("acquired")
                except invoicing_mod.InvoiceIssueBusy:
                    seen.append("busy")

            thread = threading.Thread(target=other)
            thread.start()
            thread.join()
            real_pdf(html_text, output_path, **kwargs)

        monkeypatch.setattr(invoicing_mod, "generate_invoice_pdf", probing_pdf)
        self._generate(tmp_path, config_file, config, "acme")
        assert seen == ["busy"]
        with invoicing_mod.invoice_issue_lock(tmp_path, timeout_seconds=0.2):
            pass

    def test_manual_creation_holds_the_issuing_lock(self, tmp_path, monkeypatch):
        import threading

        from istota.money.core import invoicing as invoicing_mod
        from istota.money.core.invoicing import create_manual_invoice

        config_file, config = self._setup(tmp_path, monkeypatch)
        held = threading.Event()
        release = threading.Event()

        def holder():
            with invoicing_mod.invoice_issue_lock(tmp_path):
                held.set()
                release.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(5)
        try:
            with pytest.raises(invoicing_mod.InvoiceIssueBusy):
                with invoicing_mod.invoice_issue_lock(tmp_path, timeout_seconds=0.2):
                    pass
            real_lock = invoicing_mod.invoice_issue_lock
            monkeypatch.setattr(
                invoicing_mod, "invoice_issue_lock",
                lambda d, **k: real_lock(d, timeout_seconds=0.2),
            )
            with pytest.raises(invoicing_mod.InvoiceIssueBusy):
                create_manual_invoice(
                    config=config, data_dir=tmp_path, accounting_path=tmp_path,
                    invoice_output_dir=tmp_path / "invoices", client_key="acme",
                    lines=[{"description": "x", "amount": 10}], config_path=config_file,
                )
        finally:
            release.set()
            thread.join()
        assert not (tmp_path / "invoices").exists()


class TestInvoiceHtmlEscaping:
    """Names, addresses and descriptions are user input — typed into the CLI
    once, and into a browser form since. An unescaped `&` in a company name is
    enough to garble the rendered PDF; a stray tag is enough to restyle it."""

    def _invoice(self, **overrides):
        client = ClientConfig(
            key="acme", name=overrides.get("client_name", "Acme Corp"), terms=30,
            address=overrides.get("client_address", ""),
            email=overrides.get("client_email", ""),
        )
        company = CompanyConfig(
            name=overrides.get("company_name", "My Co"),
            address=overrides.get("company_address", ""),
            payment_instructions=overrides.get("payment_instructions", ""),
        )
        items = [InvoiceLineItem(
            display_name=overrides.get("item_name", "Dev"),
            description=overrides.get("item_desc", ""),
            quantity=8, rate=150, discount=0, amount=1200,
        )]
        return Invoice(
            number="INV-000001", date=date(2026, 3, 1), due_date=date(2026, 3, 31),
            client=client, company=company, items=items, total=1200,
            group_name=overrides.get("group_name", ""),
        )

    def test_ampersand_in_company_name_is_escaped(self):
        html = generate_invoice_html(self._invoice(company_name="Smith & Jones"))
        assert "Smith &amp; Jones" in html
        assert "Smith & Jones" not in html

    def test_tags_do_not_reach_the_document(self):
        html = generate_invoice_html(self._invoice(
            client_name="<script>alert(1)</script>",
            item_desc="<b>bold</b>",
            payment_instructions="<style>*{display:none}</style>",
            group_name="<i>ref</i>",
        ))
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "<b>bold</b>" not in html
        assert "<style>*{display:none}</style>" not in html
        assert "<i>ref</i>" not in html

    def test_quotes_in_a_logo_alt_attribute_are_escaped(self, tmp_path):
        logo = tmp_path / "logo.png"
        logo.write_bytes(b"\x89PNG\r\n")
        html = generate_invoice_html(self._invoice(company_name='Co" onerror="x'), logo)
        assert 'onerror="x' not in html

    def test_ordinary_text_is_unchanged(self):
        html = generate_invoice_html(self._invoice(
            company_name="My Co", client_name="Acme Corp", item_name="Development",
        ))
        assert "My Co" in html
        assert "Acme Corp" in html
        assert "Development" in html


class TestLogoStaysInsideAccountingPath:
    """`accounting_path / logo` follows pathlib semantics, so an absolute logo
    replaces the left-hand side and the file is base64-embedded into the PDF."""

    def test_absolute_path_is_ignored(self, tmp_path):
        from istota.money.core.invoicing import _resolve_logo

        outside = tmp_path / "secret.png"
        outside.write_bytes(b"x")
        accounting = tmp_path / "accounting"
        accounting.mkdir()
        assert _resolve_logo(accounting, str(outside)) is None

    def test_parent_climb_is_ignored(self, tmp_path):
        from istota.money.core.invoicing import _resolve_logo

        outside = tmp_path / "secret.png"
        outside.write_bytes(b"x")
        accounting = tmp_path / "accounting"
        accounting.mkdir()
        assert _resolve_logo(accounting, "../secret.png") is None

    def test_relative_path_inside_resolves(self, tmp_path):
        from istota.money.core.invoicing import _resolve_logo

        accounting = tmp_path / "accounting"
        (accounting / "invoices").mkdir(parents=True)
        logo = accounting / "invoices" / "logo.png"
        logo.write_bytes(b"x")
        assert _resolve_logo(accounting, "invoices/logo.png") == logo.resolve()

    def test_missing_file_is_none(self, tmp_path):
        from istota.money.core.invoicing import _resolve_logo

        accounting = tmp_path / "accounting"
        accounting.mkdir()
        assert _resolve_logo(accounting, "invoices/nope.png") is None

    def test_blank_is_none(self, tmp_path):
        from istota.money.core.invoicing import _resolve_logo

        assert _resolve_logo(tmp_path, "") is None
