"""Durable invoice decisions through the CLI and the real sync seam."""

import json
from datetime import date
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from tests.money.test_cli import TestSyncMonarchInvoiceMatching as SyncFixtures, _invoke, _seed_monarch
from istota.money import db, work


@pytest.fixture
def scenario(tmp_path):
    helper = SyncFixtures()
    ctx = helper._ctx(tmp_path)
    runner = CliRunner()

    def call(*args, ok=True):
        result = _invoke(runner, list(args), tmp_path=tmp_path, obj=ctx)
        assert result.exit_code == (0 if ok else 1), result.output
        return json.loads(result.output)

    with patch("istota.money.core.invoicing.generate_invoice_pdf"):
        for _ in range(2):
            call("invoice", "create", "acme", "-s", "dev", "-q", "8")
    credit = helper._credit(1200)

    def sync():
        return helper._sync(runner, tmp_path, ctx, [credit])

    return ctx, call, sync


def test_review_settle_history_and_unpaid(scenario):
    ctx, call, sync = scenario
    sync()
    reviews = call("invoice", "review", "list")["matches"]
    assert len(reviews) == 1
    row = reviews[0]
    assert row["account"] == "Assets:Bank:Checking"
    assert row["profile"] == "default"
    assert row["candidate_details"] == [
        {"invoice_number": n, "client": "acme", "total": 1200.0}
        for n in ("INV-000001", "INV-000002")
    ]
    ident = row["ledger_txn_id"]
    sync()
    assert len(call("invoice", "review", "list")["matches"]) == 1
    ledger = (ctx.data_dir / "main.beancount").read_text()
    call("invoice", "review", "settle", ident, "--invoice", "INV-000001")
    assert work.get_entries_for_invoice(ctx.data_dir, "INV-000001")[0].paid_date == date.today()
    assert work.get_entries_for_invoice(ctx.data_dir, "INV-000002")[0].paid_date is None
    assert (ctx.data_dir / "main.beancount").read_text() == ledger
    assert call("invoice", "review", "list")["matches"] == []
    history = call("invoice", "matches", "--invoice", "INV-000001")["matches"]
    assert history[0]["status"] == "settled"
    assert history[0]["decided_by"] == "user"
    call("invoice", "unpaid", "INV-000001")
    assert work.get_entries_for_invoice(ctx.data_dir, "INV-000001")[0].paid_date is None
    assert call("invoice", "matches")["matches"][0]["status"] == "reverted"
    sync()
    assert len(call("invoice", "review", "list", "--all")["matches"]) == 1


def test_dismiss_and_invalid_candidate(scenario):
    ctx, call, sync = scenario
    sync()
    ident = call("invoice", "review", "list")["matches"][0]["ledger_txn_id"]
    call("invoice", "review", "settle", ident, "--invoice", "INV-999999", ok=False)
    call("invoice", "review", "dismiss", ident)
    call("invoice", "review", "settle", ident, "--invoice", "INV-000001", ok=False)
    assert call("invoice", "review", "list")["matches"] == []
    assert call("invoice", "review", "list", "--all")["matches"][0]["status"] == "dismissed"
    assert all(e.paid_date is None for e in work.load_work_entries(ctx.data_dir))


@pytest.mark.parametrize("change", ["paid", "partial", "void"])
def test_review_refuses_invoice_no_longer_open(scenario, change):
    ctx, call, sync = scenario
    sync()
    ident = call("invoice", "review", "list")["matches"][0]["ledger_txn_id"]
    if change == "void":
        call("invoice", "void", "INV-000001")
    elif change == "paid":
        call("invoice", "paid", "INV-000001", "--date", date.today().isoformat(), "--no-post")
    else:
        entries = work.load_work_entries(ctx.data_dir)
        from dataclasses import replace
        entries.append(replace(entries[0], uid="other-work-entry", paid_date=date.today()))
        work._save_entries(ctx.data_dir, entries)
    call("invoice", "review", "settle", ident, "--invoice", "INV-000001", ok=False)
    with db.get_db(ctx.db_path) as conn:
        assert db.list_payment_matches(conn)[0]["status"] == "review"


def test_auto_match_is_recorded(scenario):
    ctx, call, sync = scenario
    call("invoice", "void", "INV-000002")
    sync()
    row = call("invoice", "matches")["matches"][0]
    assert row["status"] == "settled"
    assert row["decided_by"] == "auto"
    assert row["invoice_number"] == "INV-000001"
    assert row["monarch_id"] and row["ledger_txn_id"]
    sync()
    assert len(call("invoice", "matches")["matches"]) == 1


def test_contested_credits_in_two_profiles_have_separate_reviews(scenario):
    ctx, call, _ = scenario
    other = ctx.data_dir / "other.beancount"
    other.write_text("")
    ctx.users["default"].ledgers.append({"name": "other", "path": other})
    _seed_monarch(ctx.db_path, SyncFixtures._MONARCH_TOML +
                  '\n[monarch.profiles.default.tags]\ninclude = ["default"]\n'
                  '[monarch.profiles.other]\nledger = "other"\n'
                  '[monarch.profiles.other.tags]\ninclude = ["other"]\n')
    call("invoice", "void", "INV-000002")
    helper = SyncFixtures()
    credits = [helper._credit(1200, payee=name, tags=[name]) for name in ("default", "other")]
    helper._sync(CliRunner(), ctx.data_dir, ctx, credits)
    rows = call("invoice", "review", "list")["matches"]
    assert len(rows) == 2
    assert len({r["ledger_txn_id"] for r in rows}) == 2
    assert {r["profile"] for r in rows} == {"default", "other"}
    assert all(r["candidates"] == ["INV-000001"] for r in rows)
    call("invoice", "review", "settle", rows[0]["ledger_txn_id"], "--invoice", "INV-000001")
    call("invoice", "review", "settle", rows[1]["ledger_txn_id"], "--invoice", "INV-000001", ok=False)


def test_match_identity_is_ledger_id_not_monarch_id(tmp_path):
    path = tmp_path / "money.db"
    db.init_db(path)
    db.init_db(path)
    with db.get_db(path) as conn:
        for profile in ("default", "other"):
            assert db.record_payment_match(
                conn, ledger_txn_id=profile, monarch_id="same-credit", profile=profile,
                txn_date="2026-01-01", amount=1200, status="review",
            )
        assert not db.record_payment_match(
            conn, ledger_txn_id="default", monarch_id="same-credit",
            txn_date="2026-01-01", amount=1200, status="review",
        )
        assert len(db.list_payment_matches(conn)) == 2


def test_review_validates_after_acquiring_work_lock(scenario):
    from contextlib import contextmanager

    ctx, call, sync = scenario
    sync()
    ident = call("invoice", "review", "list")["matches"][0]["ledger_txn_id"]
    original_lock = work._work_lock

    @contextmanager
    def changed_before_lock(data_dir):
        with original_lock(data_dir):
            entries = work.load_work_entries(data_dir)
            entries[0].service = "retired-service"
            work._save_entries(data_dir, entries)
            yield

    with patch.object(work, "_work_lock", changed_before_lock):
        call("invoice", "review", "settle", ident, "--invoice", "INV-000001", ok=False)
    assert all(e.paid_date is None for e in work.load_work_entries(ctx.data_dir))
    assert call("invoice", "review", "list")["matches"][0]["status"] == "review"


def test_failed_decision_write_leaves_stamped_work_and_open_review(scenario):
    from istota.money import config_store
    from istota.money.invoice_review import settle_payment_review

    ctx, call, sync = scenario
    sync()
    ident = call("invoice", "review", "list")["matches"][0]["ledger_txn_id"]
    with patch.object(db, "settle_review", side_effect=RuntimeError("write failed")):
        with pytest.raises(RuntimeError, match="write failed"):
            settle_payment_review(ctx.db_path, ctx.data_dir,
                                  config_store.load_invoicing(ctx.db_path), ident, "INV-000001")
    assert work.get_entries_for_invoice(ctx.data_dir, "INV-000001")[0].paid_date == date.today()
    assert call("invoice", "review", "list")["matches"][0]["status"] == "review"
