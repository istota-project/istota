"""Credential readiness checks use paths and library presence, never file contents."""
import importlib.util
import pytest
from istota import db, doctor
from istota.config import Config, UserConfig


def test_libraries_name_missing_extra(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    result = doctor.check_credential_library(Config(), False)
    assert result.status == doctor.WARN
    assert "pykeepass" in result.detail and "pyrage" in result.detail
    assert "vault" in result.remedy


@pytest.mark.parametrize("location,status", [("database", doctor.WARN), ("backup", doctor.WARN), ("outside", doctor.OK)])
def test_key_paths_without_reading_contents(tmp_path, monkeypatch, location, status):
    database = tmp_path / "database" / "test.db"
    database.parent.mkdir()
    db.init_db(database)
    config = Config(db_path=database)
    config.scheduler.db_backup_dir = str(tmp_path / "backup")
    folder = tmp_path / location
    folder.mkdir(exist_ok=True)
    key = folder / "istota.env"
    key.write_text("SENTINEL-key-must-not-be-read")
    monkeypatch.setattr(doctor, "_secret_key_env_file", lambda config: key)
    result = doctor.check_secret_key_separation(config, False)
    assert result.status == status
    assert "SENTINEL" not in result.detail
    assert "backups are off" in result.detail


def test_environment_only_key_and_renamed_registry(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    result = doctor.check_secret_key_separation(Config(db_path=path), False)
    assert result.status == doctor.OK
    source = __import__("inspect").getsource(doctor)
    assert '("security.credential_isolation", check_credential_isolation)' in source
    assert '("security.vault_contents",' not in source


def test_multiuser_isolation_without_a_file(monkeypatch):
    config = Config(users={"alice": UserConfig(), "bob": UserConfig()})
    monkeypatch.setattr(doctor, "_deployment_sandboxing", lambda *_: (False, "disabled"))
    assert doctor.check_credential_isolation(config, False).status == doctor.FAIL
    config.security.allow_unsandboxed_multi_user_vaults = True
    assert doctor.check_credential_isolation(config, False).status == doctor.WARN
