"""Tests for istota.cli_money — operator-facing top-level CLI."""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import cli_money
from istota.money import config_store
from istota.money.cli import UserContext


@pytest.fixture
def fake_ctx(tmp_path):
    """A UserContext rooted at tmp_path with an initialised DB."""
    data_dir = tmp_path / "money"
    db_path = data_dir / "data" / "money.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config_store.init_db(db_path)
    return UserContext(data_dir=data_dir, ledgers=[], db_path=db_path)


@pytest.fixture
def patched_loader(fake_ctx):
    """Patch resolve_for_user → fake_ctx so CLI commands skip module gating."""
    with patch.object(
        cli_money, "_load_user_ctx", return_value=fake_ctx,
    ):
        yield fake_ctx


def _run(argv: list[str], istota_config=None) -> tuple[int, str, str]:
    """Build the argparse subparser, dispatch, capture output."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    cli_money.add_subparser(sub)
    args = parser.parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_money.dispatch(args, istota_config) or 0
    return rc, out.getvalue(), err.getvalue()


class TestClientMutations:
    def test_add_client_create_then_noop(self, patched_loader):
        rc, out, _ = _run([
            "money", "client", "add", "--user", "u1", "--key", "acme",
            "--name", "Acme Corp",
        ])
        assert rc == 0
        assert "STATE: created client key=acme" in out

        rc, out, _ = _run([
            "money", "client", "add", "--user", "u1", "--key", "acme",
            "--name", "Acme Corp",
        ])
        assert "STATE: noop" in out

    def test_update_client(self, patched_loader):
        _run(["money", "client", "add", "--user", "u1", "--key", "acme", "--name", "Acme"])
        rc, out, _ = _run([
            "money", "client", "update", "--user", "u1", "--key", "acme",
            "--terms", "NET 15",
        ])
        assert "STATE: updated" in out
        cfg = config_store.load_invoicing(patched_loader.db_path)
        assert cfg.clients["acme"].terms == "NET 15"

    def test_remove_client(self, patched_loader):
        _run(["money", "client", "add", "--user", "u1", "--key", "acme", "--name", "Acme"])
        rc, out, _ = _run([
            "money", "client", "remove", "--user", "u1", "--key", "acme",
        ])
        assert "STATE: removed" in out
        rc, out, _ = _run([
            "money", "client", "remove", "--user", "u1", "--key", "acme",
        ])
        assert "STATE: noop" in out

    def test_separate_json(self, patched_loader):
        rc, out, _ = _run([
            "money", "client", "add", "--user", "u1", "--key", "acme",
            "--name", "Acme",
            "--separate-json", '["consulting", "training"]',
        ])
        assert "STATE: created" in out
        cfg = config_store.load_invoicing(patched_loader.db_path)
        assert cfg.clients["acme"].separate == ["consulting", "training"]


class TestCompanyMutations:
    def test_company_lifecycle(self, patched_loader):
        rc, out, _ = _run([
            "money", "company", "add", "--user", "u1", "--key", "ochotona",
            "--name", "Ochotona",
        ])
        assert "STATE: created" in out
        rc, out, _ = _run([
            "money", "company", "remove", "--user", "u1", "--key", "ochotona",
        ])
        assert "STATE: removed" in out


class TestServiceMutations:
    def test_service_create(self, patched_loader):
        rc, out, _ = _run([
            "money", "service", "add", "--user", "u1", "--key", "consulting",
            "--display-name", "Consulting", "--rate", "150", "--type", "hours",
        ])
        assert "STATE: created" in out
        cfg = config_store.load_invoicing(patched_loader.db_path)
        assert cfg.services["consulting"].rate == 150.0


class TestTax:
    def test_tax_set(self, patched_loader):
        rc, out, _ = _run([
            "money", "tax", "set", "--user", "u1",
            "--tax-year", "2026", "--filing-status", "mfj",
            "--w2-income", "80000",
        ])
        assert "STATE: updated" in out
        loaded = config_store.load_tax(patched_loader.db_path)
        assert loaded.tax_year == 2026
        assert loaded.w2_income == 80000

    def test_tax_set_noop(self, patched_loader):
        # Initial set produces an "updated" state (it's writing for the first time
        # against the loaded defaults).
        _run([
            "money", "tax", "set", "--user", "u1",
            "--tax-year", "2026", "--w2-income", "80000",
        ])
        # Re-running with no field changes against the loaded snapshot is a noop.
        rc, out, _ = _run([
            "money", "tax", "set", "--user", "u1",
        ])
        assert "STATE: noop" in out

    def test_tax_rates_set(self, patched_loader):
        rc, out, _ = _run([
            "money", "tax", "rates", "set", "--user", "u1", "--year", "2026",
            "--federal-standard-deduction", "30000",
            "--ca-standard-deduction", "10726",
            "--ss-wage-base", "176100",
            "--ss-rate", "0.124",
            "--federal-brackets-json", "[[0, 0.1], [23850, 0.12]]",
        ])
        assert "STATE: created" in out
        rates = config_store.list_tax_year_rates(patched_loader.db_path)
        assert rates[0]["tax_year"] == 2026
        assert rates[0]["federal_brackets"] == [[0, 0.1], [23850, 0.12]]

    def test_tax_pattern_add_remove(self, patched_loader):
        rc, out, _ = _run([
            "money", "tax", "pattern", "add", "--user", "u1",
            "--kind", "se_income", "--pattern", "Income:Side",
        ])
        assert "STATE: created" in out
        rc, out, _ = _run([
            "money", "tax", "pattern", "add", "--user", "u1",
            "--kind", "se_income", "--pattern", "Income:Side",
        ])
        assert "STATE: noop" in out
        rc, out, _ = _run([
            "money", "tax", "pattern", "remove", "--user", "u1",
            "--kind", "se_income", "--pattern", "Income:Side",
        ])
        assert "STATE: removed" in out


class TestMonarch:
    def test_profile_lifecycle(self, patched_loader):
        rc, out, _ = _run([
            "money", "monarch", "profile", "add", "--user", "u1",
            "--name", "cynium", "--ledger", "cynium",
        ])
        assert "STATE: created" in out
        rc, out, _ = _run([
            "money", "monarch", "profile", "update", "--user", "u1",
            "--name", "cynium", "--lookback-days", "60",
        ])
        assert "STATE: updated" in out
        rc, out, _ = _run([
            "money", "monarch", "profile", "remove", "--user", "u1",
            "--name", "cynium",
        ])
        assert "STATE: removed" in out

    def test_account_map_set_unset(self, patched_loader):
        _run([
            "money", "monarch", "profile", "add", "--user", "u1",
            "--name", "cynium", "--ledger", "cynium",
        ])
        rc, out, _ = _run([
            "money", "monarch", "account-map", "set", "--user", "u1",
            "--profile", "cynium",
            "--monarch-name", "Cynium Visa",
            "--account", "Liabilities:Visa",
        ])
        assert "STATE: created" in out
        rc, out, _ = _run([
            "money", "monarch", "account-map", "unset", "--user", "u1",
            "--profile", "cynium",
            "--monarch-name", "Cynium Visa",
        ])
        assert "STATE: removed" in out

    def test_global_account_map(self, patched_loader):
        rc, out, _ = _run([
            "money", "monarch", "account-map", "set", "--user", "u1",
            "--global",
            "--monarch-name", "Bank", "--account", "Assets:Bank",
        ])
        assert "STATE: created" in out

    def test_tag_filter_add(self, patched_loader):
        _run([
            "money", "monarch", "profile", "add", "--user", "u1",
            "--name", "cynium", "--ledger", "cynium",
        ])
        rc, out, _ = _run([
            "money", "monarch", "tag-filter", "add", "--user", "u1",
            "--profile", "cynium",
            "--kind", "include", "--tag", "Biz",
        ])
        assert "STATE: created" in out


class TestConfigImportExport:
    def test_export_import_round_trip(self, patched_loader, tmp_path):
        # Seed some data
        _run(["money", "client", "add", "--user", "u1", "--key", "acme",
              "--name", "Acme"])
        _run(["money", "company", "add", "--user", "u1", "--key", "ochotona",
              "--name", "Ochotona"])
        _run(["money", "service", "add", "--user", "u1", "--key", "consulting",
              "--display-name", "Consulting", "--rate", "150"])

        export_path = tmp_path / "exported.toml"
        rc, out, _ = _run([
            "money", "config", "export", "--user", "u1",
            "--section", "invoicing", "--file", str(export_path),
        ])
        assert rc == 0
        assert export_path.exists()
        text = export_path.read_text()
        assert "[clients.acme]" in text
        assert "[companies.ochotona]" in text
        assert "[services.consulting]" in text

    def test_import_dry_run_writes_nothing(self, patched_loader, tmp_path):
        toml_path = tmp_path / "in.toml"
        toml_path.write_text(
            'next_invoice_number = 5\n[clients.foo]\nname = "Foo"\n',
        )
        rc, out, _ = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "invoicing",
            "--dry-run",
        ])
        assert rc == 0
        assert "STATE: created client key=foo" in out
        cfg = config_store.load_invoicing(patched_loader.db_path)
        assert "foo" not in cfg.clients

    def test_import_actually_writes(self, patched_loader, tmp_path):
        toml_path = tmp_path / "in.toml"
        toml_path.write_text('[clients.foo]\nname = "Foo"\n')
        rc, _, _ = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "invoicing",
        ])
        assert rc == 0
        cfg = config_store.load_invoicing(patched_loader.db_path)
        assert "foo" in cfg.clients

    def test_show_toml(self, patched_loader):
        _run(["money", "client", "add", "--user", "u1", "--key", "acme",
              "--name", "Acme"])
        rc, out, _ = _run([
            "money", "config", "show", "--user", "u1", "--section", "invoicing",
        ])
        assert rc == 0
        assert "[clients.acme]" in out
        assert 'name = "Acme"' in out

    def test_diff(self, patched_loader, tmp_path):
        toml_path = tmp_path / "in.toml"
        toml_path.write_text('[clients.foo]\nname = "Foo"\n')
        rc, out, _ = _run([
            "money", "config", "diff", "--user", "u1",
            "--file", str(toml_path),
        ])
        assert rc == 0
        assert "STATE: created client key=foo" in out


# =============================================================================
# Regression tests from mulder/scully review
# =============================================================================


class TestStrictMode:
    """Scully Bug 1: --strict actually rejects unknown TOML keys."""

    def test_strict_rejects_unknown_key(self, patched_loader, tmp_path):
        toml_path = tmp_path / "in.toml"
        toml_path.write_text(
            'next_invoice_number = 5\n'
            'unknown_top_key = "X"\n'
            '[clients.foo]\nname = "Foo"\n'
        )
        rc, out, err = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "invoicing", "--strict",
        ])
        assert rc == 2
        assert "unknown" in err.lower()

    def test_non_strict_warns_but_succeeds(self, patched_loader, tmp_path):
        toml_path = tmp_path / "in.toml"
        toml_path.write_text(
            '[clients.foo]\nname = "Foo"\nbogus_field = 1\n'
        )
        rc, out, err = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "invoicing",
        ])
        assert rc == 0
        assert "warning" in err.lower()


class TestMergeModePreservesScalars:
    """Scully Bug 2: merge-mode import doesn't clobber existing scalars."""

    def test_existing_currency_preserved(self, patched_loader, tmp_path):
        # Seed an existing setting via the API path.
        from istota.money import config_store
        cfg = config_store.load_invoicing(patched_loader.db_path)
        cfg.currency = "EUR"
        cfg.next_invoice_number = 999
        config_store.save_invoicing(patched_loader.db_path, cfg)

        # Import a TOML that doesn't mention currency or next_invoice_number.
        toml_path = tmp_path / "in.toml"
        toml_path.write_text('[clients.foo]\nname = "Foo"\n')
        rc, _, _ = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "invoicing",
        ])
        assert rc == 0
        loaded = config_store.load_invoicing(patched_loader.db_path)
        assert loaded.currency == "EUR"
        assert loaded.next_invoice_number == 999
        assert "foo" in loaded.clients

    def test_replace_mode_does_overwrite(self, patched_loader, tmp_path):
        from istota.money import config_store
        cfg = config_store.load_invoicing(patched_loader.db_path)
        cfg.currency = "EUR"
        config_store.save_invoicing(patched_loader.db_path, cfg)
        toml_path = tmp_path / "in.toml"
        toml_path.write_text('[clients.foo]\nname = "Foo"\n')
        _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "invoicing", "--replace",
        ])
        loaded = config_store.load_invoicing(patched_loader.db_path)
        # --replace truncates collections; scalars not mentioned go back to defaults.
        assert loaded.currency == "USD"

    def test_existing_tax_w2_preserved(self, patched_loader, tmp_path):
        from istota.money import config_store
        existing = config_store.load_tax(patched_loader.db_path)
        existing.tax_year = 2026
        existing.w2_income = 80000
        existing.filing_status = "single"
        config_store.save_tax(patched_loader.db_path, existing)

        toml_path = tmp_path / "in.toml"
        toml_path.write_text('[tax]\ntax_year = 2027\n')
        rc, _, _ = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "tax",
        ])
        assert rc == 0
        loaded = config_store.load_tax(patched_loader.db_path)
        assert loaded.tax_year == 2027
        # Untouched
        assert loaded.w2_income == 80000
        assert loaded.filing_status == "single"

    def test_existing_monarch_sync_preserved(self, patched_loader, tmp_path):
        from istota.money import config_store
        existing = config_store.load_monarch(patched_loader.db_path)
        existing.sync.lookback_days = 99
        config_store.save_monarch(patched_loader.db_path, existing)

        toml_path = tmp_path / "in.toml"
        toml_path.write_text(
            '[monarch.profiles.cynium]\nledger = "cynium"\n'
        )
        rc, _, _ = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "monarch",
        ])
        assert rc == 0
        loaded = config_store.load_monarch(patched_loader.db_path)
        assert loaded.sync.lookback_days == 99
        assert any(p.name == "cynium" for p in loaded.profiles)


class TestCombinedImport:
    """Mulder P1 #3: combined-form file with [invoicing][tax][monarch] wrappers."""

    def test_combined_imports_invoicing(self, patched_loader, tmp_path):
        from istota.money import config_store
        toml_path = tmp_path / "combined.toml"
        toml_path.write_text(
            '[invoicing]\n'
            '[invoicing.clients.foo]\nname = "Foo"\n'
            '[tax]\ntax_year = 2027\n'
            '[monarch.profiles.x]\nledger = "x"\n'
        )
        rc, out, _ = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path),
        ])
        assert rc == 0
        # All three sections processed.
        assert "section=invoicing" in out
        assert "section=tax" in out
        assert "section=monarch" in out
        cfg = config_store.load_invoicing(patched_loader.db_path)
        assert "foo" in cfg.clients
        tax = config_store.load_tax(patched_loader.db_path)
        assert tax.tax_year == 2027
        mon = config_store.load_monarch(patched_loader.db_path)
        assert any(p.name == "x" for p in mon.profiles)

    def test_combined_with_both_forms_errors(self, patched_loader, tmp_path):
        toml_path = tmp_path / "ambiguous.toml"
        toml_path.write_text(
            '[invoicing]\n'
            '[invoicing.clients.foo]\nname = "Foo"\n'
            '[clients.bar]\nname = "Bar"\n'  # bare clients alongside [invoicing]
        )
        rc, _, err = _run([
            "money", "config", "import", "--user", "u1",
            "--file", str(toml_path), "--section", "invoicing",
        ])
        assert rc == 2
        assert "wrapper" in err.lower() or "bare" in err.lower()


class TestOperationalPassthrough:
    """`istota money <op>` forwards accounting operations to the money Click tree."""

    def test_pop_user_space_separated(self):
        user, rest = cli_money._pop_user(["generate", "-u", "alice", "--dry-run"])
        assert user == "alice"
        assert rest == ["generate", "--dry-run"]

    def test_pop_user_long_and_equals(self):
        assert cli_money._pop_user(["list", "--user", "bob"]) == ("bob", ["list"])
        assert cli_money._pop_user(["list", "--user=bob"]) == ("bob", ["list"])

    def test_pop_user_attached_short(self):
        assert cli_money._pop_user(["list", "-ubob"]) == ("bob", ["list"])

    def test_pop_user_absent(self):
        assert cli_money._pop_user(["list", "--ledger", "main"]) == (
            None, ["list", "--ledger", "main"],
        )

    def test_operational_requires_user(self):
        rc, _, err = _run(["money", "list"])
        assert rc == 2
        assert "--user/-u is required" in err

    def test_forwards_command_and_strips_user(self, patched_loader):
        """The subcommand name + remaining args reach the Click tree; -u is pulled out."""
        captured = {}

        def fake_invoke(istota_config, user_id, click_args):
            captured["user"] = user_id
            captured["args"] = click_args
            return 0

        with patch.object(cli_money, "_invoke_money_cli", side_effect=fake_invoke):
            rc, _, _ = _run([
                "money", "invoice", "generate", "-u", "alice", "--dry-run",
            ])
        assert rc == 0
        assert captured["user"] == "alice"
        assert captured["args"] == ["invoice", "generate", "--dry-run"]

    def test_end_to_end_work_list_empty(self, patched_loader):
        """A real forward through the Click tree returns JSON for an empty workspace."""
        import json

        rc, out, err = _run(["money", "work", "list", "-u", "u1"], istota_config=object())
        assert rc == 0, err
        payload = json.loads(out)
        assert payload["status"] == "ok"

    def test_dispatch_operational_options_first(self, patched_loader):
        """The main() peel path: command + options-first args, -u extracted."""
        captured = {}

        def fake_invoke(istota_config, user_id, click_args):
            captured["user"] = user_id
            captured["args"] = click_args
            return 0

        with patch.object(cli_money, "_invoke_money_cli", side_effect=fake_invoke):
            rc = cli_money.dispatch_operational(
                "list", ["-u", "alice", "--account", "Foo"], object(),
            )
        assert rc == 0
        assert captured["user"] == "alice"
        assert captured["args"] == ["list", "--account", "Foo"]

    def test_dispatch_operational_requires_user(self):
        rc = cli_money.dispatch_operational("balances", ["--account", "Foo"], object())
        assert rc == 2

    def test_is_operational(self):
        assert cli_money.is_operational("invoice")
        assert cli_money.is_operational("list")
        assert not cli_money.is_operational("client")
        assert not cli_money.is_operational("config")
