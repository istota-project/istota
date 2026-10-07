"""Set-once enrollment through the real vault and task proxy."""

import io
import json

import pytest
from pykeepass import PyKeePass

from istota import db
from istota.config import Config, UserConfig
from istota.credentials import store, vault
from istota.lib import totp
from istota.sandbox import credential_shim
from istota.sandbox.skill_proxy import SkillProxy
from istota.storage import VaultLocation
from tests.support.kdbx import create_database
from tests.test_skill_proxy_vault_create import _request, sock  # noqa: F401

SEED = "JBSWY3DP" * 4


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "vault.kdbx"
    kp = create_database(str(path), password="test-passphrase")
    group = kp.add_group(kp.root_group, "generated")
    kp.add_entry(group, "acme", "alice", "test-value", url="https://acme.example")
    kp.save()
    config = Config(db_path=tmp_path / "test.db", users={"alice": UserConfig(vault_path=str(path))})
    db.init_db(config.db_path)
    store.upsert_secret(config.db_path, "alice", "vault", "passphrase", "test-passphrase")
    vault.apply_vault(config.db_path, "alice", vault.parse_vault(path.read_bytes(), "test-passphrase"))
    monkeypatch.setattr("istota.notifications.store.deliver_pending", lambda *_: None)
    return config, path


def test_set_writes_a_password_manager_otp_and_imports_it(configured):
    config, path = configured
    write = vault.set_entry_otp(
        VaultLocation(path=path, dir_fd=None), "test-passphrase", name="generated_acme",
        uri=SEED, expected_digest=vault.read_vault_bytes(path)[1],
        lock_root=path.parent, db_path=config.db_path, user_id="alice",
    )
    assert write.name == "generated_acme"
    entry = PyKeePass(str(path), password="test-passphrase").find_entries(title="acme", first=True)
    assert totp.parse_otpauth(entry.otp) == totp.parse_user_input(SEED)
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme_totp") == entry.otp


@pytest.mark.parametrize("existing", ["otp", "TOTP Seed", "TimeOtp-Secret-Hex", "HmacOtp-Secret", "TOTP Settings"])
def test_any_existing_otp_source_is_set_once(configured, sock, existing):
    config, path = configured
    kp = PyKeePass(str(path), password="test-passphrase")
    entry = kp.find_entries(title="acme", first=True)
    if existing == "otp":
        entry.otp = "malformed"
    else:
        entry.set_custom_property(existing, "")
    kp.save()
    before = path.read_bytes()
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        reply = _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})
    assert reply["reason"] == "otp_already_set"
    assert path.read_bytes() == before


def test_flat_generated_prefix_does_not_authorize_a_write(configured, sock):
    config, path = configured
    kp = PyKeePass(str(path), password="test-passphrase")
    entry = kp.find_entries(title="acme", first=True)
    kp.move_entry(entry, kp.root_group)
    entry.title = "generated_acme"
    kp.save()
    before = path.read_bytes()
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        reply = _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})
    assert reply["reason"] == "otp_set_not_generated"
    assert path.read_bytes() == before


def test_public_write_notice_and_shared_budget(configured, sock, caplog):
    config, path = configured
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2) as proxy:
        reply = _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})
        assert reply == {"name": "generated_acme", "otp": True}
        assert "generated_acme_totp" in proxy.vault_credentials
        assert "generated_acme_totp" not in proxy._created_names
        assert _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})["reason"] == "otp_already_set"
        assert _request(sock, {"type": "vault_create", "slug": "other"})["reason"] == "vault_write_limit"
        assert _request(sock, {"type": "vault_credential", "name": "generated_acme_totp"})["reason"] == "credential_is_otp_seed"
    with db.get_db(config.db_path) as conn:
        notice = conn.execute("SELECT title, severity FROM notifications WHERE source='task_alert'").fetchone()
    assert notice["title"] == "Istota added two-factor to generated acme"
    assert notice["severity"] == "warning"
    assert SEED not in caplog.text


def test_zero_budget_and_invalid_input_leave_the_file(configured, sock):
    config, path = configured
    before = path.read_bytes()
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=0):
        assert _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})["reason"] == "vault_write_limit"
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        reply = _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": "invalid seed!"})
    assert reply["reason"] == "invalid_otp"
    assert "invalid seed!" not in json.dumps(reply)
    assert path.read_bytes() == before


def test_changed_file_retries_once(configured, sock, monkeypatch):
    config, path = configured
    original = vault.set_entry_otp
    calls = []

    def changed_once(*args, **kwargs):
        calls.append(kwargs["expected_digest"])
        if len(calls) == 1:
            raise vault.VaultChanged("changed")
        return original(*args, **kwargs)

    monkeypatch.setattr(vault, "set_entry_otp", changed_once)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        assert _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})["otp"] is True
    assert len(calls) == 2


def test_shim_reads_only_stdin(monkeypatch, capsys):
    seen = []

    def answer(payload, **kwargs):
        seen.append(payload)
        assert kwargs["timeout"] == credential_shim.CREATE_TIMEOUT_SECONDS
        return {"name": "generated_acme", "otp": True}

    monkeypatch.setattr(credential_shim, "_request", answer)
    monkeypatch.setattr("sys.stdin", io.StringIO(SEED + "\n"))
    assert credential_shim.main(["otp-set", "generated_acme"]) == 0
    assert seen == [{"type": "vault_otp_set", "name": "generated_acme", "otp": SEED}]
    assert SEED not in capsys.readouterr().out
    assert credential_shim.main(["otp-set", "generated_acme", SEED]) == credential_shim.EXIT_REFUSED
    assert len(seen) == 1


def test_create_then_enroll_keeps_same_task_access(configured, sock):
    config, path = configured
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2) as proxy:
        created = _request(sock, {"type": "vault_create", "slug": "other", "username": "alice", "url": "https://acme.example"})
        reply = _request(sock, {"type": "vault_otp_set", "name": created["name"], "otp": SEED})
        assert reply == {"name": "generated_other", "otp": True}
        assert "generated_other_totp" in proxy._created_names


def test_failed_import_keeps_committed_file_and_never_reveals_seed(configured, sock, monkeypatch):
    config, path = configured

    def fail_apply(*args):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(vault, "apply_vault", fail_apply)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        reply = _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})
        assert reply == {"name": "generated_acme", "otp": True}
        reply = _request(sock, {"type": "vault_credential", "name": "generated_acme_totp"})
        assert "value" not in reply
    entry = PyKeePass(str(path), password="test-passphrase").find_entries(title="acme", first=True)
    assert totp.parse_otpauth(entry.otp) == totp.parse_user_input(SEED)
