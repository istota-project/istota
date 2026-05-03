"""Tests for money.cli module."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from istota.money.cli import cli, _resolve


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def config_file(tmp_path):
    """Create a config.toml with data_dir pointing to tmp_path."""
    ledger_dir = tmp_path / "ledgers"
    ledger_dir.mkdir()
    ledger = ledger_dir / "main.beancount"
    ledger.write_text("")
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{tmp_path}"\n\n'
        f'[[ledgers]]\nname = "default"\npath = "ledgers/main.beancount"\n'
    )
    return config, ledger


class TestResolve:
    def test_relative(self, tmp_path):
        assert _resolve(tmp_path, "foo/bar.txt") == tmp_path / "foo/bar.txt"

    def test_absolute(self, tmp_path):
        assert _resolve(tmp_path, "/absolute/path") == Path("/absolute/path")


class TestConfigLoading:
    def test_data_dir_resolves_ledger_paths(self, runner, config_file):
        config, _ = config_file
        result = runner.invoke(cli, ["-c", str(config), "list"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"

    def test_default_db_path(self, runner, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text(
            f'data_dir = "{tmp_path}"\n\n'
            f'[[ledgers]]\nname = "default"\npath = "{tmp_path}/ledger.beancount"\n'
        )
        result = runner.invoke(cli, ["-c", str(config), "list"])
        assert result.exit_code == 0


class TestSecretsFile:
    def test_secrets_file_loaded_from_config(self, runner, tmp_path):
        """secrets_file in config.toml is loaded and stored on context."""
        secrets = tmp_path / "secrets.toml"
        secrets.write_text('[monarch]\nsession_token = "secret-123"\n')
        config = tmp_path / "config.toml"
        config.write_text(
            f'data_dir = "{tmp_path}"\n'
            f'secrets_file = "{secrets}"\n\n'
            f'[[ledgers]]\nname = "default"\npath = "{tmp_path}/main.beancount"\n'
        )
        (tmp_path / "main.beancount").write_text("")
        result = runner.invoke(cli, ["-c", str(config), "list"])
        assert result.exit_code == 0

    def test_secrets_file_env_var_override(self, runner, tmp_path):
        """MONEY_SECRETS_FILE env var overrides config."""
        secrets = tmp_path / "env-secrets.toml"
        secrets.write_text('[monarch]\nsession_token = "env-secret"\n')
        config = tmp_path / "config.toml"
        config.write_text(
            f'data_dir = "{tmp_path}"\n\n'
            f'[[ledgers]]\nname = "default"\npath = "{tmp_path}/main.beancount"\n'
        )
        (tmp_path / "main.beancount").write_text("")
        result = runner.invoke(
            cli, ["-c", str(config), "list"],
            env={"MONEY_SECRETS_FILE": str(secrets)},
        )
        assert result.exit_code == 0

    def test_default_secrets_path_not_required(self, runner, config_file):
        """Missing /etc/moneyman/secrets.toml is silently ignored."""
        config, _ = config_file
        result = runner.invoke(cli, ["-c", str(config), "list"])
        assert result.exit_code == 0

    def test_secrets_passed_to_monarch_config(self, runner, tmp_path):
        """Secrets overlay is passed through to monarch config parsing."""
        secrets = tmp_path / "secrets.toml"
        secrets.write_text('[monarch]\nsession_token = "from-secrets"\n')

        monarch_config = tmp_path / "monarch.toml"
        monarch_config.write_text(
            '[monarch]\nemail = "test@test.com"\n\n'
            '[monarch.sync]\nlookback_days = 7\n'
        )

        config = tmp_path / "config.toml"
        config.write_text(
            f'data_dir = "{tmp_path}"\n'
            f'secrets_file = "{secrets}"\n'
            f'monarch_config = "monarch.toml"\n\n'
            f'[[ledgers]]\nname = "default"\npath = "{tmp_path}/main.beancount"\n'
        )
        (tmp_path / "main.beancount").write_text("")

        # We can verify the secrets are loaded by attempting a sync
        # (it will fail at the API call, but we can patch to check the config)
        with patch("istota.money.core.transactions.sync_monarch") as mock_sync:
            mock_sync.return_value = {"status": "ok", "message": "test"}
            result = runner.invoke(cli, ["-c", str(config), "sync-monarch"])
            assert result.exit_code == 0
            # Check that parse_monarch_config was called and secrets were overlaid
            call_args = mock_sync.call_args
            monarch_cfg = call_args[1].get("config") or call_args[0][1]
            assert monarch_cfg.credentials.session_token == "from-secrets"
            assert monarch_cfg.credentials.email == "test@test.com"


class TestWorkCommands:
    def test_work_add_and_list(self, runner, config_file):
        config, _ = config_file
        result = runner.invoke(cli, ["-c", str(config), "work", "add",
            "-d", "2026-03-01", "-c", "acme", "-s", "dev", "-q", "8"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["id"] == 1

        result = runner.invoke(cli, ["-c", str(config), "work", "list"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["count"] == 1
        assert output["entries"][0]["client"] == "acme"
        assert output["entries"][0]["qty"] == 8

    def test_work_remove(self, runner, config_file):
        config, _ = config_file
        runner.invoke(cli, ["-c", str(config), "work", "add",
            "-d", "2026-03-01", "-c", "acme", "-s", "dev", "-q", "8"])

        result = runner.invoke(cli, ["-c", str(config), "work", "remove", "1"])
        assert result.exit_code == 0
        assert "Removed" in json.loads(result.output)["message"]

    def test_work_update(self, runner, config_file):
        config, _ = config_file
        runner.invoke(cli, ["-c", str(config), "work", "add",
            "-d", "2026-03-01", "-c", "acme", "-s", "dev", "-q", "8"])

        result = runner.invoke(cli, ["-c", str(config), "work", "update", "1", "-q", "10"])
        assert result.exit_code == 0

        result = runner.invoke(cli, ["-c", str(config), "work", "list"])
        output = json.loads(result.output)
        assert output["entries"][0]["qty"] == 10

    def test_work_list_uninvoiced(self, runner, config_file):
        config, _ = config_file
        runner.invoke(cli, ["-c", str(config), "work", "add",
            "-d", "2026-03-01", "-c", "acme", "-s", "dev", "-q", "8"])
        runner.invoke(cli, ["-c", str(config), "work", "add",
            "-d", "2026-03-02", "-c", "acme", "-s", "dev", "-q", "4"])

        result = runner.invoke(cli, ["-c", str(config), "work", "list", "--uninvoiced"])
        output = json.loads(result.output)
        assert output["count"] == 2


class TestListCommand:
    def test_list_with_config(self, runner, config_file):
        config, _ = config_file
        result = runner.invoke(cli, ["-c", str(config), "list"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["ledger_count"] == 1

    def test_list_no_config(self, runner):
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 1


class TestCheckCommand:
    @patch("istota.money.core.ledger.run_bean_check")
    def test_check_success(self, mock_check, runner, config_file):
        config, ledger = config_file
        ledger.write_text("content")
        mock_check.return_value = (True, [])
        result = runner.invoke(cli, ["-c", str(config), "check"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"


class TestBalancesCommand:
    @patch("istota.money.core.ledger.run_bean_query")
    def test_balances(self, mock_query, runner, config_file):
        config, _ = config_file
        mock_query.return_value = [{"account": "Assets:Bank", "sum(position)": "1000 USD"}]
        result = runner.invoke(cli, ["-c", str(config), "balances"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["account_count"] == 1


class TestQueryCommand:
    @patch("istota.money.core.ledger.run_bean_query")
    def test_query(self, mock_query, runner, config_file):
        config, _ = config_file
        mock_query.return_value = []
        result = runner.invoke(cli, ["-c", str(config), "query", "SELECT * LIMIT 1"])
        assert result.exit_code == 0


class TestReportCommand:
    @patch("istota.money.core.ledger.run_bean_query")
    def test_income_statement(self, mock_query, runner, config_file):
        config, _ = config_file
        mock_query.return_value = []
        result = runner.invoke(cli, ["-c", str(config), "report", "income-statement"])
        assert result.exit_code == 0


class TestWashSalesCommand:
    @patch("istota.money.core.ledger.run_bean_query")
    def test_wash_sales(self, mock_query, runner, config_file):
        config, _ = config_file
        mock_query.return_value = []
        result = runner.invoke(cli, ["-c", str(config), "wash-sales"])
        assert result.exit_code == 0


@pytest.fixture
def invoicing_setup(tmp_path):
    """Create config.toml + invoicing.toml for invoice create tests."""
    invoicing = tmp_path / "invoicing.toml"
    invoicing.write_text(
        'accounting_path = "."\n'
        'invoice_output = "invoices"\n'
        'next_invoice_number = 1\n\n'
        '[company]\nname = "Test Co"\naddress = "123 Main"\n\n'
        '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
        '[services.dev]\ndisplay_name = "Development"\nrate = 150.0\ntype = "hours"\n'
        'income_account = "Income:Dev"\n\n'
        '[services.hosting]\ndisplay_name = "Hosting"\nrate = 50.0\ntype = "flat"\n'
        'income_account = "Income:Hosting"\n'
    )
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{tmp_path}"\n'
        f'invoicing_config = "invoicing.toml"\n\n'
        f'[[ledgers]]\nname = "default"\npath = "{tmp_path}/main.beancount"\n'
    )
    (tmp_path / "main.beancount").write_text("")
    return config, invoicing


class TestInvoiceCreate:
    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_service_creates_db_entries(self, mock_pdf, runner, invoicing_setup):
        config, _ = invoicing_setup
        result = runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "acme",
            "-s", "dev", "-q", "8",
        ])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["invoice_number"] == "INV-000001"
        assert output["total"] == 1200.0

        # Verify work entries exist in DB with invoice number assigned
        result = runner.invoke(cli, ["-c", str(config), "work", "list", "--invoiced"])
        output = json.loads(result.output)
        assert output["count"] == 1
        entry = output["entries"][0]
        assert entry["client"] == "acme"
        assert entry["service"] == "dev"
        assert entry["qty"] == 8
        assert entry["invoice"] == "INV-000001"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_manual_item_creates_db_entries(self, mock_pdf, runner, invoicing_setup):
        config, _ = invoicing_setup
        result = runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "acme",
            "--item", '"Custom work" 500',
        ])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["total"] == 500.0

        result = runner.invoke(cli, ["-c", str(config), "work", "list", "--invoiced"])
        output = json.loads(result.output)
        assert output["count"] == 1
        entry = output["entries"][0]
        assert entry["service"] == "_manual"
        assert entry["amount"] == 500.0
        assert entry["invoice"] == "INV-000001"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_mixed_service_and_manual(self, mock_pdf, runner, invoicing_setup):
        config, _ = invoicing_setup
        result = runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "acme",
            "-s", "dev", "-q", "4",
            "--item", '"Travel expenses" 200',
        ])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["total"] == 800.0  # 4 * 150 + 200

        result = runner.invoke(cli, ["-c", str(config), "work", "list", "--invoiced"])
        output = json.loads(result.output)
        assert output["count"] == 2

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_visible_in_invoice_list(self, mock_pdf, runner, invoicing_setup):
        config, _ = invoicing_setup
        runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "acme",
            "-s", "dev", "-q", "8",
        ])

        result = runner.invoke(cli, ["-c", str(config), "invoice", "list"])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["invoice_count"] == 1
        assert output["invoices"][0]["invoice_number"] == "INV-000001"
        assert output["invoices"][0]["total"] == 1200.0
        assert output["invoices"][0]["status"] == "outstanding"

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_increments_invoice_number(self, mock_pdf, runner, invoicing_setup):
        config, invoicing = invoicing_setup
        runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "acme",
            "-s", "hosting",
        ])
        assert "next_invoice_number = 2" in invoicing.read_text()

    def test_unknown_client_error(self, runner, invoicing_setup):
        config, _ = invoicing_setup
        result = runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "nonexistent",
            "-s", "dev", "-q", "1",
        ])
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "nonexistent" in output["error"]

    def test_unknown_service_error(self, runner, invoicing_setup):
        config, _ = invoicing_setup
        result = runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "acme",
            "-s", "nonexistent", "-q", "1",
        ])
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "nonexistent" in output["error"]

    def test_no_items_error(self, runner, invoicing_setup):
        config, _ = invoicing_setup
        result = runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "acme",
        ])
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "No line items" in output["error"]


class TestInvoicePaid:
    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_ledger_posting_false_skips_post(self, mock_pdf, runner, tmp_path):
        """When client has ledger_posting = false, invoice paid skips ledger entry."""
        invoicing = tmp_path / "invoicing.toml"
        invoicing.write_text(
            'accounting_path = "."\n'
            'invoice_output = "invoices"\n'
            'next_invoice_number = 1\n\n'
            '[company]\nname = "Test Co"\n\n'
            '[clients.acme]\nname = "Acme Corp"\nterms = 30\n\n'
            '[clients.acme.invoicing]\nledger_posting = false\n\n'
            '[services.dev]\ndisplay_name = "Development"\nrate = 150.0\ntype = "hours"\n'
            'income_account = "Income:Dev"\n'
        )
        config = tmp_path / "config.toml"
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        config.write_text(
            f'data_dir = "{tmp_path}"\n'
            f'invoicing_config = "invoicing.toml"\n\n'
            f'[[ledgers]]\nname = "default"\npath = "{ledger}"\n'
        )

        # Create an invoice
        runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "acme",
            "-s", "dev", "-q", "8",
        ])

        # Record payment — should NOT write to ledger
        result = runner.invoke(cli, [
            "-c", str(config), "invoice", "paid", "INV-000001",
            "-d", "2026-04-15",
        ])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["no_post"] is True

        # Ledger should remain empty
        assert ledger.read_text() == ""

        # Invoice should be marked paid
        result = runner.invoke(cli, ["-c", str(config), "invoice", "list", "--all"])
        output = json.loads(result.output)
        assert output["invoices"][0]["status"] == "paid"


class TestInvoiceVoid:
    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_unpaid_invoice(self, mock_pdf, runner, invoicing_setup):
        config, _ = invoicing_setup
        # Create an invoice
        runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "acme",
            "-s", "dev", "-q", "8",
        ])

        result = runner.invoke(cli, ["-c", str(config), "invoice", "void", "INV-000001"])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["entries_voided"] == 1
        assert output["was_paid"] is False

        # Verify entry is now uninvoiced
        result = runner.invoke(cli, ["-c", str(config), "work", "list", "--uninvoiced"])
        output = json.loads(result.output)
        assert output["count"] == 1

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_paid_invoice_blocked(self, mock_pdf, runner, invoicing_setup):
        config, _ = invoicing_setup
        runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "acme",
            "-s", "dev", "-q", "8",
        ])
        # Mark paid (skip ledger posting since no real ledger)
        runner.invoke(cli, [
            "-c", str(config), "invoice", "paid", "INV-000001",
            "-d", "2026-04-15", "--no-post",
        ])

        result = runner.invoke(cli, ["-c", str(config), "invoice", "void", "INV-000001"])
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "paid" in output["error"].lower()

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_paid_invoice_with_force(self, mock_pdf, runner, invoicing_setup):
        config, _ = invoicing_setup
        runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "acme",
            "-s", "dev", "-q", "8",
        ])
        runner.invoke(cli, [
            "-c", str(config), "invoice", "paid", "INV-000001",
            "-d", "2026-04-15", "--no-post",
        ])

        result = runner.invoke(cli, [
            "-c", str(config), "invoice", "void", "INV-000001", "--force",
        ])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["entries_voided"] == 1
        assert output["was_paid"] is True

    def test_void_nonexistent_invoice(self, runner, invoicing_setup):
        config, _ = invoicing_setup
        result = runner.invoke(cli, ["-c", str(config), "invoice", "void", "INV-999999"])
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "not found" in output["error"].lower()

    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_then_reinvoice(self, mock_pdf, runner, invoicing_setup):
        config, invoicing = invoicing_setup
        # Create and void
        runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "acme",
            "-s", "dev", "-q", "8",
        ])
        runner.invoke(cli, ["-c", str(config), "invoice", "void", "INV-000001"])

        # Entry should be uninvoiced and available for re-invoicing
        result = runner.invoke(cli, ["-c", str(config), "work", "list", "--uninvoiced"])
        output = json.loads(result.output)
        assert output["count"] == 1

        # Invoice number counter was already bumped to 2, so next invoice is INV-000002
        result = runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "acme",
            "-s", "dev", "-q", "8",
        ])
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["invoice_number"] == "INV-000002"


    @patch("istota.money.core.invoicing.generate_invoice_pdf")
    def test_void_with_delete_pdf(self, mock_pdf, runner, invoicing_setup):
        config, _ = invoicing_setup
        runner.invoke(cli, [
            "-c", str(config), "invoice", "create", "acme",
            "-s", "dev", "-q", "8",
        ])

        # --delete-pdf should not crash (was broken: 2-tuple vs 3-tuple unpack)
        result = runner.invoke(cli, [
            "-c", str(config), "invoice", "void", "INV-000001", "--delete-pdf",
        ])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["entries_voided"] == 1


class TestSyncMonarchProfiles:
    def test_sync_no_ledger_calls_sync_all_profiles(self, runner, tmp_path):
        """sync-monarch without --ledger calls sync_all_profiles."""
        monarch_config = tmp_path / "monarch.toml"
        monarch_config.write_text(
            '[monarch]\nsession_token = "tok"\n\n'
            '[monarch.sync]\nlookback_days = 30\n'
        )
        config = tmp_path / "config.toml"
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        config.write_text(
            f'data_dir = "{tmp_path}"\n'
            f'monarch_config = "monarch.toml"\n\n'
            f'[[ledgers]]\nname = "default"\npath = "{ledger}"\n'
        )

        with patch("istota.money.core.transactions.sync_all_profiles") as mock_sync:
            mock_sync.return_value = {"status": "ok", "message": "test"}
            result = runner.invoke(cli, ["-c", str(config), "sync-monarch"])
            assert result.exit_code == 0, result.output
            mock_sync.assert_called_once()

    def test_sync_with_ledger_and_profiles(self, runner, tmp_path):
        """sync-monarch --ledger syncs only matching profile."""
        monarch_config = tmp_path / "monarch.toml"
        monarch_config.write_text(
            '[monarch]\nsession_token = "tok"\n\n'
            '[monarch.sync]\nlookback_days = 30\n\n'
            '[monarch.profiles.business]\n'
            'ledger = "biz"\n\n'
            '[monarch.profiles.business.tags]\n'
            'include = ["business"]\n'
        )
        biz_ledger = tmp_path / "biz.beancount"
        biz_ledger.write_text("")
        config = tmp_path / "config.toml"
        config.write_text(
            f'data_dir = "{tmp_path}"\n'
            f'monarch_config = "monarch.toml"\n\n'
            f'[[ledgers]]\nname = "biz"\npath = "{biz_ledger}"\n'
        )

        with patch("istota.money.core.transactions.fetch_monarch_transactions") as mock_fetch:
            mock_fetch.return_value = []
            with patch("istota.money.core.transactions.sync_monarch") as mock_sync:
                mock_sync.return_value = {"status": "ok", "transaction_count": 0}
                result = runner.invoke(cli, ["-c", str(config), "sync-monarch", "-l", "biz"])
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

    def _basic_config(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        config = tmp_path / "config.toml"
        config.write_text(
            f'data_dir = "{tmp_path}"\n\n'
            f'[[ledgers]]\nname = "default"\npath = "{ledger}"\n'
        )
        return config

    def test_run_scheduled_no_monarch_no_invoicing(self, runner, tmp_path):
        """No monarch_config and no invoicing — succeeds with the
        no-invoicing message, doesn't blow up on wiring."""
        config = self._basic_config(tmp_path)
        result = runner.invoke(cli, ["-c", str(config), "run-scheduled"])
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "ok"
        assert "No invoicing config" in out["message"]
        assert "monarch" not in out

    def test_run_scheduled_skip_monarch(self, runner, tmp_path):
        """--skip-monarch path — succeeds even with monarch_config set."""
        monarch_config = tmp_path / "monarch.toml"
        monarch_config.write_text(
            '[monarch]\nsession_token = "tok"\n\n'
            '[monarch.sync]\nlookback_days = 30\n'
        )
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        config = tmp_path / "config.toml"
        config.write_text(
            f'data_dir = "{tmp_path}"\n'
            f'monarch_config = "monarch.toml"\n\n'
            f'[[ledgers]]\nname = "default"\npath = "{ledger}"\n'
        )
        result = runner.invoke(cli, ["-c", str(config), "run-scheduled", "--skip-monarch"])
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "ok"
        assert "monarch" not in out

    def test_run_scheduled_rolls_up_monarch_error(self, runner, tmp_path):
        """ISSUE-069: a monarch sync failure must surface as outer
        envelope status='error' so the scheduler's JSON-error detector
        and any alerting layer fire, instead of nesting the failure
        invisibly under out['monarch']."""
        monarch_config = tmp_path / "monarch.toml"
        monarch_config.write_text(
            '[monarch]\nsession_token = "tok"\n\n'
            '[monarch.sync]\nlookback_days = 30\n'
        )
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        config = tmp_path / "config.toml"
        config.write_text(
            f'data_dir = "{tmp_path}"\n'
            f'monarch_config = "monarch.toml"\n\n'
            f'[[ledgers]]\nname = "default"\npath = "{ledger}"\n'
        )

        with patch("istota.money.core.transactions.sync_all_profiles") as mock_sync:
            mock_sync.return_value = {"status": "error", "error": "auth failed"}
            result = runner.invoke(cli, ["-c", str(config), "run-scheduled"])

        assert result.exit_code == 1, result.output
        out = json.loads(result.output)
        assert out["status"] == "error"
        assert "auth failed" in out["error"]
        assert out["monarch"]["status"] == "error"

    def test_run_scheduled_partial_error_on_per_profile_failure(self, runner, tmp_path):
        """When sync_all_profiles returns ok but one of its profiles
        failed, surface as partial_error so logs reflect the issue."""
        monarch_config = tmp_path / "monarch.toml"
        monarch_config.write_text(
            '[monarch]\nsession_token = "tok"\n\n'
            '[monarch.sync]\nlookback_days = 30\n'
        )
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        config = tmp_path / "config.toml"
        config.write_text(
            f'data_dir = "{tmp_path}"\n'
            f'monarch_config = "monarch.toml"\n\n'
            f'[[ledgers]]\nname = "default"\npath = "{ledger}"\n'
        )

        with patch("istota.money.core.transactions.sync_all_profiles") as mock_sync:
            mock_sync.return_value = {
                "status": "ok",
                "profiles": [
                    {"name": "personal", "ledger": "main", "status": "ok"},
                    {"name": "business", "ledger": "biz", "status": "error", "error": "ledger not found"},
                ],
            }
            result = runner.invoke(cli, ["-c", str(config), "run-scheduled"])

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

    def test_work_help(self, runner):
        result = runner.invoke(cli, ["work", "--help"])
        assert result.exit_code == 0
        assert "add" in result.output
        assert "list" in result.output
        assert "remove" in result.output
        assert "update" in result.output

    def test_invoice_help(self, runner):
        result = runner.invoke(cli, ["invoice", "--help"])
        assert result.exit_code == 0
        assert "generate" in result.output
        assert "list" in result.output
        assert "paid" in result.output
        assert "create" in result.output
        assert "void" in result.output


# =============================================================================
# Multi-user support
# =============================================================================


@pytest.fixture
def multi_user_config(tmp_path):
    """Create a multi-user config with two users."""
    bob_dir = tmp_path / "bob"
    bob_dir.mkdir()
    bob_ledger = bob_dir / "ledgers"
    bob_ledger.mkdir()
    (bob_ledger / "business.beancount").write_text("")

    alice_dir = tmp_path / "alice"
    alice_dir.mkdir()
    alice_ledger = alice_dir / "ledgers"
    alice_ledger.mkdir()
    (alice_ledger / "personal.beancount").write_text("")

    config = tmp_path / "config.toml"
    config.write_text(
        f'[users.bob]\n'
        f'data_dir = "{bob_dir}"\n\n'
        f'[[users.bob.ledgers]]\n'
        f'name = "business"\n'
        f'path = "ledgers/business.beancount"\n\n'
        f'[users.alice]\n'
        f'data_dir = "{alice_dir}"\n\n'
        f'[[users.alice.ledgers]]\n'
        f'name = "personal"\n'
        f'path = "ledgers/personal.beancount"\n'
    )
    return config, bob_dir, alice_dir


class TestMultiUserCli:
    def test_user_flag_activates_correct_user(self, runner, multi_user_config):
        config, bob_dir, alice_dir = multi_user_config
        result = runner.invoke(cli, ["-c", str(config), "-u", "bob", "list"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["ledger_count"] == 1
        assert output["ledgers"][0]["name"] == "business"

    def test_user_flag_alice(self, runner, multi_user_config):
        config, bob_dir, alice_dir = multi_user_config
        result = runner.invoke(cli, ["-c", str(config), "-u", "alice", "list"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["ledgers"][0]["name"] == "personal"

    def test_no_user_flag_multi_user_errors(self, runner, multi_user_config):
        config, _, _ = multi_user_config
        result = runner.invoke(cli, ["-c", str(config), "list"])
        assert result.exit_code == 1
        assert "Multiple users" in result.output

    def test_unknown_user_errors(self, runner, multi_user_config):
        config, _, _ = multi_user_config
        result = runner.invoke(cli, ["-c", str(config), "-u", "nobody", "list"])
        assert result.exit_code != 0
        assert "Unknown user" in result.output or "nobody" in result.output

    def test_single_user_auto_activates(self, runner, tmp_path):
        """Single-user [users] config auto-activates without --user."""
        user_dir = tmp_path / "solo"
        user_dir.mkdir()
        ledger = user_dir / "main.beancount"
        ledger.write_text("")
        config = tmp_path / "config.toml"
        config.write_text(
            f'[users.solo]\n'
            f'data_dir = "{user_dir}"\n\n'
            f'[[users.solo.ledgers]]\n'
            f'name = "main"\n'
            f'path = "{ledger}"\n'
        )
        result = runner.invoke(cli, ["-c", str(config), "list"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["ledger_count"] == 1

    def test_backward_compat_no_users_section(self, runner, config_file):
        """No [users] section works exactly as before."""
        config, _ = config_file
        result = runner.invoke(cli, ["-c", str(config), "list"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["ledger_count"] == 1

    def test_users_command(self, runner, multi_user_config):
        config, bob_dir, alice_dir = multi_user_config
        result = runner.invoke(cli, ["-c", str(config), "users"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["user_count"] == 2
        keys = [u["key"] for u in output["users"]]
        assert "bob" in keys
        assert "alice" in keys

    def test_users_command_single_user(self, runner, config_file):
        config, _ = config_file
        result = runner.invoke(cli, ["-c", str(config), "users"])
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["user_count"] == 1
        assert output["users"][0]["key"] == "default"

    def test_work_isolated_per_user(self, runner, multi_user_config):
        """Work entries are isolated per user (separate data_dirs)."""
        config, _, _ = multi_user_config
        # Add work for bob
        result = runner.invoke(cli, ["-c", str(config), "-u", "bob",
            "work", "add", "-d", "2026-03-01", "-c", "acme", "-s", "dev", "-q", "8"])
        assert result.exit_code == 0

        # Bob sees it
        result = runner.invoke(cli, ["-c", str(config), "-u", "bob", "work", "list"])
        output = json.loads(result.output)
        assert output["count"] == 1

        # Alice does not
        result = runner.invoke(cli, ["-c", str(config), "-u", "alice", "work", "list"])
        output = json.loads(result.output)
        assert output["count"] == 0


class TestMultiUserLoadContext:
    def test_multi_user_context_structure(self, multi_user_config):
        from istota.money.cli import load_context
        config, bob_dir, alice_dir = multi_user_config
        ctx = load_context(config_path=str(config))
        assert len(ctx.users) == 2
        assert "bob" in ctx.users
        assert "alice" in ctx.users
        assert ctx.users["bob"].data_dir == bob_dir
        assert ctx.users["alice"].data_dir == alice_dir

    def test_for_user_returns_independent_copy(self, multi_user_config):
        from istota.money.cli import load_context
        config, bob_dir, alice_dir = multi_user_config
        ctx = load_context(config_path=str(config))
        bob_ctx = ctx.for_user("bob")
        alice_ctx = ctx.for_user("alice")
        assert bob_ctx.data_dir == bob_dir
        assert alice_ctx.data_dir == alice_dir
        assert bob_ctx.data_dir != alice_ctx.data_dir

    def test_for_user_unknown_raises(self, multi_user_config):
        from istota.money.cli import load_context
        config, _, _ = multi_user_config
        ctx = load_context(config_path=str(config))
        with pytest.raises(Exception, match="Unknown user"):
            ctx.for_user("nobody")

    def test_legacy_config_creates_default_user(self, tmp_path):
        from istota.money.cli import load_context
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        config = tmp_path / "config.toml"
        config.write_text(
            f'data_dir = "{tmp_path}"\n\n'
            f'[[ledgers]]\nname = "main"\npath = "{ledger}"\n'
        )
        ctx = load_context(config_path=str(config))
        assert "default" in ctx.users
        assert ctx.active_user == "default"
        assert ctx.has_single_user

    def test_multi_user_not_single(self, multi_user_config):
        from istota.money.cli import load_context
        config, _, _ = multi_user_config
        ctx = load_context(config_path=str(config))
        assert not ctx.has_single_user
        assert sorted(ctx.available_users) == ["alice", "bob"]

    def test_invoicing_and_monarch_per_user(self, tmp_path):
        from istota.money.cli import load_context
        user_dir = tmp_path / "userdata"
        user_dir.mkdir()
        (user_dir / "invoicing.toml").write_text("")
        (user_dir / "monarch.toml").write_text("")
        config = tmp_path / "config.toml"
        config.write_text(
            f'[users.test]\n'
            f'data_dir = "{user_dir}"\n'
            f'invoicing_config = "invoicing.toml"\n'
            f'monarch_config = "monarch.toml"\n'
        )
        ctx = load_context(config_path=str(config))
        uctx = ctx.users["test"]
        assert uctx.invoicing_config_path == user_dir / "invoicing.toml"
        assert uctx.monarch_config_path == user_dir / "monarch.toml"


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
