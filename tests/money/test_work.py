"""Tests for money.work: work entries and invoices in money.db."""

import sqlite3
from datetime import date, datetime, time

import pytest

from istota.money import db
from istota.money.core.models import WorkEntry
from istota.money.work import (
    WorkStoreBlocked,
    WorkStoreLocked,
    _work_lock,
    add_work_entry,
    assign_invoice_number,
    assign_invoice_number_by_uids,
    backfill_work_ids,
    bind_db_path,
    create_invoice,
    db_path_for,
    entry_etag,
    get_entries_for_invoice,
    get_invoice_numbers,
    get_uninvoiced_entries,
    insert_entry,
    invoice_issue_date,
    list_invoices,
    list_work_entries,
    load_work_entries,
    mark_paid,
    mark_unpaid,
    remove_work_entry,
    remove_work_entry_by_uid,
    update_work_entry,
    update_work_entry_by_uid,
    VoidError,
    get_invoice,
    void_invoice,
    write_transaction,
)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


def _write_raw_year(data_dir, year: int, lines: list[str]) -> None:
    """Write a legacy year file by hand, for the import to find."""
    work_dir = data_dir / "invoices" / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    body = "[[entries]]\n" + "\n".join(lines) + "\n"
    (work_dir / f"{year}.toml").write_text(body)


def _insert(data_dir, **fields) -> str:
    """Insert an entry carrying fields the public add path does not take."""
    fields.setdefault("uid", f"uid-{len(load_work_entries(data_dir))}")
    entry = WorkEntry(**fields)
    with write_transaction(data_dir) as conn:
        insert_entry(conn, entry)
    return entry.uid


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

    def test_add_returns_the_display_index_it_landed_at(self, data_dir):
        add_work_entry(data_dir, "2026-03-15", "acme", "dev", qty=4)
        assert add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8) == 1

    def test_same_date_entries_keep_insertion_order(self, data_dir):
        """Display order is (date, created_at, uid): the uid only breaks a tie."""
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1, uid="zzz")
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=2, uid="aaa")
        assert [e.uid for e in load_work_entries(data_dir)] == ["zzz", "aaa"]

    def test_entries_live_in_money_db_not_in_year_files(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert not (data_dir / "invoices" / "work").exists()
        with db.get_db(db_path_for(data_dir)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM work_entries").fetchone()[0] == 1

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


class TestWhereTheStoreIs:
    """The store is the context's money.db, which is not under data_dir on a deployment."""

    def test_unbound_falls_back_under_the_data_dir(self, tmp_path):
        assert db_path_for(tmp_path / "money") == tmp_path / "money" / "data" / "money.db"

    def test_a_bound_path_is_used(self, tmp_path):
        data = tmp_path / "workspace" / "money"
        local = tmp_path / "local" / "u" / "money.db"
        bind_db_path(data, local)
        add_work_entry(data, "2026-03-01", "acme", "dev", qty=8)
        assert local.exists()
        assert not (data / "data" / "money.db").exists()

    def test_the_workspace_context_binds_its_db_path(self, tmp_path):
        from istota.money.workspace import synthesize_user_context

        local = tmp_path / "local" / "money.db"
        ctx = synthesize_user_context(tmp_path / "ws", db_path=local)
        assert db_path_for(ctx.data_dir) == local


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

    def test_update_cannot_write_invoice_stamps_directly(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        update_work_entry(data_dir, 1, paid_date=date(2026, 4, 1), qty=9)
        e = load_work_entries(data_dir)[0]
        assert e.paid_date is None
        assert e.qty == 9


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

    def test_an_entry_a_voided_invoice_lists_is_kept(self, data_dir):
        """The voided invoice still names its entries, so they cannot vanish."""
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assign_invoice_number(data_dir, [1], "INV-000001")
        void_invoice(data_dir, "INV-000001")

        result = remove_work_entry_by_uid(data_dir, uid)
        assert result.status == "on_voided_invoice"
        assert len(load_work_entries(data_dir)) == 1
        assert list_invoices(data_dir)[0].entry_uids == [uid]


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
        count = mark_paid(data_dir, "INV-000001", "2026-04-15")
        assert count == 2
        entries = get_entries_for_invoice(data_dir, "INV-000001")
        assert all(e.paid_date == date(2026, 4, 15) for e in entries)
        (record,) = list_invoices(data_dir)
        assert record.status == "paid"
        assert record.paid_date == date(2026, 4, 15)

    def test_payment_idempotent(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        mark_paid(data_dir, "INV-000001", "2026-04-15")
        count = mark_paid(data_dir, "INV-000001", "2026-05-01")
        assert count == 0
        assert list_invoices(data_dir)[0].paid_date == date(2026, 4, 15)

    def test_validate_can_refuse_the_payment(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        seen = []

        def validate(entries):
            seen.extend(entries)
            return False

        assert mark_paid(data_dir, "INV-000001", "2026-04-15", validate=validate) == 0
        assert [e.invoice for e in seen] == ["INV-000001"]
        assert list_invoices(data_dir)[0].status == "open"

    def test_a_voided_invoice_cannot_be_paid(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        void_invoice(data_dir, "INV-000001")
        assert mark_paid(data_dir, "INV-000001", "2026-04-15") == 0
        assert list_invoices(data_dir)[0].status == "voided"


class TestMarkUnpaid:
    def test_clear_payment_keeps_invoice_number(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1, 2], "INV-000001")
        mark_paid(data_dir, "INV-000001", "2026-04-15")

        count = mark_unpaid(data_dir, "INV-000001")
        assert count == 2

        entries = get_entries_for_invoice(data_dir, "INV-000001")
        assert len(entries) == 2
        assert all(e.paid_date is None for e in entries)
        assert all(e.invoice == "INV-000001" for e in entries)
        assert list_invoices(data_dir)[0].status == "open"

    def test_clear_payment_when_already_unpaid(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        count = mark_unpaid(data_dir, "INV-000001")
        assert count == 0
        assert get_entries_for_invoice(data_dir, "INV-000001")[0].invoice == "INV-000001"

    def test_clear_payment_nonexistent_invoice(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        count = mark_unpaid(data_dir, "INV-999999")
        assert count == 0

    def test_clear_payment_does_not_affect_other_invoices(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1], "INV-000001")
        assign_invoice_number(data_dir, [2], "INV-000002")
        mark_paid(data_dir, "INV-000001", "2026-04-15")
        mark_paid(data_dir, "INV-000002", "2026-04-16")

        mark_unpaid(data_dir, "INV-000001")

        entries = load_work_entries(data_dir)
        assert entries[0].paid_date is None
        assert entries[1].paid_date == date(2026, 4, 16)


class TestVoidInvoice:
    def test_void_clears_invoice_and_paid_date(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1, 2], "INV-000001")
        mark_paid(data_dir, "INV-000001", "2026-04-15")

        result = void_invoice(data_dir, "INV-000001", force=True)
        assert result.released == 2
        assert result.was_paid is True

        entries = load_work_entries(data_dir)
        assert all(e.invoice == "" for e in entries)
        assert all(e.paid_date is None for e in entries)

    def test_void_keeps_the_invoice_row_and_its_entries(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        uids = [e.uid for e in load_work_entries(data_dir)]
        assign_invoice_number(data_dir, [1, 2], "INV-000001")
        mark_paid(data_dir, "INV-000001", "2026-04-15")

        void_invoice(data_dir, "INV-000001", force=True)

        (record,) = list_invoices(data_dir)
        assert record.status == "voided"
        assert record.voided_at
        assert record.was_paid is True
        assert sorted(record.entry_uids) == sorted(uids)
        assert list_invoices(data_dir, include_voided=False) == []

    def test_void_unpaid_invoice(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")

        result = void_invoice(data_dir, "INV-000001")
        assert result.released == 1
        assert result.was_paid is False
        entries = load_work_entries(data_dir)
        assert entries[0].invoice == ""
        assert list_invoices(data_dir)[0].was_paid is False

    def test_a_paid_invoice_needs_force_and_is_left_alone_without_it(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        mark_paid(data_dir, "INV-000001", "2026-04-15")

        with pytest.raises(VoidError) as raised:
            void_invoice(data_dir, "INV-000001")
        assert raised.value.code == "paid"
        record = get_invoice(data_dir, "INV-000001")
        assert record.status == "paid"
        assert load_work_entries(data_dir)[0].invoice == "INV-000001"

    def test_void_nonexistent_invoice(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        with pytest.raises(VoidError, match="not found") as raised:
            void_invoice(data_dir, "INV-999999")
        assert raised.value.code == "not_found"

    def test_voiding_twice_is_refused_with_the_first_date(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        void_invoice(data_dir, "INV-000001", reason="first")
        first = list_invoices(data_dir)[0].voided_at
        with pytest.raises(VoidError, match=f"already voided on {first[:10]}") as raised:
            void_invoice(data_dir, "INV-000001", reason="second")
        assert raised.value.code == "already_voided"
        record = list_invoices(data_dir)[0]
        assert record.voided_at == first
        assert record.void_reason == "first"

    def test_reason_and_replacements_are_recorded(self, data_dir):
        for day in ("01", "02", "03"):
            add_work_entry(data_dir, f"2026-03-{day}", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1, 2], "INV-000251")
        void_invoice(data_dir, "INV-000251")
        assign_invoice_number(data_dir, [1], "INV-000253")
        assign_invoice_number(data_dir, [2], "INV-000254")
        assign_invoice_number(data_dir, [3], "INV-000255")
        void_invoice(data_dir, "INV-000255", reason="billed twice",
                     replaced_by=["INV-000253", " INV-000254 ", "INV-000253"])

        record = get_invoice(data_dir, "INV-000255")
        assert record.void_reason == "billed twice"
        assert record.replaced_by == ["INV-000253", "INV-000254"]

    def test_an_unknown_replacement_is_refused_and_nothing_is_written(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")

        for replaced_by in (["INV-000999"], ["INV-000001"]):
            with pytest.raises(VoidError) as raised:
                void_invoice(data_dir, "INV-000001", replaced_by=replaced_by)
            assert raised.value.code == "unknown_replacement"
        record = get_invoice(data_dir, "INV-000001")
        assert record.status == "open"
        assert record.replaced_by == []
        assert load_work_entries(data_dir)[0].invoice == "INV-000001"

    def test_a_new_pdf_path_is_stored_with_the_void(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        result = void_invoice(data_dir, "INV-000001", pdf_path="/x/Invoice-000001-VOID.pdf")
        assert result.pdf_path == "/x/Invoice-000001-VOID.pdf"
        assert get_invoice(data_dir, "INV-000001").pdf_path == "/x/Invoice-000001-VOID.pdf"

    def test_get_invoice_unknown_is_none(self, data_dir):
        assert get_invoice(data_dir, "INV-000404") is None

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

        uninvoiced = get_uninvoiced_entries(data_dir)
        assert len(uninvoiced) == 1

        count = assign_invoice_number(data_dir, [1], "INV-000002")
        assert count == 1
        entries = get_entries_for_invoice(data_dir, "INV-000002")
        assert len(entries) == 1

    def test_a_voided_number_is_never_reused(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        void_invoice(data_dir, "INV-000001")
        with pytest.raises(ValueError, match="voided"):
            assign_invoice_number(data_dir, [1], "INV-000001")
        assert load_work_entries(data_dir)[0].invoice == ""

    def test_void_removes_from_invoice_numbers_list(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")

        assert "INV-000001" in get_invoice_numbers(data_dir)
        void_invoice(data_dir, "INV-000001")
        assert "INV-000001" not in get_invoice_numbers(data_dir)


class TestInvoiceTotals:
    """An invoice's total is stored; a hand assignment edits it."""

    def _seed_config(self, data_dir):
        from istota.money import config_store
        from istota.money.core.models import CompanyConfig, InvoicingConfig, ServiceConfig

        path = db_path_for(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        config_store.init_db(path)
        config_store.save_invoicing(path, InvoicingConfig(
            accounting_path=".", invoice_output="invoices", next_invoice_number=1,
            company=CompanyConfig(name="Co", key="co"), clients={},
            services={"dev": ServiceConfig(key="dev", display_name="Dev", rate=100)},
            companies={"co": CompanyConfig(name="Co", key="co")}, default_entity="co",
        ))

    def test_hand_assignment_totals_at_the_current_rates(self, data_dir):
        self._seed_config(data_dir)
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=2)
        assign_invoice_number(data_dir, [1], "INV-000001")
        assert list_invoices(data_dir)[0].total == 800
        assign_invoice_number(data_dir, [2], "INV-000001")
        assert list_invoices(data_dir)[0].total == 1000
        assert list_invoices(data_dir)[0].entity == "co"

    def test_create_invoice_stores_the_total_it_is_given(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        with write_transaction(data_dir) as conn:
            create_invoice(
                conn, number="INV-000007", client="acme", entity="co", group_name="Services",
                issue_date="2026-03-31", total=1234.5, entry_uids=[uid], pdf_path="/x.pdf",
            )
        (record,) = list_invoices(data_dir)
        assert (record.total, record.group_name, record.pdf_path) == (1234.5, "Services", "/x.pdf")
        assert record.issue_date == date(2026, 3, 31)
        assert load_work_entries(data_dir)[0].invoice_date == date(2026, 3, 31)

    def test_create_invoice_refuses_an_unknown_entry(self, data_dir):
        with pytest.raises(ValueError, match="no work entry"):
            with write_transaction(data_dir) as conn:
                create_invoice(
                    conn, number="INV-000001", client="acme", entity="co", group_name="",
                    issue_date="2026-03-31", total=1, entry_uids=["missing"], pdf_path="",
                )
        assert list_invoices(data_dir) == []

    def test_create_invoice_refuses_a_number_already_issued(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=8)
        first, second = (e.uid for e in load_work_entries(data_dir))
        with write_transaction(data_dir) as conn:
            create_invoice(
                conn, number="INV-000001", client="acme", entity="co", group_name="",
                issue_date="2026-03-31", total=1, entry_uids=[first], pdf_path="",
            )
        with pytest.raises(sqlite3.IntegrityError):
            with write_transaction(data_dir) as conn:
                create_invoice(
                    conn, number="INV-000001", client="acme", entity="co", group_name="",
                    issue_date="2026-03-31", total=1, entry_uids=[second], pdf_path="",
                )
        assert get_entries_for_invoice(data_dir, "INV-000001")[0].uid == first


class TestCallerTransactions:
    """A caller holding a transaction on money.db passes its connection."""

    def test_mark_paid_joins_the_callers_transaction(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        with db.get_db(db_path_for(data_dir)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            assert mark_paid(data_dir, "INV-000001", "2026-04-15", conn=conn) == 1
            conn.rollback()
        assert list_invoices(data_dir)[0].status == "open"

    def test_mark_unpaid_and_void_join_it_too(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        mark_paid(data_dir, "INV-000001", "2026-04-15")
        with write_transaction(data_dir) as conn:
            mark_unpaid(data_dir, "INV-000001", conn=conn)
            void_invoice(data_dir, "INV-000001", conn=conn)
        (record,) = list_invoices(data_dir)
        assert record.status == "voided"
        assert record.was_paid is False


class TestBlockedStore:
    def test_every_function_raises_while_the_import_is_blocked(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'uid = "a"', 'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
            'invoice = "INV-000001"', 'paid_date = 2026-04-01',
        ])
        with (data_dir / "invoices" / "work" / "2026.toml").open("a") as f:
            f.write('\n[[entries]]\nuid = "b"\ndate = 2026-03-02\nclient = "acme"\n'
                    'service = "dev"\ninvoice = "INV-000001"\n')

        for call in (
            lambda: load_work_entries(data_dir),
            lambda: add_work_entry(data_dir, "2026-03-03", "acme", "dev", qty=1),
            lambda: list_invoices(data_dir),
            lambda: mark_paid(data_dir, "INV-000001", "2026-05-01"),
        ):
            with pytest.raises(WorkStoreBlocked, match="work log migration blocked: .*paid_date"):
                call()
        with db.get_db(db_path_for(data_dir)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM work_entries").fetchone()[0] == 0


class TestClientCaseNormalization:
    def test_add_normalizes_client_to_lowercase(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "Globex", "dev", qty=8)
        entries = load_work_entries(data_dir)
        assert entries[0].client == "globex"

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


class TestFieldRoundTrip:
    def test_whole_numbers_keep_their_etag_form(self, data_dir):
        """A REAL column reads 8 back as 8.0; the etag must not see a change."""
        from istota.money.work import _serialize_entry

        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        e = load_work_entries(data_dir)[0]
        assert e.qty == 8
        assert "qty = 8\n" in _serialize_entry(e) + "\n"
        assert entry_etag(e) == entry_etag(WorkEntry(**{**e.__dict__, "qty": 8}))

    def test_extra_survives_the_round_trip_with_its_types(self, data_dir):
        extra = {
            "reviewed": True, "ticket": 42, "weight": 1.5, "tags": ["a", "b"],
            "approved_on": date(2026, 4, 1),
            "logged_at": datetime(2026, 4, 1, 9, 30),
            "start": time(9, 0),
            "meta": {"source": "import", "on": date(2026, 1, 2)},
            "my key": "hello", "a.b": 1,
        }
        uid = _insert(data_dir, date=date(2026, 3, 1), client="acme", service="dev", extra=extra)
        (e,) = load_work_entries(data_dir)
        assert e.uid == uid
        assert e.extra == extra

    def test_extra_survives_an_unrelated_update(self, data_dir):
        _insert(data_dir, date=date(2026, 3, 1), client="acme", service="dev",
                extra={"billing_note": "holiday rate"})
        update_work_entry(data_dir, 1, qty=3)
        assert load_work_entries(data_dir)[0].extra == {"billing_note": "holiday rate"}

    def test_separate_defaults_off_and_round_trips(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1, description="plain")
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=1, description="alone",
                       separate=True)
        flags = {e.description: e.separate for e in load_work_entries(data_dir)}
        assert flags == {"plain": False, "alone": True}

    def test_separate_is_updatable_and_changes_the_etag(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1)
        before = load_work_entries(data_dir)[0]
        assert update_work_entry_by_uid(data_dir, before.uid, separate=True).ok
        after = load_work_entries(data_dir)[0]
        assert after.separate is True
        assert entry_etag(after) != entry_etag(before)
        assert update_work_entry(data_dir, 1, separate=False)
        assert load_work_entries(data_dir)[0].separate is False
        assert entry_etag(load_work_entries(data_dir)[0]) == entry_etag(before)

    @pytest.mark.parametrize("raw", ["a\rb", "a\tb", "a\x00b", "a\x1fb", "a\x7fb", "line\r\nline"])
    def test_control_characters_round_trip(self, data_dir, raw):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1, description=raw)
        assert load_work_entries(data_dir)[0].description == raw


class TestStableUids:
    def test_add_stamps_uid(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        e = load_work_entries(data_dir)[0]
        assert e.uid
        assert len(e.uid) == 32

    def test_uids_are_distinct(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        uids = {e.uid for e in load_work_entries(data_dir)}
        assert len(uids) == 2

    def test_uid_survives_unrelated_mutation(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-05", "acme", "dev", qty=4)
        before = [e.uid for e in load_work_entries(data_dir)]
        update_work_entry(data_dir, 2, qty=6)
        after = [e.uid for e in load_work_entries(data_dir)]
        assert before == after

    def test_uid_survives_invoice_assignment(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assign_invoice_number(data_dir, [1], "INV-000001")
        assert load_work_entries(data_dir)[0].uid == uid


class TestBackfillWorkIds:
    """Before the import, backfill stamps uids in the year files; after it, nothing."""

    def test_backfill_stamps_missing_uids_in_the_year_files(self, data_dir):
        _write_raw_year(data_dir, 2026, ['date = 2026-03-01', 'client = "acme"', 'service = "dev"'])
        assert backfill_work_ids(data_dir) == 1
        assert "uid = " in (data_dir / "invoices" / "work" / "2026.toml").read_text()
        assert backfill_work_ids(data_dir) == 0

    def test_backfill_lets_a_refused_import_through(self, data_dir):
        _write_raw_year(data_dir, 2026, ['date = 2026-03-01', 'client = "acme"', 'service = "dev"'])
        with pytest.raises(WorkStoreBlocked, match="no uid"):
            load_work_entries(data_dir)
        backfill_work_ids(data_dir)
        assert len(load_work_entries(data_dir)) == 1

    def test_backfill_is_a_no_op_after_the_import(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        _write_raw_year(data_dir, 2026, ['date = 2026-03-01', 'client = "acme"', 'service = "dev"'])
        before = (data_dir / "invoices" / "work" / "2026.toml").read_text()
        assert backfill_work_ids(data_dir) == 0
        assert (data_dir / "invoices" / "work" / "2026.toml").read_text() == before

    def test_backfill_empty_store(self, data_dir):
        assert backfill_work_ids(data_dir) == 0
        assert not (data_dir / "invoices" / "work").exists()

    def test_backfill_reports_contention_when_it_has_work_to_do(self, data_dir):
        _write_raw_year(data_dir, 2026, ['date = 2026-03-01', 'client = "acme"', 'service = "dev"'])
        with _work_lock(data_dir):
            with pytest.raises(WorkStoreLocked):
                backfill_work_ids(data_dir, timeout_seconds=0.1)


class TestUidAddressedMutations:
    def test_update_by_uid(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        result = update_work_entry_by_uid(data_dir, uid, qty=10)
        assert result.ok
        assert result.status == "ok"
        assert load_work_entries(data_dir)[0].qty == 10

    def test_update_by_uid_hits_right_entry_after_index_shift(self, data_dir):
        """The failure the uid exists to prevent: an insert before the target."""
        add_work_entry(data_dir, "2026-03-10", "acme", "dev", qty=8, description="target")
        uid = load_work_entries(data_dir)[0].uid
        add_work_entry(data_dir, "2026-03-01", "beta", "dev", qty=1, description="intruder")

        result = update_work_entry_by_uid(data_dir, uid, qty=99)
        assert result.ok

        entries = load_work_entries(data_dir)
        by_desc = {e.description: e for e in entries}
        assert by_desc["target"].qty == 99
        assert by_desc["intruder"].qty == 1

    def test_update_by_uid_not_found(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        result = update_work_entry_by_uid(data_dir, "deadbeef", qty=10)
        assert not result.ok
        assert result.status == "not_found"

    def test_update_by_uid_invoiced_refused(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assign_invoice_number(data_dir, [1], "INV-000001")
        result = update_work_entry_by_uid(data_dir, uid, qty=10)
        assert result.status == "invoiced"
        assert load_work_entries(data_dir)[0].qty == 8

    def test_update_by_uid_no_fields(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assert update_work_entry_by_uid(data_dir, uid).status == "no_fields"

    def test_update_by_uid_ignores_empty_uid(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert update_work_entry_by_uid(data_dir, "", qty=10).status == "not_found"

    def test_update_by_uid_cannot_rewrite_identity_fields(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        update_work_entry_by_uid(data_dir, uid, id=99, extra={"x": 1}, qty=9)
        e = load_work_entries(data_dir)[0]
        assert e.uid == uid
        assert e.id == 1
        assert e.extra == {}
        assert e.qty == 9

    def test_update_by_uid_coerces_date_and_client(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        update_work_entry_by_uid(data_dir, uid, date="2026-04-02", client="BETA")
        e = load_work_entries(data_dir)[0]
        assert e.date == date(2026, 4, 2)
        assert e.client == "beta"

    def test_update_by_uid_returns_fresh_entry(self, data_dir):
        add_work_entry(data_dir, "2026-03-05", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=1)
        uid = load_work_entries(data_dir)[1].uid
        result = update_work_entry_by_uid(data_dir, uid, qty=3, date="2026-02-01")
        assert result.entry is not None
        assert result.entry.qty == 3
        assert result.entry.id == 1

    def test_remove_by_uid(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assert remove_work_entry_by_uid(data_dir, uid).ok
        assert load_work_entries(data_dir) == []

    def test_remove_by_uid_hits_right_entry_after_index_shift(self, data_dir):
        add_work_entry(data_dir, "2026-03-10", "acme", "dev", qty=8, description="target")
        uid = load_work_entries(data_dir)[0].uid
        add_work_entry(data_dir, "2026-03-01", "beta", "dev", qty=1, description="intruder")

        assert remove_work_entry_by_uid(data_dir, uid).ok

        remaining = load_work_entries(data_dir)
        assert len(remaining) == 1
        assert remaining[0].description == "intruder"

    def test_remove_by_uid_not_found(self, data_dir):
        assert remove_work_entry_by_uid(data_dir, "nope").status == "not_found"

    def test_remove_by_uid_invoiced_refused(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assign_invoice_number(data_dir, [1], "INV-000001")
        assert remove_work_entry_by_uid(data_dir, uid).status == "invoiced"
        assert len(load_work_entries(data_dir)) == 1


class TestEntryEtag:
    def test_etag_is_stable_for_unchanged_entry(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        first = entry_etag(load_work_entries(data_dir)[0])
        second = entry_etag(load_work_entries(data_dir)[0])
        assert first == second
        assert first

    def test_etag_changes_when_entry_changes(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        before = entry_etag(load_work_entries(data_dir)[0])
        update_work_entry_by_uid(data_dir, uid, qty=9)
        assert entry_etag(load_work_entries(data_dir)[0]) != before

    def test_etag_ignores_display_index(self, data_dir):
        add_work_entry(data_dir, "2026-03-10", "acme", "dev", qty=8)
        before = entry_etag(load_work_entries(data_dir)[0])
        add_work_entry(data_dir, "2026-03-01", "beta", "dev", qty=1)
        moved = [e for e in load_work_entries(data_dir) if e.client == "acme"][0]
        assert moved.id == 2
        assert entry_etag(moved) == before

    def test_etag_covers_extra_keys(self, data_dir):
        _insert(data_dir, uid="aaa", date=date(2026, 3, 1), client="acme", service="dev")
        _insert(data_dir, uid="bbb", date=date(2026, 3, 1), client="acme", service="dev",
                extra={"note": "x"})
        plain, noted = load_work_entries(data_dir)
        assert entry_etag(plain) != entry_etag(WorkEntry(**{**noted.__dict__, "uid": "aaa"}))

    def test_update_with_matching_etag_succeeds(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        e = load_work_entries(data_dir)[0]
        result = update_work_entry_by_uid(data_dir, e.uid, expect_etag=entry_etag(e), qty=10)
        assert result.ok

    def test_update_with_stale_etag_conflicts(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        e = load_work_entries(data_dir)[0]
        stale = entry_etag(e)
        update_work_entry_by_uid(data_dir, e.uid, qty=99)

        result = update_work_entry_by_uid(data_dir, e.uid, expect_etag=stale, qty=10)
        assert result.status == "conflict"
        assert result.entry is not None
        assert result.entry.qty == 99
        assert load_work_entries(data_dir)[0].qty == 99

    def test_remove_with_stale_etag_conflicts(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        e = load_work_entries(data_dir)[0]
        stale = entry_etag(e)
        update_work_entry_by_uid(data_dir, e.uid, qty=99)

        result = remove_work_entry_by_uid(data_dir, e.uid, expect_etag=stale)
        assert result.status == "conflict"
        assert len(load_work_entries(data_dir)) == 1

    def test_remove_with_matching_etag_succeeds(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        e = load_work_entries(data_dir)[0]
        assert remove_work_entry_by_uid(data_dir, e.uid, expect_etag=entry_etag(e)).ok

    def test_no_etag_skips_the_check(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assert update_work_entry_by_uid(data_dir, uid, qty=10).ok


class TestAssignInvoiceNumberByUids:
    def test_stamps_by_uid(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assert assign_invoice_number_by_uids(data_dir, [uid], "INV-000001") == 1
        assert load_work_entries(data_dir)[0].invoice == "INV-000001"

    def test_hits_right_entry_after_index_shift(self, data_dir):
        add_work_entry(data_dir, "2026-03-10", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        add_work_entry(data_dir, "2026-03-01", "hooli", "dev", qty=1)

        assert assign_invoice_number_by_uids(data_dir, [uid], "INV-000001") == 1
        by_client = {e.client: e for e in load_work_entries(data_dir)}
        assert by_client["acme"].invoice == "INV-000001"
        assert by_client["hooli"].invoice == ""

    def test_skips_already_invoiced(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        assign_invoice_number_by_uids(data_dir, [uid], "INV-000001")
        assert assign_invoice_number_by_uids(data_dir, [uid], "INV-000002") == 0
        assert load_work_entries(data_dir)[0].invoice == "INV-000001"

    def test_unknown_uid_ignored(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert assign_invoice_number_by_uids(data_dir, ["nope"], "INV-000001") == 0
        assert load_work_entries(data_dir)[0].invoice == ""
        assert list_invoices(data_dir) == []

    def test_empty_list(self, data_dir):
        assert assign_invoice_number_by_uids(data_dir, [], "INV-000001") == 0


class TestInvoiceDate:
    """An invoice records when it was issued, and every entry on it reads it."""

    def _invoiced(self, data_dir, when="2026-03-01"):
        add_work_entry(data_dir, when, "acme", "dev", qty=8)
        return load_work_entries(data_dir)[0].uid

    def test_stamped_by_uid_addressed_assignment(self, data_dir):
        uid = self._invoiced(data_dir)
        assign_invoice_number_by_uids(data_dir, [uid], "INV-000001", date(2026, 5, 1))
        assert load_work_entries(data_dir)[0].invoice_date == date(2026, 5, 1)

    def test_stamped_by_index_addressed_assignment(self, data_dir):
        self._invoiced(data_dir)
        assign_invoice_number(data_dir, [1], "INV-000001", date(2026, 5, 1))
        assert load_work_entries(data_dir)[0].invoice_date == date(2026, 5, 1)

    def test_defaults_to_today_when_the_caller_names_no_date(self, data_dir):
        uid = self._invoiced(data_dir)
        assign_invoice_number_by_uids(data_dir, [uid], "INV-000001")
        assert load_work_entries(data_dir)[0].invoice_date == date.today()

    def test_accepts_an_iso_string(self, data_dir):
        uid = self._invoiced(data_dir)
        assign_invoice_number_by_uids(data_dir, [uid], "INV-000001", "2026-05-01")
        assert load_work_entries(data_dir)[0].invoice_date == date(2026, 5, 1)

    def test_a_later_entry_inherits_the_invoices_date(self, data_dir):
        uid = self._invoiced(data_dir)
        assign_invoice_number_by_uids(data_dir, [uid], "INV-000001", date(2026, 5, 1))
        add_work_entry(data_dir, "2026-04-01", "acme", "dev", qty=1)
        assign_invoice_number(data_dir, [2], "INV-000001", date(2026, 6, 1))
        assert {e.invoice_date for e in load_work_entries(data_dir)} == {date(2026, 5, 1)}

    def test_void_clears_it_with_the_number(self, data_dir):
        uid = self._invoiced(data_dir)
        assign_invoice_number_by_uids(data_dir, [uid], "INV-000001", date(2026, 5, 1))
        void_invoice(data_dir, "INV-000001")
        entry = load_work_entries(data_dir)[0]
        assert entry.invoice == ""
        assert entry.invoice_date is None

    def test_payment_does_not_touch_it(self, data_dir):
        uid = self._invoiced(data_dir)
        assign_invoice_number_by_uids(data_dir, [uid], "INV-000001", date(2026, 5, 1))
        mark_paid(data_dir, "INV-000001", "2026-06-01")
        entry = load_work_entries(data_dir)[0]
        assert entry.invoice_date == date(2026, 5, 1)
        assert entry.paid_date == date(2026, 6, 1)

    def test_an_uninvoiced_entry_has_none(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert load_work_entries(data_dir)[0].invoice_date is None

    def test_a_date_without_a_number_is_ignored(self, data_dir):
        add_work_entry(
            data_dir, "2026-03-01", "acme", "dev", qty=8,
            invoice_date=date(2026, 5, 1),
        )
        assert load_work_entries(data_dir)[0].invoice_date is None

    def test_pre_assigned_number_carries_the_date(self, data_dir):
        add_work_entry(
            data_dir, "2026-05-01", "acme", "dev", qty=8,
            invoice="INV-000001", invoice_date=date(2026, 5, 1),
        )
        assert load_work_entries(data_dir)[0].invoice_date == date(2026, 5, 1)


class TestInvoiceDateOnHandAssignment:
    """`work update --invoice` puts an entry on an invoice by hand."""

    def test_hand_assigned_number_gets_a_date(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert update_work_entry(data_dir, 1, invoice="INV-000001")
        assert load_work_entries(data_dir)[0].invoice_date == date.today()

    def test_by_uid_too(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        result = update_work_entry_by_uid(data_dir, uid, invoice="INV-000001")
        assert result.ok
        assert result.entry.invoice == "INV-000001"
        assert load_work_entries(data_dir)[0].invoice_date == date.today()

    def test_an_explicit_date_wins_over_today(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert update_work_entry(
            data_dir, 1, invoice="INV-000001", invoice_date="2026-05-01",
        )
        assert load_work_entries(data_dir)[0].invoice_date == date(2026, 5, 1)

    def test_an_existing_invoice_keeps_its_date(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001", "2026-04-01")
        assert update_work_entry(data_dir, 2, invoice="INV-000001")
        assert {e.invoice_date for e in load_work_entries(data_dir)} == {date(2026, 4, 1)}

    def test_an_empty_number_leaves_the_entry_uninvoiced_and_undated(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assert update_work_entry(data_dir, 1, invoice="")
        entry = load_work_entries(data_dir)[0]
        assert entry.invoice == ""
        assert entry.invoice_date is None

    def test_an_already_invoiced_entry_is_refused_outright(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        update_work_entry(data_dir, 1, invoice="INV-000001")
        assert not update_work_entry(data_dir, 1, invoice="")
        assert load_work_entries(data_dir)[0].invoice == "INV-000001"


class TestInvoiceIssueDate:
    """The rule the import applies to the year files' stamps."""

    def _entry(self, work_day, issue_day=None):
        return WorkEntry(
            date=date(2026, 1, work_day), client="acme", service="dev",
            invoice="INV-000001",
            invoice_date=date(2026, 5, issue_day) if issue_day else None,
        )

    def test_uses_the_stored_date_when_there_is_one(self):
        entries = [self._entry(5, 1), self._entry(20, 1)]
        assert invoice_issue_date(entries) == date(2026, 5, 1)

    def test_falls_back_to_the_latest_work_not_the_earliest(self):
        entries = [self._entry(5), self._entry(20)]
        assert invoice_issue_date(entries) == date(2026, 1, 20)

    def test_a_partially_stamped_invoice_uses_the_stored_date(self):
        entries = [self._entry(5), self._entry(20, 1)]
        assert invoice_issue_date(entries) == date(2026, 5, 1)

    def test_disagreeing_stored_dates_take_the_earliest(self):
        entries = [self._entry(5, 1), self._entry(20, 9)]
        assert invoice_issue_date(entries) == date(2026, 5, 1)

    def test_no_entries(self):
        assert invoice_issue_date([]) is None
