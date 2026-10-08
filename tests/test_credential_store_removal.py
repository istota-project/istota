"""Shared files are migration inputs, never live credential stores."""
import importlib.util
from istota import db
from tests import test_vault_retire
configured = test_vault_retire.configured
from tests.test_kdbx_import import seed
from istota.credentials import store


def test_old_vault_module_is_removed():
    assert importlib.util.find_spec("istota.credentials.vault") is None
    from istota.credentials import names, kdbx_import
    assert names.slug_name(("Example Site",)) == "example_site"
    assert callable(kdbx_import.parse_vault)


def test_init_then_retirement_preserves_final_import(configured):
    from istota.credentials import vault_retire
    config, _ = configured
    seed(config.db_path, "changed", "old", source="vault")
    seed(config.db_path, "local", "keep")
    db.init_db(config.db_path)
    assert vault_retire.retire_user(config, "alice").outcome == "ok"
    assert store.get_secret(config.db_path, "alice", "vault_entries", "changed") == "fixture-value"
    assert store.get_secret(config.db_path, "alice", "vault_entries", "local") == "keep"
    seed(config.db_path, "late", "rolling-restart", source="vault")
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT source FROM credential_bindings WHERE name='late'").fetchone()[0] == "local"


def test_unconfigured_legacy_rows_are_retired(tmp_path, monkeypatch):
    from istota.config import Config, UserConfig
    from istota.credentials import vault_retire
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    path = tmp_path / "test.db"
    db.init_db(path)
    seed(path, "old", "keep", source="vault")
    config = Config(db_path=path, users={"alice": UserConfig()})
    assert vault_retire.pending_users(config) == ["alice"]
    assert vault_retire.retire_user(config, "alice").outcome == "skipped"
    with db.get_db(path) as conn:
        assert conn.execute("SELECT source FROM credential_bindings WHERE name='old'").fetchone()[0] == "local"
    assert store.get_secret(path, "alice", "vault_entries", "old") == "keep"


def test_password_policy_survives_writer_removal():
    import pytest
    from istota.credentials.names import PasswordPolicy, generate_password, VaultWriteRefused
    password = generate_password(PasswordPolicy(length=8, require_symbols=False, allow_symbols=False))
    assert len(password) == 8 and password.isalnum()
    assert any(c.islower() for c in password)
    assert any(c.isupper() for c in password)
    assert any(c.isdigit() for c in password)
    with pytest.raises(VaultWriteRefused):
        generate_password(PasswordPolicy(length=2))
    with pytest.raises(VaultWriteRefused):
        generate_password(PasswordPolicy(require_symbols=True, allow_symbols=False))
