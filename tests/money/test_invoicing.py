"""Tests for money.core.invoicing module."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from money.core.invoicing import (
    build_line_items,
    compute_income_lines,
    create_income_posting,
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
from money.core.models import (
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
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry

        config_file = tmp_path / "invoicing.toml"
        config_file.write_text(
            f'accounting_path = "{tmp_path}"\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "My Co"\n\n'
            '[clients.sosf]\nname = "SOSF Inc"\nterms = 30\n\n'
            '[services.dev]\ndisplay_name = "Development"\nrate = 150\ntype = "hours"\n'
            'income_account = "Income:Dev"\n'
        )
        config = parse_invoicing_config(config_file)

        # add_work_entry now normalizes to lowercase, so this tests the full flow
        add_work_entry(tmp_path, "2026-03-01", "sosf", "dev", qty=8)

        results = generate_invoices_for_period(
            config=config, config_path=config_file,
            accounting_path=tmp_path, data_dir=tmp_path,
            dry_run=True,
        )
        assert len(results) == 1
        assert results[0]["client"] == "SOSF Inc"
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
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry, get_uninvoiced_entries

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
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry

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
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry

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
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry

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

        pytest.importorskip("weasyprint")

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
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry, load_work_entries

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)

        # "consulting" is not a configured service — only "dev" exists
        add_work_entry(tmp_path, "2026-03-01", "acme", "consulting", qty=8)

        pytest.importorskip("weasyprint")

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
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry

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
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry, load_work_entries

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)

        add_work_entry(tmp_path, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(tmp_path, "2026-03-02", "acme", "consulting", qty=4)

        pytest.importorskip("weasyprint")

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
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry

        config_file = self._make_config(tmp_path)
        config = parse_invoicing_config(config_file)
        add_work_entry(tmp_path, "2026-03-01", "acme", "consulting", qty=8)

        pytest.importorskip("weasyprint")

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
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry

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
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry

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
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry

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
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry

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
        from money.cli import _resolve
        result = _resolve(tmp_path, "config/invoicing.toml")
        assert result == tmp_path / "config/invoicing.toml"

    def test_absolute_path_unchanged(self, tmp_path):
        from money.cli import _resolve
        result = _resolve(tmp_path, "/etc/moneyman/config.toml")
        assert result == Path("/etc/moneyman/config.toml")


class TestLoadInvoicingConfigReturnsResolvedPaths:
    """_load_invoicing_config must return absolute paths so PDFs land in data_dir."""

    def _make_config(self, tmp_path, accounting_path=".", invoice_output="invoices/generated"):
        from money.cli import Context
        invoicing_toml = tmp_path / "invoicing.toml"
        invoicing_toml.write_text(
            f'accounting_path = "{accounting_path}"\n'
            f'invoice_output = "{invoice_output}"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "Co"\n\n'
            '[clients.acme]\nname = "Acme"\nterms = 30\n\n'
            '[services.dev]\ndisplay_name = "Dev"\nrate = 100\n'
        )
        ctx = Context()
        ctx.data_dir = tmp_path / "data"
        ctx.data_dir.mkdir()
        ctx.invoicing_config_path = invoicing_toml
        return ctx

    def test_invoice_output_dir_resolved_relative_to_data_dir(self, tmp_path):
        """invoice_output should resolve relative to data_dir, not accounting_path."""
        from money.cli import _load_invoicing_config
        ctx = self._make_config(tmp_path, accounting_path="accounting", invoice_output="invoices/generated")
        _, accounting_path, invoice_output_dir = _load_invoicing_config(ctx)
        # accounting_path is data_dir / "accounting"
        assert accounting_path == ctx.data_dir / "accounting"
        # invoice_output_dir is data_dir / "invoices/generated" (NOT accounting/invoices/generated)
        assert invoice_output_dir == ctx.data_dir / "invoices/generated"

    def test_invoice_output_dir_is_absolute(self, tmp_path):
        """Returned invoice_output_dir must be absolute."""
        from money.cli import _load_invoicing_config
        ctx = self._make_config(tmp_path)
        _, _, invoice_output_dir = _load_invoicing_config(ctx)
        assert invoice_output_dir.is_absolute()

    def test_accounting_path_dot_resolves_to_data_dir(self, tmp_path):
        """accounting_path='.' should resolve to data_dir itself."""
        from money.cli import _load_invoicing_config
        ctx = self._make_config(tmp_path, accounting_path=".")
        _, accounting_path, _ = _load_invoicing_config(ctx)
        assert accounting_path == ctx.data_dir / "."

    def test_fallback_without_data_dir_uses_resolve(self, tmp_path):
        """When data_dir is None, paths should still be absolute via resolve()."""
        from money.cli import _load_invoicing_config, Context
        invoicing_toml = tmp_path / "invoicing.toml"
        invoicing_toml.write_text(
            'accounting_path = "."\n'
            'invoice_output = "invoices/out"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "Co"\n\n'
            '[clients.a]\nname = "A"\n\n'
            '[services.s]\ndisplay_name = "S"\nrate = 1\n'
        )
        ctx = Context()
        ctx.data_dir = None
        ctx.invoicing_config_path = invoicing_toml
        _, accounting_path, invoice_output_dir = _load_invoicing_config(ctx)
        assert accounting_path.is_absolute()
        assert invoice_output_dir.is_absolute()


class TestGenerateInvoicesPdfOutputDir:
    """generate_invoices_for_period should use invoice_output_dir when provided."""

    def test_pdf_written_to_invoice_output_dir(self, tmp_path):
        """PDF should be written under invoice_output_dir, not under accounting_path."""
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry

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

        pytest.importorskip("weasyprint")

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
        from money.core.invoicing import generate_invoices_for_period
        from money.work import add_work_entry

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

        pytest.importorskip("weasyprint")

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
        from money.db import init_db
        db_path = tmp_path / "money.db"
        init_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def test_returns_due_clients_on_schedule_day(self, tmp_path):
        from money.core.invoicing import check_scheduled_invoices

        config_file = self._make_config(tmp_path, day=15)
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        result = check_scheduled_invoices(config, conn, today=date(2026, 3, 15))
        assert result == ["acme"]

    def test_returns_empty_when_not_schedule_day(self, tmp_path):
        from money.core.invoicing import check_scheduled_invoices

        config_file = self._make_config(tmp_path, day=15)
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        result = check_scheduled_invoices(config, conn, today=date(2026, 3, 14))
        assert result == []

    def test_skips_on_demand_clients(self, tmp_path):
        from money.core.invoicing import check_scheduled_invoices

        config_file = self._make_config(tmp_path, schedule="on-demand", day=15)
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        result = check_scheduled_invoices(config, conn, today=date(2026, 3, 15))
        assert result == []

    def test_skips_already_generated_this_month(self, tmp_path):
        from money.core.invoicing import check_scheduled_invoices
        from money.db import set_invoice_schedule_generation

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
        from money.core.invoicing import check_scheduled_invoices

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
        from money.core.invoicing import check_scheduled_invoices

        config_file = self._make_config(tmp_path, day=15)
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        # It's the 20th, no generation happened this month
        result = check_scheduled_invoices(config, conn, today=date(2026, 3, 20))
        assert result == ["acme"]

    def test_does_not_fire_before_schedule_day(self, tmp_path):
        """Before the schedule day, don't generate even if no generation this month."""
        from money.core.invoicing import check_scheduled_invoices

        config_file = self._make_config(tmp_path, day=15)
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        result = check_scheduled_invoices(config, conn, today=date(2026, 3, 10))
        assert result == []

    def test_schedule_day_end_of_month_clamped(self, tmp_path):
        """Schedule day 31 should fire on the last day of shorter months."""
        from money.core.invoicing import check_scheduled_invoices

        config_file = self._make_config(tmp_path, day=31)
        config = parse_invoicing_config(config_file)
        conn = self._make_db(tmp_path)

        # February 28 (non-leap) — last day, should fire
        result = check_scheduled_invoices(config, conn, today=date(2026, 2, 28))
        assert result == ["acme"]

    def test_multiple_clients_mixed(self, tmp_path):
        """Multiple clients: only due ones returned."""
        from money.core.invoicing import check_scheduled_invoices

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
