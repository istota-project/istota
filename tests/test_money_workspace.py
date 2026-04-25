"""Tests for money workspace-mode config loading and the migration script."""

from pathlib import Path

import pytest

pytest.importorskip("money", reason="money extra not installed")
pytest.importorskip("beancount", reason="money requires beancount")


from money._config_io import read_toml_config
from money.workspace import (
    INVOICING_FILENAME,
    MONARCH_FILENAME,
    TAX_FILENAME,
    list_workspace_features,
    synthesize_user_context,
)


# ---------------------------------------------------------------------------
# read_toml_config — handles plain TOML and UPPERCASE.md
# ---------------------------------------------------------------------------


class TestReadTomlConfig:
    def test_plain_toml(self, tmp_path):
        p = tmp_path / "x.toml"
        p.write_text('foo = "bar"\n')
        assert read_toml_config(p) == {"foo": "bar"}

    def test_md_with_toml_block(self, tmp_path):
        p = tmp_path / "X.md"
        p.write_text("# Title\n\n```toml\nfoo = \"bar\"\n```\n")
        assert read_toml_config(p) == {"foo": "bar"}

    def test_md_picks_first_toml_block(self, tmp_path):
        p = tmp_path / "X.md"
        p.write_text(
            "# Title\n\n```toml\nfoo = \"first\"\n```\n\n"
            "Second block:\n\n```toml\nfoo = \"second\"\n```\n"
        )
        assert read_toml_config(p) == {"foo": "first"}

    def test_md_without_toml_block_raises(self, tmp_path):
        p = tmp_path / "X.md"
        p.write_text("# Title\n\nno toml here\n")
        with pytest.raises(ValueError, match="No ```toml code block"):
            read_toml_config(p)


# ---------------------------------------------------------------------------
# Workspace synthesizer
# ---------------------------------------------------------------------------


class TestSynthesizeUserContext:
    def test_empty_workspace_yields_no_feature_paths(self, tmp_path):
        ctx = synthesize_user_context(tmp_path)
        assert ctx.invoicing_config_path is None
        assert ctx.tax_config_path is None
        assert ctx.monarch_config_path is None
        assert ctx.data_dir == (tmp_path / "money").resolve()
        assert ctx.db_path == ctx.data_dir / "moneyman.db"

    def test_md_files_picked_up(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / INVOICING_FILENAME).write_text("# Inv\n```toml\nx=1\n```\n")
        (config_dir / TAX_FILENAME).write_text("# Tax\n```toml\nx=1\n```\n")
        (config_dir / MONARCH_FILENAME).write_text("# Mon\n```toml\nx=1\n```\n")
        ctx = synthesize_user_context(tmp_path)
        assert ctx.invoicing_config_path == config_dir / INVOICING_FILENAME
        assert ctx.tax_config_path == config_dir / TAX_FILENAME
        assert ctx.monarch_config_path == config_dir / MONARCH_FILENAME

    def test_legacy_toml_fallback(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "invoicing.toml").write_text("x = 1\n")
        (config_dir / "tax.toml").write_text("x = 1\n")
        (config_dir / "monarch.toml").write_text("x = 1\n")
        ctx = synthesize_user_context(tmp_path)
        assert ctx.invoicing_config_path == config_dir / "invoicing.toml"
        assert ctx.tax_config_path == config_dir / "tax.toml"
        assert ctx.monarch_config_path == config_dir / "monarch.toml"

    def test_md_takes_precedence_over_legacy(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / INVOICING_FILENAME).write_text("# Inv\n```toml\nx=1\n```\n")
        (config_dir / "invoicing.toml").write_text("x = 2\n")
        ctx = synthesize_user_context(tmp_path)
        assert ctx.invoicing_config_path == config_dir / INVOICING_FILENAME

    def test_data_dir_override(self, tmp_path):
        custom = tmp_path / "elsewhere"
        ctx = synthesize_user_context(tmp_path, data_dir=custom)
        assert ctx.data_dir == custom.resolve()
        assert ctx.db_path == custom.resolve() / "moneyman.db"

    def test_list_workspace_features(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / INVOICING_FILENAME).write_text("```toml\nx=1\n```\n")
        (config_dir / "monarch.toml").write_text("x = 1\n")
        feats = list_workspace_features(tmp_path)
        assert feats == {"invoicing": True, "tax": False, "monarch": True}


# ---------------------------------------------------------------------------
# Parsers accept .md (smoke tests via real parsers)
# ---------------------------------------------------------------------------


class TestParsersAcceptMd:
    def test_invoicing_md(self, tmp_path):
        from money.core.invoicing import parse_invoicing_config
        p = tmp_path / "INVOICING.md"
        p.write_text(
            "# Inv\n\n"
            "```toml\n"
            "[company]\n"
            'name = "Acme"\n'
            'address = "1 Way"\n'
            'email = "x@y.z"\n'
            'tax_id = "0"\n'
            "```\n"
        )
        cfg = parse_invoicing_config(p)
        assert cfg.company.name == "Acme"

    def test_monarch_md(self, tmp_path):
        from money.core.transactions import parse_monarch_config
        p = tmp_path / "MONARCH.md"
        p.write_text(
            "# Mon\n\n```toml\n[monarch]\nemail = \"x@y.z\"\n```\n"
        )
        cfg = parse_monarch_config(p)
        assert cfg.credentials.email == "x@y.z"

    def test_tax_md(self, tmp_path):
        from money.core.tax import parse_tax_config
        p = tmp_path / "TAX.md"
        p.write_text(
            "# Tax\n\n```toml\n[tax]\nfiling_status = \"single\"\n```\n"
        )
        cfg = parse_tax_config(p)
        assert cfg.filing_status == "single"


# ---------------------------------------------------------------------------
# Migration script
# ---------------------------------------------------------------------------


class TestMigrationScript:
    def _import_script(self):
        import importlib.util
        repo_root = Path(__file__).resolve().parent.parent
        script_path = repo_root / "scripts" / "migrate_money_workspace_config.py"
        spec = importlib.util.spec_from_file_location("_mig", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_migrate_writes_md_files(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        (src / "invoicing.toml").write_text('name = "Acme"\n')
        (src / "tax.toml").write_text('filing_status = "single"\n')
        (src / "monarch.toml").write_text('[monarch]\nemail = "x@y.z"\n')
        mod = self._import_script()
        n = mod.migrate(src, dst, dry_run=False)
        assert n == 3
        for fn, expected_content in (
            ("INVOICING.md", 'name = "Acme"'),
            ("TAX.md", 'filing_status = "single"'),
            ("MONARCH.md", 'email = "x@y.z"'),
        ):
            text = (dst / fn).read_text()
            assert "```toml" in text
            assert expected_content in text

    def test_migrate_skips_missing_files(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        (src / "tax.toml").write_text('filing_status = "single"\n')
        mod = self._import_script()
        n = mod.migrate(src, dst, dry_run=False)
        assert n == 1
        assert (dst / "TAX.md").exists()
        assert not (dst / "INVOICING.md").exists()

    def test_idempotent_on_already_migrated_md(self, tmp_path):
        """If the source already has TOML embedded in markdown, extract and re-render cleanly."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        (src / "invoicing.toml").write_text(
            "# Old md format\n\n```toml\nname = \"Acme\"\n```\n"
        )
        mod = self._import_script()
        mod.migrate(src, dst, dry_run=False)
        text = (dst / "INVOICING.md").read_text()
        # Should not double-wrap
        assert text.count("```toml") == 1
        assert 'name = "Acme"' in text

    def test_migrate_output_is_loadable(self, tmp_path):
        """Migrated MONARCH.md round-trips through parse_monarch_config."""
        from money.core.transactions import parse_monarch_config
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        (src / "monarch.toml").write_text('[monarch]\nemail = "x@y.z"\n')
        mod = self._import_script()
        mod.migrate(src, dst, dry_run=False)
        cfg = parse_monarch_config(dst / "MONARCH.md")
        assert cfg.credentials.email == "x@y.z"

    def test_dry_run_writes_nothing(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        (src / "tax.toml").write_text('filing_status = "single"\n')
        mod = self._import_script()
        mod.migrate(src, dst, dry_run=True)
        assert not dst.exists() or not any(dst.iterdir())
