"""Credentials Istota generates live in the secrets table (ISSUE-686).

`new` and `otp-set` write the table; the KeePass file is an optional one-way
mirror, and a sync never deletes or overwrites a generated entry from it.
"""
from istota.credentials import kdbx_import as credential_read

import io

import pytest
from pykeepass import PyKeePass

from istota import db
from istota.config import Config, UserConfig
from istota.credentials import generated
from istota.credentials import store
from istota.credentials.broker import bindings, grants
from istota.lib import totp
from istota.sandbox.skill_proxy import SkillProxy
from tests import test_skill_proxy_vault_create as _vault_create
from tests.support.kdbx import create_database
from tests.test_skill_proxy_otp import ask
from tests.test_skill_proxy_vault_create import _request

sock = _vault_create.sock

SEED = "JBSWY3DP" * 4
PASSPHRASE = "test-passphrase"


def _config(tmp_path, monkeypatch, *, with_vault):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "vault.kdbx"
    user = UserConfig(email_addresses=["alice@example.com"])
    if with_vault:
        create_database(str(path), password=PASSPHRASE)
        user = UserConfig(vault_path=str(path), email_addresses=["alice@example.com"])
    config = Config(db_path=tmp_path / "daemon" / "test.db", workspace_path=tmp_path / "workspace",
                    users={"alice": user})
    config.db_path.parent.mkdir()
    db.init_db(config.db_path)
    if with_vault:
        store.upsert_secret(config.db_path, "alice", "vault", "passphrase", PASSPHRASE)
    monkeypatch.setattr("istota.notifications.store.deliver_pending", lambda *_: None)
    return config, path


def _task(config, room="room-a"):
    with db.get_db(config.db_path) as conn:
        return db.create_task(conn, user_id="alice", prompt="sign up", source_type="talk",
                              conversation_token=room)


def _file_entries(path):
    read = credential_read.parse_vault(path.read_bytes(), PASSPHRASE)
    return read.generated


def test_new_and_otp_set_work_with_no_vault(tmp_path, monkeypatch, sock):
    config, path = _config(tmp_path, monkeypatch, with_vault=False)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2) as proxy:
        created = _request(sock, {"type": "vault_create", "slug": "acme", "url": "https://acme.example"})
        assert created["name"] == "generated_acme"
        assert _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED}) == {
            "name": "generated_acme", "otp": True}
        password = proxy.vault_credentials["generated_acme"]
    assert not path.exists()
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme") == password
    with db.get_db(config.db_path) as conn:
        assert bindings.get_binding(conn, "alice", "generated_acme")["source"] == generated.SOURCE
        seed = bindings.get_binding(conn, "alice", "generated_acme_totp")
        assert seed["kind"] == "totp" and seed["hosts"] == ["acme.example"]
        assert bindings.credential_name(conn, "alice", "generated_acme_totp") == "generated_acme"
    stored = store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme_totp")
    assert totp.parse_otpauth(stored) == totp.parse_user_input(SEED)


def test_a_later_task_in_the_conversation_fills_otp_by_either_name(tmp_path, monkeypatch, sock):
    config, _ = _config(tmp_path, monkeypatch, with_vault=False)
    config.security.credential_broker.enabled = True
    first = _task(config)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", task_id=str(first),
                    vault_write_limit=2):
        _request(sock, {"type": "vault_create", "slug": "acme", "url": "https://acme.example"})
        _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})
    second = _task(config)
    with db.get_db(config.db_path) as conn:
        grants.ensure_credential_grants(conn, second, "alice")
    snapshot = store.get_service_secrets(config.db_path, "alice", "vault_entries")
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", task_id=str(second),
                    vault_credentials=snapshot) as server:
        for name in ("generated_acme", "generated_acme_totp"):
            assert "code" in ask(server, sock, {"type": "vault_otp", "name": name}, True)


def test_retire_refuses_a_credential_istota_did_not_generate(tmp_path, monkeypatch):
    config, _ = _config(tmp_path, monkeypatch, with_vault=False)
    store.upsert_secret(config.db_path, "alice", "vault_entries", "github", "token",
                        binding=bindings.parse_binding("github.com", {}, [], source="local"))
    with pytest.raises(generated.GeneratedCredentialError):
        generated.retire(config, "alice", "github")
    assert store.get_secret(config.db_path, "alice", "vault_entries", "github") == "token"


def test_the_read_repr_carries_no_generated_value(tmp_path, monkeypatch):
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    kp = PyKeePass(str(path), password=PASSPHRASE)
    kp.add_entry(kp.add_group(kp.root_group, "generated"), "acme", "alice", "secret-value")
    kp.save()
    read = credential_read.parse_vault(io.BytesIO(path.read_bytes()).getvalue(), PASSPHRASE)
    assert "secret-value" not in repr(read)
    assert "generated_acme" not in read.services


def test_deleting_a_member_row_retires_the_whole_credential(tmp_path, monkeypatch):
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    with db.get_db(config.db_path) as conn:
        generated.create(conn, "alice", name="generated_acme", username="u", password="pw",
                         url="https://acme.example")
    assert generated.retire(config, "alice", "generated_acme_url") is True
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme") is None
    assert "generated_acme" not in _file_entries(path)
