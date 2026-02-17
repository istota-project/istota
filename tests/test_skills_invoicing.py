"""Tests for skills/invoicing.py module."""

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.skills.invoicing import (
    ClientConfig,
    CompanyConfig,
    Invoice,
    InvoiceLineItem,
    InvoicingConfig,
    ServiceConfig,
    WorkEntry,
    _embed_logo,
    _extract_toml_from_markdown,
    _resolve_nc_path,
    build_line_items,
    compute_income_lines,
    create_income_posting,
    filter_entries_by_period,
    format_invoice_number,
    generate_invoice,
    generate_invoice_html,
    generate_invoices_for_period,
    group_entries_by_bundle,
    parse_invoicing_config,
    parse_work_log,
    resolve_bank_account,
    resolve_currency,
    resolve_entity,
    select_uninvoiced_entries,
    stamp_work_log_entries,
    stamp_work_log_paid_dates,
    update_invoice_number,
)


SAMPLE_CONFIG_TOML = """\
# Invoicing Configuration

```toml
accounting_path = "/accounting"
work_log = "/notes/_INVOICES.md"
invoice_output = "invoices/generated"
next_invoice_number = 42

[company]
name = "TestCo"
address = "123 Main St"
email = "billing@testco.com"
payment_instructions = "Pay via wire"

[clients.acme]
name = "Acme Corp"
address = "456 Oak Ave"
email = "billing@acme.com"
terms = 30

[clients.acme.invoicing]
schedule = "monthly"
day = 1
bundles = [
  { services = ["consulting", "development"], name = "Professional Services" }
]
separate = ["expenses"]

[clients.beta]
name = "Beta Inc"
address = "789 Pine St"
terms = 15

[services.consulting]
display_name = "Consulting Services"
rate = 150
type = "hours"

[services.development]
display_name = "Development Services"
rate = 175
type = "hours"

[services.expenses]
display_name = "Reimbursable Expenses"
rate = 0
type = "other"

[services.hosting]
display_name = "Monthly Hosting"
rate = 500
type = "flat"
```
"""

SAMPLE_WORK_LOG = """\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4
description = "Architecture review"

[[entries]]
date = 2026-02-03
client = "acme"
service = "development"
qty = 8
description = "Feature implementation"

[[entries]]
date = 2026-02-05
client = "acme"
service = "expenses"
amount = 340.50
description = "Flight to NYC"

[[entries]]
date = 2026-02-10
client = "beta"
service = "consulting"
qty = 2
description = "Strategy call"

[[entries]]
date = 2026-01-15
client = "acme"
service = "consulting"
qty = 6
description = "January work"
```
"""


class TestExtractToml:
    def test_extract_single_block(self):
        md = "# Config\n\n```toml\nkey = \"value\"\n```\n"
        assert _extract_toml_from_markdown(md) == 'key = "value"\n'

    def test_extract_multiple_blocks(self):
        md = "```toml\na = 1\n```\ntext\n```toml\nb = 2\n```\n"
        result = _extract_toml_from_markdown(md)
        assert "a = 1" in result
        assert "b = 2" in result

    def test_no_toml_block_raises(self):
        with pytest.raises(ValueError, match="No TOML code block"):
            _extract_toml_from_markdown("# Just markdown")

    def test_non_toml_code_block_ignored(self):
        md = "```python\nprint('hello')\n```\n"
        with pytest.raises(ValueError, match="No TOML code block"):
            _extract_toml_from_markdown(md)


class TestResolveNcPath:
    def test_no_mount_path_returns_raw(self, monkeypatch):
        monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
        result = _resolve_nc_path("/Users/alice/istota/config/file.md")
        assert result == Path("/Users/alice/istota/config/file.md")

    def test_with_mount_path_prepends(self, monkeypatch):
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", "/srv/mount/nc")
        result = _resolve_nc_path("/Users/alice/istota/config/file.md")
        assert result == Path("/srv/mount/nc/Users/alice/istota/config/file.md")

    def test_strips_leading_slash(self, monkeypatch):
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", "/srv/mount/nc")
        result = _resolve_nc_path("///some/path")
        assert result == Path("/srv/mount/nc/some/path")

    def test_empty_mount_path_returns_raw(self, monkeypatch):
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", "")
        result = _resolve_nc_path("/some/path")
        assert result == Path("/some/path")


class TestParseInvoicingConfig:
    def test_parse_full_config(self, tmp_path):
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(SAMPLE_CONFIG_TOML)

        config = parse_invoicing_config(config_file)

        assert config.accounting_path == "/accounting"
        assert config.work_log == "/notes/_INVOICES.md"
        assert config.invoice_output == "invoices/generated"
        assert config.next_invoice_number == 42

    def test_parse_company(self, tmp_path):
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(SAMPLE_CONFIG_TOML)

        config = parse_invoicing_config(config_file)

        assert config.company.name == "TestCo"
        assert config.company.address == "123 Main St"
        assert config.company.email == "billing@testco.com"
        assert config.company.payment_instructions == "Pay via wire"

    def test_parse_clients(self, tmp_path):
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(SAMPLE_CONFIG_TOML)

        config = parse_invoicing_config(config_file)

        assert "acme" in config.clients
        assert "beta" in config.clients

        acme = config.clients["acme"]
        assert acme.key == "acme"
        assert acme.name == "Acme Corp"
        assert acme.terms == 30
        assert acme.schedule == "monthly"
        assert acme.schedule_day == 1
        assert len(acme.bundles) == 1
        assert acme.bundles[0]["name"] == "Professional Services"
        assert acme.separate == ["expenses"]

        beta = config.clients["beta"]
        assert beta.terms == 15
        assert beta.schedule == "on-demand"

    def test_parse_services(self, tmp_path):
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(SAMPLE_CONFIG_TOML)

        config = parse_invoicing_config(config_file)

        assert "consulting" in config.services
        assert "development" in config.services
        assert "expenses" in config.services
        assert "hosting" in config.services

        consulting = config.services["consulting"]
        assert consulting.display_name == "Consulting Services"
        assert consulting.rate == 150.0
        assert consulting.type == "hours"

        expenses = config.services["expenses"]
        assert expenses.type == "other"
        assert expenses.rate == 0

        hosting = config.services["hosting"]
        assert hosting.type == "flat"
        assert hosting.rate == 500

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            parse_invoicing_config(tmp_path / "nonexistent.md")

    def test_parse_client_reminder_days(self, tmp_path):
        content = """\
# Invoicing

```toml
[company]
name = "Co"

[clients.acme]
name = "Acme"

[clients.acme.invoicing]
schedule = "monthly"
day = 15
reminder_days = 5

[services.consulting]
display_name = "Consulting"
rate = 100
```
"""
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(content)
        config = parse_invoicing_config(config_file)
        assert config.clients["acme"].reminder_days == 5

    def test_parse_client_reminder_days_default(self, tmp_path):
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(SAMPLE_CONFIG_TOML)
        config = parse_invoicing_config(config_file)
        # Default reminder_days is 3
        assert config.clients["beta"].reminder_days == 3

    def test_parse_client_notifications(self, tmp_path):
        content = """\
# Invoicing

```toml
[company]
name = "Co"

[clients.acme]
name = "Acme"

[clients.acme.invoicing]
notifications = "email"

[services.consulting]
display_name = "Consulting"
rate = 100
```
"""
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(content)
        config = parse_invoicing_config(config_file)
        assert config.clients["acme"].notifications == "email"

    def test_parse_global_notifications(self, tmp_path):
        content = """\
# Invoicing

```toml
notifications = "both"

[company]
name = "Co"

[clients.acme]
name = "Acme"

[services.consulting]
display_name = "Consulting"
rate = 100
```
"""
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(content)
        config = parse_invoicing_config(config_file)
        assert config.notifications == "both"

    def test_parse_notifications_defaults_empty(self, tmp_path):
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(SAMPLE_CONFIG_TOML)
        config = parse_invoicing_config(config_file)
        assert config.notifications == ""
        assert config.clients["acme"].notifications == ""

    def test_parse_global_days_until_overdue(self, tmp_path):
        content = """\
# Invoicing

```toml
days_until_overdue = 45

[company]
name = "Co"

[clients.acme]
name = "Acme"

[services.consulting]
display_name = "Consulting"
rate = 100
```
"""
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(content)
        config = parse_invoicing_config(config_file)
        assert config.days_until_overdue == 45

    def test_parse_client_days_until_overdue(self, tmp_path):
        content = """\
# Invoicing

```toml
[company]
name = "Co"

[clients.acme]
name = "Acme"

[clients.acme.invoicing]
days_until_overdue = 15

[services.consulting]
display_name = "Consulting"
rate = 100
```
"""
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(content)
        config = parse_invoicing_config(config_file)
        assert config.clients["acme"].days_until_overdue == 15

    def test_parse_days_until_overdue_defaults_zero(self, tmp_path):
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(SAMPLE_CONFIG_TOML)
        config = parse_invoicing_config(config_file)
        assert config.days_until_overdue == 0
        assert config.clients["acme"].days_until_overdue == 0


class TestParseWorkLog:
    def test_parse_entries(self, tmp_path):
        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text(SAMPLE_WORK_LOG)

        entries = parse_work_log(log_file)

        assert len(entries) == 5

    def test_entry_fields(self, tmp_path):
        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text(SAMPLE_WORK_LOG)

        entries = parse_work_log(log_file)

        first = entries[0]
        assert first.date == date(2026, 2, 1)
        assert first.client == "acme"
        assert first.service == "consulting"
        assert first.qty == 4
        assert first.description == "Architecture review"

    def test_passthrough_amount(self, tmp_path):
        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text(SAMPLE_WORK_LOG)

        entries = parse_work_log(log_file)

        expense_entry = entries[2]
        assert expense_entry.service == "expenses"
        assert expense_entry.amount == 340.50
        assert expense_entry.qty is None

    def test_empty_work_log(self, tmp_path):
        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text("# Work Log\n\n```toml\n```\n")

        entries = parse_work_log(log_file)
        assert entries == []


class TestFilterEntries:
    def _make_entries(self):
        return [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", qty=4),
            WorkEntry(date=date(2026, 2, 15), client="acme", service="development", qty=8),
            WorkEntry(date=date(2026, 2, 10), client="beta", service="consulting", qty=2),
            WorkEntry(date=date(2026, 1, 15), client="acme", service="consulting", qty=6),
            WorkEntry(date=date(2026, 3, 1), client="acme", service="consulting", qty=3),
        ]

    def test_filter_by_period(self):
        entries = self._make_entries()
        filtered = filter_entries_by_period(entries, "2026-02")

        assert len(filtered) == 3

    def test_filter_by_period_and_client(self):
        entries = self._make_entries()
        filtered = filter_entries_by_period(entries, "2026-02", client="acme")

        assert len(filtered) == 2
        assert all(e.client == "acme" for e in filtered)

    def test_filter_empty_period(self):
        entries = self._make_entries()
        filtered = filter_entries_by_period(entries, "2026-06")

        assert len(filtered) == 0

    def test_filter_none_client_includes_all(self):
        entries = self._make_entries()
        filtered = filter_entries_by_period(entries, "2026-02", client=None)

        assert len(filtered) == 3


class TestGroupEntriesByBundle:
    def _make_client_config(self):
        return ClientConfig(
            key="acme",
            name="Acme Corp",
            bundles=[{"services": ["consulting", "development"], "name": "Professional Services"}],
            separate=["expenses"],
        )

    def test_bundled_services_grouped(self):
        client = self._make_client_config()
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", qty=4),
            WorkEntry(date=date(2026, 2, 3), client="acme", service="development", qty=8),
        ]

        groups = group_entries_by_bundle(entries, client)

        assert len(groups) == 1
        assert groups[0][0] == "Professional Services"
        assert len(groups[0][1]) == 2

    def test_separate_services_get_own_group(self):
        client = self._make_client_config()
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", qty=4),
            WorkEntry(date=date(2026, 2, 5), client="acme", service="expenses", amount=340),
        ]

        groups = group_entries_by_bundle(entries, client)

        assert len(groups) == 2
        group_names = [g[0] for g in groups]
        assert "Professional Services" in group_names
        assert "expenses" in group_names

    def test_unbundled_services_default_group(self):
        client = ClientConfig(key="acme", name="Acme")
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="misc", qty=2),
        ]

        groups = group_entries_by_bundle(entries, client)

        assert len(groups) == 1
        assert groups[0][0] == "Services"

    def test_empty_entries(self):
        client = self._make_client_config()
        groups = group_entries_by_bundle([], client)
        assert groups == []


class TestBuildLineItems:
    def _make_services(self):
        return {
            "consulting": ServiceConfig(key="consulting", display_name="Consulting Services", rate=150, type="hours"),
            "expenses": ServiceConfig(key="expenses", display_name="Reimbursable Expenses", rate=0, type="other"),
            "hosting": ServiceConfig(key="hosting", display_name="Monthly Hosting", rate=500, type="flat"),
            "onsite": ServiceConfig(key="onsite", display_name="On-site Support", rate=1200, type="days"),
        }

    def test_hourly_items(self):
        services = self._make_services()
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", qty=4, description="Review"),
        ]

        items = build_line_items(entries, services)

        assert len(items) == 1
        assert items[0].display_name == "Consulting Services"
        assert items[0].description == "Review"
        assert items[0].quantity == 4
        assert items[0].rate == 150
        assert items[0].discount == 0
        assert items[0].amount == 600

    def test_discount_applied(self):
        services = self._make_services()
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", qty=4, discount=100, description="Review"),
        ]

        items = build_line_items(entries, services)

        assert len(items) == 1
        assert items[0].rate == 150
        assert items[0].discount == 100
        assert items[0].amount == 500  # (4 * 150) - 100

    def test_passthrough_items(self):
        services = self._make_services()
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="expenses", amount=340.50, description="Flight"),
        ]

        items = build_line_items(entries, services)

        assert len(items) == 1
        assert items[0].description == "Flight"
        assert items[0].quantity == 1
        assert items[0].rate == 340.50
        assert items[0].amount == 340.50

    def test_flat_items(self):
        services = self._make_services()
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="hosting"),
        ]

        items = build_line_items(entries, services)

        assert len(items) == 1
        assert items[0].rate == 500
        assert items[0].amount == 500

    def test_daily_items(self):
        services = self._make_services()
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="onsite", qty=3, description="On-site migration"),
        ]

        items = build_line_items(entries, services)

        assert len(items) == 1
        assert items[0].description == "On-site migration"
        assert items[0].quantity == 3
        assert items[0].rate == 1200
        assert items[0].amount == 3600

    def test_unknown_service_skipped(self):
        services = self._make_services()
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="unknown"),
        ]

        items = build_line_items(entries, services)
        assert len(items) == 0

    def test_hourly_with_no_hours(self):
        services = self._make_services()
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting"),
        ]

        items = build_line_items(entries, services)

        assert items[0].quantity == 0
        assert items[0].amount == 0


class TestFormatInvoiceNumber:
    def test_format_single_digit(self):
        assert format_invoice_number(1) == "INV-000001"

    def test_format_large_number(self):
        assert format_invoice_number(12345) == "INV-012345"

    def test_format_six_digit(self):
        assert format_invoice_number(999999) == "INV-999999"


class TestGenerateInvoice:
    def test_generate_basic_invoice(self):
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", qty=10, description="Work"),
        ]
        client = ClientConfig(key="acme", name="Acme Corp", terms=30)
        company = CompanyConfig(name="TestCo")
        services = {"consulting": ServiceConfig(key="consulting", display_name="Consulting", rate=150, type="hours")}

        invoice = generate_invoice(entries, "Services", client, company, services, 1, date(2026, 2, 28))

        assert invoice.number == "INV-000001"
        assert invoice.date == date(2026, 2, 28)
        assert invoice.due_date == date(2026, 3, 30)
        assert invoice.total == 1500.0
        assert len(invoice.items) == 1
        assert invoice.group_name == "Services"

    def test_invoice_string_terms(self):
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", qty=10, description="Work"),
        ]
        client = ClientConfig(key="acme", name="Acme Corp", terms="On receipt")
        company = CompanyConfig(name="TestCo")
        services = {"consulting": ServiceConfig(key="consulting", display_name="Consulting", rate=150, type="hours")}

        invoice = generate_invoice(entries, "Services", client, company, services, 1, date(2026, 2, 28))

        assert invoice.due_date is None
        html = generate_invoice_html(invoice)
        assert "On receipt" in html

    def test_invoice_multiple_items(self):
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", qty=10, description="Consulting"),
            WorkEntry(date=date(2026, 2, 5), client="acme", service="consulting", qty=5, description="More consulting"),
        ]
        client = ClientConfig(key="acme", name="Acme Corp", terms=30)
        company = CompanyConfig(name="TestCo")
        services = {"consulting": ServiceConfig(key="consulting", display_name="Consulting", rate=150, type="hours")}

        invoice = generate_invoice(entries, "", client, company, services, 42, date(2026, 2, 28))

        assert invoice.number == "INV-000042"
        assert invoice.total == 2250.0
        assert len(invoice.items) == 2


class TestGenerateInvoiceHtml:
    def _make_invoice(self):
        return Invoice(
            number="INV-000001",
            date=date(2026, 2, 1),
            due_date=date(2026, 3, 3),
            client=ClientConfig(key="acme", name="Acme Corp", address="456 Oak Ave", email="billing@acme.com"),
            company=CompanyConfig(name="TestCo", address="123 Main St", payment_instructions="Wire to XYZ"),
            items=[
                InvoiceLineItem(display_name="Consulting", description="Architecture review", quantity=10, rate=150, discount=0, amount=1500),
                InvoiceLineItem(display_name="Flight", description="", quantity=1, rate=340.50, discount=0, amount=340.50),
            ],
            total=1840.50,
            group_name="Professional Services",
        )

    def test_html_contains_key_elements(self):
        invoice = self._make_invoice()
        html = generate_invoice_html(invoice)

        assert "INVOICE" in html
        assert "INV-000001" in html
        assert "Acme Corp" in html
        assert "TestCo" in html
        assert "Consulting" in html
        assert "Architecture review" in html
        assert "$1,500.00" in html
        assert "$1,840.50" in html
        assert "Wire to XYZ" in html
        assert "Professional Services" in html

    def test_html_contains_dates(self):
        invoice = self._make_invoice()
        html = generate_invoice_html(invoice)

        assert "February 01, 2026" in html
        assert "March 03, 2026" in html

    def test_html_without_payment_instructions(self):
        invoice = self._make_invoice()
        invoice.company.payment_instructions = ""
        html = generate_invoice_html(invoice)

        assert "Payment Instructions" not in html

    def test_html_without_group_name(self):
        invoice = self._make_invoice()
        invoice.group_name = ""
        html = generate_invoice_html(invoice)

        assert "Reference" not in html

    def test_html_shows_discount_column_when_present(self):
        invoice = self._make_invoice()
        invoice.items[0].discount = 200
        invoice.items[0].amount = 1300
        html = generate_invoice_html(invoice)

        assert "Discount" in html
        assert "$200.00" in html

    def test_html_hides_discount_column_when_zero(self):
        invoice = self._make_invoice()
        html = generate_invoice_html(invoice)

        assert "Discount" not in html

    def test_html_has_summary_footer(self):
        invoice = self._make_invoice()
        html = generate_invoice_html(invoice)

        assert "Net Price" in html
        assert "Amount Due" in html

    def test_html_shows_logo_when_provided(self, tmp_path):
        invoice = self._make_invoice()
        logo_file = tmp_path / "logo.png"
        # 1x1 red PNG
        logo_file.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
            b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
            b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        html = generate_invoice_html(invoice, logo_path=logo_file)

        assert '<img class="company-logo"' in html
        assert "data:image/png;base64," in html
        # Company name text should not appear as header (only in alt attribute)
        assert '<div class="company-name">' not in html

    def test_html_falls_back_to_name_without_logo(self):
        invoice = self._make_invoice()
        html = generate_invoice_html(invoice)

        assert '<div class="company-name">' in html
        assert "TestCo" in html
        assert '<img class="company-logo"' not in html

    def test_html_falls_back_to_name_with_missing_logo(self, tmp_path):
        invoice = self._make_invoice()
        missing = tmp_path / "nonexistent.png"
        html = generate_invoice_html(invoice, logo_path=missing)

        assert '<div class="company-name">' in html
        assert '<img class="company-logo"' not in html

    def test_embed_logo_returns_data_uri(self, tmp_path):
        logo_file = tmp_path / "logo.svg"
        logo_file.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        result = _embed_logo(logo_file)

        assert result.startswith("data:image/svg+xml;base64,")


class TestCreateIncomePosting:
    def test_basic_posting(self):
        posting = create_income_posting(
            invoice_number="INV-000001",
            client_name="Acme Corp",
            income_lines={"Income:Consulting": 1500.00},
            payment_date=date(2026, 2, 15),
        )

        assert '2026-02-15 * "Acme Corp" "Payment for INV-000001"' in posting
        assert "Assets:Bank:Checking  1500.00 USD" in posting
        assert "Income:Consulting  -1500.00 USD" in posting

    def test_multiple_income_accounts(self):
        posting = create_income_posting(
            invoice_number="INV-000002",
            client_name="Acme Corp",
            income_lines={
                "Income:Consulting": 1500.00,
                "Income:Development": 875.00,
            },
            payment_date=date(2026, 2, 15),
        )

        assert "Assets:Bank:Checking  2375.00 USD" in posting
        assert "Income:Consulting  -1500.00 USD" in posting
        assert "Income:Development  -875.00 USD" in posting

    def test_custom_bank_account(self):
        posting = create_income_posting(
            invoice_number="INV-000001",
            client_name="Beta Inc",
            income_lines={"Income:Consulting": 500.00},
            payment_date=date(2026, 3, 1),
            bank_account="Assets:Bank:Savings",
        )

        assert "Assets:Bank:Savings  500.00 USD" in posting

    def test_custom_currency(self):
        posting = create_income_posting(
            invoice_number="INV-000001",
            client_name="Acme Corp",
            income_lines={"Income:Consulting": 1500.00},
            payment_date=date(2026, 2, 15),
            currency="EUR",
        )

        assert "1500.00 EUR" in posting

    def test_client_name_with_quotes(self):
        posting = create_income_posting(
            invoice_number="INV-000001",
            client_name='Company "Best"',
            income_lines={"Income:Services": 100.00},
            payment_date=date(2026, 2, 15),
        )

        assert '\\"Best\\"' in posting


class TestComputeIncomeLines:
    def test_single_service(self):
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", qty=10),
        ]
        services = {"consulting": ServiceConfig(key="consulting", display_name="Consulting Services", rate=150)}

        lines = compute_income_lines(entries, services)

        assert lines == {"Income:Consulting": 1500.00}

    def test_multiple_services(self):
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", qty=10),
            WorkEntry(date=date(2026, 2, 3), client="acme", service="development", qty=5),
        ]
        services = {
            "consulting": ServiceConfig(key="consulting", display_name="Consulting Services", rate=150),
            "development": ServiceConfig(key="development", display_name="Development Services", rate=175),
        }

        lines = compute_income_lines(entries, services)

        assert lines == {"Income:Consulting": 1500.00, "Income:Development": 875.00}

    def test_custom_income_account(self):
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", qty=10),
        ]
        services = {"consulting": ServiceConfig(key="consulting", display_name="Consulting Services", rate=150, income_account="Income:Professional")}

        lines = compute_income_lines(entries, services)

        assert lines == {"Income:Professional": 1500.00}

    def test_aggregates_same_service(self):
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", qty=4),
            WorkEntry(date=date(2026, 2, 3), client="acme", service="consulting", qty=6),
        ]
        services = {"consulting": ServiceConfig(key="consulting", display_name="Consulting Services", rate=150)}

        lines = compute_income_lines(entries, services)

        assert lines == {"Income:Consulting": 1500.00}


class TestUpdateInvoiceNumber:
    def test_update_number(self, tmp_path):
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(SAMPLE_CONFIG_TOML)

        update_invoice_number(config_file, 99)

        updated_text = config_file.read_text()
        assert "next_invoice_number = 99" in updated_text
        # Other content should remain
        assert "TestCo" in updated_text

    def test_update_preserves_content(self, tmp_path):
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(SAMPLE_CONFIG_TOML)

        update_invoice_number(config_file, 100)

        config = parse_invoicing_config(config_file)
        assert config.next_invoice_number == 100
        assert config.company.name == "TestCo"
        assert "acme" in config.clients


class TestGenerateInvoicesForPeriod:
    def _setup_config_and_log(self, tmp_path, monkeypatch=None):
        """Create config, work log, and ledger files. Return config path."""
        config_file = tmp_path / "INVOICING.md"
        # Override paths to use tmp_path
        config_text = SAMPLE_CONFIG_TOML.replace(
            'accounting_path = "/accounting"',
            f'accounting_path = "{tmp_path / "accounting"}"',
        ).replace(
            'work_log = "/notes/_INVOICES.md"',
            f'work_log = "{tmp_path / "_INVOICES.md"}"',
        )
        config_file.write_text(config_text)

        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text(SAMPLE_WORK_LOG)

        # Create a ledger file for posting
        ledger_file = tmp_path / "accounting" / "ledger.beancount"
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        ledger_file.write_text("")
        if monkeypatch:
            monkeypatch.setenv("LEDGER_PATH", str(ledger_file))

        return config_file

    def test_dry_run_no_files_created(self, tmp_path):
        config_file = self._setup_config_and_log(tmp_path)
        config = parse_invoicing_config(config_file)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
            dry_run=True,
        )

        assert len(results) > 0
        # No file key in dry run results
        for r in results:
            assert "file" not in r

        # Invoice number should not be updated
        updated_config = parse_invoicing_config(config_file)
        assert updated_config.next_invoice_number == 42

    @patch("istota.skills.invoicing.generate_invoice_pdf")
    def test_generates_invoices(self, mock_pdf, tmp_path):
        config_file = self._setup_config_and_log(tmp_path)
        config = parse_invoicing_config(config_file)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
        )

        # Should generate invoices for acme (bundled + separate) and beta
        assert len(results) >= 2
        assert mock_pdf.called

        # Invoice numbers should be updated
        updated_config = parse_invoicing_config(config_file)
        assert updated_config.next_invoice_number == 42 + len(results)

    @patch("istota.skills.invoicing.generate_invoice_pdf")
    def test_client_filter(self, mock_pdf, tmp_path):
        config_file = self._setup_config_and_log(tmp_path)
        config = parse_invoicing_config(config_file)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
            client_filter="beta",
        )

        assert len(results) == 1
        assert results[0]["client"] == "Beta Inc"

    def test_no_entries_before_period(self, tmp_path):
        config_file = self._setup_config_and_log(tmp_path)
        config = parse_invoicing_config(config_file)

        # Use a period before any entries exist (all entries are Jan 2026+)
        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2025-06",
        )

        assert results == []

    @patch("istota.skills.invoicing.generate_invoice_pdf")
    def test_no_ledger_entries_at_invoice_time(self, mock_pdf, tmp_path, monkeypatch):
        """Cash-basis: invoice generation should not create any ledger entries."""
        config_file = self._setup_config_and_log(tmp_path, monkeypatch)
        config = parse_invoicing_config(config_file)

        ledger_file = tmp_path / "accounting" / "ledger.beancount"
        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
        )

        assert len(results) > 0
        # Ledger should remain empty (cash-basis: no A/R at invoice time)
        assert ledger_file.read_text() == ""

    def test_missing_work_log_auto_created(self, tmp_path):
        work_log_path = tmp_path / "notes" / "_INVOICES.md"
        config_file = tmp_path / "INVOICING.md"
        config_text = SAMPLE_CONFIG_TOML.replace(
            'work_log = "/notes/_INVOICES.md"',
            f'work_log = "{work_log_path}"',
        )
        config_file.write_text(config_text)
        config = parse_invoicing_config(config_file)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
        )

        # Should return empty (no entries) and create the file
        assert results == []
        assert work_log_path.exists()
        content = work_log_path.read_text()
        assert "Work Log" in content
        assert "[[entries]]" in content


class TestAccountingCLIInvoiceCommands:
    """Test the invoice subcommands in accounting.py CLI."""

    def test_parser_invoice_generate(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args(["invoice", "generate", "--period", "2026-02"])
        assert args.command == "invoice"
        assert args.invoice_command == "generate"
        assert args.period == "2026-02"
        assert args.dry_run is False

    def test_parser_invoice_generate_all_flags(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "invoice", "generate",
            "--period", "2026-02",
            "--client", "acme",
            "--dry-run",
        ])
        assert args.client == "acme"
        assert args.dry_run is True

    def test_parser_invoice_list(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args(["invoice", "list"])
        assert args.invoice_command == "list"

    def test_parser_invoice_list_with_client(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args(["invoice", "list", "--client", "acme"])
        assert args.client == "acme"

    def test_parser_invoice_list_all(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args(["invoice", "list", "--all"])
        assert args.all is True

    def test_parser_invoice_paid(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args(["invoice", "paid", "INV-000001", "--date", "2026-02-15"])
        assert args.invoice_command == "paid"
        assert args.invoice_number == "INV-000001"
        assert args.date == "2026-02-15"

    def test_parser_invoice_paid_with_bank(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "invoice", "paid", "INV-000001",
            "--date", "2026-02-15",
            "--bank", "Assets:Bank:Savings",
        ])
        assert args.bank == "Assets:Bank:Savings"

    def test_parser_invoice_paid_no_post(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "invoice", "paid", "INV-000001",
            "--date", "2026-02-15",
            "--no-post",
        ])
        assert args.no_post is True

    def test_parser_invoice_create(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "invoice", "create", "acme",
            "--service", "consulting",
            "--qty", "40",
        ])
        assert args.invoice_command == "create"
        assert args.client == "acme"
        assert args.service == "consulting"
        assert args.qty == 40.0

    def test_parser_invoice_create_with_items(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "invoice", "create", "acme",
            "--item", '"Travel expenses" 340.50',
            "--item", '"Software licenses" 99.99',
        ])
        assert len(args.items) == 2

    def test_cmd_invoice_generate_missing_config(self, monkeypatch):
        from istota.skills.accounting import cmd_invoice_generate

        monkeypatch.setenv("INVOICING_CONFIG", "/nonexistent/INVOICING.md")
        args = MagicMock()
        args.period = "2026-02"
        args.client = None
        args.dry_run = False

        result = cmd_invoice_generate(args)
        assert result["status"] == "error"
        assert "Config not found" in result["error"]

    def test_cmd_invoice_generate_missing_env(self, monkeypatch):
        from istota.skills.accounting import cmd_invoice_generate

        monkeypatch.delenv("INVOICING_CONFIG", raising=False)
        args = MagicMock()
        args.period = "2026-02"
        args.client = None
        args.dry_run = False

        result = cmd_invoice_generate(args)
        assert result["status"] == "error"

    def test_cmd_invoice_list_outstanding(self, tmp_path, monkeypatch):
        from istota.skills.accounting import cmd_invoice_list

        config_file = tmp_path / "INVOICING.md"
        config_text = SAMPLE_CONFIG_TOML.replace(
            'accounting_path = "/accounting"',
            f'accounting_path = "{tmp_path / "accounting"}"',
        ).replace(
            'work_log = "/notes/_INVOICES.md"',
            f'work_log = "{tmp_path / "_INVOICES.md"}"',
        )
        config_file.write_text(config_text)
        monkeypatch.setenv("INVOICING_CONFIG", str(config_file))

        # Work log with one invoiced entry (outstanding) and one paid
        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text("""\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4
invoice = "INV-000042"

[[entries]]
date = 2026-02-03
client = "beta"
service = "consulting"
qty = 2
invoice = "INV-000043"
paid_date = 2026-02-20
```
""")

        args = MagicMock()
        args.client = None
        args.all = False

        result = cmd_invoice_list(args)

        assert result["status"] == "ok"
        assert result["outstanding_count"] == 1
        assert len(result["invoices"]) == 1
        assert result["invoices"][0]["invoice_number"] == "INV-000042"
        assert result["invoices"][0]["status"] == "outstanding"

    def test_cmd_invoice_list_all(self, tmp_path, monkeypatch):
        from istota.skills.accounting import cmd_invoice_list

        config_file = tmp_path / "INVOICING.md"
        config_text = SAMPLE_CONFIG_TOML.replace(
            'accounting_path = "/accounting"',
            f'accounting_path = "{tmp_path / "accounting"}"',
        ).replace(
            'work_log = "/notes/_INVOICES.md"',
            f'work_log = "{tmp_path / "_INVOICES.md"}"',
        )
        config_file.write_text(config_text)
        monkeypatch.setenv("INVOICING_CONFIG", str(config_file))

        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text("""\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4
invoice = "INV-000042"

[[entries]]
date = 2026-02-03
client = "beta"
service = "consulting"
qty = 2
invoice = "INV-000043"
paid_date = 2026-02-20
```
""")

        args = MagicMock()
        args.client = None
        args.all = True

        result = cmd_invoice_list(args)

        assert result["status"] == "ok"
        assert result["invoice_count"] == 2
        assert result["outstanding_count"] == 1

    @patch("istota.skills.accounting._run_bean_check")
    @patch("istota.skills.accounting._get_ledger_path")
    def test_cmd_invoice_paid(self, mock_ledger_path, mock_check, tmp_path, monkeypatch):
        from istota.skills.accounting import cmd_invoice_paid

        config_file = tmp_path / "INVOICING.md"
        config_text = SAMPLE_CONFIG_TOML.replace(
            'accounting_path = "/accounting"',
            f'accounting_path = "{tmp_path / "accounting"}"',
        ).replace(
            'work_log = "/notes/_INVOICES.md"',
            f'work_log = "{tmp_path / "_INVOICES.md"}"',
        )
        config_file.write_text(config_text)
        monkeypatch.setenv("INVOICING_CONFIG", str(config_file))

        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text("""\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 10
invoice = "INV-000042"
```
""")

        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()
        ledger_file = ledger_dir / "main.beancount"
        ledger_file.write_text("")
        mock_ledger_path.return_value = ledger_file
        mock_check.return_value = (True, [])

        args = MagicMock()
        args.invoice_number = "INV-000042"
        args.date = "2026-02-15"
        args.bank = None
        args.no_post = False

        result = cmd_invoice_paid(args)

        assert result["status"] == "ok"
        assert result["amount"] == 1500.0
        assert result["invoice_number"] == "INV-000042"
        assert result["client"] == "Acme Corp"

        # Verify income posting was appended to ledger
        content = ledger_file.read_text()
        assert "Payment for INV-000042" in content
        assert "Income:Consulting  -1500.00 USD" in content
        assert "Assets:Bank:Checking  1500.00 USD" in content

        # Verify paid_date was stamped in work log
        entries = parse_work_log(log_file)
        assert entries[0].paid_date == date(2026, 2, 15)

    def test_cmd_invoice_paid_not_found(self, tmp_path, monkeypatch):
        from istota.skills.accounting import cmd_invoice_paid

        config_file = tmp_path / "INVOICING.md"
        config_text = SAMPLE_CONFIG_TOML.replace(
            'accounting_path = "/accounting"',
            f'accounting_path = "{tmp_path / "accounting"}"',
        ).replace(
            'work_log = "/notes/_INVOICES.md"',
            f'work_log = "{tmp_path / "_INVOICES.md"}"',
        )
        config_file.write_text(config_text)
        monkeypatch.setenv("INVOICING_CONFIG", str(config_file))

        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text("# Work Log\n\n```toml\n```\n")

        args = MagicMock()
        args.invoice_number = "INV-999999"
        args.date = "2026-02-15"
        args.bank = None
        args.no_post = False

        result = cmd_invoice_paid(args)

        assert result["status"] == "error"
        assert "not found" in result["error"]

    def test_cmd_invoice_paid_invalid_date(self, monkeypatch):
        from istota.skills.accounting import cmd_invoice_paid

        monkeypatch.setenv("INVOICING_CONFIG", "/nonexistent")

        args = MagicMock()
        args.invoice_number = "INV-000001"
        args.date = "invalid-date"
        args.bank = None
        args.no_post = False

        result = cmd_invoice_paid(args)

        assert result["status"] == "error"
        assert "Invalid date" in result["error"]

    def test_cmd_invoice_paid_no_post(self, tmp_path, monkeypatch):
        from istota.skills.accounting import cmd_invoice_paid

        config_file = tmp_path / "INVOICING.md"
        config_text = SAMPLE_CONFIG_TOML.replace(
            'accounting_path = "/accounting"',
            f'accounting_path = "{tmp_path / "accounting"}"',
        ).replace(
            'work_log = "/notes/_INVOICES.md"',
            f'work_log = "{tmp_path / "_INVOICES.md"}"',
        )
        config_file.write_text(config_text)
        monkeypatch.setenv("INVOICING_CONFIG", str(config_file))

        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text("""\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 10
invoice = "INV-000042"
```
""")

        args = MagicMock()
        args.invoice_number = "INV-000042"
        args.date = "2026-02-15"
        args.bank = None
        args.no_post = True

        result = cmd_invoice_paid(args)

        assert result["status"] == "ok"
        assert result["no_post"] is True

        # Verify paid_date was still stamped
        entries = parse_work_log(log_file)
        assert entries[0].paid_date == date(2026, 2, 15)

    def test_cmd_invoice_paid_already_paid(self, tmp_path, monkeypatch):
        from istota.skills.accounting import cmd_invoice_paid

        config_file = tmp_path / "INVOICING.md"
        config_text = SAMPLE_CONFIG_TOML.replace(
            'accounting_path = "/accounting"',
            f'accounting_path = "{tmp_path / "accounting"}"',
        ).replace(
            'work_log = "/notes/_INVOICES.md"',
            f'work_log = "{tmp_path / "_INVOICES.md"}"',
        )
        config_file.write_text(config_text)
        monkeypatch.setenv("INVOICING_CONFIG", str(config_file))

        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text("""\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 10
invoice = "INV-000042"
paid_date = 2026-02-10
```
""")

        args = MagicMock()
        args.invoice_number = "INV-000042"
        args.date = "2026-02-15"
        args.bank = None
        args.no_post = False

        result = cmd_invoice_paid(args)

        assert result["status"] == "error"
        assert "already marked as paid" in result["error"]


class TestCLIMainInvoice:
    """Test main() dispatch for invoice subcommands."""

    @patch("istota.skills.accounting.cmd_invoice_generate")
    def test_main_invoice_generate(self, mock_cmd, capsys):
        from istota.skills.accounting import main

        mock_cmd.return_value = {"status": "ok", "invoices": []}

        main(["invoice", "generate", "--period", "2026-02"])

        mock_cmd.assert_called_once()
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"

    @patch("istota.skills.accounting.cmd_invoice_list")
    def test_main_invoice_list(self, mock_cmd, capsys):
        from istota.skills.accounting import main

        mock_cmd.return_value = {"status": "ok", "receivables": []}

        main(["invoice", "list"])

        mock_cmd.assert_called_once()

    def test_main_invoice_missing_subcommand(self):
        from istota.skills.accounting import main

        with pytest.raises(SystemExit):
            main(["invoice"])


# --- Multi-Entity Tests ---

MULTI_ENTITY_CONFIG_TOML = """\
# Multi-entity invoicing config

```toml
accounting_path = "/accounting"
work_log = "/notes/_INVOICES.md"
invoice_output = "invoices/generated"
next_invoice_number = 1
default_entity = "personal"

[companies.personal]
name = "Jane Doe"
address = "123 Main St"
email = "jane@example.com"
payment_instructions = "Zelle to jane@example.com"
logo = "invoices/assets/logo-personal.png"

[companies.llc]
name = "JD Consulting LLC"
address = "456 Business Blvd"
payment_instructions = "Wire to: ..."
logo = "invoices/assets/logo-llc.png"
ar_account = "Assets:Receivables:LLC"
bank_account = "Assets:Bank:Business"
currency = "EUR"

[clients.acme]
name = "Acme Corp"
address = "789 Oak Ave"
terms = 30
entity = "llc"

[clients.beta]
name = "Beta Inc"
address = "321 Pine St"
terms = 15

[services.consulting]
display_name = "Consulting Services"
rate = 150
type = "hours"

[services.development]
display_name = "Development Services"
rate = 175
type = "hours"
```
"""

MULTI_ENTITY_WORK_LOG = """\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4
description = "Architecture review"

[[entries]]
date = 2026-02-02
client = "acme"
service = "consulting"
qty = 2
entity = "personal"
description = "Side project"

[[entries]]
date = 2026-02-03
client = "beta"
service = "development"
qty = 8
description = "Feature work"
```
"""


class TestMultiEntityConfigParsing:
    def test_parse_companies(self, tmp_path):
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(MULTI_ENTITY_CONFIG_TOML)

        config = parse_invoicing_config(config_file)

        assert "personal" in config.companies
        assert "llc" in config.companies
        assert len(config.companies) == 2

    def test_default_entity(self, tmp_path):
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(MULTI_ENTITY_CONFIG_TOML)

        config = parse_invoicing_config(config_file)

        assert config.default_entity == "personal"
        assert config.company.name == "Jane Doe"
        assert config.company.key == "personal"

    def test_entity_fields(self, tmp_path):
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(MULTI_ENTITY_CONFIG_TOML)

        config = parse_invoicing_config(config_file)

        llc = config.companies["llc"]
        assert llc.key == "llc"
        assert llc.name == "JD Consulting LLC"
        assert llc.ar_account == "Assets:Receivables:LLC"
        assert llc.bank_account == "Assets:Bank:Business"
        assert llc.currency == "EUR"

    def test_client_entity_field(self, tmp_path):
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(MULTI_ENTITY_CONFIG_TOML)

        config = parse_invoicing_config(config_file)

        assert config.clients["acme"].entity == "llc"
        assert config.clients["beta"].entity == ""

    def test_backward_compat_single_company(self, tmp_path):
        """Single [company] should be wrapped as 'default' entity."""
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(SAMPLE_CONFIG_TOML)

        config = parse_invoicing_config(config_file)

        assert "default" in config.companies
        assert config.default_entity == "default"
        assert config.company.name == "TestCo"
        assert config.company.key == "default"
        assert config.companies["default"].name == "TestCo"

    def test_default_entity_defaults_to_first_key(self, tmp_path):
        """When default_entity not set, uses first key."""
        toml = """\
# Config

```toml
accounting_path = "/accounting"
work_log = "/notes/_INVOICES.md"
next_invoice_number = 1

[companies.alpha]
name = "Alpha Co"

[companies.bravo]
name = "Bravo Co"
```
"""
        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(toml)

        config = parse_invoicing_config(config_file)

        assert config.default_entity == "alpha"
        assert config.company.name == "Alpha Co"


class TestEntityResolution:
    def _make_config(self):
        personal = CompanyConfig(name="Jane Doe", key="personal")
        llc = CompanyConfig(
            name="JD Consulting LLC", key="llc",
            ar_account="Assets:Receivables:LLC",
            bank_account="Assets:Bank:Business",
            currency="EUR",
        )
        return InvoicingConfig(
            accounting_path="/accounting",
            work_log="/log",
            invoice_output="invoices",
            next_invoice_number=1,
            company=personal,
            clients={
                "acme": ClientConfig(key="acme", name="Acme Corp", entity="llc"),
                "beta": ClientConfig(key="beta", name="Beta Inc"),
            },
            services={},
            companies={"personal": personal, "llc": llc},
            default_entity="personal",
        )

    def test_resolve_entity_from_entry(self):
        config = self._make_config()
        entry = WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", entity="personal")
        client = config.clients["acme"]

        entity = resolve_entity(config, entry=entry, client_config=client)
        assert entity.key == "personal"

    def test_resolve_entity_from_client(self):
        config = self._make_config()
        entry = WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting")
        client = config.clients["acme"]

        entity = resolve_entity(config, entry=entry, client_config=client)
        assert entity.key == "llc"

    def test_resolve_entity_from_default(self):
        config = self._make_config()
        entry = WorkEntry(date=date(2026, 2, 1), client="beta", service="consulting")
        client = config.clients["beta"]

        entity = resolve_entity(config, entry=entry, client_config=client)
        assert entity.key == "personal"

    def test_resolve_entity_entry_overrides_client(self):
        config = self._make_config()
        entry = WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", entity="personal")
        client = config.clients["acme"]  # has entity="llc"

        entity = resolve_entity(config, entry=entry, client_config=client)
        assert entity.key == "personal"

    def test_resolve_bank_account_from_entity(self):
        config = self._make_config()
        llc = config.companies["llc"]

        assert resolve_bank_account(llc, config) == "Assets:Bank:Business"

    def test_resolve_bank_account_from_config_default(self):
        config = self._make_config()
        personal = config.companies["personal"]

        assert resolve_bank_account(personal, config) == "Assets:Bank:Checking"

    def test_resolve_currency_from_entity(self):
        config = self._make_config()
        llc = config.companies["llc"]

        assert resolve_currency(llc, config) == "EUR"

    def test_resolve_currency_from_config_default(self):
        config = self._make_config()
        personal = config.companies["personal"]

        assert resolve_currency(personal, config) == "USD"


class TestWorkLogEntityField:
    def test_parse_entity_field(self, tmp_path):
        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text(MULTI_ENTITY_WORK_LOG)

        entries = parse_work_log(log_file)

        assert entries[0].entity == ""  # no entity set
        assert entries[1].entity == "personal"  # explicit override
        assert entries[2].entity == ""

    def test_entity_field_empty_by_default(self, tmp_path):
        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text(SAMPLE_WORK_LOG)

        entries = parse_work_log(log_file)
        assert all(e.entity == "" for e in entries)


class TestMultiEntityInvoiceGeneration:
    def _setup(self, tmp_path):
        config_file = tmp_path / "INVOICING.md"
        config_text = MULTI_ENTITY_CONFIG_TOML.replace(
            'accounting_path = "/accounting"',
            f'accounting_path = "{tmp_path / "accounting"}"',
        ).replace(
            'work_log = "/notes/_INVOICES.md"',
            f'work_log = "{tmp_path / "_INVOICES.md"}"',
        )
        config_file.write_text(config_text)

        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text(MULTI_ENTITY_WORK_LOG)

        ledger_file = tmp_path / "accounting" / "ledger.beancount"
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        ledger_file.write_text("")

        return config_file

    @patch("istota.skills.invoicing.generate_invoice_pdf")
    def test_entries_grouped_by_entity(self, mock_pdf, tmp_path, monkeypatch):
        config_file = self._setup(tmp_path)
        monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "accounting" / "ledger.beancount"))
        config = parse_invoicing_config(config_file)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
        )

        # acme has 2 entities: llc (4h) and personal (2h), beta has personal (8h)
        assert len(results) == 3
        clients_entities = [(r["client"], r.get("entity")) for r in results]
        assert ("Acme Corp", "llc") in clients_entities
        assert ("Acme Corp", "personal") in clients_entities
        assert ("Beta Inc", "personal") in clients_entities

    @patch("istota.skills.invoicing.generate_invoice_pdf")
    def test_entity_filter(self, mock_pdf, tmp_path):
        config_file = self._setup(tmp_path)
        config = parse_invoicing_config(config_file)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
            entity_filter="llc",
        )

        # Only acme's llc entries
        assert len(results) == 1
        assert results[0]["client"] == "Acme Corp"
        assert results[0]["entity"] == "llc"

    @patch("istota.skills.invoicing.generate_invoice_pdf")
    def test_entity_filter_no_match(self, mock_pdf, tmp_path):
        config_file = self._setup(tmp_path)
        config = parse_invoicing_config(config_file)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
            entity_filter="nonexistent",
        )

        assert results == []

    @patch("istota.skills.invoicing.generate_invoice_pdf")
    def test_per_entity_logo_resolution(self, mock_pdf, tmp_path):
        config_file = self._setup(tmp_path)
        config = parse_invoicing_config(config_file)

        # Create logo files for both entities
        acct_path = tmp_path / "accounting"
        personal_logo = acct_path / "invoices" / "assets" / "logo-personal.png"
        llc_logo = acct_path / "invoices" / "assets" / "logo-llc.png"
        personal_logo.parent.mkdir(parents=True, exist_ok=True)
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
            b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
            b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        personal_logo.write_bytes(png_bytes)
        llc_logo.write_bytes(png_bytes)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
        )

        # Should have called generate_invoice_pdf 3 times
        assert mock_pdf.call_count == 3

        # Each call's HTML should contain the correct entity's logo
        for call in mock_pdf.call_args_list:
            html = call[0][0]
            assert "data:image/png;base64," in html

    @patch("istota.skills.invoicing.generate_invoice_pdf")
    def test_no_ledger_entries_multi_entity(self, mock_pdf, tmp_path, monkeypatch):
        """Cash-basis: multi-entity invoice generation should not create any ledger entries."""
        config_file = self._setup(tmp_path)
        ledger_file = tmp_path / "accounting" / "ledger.beancount"
        monkeypatch.setenv("LEDGER_PATH", str(ledger_file))
        config = parse_invoicing_config(config_file)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
        )

        assert len(results) == 3
        # Ledger should remain empty (cash-basis)
        assert ledger_file.read_text() == ""

    def test_dry_run_multi_entity(self, tmp_path):
        config_file = self._setup(tmp_path)
        config = parse_invoicing_config(config_file)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
            dry_run=True,
        )

        assert len(results) == 3
        for r in results:
            assert "file" not in r

    @patch("istota.skills.invoicing.generate_invoice_pdf")
    def test_invoice_company_matches_entity(self, mock_pdf, tmp_path):
        """Each invoice should use the correct entity as company."""
        config_file = self._setup(tmp_path)
        config = parse_invoicing_config(config_file)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
        )

        # Check the HTML content passed to generate_invoice_pdf
        for call in mock_pdf.call_args_list:
            html = call[0][0]
            # Every invoice should contain one of the entity names
            assert "Jane Doe" in html or "JD Consulting LLC" in html


class TestMultiEntityIncomePosting:
    def test_income_posting_with_entity_currency(self):
        """Entity currency should be used in income postings."""
        posting = create_income_posting(
            invoice_number="INV-000001",
            client_name="Acme Corp",
            income_lines={"Income:Consulting": 1500.00},
            payment_date=date(2026, 2, 15),
            bank_account="Assets:Bank:Business",
            currency="EUR",
        )

        assert "Assets:Bank:Business  1500.00 EUR" in posting
        assert "Income:Consulting  -1500.00 EUR" in posting


class TestMultiEntityCLI:
    def test_parser_invoice_generate_entity_flag(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "invoice", "generate",
            "--period", "2026-02",
            "--entity", "llc",
        ])
        assert args.entity == "llc"

    def test_parser_invoice_generate_entity_short_flag(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "invoice", "generate",
            "--period", "2026-02",
            "-e", "personal",
        ])
        assert args.entity == "personal"

    def test_parser_invoice_create_entity_flag(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "invoice", "create", "acme",
            "--service", "consulting",
            "--qty", "10",
            "--entity", "llc",
        ])
        assert args.entity == "llc"

    def test_cmd_invoice_create_invalid_entity(self, tmp_path, monkeypatch):
        from istota.skills.accounting import cmd_invoice_create

        config_file = tmp_path / "INVOICING.md"
        config_file.write_text(MULTI_ENTITY_CONFIG_TOML.replace(
            'accounting_path = "/accounting"',
            f'accounting_path = "{tmp_path / "accounting"}"',
        ))
        monkeypatch.setenv("INVOICING_CONFIG", str(config_file))

        args = MagicMock()
        args.client = "acme"
        args.service = "consulting"
        args.qty = 10.0
        args.description = ""
        args.items = None
        args.entity = "nonexistent"

        result = cmd_invoice_create(args)
        assert result["status"] == "error"
        assert "nonexistent" in result["error"]


# --- Uninvoiced Entry Selection & Stamping Tests ---

WORK_LOG_WITH_INVOICED = """\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4
description = "Architecture review"

[[entries]]
date = 2026-02-03
client = "acme"
service = "development"
qty = 8
description = "Feature implementation"
invoice = "INV-000042"

[[entries]]
date = 2026-02-05
client = "acme"
service = "expenses"
amount = 340.50
description = "Flight to NYC"

[[entries]]
date = 2026-02-10
client = "beta"
service = "consulting"
qty = 2
description = "Strategy call"

[[entries]]
date = 2026-01-15
client = "acme"
service = "consulting"
qty = 6
description = "January work"
```
"""


class TestWorkEntryInvoiceField:
    def test_parse_invoice_field(self, tmp_path):
        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text(WORK_LOG_WITH_INVOICED)

        entries = parse_work_log(log_file)

        assert entries[0].invoice == ""
        assert entries[1].invoice == "INV-000042"
        assert entries[2].invoice == ""

    def test_invoice_field_default_empty(self):
        entry = WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting")
        assert entry.invoice == ""

    def test_invoice_field_set(self):
        entry = WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", invoice="INV-000001")
        assert entry.invoice == "INV-000001"


class TestWorkEntryPaidDate:
    def test_paid_date_default_none(self):
        entry = WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting")
        assert entry.paid_date is None

    def test_paid_date_set(self):
        entry = WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", paid_date=date(2026, 2, 15))
        assert entry.paid_date == date(2026, 2, 15)

    def test_parse_paid_date_from_work_log(self, tmp_path):
        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text("""\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4
invoice = "INV-000042"
paid_date = 2026-02-15

[[entries]]
date = 2026-02-03
client = "beta"
service = "consulting"
qty = 2
invoice = "INV-000043"
```
""")

        entries = parse_work_log(log_file)

        assert entries[0].paid_date == date(2026, 2, 15)
        assert entries[1].paid_date is None

    def test_parse_entries_without_paid_date(self, tmp_path):
        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text(SAMPLE_WORK_LOG)

        entries = parse_work_log(log_file)
        assert all(e.paid_date is None for e in entries)


class TestStampPaidDates:
    def test_stamp_single_entry(self, tmp_path):
        work_log = tmp_path / "_INVOICES.md"
        work_log.write_text("""\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4
invoice = "INV-000042"
```
""")

        stamp_work_log_paid_dates(work_log, {0: "2026-02-15"})

        entries = parse_work_log(work_log)
        assert entries[0].paid_date == date(2026, 2, 15)

    def test_stamp_multiple_entries(self, tmp_path):
        work_log = tmp_path / "_INVOICES.md"
        work_log.write_text("""\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4
invoice = "INV-000042"

[[entries]]
date = 2026-02-03
client = "acme"
service = "development"
qty = 8
invoice = "INV-000042"

[[entries]]
date = 2026-02-10
client = "beta"
service = "consulting"
qty = 2
invoice = "INV-000043"
```
""")

        stamp_work_log_paid_dates(work_log, {0: "2026-02-15", 1: "2026-02-15"})

        entries = parse_work_log(work_log)
        assert entries[0].paid_date == date(2026, 2, 15)
        assert entries[1].paid_date == date(2026, 2, 15)
        assert entries[2].paid_date is None

    def test_preserves_existing_fields(self, tmp_path):
        work_log = tmp_path / "_INVOICES.md"
        work_log.write_text("""\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4
invoice = "INV-000042"
```
""")

        stamp_work_log_paid_dates(work_log, {0: "2026-02-15"})

        entries = parse_work_log(work_log)
        assert entries[0].invoice == "INV-000042"
        assert entries[0].paid_date == date(2026, 2, 15)
        assert entries[0].qty == 4


class TestSelectUninvoicedEntries:
    def _make_entries(self):
        return [
            WorkEntry(date=date(2026, 1, 15), client="acme", service="consulting", qty=6),
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", qty=4),
            WorkEntry(date=date(2026, 2, 3), client="acme", service="development", qty=8, invoice="INV-000042"),
            WorkEntry(date=date(2026, 2, 5), client="acme", service="expenses", amount=340.50),
            WorkEntry(date=date(2026, 2, 10), client="beta", service="consulting", qty=2),
            WorkEntry(date=date(2026, 3, 5), client="acme", service="consulting", qty=3),
        ]

    def test_skips_invoiced_entries(self):
        entries = self._make_entries()
        result = select_uninvoiced_entries(entries)
        # Entry at index 2 has invoice set, should be skipped
        indices = [idx for idx, _ in result]
        assert 2 not in indices
        assert len(result) == 5

    def test_returns_indices(self):
        entries = self._make_entries()
        result = select_uninvoiced_entries(entries)
        indices = [idx for idx, _ in result]
        assert indices == [0, 1, 3, 4, 5]

    def test_no_period_returns_all_uninvoiced(self):
        entries = self._make_entries()
        result = select_uninvoiced_entries(entries, period=None)
        assert len(result) == 5

    def test_period_as_upper_bound(self):
        entries = self._make_entries()
        result = select_uninvoiced_entries(entries, period="2026-02")
        # Should include Jan and Feb entries, but not March
        indices = [idx for idx, _ in result]
        assert 0 in indices  # Jan 15
        assert 1 in indices  # Feb 1
        assert 3 in indices  # Feb 5
        assert 4 in indices  # Feb 10
        assert 5 not in indices  # Mar 5
        assert len(result) == 4

    def test_period_december_upper_bound(self):
        entries = [
            WorkEntry(date=date(2026, 12, 15), client="acme", service="consulting", qty=4),
            WorkEntry(date=date(2027, 1, 5), client="acme", service="consulting", qty=2),
        ]
        result = select_uninvoiced_entries(entries, period="2026-12")
        assert len(result) == 1
        assert result[0][0] == 0

    def test_client_filter(self):
        entries = self._make_entries()
        result = select_uninvoiced_entries(entries, client="beta")
        assert len(result) == 1
        assert result[0][1].client == "beta"

    def test_period_and_client_combined(self):
        entries = self._make_entries()
        result = select_uninvoiced_entries(entries, period="2026-02", client="acme")
        # Jan acme + Feb acme (minus invoiced)
        indices = [idx for idx, _ in result]
        assert all(entries[i].client == "acme" for i in indices)
        assert 4 not in indices  # beta entry
        assert len(result) == 3

    def test_all_invoiced_returns_empty(self):
        entries = [
            WorkEntry(date=date(2026, 2, 1), client="acme", service="consulting", qty=4, invoice="INV-000001"),
            WorkEntry(date=date(2026, 2, 3), client="acme", service="development", qty=8, invoice="INV-000002"),
        ]
        result = select_uninvoiced_entries(entries)
        assert result == []

    def test_empty_entries(self):
        result = select_uninvoiced_entries([])
        assert result == []


class TestStampWorkLogEntries:
    def test_stamp_single_entry(self, tmp_path):
        work_log = tmp_path / "_INVOICES.md"
        work_log.write_text("""\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4
description = "Architecture review"
```
""")

        stamp_work_log_entries(work_log, {0: "INV-000042"})

        text = work_log.read_text()
        assert 'invoice = "INV-000042"' in text

        # Verify re-parsing works
        entries = parse_work_log(work_log)
        assert entries[0].invoice == "INV-000042"

    def test_stamp_multiple_entries(self, tmp_path):
        work_log = tmp_path / "_INVOICES.md"
        work_log.write_text("""\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4

[[entries]]
date = 2026-02-03
client = "acme"
service = "development"
qty = 8

[[entries]]
date = 2026-02-10
client = "beta"
service = "consulting"
qty = 2
```
""")

        stamp_work_log_entries(work_log, {0: "INV-000042", 2: "INV-000043"})

        entries = parse_work_log(work_log)
        assert entries[0].invoice == "INV-000042"
        assert entries[1].invoice == ""
        assert entries[2].invoice == "INV-000043"

    def test_preserves_markdown(self, tmp_path):
        work_log = tmp_path / "_INVOICES.md"
        content = """\
# Work Log

Some descriptive text here.

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4
```
"""
        work_log.write_text(content)

        stamp_work_log_entries(work_log, {0: "INV-000042"})

        text = work_log.read_text()
        assert "# Work Log" in text
        assert "Some descriptive text here." in text

    def test_stamp_last_entry_before_fence(self, tmp_path):
        work_log = tmp_path / "_INVOICES.md"
        work_log.write_text("""\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4

[[entries]]
date = 2026-02-03
client = "beta"
service = "development"
qty = 8
```
""")

        stamp_work_log_entries(work_log, {1: "INV-000042"})

        entries = parse_work_log(work_log)
        assert entries[0].invoice == ""
        assert entries[1].invoice == "INV-000042"

    def test_empty_stamps_is_noop(self, tmp_path):
        work_log = tmp_path / "_INVOICES.md"
        original = """\
# Work Log

```toml
[[entries]]
date = 2026-02-01
client = "acme"
service = "consulting"
qty = 4
```
"""
        work_log.write_text(original)

        stamp_work_log_entries(work_log, {})

        assert work_log.read_text() == original

    def test_reparse_after_stamp_shows_field(self, tmp_path):
        work_log = tmp_path / "_INVOICES.md"
        work_log.write_text(SAMPLE_WORK_LOG)

        stamp_work_log_entries(work_log, {0: "INV-000042", 1: "INV-000042", 2: "INV-000042"})

        entries = parse_work_log(work_log)
        assert entries[0].invoice == "INV-000042"
        assert entries[1].invoice == "INV-000042"
        assert entries[2].invoice == "INV-000042"
        assert entries[3].invoice == ""
        assert entries[4].invoice == ""

    def test_stamp_preserves_existing_invoice_fields(self, tmp_path):
        work_log = tmp_path / "_INVOICES.md"
        work_log.write_text(WORK_LOG_WITH_INVOICED)

        # Stamp the first entry (index 0), which doesn't have invoice yet
        stamp_work_log_entries(work_log, {0: "INV-000099"})

        entries = parse_work_log(work_log)
        assert entries[0].invoice == "INV-000099"
        assert entries[1].invoice == "INV-000042"  # Existing stamp preserved


class TestGenerateInvoicesStamping:
    def _setup(self, tmp_path, work_log_content=None):
        config_file = tmp_path / "INVOICING.md"
        config_text = SAMPLE_CONFIG_TOML.replace(
            'accounting_path = "/accounting"',
            f'accounting_path = "{tmp_path / "accounting"}"',
        ).replace(
            'work_log = "/notes/_INVOICES.md"',
            f'work_log = "{tmp_path / "_INVOICES.md"}"',
        )
        config_file.write_text(config_text)

        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text(work_log_content or SAMPLE_WORK_LOG)

        ledger_file = tmp_path / "accounting" / "ledger.beancount"
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        ledger_file.write_text("")

        return config_file

    @patch("istota.skills.invoicing.generate_invoice_pdf")
    def test_stamps_after_generation(self, mock_pdf, tmp_path, monkeypatch):
        config_file = self._setup(tmp_path)
        monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "accounting" / "ledger.beancount"))
        config = parse_invoicing_config(config_file)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
        )

        assert len(results) > 0

        # Work log should have stamps
        log_file = tmp_path / "_INVOICES.md"
        entries = parse_work_log(log_file)

        # Feb entries should be stamped
        feb_entries = [e for e in entries if e.date.month == 2]
        assert all(e.invoice != "" for e in feb_entries)

        # Jan entry is also uninvoiced and included (period is upper bound)
        jan_entries = [e for e in entries if e.date.month == 1]
        assert all(e.invoice != "" for e in jan_entries)

    def test_dry_run_does_not_stamp(self, tmp_path):
        config_file = self._setup(tmp_path)
        config = parse_invoicing_config(config_file)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
            dry_run=True,
        )

        assert len(results) > 0

        # Work log should NOT have stamps
        log_file = tmp_path / "_INVOICES.md"
        entries = parse_work_log(log_file)
        assert all(e.invoice == "" for e in entries)

    @patch("istota.skills.invoicing.generate_invoice_pdf")
    def test_rerun_skips_stamped_entries(self, mock_pdf, tmp_path, monkeypatch):
        config_file = self._setup(tmp_path)
        monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "accounting" / "ledger.beancount"))
        config = parse_invoicing_config(config_file)

        # First run
        results1 = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
        )
        assert len(results1) > 0

        # Re-parse config to get updated invoice number
        config2 = parse_invoicing_config(config_file)

        # Second run — should find no uninvoiced entries for Feb
        results2 = generate_invoices_for_period(
            config=config2,
            config_path=config_file,
            period="2026-02",
        )
        assert results2 == []

    @patch("istota.skills.invoicing.generate_invoice_pdf")
    def test_period_optional_all_uninvoiced(self, mock_pdf, tmp_path, monkeypatch):
        config_file = self._setup(tmp_path)
        monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "accounting" / "ledger.beancount"))
        config = parse_invoicing_config(config_file)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period=None,
        )

        # Should include all entries (Jan + Feb)
        assert len(results) > 0

        # All entries should be stamped
        log_file = tmp_path / "_INVOICES.md"
        entries = parse_work_log(log_file)
        assert all(e.invoice != "" for e in entries)

    @patch("istota.skills.invoicing.generate_invoice_pdf")
    def test_backward_compat_no_invoice_fields(self, mock_pdf, tmp_path, monkeypatch):
        """Entries without invoice field should be treated as uninvoiced."""
        config_file = self._setup(tmp_path)
        monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "accounting" / "ledger.beancount"))
        config = parse_invoicing_config(config_file)

        results = generate_invoices_for_period(
            config=config,
            config_path=config_file,
            period="2026-02",
        )

        assert len(results) > 0


class TestCLIPeriodOptional:
    def test_period_not_required(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args(["invoice", "generate"])
        assert args.period is None

    def test_period_default_none(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args(["invoice", "generate"])
        assert args.period is None
        assert args.command == "invoice"
        assert args.invoice_command == "generate"

    def test_period_still_works_when_provided(self):
        from istota.skills.accounting import build_parser

        parser = build_parser()
        args = parser.parse_args(["invoice", "generate", "--period", "2026-02"])
        assert args.period == "2026-02"

    def test_cmd_generate_no_period_no_entries(self, tmp_path, monkeypatch):
        from istota.skills.accounting import cmd_invoice_generate

        config_file = tmp_path / "INVOICING.md"
        config_text = SAMPLE_CONFIG_TOML.replace(
            'accounting_path = "/accounting"',
            f'accounting_path = "{tmp_path / "accounting"}"',
        ).replace(
            'work_log = "/notes/_INVOICES.md"',
            f'work_log = "{tmp_path / "_INVOICES.md"}"',
        )
        config_file.write_text(config_text)

        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text("# Work Log\n\n```toml\n```\n")

        monkeypatch.setenv("INVOICING_CONFIG", str(config_file))

        args = MagicMock()
        args.period = None
        args.client = None
        args.entity = None
        args.dry_run = False

        result = cmd_invoice_generate(args)
        assert result["status"] == "ok"
        assert "uninvoiced" in result["message"].lower()
        assert "period" not in result

    @patch("istota.skills.invoicing.generate_invoice_pdf")
    def test_cmd_generate_no_period_with_entries(self, mock_pdf, tmp_path, monkeypatch):
        from istota.skills.accounting import cmd_invoice_generate

        config_file = tmp_path / "INVOICING.md"
        config_text = SAMPLE_CONFIG_TOML.replace(
            'accounting_path = "/accounting"',
            f'accounting_path = "{tmp_path / "accounting"}"',
        ).replace(
            'work_log = "/notes/_INVOICES.md"',
            f'work_log = "{tmp_path / "_INVOICES.md"}"',
        )
        config_file.write_text(config_text)

        log_file = tmp_path / "_INVOICES.md"
        log_file.write_text(SAMPLE_WORK_LOG)

        ledger_file = tmp_path / "accounting" / "ledger.beancount"
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        ledger_file.write_text("")
        monkeypatch.setenv("INVOICING_CONFIG", str(config_file))
        monkeypatch.setenv("LEDGER_PATH", str(ledger_file))

        args = MagicMock()
        args.period = None
        args.client = None
        args.entity = None
        args.dry_run = False

        result = cmd_invoice_generate(args)
        assert result["status"] == "ok"
        assert result["invoice_count"] > 0
        assert "period" not in result
