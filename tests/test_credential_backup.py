"""Scheduled backups through the encrypted store and real age library."""
import io
import json
import tarfile
from datetime import date
import pytest
from pykeepass import PyKeePass
from istota import db
from istota.config import Config, UserConfig
from tests.test_kdbx_import import database, seed  # noqa: F401
from tests.test_kdbx_export import cheap  # noqa: F401
# ruff: noqa: F811

@pytest.fixture
def backup(database, cheap, tmp_path, monkeypatch):
    from istota.credentials import backup_export
    import pyrage
    monkeypatch.setattr(cheap, "BACKUP", cheap.INTERACTIVE)
    config = Config(db_path=database, nextcloud_mount_path=tmp_path / "workspace", users={"alice": UserConfig()})
    config.workspace_root("alice").mkdir(parents=True)
    return backup_export, config, pyrage.x25519.Identity.generate()


def register(backup):
    mod, config, identity = backup
    with db.get_db(config.db_path) as conn:
        mod.set_recipient(conn, "alice", str(identity.to_public()), actor="web:alice")


def test_recipient_validation_notice_and_clear(backup):
    mod, config, identity = backup
    recipient = str(identity.to_public())
    assert mod.parse_recipient(" " + recipient + "\n") == recipient
    for value in ("garbage", "ssh-ed25519 AAAA", "", str(identity)):
        with pytest.raises(mod.BackupRecipientError):
            mod.parse_recipient(value)
    register(backup)
    with db.get_db(config.db_path) as conn:
        assert mod.recipient_for(conn, "alice") == recipient
        assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
        assert conn.execute("SELECT action FROM credential_audit").fetchone()[0] == "backup_recipient_set"
        mod.set_recipient(conn, "alice", None, actor="web:alice")
        assert mod.recipient_for(conn, "alice") is None


def test_round_trip_retention_and_no_plaintext(backup, tmp_path, monkeypatch, caplog):
    import pyrage
    mod, config, identity = backup
    assert mod.run_backup(config, "alice").outcome == "skipped"
    register(backup)
    assert mod.run_backup(config, "alice").outcome == "empty"
    seed(config.db_path, "portal", "SENTINEL-value-7f3a")
    seed(config.db_path, "operator", "excluded", source="config")
    from istota.credentials import store
    store.set_secret(config.db_path, "alice", "ntfy", "token", "excluded-service")
    password = "SENTINEL-backup-password-7f3a"
    monkeypatch.setattr(mod.secrets, "token_urlsafe", lambda n: password)
    config.security.credential_backup_retention = 2
    destination = config.workspace_root("alice") / config.bot_dir_name / "exports" / "credential-backups"
    for day in (1, 2, 3):
        result = mod.run_backup(config, "alice", today=date(2026, 10, day))
        assert result.outcome == "ok", result
        (destination / "keep.txt").write_text("unrelated")
    files = sorted(destination.glob("*.age"))
    assert [p.name for p in files] == [f"istota-credentials-2026-10-0{d}.tar.age" for d in (2, 3)]
    encrypted = files[-1].read_bytes()
    assert b"\x03\xd9\xa2\x9a" not in encrypted
    with tarfile.open(fileobj=io.BytesIO(pyrage.decrypt(encrypted, [identity]))) as archive:
        assert set(archive.getnames()) == {"credentials.kdbx", "PASSWORD.txt"}
        pw = archive.extractfile("PASSWORD.txt").read().decode().strip()
        data = archive.extractfile("credentials.kdbx").read()
    kp = PyKeePass(io.BytesIO(data), password=pw)
    assert [(e.title, e.password) for e in kp.entries] == [("portal", "SENTINEL-value-7f3a")]
    assert files[-1].stat().st_mode & 0o777 == 0o600
    assert destination.stat().st_mode & 0o777 == 0o700
    assert (destination / "keep.txt").exists()
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert password.encode() not in p.read_bytes()
    with db.get_db(config.db_path) as conn:
        assert password not in "\n".join(conn.iterdump())
        assert json.loads(db.kv_get(conn, "alice", mod.NAMESPACE, "last_run")["value"])["outcome"] == "ok"
    assert password not in caplog.text


def test_failure_notice_recovery_and_symlink_refusal(backup, tmp_path):
    mod, config, identity = backup
    register(backup)
    seed(config.db_path, "portal", "fixture")
    root = config.workspace_root("alice")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / config.bot_dir_name).symlink_to(outside, target_is_directory=True)
    for _ in range(4):
        assert mod.run_backup(config, "alice").reason == "destination_unavailable"
    assert list(outside.iterdir()) == []
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM notifications WHERE dedup_key='credential-backup-failing' AND state='open'").fetchone()[0] == 1
    (root / config.bot_dir_name).unlink()
    assert mod.run_backup(config, "alice").outcome == "ok"
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM notifications WHERE dedup_key='credential-backup-failing' AND state='open'").fetchone()[0] == 0


def test_reserved_namespace_blocks_task_write(database, monkeypatch, capsys):
    from istota.skills.kv import main
    monkeypatch.setenv("ISTOTA_DB_PATH", str(database))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    with pytest.raises(SystemExit) as exc:
        main(["set", "_credential_backup", "recipient", '"attacker"'])
    assert exc.value.code == 1
    assert "reserved" in capsys.readouterr().out


def test_doctor_staleness_and_gate_seed(backup):
    from istota.doctor import check_credential_backup
    from istota.scheduler import build_interval_gates
    mod, config, _ = backup
    assert check_credential_backup(config, False)[0].status == "skip"
    register(backup)
    assert check_credential_backup(config, False)[0].status == "warn"
    with db.get_db(config.db_path) as conn:
        db.kv_set(conn, "alice", mod.NAMESPACE, "last_run", json.dumps({"at": "2020-01-01T00:00:00+00:00", "outcome": "ok"}))
    assert check_credential_backup(config, False)[0].status == "warn"
    gate = next(g for g in build_interval_gates(config) if g.name == "credential-backup")
    assert gate.field == "credential_backup_interval" and gate.background
    assert gate.seed(config) == 1577836800
    seed(config.db_path, "portal", "fixture")
    assert mod.run_all(config)[0].outcome == "ok"
    assert check_credential_backup(config, False)[0].status == "ok"
