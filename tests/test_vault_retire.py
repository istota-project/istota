"""The last automatic import must never delete credentials or alter the file."""
from istota.credentials import kdbx_import as credential_read
from istota.credentials import vault_retire as credential_migration
import hashlib
import json
import pytest
from istota import db
from istota.config import Config, UserConfig, load_config
from istota.credentials import store
from istota.credentials.broker import bindings
from tests.test_kdbx_import import kdbx, seed, PASSPHRASE

@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    path = tmp_path / "test.db"
    db.init_db(path)
    root = tmp_path / "mount"
    folder = root / "Users/alice"
    folder.mkdir(parents=True)
    file = folder / "credentials.kdbx"
    file.write_bytes(kdbx([{"title": "new"}, {"title": "changed"}, {"title": "local"},
                          {"title": "account", "generated": True}]))
    config = Config(db_path=path, workspace_path=root, temp_dir=tmp_path / "tmp",
                    users={"alice": UserConfig(vault_path="credentials.kdbx")})
    store.set_secret(path, "alice", "vault", "passphrase", PASSPHRASE)
    return config, file


def test_last_import_keeps_missing_rows_and_local_values(configured, caplog):
    from istota.credentials import vault_retire
    config, file = configured
    seed(config.db_path, "changed", "previous", source="vault")
    seed(config.db_path, "changed_username", "keep-field", source="vault", owner="changed")
    seed(config.db_path, "missing", "keep-entry", source="vault")
    seed(config.db_path, "local", "keep-local")
    with db.get_db(config.db_path) as conn:
        for ns in ("_vault_sync", "_vault_file", "_generated_credentials"):
            db.kv_set(conn, "alice", ns, "fixture", "1")
    before = hashlib.sha256(file.read_bytes()).digest()
    result = vault_retire.retire_user(config, "alice", now=1000)
    assert result.outcome == "ok"
    assert set(result.imported) == {"new", "changed", "generated_account"}
    for name, value in {"changed": "fixture-value", "new": "fixture-value",
                        "changed_username": "keep-field", "missing": "keep-entry",
                        "local": "keep-local"}.items():
        assert store.get_secret(config.db_path, "alice", "vault_entries", name) == value
    assert not store.secret_exists(config.db_path, "alice", "vault", "passphrase")
    with db.get_db(config.db_path) as conn:
        assert bindings.get_binding(conn, "alice", "missing")["source"] == "local"
        for ns in ("_vault_sync", "_vault_file", "_generated_credentials"):
            assert not db.kv_list(conn, "alice", ns)
        marker = json.loads(db.kv_get(conn, "alice", "_credential_migration", "vault_retired")["value"])
        assert marker["at"] == 1000
        assert conn.execute("SELECT count(*) FROM notifications WHERE dedup_key='vault-sync-retired'").fetchone()[0] == 1
        assert PASSPHRASE not in "\n".join(conn.iterdump())
    assert PASSPHRASE not in caplog.text
    assert hashlib.sha256(file.read_bytes()).digest() == before
    assert vault_retire.retire_user(config, "alice").outcome == "already_retired"
    assert not vault_retire.pending_users(config)


@pytest.mark.parametrize("failure", [credential_read.VaultLocked, credential_read.VaultCorrupt,
    credential_migration.VaultPassphraseMissing, credential_migration.VaultKeyUnusable, credential_migration.VaultPathRefused, credential_read.VaultLibraryMissing])
def test_permanent_failures_retire_without_exception_text(configured, monkeypatch, failure, caplog):
    from istota.credentials import vault_retire
    config, _ = configured
    def fail(*args, **kwargs):
        raise failure(PASSPHRASE)
    monkeypatch.setattr(credential_migration, "read_vault_bytes", fail)
    result = vault_retire.retire_user(config, "alice", now=1000)
    assert result.outcome == "skipped"
    assert result.reason == failure.__name__
    assert not store.secret_exists(config.db_path, "alice", "vault", "passphrase")
    with db.get_db(config.db_path) as conn:
        assert PASSPHRASE not in "\n".join(conn.iterdump())
    assert PASSPHRASE not in caplog.text


@pytest.mark.parametrize("failure", [credential_migration.VaultMissing, credential_migration.VaultUnreadable, OSError])
def test_transient_failures_retry_for_seven_days(configured, monkeypatch, failure):
    from istota.credentials import vault_retire
    config, _ = configured
    def fail(*args, **kwargs):
        raise failure("unavailable")
    monkeypatch.setattr(credential_migration, "read_vault_bytes", fail)
    assert vault_retire.retire_user(config, "alice", now=1000).outcome == "retry"
    assert vault_retire.retire_user(config, "alice", now=1000 + 7*86400 - 1).outcome == "retry"
    assert store.secret_exists(config.db_path, "alice", "vault", "passphrase")
    assert vault_retire.pending_users(config) == ["alice"]
    assert vault_retire.retire_user(config, "alice", now=1000 + 7*86400).outcome == "skipped"
    assert not vault_retire.pending_users(config)


def test_retired_config_keys_warn_but_stay_parseable(tmp_path, caplog):
    path = tmp_path / "config.toml"
    path.write_text('[scheduler]\nvault_sync_interval = 0\n[users.alice]\nvault_path = "credentials.kdbx"\n')
    config = load_config(path)
    assert config.users["alice"].vault_path == "credentials.kdbx"
    for key in ("vault_sync_interval", "vault_path"):
        assert any(key in r.message and "retired" in r.message for r in caplog.records)


def test_retry_recovers_and_does_not_replace_generated_values(configured):
    from istota.credentials import generated, vault_retire
    config, file = configured
    data = file.read_bytes()
    file.unlink()
    assert vault_retire.retire_user(config, "alice", now=1000).outcome == "retry"
    file.write_bytes(data)
    with db.get_db(config.db_path) as conn:
        generated.create(conn, "alice", name="generated_account", username="", password="keep-generated", url="")
    assert vault_retire.retire_user(config, "alice", now=1001).outcome == "ok"
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_account") == "keep-generated"


def test_generated_updates_and_retirement_never_touch_file_or_queue_a_mirror(configured):
    from istota.credentials import generated
    from tests.test_kdbx_import import OTP
    config, file = configured
    before = file.read_bytes()
    with db.get_db(config.db_path) as conn:
        generated.create(conn, "alice", name="generated_created", username="", password="created-value", url="")
        generated.set_otp(conn, "alice", "generated_created", OTP)
        generated.set_recovery(conn, "alice", "generated_created", "first-code")
        assert not db.kv_list(conn, "alice", "_generated_credentials")
    assert generated.retire(config, "alice", "generated_created")
    assert file.read_bytes() == before
    with db.get_db(config.db_path) as conn:
        assert not db.kv_list(conn, "alice", "_generated_credentials")


def test_retirement_closes_connected_service_notice(configured):
    from istota.credentials import vault_retire
    from istota.notifications.resolvers import connected_service
    config, _ = configured
    with db.get_db(config.db_path) as conn:
        connected_service.write(conn, "alice", service="vault", reason="locked")
    vault_retire.retire_user(config, "alice")
    with db.get_db(config.db_path) as conn:
        row = conn.execute("SELECT state FROM notifications WHERE source='connected_service'").fetchone()
        assert row["state"] == "resolved"


def test_final_import_does_not_resurrect_a_retired_generated_copy(configured):
    from istota.credentials import vault_retire
    config, _ = configured
    with db.get_db(config.db_path) as conn:
        db.kv_set(conn, "alice", "_generated_credentials", "generated_account", '{"state":"retired"}')
    result = vault_retire.retire_user(config, "alice")
    assert "generated_account" not in result.imported
    assert not store.secret_exists(config.db_path, "alice", "vault_entries", "generated_account")


def test_failed_finalization_rolls_back_the_import_and_leaves_retry_possible(configured, monkeypatch):
    from istota.credentials import audit, vault_retire
    config, _ = configured
    original = audit.record
    def fail(conn, user_id, **kwargs):
        if kwargs["action"] == "migration":
            raise RuntimeError("failed finalization")
        return original(conn, user_id, **kwargs)
    monkeypatch.setattr(audit, "record", fail)
    with pytest.raises(RuntimeError):
        vault_retire.retire_user(config, "alice")
    assert not store.secret_exists(config.db_path, "alice", "vault_entries", "new")
    assert store.secret_exists(config.db_path, "alice", "vault", "passphrase")
    assert vault_retire.pending_users(config) == ["alice"]


def test_unavailable_mount_retries_without_discarding_passphrase(configured):
    from istota.credentials import vault_retire
    config, file = configured
    file.parent.rename(file.parent.with_name("unmounted"))
    result = vault_retire.retire_user(config, "alice", now=1000)
    assert result.outcome == "retry"
    assert store.secret_exists(config.db_path, "alice", "vault", "passphrase")
