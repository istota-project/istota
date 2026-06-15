"""Tests for money.cli module.

The standalone config loader (``load_context`` / ``_parse_user_context`` /
``--config``) was removed: the money CLI is injection-only now. Callers (the
``istota money …`` operator CLI and the money skill) resolve a per-user
:class:`Context` and pass it via ``CliRunner.invoke(obj=...)``; config
(invoicing / monarch / tax) is read only from the per-user money DB through
:mod:`istota.money.config_store`.

These tests therefore build the Context directly and seed any invoicing /
monarch config into the DB.
"""

import json
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from istota.money import config_store
from istota.money.cli import Context, UserContext, cli, _resolve


@pytest.fixture
def runner():
    return CliRunner()


def _make_context(tmp_path, *, ledgers=None, db_path=None, secrets=None,
                  invoicing_config_path=None, monarch_config_path=None,
                  user="default"):
    """Build an injected Context for a tmp workspace, with the DB initialised."""
    dbp = db_path or (tmp_path / "data" / "money.db")
    dbp.parent.mkdir(parents=True, exist_ok=True)
    config_store.init_db(dbp)
    uctx = UserContext(
        data_dir=tmp_path,
        ledgers=ledgers or [],
        db_path=dbp,
        invoicing_config_path=invoicing_config_path,
        monarch_config_path=monarch_config_path,
    )
    obj = Context()
    obj.users[user] = uctx
    obj.activate_user(user)
    obj.secrets = secrets
    return obj


def _invoke(runner, cli_args, *, tmp_path, ledgers=None, db_path=None,
            secrets=None, invoicing_config_path=None, monarch_config_path=None,
            user="default", obj=None):
    """Invoke a money command through an injected Context."""
    if obj is None:
        obj = _make_context(
            tmp_path, ledgers=ledgers, db_path=db_path, secrets=secrets,
            invoicing_config_path=invoicing_config_path,
            monarch_config_path=monarch_config_path, user=user,
        )
    return runner.invoke(cli, ["-u", user, *cli_args], obj=obj)


def _seed_invoicing(db_path, toml_text):
    """Hydrate the DB invoicing config from a TOML string (old-fixture shape)."""
    cfg = config_store.invoicing_config_from_toml_dict(tomllib.loads(toml_text))
    config_store.save_invoicing(db_path, cfg, replace_collections=True)


def _seed_monarch(db_path, toml_text):
    """Hydrate the DB monarch config from a TOML string (old-fixture shape)."""
    cfg = config_store.monarch_config_from_toml_dict(tomllib.loads(toml_text))
    config_store.save_monarch(db_path, cfg, replace_collections=True)


@pytest.fixture
def single_ledger(tmp_path):
    """A default ledger file + the ledgers list for a Context."""
    ledger = tmp_path / "main.beancount"
    ledger.write_text("")
    return ledger, [{"name": "default", "path": ledger}]


@pytest.fixture
def invoicing_ctx(tmp_path):
    """Context (obj) with a seeded invoicing config matching the old fixture.

    Mirrors the previous ``invoicing.toml``: company "Test Co", client "acme"
    (terms 30), services "dev" (hours, 150.0) and "hosting" (flat, 50.0).
    accounting_path "." / invoice_output "invoices" / next_invoice_number 1.
    """
    ledger = tmp_path / "main.beancount"
    ledger.write_text("")
    obj = _make_context(tmp_path, ledgers=[{"name": "default", "path": ledger}])
    _seed_invoicing(
        obj.db_path,
        'accounting_path = "."\n'
        'invoice_output = "invoices"\n'
        'next_invoice_number = 1\n\n'
        '[company]\nname = "Test Co"\naddress = "123 Main"\n\n'
        '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
        '[services.dev]\ndisplay_name = "Development"\nrate = 150.0\ntype = "hours"\n'
        'income_account = "Income:Dev"\n\n'
        '[services.hosting]\ndisplay_name = "Hosting"\nrate = 50.0\ntype = "flat"\n'
        'income_account = "Income:Hosting"\n',
    )
    return obj


class TestResolve:
    def test_relative(self, tmp_path):
        assert _resolve(tmp_path, "foo/bar.txt") == tmp_path / "foo/bar.txt"

    def test_absolute(self, tmp_path):
        assert _resolve(tmp_path, "/absolute/path") == Path("/absolute/path")


class TestWorkCommands:
    def test_work_add_and_list(self, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        obj = _make_context(tmp_path, ledgers=ledgers)
        result = _invoke(runner, ["work", "add",
            "-d", "2026-03-01", "-c", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["id"] == 1

        result = _invoke(runner, ["work", "list"], tmp_path=tmp_path, obj=obj)
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["count"] == 1
        assert output["entries"][0]["client"] == "acme"
        assert output["entries"][0]["qty"] == 8

    def test_work_remove(self, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        obj = _make_context(tmp_path, ledgers=ledgers)
        _invoke(runner, ["work", "add",
            "-d", "2026-03-01", "-c", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        result = _invoke(runner, ["work", "remove", "1"], tmp_path=tmp_path, obj=obj)
        assert result.exit_code == 0
        assert "Removed" in json.loads(result.output)["message"]

    def test_work_update(self, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        obj = _make_context(tmp_path, ledgers=ledgers)
        _invoke(runner, ["work", "add",
            "-d", "2026-03-01", "-c", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)

        result = _invoke(runner, ["work", "update", "1", "-q", "10"],
            tmp_path=tmp_path, obj=obj)
        assert result.exit_code == 0

        result = _invoke(runner, ["work", "list"], tmp_path=tmp_path, obj=obj)
        output = json.loads(result.output)
        assert output["entries"][0]["qty"] == 10

    def test_work_list_uninvoiced(self, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        obj = _make_context(tmp_path, ledgers=ledgers)
        _invoke(runner, ["work", "add",
            "-d", "2026-03-01", "-c", "acme", "-s", "dev", "-q", "8"],
            tmp_path=tmp_path, obj=obj)
        _invoke(runner, ["work", "add",
            "-d", "2026-03-02", "-c", "acme", "-s", "dev", "-q", "4"],
            tmp_path=tmp_path, obj=obj)

        result = _invoke(runner, ["work", "list", "--uninvoiced"],
            tmp_path=tmp_path, obj=obj)
        output = json.loads(result.output)
        assert output["count"] == 2


class TestListCommand:
    def test_list_with_config(self, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        result = _invoke(runner, ["list"], tmp_path=tmp_path, ledgers=ledgers)
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["ledger_count"] == 1

    def test_list_no_config(self, runner):
        # No injected Context with users → the group refuses to run.
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 1


class TestCheckCommand:
    @patch("istota.money.core.ledger.run_bean_check")
    def test_check_success(self, mock_check, runner, tmp_path, single_ledger):
        ledger, ledgers = single_ledger
        ledger.write_text("content")
        mock_check.return_value = (True, [])
        result = _invoke(runner, ["check"], tmp_path=tmp_path, ledgers=ledgers)
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"


class TestBalancesCommand:
    @patch("istota.money.core.ledger.run_bean_query")
    def test_balances(self, mock_query, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        mock_query.return_value = [{"account": "Assets:Bank", "sum(position)": "1000 USD"}]
        result = _invoke(runner, ["balances"], tmp_path=tmp_path, ledgers=ledgers)
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["account_count"] == 1


class TestQueryCommand:
    @patch("istota.money.core.ledger.run_bean_query")
    def test_query(self, mock_query, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        mock_query.return_value = []
        result = _invoke(runner, ["query", "SELECT * LIMIT 1"],
            tmp_path=tmp_path, ledgers=ledgers)
        assert result.exit_code == 0


class TestReportCommand:
    @patch("istota.money.core.ledger.run_bean_query")
    def test_income_statement(self, mock_query, runner, tmp_path, single_ledger):
        _, ledgers = single_ledger
        mock_query.return_value = []
        result = _invoke(runner, ["report", "income-statement"],
            tmp_path=tmp_path, ledgers=ledgers)
        assert result.exit_code == 0


class TestWashSalesCommand:
    @patch("istota.money.core.ledger.run_bean_query")
    def test_wash_sales(self, mock_query, runner, tmp_path, single_ledger, monkeypatch):
        # wash-sales is experimental — operator must enable money_wash_sales
        monkeypatch.setenv("ISTOTA_EXPERIMENTAL_FEATURES", "money_wash_sales")
        _, ledgers = single_ledger
        mock_query.return_value = []
        result = _invoke(runner, ["wash-sales"], tmp_path=tmp_path, ledgers=ledgers)
        assert result.exit_code == 0


class TestInvoiceCreate:
    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_service_creates_db_entries(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["invoice_number"] == "INV-000001"
        assert output["total"] == 1200.0

        # Verify work entries exist in DB with invoice number assigned
        result = _invoke(runner, ["work", "list", "--invoiced"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["count"] == 1
        entry = output["entries"][0]
        assert entry["client"] == "acme"
        assert entry["service"] == "dev"
        assert entry["qty"] == 8
        assert entry["invoice"] == "INV-000001"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_manual_item_creates_db_entries(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, [
            "invoice", "create", "acme", "--item", '"Custom work" 500',
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["total"] == 500.0

        result = _invoke(runner, ["work", "list", "--invoiced"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["count"] == 1
        entry = output["entries"][0]
        assert entry["service"] == "_manual"
        assert entry["amount"] == 500.0
        assert entry["invoice"] == "INV-000001"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_mixed_service_and_manual(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "4",
            "--item", '"Travel expenses" 200',
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["total"] == 800.0  # 4 * 150 + 200

        result = _invoke(runner, ["work", "list", "--invoiced"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["count"] == 2

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_visible_in_invoice_list(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)

        result = _invoke(runner, ["invoice", "list"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["invoice_count"] == 1
        assert output["invoices"][0]["invoice_number"] == "INV-000001"
        assert output["invoices"][0]["total"] == 1200.0
        assert output["invoices"][0]["status"] == "outstanding"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_increments_invoice_number(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "hosting",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        # The counter is now DB-backed (persist_next_invoice_number writes to
        # the DB when invoicing data exists there), not the old TOML file.
        assert config_store.load_invoicing(invoicing_ctx.db_path).next_invoice_number == 2

    def test_unknown_client_error(self, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, [
            "invoice", "create", "nonexistent", "-s", "dev", "-q", "1",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "nonexistent" in output["error"]

    def test_unknown_service_error(self, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, [
            "invoice", "create", "acme", "-s", "nonexistent", "-q", "1",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "nonexistent" in output["error"]

    def test_no_items_error(self, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, [
            "invoice", "create", "acme",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "No line items" in output["error"]


class TestInvoicePaid:
    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_ledger_posting_false_skips_post(self, mock_pdf, runner, tmp_path):
        """When client has ledger_posting = false, invoice paid skips ledger entry."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        obj = _make_context(tmp_path, ledgers=[{"name": "default", "path": ledger}])
        _seed_invoicing(
            obj.db_path,
            'accounting_path = "."\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "Test Co"\n\n'
            '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
            '[clients.acme.invoicing]\nledger_posting = false\n\n'
            '[services.dev]\ndisplay_name = "Development"\nrate = 150.0\ntype = "hours"\n'
            'income_account = "Income:Dev"\n',
        )

        # Create an invoice
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=obj)

        # Record payment — should NOT write to ledger
        result = _invoke(runner, [
            "invoice", "paid", "INV-000001", "-d", "2026-04-15",
        ], tmp_path=tmp_path, obj=obj)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["no_post"] is True

        # Ledger should remain empty
        assert ledger.read_text() == ""

        # Invoice should be marked paid
        result = _invoke(runner, ["invoice", "list", "--all"],
            tmp_path=tmp_path, obj=obj)
        output = json.loads(result.output)
        assert output["invoices"][0]["status"] == "paid"


class TestInvoiceVoid:
    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_unpaid_invoice(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        # Create an invoice
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)

        result = _invoke(runner, ["invoice", "void", "INV-000001"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["entries_voided"] == 1
        assert output["was_paid"] is False

        # Verify entry is now uninvoiced
        result = _invoke(runner, ["work", "list", "--uninvoiced"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["count"] == 1

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_paid_invoice_blocked(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        # Mark paid (skip ledger posting since no real ledger)
        _invoke(runner, [
            "invoice", "paid", "INV-000001", "-d", "2026-04-15", "--no-post",
        ], tmp_path=tmp_path, obj=invoicing_ctx)

        result = _invoke(runner, ["invoice", "void", "INV-000001"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "paid" in output["error"].lower()

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_paid_invoice_with_force(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        _invoke(runner, [
            "invoice", "paid", "INV-000001", "-d", "2026-04-15", "--no-post",
        ], tmp_path=tmp_path, obj=invoicing_ctx)

        result = _invoke(runner, [
            "invoice", "void", "INV-000001", "--force",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["entries_voided"] == 1
        assert output["was_paid"] is True

    def test_void_nonexistent_invoice(self, runner, tmp_path, invoicing_ctx):
        result = _invoke(runner, ["invoice", "void", "INV-999999"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "not found" in output["error"].lower()

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_then_reinvoice(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        # Create and void
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        _invoke(runner, ["invoice", "void", "INV-000001"],
            tmp_path=tmp_path, obj=invoicing_ctx)

        # Entry should be uninvoiced and available for re-invoicing
        result = _invoke(runner, ["work", "list", "--uninvoiced"],
            tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["count"] == 1

        # Invoice number counter was already bumped to 2, so next invoice is INV-000002
        result = _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["invoice_number"] == "INV-000002"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_with_delete_pdf(self, mock_pdf, runner, tmp_path, invoicing_ctx):
        _invoke(runner, [
            "invoice", "create", "acme", "-s", "dev", "-q", "8",
        ], tmp_path=tmp_path, obj=invoicing_ctx)

        # --delete-pdf should not crash (was broken: 2-tuple vs 3-tuple unpack)
        result = _invoke(runner, [
            "invoice", "void", "INV-000001", "--delete-pdf",
        ], tmp_path=tmp_path, obj=invoicing_ctx)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["entries_voided"] == 1


class TestSyncMonarchProfiles:
    def test_sync_no_ledger_calls_sync_all_profiles(self, runner, tmp_path):
        """sync-monarch without --ledger calls sync_all_profiles."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        obj = _make_context(tmp_path, ledgers=[{"name": "default", "path": ledger}])
        _seed_monarch(
            obj.db_path,
            '[monarch]\nsession_id = "sid"\ncsrftoken = "csrf"\n\n'
            '[monarch.sync]\nlookback_days = 30\n\n'
            '[monarch.profiles.default]\nledger = "default"\n',
        )

        with patch("istota.money.core.transactions.sync_all_profiles") as mock_sync:
            mock_sync.return_value = {"status": "ok", "message": "test"}
            result = _invoke(runner, ["sync-monarch"], tmp_path=tmp_path, obj=obj)
            assert result.exit_code == 0, result.output
            mock_sync.assert_called_once()

    def test_sync_with_ledger_and_profiles(self, runner, tmp_path):
        """sync-monarch --ledger syncs only matching profile."""
        biz_ledger = tmp_path / "biz.beancount"
        biz_ledger.write_text("")
        obj = _make_context(tmp_path, ledgers=[{"name": "biz", "path": biz_ledger}])
        _seed_monarch(
            obj.db_path,
            '[monarch]\nsession_id = "sid"\ncsrftoken = "csrf"\n\n'
            '[monarch.sync]\nlookback_days = 30\n\n'
            '[monarch.profiles.business]\n'
            'ledger = "biz"\n\n'
            '[monarch.profiles.business.tags]\n'
            'include = ["business"]\n',
        )

        with patch("istota.money.core.transactions.fetch_monarch_transactions") as mock_fetch:
            mock_fetch.return_value = []
            with patch("istota.money.core.transactions.sync_monarch") as mock_sync:
                mock_sync.return_value = {"status": "ok", "transaction_count": 0}
                result = _invoke(runner, ["sync-monarch", "-l", "biz"],
                    tmp_path=tmp_path, obj=obj)
                assert result.exit_code == 0, result.output
                mock_sync.assert_called_once()
                call_kwargs = mock_sync.call_args
                assert call_kwargs[1]["profile"] == "business"


class TestRunScheduled:
    """End-to-end CliRunner coverage for `run-scheduled` — mirrors
    `tests/test_feeds_cli.py::TestPoll.test_run_scheduled_polls_due_feeds`
    so wiring regressions (TypeError from pass-decorator collisions, etc.)
    are caught at the same shape of layer.
    """

    _MONARCH_TOML = (
        '[monarch]\nsession_id = "sid"\ncsrftoken = "csrf"\n\n'
        '[monarch.sync]\nlookback_days = 30\n\n'
        '[monarch.profiles.default]\nledger = "default"\n'
    )

    def test_run_scheduled_no_monarch_no_invoicing(self, runner, tmp_path):
        """No monarch_config and no invoicing — succeeds with the
        no-invoicing message, doesn't blow up on wiring."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        result = _invoke(runner, ["run-scheduled"], tmp_path=tmp_path,
            ledgers=[{"name": "default", "path": ledger}])
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "ok"
        assert "No invoicing config" in out["message"]
        assert "monarch" not in out

    def test_run_scheduled_skip_monarch(self, runner, tmp_path):
        """--skip-monarch path — succeeds even with monarch_config set."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        # monarch_config_path set (so the monarch branch would normally fire),
        # but --skip-monarch suppresses it.
        obj = _make_context(tmp_path, ledgers=[{"name": "default", "path": ledger}],
            monarch_config_path=tmp_path / "monarch.toml")
        _seed_monarch(obj.db_path, self._MONARCH_TOML)
        result = _invoke(runner, ["run-scheduled", "--skip-monarch"],
            tmp_path=tmp_path, obj=obj)
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "ok"
        assert "monarch" not in out

    def test_run_scheduled_rolls_up_monarch_error(self, runner, tmp_path):
        """ISSUE-069: a monarch sync failure must surface as outer
        envelope status='error' so the scheduler's JSON-error detector
        and any alerting layer fire, instead of nesting the failure
        invisibly under out['monarch']."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        obj = _make_context(tmp_path, ledgers=[{"name": "default", "path": ledger}],
            monarch_config_path=tmp_path / "monarch.toml")
        _seed_monarch(obj.db_path, self._MONARCH_TOML)

        with patch("istota.money.core.transactions.sync_all_profiles") as mock_sync:
            mock_sync.return_value = {"status": "error", "error": "auth failed"}
            result = _invoke(runner, ["run-scheduled"], tmp_path=tmp_path, obj=obj)

        assert result.exit_code == 1, result.output
        out = json.loads(result.output)
        assert out["status"] == "error"
        assert "auth failed" in out["error"]
        assert out["monarch"]["status"] == "error"

    def test_run_scheduled_partial_error_on_per_profile_failure(self, runner, tmp_path):
        """When sync_all_profiles returns ok but one of its profiles
        failed, surface as partial_error so logs reflect the issue."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        obj = _make_context(tmp_path, ledgers=[{"name": "default", "path": ledger}],
            monarch_config_path=tmp_path / "monarch.toml")
        _seed_monarch(obj.db_path, self._MONARCH_TOML)

        with patch("istota.money.core.transactions.sync_all_profiles") as mock_sync:
            mock_sync.return_value = {
                "status": "ok",
                "profiles": [
                    {"name": "personal", "ledger": "main", "status": "ok"},
                    {"name": "business", "ledger": "biz", "status": "error", "error": "ledger not found"},
                ],
            }
            result = _invoke(runner, ["run-scheduled"], tmp_path=tmp_path, obj=obj)

        # partial_error is not a hard failure — exit 0, but the envelope
        # records the per-profile breakage for logs/alerting.
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "partial_error"
        assert "business" in out["monarch_errors"]


class TestHelp:
    def test_main_help(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Money" in result.output

    def test_work_help(self, runner, tmp_path):
        # Descending into a subgroup runs the root cli() callback first, which
        # requires an injected Context with users.
        obj = _make_context(tmp_path)
        result = runner.invoke(cli, ["-u", "default", "work", "--help"], obj=obj)
        assert result.exit_code == 0
        assert "add" in result.output
        assert "list" in result.output
        assert "remove" in result.output
        assert "update" in result.output

    def test_invoice_help(self, runner, tmp_path):
        obj = _make_context(tmp_path)
        result = runner.invoke(cli, ["-u", "default", "invoice", "--help"], obj=obj)
        assert result.exit_code == 0
        assert "generate" in result.output
        assert "list" in result.output
        assert "paid" in result.output
        assert "create" in result.output
        assert "void" in result.output


class TestInjectedContext:
    """Callers (the istota money skill) build a Context and inject via obj=."""

    def test_pre_built_context_skips_load_context(self, runner, tmp_path):
        """When obj has users, the cli() group does not try to load a config file."""
        from istota.money.cli import Context, UserContext, cli

        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        obj = Context()
        obj.users["alice"] = UserContext(
            data_dir=tmp_path,
            ledgers=[{"name": "main", "path": ledger}],
        )
        obj.activate_user("alice")

        # No -c flag, no MONEY_CONFIG env var — load_context would normally
        # find nothing, but the injected obj is preferred.
        result = runner.invoke(cli, ["-u", "alice", "list"], obj=obj)
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["ledger_count"] == 1


class TestBackfillIds:
    def test_backfill_ids_command(self, runner, tmp_path):
        ledger = tmp_path / "ledgers" / "main.beancount"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(
            "2024-01-01 open Assets:Bank:Checking\n"
            "2024-01-01 open Expenses:Food\n\n"
            '2024-02-01 * "Acme" "Coffee"\n'
            "  Expenses:Food   5.00 USD\n"
            "  Assets:Bank:Checking\n"
        )
        result = _invoke(runner, ["backfill-ids"], tmp_path=tmp_path,
            ledgers=[{"name": "default", "path": ledger}])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["stamped"] == 1
        assert 'id: "' in ledger.read_text()


class TestEditTransactionCLI:
    def test_edit_transaction_command(self, runner, tmp_path):
        ledger = tmp_path / "ledgers" / "main.beancount"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(
            "2024-01-01 open Assets:Bank:Checking\n"
            "2024-01-01 open Expenses:Food:Coffee\n"
            "2024-01-01 open Expenses:Food:Restaurants\n\n"
            '2024-02-01 * "Acme" "Coffee"\n'
            '  id: "txn-1"\n'
            "  Expenses:Food:Coffee   5.00 USD\n"
            "  Assets:Bank:Checking\n"
        )
        result = _invoke(runner, [
            "edit-transaction", "--id", "txn-1",
            "--old-account", "Expenses:Food:Coffee", "--old-position", "5.00 USD",
            "--account", "Expenses:Food:Restaurants",
        ], tmp_path=tmp_path, ledgers=[{"name": "default", "path": ledger}])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert "Expenses:Food:Restaurants" in ledger.read_text()

    def test_edit_transaction_not_found_exits_nonzero(self, runner, tmp_path):
        ledger = tmp_path / "ledgers" / "main.beancount"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("2024-01-01 open Assets:Bank:Checking\n")
        result = _invoke(runner, [
            "edit-transaction", "--id", "nope",
            "--account", "Expenses:X",
        ], tmp_path=tmp_path, ledgers=[{"name": "default", "path": ledger}])
        assert result.exit_code == 1
        assert json.loads(result.output)["status"] == "error"
