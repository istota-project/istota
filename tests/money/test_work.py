"""Tests for money.work module."""

from datetime import date

import pytest

from istota.money.work import (
    WorkStoreLocked,
    _work_dir,
    _work_lock,
    add_work_entry,
    assign_invoice_number,
    backfill_work_ids,
    clear_invoice_payment,
    entry_etag,
    get_entries_for_invoice,
    get_invoice_numbers,
    get_uninvoiced_entries,
    list_work_entries,
    load_work_entries,
    record_invoice_payment,
    remove_work_entry,
    remove_work_entry_by_uid,
    update_work_entry,
    update_work_entry_by_uid,
    void_invoice,
)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


def _write_raw_year(data_dir, year: int, lines: list[str]) -> None:
    """Write a year file by hand, bypassing the serializer.

    Used to simulate a legacy (pre-uid) store or a hand-edited file.
    """
    work_dir = data_dir / "invoices" / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    body = "[[entries]]\n" + "\n".join(lines) + "\n"
    (work_dir / f"{year}.toml").write_text(body)


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


class TestClearInvoicePayment:
    def test_clear_payment_keeps_invoice_number(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1, 2], "INV-000001")
        record_invoice_payment(data_dir, "INV-000001", "2026-04-15")

        count = clear_invoice_payment(data_dir, "INV-000001")
        assert count == 2

        # paid_date cleared, but the invoice number stays put
        entries = get_entries_for_invoice(data_dir, "INV-000001")
        assert len(entries) == 2
        assert all(e.paid_date is None for e in entries)
        assert all(e.invoice == "INV-000001" for e in entries)

    def test_clear_payment_when_already_unpaid(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        count = clear_invoice_payment(data_dir, "INV-000001")
        assert count == 0
        # Still invoiced, just never paid.
        assert get_entries_for_invoice(data_dir, "INV-000001")[0].invoice == "INV-000001"

    def test_clear_payment_nonexistent_invoice(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        count = clear_invoice_payment(data_dir, "INV-999999")
        assert count == 0

    def test_clear_payment_does_not_affect_other_invoices(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        add_work_entry(data_dir, "2026-03-02", "acme", "dev", qty=4)
        assign_invoice_number(data_dir, [1], "INV-000001")
        assign_invoice_number(data_dir, [2], "INV-000002")
        record_invoice_payment(data_dir, "INV-000001", "2026-04-15")
        record_invoice_payment(data_dir, "INV-000002", "2026-04-16")

        clear_invoice_payment(data_dir, "INV-000001")

        entries = load_work_entries(data_dir)
        assert entries[0].paid_date is None
        assert entries[1].paid_date == date(2026, 4, 16)


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


class TestConcurrencySafety:
    def test_work_lock_is_exclusive(self, data_dir):
        # flock is per-open-file-description and mutually exclusive across
        # fds even within one process, so a nested non-blocking acquire times
        # out — proving two writers can't interleave.
        with _work_lock(data_dir):
            with pytest.raises(WorkStoreLocked):
                with _work_lock(data_dir, timeout_seconds=0.2):
                    pass

    def test_lock_released_after_context(self, data_dir):
        with _work_lock(data_dir):
            pass
        # Re-acquire immediately; should not raise.
        with _work_lock(data_dir, timeout_seconds=0.2):
            pass

    def test_save_leaves_no_temp_files(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        assign_invoice_number(data_dir, [1], "INV-000001")
        record_invoice_payment(data_dir, "INV-000001", "2026-04-15")
        wd = _work_dir(data_dir)
        leftovers = [p.name for p in wd.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_lock_file_not_parsed_as_year(self, data_dir):
        # The .work.lock anchor lives in the work dir; it must never be
        # mistaken for a {year}.toml data file.
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        with _work_lock(data_dir):
            pass
        entries = load_work_entries(data_dir)
        assert len(entries) == 1


class TestClientCaseNormalization:
    def test_add_normalizes_client_to_lowercase(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "Globex", "dev", qty=8)
        entries = load_work_entries(data_dir)
        assert entries[0].client == "globex"

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


class TestStableUids:
    def test_add_stamps_uid(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        e = load_work_entries(data_dir)[0]
        assert e.uid
        assert len(e.uid) == 32

    def test_uid_persisted_to_toml(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        text = (data_dir / "invoices" / "work" / "2026.toml").read_text()
        assert "uid = " in text

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

    def test_load_does_not_stamp_uids(self, data_dir):
        """Reading is never a write — an un-backfilled file stays un-backfilled."""
        _write_raw_year(data_dir, 2026, ['date = 2026-03-01', 'client = "acme"', 'service = "dev"'])
        entries = load_work_entries(data_dir)
        assert entries[0].uid == ""
        assert "uid" not in (data_dir / "invoices" / "work" / "2026.toml").read_text()


class TestBackfillWorkIds:
    def test_backfill_stamps_missing_uids(self, data_dir):
        _write_raw_year(data_dir, 2026, ['date = 2026-03-01', 'client = "acme"', 'service = "dev"'])
        assert backfill_work_ids(data_dir) == 1
        e = load_work_entries(data_dir)[0]
        assert e.uid

    def test_backfill_is_idempotent(self, data_dir):
        _write_raw_year(data_dir, 2026, ['date = 2026-03-01', 'client = "acme"', 'service = "dev"'])
        backfill_work_ids(data_dir)
        uid = load_work_entries(data_dir)[0].uid
        assert backfill_work_ids(data_dir) == 0
        assert load_work_entries(data_dir)[0].uid == uid

    def test_backfill_empty_store(self, data_dir):
        assert backfill_work_ids(data_dir) == 0

    def test_backfill_stamps_invoiced_entries_too(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
            'invoice = "INV-000001"',
        ])
        assert backfill_work_ids(data_dir) == 1
        assert load_work_entries(data_dir)[0].uid


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
        # Something else inserts a backdated entry — the target is now #2.
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
        """An un-backfilled entry (uid == '') must not be addressable by ''."""
        _write_raw_year(data_dir, 2026, ['date = 2026-03-01', 'client = "acme"', 'service = "dev"'])
        assert update_work_entry_by_uid(data_dir, "", qty=10).status == "not_found"

    def test_update_by_uid_cannot_rewrite_identity_fields(self, data_dir):
        """``uid`` is structurally unsettable (it's the positional arg);
        ``id`` and ``extra`` are filtered out."""
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
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        uid = load_work_entries(data_dir)[0].uid
        result = update_work_entry_by_uid(data_dir, uid, qty=3)
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
        """Two entries differing only in position must not share an etag,
        but the same entry at a different index keeps its etag."""
        add_work_entry(data_dir, "2026-03-10", "acme", "dev", qty=8)
        before = entry_etag(load_work_entries(data_dir)[0])
        add_work_entry(data_dir, "2026-03-01", "beta", "dev", qty=1)
        moved = [e for e in load_work_entries(data_dir) if e.client == "acme"][0]
        assert moved.id == 2
        assert entry_etag(moved) == before

    def test_update_with_matching_etag_succeeds(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        e = load_work_entries(data_dir)[0]
        result = update_work_entry_by_uid(data_dir, e.uid, expect_etag=entry_etag(e), qty=10)
        assert result.ok

    def test_update_with_stale_etag_conflicts(self, data_dir):
        add_work_entry(data_dir, "2026-03-01", "acme", "dev", qty=8)
        e = load_work_entries(data_dir)[0]
        stale = entry_etag(e)
        # Someone else edits the same entry first.
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


class TestUnknownKeyRoundTrip:
    def test_unknown_key_survives_load_save(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'uid = "aaa"', 'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
            'qty = 8', 'billing_note = "holiday rate"',
        ])
        # Mutating a *different* entry rewrites the whole year file.
        add_work_entry(data_dir, "2026-03-05", "beta", "dev", qty=1)

        text = (data_dir / "invoices" / "work" / "2026.toml").read_text()
        assert 'billing_note = "holiday rate"' in text

    def test_unknown_key_exposed_on_the_entry(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'date = 2026-03-01', 'client = "acme"', 'service = "dev"', 'reviewed = true',
        ])
        assert load_work_entries(data_dir)[0].extra == {"reviewed": True}

    def test_unknown_key_types_round_trip(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
            'reviewed = true', 'ticket = 42', 'weight = 1.5',
            'tags = ["a", "b"]', 'approved_on = 2026-04-01',
        ])
        backfill_work_ids(data_dir)
        e = load_work_entries(data_dir)[0]
        assert e.extra == {
            "reviewed": True,
            "ticket": 42,
            "weight": 1.5,
            "tags": ["a", "b"],
            "approved_on": date(2026, 4, 1),
        }

    def test_unserializable_extra_is_dropped_not_fatal(self, data_dir):
        """A nested table can't be written back by the hand-rolled serializer.

        It's dropped rather than crashing the write — the alternative is
        a save path that can be poisoned by an arbitrary hand edit.
        """
        work_dir = data_dir / "invoices" / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "2026.toml").write_text(
            "[[entries]]\n"
            "date = 2026-03-01\n"
            'client = "acme"\n'
            'service = "dev"\n'
            "\n[entries.meta]\n"
            'source = "import"\n'
        )
        assert backfill_work_ids(data_dir) == 1
        e = load_work_entries(data_dir)[0]
        assert e.uid
        assert "meta" not in (work_dir / "2026.toml").read_text()

    def test_etag_covers_extra_keys(self, data_dir):
        _write_raw_year(data_dir, 2026, [
            'uid = "aaa"', 'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
        ])
        plain = entry_etag(load_work_entries(data_dir)[0])
        _write_raw_year(data_dir, 2026, [
            'uid = "aaa"', 'date = 2026-03-01', 'client = "acme"', 'service = "dev"',
            'note = "x"',
        ])
        assert entry_etag(load_work_entries(data_dir)[0]) != plain
