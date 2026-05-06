"""Tests for money._migrate — legacy TOML → DB importer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from istota.money import _migrate, config_store
from istota.money.cli import UserContext


INVOICING_TOML = """\
accounting_path = "."
next_invoice_number = 100

[companies.acme]
name = "Acme"

[clients.foo]
name = "Foo Corp"
terms = 30

[services.dev]
display_name = "Dev"
rate = 100
"""


TAX_TOML = """\
[tax]
filing_status = "mfj"
tax_year = 2026

[tax.w2]
income = 50000

[tax.rates]
federal_standard_deduction = 30000
"""


MONARCH_TOML = """\
[monarch.sync]
lookback_days = 45

[monarch.profiles.cynium]
ledger = "cynium"

[monarch.profiles.cynium.tags]
include = ["Biz"]
"""


def _make_ctx(workspace: Path) -> UserContext:
    """Build a UserContext rooted at ``workspace`` with the default layout."""
    data_dir = workspace / "money"
    return UserContext(
        data_dir=data_dir,
        ledgers=[],
        db_path=data_dir / "data" / "money.db",
    )


def _write_workspace_config(workspace: Path, filename: str, body: str) -> Path:
    cfg_dir = workspace / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    target = cfg_dir / filename
    target.write_text(body)
    return target


def _write_data_dir_config(workspace: Path, filename: str, body: str) -> Path:
    cfg_dir = workspace / "money" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    target = cfg_dir / filename
    target.write_text(body)
    return target


class TestEnsureInitialised:
    def test_creates_dirs_and_db(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        _migrate.ensure_initialised(ctx)
        assert (tmp_path / "money" / "data").is_dir()
        assert (tmp_path / "money" / "ledgers").is_dir()
        assert (tmp_path / "money" / "data" / "money.db").is_file()
        assert config_store.get_meta(ctx.db_path, "schema_version") == "1"

    def test_idempotent(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        _migrate.ensure_initialised(ctx)
        _migrate.ensure_initialised(ctx)


class TestMigrateInvoicing:
    def test_imports_workspace_config(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        toml_path = _write_workspace_config(tmp_path, "invoicing.toml", INVOICING_TOML)
        _migrate.ensure_initialised(ctx)

        loaded = config_store.load_invoicing(ctx.db_path)
        assert loaded.next_invoice_number == 100
        assert "foo" in loaded.clients
        assert "dev" in loaded.services

        assert not toml_path.exists()
        renamed = toml_path.with_name("invoicing.toml.imported")
        assert renamed.exists()
        assert config_store.get_meta(
            ctx.db_path, "invoicing_legacy_imported_at",
        ) is not None

    def test_data_dir_config_takes_precedence(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        # Both locations have an invoicing.toml; data_dir wins.
        _write_workspace_config(
            tmp_path, "invoicing.toml",
            'next_invoice_number = 1\n[clients.workspace_only]\nname = "WS"\n',
        )
        _write_data_dir_config(tmp_path, "invoicing.toml", INVOICING_TOML)
        _migrate.ensure_initialised(ctx)
        loaded = config_store.load_invoicing(ctx.db_path)
        assert "foo" in loaded.clients
        assert "workspace_only" not in loaded.clients

    def test_idempotent_doesnt_reimport(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        toml_path = _write_workspace_config(
            tmp_path, "invoicing.toml", INVOICING_TOML,
        )
        _migrate.ensure_initialised(ctx)
        # Drop the .imported marker back to the live name; a second run should
        # see the sentinel and not touch it.
        renamed = toml_path.with_name("invoicing.toml.imported")
        renamed.rename(toml_path)
        toml_path.write_text(
            'next_invoice_number = 999\n[clients.bar]\nname = "Bar"\n'
        )
        _migrate.ensure_initialised(ctx)
        loaded = config_store.load_invoicing(ctx.db_path)
        assert loaded.next_invoice_number == 100
        assert "bar" not in loaded.clients
        # File was left in place because the sentinel is set.
        assert toml_path.exists()


class TestMigrateTax:
    def test_imports(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        _write_workspace_config(tmp_path, "tax.toml", TAX_TOML)
        _migrate.ensure_initialised(ctx)
        loaded = config_store.load_tax(ctx.db_path)
        assert loaded.tax_year == 2026
        assert loaded.w2_income == 50000
        assert loaded.federal_standard_deduction == 30000


class TestMigrateMonarch:
    def test_imports(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        _write_workspace_config(tmp_path, "monarch.toml", MONARCH_TOML)
        _migrate.ensure_initialised(ctx)
        loaded = config_store.load_monarch(ctx.db_path)
        assert loaded.sync.lookback_days == 45
        assert any(p.name == "cynium" for p in loaded.profiles)


class TestPerSectionIndependence:
    def test_only_monarch_present(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        _write_workspace_config(tmp_path, "monarch.toml", MONARCH_TOML)
        _migrate.ensure_initialised(ctx)

        assert config_store.get_meta(
            ctx.db_path, "monarch_legacy_imported_at"
        ) is not None
        assert config_store.get_meta(
            ctx.db_path, "invoicing_legacy_imported_at"
        ) is None
        assert config_store.get_meta(
            ctx.db_path, "tax_legacy_imported_at"
        ) is None

    def test_all_three_present(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        _write_workspace_config(tmp_path, "invoicing.toml", INVOICING_TOML)
        _write_workspace_config(tmp_path, "tax.toml", TAX_TOML)
        _write_workspace_config(tmp_path, "monarch.toml", MONARCH_TOML)
        _migrate.ensure_initialised(ctx)
        for section in ("invoicing", "tax", "monarch"):
            assert config_store.get_meta(
                ctx.db_path, f"{section}_legacy_imported_at",
            ) is not None


class TestDbAlreadyPopulated:
    def test_skips_when_db_has_data(self, tmp_path, caplog):
        ctx = _make_ctx(tmp_path)
        # Pre-populate via the API path.
        config_store.upsert_client(ctx.db_path, "preexisting", name="Pre")
        toml_path = _write_workspace_config(
            tmp_path, "invoicing.toml", INVOICING_TOML,
        )
        with caplog.at_level("WARNING"):
            _migrate.ensure_initialised(ctx)
        assert "money_legacy_present_but_db_populated" in caplog.text
        loaded = config_store.load_invoicing(ctx.db_path)
        assert "preexisting" in loaded.clients
        assert "foo" not in loaded.clients
        # Sentinel NOT set, file NOT renamed.
        assert toml_path.exists()
        assert config_store.get_meta(
            ctx.db_path, "invoicing_legacy_imported_at",
        ) is None


class TestMissingFiles:
    def test_no_files_no_op(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        result = _migrate.migrate_legacy_workspace_config(ctx)
        assert result is None


class TestSentinelLifecycle:
    """Mulder P0: sentinel only set after successful import; lock cleared on failure."""

    def test_sentinel_not_set_when_import_fails(self, tmp_path, monkeypatch):
        """If the importer raises, the real sentinel must NOT be set, so the
        next run can retry instead of being permanently locked out."""
        ctx = _make_ctx(tmp_path)
        toml_path = _write_workspace_config(tmp_path, "invoicing.toml", INVOICING_TOML)

        from istota.money import _migrate, config_store

        def boom(db_path, parsed):
            raise RuntimeError("synthetic save failure")

        monkeypatch.setitem(_migrate._IMPORTERS, "invoicing", boom)
        with pytest.raises(RuntimeError):
            _migrate.ensure_initialised(ctx)

        assert config_store.get_meta(
            ctx.db_path, "invoicing_legacy_imported_at",
        ) is None
        # And the lock has been cleared so a retry is possible.
        assert config_store.get_meta(
            ctx.db_path, "_migrate_lock_invoicing",
        ) is None
        # File still on disk for the retry.
        assert toml_path.exists()

    def test_sentinel_set_after_successful_import(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        _write_workspace_config(tmp_path, "invoicing.toml", INVOICING_TOML)
        from istota.money import config_store
        _migrate.ensure_initialised(ctx)
        assert config_store.get_meta(
            ctx.db_path, "invoicing_legacy_imported_at",
        ) is not None
        assert config_store.get_meta(
            ctx.db_path, "_migrate_lock_invoicing",
        ) is None
