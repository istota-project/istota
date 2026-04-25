"""Tests for money.work module."""

from datetime import date

import pytest

from istota.money.work import (
    add_work_entry,
    assign_invoice_number,
    get_entries_for_invoice,
    get_invoice_numbers,
    get_uninvoiced_entries,
    list_work_entries,
    load_work_entries,
    record_invoice_payment,
    remove_work_entry,
    update_work_entry,
    void_invoice,
)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


class TestLoadAndAdd:
    def test_empty(self, data_dir):
        entries = load_work_entries(data_dir)
        assert entries == []

    def test_add_and_load(self, data_dir):
        idx = add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8, description="Coding")
        assert idx == 1
        entries = load_work_entries(data_dir)
        assert len(entries) == 1
        e = entries[0]
        assert e.id == 1
        assert e.date == date(2026, 3, 1)
        assert e.client == "acme"
        assert e.service == "dev"
        assert e.qty == 8
        assert e.description == "Coding"
        assert e.invoice == ""
        assert e.paid_date is None

    def test_add_multiple_sorted_by_date(self, data_dir):
        add_work_entry(data_dir, "2026-03-15", "acme", "dev", qty=4)
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        entries = load_work_entries(data_dir)
        assert len(entries) == 2
        assert entries[0].date == date(2026, 3, 1)
        assert entries[1].date == date(2026, 3, 15)
        assert entries[0].id == 1
        assert entries[1].id == 2

    def test_year_partitioning(self, data_dir):
        add_work_entry(data_dir, "2025-12-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-01-15", "acme", "dev", qty=4)
        work_dir = data_dir / "invoices" / "work"
        assert (work_dir / "2025.toml").exists()
        assert (work_dir / "2026.toml").exists()

    def test_add_with_invoice(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8, invoice="INV-000001")
        entries = load_work_entries(data_dir)
        assert entries[0].invoice == "INV-000001"

    def test_add_all_fields(self, data_dir):
        add_work_entry(
            data_dir, "2026-03-01", "acme", "dev",
            qty=2.5, amount=375.0, discount=50, description="Work",
            entity="llc", invoice="INV-001",
        )
        e = load_work_entries(data_dir)[0]
        assert e.qty == 2.5
        assert e.amount == 375.0
        assert e.discount == 50
        assert e.description == "Work"
        assert e.entity == "llc"
        assert e.invoice == "INV-001"


class TestListFilters:
    def test_list_all(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "beta", "dev", qty=4)
        assert len(list_work_entries(data_dir)) == 2

    def test_list_by_client(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "beta", "dev", qty=4)
        entries = list_work_entries(data_dir, client="acme")
        assert len(entries) == 1
        assert entries[0].client == "acme"

    def test_list_by_period(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-04-01", "acme", "dev", qty=4)
        entries = list_work_entries(data_dir, period="2026-03")
        assert len(entries) == 1

    def test_list_invoiced_filter(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1], "INV-000001")
        assert len(list_work_entries(data_dir, invoiced=False)) == 1
        assert len(list_work_entries(data_dir, invoiced=True)) == 1


class TestUpdate:
    def test_update(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert update_work_entry(data_dir, 1, qty=10, description="Updated") is True
        e = load_work_entries(data_dir)[0]
        assert e.qty == 10
        assert e.description == "Updated"

    def test_update_nonexistent(self, data_dir):
        assert update_work_entry(data_dir, 99, qty=10) is False

    def test_update_no_fields(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert update_work_entry(data_dir, 1) is False

    def test_update_invoiced_blocked(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        assert update_work_entry(data_dir, 1, qty=10) is False

    def test_update_date_string(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        update_work_entry(data_dir, 1, date="2026-04-01")
        e = load_work_entries(data_dir)[0]
        assert e.date == date(2026, 4, 1)


class TestRemove:
    def test_remove_uninvoiced(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert remove_work_entry(data_dir, 1) is True
        assert load_work_entries(data_dir) == []

    def test_remove_invoiced_blocked(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        assert remove_work_entry(data_dir, 1) is False

    def test_remove_nonexistent(self, data_dir):
        assert remove_work_entry(data_dir, 99) is False

    def test_remove_cleans_empty_year_file(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        year_file = data_dir / "invoices" / "work" / "2026.toml"
        assert year_file.exists()
        remove_work_entry(data_dir, 1)
        assert not year_file.exists()


class TestUninvoiced:
    def test_get_uninvoiced(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-15", "acme", "dev", qty=4)
        add_work_entry(data_dir, "2026-04-01", "beta", "dev", qty=6)
        assign_invoice_number(data_dir, [1], "INV-000001")
        entries = get_uninvoiced_entries(data_dir)
        assert len(entries) == 2

    def test_get_uninvoiced_with_period(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-04-01", "acme", "dev", qty=4)
        entries = get_uninvoiced_entries(data_dir, period="2026-03")
        assert len(entries) == 1
        assert entries[0].date == date(2026, 3, 1)

    def test_get_uninvoiced_with_client(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "beta", "dev", qty=4)
        entries = get_uninvoiced_entries(data_dir, client="beta")
        assert len(entries) == 1
        assert entries[0].client == "beta"


class TestInvoiceAssignment:
    def test_assign_and_list(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        count = assign_invoice_number(data_dir, [1, 2], "INV-000001")
        assert count == 2
        entries = get_entries_for_invoice(data_dir, "INV-000001")
        assert len(entries) == 2
        assert all(e.invoice == "INV-000001" for e in entries)

    def test_assign_skips_already_invoiced(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        count = assign_invoice_number(data_dir, [1], "INV-000002")
        assert count == 0

    def test_get_invoice_numbers(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1], "INV-000001")
        assign_invoice_number(data_dir, [2], "INV-000002")
        numbers = get_invoice_numbers(data_dir)
        assert numbers == ["INV-000001", "INV-000002"]

    def test_record_payment(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1, 2], "INV-000001")
        count = record_invoice_payment(data_dir, "INV-000001", "2026-04-15")
        assert count == 2
        entries = get_entries_for_invoice(data_dir, "INV-000001")
        assert all(e.paid_date == date(2026, 4, 15) for e in entries)

    def test_payment_idempotent(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        record_invoice_payment(data_dir, "INV-000001", "2026-04-15")
        count = record_invoice_payment(data_dir, "INV-000001", "2026-05-01")
        assert count == 0


class TestVoidInvoice:
    def test_void_clears_invoice_and_paid_date(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1, 2], "INV-000001")
        record_invoice_payment(data_dir, "INV-000001", "2026-04-15")

        count = void_invoice(data_dir, "INV-000001")
        assert count == 2

        # Entries should now be uninvoiced and unpaid
        entries = load_work_entries(data_dir)
        assert all(e.invoice == "" for e in entries)
        assert all(e.paid_date is None for e in entries)

    def test_void_unpaid_invoice(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")

        count = void_invoice(data_dir, "INV-000001")
        assert count == 1
        entries = load_work_entries(data_dir)
        assert entries[0].invoice == ""

    def test_void_nonexistent_invoice(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        count = void_invoice(data_dir, "INV-999999")
        assert count == 0

    def test_void_does_not_affect_other_invoices(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1], "INV-000001")
        assign_invoice_number(data_dir, [2], "INV-000002")

        void_invoice(data_dir, "INV-000001")

        entries = load_work_entries(data_dir)
        assert entries[0].invoice == ""
        assert entries[1].invoice == "INV-000002"

    def test_void_entries_become_reinvoiceable(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")

        void_invoice(data_dir, "INV-000001")

        # Should now appear as uninvoiced
        uninvoiced = get_uninvoiced_entries(data_dir)
        assert len(uninvoiced) == 1

        # Should be assignable to a new invoice
        count = assign_invoice_number(data_dir, [1], "INV-000002")
        assert count == 1
        entries = get_entries_for_invoice(data_dir, "INV-000002")
        assert len(entries) == 1

    def test_void_removes_from_invoice_numbers_list(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")

        assert "INV-000001" in get_invoice_numbers(data_dir)
        void_invoice(data_dir, "INV-000001")
        assert "INV-000001" not in get_invoice_numbers(data_dir)


class TestClientCaseNormalization:
    def test_add_normalizes_client_to_lowercase(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "SOSF", "dev", qty=8)
        entries = load_work_entries(data_dir)
        assert entries[0].client == "sosf"

    def test_add_mixed_case_normalized(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "AcMe", "dev", qty=8)
        entries = load_work_entries(data_dir)
        assert entries[0].client == "acme"

    def test_list_filter_case_insensitive(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        entries = list_work_entries(data_dir, client="ACME")
        assert len(entries) == 1
        assert entries[0].client == "acme"

    def test_uninvoiced_filter_case_insensitive(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        entries = get_uninvoiced_entries(data_dir, client="ACME")
        assert len(entries) == 1

    def test_update_normalizes_client(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        update_work_entry(data_dir, 1, client="BETA")
        entries = load_work_entries(data_dir)
        assert entries[0].client == "beta"


class TestFileFormat:
    def test_optional_fields_omitted(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1)
        content = (data_dir / "invoices" / "work" / "2026.toml").read_text()
        assert "discount" not in content
        assert "description" not in content
        assert "entity" not in content
        assert "invoice" not in content
        assert "paid_date" not in content
        assert "amount" not in content

    def test_roundtrip_all_fields(self, data_dir):
        add_work_entry(
            data_dir, "2026-03-01", "acme", "dev",
            qty=2.5, discount=50, description="Test work",
            entity="llc", invoice="INV-001",
        )
        entries = load_work_entries(data_dir)
        assert len(entries) == 1
        e = entries[0]
        assert e.qty == 2.5
        assert e.discount == 50
        assert e.description == "Test work"
        assert e.entity == "llc"
        assert e.invoice == "INV-001"

    def test_whole_numbers_no_decimal(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        content = (data_dir / "invoices" / "work" / "2026.toml").read_text()
        assert "qty = 8\n" in content
        assert "8.0" not in content
